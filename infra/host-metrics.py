#!/usr/bin/env python3
"""Read actual Linux host and Incus guest counters without lifecycle side effects."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def select_instances(
    inventory, project, *, all_projects=False, instance_prefix=None, active_only=False
):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", project):
        raise ValueError("invalid Incus project")
    if instance_prefix is not None and not re.fullmatch(r"mc-[a-fA-F0-9-]{0,48}", instance_prefix):
        raise ValueError("invalid managed instance prefix")
    selected = []
    for instance in inventory:
        config = instance.get("config", {})
        actual_project = instance.get("project", project)
        computer_id = config.get("user.meerkat-computer.id")
        branch = (
            all_projects
            and isinstance(computer_id, str)
            and re.fullmatch(r"[a-fA-F0-9-]{16,48}", computer_id) is not None
            # Short names keep QEMU UNIX socket paths within their length limit.
            # Retained legacy branches still belong in pool resource telemetry.
            and actual_project in {f"mcf-{computer_id}", f"{project}-fork-{computer_id}"}
            and config.get("user.meerkat-computer.fork-pool") == project
        )
        if config.get("user.meerkat-computer.managed") != "1" or (
            actual_project != project and not branch
        ):
            continue
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", actual_project):
            raise ValueError("invalid managed guest project")
        name = instance["name"]
        if not re.fullmatch(r"mc-[a-fA-F0-9-]{16,48}", name):
            raise ValueError("invalid managed guest name")
        if instance_prefix is not None and not name.startswith(instance_prefix):
            continue
        if active_only and instance.get("status") == "Stopped":
            continue
        selected.append({**instance, "project": actual_project})
    return selected


def run_incus(arguments):
    return json.loads(
        subprocess.run(
            ["incus", "--force-local", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    )


def guest_metrics(
    project, *, all_projects=False, instance_prefix=None, active_only=False, incus=run_incus
):
    # Incus query forbids a global --project flag. Project selection belongs in
    # the query URL; same-name branches are separate (project, name) entries.
    select_instances([], project, instance_prefix=instance_prefix)
    listing = (
        ["list", "--all-projects", "--format=json"]
        if all_projects
        else ["--project", project, "list", "--format=json"]
    )
    inventory = select_instances(
        incus(listing),
        project,
        all_projects=all_projects,
        instance_prefix=instance_prefix,
        active_only=active_only,
    )
    guests = []
    for instance in inventory:
        name, actual_project = instance["name"], instance["project"]
        data = incus(["query", f"/1.0/instances/{name}/state?project={actual_project}"])
        data = data.get("metadata", data)
        guests.append(
            {
                "computer_id": instance.get("config", {}).get("user.meerkat-computer.id"),
                "project": actual_project,
                "instance_name": name,
                "status": data.get("status"),
                "cpu": data.get("cpu"),
                "memory": data.get("memory"),
                "processes": data.get("processes"),
            }
        )
    return guests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="meerkat-computer")
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="Include managed fork projects belonging to this pool",
    )
    parser.add_argument("--instance-prefix", help="Optional scoped managed instance name prefix")
    parser.add_argument("--active-only", action="store_true", help="Exclude stopped guests")
    args = parser.parse_args()
    guests = guest_metrics(
        args.project,
        all_projects=args.all_projects,
        instance_prefix=args.instance_prefix,
        active_only=args.active_only,
    )

    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            memory[key + "_bytes"] = int(value.split()[0]) * 1024
    cpu_ticks = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    print(
        json.dumps(
            {
                "sampled_at_unix_ns": time.time_ns(),
                "host_memory": memory,
                "host_cpu_ticks": cpu_ticks,
                "load_average": Path("/proc/loadavg").read_text().split()[:3],
                "guests": guests,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
