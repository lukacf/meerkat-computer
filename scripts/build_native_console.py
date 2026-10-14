#!/usr/bin/env python3
"""Explicitly build both native fixtures serially and issue a provenance receipt."""

import argparse
import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from native_console_inputs import clean_env, computer_sources, digest, native_sources
from prepare_native_console import CORE_REV, CORE_VERSION, META_RELEASE, META_REV, ROOT


def build(source, command, binary_name, env, output, name):
    wrapper = source / "scripts/repo-cargo"
    cargo_env = dict(
        line.split("=", 1)
        for line in subprocess.check_output(
            [str(wrapper), "--print-env"], cwd=source, env=env, text=True
        ).splitlines()
    )
    started = time.monotonic()
    peak_kib = 0
    with (output / f"{name}.log").open("w") as log:
        process = subprocess.Popen(
            [str(wrapper), *command],
            cwd=source,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                rows = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
                rows = [tuple(map(int, row.split())) for row in rows.splitlines()]
                owned = {process.pid}
                while True:
                    descendants = owned | {pid for pid, parent, _ in rows if parent in owned}
                    if descendants == owned:
                        break
                    owned = descendants
                rss = sum(rss for pid, _, rss in rows if pid in owned)
                peak_kib = max(peak_kib, rss)
                if rss > 8 * 1024 * 1024:
                    raise RuntimeError("Build exceeded its 8 GiB RSS bound")
                if time.monotonic() - started > 1200:
                    raise RuntimeError("Build exceeded its 20 minute bound")
                time.sleep(1)
            if process.returncode:
                raise RuntimeError(f"{name} build failed; inspect {log.name}")
        finally:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    binary = Path(cargo_env["CARGO_TARGET_DIR"]) / "debug/examples" / binary_name
    return {
        "binary": str(binary),
        "binary_sha256": digest(binary),
        "command": command,
        "rustc": subprocess.check_output(["rustc", "-vV"], cwd=source, env=env, text=True).strip(),
        "elapsed_seconds": time.monotonic() - started,
        "peak_rss_kib": peak_kib,
        "cargo_home": cargo_env["CARGO_HOME"],
        "cargo_target_dir": cargo_env["CARGO_TARGET_DIR"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--meta-source", type=Path, default=ROOT / ".state/native-console-inputs/meta"
    )
    parser.add_argument("--meta-cargo-home", type=Path)
    parser.add_argument("--meta-target", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output, source = args.output.resolve(), args.meta_source.resolve()
    if output.exists():
        parser.error("Output already exists; choose a fresh receipt directory")
    before = {"broker": computer_sources(), "consumer": native_sources(source)}
    output.mkdir(parents=True)
    env = {
        **clean_env(),
        "CARGO_BUILD_JOBS": "2",
        "RUST_LANE_ID": os.environ.get("RUST_LANE_ID", "native-console"),
    }
    broker = build(
        ROOT,
        ["build", "--locked", "--example", "native_console_broker"],
        "native_console_broker",
        env,
        output,
        "broker",
    )
    consumer_env = dict(env)
    for key, value in (
        ("CARGO_HOME", args.meta_cargo_home),
        ("CARGO_TARGET_DIR", args.meta_target),
    ):
        if value:
            consumer_env[key] = str(value.resolve())
    consumer = build(
        source,
        ["build", "--locked", "-p", "meerkat-meta", "--example", "computer_native_acceptance"],
        "computer_native_acceptance",
        consumer_env,
        output,
        "consumer",
    )
    after = {"broker": computer_sources(), "consumer": native_sources(source)}
    if before != after:
        raise RuntimeError("Sources changed during builds; no acceptance receipt issued")
    receipt = {
        "schema_version": 1,
        "meta_release": META_RELEASE,
        "meta_revision": META_REV,
        "core_revision": CORE_REV,
        "core_version": CORE_VERSION,
        "broker": {**broker, "sources": before["broker"]},
        "consumer": {**consumer, "sources": before["consumer"]},
    }
    (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Built native acceptance fixtures: {output / 'build.json'}")


if __name__ == "__main__":
    main()
