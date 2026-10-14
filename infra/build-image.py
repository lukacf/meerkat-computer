#!/usr/bin/env python3
"""Build a private, credential-free worker image and print its immutable fingerprint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER_OWNER = "user.meerkat-computer.image-build"
PROTOCOL_KEY = "user.meerkat-computer.worker-protocol"
DESKTOP_KEY = "user.meerkat-computer.desktop"
APPS_KEY = "user.meerkat-computer.apps"


def source_files(root: Path) -> list[Path]:
    files = [
        root / "pyproject.toml",
        root / "uv.lock",
        root / "README.md",
        root / "LICENSE",
        root / "python/meerkat_computer/resources/preview-v1.html",
        root / "infra/provision-worker.sh",
        root / "infra/chromium-profile.py",
        root / "infra/desktop-session.py",
        root / "infra/image-check.py",
    ]
    files += sorted((root / "python/meerkat_computer").rglob("*.py"))
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError("worker source, lockfile and provisioning script must be regular files")
    return files


def source_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in source_files(root):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def archive_sources(root: Path, target: Path) -> None:
    def guest_owner(member: tarfile.TarInfo) -> tarfile.TarInfo:
        # Host OS Login IDs can exceed an unprivileged guest's UID/GID mapping.
        member.uid = member.gid = 0
        member.uname = member.gname = "root"
        return member

    with tarfile.open(target, "w:gz") as archive:
        for path in source_files(root):
            archive.add(
                path,
                arcname=str(path.relative_to(root)),
                recursive=False,
                filter=guest_owner,
            )


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def incus(state: dict, *args: str, input: str | None = None, timeout: int = 1200) -> str:
    return subprocess.run(
        ["incus", "--force-local", "--project", state["project"], *args],
        input=input,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
    ).stdout.strip()


def instance(state: dict) -> dict | None:
    items = json.loads(incus(state, "list", "--format=json", timeout=30))
    return next((item for item in items if item["name"] == state["name"]), None)


def verify_owner(state: dict, item: dict) -> None:
    config = item.get("config", {})
    if (
        item.get("name") != state["name"]
        or item.get("type") != state["kind"]
        or config.get(BUILDER_OWNER) != state["ownership_id"]
        or not state.get("create_intent")
        or not config.get("volatile.uuid")
    ):
        raise ValueError("image builder name, kind, creation intent and ownership must match")
    if state.get("instance_id") and config["volatile.uuid"] != state["instance_id"]:
        raise ValueError("image builder incarnation changed")


def published_image(state: dict, images: list[dict]) -> dict | None:
    matches = [
        item
        for item in images
        if item.get("properties", {}).get(BUILDER_OWNER) == state["ownership_id"]
    ]
    if not matches:
        return None
    if len(matches) != 1 or not state.get("publish_intent"):
        raise ValueError("published image does not match a unique saved publication intent")
    image = matches[0]
    properties = image.get("properties", {})
    if (
        not re.fullmatch(r"[a-f0-9]{64}", image.get("fingerprint", ""))
        or image.get("public") is not False
        or image.get("type") != state["kind"]
        or properties.get(PROTOCOL_KEY) != "1"
        or properties.get("user.meerkat-computer.source-sha256") != state["source_hash"]
        or properties.get(DESKTOP_KEY, "0") != ("1" if state.get("desktop") else "0")
        or properties.get(APPS_KEY, "none") != state.get("apps", "none")
        or state.get("fingerprint", image["fingerprint"]) != image["fingerprint"]
    ):
        raise ValueError("published image identity, protocol or visibility differs")
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="meerkat-computer")
    parser.add_argument("--name", default="mc-image-v1")
    parser.add_argument("--base-image", default="images:ubuntu/24.04/cloud")
    parser.add_argument("--kind", choices=["container", "virtual-machine"], default="container")
    parser.add_argument("--desktop", action="store_true", help="include X11 and AT-SPI desktop")
    parser.add_argument(
        "--apps",
        choices=["minimal", "full"],
        default="full",
        help="desktop app bundle (default: full)",
    )
    parser.add_argument("--state", type=Path, default=Path(".state/image-build.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", args.name):
        raise ValueError("use a dedicated lowercase builder name")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", args.project) or args.base_image.startswith("-"):
        raise ValueError("invalid project or base image")
    request = {
        "name": args.name,
        "project": args.project,
        "base_image": args.base_image,
        "kind": args.kind,
        "desktop": args.desktop,
        "apps": args.apps if args.desktop else "none",
        "source_hash": source_hash(ROOT),
    }
    if args.state.exists():
        state = json.loads(args.state.read_text())
        if any(state.get(key) != value for key, value in request.items()):
            raise ValueError(
                "image source or build parameters changed; use a new builder and state"
            )
    else:
        state = dict(request, ownership_id=uuid.uuid4().hex, schema_version=1)
    create = [
        "init",
        state["base_image"],
        state["name"],
        "--profile",
        "default",
        "-c",
        BUILDER_OWNER + "=" + state["ownership_id"],
        "-c",
        "limits.cpu=2",
        "-c",
        "limits.memory=2048MiB",
        "-d",
        "root,size=16GiB",
    ]
    create += (
        ["--vm"]
        if state["kind"] == "virtual-machine"
        else [
            "-c",
            "security.privileged=false",
            "-c",
            "security.idmap.isolated=true",
        ]
    )
    print(
        shlex.join(["incus", "--force-local", "--project", state["project"], *create]), flush=True
    )
    print(
        "Then: copy allowlisted worker source, install locked dependencies and Chromium, scrub guest identity, stop and publish privately.",
        flush=True,
    )
    print(
        "Image variant: "
        + ("desktop with " + state["apps"] + " apps" if state["desktop"] else "headless browser"),
        flush=True,
    )
    if not args.apply:
        return
    if state.get("publish_intent") or state.get("fingerprint"):
        published = published_image(
            state, json.loads(incus(state, "image", "list", "--format=json"))
        )
        if published is None and state.get("fingerprint"):
            raise ValueError("saved published image disappeared")
        if published is None:
            raise ValueError(
                "saved publication is unconfirmed; inspect the original Incus operation "
                "and retry observation after it completes, without replaying guest changes"
            )
        if published:
            state["fingerprint"] = published["fingerprint"]
            save(args.state, state)
    if state.get("fingerprint"):
        print(
            json.dumps(
                {
                    "image": state["fingerprint"],
                    "kind": state["kind"],
                    "desktop": state["desktop"],
                    "apps": state["apps"],
                    "source_hash": state["source_hash"],
                }
            )
        )
        return
    current = instance(state)
    if current is None:
        if state.get("instance_id"):
            raise ValueError("saved builder disappeared; choose a new builder")
        state["create_intent"] = True
        save(args.state, state)
        incus(state, *create)
        current = instance(state)
        if current is None:
            raise ValueError("created builder is absent")
    verify_owner(state, current)
    state["instance_id"] = current["config"]["volatile.uuid"]
    state["base_fingerprint"] = current["config"].get("volatile.base_image")
    save(args.state, state)
    if current["status"] == "Stopped":
        incus(state, "start", state["name"])
    elif current["status"] != "Running":
        raise ValueError("builder is neither stopped nor running")
    deadline = time.monotonic() + 180
    while True:
        try:
            incus(state, "exec", state["name"], "--", "true", timeout=10)
            break
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise ValueError("builder guest agent did not become ready") from None
            time.sleep(1)
    verify_owner(state, instance(state))
    with tempfile.TemporaryDirectory(prefix="meerkat-worker-image-") as temporary:
        payload = Path(temporary) / "worker-source.tgz"
        archive_sources(ROOT, payload)
        incus(state, "file", "push", str(payload), state["name"] + "/root/worker-source.tgz")
    incus(
        state,
        "exec",
        state["name"],
        "--",
        "bash",
        "-s",
        "--",
        *(["--desktop", "--apps", state["apps"]] if state["desktop"] else []),
        input=(ROOT / "infra/provision-worker.sh").read_text(),
    )
    verify_owner(state, instance(state))
    incus(state, "stop", state["name"], "--timeout", "120", timeout=150)
    current = instance(state)
    verify_owner(state, current)
    if current["status"] != "Stopped":
        raise ValueError("image builder did not stop")
    # No --reuse or --public. Runtime consumers receive a fingerprint, never an alias.
    state["publish_intent"] = True
    save(args.state, state)
    incus(
        state,
        "publish",
        state["name"],
        BUILDER_OWNER + "=" + state["ownership_id"],
        PROTOCOL_KEY + "=1",
        DESKTOP_KEY + ("=1" if state["desktop"] else "=0"),
        APPS_KEY + "=" + state["apps"],
        "user.meerkat-computer.source-sha256=" + state["source_hash"],
    )
    published = published_image(state, json.loads(incus(state, "image", "list", "--format=json")))
    if published is None:
        raise ValueError("published image is absent")
    state["fingerprint"] = published["fingerprint"]
    save(args.state, state)
    print(
        json.dumps(
            {
                "image": state["fingerprint"],
                "kind": state["kind"],
                "desktop": state["desktop"],
                "apps": state["apps"],
                "source_hash": state["source_hash"],
            }
        )
    )


if __name__ == "__main__":
    main()
