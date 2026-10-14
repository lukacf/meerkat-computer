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
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
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


def test_success_configures_sandbox_and_propagates_setup_failure(installer, monkeypatch):
    configured = []

    def configure():
        configured.append(True)
        return 7

    monkeypatch.setattr(installer, "configure_sandbox", configure, raising=False)
    result, calls, waits = exercise(
        installer, monkeypatch, [SimpleNamespace(returncode=0, stdout="ready")]
    )
    assert configured == [True]
    assert result == 7 and len(calls) == 1 and waits == []


def browsers(tmp_path):
    root = tmp_path.resolve() / "runner cache"
    chrome = root / "chromium-1243/chrome-linux64/chrome"
    shell = root / "chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell"
    for executable in (chrome, shell):
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"\x7fELFfake test browser")
        executable.chmod(0o755)
    inventory = (
        f"Chrome for Testing 153.0 (playwright chromium v1243)\n"
        f"  Install location:    {chrome.parent.parent}\n\n"
        f"FFmpeg (playwright ffmpeg v1011)\n  Install location:    {root}/ffmpeg-1011\n\n"
        f"Chrome Headless Shell 153.0 (playwright chromium-headless-shell v1243)\n"
        f"  Install location:    {shell.parent.parent}\n"
    )
    return chrome, shell, inventory


def test_inventory_selects_exact_current_browsers_and_ignores_other_caches(installer, tmp_path):
    chrome, shell, inventory = browsers(tmp_path)
    old = chrome.parent.parent.parent / "chromium-1234/chrome"
    old.parent.mkdir()
    old.write_bytes(b"\x7fELFold")
    assert installer.installed_executables(inventory) == [str(chrome), str(shell)]
    profile = installer.render_sandbox_profile(str(chrome))
    assert f'"{chrome}" flags=(unconfined) {{\n  userns,\n}}' in profile
    assert profile == installer.render_sandbox_profile(str(chrome))
    assert profile != installer.render_sandbox_profile(str(shell))
    assert profile.count("profile ") == 1 and "*" not in profile


@pytest.mark.parametrize(
    "path",
    [
        "relative/chrome",
        "/tmp/../outside/chrome",
        "/tmp/./chrome",
        "/tmp//chrome",
        '/tmp/"/chrome',
        "/tmp/*/chrome",
        "/tmp/{a,b}/chrome",
        "/tmp/@{HOME}/chrome",
        "/tmp/[abc]/chrome",
        "/tmp/line\nnext/chrome",
        "/tmp/back\\slash/chrome",
        "/bin/sh",
    ],
)
def test_profile_rejects_ambiguous_or_injectable_paths(installer, path):
    with pytest.raises(ValueError, match="exact installed"):
        installer.render_sandbox_profile(path)


@pytest.mark.parametrize("kind", ["symlink", "non_elf", "not_executable", "duplicate", "missing"])
def test_inventory_rejects_unexpected_binary(installer, tmp_path, kind):
    chrome, _shell, inventory = browsers(tmp_path)
    if kind == "symlink":
        target = chrome.with_name("other")
        chrome.rename(target)
        chrome.symlink_to(target)
    elif kind == "non_elf":
        chrome.write_bytes(b"#!/bin/sh")
    elif kind == "not_executable":
        chrome.chmod(0o644)
    elif kind == "duplicate":
        (chrome.parent.parent / "chrome").write_bytes(b"\x7fELFduplicate")
    else:
        chrome.unlink()
    with pytest.raises(ValueError):
        installer.installed_executables(inventory)


@pytest.mark.parametrize("change", ["missing", "duplicate", "format"])
def test_inventory_format_drift_fails_closed(installer, tmp_path, change):
    _chrome, _shell, inventory = browsers(tmp_path)
    if change == "missing":
        inventory = inventory.split("Chrome Headless Shell")[0]
    elif change == "duplicate":
        inventory += inventory
    else:
        inventory = inventory.replace("Install location:", "Download directory:")
    with pytest.raises(ValueError):
        installer.installed_executables(inventory)


def hosted(installer, monkeypatch, tmp_path):
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    restriction = tmp_path / "restriction"
    restriction.write_text("1\n")
    monkeypatch.setattr(installer, "USERNS_RESTRICTION", restriction)
    return restriction


@pytest.mark.parametrize("mode", ["non_linux", "workstation", "self_hosted", "disabled", "absent"])
def test_policy_is_limited_to_restricted_hosted_linux_jobs(installer, monkeypatch, tmp_path, mode):
    restriction = hosted(installer, monkeypatch, tmp_path)
    if mode == "non_linux":
        monkeypatch.setattr(installer.sys, "platform", "darwin")
    elif mode == "workstation":
        monkeypatch.delenv("GITHUB_ACTIONS")
    elif mode == "self_hosted":
        monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    elif mode == "disabled":
        restriction.write_text("0\n")
    else:
        restriction.unlink()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Policy setup is not authorized on this host")

    monkeypatch.setattr(installer.subprocess, "run", unexpected)
    assert installer.configure_sandbox() == 0


@pytest.mark.parametrize("returncode", [0, 7])
def test_hosted_policy_loads_on_stdin_with_bounded_sudo_and_propagates_failure(
    installer, monkeypatch, tmp_path, returncode
):
    hosted(installer, monkeypatch, tmp_path)
    chrome, shell, inventory = browsers(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout=inventory)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.configure_sandbox() == returncode
    assert calls[0][0] == [
        installer.sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"
    ]
    assert calls[0][1]["check"] and calls[0][1]["timeout"] == 30
    assert calls[1][0] == ["sudo", "-n", "/usr/sbin/apparmor_parser", "--replace"]
    assert calls[1][1]["timeout"] == 30
    policies = calls[1][1]["input"]
    assert policies.count("abi <abi/4.0>,") == 1
    assert policies.count("include <tunables/global>") == 1
    assert policies.count("profile ") == 2
    assert policies.count("  userns,") == 2
    assert f'"{chrome}"' in policies and f'"{shell}"' in policies
    assert "sysctl" not in policies and "--no-sandbox" not in policies
