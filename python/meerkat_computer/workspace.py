"""Bounded workspace file I/O and explicit guest shell execution."""

import asyncio
import os
import signal
from pathlib import Path, PurePosixPath

from .apps import desktop_environment
from .browser import text_block
from .protocol import WorkerError

MAX_FILE_BYTES = 1_048_576
MAX_COMMAND_OUTPUT_BYTES = 65_536


class Workspace:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    def parts(self, path: str) -> tuple[str, ...]:
        parts = PurePosixPath(path).parts
        if not parts or PurePosixPath(path).is_absolute() or ".." in parts or "\x00" in path:
            raise WorkerError("invalid_path", "Use a relative path inside the workspace", 422)
        return parts

    def parent(self, path: str) -> tuple[int, str]:
        parts = self.parts(path)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except OSError:
            os.close(fd)
            raise WorkerError(
                "invalid_path", "Parent directory is missing or not a workspace directory", 422
            ) from None

    def validate(self, name: str, arguments):
        if name == "workspace_exec":
            return
        if name == "workspace_write" and len(arguments.text.encode()) > MAX_FILE_BYTES:
            raise WorkerError("file_too_large", "File exceeds the write limit", 422)
        parent_fd, filename = self.parent(arguments.path)
        try:
            try:
                info = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name == "workspace_read":
                    raise WorkerError(
                        "file_not_found", "Workspace file does not exist", 422
                    ) from None
                return
            import stat

            if not stat.S_ISREG(info.st_mode):
                raise WorkerError(
                    "invalid_path", "Path must name a regular file, not a symlink or device", 422
                )
        finally:
            os.close(parent_fd)

    async def perform(self, name: str, arguments) -> tuple[list[dict], dict]:
        if name == "workspace_exec":
            return await self.execute(arguments.command, arguments.timeout_seconds)
        parent_fd, filename = self.parent(arguments.path)
        try:
            flags = (
                os.O_RDONLY if name == "workspace_read" else os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            )
            fd = os.open(
                filename, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode=0o600, dir_fd=parent_fd
            )
            import stat

            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise WorkerError("invalid_path", "Path must remain a regular file", 422)
            if name == "workspace_read":
                with os.fdopen(fd, "rb") as stream:
                    value = stream.read(MAX_FILE_BYTES + 1)
                return [
                    text_block(
                        {
                            "path": arguments.path,
                            "text": value[:MAX_FILE_BYTES].decode("utf-8", errors="replace"),
                            "truncated": len(value) > MAX_FILE_BYTES,
                        }
                    )
                ], {"file_reads": 1, "file_bytes_read": min(len(value), MAX_FILE_BYTES)}
            value = arguments.text.encode()
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            return [text_block({"path": arguments.path, "bytes_written": len(value)})], {
                "file_writes": 1,
                "file_bytes_written": len(value),
            }
        finally:
            os.close(parent_fd)

    async def execute(self, command: str, timeout: int) -> tuple[list[dict], dict]:
        # Only guest tool execution is granted. This is not a shell filesystem sandbox.
        # The worker's bearer token and broker credentials must not be inherited.
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "HOME": str(self.root),
            "TMPDIR": "/tmp",
            **desktop_environment(),
        }
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=self.root,
            env=env,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        counts = {"stdout": 0, "stderr": 0}

        async def drain(name, stream):
            while chunk := await stream.read(8192):
                counts[name] += len(chunk)
                remaining = MAX_COMMAND_OUTPUT_BYTES - sum(map(len, outputs.values()))
                outputs[name].extend(chunk[: max(0, remaining)])

        pending = asyncio.gather(
            process.wait(), drain("stdout", process.stdout), drain("stderr", process.stderr)
        )
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(pending), timeout)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(pending, 5)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            pending.cancel()
            raise
        body = {
            name: bytes(data).decode("utf-8", errors="replace") for name, data in outputs.items()
        }
        body.update(
            exit_code=process.returncode,
            timed_out=timed_out,
            truncated=sum(counts.values()) > MAX_COMMAND_OUTPUT_BYTES,
            output_bytes=sum(counts.values()),
        )
        return [text_block(body)], {"exec_commands": 1, "exec_timeouts": int(timed_out)}
