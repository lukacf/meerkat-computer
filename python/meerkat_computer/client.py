"""Synchronous, host-owned computer grants over the existing broker protocol.

``ComputerClient(broker_url, operator_token=..., host_token=None, timeout=150)``
is a context manager; ``close()`` closes HTTP resources only. The host explicitly calls
``attach(*, owner, slot, profile, session_id, runtime_generation, ttl_seconds,
takeover=False)``, ``renew(grant, ttl_seconds)``, and ``release(grant)``. There
is no background renewal, implicit release, retry, takeover, or identity lookup.

``list_tools(grant)`` returns official MCP Tool objects. ``call_tool(grant,
name, arguments, action_id=None)`` returns an official CallToolResult with text,
images, action_id and validated metrics. Confirmed tool rejections set isError.
Unavailable grants raise GrantUnavailable; uncertain effects raise EffectUnknown.
Errors expose only code, effect, status, action_id and validated metrics. Hosts
must retain the action ID and must not automatically repeat an uncertain action.

Use loopback HTTP, including an SSH/IAP tunnel for remote brokers. Operator
credentials authorize attachments, inventory and staged file management;
lease and guest tool methods use the grant's token.
For an always-on demand gateway, set ``broker_url`` to its loopback URL and
pass its separate ``host_token`` from trusted host configuration. The client
sends it as ``X-Meerkat-Computer-Host-Token`` on each request, preserving the
broker's Authorization bearer. Omitting it keeps direct broker behavior.
The gateway owns wake and idle policy; this client has no provider dependency.
Set ``timeout`` to cover the gateway's wake and forwarded operation deadlines.
Gateway errors preserve their declared effect: confirmed failures have no
effect, while uncertain forwarding must never be retried automatically.
Grant repr hides the token, but grants remain credentials and must never be
serialized into model output. Host and operator tokens are also trusted host
credentials, never tool arguments. Optional ``transport`` injection is for HTTP tests.

``ensure_host()`` explicitly asks a configured gateway to wake the broker host;
without ``host_token`` it is a no-op. It neither attaches nor runs a guest action.
``list_computers()`` uses the operator credential to return immutable, token-free
``ComputerView`` snapshots, including the exact owner, slot and optional lease.
It never wakes implicitly or decides whether a computer can be taken over.
A trusted host reconciling an uncertain attachment can call ``ensure_host()``,
then ``list_computers()``, inspect the matching owner/slot/lease, and make its own
explicit decision about a new attachment. Neither call replays the old request.

``stage_file(computer_id, filename, data)`` and ``list_files(computer_id)`` return
immutable ``ComputerFile`` metadata. ``download_file(computer_id, file_id)``
returns export bytes after checking the broker's inventory, length and SHA-256.
``delete_file(computer_id, file_id)`` removes a staged file. These operator-only
methods accept canonical UUIDs, never host paths or URLs. The caller supplies
upload bytes and decides where to write downloaded bytes. Each file is bounded
to 16 MiB, and mutation failures with uncertain outcomes must not be retried.
"""

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
from mcp import types

from .protocol import STALE_INPUT_REASONS

__all__ = [
    "ComputerClient",
    "ComputerClientError",
    "ComputerFile",
    "ComputerGrant",
    "ComputerLease",
    "ComputerView",
    "EffectUnknown",
    "GrantUnavailable",
]

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_MAX_FILE_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITIES = frozenset(
    {
        "browser",
        "desktop",
        "screenshots",
        "browser_semantics",
        "desktop_semantics",
        "exec",
        "files",
        "file_import",
        "file_export",
    }
)
_ERROR_CODES = frozenset(
    {
        "unauthorized",
        "invalid_token",
        "lease_expired",
        "capability_denied",
        "not_found",
        "invalid_request",
        "invalid_config",
        "invalid_input",
        "capacity_exhausted",
        "capacity",
        "storage_error",
        "storage_failure",
        "internal",
        "backend_error",
        "backend_failure",
        "worker_unavailable",
        "host_unavailable",
        "host_asleep",
        "host_busy",
        "host_effect_unknown",
        "conflict",
        "unknown_effect",
        "stale_lease",
        "stale_observation",
    }
)
_COUNTERS = frozenset(
    {
        "duration_ms",
        "clicks",
        "ui_command_attempts",
        "ui_commands",
        "ui_input_rejections",
        "observation_attempts",
        "observations",
        "observation_fallbacks",
        "navigation_attempts",
        "navigations",
        "tab_command_attempts",
        "tab_commands",
        "native_focus_commands",
        "native_invocations",
        "native_text_commands",
        "application_launches",
        "application_launch_attempts",
        "tool_calls_admitted",
        "tool_calls_completed",
        "tool_calls_failed",
        "uncertain_effects",
        "secure_input_attempts",
        "secure_input_completed",
        "secure_fields_filled",
        "file_reads",
        "file_writes",
        "file_bytes_read",
        "file_bytes_written",
        "file_imports",
        "file_imports_bytes",
        "file_exports",
        "file_exports_bytes",
        "exec_commands",
        "exec_timeouts",
    }
) | frozenset("stale_input_" + reason for reason in STALE_INPUT_REASONS)


def _counter(value):
    return type(value) in {int, float} and 0 <= value <= 2**64 - 1 and math.isfinite(value)


def _metrics(value, action_id=None):
    if not isinstance(value, dict):
        return {}
    result = {key: item for key, item in value.items() if key in _COUNTERS and _counter(item)}
    for group in ("delta", "totals"):
        if isinstance(value.get(group), dict):
            result[group] = {
                key: item
                for key, item in value[group].items()
                if key in _COUNTERS and _counter(item)
            }
    if type(value.get("counts_are_lower_bounds")) is bool:
        result["counts_are_lower_bounds"] = value["counts_are_lower_bounds"]
    if action_id is not None and value.get("action_id") == action_id:
        result["action_id"] = action_id
    return result


class ComputerClientError(Exception):
    """A secret-safe failure; no request, response, token or raw message is retained."""

    def __init__(self, code, *, effect="none", status=None, action_id=None, metrics=None):
        self.code = code
        self.effect = effect
        self.status = status
        self.action_id = action_id
        self.metrics = _metrics(metrics)
        super().__init__(f"Computer operation failed: {code} (effect={effect})")


class GrantUnavailable(ComputerClientError):
    """The broker no longer accepts this grant, or renewal changed its binding."""


class EffectUnknown(ComputerClientError):
    """An operation may have executed. Its action ID must not be silently replayed."""

    def __init__(self, code, *, status=None, action_id=None, metrics=None):
        super().__init__(
            code, effect="unknown", status=status, action_id=action_id, metrics=metrics
        )


@dataclass(frozen=True, slots=True)
class ComputerLease:
    """A token-free broker lease snapshot; reading it does not grant control."""

    computer_id: str
    lease_id: str
    epoch: int
    expires_at_ms: int
    session_id: str
    runtime_generation: int
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComputerGrant(ComputerLease):
    """A host credential with an immutable session/epoch binding; never model data."""

    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ComputerView:
    """An operator-only snapshot, not control authority or a takeover decision."""

    computer_id: str
    owner: Mapping[str, str]
    slot: str
    profile: str
    phase: str
    epoch: int
    lease: ComputerLease | None


@dataclass(frozen=True, slots=True)
class ComputerFile:
    """Token-free staged file metadata; filenames are display names, not host paths."""

    file_id: str
    filename: str
    direction: str
    size_bytes: int
    sha256: str
    created_at_ms: int


def _canonical_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _computer_id(value):
    # Existing broker computers use lowercase UUID.simple(); preserve that exact
    # key instead of normalizing it to the hyphenated encoding used by file IDs.
    return _canonical_uuid(value) or (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None
    )


def _filename(value):
    if not isinstance(value, str) or value in {".", ".."}:
        return False
    try:
        return 1 <= len(value.encode("utf-8")) <= 255 and all(
            character not in {"/", "\\"}
            and not (ord(character) < 32 or 127 <= ord(character) <= 159)
            for character in value
        )
    except UnicodeError:
        return False


def _computer_file(body):
    if not isinstance(body, dict):
        return None
    if not (
        _canonical_uuid(body.get("file_id"))
        and _filename(body.get("filename"))
        and body.get("direction") in ("import", "export")
        and _uint(body.get("size_bytes"))
        and body["size_bytes"] <= _MAX_FILE_BYTES
        and isinstance(body.get("sha256"), str)
        and _SHA256.fullmatch(body["sha256"])
        and _uint(body.get("created_at_ms"))
    ):
        return None
    return ComputerFile(**{key: body[key] for key in ComputerFile.__dataclass_fields__})


def _raw_chunks(response):
    # Mock/custom transports may provide an already buffered response. Real HTTP
    # responses stay streamed, with no decompression before the caller's checks.
    if response.is_stream_consumed:
        yield response.content
    else:
        yield from response.iter_raw()


def _identifier(value, limit=256):
    return isinstance(value, str) and len(value) <= limit and bool(_IDENTIFIER.fullmatch(value))


def _uint(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**64 - 1


def _token(value):
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 512
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _owner(value, session_id):
    if not isinstance(value, dict):
        return False
    if value.get("kind") == "identity":
        keys = {"kind", "realm", "namespace", "identity"}
    elif value.get("kind") == "session" and value.get("session_id") == session_id:
        keys = {"kind", "realm", "session_id"}
    else:
        return False
    try:
        return set(value) == keys and all(
            isinstance(item, str)
            and 0 < len(item.encode("utf-8")) <= 512
            and item.strip() == item
            and all(ord(character) >= 32 and ord(character) != 127 for character in item)
            for item in value.values()
        )
    except UnicodeError:
        return False


def _lease_fields(body):
    if not isinstance(body, dict):
        return None
    if not all(_identifier(body.get(key)) for key in ("computer_id", "lease_id", "session_id")):
        return None
    if not _uint(body.get("epoch"), 1) or not _uint(body.get("expires_at_ms"), 1):
        return None
    if not _uint(body.get("runtime_generation")):
        return None
    capabilities = body.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not all(isinstance(item, str) and item in _CAPABILITIES for item in capabilities)
        or len(set(capabilities)) != len(capabilities)
    ):
        return None
    return dict(
        **{
            key: body[key]
            for key in (
                "computer_id",
                "lease_id",
                "epoch",
                "expires_at_ms",
                "session_id",
                "runtime_generation",
            )
        },
        capabilities=tuple(capabilities),
    )


def _grant(body, token):
    fields = _lease_fields(body)
    return ComputerGrant(**fields, token=token) if fields is not None and _token(token) else None


def _computer_view(body):
    if not isinstance(body, dict) or "lease" not in body:
        return None
    if not all(_identifier(body.get(key)) for key in ("computer_id", "slot", "profile", "phase")):
        return None
    owner = body.get("owner")
    if not isinstance(owner, dict) or not _owner(owner, owner.get("session_id")):
        return None
    if not _uint(body.get("epoch")):
        return None
    lease = None
    if body["lease"] is not None:
        fields = _lease_fields(body["lease"])
        if fields is None or any(fields[key] != body[key] for key in ("computer_id", "epoch")):
            return None
        lease = ComputerLease(**fields)
    return ComputerView(
        **{key: body[key] for key in ("computer_id", "slot", "profile", "phase", "epoch")},
        owner=MappingProxyType(dict(owner)),
        lease=lease,
    )


def _receipt(code, effect, action_id, metrics):
    body = {"error": {"code": code, "effect": effect}, "action_id": action_id, "metrics": metrics}
    return types.CallToolResult(
        isError=True,
        content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=True))],
        structuredContent={"action_id": action_id, "metrics": metrics},
    )


class ComputerClient:
    """One trusted host's broker connection. Lifecycle decisions remain with the host."""

    def __init__(self, broker_url, *, operator_token, host_token=None, timeout=150, transport=None):
        valid = False
        try:
            origin = urlsplit(broker_url)
            valid = (
                origin.scheme == "http"
                and origin.hostname in {"localhost", "127.0.0.1", "::1"}
                and origin.port != 0
                and origin.username is None
                and origin.password is None
                and not origin.query
                and not origin.fragment
                and origin.path in {"", "/"}
                and not any(character.isspace() for character in broker_url)
                and _token(operator_token)
                and (host_token is None or _token(host_token))
                and type(timeout) in {int, float}
                and 0 < timeout <= 86400
                and math.isfinite(timeout)
            )
        except (TypeError, ValueError):
            pass
        if not valid:
            raise ComputerClientError("invalid_configuration")
        self._operator_token = operator_token
        self._host_token = host_token
        self._http = httpx.Client(
            base_url=broker_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self):
        if self._http.is_closed:
            raise ComputerClientError("client_closed")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        """Close transport only; the host must explicitly release each grant."""
        self._http.close()

    def ensure_host(self) -> None:
        """Explicitly wake a configured gateway host; no-op for a direct broker."""
        if self._host_token is None:
            return
        body, status = self._request("POST", "/host/wake", self._operator_token)
        if body.get("ready") is not True:
            raise EffectUnknown("invalid_host_receipt", status=status)

    def list_computers(self) -> list[ComputerView]:
        """Read token-free operator inventory once, without waking or taking control."""
        body, status = self._request("GET", "/v1/computers", self._operator_token)
        if not isinstance(body.get("computers"), list):
            raise ComputerClientError("invalid_computer_inventory", status=status)
        computers = []
        for item in body["computers"]:
            computer = _computer_view(item)
            if computer is None:
                raise ComputerClientError("invalid_computer_inventory", status=status)
            computers.append(computer)
        return computers

    def stage_file(self, computer_id: str, filename: str, data: bytes) -> ComputerFile:
        """Stage bounded upload bytes once; an uncertain outcome must not be replayed."""
        if not (
            _computer_id(computer_id)
            and _filename(filename)
            and isinstance(data, bytes)
            and len(data) <= _MAX_FILE_BYTES
        ):
            raise ComputerClientError("invalid_request")
        digest = hashlib.sha256(data).hexdigest()
        body, status = self._request(
            "POST",
            f"/v1/computers/{computer_id}/files",
            self._operator_token,
            content=data,
            params={"filename": filename},
        )
        item = _computer_file(body)
        if item is None or (item.direction, item.filename, item.size_bytes, item.sha256) != (
            "import",
            filename,
            len(data),
            digest,
        ):
            raise EffectUnknown("invalid_file_receipt", status=status)
        return item

    def list_files(self, computer_id: str) -> list[ComputerFile]:
        """Read staged metadata using operator authority; no guest dispatch or file read."""
        if not _computer_id(computer_id):
            raise ComputerClientError("invalid_request")
        body, status = self._request(
            "GET", f"/v1/computers/{computer_id}/files", self._operator_token
        )
        if not isinstance(body.get("files"), list):
            raise ComputerClientError("invalid_file_inventory", status=status)
        files = []
        seen = set()
        for raw in body["files"]:
            item = _computer_file(raw)
            if item is None or item.file_id in seen:
                raise ComputerClientError("invalid_file_inventory", status=status)
            seen.add(item.file_id)
            files.append(item)
        return files

    def download_file(self, computer_id: str, file_id: str) -> bytes:
        """Return an exact export after bounded length/digest checks; never write a path."""
        if not _computer_id(computer_id) or not _canonical_uuid(file_id):
            raise ComputerClientError("invalid_request")
        item = next(
            (item for item in self.list_files(computer_id) if item.file_id == file_id), None
        )
        if item is None or item.direction != "export":
            raise ComputerClientError("not_found")
        headers = {**self._headers(self._operator_token), "Accept-Encoding": "identity"}
        # Stream raw bytes so compressed or oversized responses cannot allocate an
        # unbounded body before validation. JSON error bodies have a separate small cap.
        try:
            with self._http.stream(
                "GET", f"/v1/computers/{computer_id}/files/{file_id}", headers=headers
            ) as response:
                status = response.status_code
                if not response.is_success:
                    error_body = bytearray()
                    for chunk in _raw_chunks(response):
                        if len(error_body) + len(chunk) > 65536:
                            break
                        error_body.extend(chunk)
                    self._response(httpx.Response(status, content=bytes(error_body)), "GET")
                if (
                    response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    != "application/octet-stream"
                    or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                    or response.headers.get("Content-Length", str(item.size_bytes))
                    != str(item.size_bytes)
                    or response.headers.get("X-Content-SHA256", item.sha256) != item.sha256
                ):
                    raise ComputerClientError("invalid_file_response", status=status)
                data = bytearray()
                digest = hashlib.sha256()
                for chunk in _raw_chunks(response):
                    if len(data) + len(chunk) > item.size_bytes:
                        raise ComputerClientError("invalid_file_response", status=status)
                    data.extend(chunk)
                    digest.update(chunk)
                if len(data) != item.size_bytes or digest.hexdigest() != item.sha256:
                    raise ComputerClientError("invalid_file_response", status=status)
                return bytes(data)
        except httpx.HTTPError:
            pass
        # Do not retain the HTTP exception or its credential-bearing request as context.
        raise ComputerClientError("transport_unavailable")

    def delete_file(self, computer_id: str, file_id: str) -> None:
        """Delete one staged file once, without retrying an uncertain response."""
        if not _computer_id(computer_id) or not _canonical_uuid(file_id):
            raise ComputerClientError("invalid_request")
        body, status = self._request(
            "DELETE", f"/v1/computers/{computer_id}/files/{file_id}", self._operator_token
        )
        if body.get("deleted") != file_id:
            raise EffectUnknown("invalid_file_receipt", status=status)

    def _headers(self, token):
        headers = {"Authorization": "Bearer " + token}
        if self._host_token is not None:
            headers["X-Meerkat-Computer-Host-Token"] = self._host_token
        return headers

    def _request(
        self,
        method,
        path,
        token,
        *,
        payload=None,
        content=None,
        params=None,
        action_id=None,
        tool=False,
    ):
        if self._http.is_closed:
            raise ComputerClientError("client_closed", action_id=action_id)
        effect = "none" if method == "GET" else "unknown"
        response = None
        headers = self._headers(token)
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            response = self._http.request(
                method,
                path,
                headers=headers,
                json=payload,
                content=content,
                params=params,
            )
        except httpx.HTTPError:
            pass
        if response is None:
            error_type = EffectUnknown if effect == "unknown" else ComputerClientError
            raise error_type("transport_unavailable", action_id=action_id)
        return self._response(response, method, action_id=action_id, tool=tool)

    @staticmethod
    def _response(response, method, *, action_id=None, tool=False):
        effect = "none" if method == "GET" else "unknown"
        body = None
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            pass
        status = response.status_code
        if status == 401:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
            raise GrantUnavailable(
                code
                if isinstance(code, str)
                and code in {"unauthorized", "invalid_token", "lease_expired"}
                else "unauthorized",
                status=status,
                action_id=action_id,
            )
        if not isinstance(body, dict):
            error_type = EffectUnknown if effect == "unknown" else ComputerClientError
            raise error_type("invalid_broker_response", status=status, action_id=action_id)
        if not response.is_success or "error" in body:
            error = body.get("error")
            if isinstance(error, dict):
                candidate = error.get("code")
                code = (
                    candidate
                    if isinstance(candidate, str) and candidate in _ERROR_CODES
                    else "broker_error"
                )
                candidate_effect = error.get("effect")
                if isinstance(candidate_effect, str) and candidate_effect in {"none", "unknown"}:
                    effect = candidate_effect
            else:
                code = "broker_error"
            metrics = _metrics(body.get("metrics"), action_id)
            if code in {"unauthorized", "invalid_token", "lease_expired", "stale_lease"}:
                raise GrantUnavailable(code, status=status, action_id=action_id, metrics=metrics)
            if effect == "unknown" or code == "unknown_effect":
                raise EffectUnknown(code, status=status, action_id=action_id, metrics=metrics)
            if tool:
                return _receipt(code, effect, action_id, metrics), status
            raise ComputerClientError(code, status=status, action_id=action_id, metrics=metrics)
        return body, status

    @staticmethod
    def _control(grant):
        if not isinstance(grant, ComputerGrant) or not _token(grant.token):
            raise GrantUnavailable("invalid_grant")
        return grant.token

    def attach(
        self, *, owner, slot, profile, session_id, runtime_generation, ttl_seconds, takeover=False
    ):
        """Acquire a grant once using an explicit trusted host identity and execution."""
        if not (
            _identifier(session_id)
            and _identifier(slot)
            and _identifier(profile)
            and _owner(owner, session_id)
            and _uint(runtime_generation)
            and type(ttl_seconds) is int
            and 1 <= ttl_seconds <= 86400
            and type(takeover) is bool
        ):
            raise ComputerClientError("invalid_request")
        body, status = self._request(
            "POST",
            "/v1/attachments",
            self._operator_token,
            payload={
                "owner": owner,
                "slot": slot,
                "profile": profile,
                "session_id": session_id,
                "runtime_generation": runtime_generation,
                "ttl_seconds": ttl_seconds,
                "takeover": takeover,
            },
        )
        grant = _grant(body, body.get("token"))
        if grant is None or (grant.session_id, grant.runtime_generation) != (
            session_id,
            runtime_generation,
        ):
            raise EffectUnknown("invalid_attachment", status=status)
        return grant

    def renew(self, grant, ttl_seconds):
        """Renew the same token and binding; return the broker's updated expiry."""
        token = self._control(grant)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ComputerClientError("invalid_request")
        body, status = self._request(
            "POST",
            "/v1/lease/renew",
            token,
            payload={"ttl_seconds": ttl_seconds},
        )
        renewed = _grant(body.get("lease"), token)
        immutable = (
            "computer_id",
            "lease_id",
            "epoch",
            "session_id",
            "runtime_generation",
            "capabilities",
        )
        if renewed is None or any(
            getattr(renewed, key) != getattr(grant, key) for key in immutable
        ):
            raise GrantUnavailable("binding_mismatch", effect="unknown", status=status)
        return renewed

    def release(self, grant):
        """Explicitly release control; closing this client does not call this method."""
        body, status = self._request("POST", "/v1/lease/release", self._control(grant))
        if body.get("released") is not True:
            raise EffectUnknown("invalid_release_receipt", status=status)

    def list_tools(self, grant):
        """Read this grant's official MCP tool definitions without invoking an action."""
        body, status = self._request("GET", "/v1/tools", self._control(grant))
        result = None
        try:
            if isinstance(body.get("tools"), list):
                result = [types.Tool.model_validate(item) for item in body["tools"]]
        except (ValueError, TypeError):
            pass
        if result is None:
            raise ComputerClientError("invalid_tool_catalog", status=status)
        return result

    def call_tool(self, grant, name, arguments, action_id=None):
        """Submit once. Unknown outcomes raise with the exact submitted action ID."""
        token = self._control(grant)
        action_id = uuid.uuid4().hex if action_id is None else action_id
        valid = (
            _identifier(name, 100) and _identifier(action_id, 128) and isinstance(arguments, dict)
        )
        try:
            json.dumps(arguments, allow_nan=False, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise ComputerClientError("invalid_request")
        body, status = self._request(
            "POST",
            "/v1/tools/call",
            token,
            payload={
                "action_id": action_id,
                "name": name,
                "arguments": arguments,
            },
            action_id=action_id,
            tool=True,
        )
        if isinstance(body, types.CallToolResult):
            return body
        content = None
        try:
            if isinstance(body.get("content"), list):
                content = []
                for block in body["content"]:
                    if not isinstance(block, dict) or block.get("type") not in {"text", "image"}:
                        raise ValueError
                    model = types.TextContent if block["type"] == "text" else types.ImageContent
                    content.append(model.model_validate(block))
        except (TypeError, ValueError):
            content = None
        if content is None:
            raise EffectUnknown(
                "invalid_tool_result",
                status=status,
                action_id=action_id,
                metrics=body.get("metrics"),
            )
        return types.CallToolResult(
            content=content,
            structuredContent={
                "action_id": action_id,
                "metrics": _metrics(body.get("metrics"), action_id),
            },
        )
