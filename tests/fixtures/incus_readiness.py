#!/usr/bin/env python3
"""Strict local Incus subprocess fixture, never a real guest or cloud operation."""

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

root = Path(__file__).parent
settings = json.loads((root / "fixture.json").read_text())
args = sys.argv[1:]
assert args.pop(0) == "--force-local"
if args[:1] == ["--project"]:
    assert args[1] == "computers"
    args = args[2:]
operation = args.pop(0)


def inventory():
    with (root / "inventory.json").open() as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return json.load(stream)


def count_call(label):
    path = root / f"{label}-{name}"
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count


def critical(callback):
    # This measures overlap of actual admission-phase CLI processes.
    marker = root / "critical"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        (root / "admission-overlapped").touch()
        sys.exit(31)
    try:
        time.sleep(0.05)
        callback()
    finally:
        os.close(descriptor)
        marker.unlink()


if operation == "list":
    assert args in (["--format=json"], ["--all-projects", "--format=json"])
    print(json.dumps(inventory()))
elif operation == "query":
    assert len(args) == 1
    url = urlsplit(args[0])
    if url.path.startswith("/1.0/networks/"):
        assert parse_qs(url.query) == {"project": ["default"]}
        critical(lambda: print(json.dumps(settings["network"])))
    elif url.path.startswith("/1.0/network-acls/"):
        assert parse_qs(url.query) == {"project": ["default"]}
        critical(lambda: print(json.dumps(settings["acl"])))
    else:
        assert url.path.startswith("/1.0/instances/") and url.path.endswith("/state")
        assert parse_qs(url.query) == {"project": ["computers"]}
        name = url.path.split("/")[3]
        ip = settings["addresses"][name]
        print(json.dumps({"network": {"eth0": {"addresses": [{"family": "inet", "address": ip}]}}}))
elif operation == "exec":
    name = args.pop(0)
    assert name in settings["addresses"] and args.pop(0) == "--"
    if args == ["true"]:
        (root / f"agent-ready-{name}").touch()
        if settings["bootstrap_barrier"] and len(list(root.glob("agent-ready-*"))) < 2:
            sys.exit(1)
    elif args == ["systemctl", "show", "--property=Version", "--value"]:
        attempt = count_call("manager-probes")
        if settings.get("manager_unavailable") or attempt <= settings.get("manager_failures", 0):
            sys.exit(1)
        if attempt <= settings.get("manager_failures", 0) + settings.get("manager_empty", 0):
            sys.exit(0)
        (root / f"manager-ready-{name}").touch()
        print("255.4")
    elif args[:2] == ["python3", "-c"]:
        if settings.get("require_manager") and not (root / f"manager-ready-{name}").exists():
            (root / "bootstrap-before-manager").touch()
        payload = json.load(sys.stdin)
        assert len(payload["token"]) >= 32
        assert payload["guest_admin"] is False
        (root / f"bootstrapped-{name}").touch()
    else:
        assert args == ["systemctl", "start", "meerkat-computer-worker.service"]
        count_call("service-starts")
        if settings.get("require_manager") and not (root / f"manager-ready-{name}").exists():
            (root / "service-start-before-manager").touch()
        if settings.get("service_start_fails"):
            sys.exit(1)
elif operation == "start":
    assert len(args) == 1

    def start():
        with (root / "inventory.json").open("r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            instances = json.load(stream)
            instance = next(item for item in instances if item["name"] == args[0])
            assert instance["status"] == "Stopped"
            instance["status"] = "Running"
            stream.seek(0)
            json.dump(instances, stream)
            stream.truncate()

    critical(start)
else:
    raise AssertionError("unexpected fixture operation")
