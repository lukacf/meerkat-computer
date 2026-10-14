"""Guest-local secret input. Values never enter ordinary calls or receipts."""

import asyncio
import contextlib
import ipaddress
import json
import os
import secrets
import socket
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, model_validator

from .protocol import Call, ForkControl, Lease, StrictModel, WorkerError


class SecureTarget(StrictModel):
    observation_id: str = Field(min_length=1, max_length=128)
    origin: str = Field(min_length=1, max_length=2048)
    secret_ref: str = Field(min_length=1, max_length=100)
    username_ref: str | None = Field(default=None, min_length=1, max_length=100)


class SecureValues(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    secret: str = Field(min_length=1, max_length=16384, repr=False)
    username: str | None = Field(default=None, max_length=16384, repr=False)


class SecureInputEnvelope(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    lease: Lease
    action_id: str = Field(min_length=1, max_length=128)
    target: SecureTarget
    values: SecureValues = Field(repr=False)

    @model_validator(mode="after")
    def validate_fields(self):
        if (self.target.username_ref is None) != (self.values.username is None):
            raise ValueError("Username requires an observed username field")
        if self.target.username_ref == self.target.secret_ref:
            raise ValueError("Credential fields must be distinct")
        return self

    def journal_call(self):
        return Call(
            lease_id=self.lease.lease_id,
            epoch=self.lease.epoch,
            action_id=self.action_id,
            name="secure_input",
            arguments=self.target.model_dump(),
        )


def exact_origin(value: str, *, origin_only=False) -> str:
    try:
        parts = urlsplit(value)
        if (
            parts.username is not None
            or parts.password is not None
            or not parts.hostname
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError
        if origin_only and (parts.path or parts.query or parts.fragment):
            raise ValueError
        host = parts.hostname.lower()
        if parts.scheme == "http":
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        elif parts.scheme != "https":
            raise ValueError
        port = parts.port
        host = f"[{host}]" if ":" in host else host
        suffix = f":{port}" if port and port != {"http": 80, "https": 443}[parts.scheme] else ""
        return f"{parts.scheme}://{host}{suffix}"
    except (ValueError, TypeError):
        raise WorkerError(
            "secure_target_denied", "Secure input requires an exact HTTPS origin", 403
        ) from None


# form.elements omits image inputs, including those associated by a form attribute.
FIELD_INFO = """el => {
  const submitters = el.form ? Array.from(el.getRootNode().querySelectorAll('button,input'))
    .filter(x => x.form === el.form && ['submit','image'].includes(x.type)) : [];
  return {connected:el.isConnected, origin:location.origin,
  action:el.form ? el.form.action : location.href,
  method:el.form ? el.form.method : null,
  methodOverrides:submitters.filter(x => x.hasAttribute('formmethod')).map(x => x.formMethod),
  overrides:submitters.filter(x => x.hasAttribute('formaction')).map(x => x.formAction),
  editable:(el.tagName==='TEXTAREA' || (el.tagName==='INPUT' &&
    ['text','email','password','tel','url','search','number'].includes(el.type))) &&
    !el.disabled && !el.readOnly};
}"""
NONEDITABLE = """el => el.isConnected && !el.isContentEditable &&
  !['TEXTAREA','SELECT'].includes(el.tagName) &&
  (el.tagName !== 'INPUT' || ['submit','button','image'].includes(el.type)) &&
  !el.closest('[contenteditable=true]') &&
  !['textbox','searchbox','combobox','spinbutton'].includes(el.getAttribute('role'))"""


class SecureInput:
    def __init__(self, worker):
        self.worker, self.browser = worker, worker.browser
        self.page = None
        self.marker = None
        self.worker.journal.db.execute(
            "CREATE TABLE IF NOT EXISTS secure_input_privacy (singleton INTEGER PRIMARY KEY CHECK(singleton=1))"
        )
        self.worker.journal.db.commit()
        self.active = (
            self.worker.journal.db.execute("SELECT singleton FROM secure_input_privacy").fetchone()
            is not None
        )

    async def anchor(self):
        self.page = self.browser.current()
        self.marker = "__meerkat_private_document_" + secrets.token_hex(24)
        await self.page.evaluate(
            "key => Object.defineProperty(window,key,{value:true,configurable:false})", self.marker
        )

    async def restore(self):
        if self.active:
            await self.anchor()

    async def protect(self):
        with self.worker.journal.db:
            self.worker.journal.db.execute("INSERT OR IGNORE INTO secure_input_privacy VALUES (1)")
        self.active = True
        await self.anchor()

    async def guarded(self):
        if not self.active:
            return False
        if self.page is None:
            # An incomplete restore is not evidence that the old document is gone.
            return True
        if not self.page.is_closed():
            try:
                same_document = await self.page.evaluate("key => window[key] === true", self.marker)
            except Exception:  # noqa: BLE001 - transition/closed context is not navigation proof
                return True
            if same_document:
                return True
        self.active = False
        self.page = self.marker = None
        with self.worker.journal.db:
            self.worker.journal.db.execute("DELETE FROM secure_input_privacy")
        await self.worker.computer.invalidate()
        return False

    async def continuation(self):
        browser = self.browser
        if browser.current() is not self.page:
            await browser.invalidate()
        retained = {}
        for ref, handle in browser.refs.items():
            try:
                if await handle.evaluate(NONEDITABLE):
                    retained[ref] = handle
                    continue
            except Exception:  # noqa: BLE001,S110 - logging may expose page or secret text
                pass
            with contextlib.suppress(Exception):
                await handle.dispose()
        browser.refs = retained
        browser.observation_id = secrets.token_hex(16)
        browser.observation_signature = await browser.signature()
        return {
            "surface": "browser",
            "observation_id": browser.observation_id,
            "action_refs": list(retained),
            "privacy": {
                "active": True,
                "message": "Screenshots and semantic content are withheld. You may explicitly click an already observed action ref, or navigate away. A new document releases the privacy guard.",
            },
        }

    async def observation(self):
        if not await self.guarded():
            return None
        return [{"type": "text", "text": json.dumps(await self.continuation(), ensure_ascii=True)}]

    async def validate_target(self, target):
        browser = self.browser
        if self.worker.computer.active != "browser":
            raise WorkerError("stale_observation", "Observe the browser before secure input")
        if not target.observation_id or target.observation_id != browser.observation_id:
            raise WorkerError("stale_observation", "Observe before secure input")
        if browser.observation_signature != await browser.signature():
            raise WorkerError("stale_observation", "Page changed before secure input")
        expected = exact_origin(target.origin, origin_only=True)
        if expected != target.origin or exact_origin(browser.current().url) != expected:
            raise WorkerError(
                "secure_target_denied", "Browser origin does not match the request", 403
            )
        fields = []
        for ref in [target.username_ref, target.secret_ref]:
            if ref is None:
                continue
            handle = browser.refs.get(ref)
            if handle is None:
                raise WorkerError("stale_ref", "Secure input requires observed field references")
            info = await handle.evaluate(FIELD_INFO)
            owner = await handle.owner_frame()
            if (
                not info["connected"]
                or not info["editable"]
                or not await handle.is_editable()
                or not await handle.is_visible()
                or owner is None
                or exact_origin(owner.url) != expected
                or info["origin"] != expected
                or exact_origin(info["action"]) != expected
                or info["method"] not in (None, "post")
                or any(value != "post" for value in info["methodOverrides"])
                or any(exact_origin(value) != expected for value in info["overrides"])
            ):
                raise WorkerError(
                    "secure_target_denied",
                    "Credential field or form destination is not authorized",
                    403,
                )
            fields.append(handle)
        return fields

    async def deliver(self, request):
        async with self.worker.lock:
            self.worker.ensure_effects_allowed()
            started = time.monotonic()
            lease = self.worker.journal.lease()
            if lease is None or (lease.lease_id, lease.epoch) != (
                request.lease.lease_id,
                request.lease.epoch,
            ):
                raise WorkerError("stale_lease", "Control lease is not current")
            if lease.expires_at_ms <= self.worker.now_ms():
                raise WorkerError("lease_expired", "Control lease expired")
            if lease != request.lease or self.worker.applied_lease != (lease.lease_id, lease.epoch):
                raise WorkerError(
                    "stale_lease", "Secure input lease does not match the current grant"
                )
            if "browser" not in lease.capabilities or "browser_semantics" not in lease.capabilities:
                raise WorkerError(
                    "capability_denied", "Secure input requires browser semantics", 403
                )
            call = request.journal_call()
            receipt = self.worker.journal.receipt(call)
            if receipt is not None:
                return receipt
            try:
                if await self.guarded():
                    raise WorkerError(
                        "privacy_guarded", "Navigate to a new document before another secure input"
                    )
                fields = await self.validate_target(request.target)
            except WorkerError:
                raise
            except Exception:  # noqa: BLE001 - never expose page or credential diagnostics
                raise WorkerError(
                    "secure_target_denied", "Cannot validate credential fields", 403
                ) from None
            if lease.expires_at_ms <= self.worker.now_ms():
                raise WorkerError("lease_expired", "Control lease expired before secure input")
            self.worker.journal.admit(call)
            self.worker.journal.increment("secure_input_attempts")
            self.browser.observation_id = ""
            self.browser.observation_signature = None
            self.browser.operation_metrics = {}
            try:
                await self.protect()
                if self.worker.computer.desktop is not None:
                    await self.worker.computer.desktop.invalidate()
                values = (
                    [request.values.secret]
                    if request.values.username is None
                    else [request.values.username, request.values.secret]
                )
                for handle, value in zip(fields, values, strict=True):
                    # Earlier input handlers may change the form destination or navigate.
                    info = await handle.evaluate(FIELD_INFO)
                    if (
                        not info["connected"]
                        or not info["editable"]
                        or exact_origin(self.browser.current().url) != request.target.origin
                        or info["origin"] != request.target.origin
                        or exact_origin(info["action"]) != request.target.origin
                        or info["method"] not in (None, "post")
                        or any(value != "post" for value in info["methodOverrides"])
                        or any(
                            exact_origin(item) != request.target.origin
                            for item in info["overrides"]
                        )
                    ):
                        raise WorkerError("secure_target_denied", "Credential target changed")
                    remaining = (lease.expires_at_ms - self.worker.now_ms()) / 1000
                    if remaining <= 0:
                        raise WorkerError(
                            "lease_expired", "Control lease expired before field input"
                        )
                    self.browser.record("ui_command_attempts")
                    await asyncio.wait_for(handle.fill(value), min(remaining, 10))
                    self.browser.record("ui_commands")
                    self.browser.record("secure_fields_filled")
                body = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"filled": True, "submitted": False, **await self.continuation()},
                                ensure_ascii=True,
                            ),
                        }
                    ]
                }
                self.worker.journal.increment("secure_input_completed")
                status = 200
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - values can occur anywhere in browser exceptions
                body = WorkerError(
                    "action_effect_unknown",
                    "Secure input may have occurred. Observations remain private until navigation; do not replay.",
                    effect="unknown",
                ).response()
                self.worker.journal.increment("uncertain_effects")
                status = 409
            body["metrics"] = {
                "action_id": request.action_id,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "delta": self.browser.operation_metrics,
                "totals": self.worker.journal.metrics(),
            }
            if status != 200:
                body["metrics"]["counts_are_lower_bounds"] = True
            self.worker.journal.finish(call, status, body)
            return status, body


def create_secure_app(worker):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(
            WorkerError("invalid_input", "Invalid private worker request", 422).response(),
            status_code=422,
        )

    @app.exception_handler(WorkerError)
    async def worker_error(request, exc):
        return JSONResponse(exc.response(), status_code=exc.status)

    @app.post("/v1/secure-input")
    async def deliver(request: SecureInputEnvelope):
        status, body = await worker.secure_input(request)
        return JSONResponse(body, status_code=status)

    @app.post("/v1/fork-control")
    async def fork_control(request: ForkControl):
        return await worker.fork_control(request)

    return app


@contextlib.contextmanager
def private_socket(path: Path):
    if os.path.lexists(path):
        raise ValueError("Secure input socket path already exists")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    inode = None
    try:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        inode = path.stat().st_ino
        sock.listen(128)
        sock.setblocking(False)
        yield sock
    finally:
        sock.close()
        if inode is not None and path.exists() and path.stat().st_ino == inode:
            path.unlink()


@contextlib.asynccontextmanager
async def secure_listener(worker):
    path = worker.state_dir / "secure-input.sock"
    # Worker already owns the journal's exclusive process lock, so a socket left
    # by a crashed previous owner is stale. Never remove another kind of path.
    if os.path.lexists(path) and stat.S_ISSOCK(path.lstat().st_mode):
        path.unlink()
    with private_socket(path) as sock:
        server = uvicorn.Server(
            uvicorn.Config(
                create_secure_app(worker),
                access_log=False,
                log_level="critical",
                lifespan="off",
                timeout_graceful_shutdown=3,
            )
        )
        server.capture_signals = contextlib.nullcontext
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Secure input listener failed to start")
                await asyncio.sleep(0.01)
            yield
        finally:
            server.should_exit = True
            await task
