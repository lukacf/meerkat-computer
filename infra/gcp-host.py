#!/usr/bin/env python3
"""Plan or explicitly apply a private GCP host. No active gcloud config is changed."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
import time
import tomllib
import uuid
from pathlib import Path


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
        # Persist the replacement directory entry before the caller can send
        # a cloud request. A failed sync must leave that effect unsubmitted.
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_state(config: dict, name: str) -> dict:
    cloud = dict(config["cloud"])
    required = ("project", "account", "zone", "machine_type", "network", "subnet")
    for key in required:
        value = cloud.get(key)
        if not isinstance(value, str) or not value or value.startswith("-"):
            raise ValueError(f"cloud.{key} must be explicit")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", name):
        raise ValueError("host name must be a lowercase GCP name of at most 49 characters")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+-[a-z]", cloud["zone"]):
        raise ValueError("cloud.zone must be an explicit zone")
    for key, fallback in (("disk_gib", 100), ("max_run_seconds", 14400)):
        value = cloud.get(key, fallback)
        if type(value) is not int or value <= 0:
            raise ValueError(f"cloud.{key} must be a positive integer")
        cloud[key] = value
    if type(cloud.get("nested_virtualization", False)) is not bool:
        raise ValueError("cloud.nested_virtualization must be true or false")
    return {
        "schema_version": 1,
        "name": name,
        "ownership_id": uuid.uuid4().hex,
        "cloud": cloud,
        "resources": {},
    }


def gcloud(state: dict, *args: str) -> list[str]:
    cloud = state["cloud"]
    return [
        "gcloud",
        "--project",
        cloud["project"],
        "--account",
        cloud["account"],
        "--quiet",
        *args,
    ]


def specs(state: dict) -> list[dict]:
    cloud, name, owner = state["cloud"], state["name"], state["ownership_id"]
    instance_args = [
        "--zone",
        cloud["zone"],
        "--machine-type",
        cloud["machine_type"],
        "--image-family=ubuntu-2404-lts-amd64",
        "--image-project=ubuntu-os-cloud",
        "--boot-disk-size",
        f"{cloud['disk_gib']}GB",
        "--boot-disk-type=pd-balanced",
        "--boot-disk-auto-delete",
        "--network",
        cloud["network"],
        "--subnet",
        cloud["subnet"],
        "--no-address",
        "--tags",
        name,
        "--labels",
        f"app=meerkat-computer,computer-owner={owner}",
        "--no-service-account",
        "--no-scopes",
        "--no-restart-on-failure",
        "--max-run-duration",
        f"{cloud['max_run_seconds']}s",
        "--instance-termination-action=STOP",
        "--metadata=block-project-ssh-keys=TRUE",
    ]
    if cloud.get("nested_virtualization", False):
        instance_args.extend(["--enable-nested-virtualization", "--maintenance-policy=TERMINATE"])
    return [
        {
            "key": "firewall",
            "group": "firewall-rules",
            "name": name + "-iap-ssh",
            "location": [],
            "create": [
                "--network",
                cloud["network"],
                "--allow=tcp:22",
                "--source-ranges=35.235.240.0/20",
                "--target-tags",
                name,
                "--description",
                "meerkat-computer:" + owner,
            ],
        },
        {
            "key": "instance",
            "group": "instances",
            "name": name,
            "location": ["--zone", cloud["zone"]],
            "create": instance_args,
        },
    ]


def command(state: dict, spec: dict, operation: str) -> list[str]:
    args = ["compute", spec["group"], operation, spec["name"]]
    args += spec["create"] if operation == "create" else spec["location"]
    if operation != "delete":
        args.append("--format=json")
    return gcloud(state, *args)


def verify_owner(state: dict, spec: dict, resource: dict) -> None:
    expected = state["resources"].get(spec["key"], {})
    owned = (
        resource.get("labels", {}).get("computer-owner") == state["ownership_id"]
        if spec["key"] == "instance"
        else resource.get("description") == "meerkat-computer:" + state["ownership_id"]
    )
    if resource.get("name") != spec["name"] or not resource.get("id") or not owned:
        raise ValueError("resource name and ownership marker must match the saved creation intent")
    if expected.get("id") and str(expected["id"]) != str(resource["id"]):
        raise ValueError("resource ID changed; refusing to adopt its replacement")
    if not expected:
        raise ValueError("no saved creation intent exists for this resource")


def run(args: list[str], *, capture: bool = True, timeout: float = 600) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout)
    return result.stdout if capture else ""


def unpack(raw: str) -> dict:
    value = json.loads(raw)
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("expected exactly one GCP resource")
        value = value[0]
    if not isinstance(value, dict):
        raise TypeError("GCP response is not an object")
    return value


def describe(state: dict, spec: dict, *, timeout: float = 600) -> dict | None:
    try:
        return unpack(run(command(state, spec, "describe"), timeout=timeout))
    except subprocess.CalledProcessError as error:
        cloud = state["cloud"]
        resource = (
            f"zones/{cloud['zone']}/instances/{spec['name']}"
            if spec["key"] == "instance"
            else f"global/firewalls/{spec['name']}"
        )
        missing = f"The resource 'projects/{cloud['project']}/{resource}' was not found"
        if missing in (error.stderr or ""):
            return None
        raise


def remember(state: dict, spec: dict, data: dict) -> None:
    verify_owner(state, spec, data)
    state["resources"][spec["key"]] = {
        "name": data["name"],
        "id": str(data["id"]),
        "status": data.get("status"),
    }


def reconcile_power(
    state: dict,
    path: Path,
    action: str,
    *,
    apply: bool,
    wait_seconds: float,
    poll_seconds: float,
) -> dict:
    """Reach an observed power state without replaying an uncertain request.

    The gateway serializes calls for this creation intent. A request is saved
    before submission; subsequent invocations reconcile that exact request by
    reading GCP, even when the original CLI was interrupted or timed out.
    """
    if not all(math.isfinite(value) and value > 0 for value in (wait_seconds, poll_seconds)):
        raise ValueError("power wait and poll seconds must be finite positive numbers")
    for key in ("project", "account", "zone"):
        value = state["cloud"].get(key)
        if not isinstance(value, str) or not value or value.startswith("-"):
            raise ValueError(f"saved cloud.{key} must be explicit")
    spec = specs(state)[-1]
    expected = state["resources"].get("instance", {})
    instance_id = expected.get("id")
    if (
        isinstance(instance_id, bool)
        or not isinstance(instance_id, (str, int))
        or not str(instance_id)
    ):
        raise ValueError("a saved immutable instance ID is required for power management")
    instance_id = str(instance_id)
    targets = {"start": "RUNNING", "resume": "RUNNING", "suspend": "SUSPENDED"}
    desired = "RUNNING" if action == "ensure-running" else "SUSPENDED"
    pending = expected.get("pending_power")
    if pending is not None and (
        not isinstance(pending, dict)
        or pending.get("instance_id") != instance_id
        or pending.get("operation") not in targets
        or pending.get("target") != targets.get(pending.get("operation"))
    ):
        raise ValueError("saved pending power request does not match this instance")
    if not apply:
        operations = ["start", "resume"] if action == "ensure-running" else ["suspend"]
        return {
            "action": action,
            "name": spec["name"],
            "id": instance_id,
            "target": desired,
            "apply": False,
            "commands": [command(state, spec, operation) + ["--async"] for operation in operations],
        }

    deadline = time.monotonic() + wait_seconds
    operation = None
    reconciled = False
    transitions = {"PROVISIONING", "STAGING", "STOPPING", "SUSPENDING", "REPAIRING"}

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            detail = (
                "power effect unconfirmed" if pending is not None else "host transition unresolved"
            )
            raise TimeoutError(f"{detail}; no power request was repeated")
        return budget

    def pause() -> None:
        time.sleep(min(poll_seconds, remaining()))

    while True:
        actual = describe(state, spec, timeout=remaining())
        if actual is None:
            raise ValueError("saved host is absent")
        verify_owner(state, spec, actual)
        status = actual.get("status")
        if status not in transitions | {"RUNNING", "TERMINATED", "SUSPENDED"}:
            raise ValueError(f"unsupported GCP instance status: {status}")
        if pending is not None:
            if status != pending["target"]:
                pause()
                continue
            expected["last_operation"] = pending["operation"]
            expected["status"] = status
            expected.pop("pending_power")
            save(path, state)
            pending = None
            reconciled = True
        if status == desired:
            return {
                "action": action,
                "name": spec["name"],
                "id": instance_id,
                "status": status,
                "operation": operation,
                "reconciled": reconciled,
            }
        if status in transitions:
            pause()
            continue
        if action == "ensure-running":
            operation = {"TERMINATED": "start", "SUSPENDED": "resume"}[status]
        elif status == "RUNNING":
            operation = "suspend"
        else:
            raise ValueError(f"cannot suspend a host in {status}")
        # Persist before crossing the side-effect boundary. A CLI failure does
        # not prove the API refused the request, so it never clears this fence.
        remaining()
        pending = {"operation": operation, "target": targets[operation], "instance_id": instance_id}
        expected["pending_power"] = pending
        save(path, state)
        budget = remaining()
        try:
            run(command(state, spec, operation) + ["--async"], timeout=budget)
        except (subprocess.SubprocessError, OSError):
            raise RuntimeError(
                f"power effect unconfirmed for {operation}; reconcile saved intent"
            ) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "plan",
            "create",
            "status",
            "start",
            "stop",
            "ensure-running",
            "suspend",
            "delete",
            "tunnel",
        ],
    )
    parser.add_argument("--config", type=Path, default=Path("infra/gcp.example.toml"))
    parser.add_argument("--state", type=Path, default=Path(".state/gcp-host.json"))
    parser.add_argument("--name")
    parser.add_argument("--local-port", type=int, default=18760)
    parser.add_argument("--broker-port", type=int, default=18760)
    parser.add_argument("--wait-seconds", type=float, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action in ("plan", "create") and not args.state.exists():
        if not args.name:
            parser.error("--name is required for a new host")
        state = new_state(tomllib.loads(args.config.read_text()), args.name)
    else:
        state = json.loads(args.state.read_text())
        if state.get("schema_version") != 1:
            raise ValueError("unsupported saved host schema")
        if args.name and args.name != state["name"]:
            raise ValueError("requested name differs from saved host")
    resources = specs(state)
    if args.action in ("ensure-running", "suspend"):
        result = reconcile_power(
            state,
            args.state,
            args.action,
            apply=args.apply,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
        print(json.dumps(result, indent=2))
        return
    if args.action in ("plan", "create"):
        for spec in resources:
            proposed = command(state, spec, "create")
            print(shlex.join(proposed), flush=True)
            if args.action != "create" or not args.apply:
                continue
            prior_intent = state["resources"].get(spec["key"])
            existing = describe(state, spec)
            if existing is not None:
                if not prior_intent:
                    raise ValueError(
                        "resource name already exists without our saved creation intent"
                    )
                remember(state, spec, existing)
            else:
                if prior_intent and prior_intent.get("id"):
                    raise ValueError("saved resource disappeared; choose a new host state")
                state["resources"][spec["key"]] = {"name": spec["name"], "pending": True}
                save(args.state, state)
                remember(state, spec, unpack(run(proposed)))
            save(args.state, state)
        return
    if args.action == "status":
        statuses = []
        for spec in resources:
            if spec["key"] not in state["resources"]:
                continue
            actual = describe(state, spec)
            if actual is not None:
                verify_owner(state, spec, actual)
            statuses.append(
                {
                    "resource": spec["name"],
                    "present": actual is not None,
                    "status": actual.get("status") if actual else None,
                }
            )
        print(json.dumps(statuses, indent=2))
        return
    if args.action == "tunnel":
        if not 1 <= args.local_port <= 65535 or not 1 <= args.broker_port <= 65535:
            raise ValueError("invalid tunnel port")
        tunnel = gcloud(
            state,
            "compute",
            "ssh",
            state["name"],
            "--zone",
            state["cloud"]["zone"],
            "--tunnel-through-iap",
            "--",
            "-N",
            "-L",
            f"127.0.0.1:{args.local_port}:127.0.0.1:{args.broker_port}",
        )
        print(shlex.join(tunnel), flush=True)
        if args.apply:
            actual = describe(state, resources[-1])
            if actual is None:
                raise ValueError("host is absent")
            verify_owner(state, resources[-1], actual)
            subprocess.run(tunnel, check=True)
        return
    selected = list(reversed(resources)) if args.action == "delete" else [resources[-1]]
    for spec in selected:
        proposed = command(state, spec, args.action)
        print(shlex.join(proposed), flush=True)
        if not args.apply:
            continue
        actual = describe(state, spec)
        if actual is None:
            if args.action == "delete":
                continue
            raise ValueError("saved host is absent")
        verify_owner(state, spec, actual)
        run(proposed)
        state["resources"][spec["key"]]["last_operation"] = args.action
        save(args.state, state)


if __name__ == "__main__":
    main()
