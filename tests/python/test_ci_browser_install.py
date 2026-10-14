"""Dependency setup may retry known apt locks, never arbitrary failures or tests."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).resolve().parents[2] / "scripts/install_ci_chromium.py"
    spec = importlib.util.spec_from_file_location("install_ci_chromium", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOCK = (
    "E: Could not get lock /var/lib/dpkg/lock-frontend. "
    "It is held by process 32082 (unattended-upgr)\n"
    "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), "
    "is another process using it?\n"
    "Failed to install browsers\nError: Installation process exited with code: 100\n"
)


def exercise(installer, monkeypatch, results, budget=10):
    calls, waits = [], []
    clock = [0.0]

    def run(args, **kwargs):
        calls.append(args)
        return results[min(len(calls) - 1, len(results) - 1)]

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer.time, "sleep", sleep)
    result = installer.install_chromium(lock_wait_seconds=budget)
    return result, calls, waits


def test_known_apt_lock_retries_dependency_install_only(installer, monkeypatch):
    result, calls, waits = exercise(
        installer,
        monkeypatch,
        [SimpleNamespace(returncode=1, stdout=LOCK), SimpleNamespace(returncode=0, stdout="ready")],
    )
    assert result == 0 and len(calls) == 2 and waits == [5]
    assert (
        calls[0]
        == calls[1]
        == [installer.sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]
    )


@pytest.mark.parametrize(
    "output",
    [
        "Error: Download failed: network error",
        "E: Unable to locate package imaginary",
        "E: Could not open lock file /var/lib/dpkg/lock-frontend - open (13: Permission denied)",
        LOCK + "E: Failed to fetch https://example.invalid/pkg\n",
    ],
)
def test_non_lock_failure_is_not_retried(installer, monkeypatch, output):
    result, calls, waits = exercise(
        installer, monkeypatch, [SimpleNamespace(returncode=1, stdout=output)]
    )
    assert result == 1 and len(calls) == 1 and waits == []


def test_lock_retry_budget_is_bounded(installer, monkeypatch):
    result, calls, waits = exercise(
        installer, monkeypatch, [SimpleNamespace(returncode=1, stdout=LOCK)], budget=7
    )
    assert result == 1 and len(calls) == 2 and waits == [5, 2]


def test_success_never_retries(installer, monkeypatch):
    result, calls, waits = exercise(
        installer, monkeypatch, [SimpleNamespace(returncode=0, stdout="ready")]
    )
    assert result == 0 and len(calls) == 1 and waits == []
