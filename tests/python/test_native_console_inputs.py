"""Acceptance receipts must reject stale binaries and source drift."""

import errno
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def inputs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("native_console_inputs")


@pytest.fixture
def prepare(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("prepare_native_console")


def test_native_release_gate_refuses_old_core_before_any_preparation(prepare, monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "META_RELEASE", "v0.8.49")
    monkeypatch.setattr(prepare, "CORE_VERSION", "0.8.51")
    output = tmp_path / "not-created"
    monkeypatch.setattr("sys.argv", ["prepare_native_console.py", "--output", str(output)])
    commands = []
    monkeypatch.setattr(prepare, "run", lambda *args: commands.append(args))
    with pytest.raises(ValueError, match=r"v0\.8\.49.*0\.8\.51.*0\.9\.0"):
        prepare.main()
    assert not output.exists()
    assert commands == []


def test_native_build_gate_refuses_old_receipts_before_reading_sources(inputs, prepare, monkeypatch):
    monkeypatch.setattr(prepare, "CORE_VERSION", "0.8.51")
    calls = []
    monkeypatch.setattr(inputs, "git", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="released Meta"):
        inputs.native_sources(Path("not-a-source-checkout"))
    assert calls == []


def test_native_release_tag_and_core_version_are_exact_stable_versions(prepare, monkeypatch):
    monkeypatch.setattr(prepare, "CORE_VERSION", prepare.REQUIRED_CORE_VERSION)
    prepare.require_compatible_release()
    for invalid in ("main", "v0.9.0-rc.1", "v0.9.0+private"):
        monkeypatch.setattr(prepare, "META_RELEASE", invalid)
        with pytest.raises(ValueError, match="stable release"):
            prepare.require_compatible_release()


def released_source(prepare, tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[workspace.package]\nversion = "' + prepare.META_RELEASE.removeprefix("v") + '"\n'
    )
    manifest = tmp_path / "crates/meerkat-meta/Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[dependencies]\nmeerkat = { version = "=' + prepare.CORE_VERSION + '" }\n')
    for name in ("console/mcp-apps-sandbox.cjs", "crates/meerkat-meta/src/http_console/mcp_apps.rs"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    return manifest


def test_native_selection_targets_published_meta_09(prepare):
    assert prepare.META_RELEASE == "v0.9.0"
    assert prepare.META_REV == "b275e0ed4fe61cf4ff43d16691c54efb17812919"
    assert prepare.CORE_REV == "a00bafaf1d862fb41112ff1f30312b350ad05246"
    assert prepare.CORE_VERSION == prepare.REQUIRED_CORE_VERSION == "0.9.0"


def test_prepare_uses_released_meta_tag_and_new_crate_layout(prepare, monkeypatch, tmp_path):
    output = tmp_path / "meta"
    commands = []

    def command(argv, cwd, env):
        commands.append(argv)
        if argv[:2] == ["git", "checkout"]:
            released_source(prepare, cwd)
            (cwd / "crates/meerkat-meta/examples").mkdir()

    monkeypatch.setattr(prepare, "run", command)
    monkeypatch.setattr(prepare.subprocess, "check_output", lambda *a, **kw: prepare.META_REV)
    monkeypatch.setattr("sys.argv", ["prepare_native_console.py", "--output", str(output)])
    prepare.main()
    fetch = next(argv for argv in commands if argv[:2] == ["git", "fetch"])
    assert fetch[-2:] == ["https://github.com/lukacf/meerkat-meta.git", "refs/tags/v0.9.0"]
    assert (output / "crates/meerkat-meta/examples/computer_native_acceptance.rs").is_file()
    receipt = json.loads((tmp_path / "inputs.json").read_text())
    assert receipt["meta_revision"] == prepare.META_REV
    assert "-p meerkat-meta" in receipt["build_command"]
    assert receipt["core_version"] == "0.9.0"


def test_native_manifest_rejects_git_or_floating_core_dependencies(prepare, monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "CORE_VERSION", prepare.REQUIRED_CORE_VERSION)
    manifest = released_source(prepare, tmp_path)
    prepare.validate_release_source(tmp_path)
    for dependency in ('"0.9"', '{ version = "=0.9.0", git = "https://example.test/core" }',
                       '{ version = "=0.9.0", path = "../private-core" }'):
        manifest.write_text('[dependencies]\nmeerkat = ' + dependency + '\n')
        with pytest.raises(ValueError, match="released Meerkat"):
            prepare.validate_release_source(tmp_path)


def test_native_manifest_requires_actual_apps_host_and_matching_release(prepare, monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "CORE_VERSION", prepare.REQUIRED_CORE_VERSION)
    released_source(prepare, tmp_path)
    (tmp_path / "console/mcp-apps-sandbox.cjs").unlink()
    with pytest.raises(ValueError, match="MCP Apps"):
        prepare.validate_release_source(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[workspace.package]\nversion = "0.9.0-rc.1"\n')
    with pytest.raises(ValueError, match="release version"):
        prepare.validate_release_source(tmp_path)


def test_build_receipt_rejects_changed_binary(inputs, tmp_path):
    binary = tmp_path / "fixture"
    binary.write_bytes(b"built")
    record = {"binary_sha256": inputs.digest(binary), "sources": {"src/main.rs": "old"}}
    inputs.verify_build(record, binary, record["sources"])
    binary.write_bytes(b"different executable")
    with pytest.raises(ValueError, match="binary"):
        inputs.verify_build(record, binary, record["sources"])


def test_build_receipt_rejects_changed_source(inputs, tmp_path):
    binary = tmp_path / "fixture"
    binary.write_bytes(b"built")
    record = {"binary_sha256": inputs.digest(binary), "sources": {"src/main.rs": "old"}}
    with pytest.raises(ValueError, match="source"):
        inputs.verify_build(record, binary, {"src/main.rs": "new"})


def test_native_pin_rejects_modified_console_assets(inputs, monkeypatch, tmp_path):
    monkeypatch.setattr(inputs, "require_compatible_release", lambda: None)
    monkeypatch.setattr(
        inputs,
        "git",
        lambda *args: (
            inputs.META_REV if args[1:] == ("rev-parse", "HEAD") else " M console/dist/index.html"
        ),
    )
    with pytest.raises(ValueError, match="tracked"):
        inputs.native_sources(tmp_path)


def test_source_receipt_includes_embedded_assets(inputs, tmp_path):
    for name in ("src/lib.rs", "ui/preview.js", "python/meerkat_computer/private_client.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before")
    first = inputs.computer_sources(tmp_path)
    (tmp_path / "ui/preview.js").write_text("after")
    assert first != inputs.computer_sources(tmp_path)
    assert "python/meerkat_computer/private_client.py" in first


def test_binary_replaced_between_verification_and_copy_is_rejected(inputs, tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"original")
    record = {"binary_sha256": inputs.digest(binary)}
    binary.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="Copied binary"):
        inputs.freeze_binary(record, binary, tmp_path / "frozen")


def test_later_cache_mutation_cannot_change_frozen_executable(inputs, tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"original")
    record = {"binary_sha256": inputs.digest(binary)}
    frozen = inputs.freeze_binary(record, binary, tmp_path / "frozen")
    binary.write_bytes(b"replacement")
    assert inputs.digest(frozen) == record["binary_sha256"]


def test_controller_timeout_overrides_premature_success(inputs, tmp_path):
    import json
    import subprocess

    (tmp_path / "result.json").write_text('{"passed":true}')
    inputs.controller_failure(tmp_path, subprocess.TimeoutExpired("fixture", 1))
    assert json.loads((tmp_path / "result.json").read_text())["passed"] is False
    assert json.loads((tmp_path / "child-result.json").read_text())["passed"] is True


def test_hung_controller_is_reaped_and_cannot_leave_success(inputs, tmp_path):
    import json
    import subprocess
    import sys

    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import time; Path('result.json').write_text('{\"passed\":true}'); time.sleep(30)",
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        inputs.run_controller(command, tmp_path, inputs.clean_env(), lambda: None, timeout=0.2)
    assert json.loads((tmp_path / "result.json").read_text())["passed"] is False


def cleanup_clock(inputs, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        inputs,
        "time",
        SimpleNamespace(
            monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay)
        ),
        raising=False,
    )


def test_runtime_cleanup_retries_profile_flush_without_replacing_primary_error(
    inputs, tmp_path, monkeypatch
):
    cleanup_clock(inputs, monkeypatch)
    remove = inputs.shutil.rmtree
    attempts = []

    def flushing(path):
        attempts.append(Path(path))
        if len(attempts) < 3:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(Path(path) / "Default"))
        remove(path)

    monkeypatch.setattr(inputs.shutil, "rmtree", flushing)
    primary = RuntimeError("original native failure")
    with (
        pytest.raises(RuntimeError) as raised,
        inputs.private_runtime_state(tmp_path, parent=tmp_path, cleanup_timeout=0.2) as state,
    ):
        profile = Path(state) / "Default"
        profile.mkdir()
        (profile / "Preferences").write_text("fixture")
        raise primary
    assert raised.value is primary
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert not Path(state).exists()


def test_unsettled_cleanup_preserves_primary_receipt_and_private_directory(
    inputs, tmp_path, monkeypatch
):
    cleanup_clock(inputs, monkeypatch)
    monkeypatch.setattr(
        inputs.shutil,
        "rmtree",
        lambda path: (_ for _ in ()).throw(OSError(errno.ENOTEMPTY, "Directory not empty")),
    )
    primary = RuntimeError("original native failure")
    (tmp_path / "result.json").write_text('{"passed":true}')
    with (
        pytest.raises(RuntimeError) as raised,
        inputs.private_runtime_state(tmp_path, parent=tmp_path, cleanup_timeout=0.2) as state,
    ):
        inputs.controller_failure(tmp_path, primary)
        raise primary
    assert raised.value is primary and Path(state).is_dir()
    assert Path(state).stat().st_mode & 0o777 == 0o700
    assert json.loads((tmp_path / "result.json").read_text())["error_type"] == "RuntimeError"
    assert json.loads((tmp_path / "child-result.json").read_text())["passed"] is True
    cleanup = json.loads((tmp_path / "cleanup.json").read_text())
    assert cleanup["runtime_state"] == state and cleanup["removed"] is False
    assert any(state in note for note in primary.__notes__)


def test_cleanup_timeout_cannot_leave_a_passed_acceptance_receipt(inputs, tmp_path, monkeypatch):
    cleanup_clock(inputs, monkeypatch)
    monkeypatch.setattr(
        inputs.shutil,
        "rmtree",
        lambda path: (_ for _ in ()).throw(OSError(errno.ENOTEMPTY, "Directory not empty")),
    )
    (tmp_path / "result.json").write_text('{"passed":true}')
    with (
        pytest.raises(TimeoutError, match="cleanup"),
        inputs.private_runtime_state(tmp_path, parent=tmp_path, cleanup_timeout=0.2) as state,
    ):
        assert Path(state).is_dir()
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["passed"] is False and result["phase"] == "cleanup"
    assert json.loads((tmp_path / "child-result.json").read_text())["passed"] is True
    assert Path(state).is_dir()


def test_cleanup_refuses_a_replacement_directory_at_the_owned_path(inputs, tmp_path):
    (tmp_path / "result.json").write_text('{"passed":true}')
    with (
        pytest.raises(RuntimeError, match="identity"),
        inputs.private_runtime_state(tmp_path, parent=tmp_path) as state,
    ):
        original = Path(state)
        retained = tmp_path / "retained-original"
        original.rename(retained)
        original.mkdir()
        (original / "unrelated").write_text("preserve")
    assert (original / "unrelated").read_text() == "preserve"
    assert retained.is_dir()


def test_cleanup_metadata_write_failure_cannot_preserve_success(inputs, tmp_path, monkeypatch):
    cleanup_clock(inputs, monkeypatch)
    monkeypatch.setattr(
        inputs.shutil,
        "rmtree",
        lambda path: (_ for _ in ()).throw(OSError(errno.ENOTEMPTY, "Directory not empty")),
    )
    write = Path.write_text

    def refuse_cleanup_metadata(path, *args, **kwargs):
        if path.name == "cleanup.json":
            raise PermissionError("Cannot write cleanup metadata")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse_cleanup_metadata)
    (tmp_path / "result.json").write_text('{"passed":true}')
    with (
        pytest.raises(TimeoutError, match="cleanup"),
        inputs.private_runtime_state(tmp_path, parent=tmp_path, cleanup_timeout=0.2),
    ):
        pass
    assert json.loads((tmp_path / "result.json").read_text())["passed"] is False


def test_native_fixture_exposes_only_narrow_control_routes():
    source = (Path(__file__).resolve().parents[1] / "native-console/consumer.rs").read_text()
    assert set(re.findall(r'\.route\(\s*"([^"]+)"', source)) == {
        "/state", "/completion", "/release"
    }
    assert "handle_unified_rpc_json" not in source
