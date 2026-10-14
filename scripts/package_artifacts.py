#!/usr/bin/env python3
"""Build and verify private distributions without publishing or touching guests."""

import argparse
import email.parser
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {
    ".github",
    "configs",
    "docs",
    "examples",
    "infra",
    "python",
    "scripts",
    "skills",
    "src",
    "tests",
    "ui",
}
SOURCE_NAMES = {
    "Cargo.toml",
    "Cargo.lock",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "LICENSE",
    "rust-toolchain.toml",
    ".gitignore",
    ".editorconfig",
}
EXCLUDED = {
    ".git",
    ".state",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    ".mintlify",
    ".playwright-cli",
    ".cache",
    ".DS_Store",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_release_tree(command, *, modified, allow_dirty):
    if command == "bundle" and modified is None:
        raise ValueError("Release artifacts require a Git checkout with a committed revision")
    if command == "bundle" and modified and not allow_dirty:
        raise ValueError(
            "Release artifacts require a clean tree; --allow-dirty is development only"
        )


def run(args, *, cwd=ROOT, env=None, capture=False, capture_stdout=False):
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture or capture_stdout else None,
        stderr=subprocess.PIPE if capture else None,
    )


def project_metadata(root):
    cargo = tomllib.loads((root / "Cargo.toml").read_text())
    python = tomllib.loads((root / "pyproject.toml").read_text())
    version = cargo["package"]["version"]
    if version != python["project"]["version"]:
        raise ValueError("Rust and Python package version differ")
    if cargo["package"].get("publish") is not False:
        raise ValueError("Private Rust distribution must disable registry publishing")
    if python["project"].get("license") != cargo["package"]["license"]:
        raise ValueError("Rust and Python license metadata differ")
    if python["project"].get("readme") != "README.md":
        raise ValueError("Python package requires its README metadata")
    build_requires = python["build-system"]["requires"]
    if not all(
        re.fullmatch(r"[A-Za-z0-9_-]+==[0-9]+\.[0-9]+\.[0-9]+", item) for item in build_requires
    ):
        raise ValueError("Build backend must use an exact version")
    lock = tomllib.loads((root / "Cargo.lock").read_text())
    dependencies = {}
    for name, dep in cargo["dependencies"].items():
        if isinstance(dep, dict) and "git" in dep:
            revision = dep.get("rev", "")
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError(f"{name} requires an immutable Git commit")
            source = f"git+{dep['git']}?rev={revision}#{revision}"
            if not any(
                item["name"] == name and item.get("source") == source for item in lock["package"]
            ):
                raise ValueError(f"{name} commit differs from Cargo.lock")
            dependencies[name] = revision
    toolchain = tomllib.loads((root / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", toolchain):
        raise ValueError("Rust toolchain must use an exact version")
    return {
        "version": version,
        "license": cargo["package"]["license"],
        "rust_toolchain": toolchain,
        "git_dependencies": dependencies,
    }


def validate_members(names):
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"Unsafe archive path: {name}")
        if any(
            part in EXCLUDED
            or part == ".env"
            or part.startswith(".env.")
            or part.endswith(".egg-info")
            for part in path.parts
        ) or path.suffix in {".pyc", ".pyo"}:
            raise ValueError(f"Private or generated archive member: {name}")


def is_git_checkout(root):
    try:
        top = run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if (root / ".git").exists() or (root / ".git").is_symlink():
            raise ValueError("Cannot enumerate release source from Git") from None
        return False
    return Path(top).resolve() == root.resolve()


def source_revision(root):
    if not is_git_checkout(root):
        return {
            "git_head": None,
            "working_tree_modified": None,
            "source_provenance": "unversioned_directory",
        }
    return {
        "git_head": run(["git", "rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip(),
        "working_tree_modified": bool(
            run(["git", "status", "--porcelain"], cwd=root, capture=True).stdout.strip()
        ),
        "source_provenance": "git_checkout",
    }


def git_source_inventory(root):
    """Include source changes, but never ignored local files in a checkout."""
    if not is_git_checkout(root):
        return None
    listing = run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture=True,
    ).stdout
    return {Path(name) for name in listing.split("\0") if name}


def source_files(root):
    inventory = git_source_inventory(root)
    paths = []
    for entry in root.iterdir():
        if entry.is_symlink() and (
            entry.name in SOURCE_DIRS or entry.name in SOURCE_NAMES or entry.suffix == ".md"
        ):
            raise ValueError(f"Source symlink is not distributable: {entry.name}")
        if entry.is_dir() and entry.name in SOURCE_DIRS:
            candidates = entry.rglob("*")
        elif entry.name in SOURCE_NAMES or entry.suffix == ".md":
            candidates = [entry]
        else:
            continue
        for path in candidates:
            relative = path.relative_to(root)
            if inventory is not None and relative not in inventory:
                continue
            try:
                validate_members([relative.as_posix()])
            except ValueError:
                continue
            if path.is_symlink():
                raise ValueError(f"Source symlink is not distributable: {relative}")
            if path.is_file():
                paths.append(relative)
    return sorted(paths)


def source_hashes(root):
    return {str(path): digest(root / path) for path in source_files(root)}


def write_source_archive(root, destination, prefix, epoch):
    with (
        destination.open("wb") as output,
        gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for path in source_files(root):
            content = (root / path).read_bytes()
            info = tarfile.TarInfo(f"{prefix}/{path.as_posix()}")
            info.size, info.mtime = len(content), epoch
            info.mode = 0o755 if (root / path).stat().st_mode & 0o111 else 0o644
            info.uid = info.gid = 0
            archive.addfile(info, io.BytesIO(content))


def extract_sdist(path, destination):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        validate_members([member.name for member in members])
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise ValueError("Source distribution contains a link or special file")
        tops = {PurePosixPath(member.name).parts[0] for member in members}
        if len(tops) != 1:
            raise ValueError("Source distribution must have one root")
        source = destination / tops.pop()
        if f"{source.name}/pyproject.toml" not in {member.name for member in members}:
            raise ValueError("Source distribution lacks pyproject.toml")
        # Links/special files and escaping paths were rejected above. Manual
        # extraction supports Python 3.11 before tarfile's filter backport.
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return source


def inspect_wheel(path, root):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        validate_members(names)
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError("Wheel lacks unique distribution metadata")
        metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_paths[0]))
        project = project_metadata(root)
        if metadata["Name"] != "meerkat-computer" or metadata["Version"] != project["version"]:
            raise ValueError("Wheel metadata version/name mismatch")
        license_paths = [name for name in names if name.endswith("/licenses/LICENSE")]
        if (
            len(license_paths) != 1
            or archive.read(license_paths[0]) != (root / "LICENSE").read_bytes()
        ):
            raise ValueError("Wheel license is missing or differs from source")
        required = [
            p
            for p in (root / "python/meerkat_computer").rglob("*")
            if p.is_file() and p.suffix in {".py", ".html"} and "__pycache__" not in p.parts
        ]
        for source in required:
            member = source.relative_to(root / "python").as_posix()
            if member not in names or archive.read(member) != source.read_bytes():
                raise ValueError(f"Wheel content missing or stale: {member}")
        html = archive.read("meerkat_computer/resources/preview-v1.html")
        if b"Bundled third-party licenses" not in html:
            raise ValueError("Packaged preview lacks third-party licenses")
        return {"wheel": path.name, "version": metadata["Version"], "members": len(names)}


def write_manifest(output, metadata, artifacts):
    manifest = {
        **metadata,
        "artifacts": [
            {"name": p.name, "bytes": p.stat().st_size, "sha256": digest(p)} for p in artifacts
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksum_paths = [*artifacts, output / "manifest.json"]
    (output / "SHA256SUMS").write_text("".join(f"{digest(p)}  {p.name}\n" for p in checksum_paths))


def check_python(output, uv):
    metadata = project_metadata(ROOT)
    version = metadata["version"]
    # Hatchling's timestamp is fixed for byte-reproducible wheels and sdists.
    env = {**os.environ, "SOURCE_DATE_EPOCH": "315532800"}
    with tempfile.TemporaryDirectory(prefix="computer-package-") as work:
        work = Path(work)
        first, second = work / "first", work / "second"
        for directory in (first, second):
            run(
                [
                    sys.executable,
                    "-m",
                    "hatchling",
                    "build",
                    "-t",
                    "sdist",
                    "-t",
                    "wheel",
                    "-d",
                    directory,
                ],
                env=env,
            )
        wheel_name, sdist_name = (
            f"meerkat_computer-{version}-py3-none-any.whl",
            f"meerkat_computer-{version}.tar.gz",
        )
        for name in (wheel_name, sdist_name):
            if digest(first / name) != digest(second / name):
                raise ValueError(f"Distribution is not byte reproducible: {name}")
        inspection = inspect_wheel(first / wheel_name, ROOT)
        expanded = extract_sdist(first / sdist_name, work / "source")
        run(
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", work / "rebuilt"],
            cwd=expanded,
            env=env,
        )
        if digest(first / wheel_name) != digest(work / "rebuilt" / wheel_name):
            raise ValueError("Wheel rebuilt from sdist differs from direct wheel")
        requirements = work / "requirements.txt"
        exported = run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            capture=True,
        )
        requirements.write_text(exported.stdout)
        installed = work / "installed"
        run([uv, "venv", "--python", sys.executable, installed])
        python = installed / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([uv, "pip", "sync", "--python", python, "--require-hashes", requirements])
        run([uv, "pip", "install", "--python", python, "--no-deps", first / wheel_name])
        # Isolated Python ignores PYTHONPATH and never imports the checkout.
        probe = work / "installed_package_probe.py"
        shutil.copyfile(ROOT / "scripts/installed_package_probe.py", probe)
        probe_result = run(
            [python, "-I", probe, version, digest(first / wheel_name)],
            cwd=work,
            capture=True,
            env={
                key: os.environ[key]
                for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
                if key in os.environ
            },
        )
        install_evidence = json.loads(probe_result.stdout)
        for name in (wheel_name, sdist_name):
            shutil.copyfile(first / name, output / name)
        return {
            **metadata,
            **inspection,
            "python_byte_reproducible": True,
            "sdist_rebuild_matches": True,
            "installed_probe": install_evidence,
        }


def operator_artifact(messages, manifest):
    executables = set()
    finished = False
    for line in messages.splitlines():
        message = json.loads(line)
        if message.get("reason") == "build-finished":
            finished = message.get("success") is True
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "rkat-computer"
            and message.get("target", {}).get("kind") == ["bin"]
            and message.get("manifest_path")
            and Path(message["manifest_path"]).resolve() == manifest.resolve()
            and message.get("executable")
        ):
            executables.add(Path(message["executable"]))
    if not finished or len(executables) != 1:
        raise ValueError("Cargo did not report one successful operator executable")
    binary = executables.pop()
    if not binary.is_file():
        raise ValueError("Cargo's operator executable is missing")
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "bundle"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv", default="uv")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a development bundle that is not eligible for distribution",
    )
    args = parser.parse_args()
    revision = source_revision(ROOT)
    validate_release_tree(
        args.command, modified=revision["working_tree_modified"], allow_dirty=args.allow_dirty
    )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Output must be empty; select a new candidate directory")
    before = source_hashes(ROOT)
    metadata = check_python(output, args.uv)
    metadata.update(
        {
            "schema_version": 1,
            "kind": "private_package_check",
            **revision,
            "source_sha256": before,
            "python": sys.version.split()[0],
            "uv": run([args.uv, "--version"], capture=True).stdout.strip(),
            "qualification": "Installed package and local deterministic broker; no native Console, VM or HomeCore claim",
        }
    )
    artifacts = sorted(output.iterdir())
    if args.command == "bundle":
        rustc = run(["rustc", "-vV"], capture=True).stdout
        target = next(
            line.removeprefix("host: ") for line in rustc.splitlines() if line.startswith("host: ")
        )
        # This release lane qualifies a native executable. An explicit target
        # prevents ambient Cargo configuration from changing that contract.
        built = run(
            [
                ROOT / "scripts/repo-cargo",
                "build",
                "--locked",
                "--release",
                "--bin",
                "rkat-computer",
                "--target",
                target,
                "--message-format=json-render-diagnostics",
            ],
            capture_stdout=True,
        )
        binary = operator_artifact(built.stdout, ROOT / "Cargo.toml")
        version_output = run([binary, "--version"], capture=True).stdout.strip()
        if version_output != f"rkat-computer {metadata['version']}":
            raise ValueError("Built operator version mismatch")
        run([binary, "--help"], capture=True)
        prefix = f"meerkat-computer-{metadata['version']}"
        source = output / f"{prefix}-source.tar.gz"
        epoch = int(run(["git", "show", "-s", "--format=%ct", "HEAD"], capture=True).stdout.strip())
        write_source_archive(ROOT, source, prefix, epoch)
        copied = output / f"rkat-computer-{metadata['version']}-{target}"
        shutil.copyfile(binary, copied)
        copied.chmod(0o755)
        artifacts.extend([source, copied])
        metadata.update(
            {
                "kind": "private_release_candidate",
                "target": target,
                "rustc": rustc.strip(),
                "operator_version": version_output,
                "source_archive_epoch": epoch,
            }
        )
    if before != source_hashes(ROOT) or revision != source_revision(ROOT):
        raise ValueError("Source changed during validation; discard this candidate and retry")
    write_manifest(output, metadata, artifacts)
    print(f"Verified {args.command} artifacts: {output}")


if __name__ == "__main__":
    main()
