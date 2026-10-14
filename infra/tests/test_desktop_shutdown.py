"""Bounded shutdown and systemd signal-routing contracts."""

import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_session", INFRA / "desktop-session.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


def test_unresponsive_worker_is_killed_and_reaped_within_shutdown_budget(tmp_path):
    ready = tmp_path / "ready"
    session = desktop.Session(dict(os.environ), startup_seconds=5)
    worker = session.spawn(
        [
            sys.executable,
            "-c",
            (
                "import signal,time,pathlib,sys; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
            ),
            str(ready),
        ],
        worker=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert worker.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        started = time.monotonic()
        session.close(worker_grace_seconds=0.05, grace_seconds=0.05)
        assert time.monotonic() - started < 2
        assert worker.poll() == -signal.SIGKILL
    finally:
        desktop.stop_processes(session.processes, grace_seconds=0.05)


def test_service_preserves_supervisor_shutdown_order():
    provision = (INFRA / "provision-worker.sh").read_text()
    # systemd must send the initial TERM only to the main supervisor/worker;
    # its final KILL still contains every descendant in the service cgroup.
    assert "\nKillMode=mixed\n" in provision
    assert "\nTimeoutStopSec=20\n" in provision
