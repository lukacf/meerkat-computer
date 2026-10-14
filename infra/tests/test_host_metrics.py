"""Host sampler selection and command tests. Never invokes Incus or cloud APIs."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "fork_host_metrics", Path(__file__).resolve().parents[1] / "host-metrics.py"
)
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)

PARENT_ID, LEGACY_ID, CHILD_ID = "a" * 32, "b" * 32, "c" * 32
LEGACY_PROJECT = "computers-fork-" + LEGACY_ID
CHILD_PROJECT = "mcf-" + CHILD_ID


def instance(
    project, name="mc-" + PARENT_ID, *, marker=None, status="Running", computer_id=PARENT_ID
):
    config = {"user.meerkat-computer.managed": "1", "user.meerkat-computer.id": computer_id}
    if marker is not None:
        config["user.meerkat-computer.fork-pool"] = marker
    return {"project": project, "name": name, "status": status, "config": config}


def test_all_projects_samples_same_name_branch_guests_and_excludes_other_pools():
    inventory = [
        instance("computers"),
        instance(LEGACY_PROJECT, marker="computers", computer_id=LEGACY_ID),
        instance(CHILD_PROJECT, marker="computers", computer_id=CHILD_ID),
        instance("computers-fork-unowned"),
        instance("other-fork-child", marker="other"),
    ]
    selected = metrics.select_instances(inventory, "computers", all_projects=True)
    assert [item["project"] for item in selected] == ["computers", LEGACY_PROJECT, CHILD_PROJECT]
    assert len({item["name"] for item in selected}) == 1
    assert len(metrics.select_instances(inventory, "computers", all_projects=False)) == 1


def test_optional_instance_prefix_and_active_filter_do_not_count_old_stopped_guests():
    inventory = [
        instance("computers", status="Stopped"),
        instance(CHILD_PROJECT, marker="computers", computer_id=CHILD_ID),
        instance("computers", name="mc-" + "b" * 32),
    ]
    selected = metrics.select_instances(
        inventory, "computers", all_projects=True, instance_prefix="mc-a", active_only=True
    )
    assert [item["project"] for item in selected] == [CHILD_PROJECT]


def test_short_and_legacy_names_require_exact_pool_and_full_computer_id():
    inventory = [
        instance(CHILD_PROJECT, marker="other", computer_id=CHILD_ID),
        instance(CHILD_PROJECT, computer_id=CHILD_ID),
        instance(CHILD_PROJECT, marker="computers", computer_id=LEGACY_ID),
        instance(LEGACY_PROJECT, marker="computers", computer_id=CHILD_ID),
        instance("mcf-" + CHILD_ID[:16], marker="computers", computer_id=CHILD_ID),
        instance("unrelated", marker="computers", computer_id=CHILD_ID),
    ]
    assert metrics.select_instances(inventory, "computers", all_projects=True) == []


def test_query_uses_project_in_url_not_forbidden_global_project_flag():
    calls = []

    def fake(arguments):
        calls.append(arguments)
        if arguments[0] == "list":
            return [
                instance("computers"),
                instance(LEGACY_PROJECT, marker="computers", computer_id=LEGACY_ID),
                instance(CHILD_PROJECT, marker="computers", computer_id=CHILD_ID),
            ]
        return {"status": "Running", "memory": {"usage": 100}}

    result = metrics.guest_metrics("computers", all_projects=True, incus=fake)
    queries = [args for args in calls if args[0] == "query"]
    assert len(result) == len(queries) == 3
    assert all("--project" not in query for query in queries)
    assert queries[1][1].endswith("?project=" + LEGACY_PROJECT)
    assert queries[2][1].endswith("?project=" + CHILD_PROJECT)
    assert result[1]["project"] == LEGACY_PROJECT
    assert result[2]["project"] == CHILD_PROJECT
