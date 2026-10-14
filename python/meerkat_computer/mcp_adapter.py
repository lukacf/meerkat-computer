"""Thin official-SDK MCP stdio adapter for a single host-issued control binding.

``--host-token-env NAME`` explicitly connects through a trusted loopback HTTP
demand gateway. The named environment credential is sent only as a host header;
the binding's Authorization bearer still belongs to the broker. Startup performs
one ``POST /host/wake`` before MCP initialization. Tool discovery and preview
remain passive reads, with no implicit wake or retry after startup.

``--timeout SECONDS`` covers HTTP operations, including the startup wake. Its
default is 150 seconds for direct brokers and 900 seconds for a gateway.
"""

import argparse
import asyncio
import json
import math
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from . import mcp_apps as apps


def read_binding(path: Path) -> dict:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("Binding must be an owned regular file with mode 0600")
    if info.st_size > 65536:
        raise ValueError("Binding file is too large")
    try:
        binding = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise ValueError("Binding must contain a JSON attachment result") from None
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("token"), str)
        or len(binding["token"]) < 32
    ):
        raise ValueError("Binding lacks a control token")
    return binding


class BrokerAdapter:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def list_tools(self) -> list[types.Tool]:
        try:
            response = await self.client.get("/v1/tools")
            response.raise_for_status()
            body = response.json()
            return [types.Tool.model_validate(tool) for tool in body["tools"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # The SDK may discover a tool before calling it. Do not let that path
            # surface transport diagnostics inside an otherwise safe preview.
            raise RuntimeError("Broker refused tool discovery") from None

    async def preview(self, arguments: dict) -> types.CallToolResult:
        if not apps.valid_preview_arguments(arguments):
            return apps.preview_error("Invalid preview arguments.")
        try:
            # The client's existing control binding selects the computer. This read
            # never passes through the action endpoint or acquires an action ID.
            response = await self.client.get(
                "/v1/preview", params=arguments, follow_redirects=False
            )
            if not response.is_success:
                return apps.preview_error()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return apps.preview_error()
        if not apps.valid_preview_body(body):
            return apps.preview_error()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Computer preview updated.")],
            _meta={"meerkat-computer/preview": body},
        )

    async def call_tool(
        self, name: str, arguments: dict, action_id: str | None = None
    ) -> types.CallToolResult:
        action_id = action_id or uuid.uuid4().hex
        try:
            response = await self.client.post(
                "/v1/tools/call",
                json={
                    "action_id": action_id,
                    "name": name,
                    "arguments": arguments,
                },
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            # The request may have reached the worker. Never automatically retry.
            return _unknown_result(action_id, "transport_effect_unknown")
        if not isinstance(body, dict):
            return _unknown_result(action_id)
        metrics = body.get("metrics", {})
        if "error" in body:
            error = body["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or error.get("effect") not in ("none", "unknown")
            ):
                return _unknown_result(action_id, metrics=metrics)
            return types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=True))],
                structuredContent={"action_id": action_id, "metrics": metrics},
            )
        if not response.is_success or not isinstance(body.get("content"), list):
            return _unknown_result(action_id, metrics=metrics)
        try:
            content = []
            for block in body["content"]:
                if not isinstance(block, dict):
                    raise TypeError("Invalid content block")
                if block.get("type") == "text":
                    content.append(types.TextContent.model_validate(block))
                elif block.get("type") == "image":
                    content.append(types.ImageContent.model_validate(block))
                else:
                    raise ValueError("Unsupported content block")
            return types.CallToolResult(
                content=content,
                structuredContent={"action_id": action_id, "metrics": metrics},
            )
        except (TypeError, ValueError):
            return _unknown_result(action_id, metrics=metrics)


def _unknown_result(
    action_id: str, code: str = "invalid_broker_response", *, metrics=None
) -> types.CallToolResult:
    # Protocol failure cannot establish whether the action happened. Keep the
    # broker's telemetry when present, without echoing an unvalidated payload.
    return types.CallToolResult(
        isError=True,
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(
                    {"error": {"code": code, "effect": "unknown", "action_id": action_id}}
                ),
            )
        ],
        structuredContent={"action_id": action_id, "metrics": metrics or {}},
    )


def create_server(adapter: BrokerAdapter, *, mcp_apps: bool = False) -> Server:
    server = Server("meerkat-computer", version="0.1.0")
    execution_id = uuid.uuid4().hex
    preview_html = apps.load_preview_html() if mcp_apps else None

    def apps_enabled() -> bool:
        client_params = server.request_context.session.client_params
        return (
            bool(preview_html)
            and client_params is not None
            and apps.supports_apps(client_params.capabilities)
        )

    @server.list_tools()
    async def list_tools():
        tools = await adapter.list_tools()
        return apps.with_preview(tools) if apps_enabled() else tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict | None):
        if name == apps.PREVIEW_TOOL:
            if not apps_enabled():
                return apps.preview_error()
            return await adapter.preview(arguments or {})
        # Stable within this MCP execution if a client reuses a request ID.
        request_id = server.request_context.request_id
        action_id = uuid.uuid5(uuid.NAMESPACE_OID, execution_id + json.dumps(request_id)).hex
        return await adapter.call_tool(name, arguments or {}, action_id)

    if mcp_apps:

        @server.list_resources()
        async def list_resources():
            if not apps_enabled():
                return []
            return [
                types.Resource(
                    uri=apps.PREVIEW_URI,
                    name="My Computer",
                    description="Preview of the latest saved agent screenshot.",
                    mimeType=apps.PREVIEW_MIME,
                )
            ]

        @server.read_resource()
        async def read_resource(uri):
            if not apps_enabled() or str(uri) != apps.PREVIEW_URI:
                raise McpError(
                    types.ErrorData(code=types.INVALID_PARAMS, message="UI resource unavailable")
                )
            return [
                ReadResourceContents(
                    content=preview_html, mime_type=apps.PREVIEW_MIME, meta=apps.RESOURCE_META
                )
            ]

    return server


def _connection_timeout(broker_url, host_token, timeout):
    resolved = (900 if host_token is not None else 150) if timeout is None else timeout
    valid = False
    try:
        parsed = urlsplit(broker_url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
            and not any(character.isspace() for character in broker_url)
            and type(resolved) in {int, float}
            and 0 < resolved <= 86400
            and math.isfinite(resolved)
        )
        if host_token is not None:
            valid = (
                valid
                and parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and isinstance(host_token, str)
                and 32 <= len(host_token) <= 4096
                and all(33 <= ord(character) <= 126 for character in host_token)
            )
    except (TypeError, ValueError):
        pass
    if not valid:
        raise ValueError("Invalid computer adapter connection configuration")
    return resolved


async def _wake_host(client: httpx.AsyncClient):
    ready = False
    try:
        response = await client.post("/host/wake")
        body = response.json() if response.is_success else None
        ready = isinstance(body, dict) and body.get("ready") is True and "error" not in body
    except (httpx.HTTPError, ValueError):
        pass
    if not ready:
        # Do not retain transport exceptions or expose the gateway's raw response.
        raise RuntimeError("Computer host did not confirm readiness")


async def run(
    broker_url: str,
    binding: dict,
    *,
    mcp_apps: bool = False,
    host_token: str | None = None,
    timeout: float | None = None,
):
    timeout = _connection_timeout(broker_url, host_token, timeout)
    headers = {"Authorization": "Bearer " + binding["token"]}
    if host_token is not None:
        headers["X-Meerkat-Computer-Host-Token"] = host_token
    async with httpx.AsyncClient(
        base_url=broker_url,
        headers=headers,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        if host_token is not None:
            await _wake_host(client)
        server = create_server(BrokerAdapter(client), mcp_apps=mcp_apps)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(
        description="Expose one host-issued computer binding over MCP stdio"
    )
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--binding-file", required=True, type=Path)
    parser.add_argument(
        "--host-token-env",
        help="Explicit trusted gateway credential environment variable; requires loopback HTTP",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="HTTP timeout in seconds (default: 150 direct, 900 with --host-token-env)",
    )
    parser.add_argument(
        "--mcp-apps",
        action="store_true",
        help="Enable the inline preview for hosts that negotiate MCP Apps support",
    )
    arguments = parser.parse_args()
    host_token = None
    try:
        if arguments.host_token_env is not None:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", arguments.host_token_env):
                raise ValueError("Invalid gateway credential environment variable")
            host_token = os.environ.get(arguments.host_token_env)
            if host_token is None:
                raise ValueError("Gateway credential environment variable is unset")
        _connection_timeout(arguments.broker_url, host_token, arguments.timeout)
        binding = read_binding(arguments.binding_file)
    except (OSError, ValueError):
        parser.error(
            "Use a valid broker origin, positive timeout and owned JSON binding file with mode 0600; "
            "gateway mode requires loopback HTTP and a valid explicit credential environment variable"
        )
    options = {"mcp_apps": arguments.mcp_apps}
    if host_token is not None:
        options["host_token"] = host_token
    if arguments.timeout is not None:
        options["timeout"] = arguments.timeout
    asyncio.run(run(arguments.broker_url, binding, **options))


if __name__ == "__main__":
    main()
