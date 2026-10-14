#!/usr/bin/env python3
"""Qualify an installed wheel through a prebuilt, pinned native Console consumer.

No Rust build runs implicitly. The local worker is not a VM or security sandbox.
"""

import argparse
import json
import subprocess
from pathlib import Path

from native_console_inputs import (
    clean_env,
    computer_sources,
    digest,
    freeze_binary,
    native_sources,
    private_runtime_state,
    run_controller,
    verify_build,
)
from package_artifacts import inspect_wheel
from prepare_native_console import (
    CORE_REV,
    CORE_VERSION,
    META_RELEASE,
    META_REV,
    ROOT,
    require_compatible_release,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--broker-binary", required=True, type=Path)
    parser.add_argument("--consumer-binary", required=True, type=Path)
    parser.add_argument("--build-receipt", required=True, type=Path)
    parser.add_argument(
        "--meta-source", type=Path, default=ROOT / ".state/native-console-inputs/meta"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=int, default=8, choices=range(8, 121))
    args = parser.parse_args()
    require_compatible_release()
    output = args.output.resolve()
    wheel = args.wheel.resolve()
    source = args.meta_source.resolve()
    env = clean_env()
    if output.exists():
        parser.error("Output already exists; choose a fresh evidence path")
    for item in (wheel, args.broker_binary, args.consumer_binary):
        if not item.is_file():
            parser.error("Supply existing wheel and prebuilt binary paths")
    builds = json.loads(args.build_receipt.read_text())
    if (
        builds.get("meta_release") != META_RELEASE
        or builds.get("meta_revision") != META_REV
        or builds.get("core_revision") != CORE_REV
        or builds.get("core_version") != CORE_VERSION
    ):
        parser.error("Build receipt has different native source pins")
    verify_build(builds["broker"], args.broker_binary, computer_sources())
    verify_build(builds["consumer"], args.consumer_binary, native_sources(source))
    inspect_wheel(wheel, ROOT)
    consumer_source = source / "crates/meerkat-meta/examples/computer_native_acceptance.rs"
    if digest(consumer_source) != digest(ROOT / "tests/native-console/consumer.rs"):
        parser.error("Staged consumer source differs; copy and rebuild it before acceptance")
    output.mkdir(parents=True)
    broker_binary = freeze_binary(builds["broker"], args.broker_binary, output / "broker")
    consumer_binary = freeze_binary(builds["consumer"], args.consumer_binary, output / "consumer")
    fingerprints = {
        path: digest(path)
        for path in (
            ROOT / "uv.lock",
            ROOT / "pyproject.toml",
            wheel,
            ROOT / "tests/native-console/browser.cjs",
            ROOT / "scripts/test_native_console.py",
            ROOT / "scripts/native_console_inputs.py",
        )
    }

    def verify_inputs():
        verify_build(builds["broker"], broker_binary, computer_sources())
        verify_build(builds["consumer"], consumer_binary, native_sources(source))
        if any(digest(path) != value for path, value in fingerprints.items()):
            raise ValueError("Acceptance inputs changed during the run")

    python = output / "venv/bin/python"
    subprocess.run(["uv", "venv", str(output / "venv")], env=env, check=True)
    requirements = output / "runtime-requirements.txt"
    requirements.write_text(
        subprocess.check_output(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            cwd=ROOT,
            env=env,
            text=True,
        )
    )
    subprocess.run(
        ["uv", "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)],
        env=env,
        check=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        env=env,
        check=True,
    )
    probe = subprocess.check_output(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.metadata as m,json;import meerkat_computer;"
                "print(json.dumps({'path':meerkat_computer.__file__,"
                "'version':m.version('meerkat-computer'),'mcp_version':m.version('mcp'),"
                "'dependencies':sorted((d.metadata['Name'],d.version) for d in m.distributions())}))"
            ),
        ],
        cwd=output,
        env=env,
        text=True,
    )
    package = json.loads(probe)
    if not Path(package["path"]).is_relative_to(output / "venv"):
        raise RuntimeError("Acceptance must import the installed wheel only")
    package["wheel_sha256"] = digest(wheel)
    package["runtime_requirements_sha256"] = digest(requirements)
    package["uv_lock_sha256"] = digest(ROOT / "uv.lock")
    (output / "build.json").write_text(json.dumps(builds, indent=2) + "\n")
    with private_runtime_state(output) as state:
        inputs = {
            "output": str(output),
            "runtimeState": state,
            "python": str(python),
            "brokerBinary": str(broker_binary),
            "consumerBinary": str(consumer_binary),
            "metaSource": str(source),
            "metaRelease": META_RELEASE,
            "metaRevision": META_REV,
            "coreRevision": CORE_REV,
            "coreVersion": CORE_VERSION,
            "package": package,
            "pollMs": args.poll_seconds * 1000,
        }
        (output / "inputs.json").write_text(
            json.dumps(
                {
                    **inputs,
                    "brokerBinarySha256": digest(broker_binary),
                    "consumerBinarySha256": digest(consumer_binary),
                    "consumerSourceSha256": digest(consumer_source),
                    "inputFingerprints": {str(path): value for path, value in fingerprints.items()},
                },
                indent=2,
            )
            + "\n"
        )
        verify_inputs()
        run_controller(
            ["node", str(ROOT / "tests/native-console/browser.cjs"), json.dumps(inputs)],
            output,
            env,
            verify_inputs,
            timeout=args.poll_seconds + 240,
        )


if __name__ == "__main__":
    main()
