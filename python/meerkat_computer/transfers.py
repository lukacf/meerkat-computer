"""Private, bounded guest byte staging. Action receipts contain metadata only."""

import hashlib
import os
import stat
import uuid
from pathlib import Path

from .browser import text_block
from .protocol import WorkerError

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_STORED_BYTES = 128 * 1024 * 1024


def file_id(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise WorkerError("invalid_file_id", "Expected an opaque file ID", 422) from None
    return value


def metadata(identifier, data):
    return {"file_id": identifier, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


class Transfers:
    def __init__(self, state_dir: Path, workspace: Path):
        self.root = state_dir / "transfers"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.workspace = workspace
        self.directory = None
        self.max_stored_bytes = MAX_STORED_BYTES
        self.prepared = None

    def configure(self, lease):
        key = hashlib.sha256(f"{lease.lease_id}:{lease.epoch}".encode()).hexdigest()
        directory = self.root / key
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise WorkerError("invalid_staging", "Private staging directory is unavailable", 503)
        # A changed fence cannot inherit transport blobs, including after a VM fork.
        for old in self.root.iterdir():
            if old == directory or old.is_symlink() or not old.is_dir():
                continue
            for item in old.iterdir():
                if item.is_file() or item.is_symlink():
                    item.unlink()
            old.rmdir()
        self.directory = directory
        for partial in directory.glob(".partial-*"):
            partial.unlink()
        self.prepared = None

    def usage(self):
        entries = list(self.directory.iterdir())
        return sum(p.lstat().st_size for p in entries), len(entries)

    def capacity(self, size):
        used, count = self.usage()
        if size > MAX_FILE_BYTES or used + size > self.max_stored_bytes or count >= 1024:
            raise WorkerError("transfer_limit", "Private file staging limit exceeded", 413)

    def blob(self, identifier, direction):
        return self.directory / (direction + "-" + file_id(identifier))

    def read_blob(self, identifier, direction):
        try:
            fd = os.open(self.blob(identifier, direction), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise WorkerError("file_not_found", "Staged file is unavailable", 404) from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise WorkerError("invalid_staging", "Staged file is not a bounded regular file", 422)
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise WorkerError("transfer_limit", "Staged file exceeds the byte limit", 413)
        return data

    def put(self, identifier, data, direction="import"):
        path = self.blob(identifier, direction)
        if path.exists():
            if self.read_blob(identifier, direction) == data:
                return metadata(identifier, data)
            raise WorkerError("file_id_conflict", "File ID already contains different bytes")
        self.capacity(len(data))
        temporary = self.directory / (".partial-" + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path, follow_symlinks=False)
        except OSError:
            raise WorkerError("staging_failed", "Cannot persist private staged file", 503) from None
        finally:
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return metadata(identifier, data)

    def remove(self, identifier):
        for direction in ("import", "export"):
            self.blob(identifier, direction).unlink(missing_ok=True)

    def parent(self, path):
        absolute = path.startswith("/")
        parts = path[1:].split("/") if absolute else path.split("/")
        if (not parts or any(part in {"", ".", ".."} for part in parts)
                or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise WorkerError("invalid_path", "Use a regular guest file path without traversal", 422)
        fd = None
        try:
            fd = os.open("/" if absolute else self.workspace,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except OSError:
            if fd is not None:
                os.close(fd)
            raise WorkerError("invalid_path", "Guest parent directory is unavailable", 422) from None

    def read_guest(self, path, limit):
        parent, name = self.parent(path)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise WorkerError("invalid_path", "Guest path must name a regular file", 422)
                if before.st_size > limit:
                    raise WorkerError("transfer_limit", "Guest file exceeds the byte limit", 413)
                data = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                if (len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise WorkerError("file_changed", "Guest file changed while being read")
            return data
        except OSError:
            raise WorkerError("invalid_path", "Guest regular file is unavailable", 422) from None
        finally:
            os.close(parent)

    def destination(self, path, overwrite):
        parent, name = self.parent(path)
        try:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode) or not overwrite:
                raise WorkerError("destination_exists", "Destination exists or is not a regular file")
        finally:
            os.close(parent)

    def validate(self, name, arguments):
        self.prepared = None
        file_id(arguments.file_id)
        if name == "transfer_import":
            data = self.read_blob(arguments.file_id, "import")
            meta = metadata(arguments.file_id, data)
            if (meta["sha256"], meta["size_bytes"]) != (arguments.sha256, arguments.size_bytes):
                raise WorkerError("file_integrity", "Staged file does not match the approved bytes", 422)
            self.destination(arguments.path, arguments.overwrite)
        else:
            data = self.read_guest(arguments.path, arguments.max_bytes)
            self.capacity(len(data))
            if self.blob(arguments.file_id, "export").exists():
                raise WorkerError("file_id_conflict", "Export file ID already exists")
        self.prepared = (name, arguments.model_dump(), data)

    async def perform(self, name, arguments):
        prepared, self.prepared = self.prepared, None
        if prepared is None or prepared[:2] != (name, arguments.model_dump()):
            raise WorkerError("transfer_unprepared", "Transfer was not prepared")
        data = prepared[2]
        if name == "transfer_import":
            self.destination(arguments.path, arguments.overwrite)
            parent, destination = self.parent(arguments.path)
            temporary = ".meerkat-transfer-" + uuid.uuid4().hex
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if arguments.overwrite:
                    os.replace(temporary, destination, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temporary, destination, src_dir_fd=parent, dst_dir_fd=parent,
                            follow_symlinks=False)
                    os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)
            self.remove(arguments.file_id)
            operation = "file_imports"
        else:
            self.put(arguments.file_id, data, "export")
            operation = "file_exports"
        return [text_block({**metadata(arguments.file_id, data), "path": arguments.path})], {
            operation: 1, operation + "_bytes": len(data),
        }


def routes(app, worker):
    """Raw transport requires both the worker credential and a current control fence."""
    import asyncio

    from fastapi import Request
    from fastapi.responses import Response

    def authority(request, capability):
        worker.authorize(request.headers.get("authorization"))
        worker.ensure_effects_allowed()
        lease = worker.journal.lease()
        if (lease is None or request.headers.get("x-meerkat-lease-id") != lease.lease_id
                or request.headers.get("x-meerkat-epoch") != str(lease.epoch)):
            raise WorkerError("stale_lease", "Control lease is not current")
        if lease.expires_at_ms <= worker.now_ms():
            raise WorkerError("lease_expired", "Control lease expired")
        if worker.applied_lease != (lease.lease_id, lease.epoch):
            raise WorkerError("worker_not_ready", "Current lease configuration was not applied", 503)
        if capability and capability not in lease.capabilities:
            raise WorkerError("capability_denied", "Transfer direction is not enabled", 403)

    @app.put("/v1/transfers/{identifier}")
    async def put(identifier: str, request: Request):
        async with worker.lock:
            authority(request, "file_import")
            file_id(identifier)
            value = bytearray()
            already_staged = worker.transfers.blob(identifier, "import").exists()
            try:
                async with asyncio.timeout(30):
                    async for chunk in request.stream():
                        if len(value) + len(chunk) > MAX_FILE_BYTES:
                            raise WorkerError("transfer_limit", "File exceeds the byte limit", 413)
                        value.extend(chunk)
                        if not already_staged:
                            worker.transfers.capacity(len(value))
            except TimeoutError:
                raise WorkerError("transfer_timeout", "File body did not arrive in time", 408) from None
            except WorkerError:
                raise
            except Exception:  # noqa: BLE001 - request stream diagnostics are private
                raise WorkerError("transfer_interrupted", "File body was incomplete", 400) from None
            authority(request, "file_import")
            return worker.transfers.put(identifier, bytes(value))

    @app.get("/v1/transfers/{identifier}")
    async def get(identifier: str, request: Request):
        async with worker.lock:
            authority(request, "file_export")
            data = worker.transfers.read_blob(identifier, "export")
            return Response(data, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.delete("/v1/transfers/{identifier}")
    async def delete(identifier: str, request: Request):
        async with worker.lock:
            authority(request, None)
            worker.transfers.remove(identifier)
            return {"deleted": identifier}
