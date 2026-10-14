#!/usr/bin/env python3
"""Create an owned Incus project, Btrfs pool and private bridge on an existing Linux host."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path

OWNER_KEY = "user.meerkat-computer.host-owner"
# Public IPv4 egress is allowed. Guest peers, home/VPC networks, metadata,
# carrier-grade NAT, loopback and special-purpose ranges are not guest routes.
DENIED_EGRESS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/3",
)


def acl_name(state: dict) -> str:
    return state["bridge"] + "-isolation"


def resource_name(state: dict, kind: str) -> str:
    if kind == "acl":
        return acl_name(state)
    return state[{"project": "project", "network": "bridge", "storage": "storage"}[kind]]


def network_acl(state: dict) -> dict:
    return {
        "description": "Private Meerkat Computer network boundary",
        "config": {OWNER_KEY: state["ownership_id"]},
        "ingress": [
            {
                "action": "allow",
                "state": "enabled",
                "source": str(ipaddress.ip_interface(state["address"]).ip) + "/32",
            }
        ],
        "egress": [
            {
                "action": "reject",
                "state": "enabled",
                "destination": ",".join(DENIED_EGRESS),
            }
        ],
    }


def acl_settings(state: dict) -> dict[str, str]:
    return {
        "security.acls": acl_name(state),
        "security.acls.default.ingress.action": "reject",
        "security.acls.default.egress.action": "allow",
    }


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate(project: str, bridge: str, storage: str, address: str, storage_gib: int) -> None:
    for name in (project, bridge, storage):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name) or name == "default":
            raise ValueError("use dedicated lowercase names, never 'default'")
    interface = ipaddress.ip_interface(address)
    private_ranges = [
        ipaddress.ip_network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    if (
        interface.version != 4
        or not any(interface.ip in block for block in private_ranges)
        or interface.ip in (interface.network.network_address, interface.network.broadcast_address)
        or not 16 <= interface.network.prefixlen <= 28
    ):
        raise ValueError("bridge needs a private IPv4 address and /16-/28 prefix")
    if storage_gib < 20:
        raise ValueError("Btrfs loop storage must be at least 20 GiB")


def commands(state: dict) -> list[tuple[str, list[str]]]:
    owner, project = state["ownership_id"], state["project"]
    return [
        (
            "project",
            [
                "incus",
                "--force-local",
                # Incus 6.0 project create has no description flag or stdin
                # configuration. The API creates the ownership marker and
                # project atomically, so interruption cannot leave an unowned
                # project that the recovery path would have to adopt. Query
                # rejects --project; this endpoint is the global collection.
                "query",
                "--request",
                "POST",
                "/1.0/projects",
                "--data",
                json.dumps(
                    {
                        "name": project,
                        "description": "meerkat-computer:" + owner,
                        "config": {
                            "features.images": "true",
                            "features.profiles": "true",
                            "features.networks": "false",
                        },
                    }
                ),
            ],
        ),
        (
            "storage",
            [
                "incus",
                "--force-local",
                "--project",
                "default",
                "storage",
                "create",
                state["storage"],
                "btrfs",
                f"size={state['storage_gib']}GiB",
                OWNER_KEY + "=" + owner,
            ],
        ),
        (
            "acl",
            [
                "incus",
                "--force-local",
                "--project",
                "default",
                "network",
                "acl",
                "create",
                acl_name(state),
                OWNER_KEY + "=" + owner,
            ],
        ),
        (
            "network",
            [
                "incus",
                "--force-local",
                "--project",
                "default",
                "network",
                "create",
                state["bridge"],
                "ipv4.address=" + state["address"],
                "ipv4.nat=true",
                "ipv6.address=none",
                OWNER_KEY + "=" + owner,
                *(key + "=" + value for key, value in acl_settings(state).items()),
            ],
        ),
    ]


def profile(state: dict) -> dict:
    return {
        "description": "Private Meerkat Computer guests",
        "config": {OWNER_KEY: state["ownership_id"]},
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": state["storage"]},
            "eth0": {
                "type": "nic",
                "name": "eth0",
                "network": state["bridge"],
                "security.port_isolation": "true",
                "security.ipv4_filtering": "true",
                "security.ipv6_filtering": "true",
                "security.mac_filtering": "true",
            },
        },
    }


def run(args: list[str], *, data: str | None = None) -> str:
    return subprocess.run(
        args, input=data, check=True, capture_output=True, text=True, timeout=300
    ).stdout


def inventory(kind: str) -> list[dict]:
    return json.loads(
        run(
            [
                "incus",
                "--force-local",
                "--project",
                "default",
                *(["network", "acl"] if kind == "acl" else [kind]),
                "list",
                "--format=json",
            ]
        )
    )


def verify_owner(state: dict, kind: str, actual: dict) -> None:
    name = resource_name(state, kind)
    owner = (
        actual.get("description") == "meerkat-computer:" + state["ownership_id"]
        if kind == "project"
        else actual.get("config", {}).get(OWNER_KEY) == state["ownership_id"]
    )
    if actual.get("name") != name or not owner or kind not in state.get("intents", []):
        raise ValueError(
            f"refusing {kind}: saved intent, exact name and ownership marker are required"
        )
    if kind == "storage" and actual.get("driver") != "btrfs":
        raise ValueError("owned storage changed driver")
    if kind == "acl":
        desired = network_acl(state)
        # An interrupted ACL creation may have no rules yet. Never overwrite an
        # owned ACL with other rules; a name and marker are insufficient.
        if actual.get("ingress") or actual.get("egress"):
            for direction in ("ingress", "egress"):
                normalized = [
                    {key: value for key, value in rule.items() if value != ""}
                    for rule in actual.get(direction, [])
                ]
                if normalized != desired[direction]:
                    raise ValueError("owned network ACL rules changed")
        if actual.get("config") != desired["config"]:
            raise ValueError("owned network ACL configuration changed")
    if kind == "network":
        config = actual.get("config", {})
        if (
            config.get("ipv4.address") != state["address"]
            or config.get("ipv4.nat") != "true"
            or config.get("ipv6.address") != "none"
        ):
            raise ValueError("owned bridge configuration changed")
        if any(config.get(key) != value for key, value in acl_settings(state).items()):
            raise ValueError("owned bridge isolation differs; provision a fresh owned pool")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="meerkat-computer")
    parser.add_argument("--bridge", default="mcbr0")
    parser.add_argument("--storage", default="mc-storage")
    parser.add_argument("--address", default="10.211.0.1/24")
    parser.add_argument("--storage-gib", type=int, default=100)
    parser.add_argument(
        "--state", type=Path, default=Path("/var/lib/meerkat-computer-host/setup.json")
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    validate(args.project, args.bridge, args.storage, args.address, args.storage_gib)
    requested = {
        "project": args.project,
        "bridge": args.bridge,
        "storage": args.storage,
        "address": args.address,
        "storage_gib": args.storage_gib,
    }
    if args.state.exists():
        state = json.loads(args.state.read_text())
        if any(state.get(key) != value for key, value in requested.items()):
            raise ValueError("configuration differs from the saved host setup")
    else:
        state = dict(requested, ownership_id=uuid.uuid4().hex, schema_version=1, intents=[])
    for kind, proposed in commands(state):
        print(shlex.join(proposed), flush=True)
        if not args.apply:
            if kind == "acl":
                print(
                    shlex.join(
                        [
                            "incus",
                            "--force-local",
                            "--project",
                            "default",
                            "network",
                            "acl",
                            "edit",
                            acl_name(state),
                        ]
                    )
                    + " < generated-private-acl.json",
                    flush=True,
                )
                print(json.dumps(network_acl(state), indent=2), flush=True)
            continue
        name = resource_name(state, kind)
        existing = next((item for item in inventory(kind) if item["name"] == name), None)
        if existing is None:
            # Intent is durable before the command. Unknown outcomes can be inspected safely.
            if kind not in state["intents"]:
                state["intents"].append(kind)
                save(args.state, state)
            run(proposed)
            existing = next((item for item in inventory(kind) if item["name"] == name), None)
            if existing is None:
                raise ValueError("created resource is absent from Incus inventory")
        verify_owner(state, kind, existing)
        if kind == "acl":
            run(
                [
                    "incus",
                    "--force-local",
                    "--project",
                    "default",
                    "network",
                    "acl",
                    "edit",
                    acl_name(state),
                ],
                data=json.dumps(network_acl(state)),
            )
            verified = next((item for item in inventory(kind) if item["name"] == name), None)
            if verified is None or not verified.get("ingress") or not verified.get("egress"):
                raise ValueError("network ACL rules were not confirmed after edit")
            verify_owner(state, kind, verified)
    edit = ["incus", "--force-local", "--project", state["project"], "profile", "edit", "default"]
    print(shlex.join(edit) + " < generated-private-profile.json", flush=True)
    if args.apply:
        # This default profile belongs to our newly created and verified project.
        current = json.loads(
            run(
                [
                    "incus",
                    "--force-local",
                    "--project",
                    state["project"],
                    "profile",
                    "list",
                    "--format=json",
                ]
            )
        )
        current = next(item for item in current if item["name"] == "default")
        marker = current.get("config", {}).get(OWNER_KEY)
        if marker is not None and marker != state["ownership_id"]:
            raise ValueError("default profile has another owner")
        if marker is None and (current.get("config") or current.get("devices")):
            raise ValueError("default profile is already configured; refusing adoption")
        if marker and (
            current.get("config") != profile(state)["config"]
            or current.get("devices") != profile(state)["devices"]
        ):
            raise ValueError("owned default profile was modified; refusing overwrite")
        run(edit, data=json.dumps(profile(state)))
        print(
            json.dumps(
                {
                    "project": state["project"],
                    "bridge": state["bridge"],
                    "storage": state["storage"],
                }
            )
        )


if __name__ == "__main__":
    main()
