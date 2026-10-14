"""Source and binary provenance shared by native acceptance preparation and runs."""

import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from prepare_native_console import (
    META_REV,
    ROOT,
    require_compatible_release,
    validate_release_source,
)


def clean_env():
    return {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
        if key in os.environ
    }


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(source, *args):
    return subprocess.check_output(["git", *args], cwd=source, env=clean_env(), text=True).strip()


def computer_sources(root=ROOT):
    files = [root / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")]
    for directory in ("src", "configs", "ui", "python/meerkat_computer"):
        files.extend(
            p
            for p in (root / directory).rglob("*")
            if p.is_file()
            and p.suffix in {".rs", ".py", ".toml", ".html", ".js", ".css"}
            and not {"node_modules", "__pycache__"}.intersection(p.parts)
        )
    files.extend(
        root / name
        for name in (
            "examples/native_console_broker.rs",
            "tests/support/local_worker.rs",
            "scripts/repo-cargo",
        )
    )
    return {str(p.relative_to(root)): digest(p) for p in sorted(files) if p.is_file()}


def native_sources(source):
    require_compatible_release()
    if git(source, "rev-parse", "HEAD") != META_REV:
        raise ValueError("Native source revision differs from the acceptance pin")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Native tracked source or Console assets have changed")
    validate_release_source(source)
    fixture = source / "crates/meerkat-meta/examples/computer_native_acceptance.rs"
    if digest(fixture) != digest(ROOT / "tests/native-console/consumer.rs"):
        raise ValueError("Staged consumer source differs; copy and rebuild it")
    return {"git_revision": META_REV, "consumer_sha256": digest(fixture)}


def verify_build(record, binary, sources):
    if record.get("sources") != sources:
        raise ValueError("Build receipt source drift; rebuild before acceptance")
    if record.get("binary_sha256") != digest(binary):
        raise ValueError("Build receipt binary mismatch; rebuild before acceptance")


def freeze_binary(record, binary, destination):
    with Path(binary).open("rb") as source, destination.open("xb") as output:
        shutil.copyfileobj(source, output)
    if digest(destination) != record["binary_sha256"]:
        raise ValueError("Copied binary differs from build receipt")
    destination.chmod(0o500)
    return destination


def controller_failure(output, error, *, phase="controller"):
    result = output / "result.json"
    if result.exists():
        shutil.copyfile(result, output / "child-result.json")
    failure = {"passed": False, "phase": phase, "error_type": type(error).__name__}
    result.write_text(json.dumps(failure, indent=2) + "\n")


def remove_runtime_state(state, identity, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            current = state.lstat()
            if (current.st_dev, current.st_ino) != identity:
                raise RuntimeError("Native runtime directory identity changed; refusing cleanup")
            shutil.rmtree(state)
            return
        except OSError as error:
            if error.errno == errno.ENOENT and not state.exists():
                return
            if error.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Native runtime cleanup timed out; retained private directory: {state}"
            )
        time.sleep(min(0.05, remaining))


@contextmanager
def private_runtime_state(output, *, parent="/tmp", cleanup_timeout=5.0):
    """Own one private directory and tolerate bounded asynchronous profile cleanup."""
    state = Path(tempfile.mkdtemp(prefix="mc-native-", dir=parent))
    created = state.lstat()
    primary_error = None
    try:
        yield str(state)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            remove_runtime_state(state, (created.st_dev, created.st_ino), cleanup_timeout)
        except BaseException as cleanup_error:
            try:
                if primary_error is None:
                    controller_failure(output, cleanup_error, phase="cleanup")
                (output / "cleanup.json").write_text(
                    json.dumps(
                        {
                            "removed": False,
                            "runtime_state": str(state),
                            "error_type": type(cleanup_error).__name__,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            except OSError as report_error:
                cleanup_error.add_note(
                    f"Cleanup evidence could not be written: {type(report_error).__name__}"
                )
            if primary_error is None:
                raise
            primary_error.add_note(
                f"Native runtime cleanup failed; retained private directory: {state} "
                f"({type(cleanup_error).__name__})"
            )


def run_controller(command, output, env, verify, timeout):
    process = None
    try:
        process = subprocess.Popen(command, cwd=output, env=env, start_new_session=True)
        if process.wait(timeout=timeout):
            raise RuntimeError("Native Console acceptance failed; inspect its evidence directory")
        verify()
        if json.loads((output / "result.json").read_text()).get("passed") is not True:
            raise RuntimeError("Native Console did not issue a passed receipt")
    except BaseException as error:
        controller_failure(output, error)
        raise
    finally:
        if process is not None:
            # Stop this harness's own group. Browsers can have separate groups;
            # the runtime-directory owner handles asynchronous profile cleanup.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
