"""Bounded reads of guest files for browser uploads, without following symlinks."""

import contextlib
import mimetypes
import os
import stat
from pathlib import PurePosixPath

from .protocol import WorkerError

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_FILES = 16


@contextlib.contextmanager
def regular_guest_file(value):
    path = os.fspath(value)
    parts = PurePosixPath(path).parts
    if (
        not path.startswith("/")
        or path.startswith("//")
        or len(path) > 4096
        or "\x00" in path
        or ".." in parts
        or len(parts) < 2
    ):
        raise WorkerError("invalid_upload_path", "Use an absolute guest file path", 422)
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        directory = os.open("/", flags | os.O_DIRECTORY)
        descriptors.append(directory)
        for part in parts[1:-1]:
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            descriptors.append(directory)
        # O_NONBLOCK prevents a file-to-FIFO race from hanging open; fstat below
        # still rejects anything except an ordinary file before any bytes are read.
        before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise WorkerError("invalid_upload_file", "Upload requires regular guest files", 422)
        descriptor = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkerError("invalid_upload_file", "Upload requires regular guest files", 422)
        yield descriptor
    except OSError:
        # File paths and OS diagnostics can contain private guest information.
        raise WorkerError(
            "invalid_upload_file",
            "Cannot read a regular guest file without following symlinks",
            422,
        ) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_upload_payloads(paths):
    if not 1 <= len(paths) <= MAX_UPLOAD_FILES:
        raise WorkerError("invalid_upload_files", "Upload accepts 1-16 guest files", 422)
    remaining = MAX_UPLOAD_BYTES
    payloads = []
    for path in paths:
        with regular_guest_file(path) as descriptor:
            before = os.fstat(descriptor)
            if before.st_size > remaining:
                raise WorkerError(
                    "upload_too_large", "Upload exceeds the 16 MiB aggregate limit", 422
                )
            chunks = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(65_536, remaining - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > remaining:
                    raise WorkerError(
                        "upload_too_large", "Upload exceeds the 16 MiB aggregate limit", 422
                    )
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise WorkerError("upload_file_changed", "Guest file changed while reading", 409)
        name = PurePosixPath(os.fspath(path)).name
        payloads.append(
            {
                "name": name,
                "mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "buffer": b"".join(chunks),
            }
        )
        remaining -= size
    return payloads
