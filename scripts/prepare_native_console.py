#!/usr/bin/env python3
"""Stage exact native Console sources without changing a neighboring checkout.

This prepares test inputs only. Rust compilation is a separate explicit step.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

# These are released sources, not the historical unreleased MCP Apps candidate.
# Update all release pins together only after a compatible stable Meta release.
META_RELEASE = "v0.9.0"
META_REV = "b275e0ed4fe61cf4ff43d16691c54efb17812919"
CORE_REV = "a00bafaf1d862fb41112ff1f30312b350ad05246"
CORE_VERSION = "0.9.0"
REQUIRED_CORE_VERSION = "0.9.0"
ROOT = Path(__file__).resolve().parents[1]


def require_compatible_release():
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", META_RELEASE):
        raise ValueError("Native qualification requires an exact stable release tag")
    if CORE_VERSION != REQUIRED_CORE_VERSION:
        raise ValueError(
            f"No compatible released Meta consumer: {META_RELEASE} uses Meerkat "
            f"{CORE_VERSION}; this candidate requires {REQUIRED_CORE_VERSION}. "
            "Wait for a compatible stable Meta release with native MCP Apps; "
            "unreleased source and historical receipts do not qualify this candidate."
        )


def validate_release_source(source):
    workspace = tomllib.loads((source / "Cargo.toml").read_text())
    if workspace.get("workspace", {}).get("package", {}).get("version") != META_RELEASE[1:]:
        raise ValueError("Native source has a different release version")
    manifest = tomllib.loads((source / "crates/meerkat-meta/Cargo.toml").read_text())
    dependencies = {
        name: dependency for name, dependency in manifest.get("dependencies", {}).items()
        if name == "meerkat" or name.startswith("meerkat-")
    }
    if "meerkat" not in dependencies or any(
        (dependency if isinstance(dependency, str) else dependency.get("version"))
        != "=" + CORE_VERSION
        or (isinstance(dependency, dict) and {"git", "path"}.intersection(dependency))
        for dependency in dependencies.values()
    ):
        raise ValueError("Native source must use the exact released Meerkat crate versions")
    for name in ("console/mcp-apps-sandbox.cjs", "crates/meerkat-meta/src/http_console/mcp_apps.rs"):
        if not (source / name).is_file():
            raise ValueError("Released Meta source does not include the native MCP Apps host")


def run(command, cwd, env):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="https://github.com/lukacf/meerkat-meta.git")
    parser.add_argument("--output", type=Path, default=ROOT / ".state/native-console-inputs/meta")
    args = parser.parse_args()
    require_compatible_release()
    output = args.output.resolve()
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    if output.exists():
        parser.error("Output already exists; choose a fresh path to preserve prior evidence")
    output.mkdir(parents=True)
    run(["git", "init", "--quiet"], output, env)
    run(["git", "fetch", "--quiet", "--depth=1", args.source, f"refs/tags/{META_RELEASE}"], output, env)
    run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], output, env)
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=output, env=env, text=True
    ).strip()
    if actual != META_REV:
        raise RuntimeError("Native Console source revision did not match")
    validate_release_source(output)
    source = ROOT / "tests/native-console/consumer.rs"
    destination = output / "crates/meerkat-meta/examples/computer_native_acceptance.rs"
    shutil.copyfile(source, destination)
    run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], output / "console", env)
    run(["node", "node_modules/playwright/cli.js", "install", "chromium"], output / "console", env)
    receipt = {
        "meta_release": META_RELEASE,
        "meta_revision": actual,
        "core_revision": CORE_REV,
        "core_version": CORE_VERSION,
        "consumer_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source": str(output),
        "rust_build_started": False,
        "build_command": "./scripts/repo-cargo build --locked -p meerkat-meta --example computer_native_acceptance",
    }
    (output.parent / "inputs.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
