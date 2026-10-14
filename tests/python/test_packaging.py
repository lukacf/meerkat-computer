"""Release boundaries: actual contents, provenance and install inputs."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def packaging():
    spec = importlib.util.spec_from_file_location(
        "package_artifacts", ROOT / "scripts/package_artifacts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_metadata_and_rust_version_agree(packaging):
    metadata = packaging.project_metadata(ROOT)
    assert metadata["version"] == "0.1.0"
    assert metadata["license"] == "MIT"


def test_version_drift_is_rejected(packaging, tmp_path):
    for name in ("Cargo.toml", "pyproject.toml", "Cargo.lock", "rust-toolchain.toml"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text().replace('version = "0.1.0"', 'version = "0.1.1"'))
    with pytest.raises(ValueError, match="version"):
        packaging.project_metadata(tmp_path)


@pytest.mark.parametrize("dependency", ["meerkat-machine-dsl", "meerkat-machine-schema"])
def test_floating_machine_dependency_is_rejected(packaging, tmp_path, dependency):
    for name in ("Cargo.toml", "pyproject.toml", "Cargo.lock", "rust-toolchain.toml"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    path = tmp_path / "Cargo.toml"
    source = path.read_text()
    revision = tomllib.loads(source)["dependencies"][dependency]["rev"]
    lines = source.splitlines(keepends=True)
    path.write_text("".join(
        line.replace(f'rev = "{revision}"', 'rev = "main"')
        if line.startswith(f"{dependency} = ") else line
        for line in lines
    ))
    assert tomllib.loads(path.read_text())["dependencies"][dependency]["rev"] == "main"
    with pytest.raises(ValueError, match="commit"):
        packaging.project_metadata(tmp_path)


def test_private_and_generated_paths_are_never_source_artifacts(packaging, tmp_path):
    for name in (
        "README.md",
        "LICENSE",
        "python/meerkat_computer/worker.py",
        "src/main.rs",
        ".state/operator.token",
        "python/meerkat_computer/__pycache__/worker.pyc",
        "docs/node_modules/private/token",
        ".env",
        "target/debug/rkat-computer",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    assert packaging.source_files(tmp_path) == [
        Path("LICENSE"),
        Path("README.md"),
        Path("python/meerkat_computer/worker.py"),
        Path("src/main.rs"),
    ]


def test_source_symlink_cannot_import_outside_material(packaging, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/private.rs").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        packaging.source_files(tmp_path)


def test_root_source_directory_symlink_is_rejected(packaging, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_text("private")
    source = tmp_path / "source"
    source.mkdir()
    (source / "python").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        packaging.source_files(source)


def test_ignored_local_files_cannot_enter_a_source_archive(packaging, tmp_path):
    subprocess.run(["git", "init", "--quiet", tmp_path], check=True)
    (tmp_path / ".gitignore").write_text(".playwright-cli/\nconfigs/local-credentials.json\n")
    paths = (
        "README.md",
        "python/meerkat_computer/new_module.py",
        "python/meerkat_computer/resources/preview-v1.html",
        "docs/.playwright-cli/session.json",
        "ui/mcp-app/.playwright-cli/page.yml",
        "configs/local-credentials.json",
    )
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    assert packaging.source_files(tmp_path) == [
        Path(".gitignore"),
        Path("README.md"),
        Path("python/meerkat_computer/new_module.py"),
        Path("python/meerkat_computer/resources/preview-v1.html"),
    ]


def test_browser_state_is_excluded_without_git_metadata(packaging, tmp_path):
    state = tmp_path / "docs/.playwright-cli/session.json"
    state.parent.mkdir(parents=True)
    state.write_text("private browser state")
    assert packaging.source_files(tmp_path) == []


def test_source_archive_includes_repository_editor_and_review_contract(packaging, tmp_path):
    for name in (
        ".editorconfig",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "SECURITY.md",
        "CHANGELOG.md",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source contract\n")
    assert set(packaging.source_files(tmp_path)) == {
        Path(".editorconfig"),
        Path(".github/PULL_REQUEST_TEMPLATE.md"),
        Path("SECURITY.md"),
        Path("CHANGELOG.md"),
    }


@pytest.mark.parametrize("enclosing_checkout", [False, True])
def test_package_check_from_extracted_source_has_honest_unversioned_provenance(
    packaging, tmp_path, monkeypatch, enclosing_checkout
):
    if enclosing_checkout:
        subprocess.run(["git", "init", "--quiet", tmp_path], check=True)
    source = tmp_path / "extracted"
    source.mkdir()
    (source / "README.md").write_text("source archive\n")
    monkeypatch.setattr(packaging, "ROOT", source)
    actual_run = packaging.run

    def run(args, **kwargs):
        if list(args) == ["uv", "--version"]:
            return SimpleNamespace(stdout="uv 0.10.9")
        return actual_run(args, **{**kwargs, "cwd": source})

    monkeypatch.setattr(packaging, "run", run)
    monkeypatch.setattr(packaging, "check_python", lambda *args: {"version": "0.1.0"})
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["package_artifacts.py", "check", "--output", str(output)])
    packaging.main()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["git_head"] is None
    assert manifest["working_tree_modified"] is None
    assert manifest["source_provenance"] == "unversioned_directory"
    assert manifest["source_sha256"] == {"README.md": packaging.digest(source / "README.md")}


def test_unversioned_directory_cannot_create_release_bundle(packaging):
    with pytest.raises(ValueError, match="Git checkout"):
        packaging.validate_release_tree("bundle", modified=None, allow_dirty=True)


def test_package_manifest_rejects_commit_change_during_validation(packaging, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    heads = iter(["a" * 40, "b" * 40])

    def run(args, **kwargs):
        if list(args) == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(stdout=str(source))
        if list(args) == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=next(heads))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(packaging, "ROOT", source)
    monkeypatch.setattr(packaging, "run", run)
    monkeypatch.setattr(packaging, "source_hashes", lambda *args: {})
    monkeypatch.setattr(packaging, "check_python", lambda *args: {"version": "0.1.0"})
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["package_artifacts.py", "check", "--output", str(output)])
    with pytest.raises(ValueError, match="Source.*changed"):
        packaging.main()
    assert not (output / "manifest.json").exists()


def test_bundle_uses_the_current_compiler_artifact_not_a_stale_host_path(
    packaging, tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    stale_root = tmp_path / "old-target"
    stale_binary = stale_root / "release/rkat-computer"
    stale_binary.parent.mkdir(parents=True)
    stale_binary.write_bytes(b"old candidate with the same package version")
    compiled = tmp_path / "actual-target/aarch64-apple-darwin/release/rkat-computer"
    compiled.parent.mkdir(parents=True)
    compiled.write_bytes(b"current candidate")
    build_calls = []

    def run(args, **kwargs):
        args = list(map(str, args))
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(stdout=str(source))
        if args[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="")
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="a" * 40)
        if args[:3] == ["git", "show", "-s"]:
            return SimpleNamespace(stdout="1234567890")
        if args == ["uv", "--version"]:
            return SimpleNamespace(stdout="uv 0.10.9")
        if args == ["rustc", "-vV"]:
            return SimpleNamespace(stdout="rustc 1.94.1\nhost: aarch64-apple-darwin\n")
        if args[0].endswith("repo-cargo"):
            if args[1] == "--print-env":
                return SimpleNamespace(stdout=f"CARGO_TARGET_DIR={stale_root}\n")
            build_calls.append(args)
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "reason": "compiler-artifact",
                        "manifest_path": str(source / "Cargo.toml"),
                        "target": {"name": "rkat-computer", "kind": ["bin"]},
                        "executable": str(compiled),
                    }
                )
                + '\n{"reason":"build-finished","success":true}\n'
            )
        if args[0] in {str(stale_binary), str(compiled)}:
            return SimpleNamespace(stdout="rkat-computer 0.1.0" if args[1] == "--version" else "")
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(packaging, "ROOT", source)
    monkeypatch.setattr(packaging, "run", run)
    monkeypatch.setattr(packaging, "source_hashes", lambda root: {})
    monkeypatch.setattr(packaging, "check_python", lambda *args: {"version": "0.1.0"})
    monkeypatch.setattr(
        packaging,
        "write_source_archive",
        lambda root, output, *args: output.write_bytes(b"source archive"),
    )
    monkeypatch.setenv("CARGO_BUILD_TARGET", "x86_64-unknown-linux-gnu")
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["package_artifacts.py", "bundle", "--output", str(output)])
    packaging.main()
    candidate = output / "rkat-computer-0.1.0-aarch64-apple-darwin"
    assert candidate.read_bytes() == b"current candidate"
    assert build_calls[0][build_calls[0].index("--target") + 1] == "aarch64-apple-darwin"
    assert "--message-format=json-render-diagnostics" in build_calls[0]


@pytest.mark.parametrize("fault", ["wrong_project", "failed_build", "missing_file", "ambiguous"])
def test_operator_artifact_must_be_unique_present_and_successful(packaging, tmp_path, fault):
    binary = tmp_path / "rkat-computer"
    binary.write_bytes(b"candidate")
    manifest = tmp_path / "Cargo.toml"
    artifact = {
        "reason": "compiler-artifact",
        "manifest_path": str(manifest),
        "target": {"name": "rkat-computer", "kind": ["bin"]},
        "executable": str(binary),
    }
    if fault == "wrong_project":
        artifact["manifest_path"] = str(tmp_path / "dependency/Cargo.toml")
    elif fault == "missing_file":
        binary.unlink()
    messages = [artifact]
    if fault == "ambiguous":
        other = tmp_path / "other-rkat-computer"
        other.write_bytes(b"other candidate")
        messages.append({**artifact, "executable": str(other)})
    messages.append({"reason": "build-finished", "success": fault != "failed_build"})
    with pytest.raises(ValueError, match="operator executable"):
        packaging.operator_artifact("\n".join(map(json.dumps, messages)), manifest)


def test_release_candidate_rejects_modified_tree_without_explicit_override(packaging):
    with pytest.raises(ValueError, match="clean"):
        packaging.validate_release_tree("bundle", modified=True, allow_dirty=False)
    packaging.validate_release_tree("check", modified=True, allow_dirty=False)
    packaging.validate_release_tree("bundle", modified=True, allow_dirty=True)
    packaging.validate_release_tree("bundle", modified=False, allow_dirty=False)


def test_source_archive_is_reproducible_and_retains_executable_mode(packaging, tmp_path):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/repo-cargo"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    (root / "README.md").write_text("Read me\n")
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    packaging.write_source_archive(root, first, "computer-0.1.0", 123)
    packaging.write_source_archive(root, second, "computer-0.1.0", 123)
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        entry = archive.getmember("computer-0.1.0/scripts/repo-cargo")
        assert entry.mode == 0o755 and entry.uid == entry.gid == 0


def test_package_rejects_hidden_state_even_if_under_package_directory(packaging):
    with pytest.raises(ValueError, match="private|generated"):
        packaging.validate_members(["meerkat_computer/.state/operator.token"])
    with pytest.raises(ValueError, match="path"):
        packaging.validate_members(["../operator.token"])


def test_wheel_requires_bundled_preview_and_license(packaging, tmp_path):
    wheel = tmp_path / "incomplete.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("meerkat_computer/mcp_adapter.py", "# adapter\n")
    with pytest.raises(ValueError, match="preview|license|metadata"):
        packaging.inspect_wheel(wheel, ROOT)


def test_archive_extraction_rejects_symlinks(packaging, tmp_path):
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        link = tarfile.TarInfo("package/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    with pytest.raises(ValueError, match="link"):
        packaging.extract_sdist(archive_path, tmp_path / "expanded")


def test_manifest_records_each_artifact_hash(packaging, tmp_path):
    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"test artifact")
    packaging.write_manifest(tmp_path, {"kind": "test"}, [artifact])
    receipt = json.loads((tmp_path / "manifest.json").read_text())
    assert receipt["artifacts"][0]["sha256"] == packaging.digest(artifact)
    assert receipt["artifacts"][0]["bytes"] == 13
    assert "package.whl" in (tmp_path / "SHA256SUMS").read_text()


def test_sdist_missing_package_metadata_cannot_be_rebuilt(packaging, tmp_path):
    archive_path = tmp_path / "incomplete.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("package/worker.py")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="pyproject"):
        packaging.extract_sdist(archive_path, tmp_path / "expanded")


def test_source_archive_cargo_wrapper_does_not_need_git_metadata(tmp_path):
    source = tmp_path / "source with spaces"
    (source / "scripts").mkdir(parents=True)
    wrapper = source / "scripts/repo-cargo"
    shutil.copyfile(ROOT / "scripts/repo-cargo", wrapper)
    wrapper.chmod(0o755)
    result = subprocess.run(
        [str(wrapper), "--print-env"],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "RUST_LANE_ID": "package-wrapper-test",
        },
        check=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert values["repo_root"] == str(source)
    assert not Path(values["CARGO_TARGET_DIR"]).is_relative_to(source)
    assert values["lane_key"] == "package-wrapper-test"
