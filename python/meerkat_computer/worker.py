"""Private guest HTTP worker. The broker owns assignments and lease issuance."""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .browser import Browser
from .journal import Journal
from .protocol import STALE_INPUT_REASONS, TOOL_ARGUMENTS, Call, Lease, WorkerError
from .secure_input import SecureInput, secure_listener
from .surfaces import ComputerSurfaces
from .transfers import Transfers
from .transfers import routes as transfer_routes
from .workspace import Workspace


class Worker:
    def __init__(
        self,
        state_dir: Path,
        workspace: Path,
        token: str,
        browser=None,
        *,
        desktop=None,
        clock=time.time,
        token_file: Path | None = None,
    ):
        if len(token) < 32:
            raise ValueError("Worker token must have at least 32 characters")
        self.token = token
        configured_token_file = os.environ.get("MEERKAT_COMPUTER_WORKER_TOKEN_FILE")
        self.token_file = token_file or (Path(configured_token_file) if configured_token_file else None)
        self.state_dir = state_dir
        self.journal = Journal(state_dir)
        self.workspace = Workspace(workspace)
        self.transfers = Transfers(state_dir, self.workspace.root)
        self.browser = browser if browser is not None else Browser(state_dir / "browser-profile")
        self.browser.metric_sink = self.journal.increment
        self.computer = ComputerSurfaces(self.browser, desktop)
        self.computer.metric_sink = self.journal.increment
        if desktop is not None:
            desktop.metric_sink = self.journal.increment
        self.clock = clock
        self.lock = asyncio.Lock()
        self.applied_lease = None
        self.secure = SecureInput(self)
        self.browser.secure = self.secure

    async def start(self):
        await self.computer.start()
        await self.secure.restore()
        lease = self.journal.lease()
        if lease is not None:
            self.transfers.configure(lease)
            await self.computer.configure(lease)
            self.applied_lease = (lease.lease_id, lease.epoch)

    async def close(self):
        # Request cancellation must release its effect lock before browser or
        # journal shutdown. Pending admissions remain non-replayable on restart.
        async with self.lock:
            try:
                await self.computer.close()
            finally:
                self.journal.close()

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    async def secure_input(self, request):
        return await self.secure.deliver(request)

    def authorize(self, value: str | None):
        if value is None or not hmac.compare_digest(
            value.encode(), ("Bearer " + self.token).encode()
        ):
            raise WorkerError("unauthorized", "Worker authentication failed", 401)

    def ensure_effects_allowed(self):
        if self.journal.prepared_checkpoint() is not None:
            raise WorkerError("fork_prepared", "Computer control is gated for a prepared fork")

    def check_token_file(self):
        try:
            info = self.token_file.lstat() if self.token_file is not None else None
            if info is None or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError
        except (OSError, ValueError):
            raise WorkerError(
                "fork_token_file_unavailable", "Fork requires a private persistent worker token file", 503
            ) from None

    def persist_token(self, token: str):
        self.check_token_file()
        descriptor, name = tempfile.mkstemp(prefix=".worker-token-", dir=self.token_file.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(token)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.token_file)
            directory = os.open(self.token_file.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    async def fork_control(self, request):
        async with self.lock:
            if request.operation == "fork_capabilities":
                return {
                    "protocol_version": 1, "full_state_fork": True,
                    "prepared_checkpoint_id": self.journal.prepared_checkpoint(),
                }
            receipt = {
                "protocol_version": 1, "operation": request.operation,
                "checkpoint_id": request.checkpoint_id,
            }
            if request.operation == "fork_prepare":
                self.check_token_file()
                self.journal.prepare_fork(request.checkpoint_id)
                return {**receipt, "prepared": True}
            if request.operation == "fork_resume":
                self.journal.resume_fork(request.checkpoint_id)
                return {**receipt, "resumed": True}
            fingerprint = hashlib.sha256(json.dumps(
                request.model_dump(), sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            prior = self.journal.rebind_record(request.fork_id)
            receipt.update(
                fork_id=request.fork_id, computer_id=request.computer_id,
                incarnation_id=request.incarnation_id, rebound=True,
            )
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise WorkerError("fork_rebind_conflict", "Fork ID was already bound differently")
                if prior["phase"] == "complete":
                    return receipt
                if prior["phase"] != "pending":
                    raise WorkerError("fork_rebind_conflict", "Fork binding has been superseded")
            elif hmac.compare_digest(request.worker_token.encode(), self.token.encode()):
                raise WorkerError("fork_rebind_conflict", "Child requires an independent worker token")
            self.journal.begin_rebind(request, fingerprint)
            self.applied_lease = None
            try:
                self.persist_token(request.worker_token)
                self.token = request.worker_token
                # Never reconstruct Browser, Desktop, their connections or the
                # private page guard. Only inherited control references expire.
                await self.computer.invalidate()
                self.computer.lease = self.browser.lease = None
                self.browser.operation_metrics = {}
                self.computer.current_target = None
                self.computer.local_metrics = self.computer.fallback_metrics = {}
                if self.computer.desktop is not None:
                    self.computer.desktop.lease = None
                    self.computer.desktop.operation_metrics = {}
                self.journal.finish_rebind(request)
            except Exception:  # noqa: BLE001 - tokens and application diagnostics stay private
                raise WorkerError(
                    "fork_rebind_incomplete", "Rebind remains gated; retry the exact private request", 503,
                    effect="unknown",
                ) from None
            return receipt

    async def grant(self, lease: Lease, *, authorization: str | None = None) -> dict:
        async with self.lock:
            if authorization is not None:
                self.authorize(authorization)
            self.ensure_effects_allowed()
            prior = self.journal.lease()
            if prior and (
                lease.epoch < prior.epoch
                or (lease.epoch == prior.epoch and lease.lease_id != prior.lease_id)
            ):
                raise WorkerError("stale_lease", "Lease epoch has already been fenced")
            if (
                prior
                and lease.epoch == prior.epoch
                and (
                    set(prior.capabilities) != set(lease.capabilities)
                    or prior.observation != lease.observation
                )
            ):
                raise WorkerError(
                    "lease_profile_changed",
                    "A renewal cannot change capabilities or observation profile",
                )
            if lease.expires_at_ms != 0 and lease.expires_at_ms <= self.now_ms():
                raise WorkerError("lease_expired", "Lease has already expired")
            # Persist the fence before browser changes. A failed configuration must
            # never restore authority to the old epoch.
            self.journal.set_lease(lease)
            self.applied_lease = None
            try:
                self.transfers.configure(lease)
                await self.computer.configure(lease)
            except Exception:  # noqa: BLE001 - lease stays fenced, exception data stays private
                raise WorkerError(
                    "worker_not_ready", "Lease fenced but computer configuration failed", 503
                ) from None
            self.applied_lease = (lease.lease_id, lease.epoch)
            return {"protocol_version": 1, "lease_id": lease.lease_id, "epoch": lease.epoch}

    def _record_input_rejection(self, call: Call, error: WorkerError, started: float):
        delta = {"ui_input_rejections": 1}
        reasons = error.reasons if isinstance(error.reasons, tuple) else ()
        for reason in reasons:
            if isinstance(reason, str) and reason in STALE_INPUT_REASONS:
                delta["stale_input_" + reason] = 1
        body = error.response()
        # Validation precedes perform(), so previous operation metrics are not current.
        body["metrics"] = {
            "action_id": call.action_id,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        saved = self.journal.reject(call, error.status, body, delta)
        error.metrics = saved["metrics"]

    async def call(self, call: Call, *, authorization: str | None = None) -> tuple[int, dict]:
        async with self.lock:
            if authorization is not None:
                self.authorize(authorization)
            self.ensure_effects_allowed()
            started = time.monotonic()
            lease = self.journal.lease()
            if lease is None or lease.lease_id != call.lease_id or lease.epoch != call.epoch:
                raise WorkerError("stale_lease", "Control lease is not current")
            if lease.expires_at_ms <= self.now_ms():
                raise WorkerError("lease_expired", "Control lease expired")
            if self.applied_lease != (lease.lease_id, lease.epoch):
                raise WorkerError(
                    "worker_not_ready", "Current lease configuration was not applied", 503
                )
            receipt = self.journal.receipt(call)
            if receipt is not None:
                return receipt
            entry = TOOL_ARGUMENTS.get(call.name)
            if entry is None:
                raise WorkerError("unknown_tool", "Tool is not supported", 422)
            capability, model = entry
            required = (capability,) if isinstance(capability, str) else capability
            if not any(item in lease.capabilities for item in required):
                raise WorkerError(
                    "capability_denied", "Tool is not enabled by the computer profile", 403
                )
            try:
                arguments = model.model_validate(call.arguments)
            except ValidationError:
                raise WorkerError(
                    "invalid_input", "Arguments do not match the tool schema", 422
                ) from None
            browser_call = call.name.startswith(("computer_", "browser_"))
            effectful = call.name not in {
                "computer_observe", "workspace_read", "browser_downloads"
            } and not (
                call.name in {"browser_tabs", "computer_apps"} and arguments.action == "list"
            )
            target = (self.computer if browser_call else self.transfers
                      if call.name.startswith("transfer_") else self.workspace)
            privacy_observation = None
            try:
                if browser_call and await self.secure.guarded():
                    if call.name == "browser_upload":
                        raise WorkerError(
                            "privacy_guarded", "Uploads are withheld during secure input"
                        )
                    surface = self.computer.surface_for(call.name, arguments)
                    if surface == "desktop":
                        if call.name == "computer_observe":
                            privacy_observation = await self.secure.observation()
                        else:
                            raise WorkerError(
                                "privacy_guarded", "Desktop access is withheld during secure input"
                            )
                try:
                    if browser_call:
                        await target.validate(call.name, arguments)
                    else:
                        target.validate(call.name, arguments)
                except WorkerError:
                    raise
                except Exception:  # noqa: BLE001 - validation diagnostics stay private
                    raise WorkerError(
                        "validation_failed",
                        "Cannot validate this operation against current state", 409,
                    ) from None
                if not browser_call and effectful:
                    # Guest commands and watched files can redraw UI without accessible events.
                    await self.computer.invalidate()
                if lease.expires_at_ms <= self.now_ms():
                    raise WorkerError("lease_expired", "Control lease expired before action admission")
            except WorkerError as error:
                if call.name == "computer_act":
                    self._record_input_rejection(call, error, started)
                raise
            self.journal.admit(call)
            self.journal.increment("tool_calls_admitted")
            try:
                remaining = max(0.001, (lease.expires_at_ms - self.now_ms()) / 1000)
                if privacy_observation is not None:
                    content, deltas = privacy_observation, {}
                else:
                    content, deltas = await asyncio.wait_for(
                        target.perform(call.name, arguments), min(remaining, 150)
                    )
                if not getattr(target, "persists_metrics", False):
                    for key, value in deltas.items():
                        self.journal.increment(key, value)
                self.journal.increment("tool_calls_completed")
                body = {
                    "content": content,
                    "metrics": {
                        "action_id": call.action_id,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "delta": deltas,
                        "totals": self.journal.metrics(),
                    },
                }
                status = 200
            except asyncio.CancelledError:
                # Leave the durable pending admission in place. Restart/retry
                # reports unknown effect instead of running the action again.
                raise
            except Exception as exc:  # noqa: BLE001 - boundary classifies uncertain effects without leaking exceptions
                effect = "unknown" if effectful else "none"
                self.journal.increment("tool_calls_failed")
                if effectful:
                    self.journal.increment("uncertain_effects")
                    await self.computer.invalidate()
                if isinstance(exc, WorkerError) and not effectful:
                    body, status = exc.response(), exc.status
                else:
                    # Never return exception text, which may include command data,
                    # browser content or credentials.
                    error = WorkerError(
                        "action_effect_unknown" if effectful else "operation_failed",
                        "Operation failed after admission; observe before continuing"
                        if effectful
                        else "Read operation failed",
                        409 if effectful else 500,
                        effect,
                    )
                    body, status = error.response(), error.status
                body["metrics"] = {
                    "action_id": call.action_id,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "delta": getattr(target, "operation_metrics", {}),
                    "totals": self.journal.metrics(),
                    "counts_are_lower_bounds": effectful,
                }
            self.journal.finish(call, status, body)
            return status, body


def create_app(worker: Worker) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        try:
            await worker.start()
            async with secure_listener(worker):
                yield
        finally:
            await worker.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.method == "POST":
            try:
                worker.authorize(request.headers.get("authorization"))
            except WorkerError as exc:
                return JSONResponse(exc.response(), status_code=exc.status)
        return await call_next(request)

    @app.exception_handler(WorkerError)
    async def worker_error(request, exc):
        return JSONResponse(exc.response(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(
            WorkerError(
                "invalid_input", "Request does not match the protocol schema", 422
            ).response(),
            status_code=422,
        )

    @app.get("/health")
    async def health():
        return {
            "protocol_version": 1,
            "ready": worker.computer.ready(),
            "surfaces": ["browser", "desktop"]
            if worker.computer.desktop is not None
            else ["browser"],
        }

    @app.post("/v1/lease")
    async def grant(lease: Lease, request: Request):
        return await worker.grant(lease, authorization=request.headers.get("authorization"))

    @app.post("/v1/call")
    async def call(body: Call, request: Request):
        status, body = await worker.call(body, authorization=request.headers.get("authorization"))
        return JSONResponse(body, status_code=status)

    transfer_routes(app, worker)
    return app


def load_token() -> str:
    filename = os.environ.get("MEERKAT_COMPUTER_WORKER_TOKEN_FILE")
    if filename:
        path = Path(filename)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Worker token file must be a private regular file")
        token = path.read_text().strip()
    else:
        token = os.environ.get("MEERKAT_COMPUTER_WORKER_TOKEN", "").strip()
    if len(token) < 32:
        raise ValueError(
            "Set a worker token of at least 32 characters using the token environment or private file"
        )
    return token


def main():
    parser = argparse.ArgumentParser(description="Run a private Meerkat computer guest worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/meerkat-computer/state"))
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument(
        "--desktop", action="store_true", help="Enable the guest's private Linux X11/AT-SPI desktop"
    )
    arguments = parser.parse_args()
    try:
        desktop = None
        if arguments.desktop:
            from .desktop import Desktop

            desktop = Desktop(arguments.state_dir / "desktop")
        worker = Worker(
            arguments.state_dir,
            arguments.workspace,
            load_token(),
            Browser(arguments.state_dir / "browser-profile", headless=not arguments.desktop),
            desktop=desktop,
        )
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(
        create_app(worker),
        host=arguments.host,
        port=arguments.port,
        access_log=False,
        log_level="warning",
        # Leave time for the private listener and Chromium to close inside the
        # desktop supervisor's worker grace period.
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
