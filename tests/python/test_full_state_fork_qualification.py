"""Offline qualification-oracle tests; these do not claim live VM coverage."""

import contextlib
import copy
import importlib.util
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "full_state_fork_qualification", SCRIPTS / "qualify-full-state-fork.py"
)
qualification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualification
SPEC.loader.exec_module(qualification)


def args(tmp_path, *extra):
    return qualification.parser().parse_args(
        [
            "--broker-url",
            "http://127.0.0.1:18760",
            "--profile",
            "desktop-full",
            "--report-dir",
            str(tmp_path / "report"),
            *extra,
        ]
    )


def snapshot(label="checkpoint", number=0):
    return {
        "browser": {"nonce": "a" * 32, "value": label},
        "editor": {"pid": 127, "start_ticks": "551", "text": "unsaved " + label, "unsaved": True},
        "blender": {
            "nonce": "b" * 32,
            "pid": 143,
            "start_ticks": "593",
            "request_id": "read-one",
            "filepath": "",
            "object": "MC_FORK_CUBE",
            "location": [number, 0.0, 0.0],
        },
        "system": {"boot_id": "original-boot", "init_start_ticks": "1"},
        "disk": label,
    }


def test_dry_plan_is_inert_and_requires_one_checkpoint_with_multiple_children(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MEERKAT_COMPUTER_OPERATOR_TOKEN", raising=False)
    proposed = qualification.plan(args(tmp_path))
    assert proposed["apply"] is False
    assert proposed["children"] == 2 and proposed["checkpoint_count"] == 1
    assert "unsaved_blender" in proposed["scope"]
    assert not (tmp_path / "report").exists()
    with pytest.raises(ValueError, match="2-16"):
        qualification.plan(args(tmp_path, "--children", "1"))


def test_completed_receipt_requires_exact_source_request_and_all_distinct_children():
    valid = {
        "request_id": "request-one",
        "source_computer_id": "source",
        "phase": "completed",
        "children": ["child-a", "child-b"],
        "ready_children": 2,
        "error": None,
    }
    qualification.check_fork_view(valid, "request-one", "source", 2)
    for change in (
        {"phase": "unknown"},
        {"ready_children": 1},
        {"children": ["child-a", "child-a"]},
        {"children": ["source", "child-b"]},
        {"source_computer_id": "different"},
        {"request_id": "different"},
    ):
        with pytest.raises(qualification.QualificationFailure):
            qualification.check_fork_view({**valid, **change}, "request-one", "source", 2)


@pytest.mark.parametrize(
    "path,value",
    [
        (("browser", "nonce"), "reloaded"),
        (("editor", "pid"), 500),
        (("editor", "text"), "lost unsaved text"),
        (("editor", "unsaved"), False),
        (("blender", "nonce"), "restarted"),
        (("blender", "filepath"), "/workspace/saved.blend"),
        (("blender", "location"), [1, 0, 0]),
        (("system", "boot_id"), "new-boot"),
        (("disk",), "lost-disk"),
    ],
)
def test_restore_oracle_rejects_disk_only_reconstruction_and_partial_app_loss(path, value):
    before, after = snapshot(), snapshot()
    after["blender"]["request_id"] = "fresh-read"
    qualification.check_restored(before, after)
    target = after
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(qualification.QualificationFailure):
        qualification.check_restored(before, after)


def test_independent_mutations_recheck_parent_and_all_children_after_every_write():
    before = snapshot()
    states = [snapshot("parent", 1), snapshot("child-1", 2), snapshot("child-2", 3)]
    qualification.check_divergence(before, states, ["parent", "child-1", "child-2"])
    changed = copy.deepcopy(states)
    changed[0]["browser"]["value"] = "child-2"
    with pytest.raises(qualification.QualificationFailure):
        qualification.check_divergence(before, changed, ["parent", "child-1", "child-2"])
    with pytest.raises(qualification.QualificationFailure):
        qualification.check_divergence(before, states[:2], ["parent", "child-1", "child-2"])


def test_blender_oracle_rejects_copied_response_until_fresh_challenge_is_processed():
    response = snapshot()["blender"]
    with pytest.raises(qualification.QualificationFailure, match="challenge"):
        qualification.check_blender_response(response, "fresh-read")
    qualification.check_blender_response({**response, "request_id": "fresh-read"}, "fresh-read")


def test_worker_auth_probe_needs_positive_control_and_actual_rotated_token():
    good = {"old_status": 401, "current_status": 422, "token_changed": True}
    qualification.check_worker_auth(good, child=True)
    qualification.check_worker_auth(
        {"old_status": 422, "current_status": 422, "token_changed": False}, child=False
    )
    for change in ({"old_status": 422}, {"current_status": 401}, {"token_changed": False}):
        with pytest.raises(qualification.QualificationFailure):
            qualification.check_worker_auth({**good, **change}, child=True)


def test_inherited_observation_denial_matches_exact_public_broker_contract():
    qualification.check_parent_observation_denied({"error": {"code": "conflict", "effect": "none"}})
    for receipt in (
        {"content": []},
        {"error": {"code": "conflict", "effect": "unknown"}},
        {"error": {"code": "stale_observation", "effect": "none"}},
        {"error": {"code": "unauthorized", "effect": "none"}},
        {"error": {"code": "backend_failure", "effect": "none"}},
    ):
        with pytest.raises(qualification.QualificationFailure, match="no-effect conflict"):
            qualification.check_parent_observation_denied(receipt)


@pytest.mark.asyncio
async def test_semantic_action_reobserves_and_reselects_after_only_no_effect_conflict(tmp_path):
    observations, actions = [], []

    def handler(request):
        value = json.loads(request.content)
        if value["name"] == "computer_observe":
            number = len(observations) + 1
            observations.append(value)
            body = {
                "observation_id": "observation-" + str(number),
                "elements": [{"ref": "fresh-ref-" + str(number), "label": "Target"}],
            }
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": json.dumps(body)}]}
            )
        actions.append(value)
        if len(actions) == 1:
            return httpx.Response(409, json={"error": {"code": "conflict", "effect": "none"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    evidence = qualification.Evidence(tmp_path / "evidence", "semantic-action")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = qualification.BrokerAPI(client, "http://127.0.0.1:1", "operator-token", evidence)
        runner = qualification.FullStateQualification(args(tmp_path), api, evidence)
        result = await runner.semantic_action(
            {"binding": {"computer_id": "parent", "token": "agent-token"}},
            "desktop",
            lambda view: {"type": "focus", "ref": view["elements"][0]["ref"]},
        )
    assert qualification.text_receipt(result) == {}
    assert len(observations) == len(actions) == 2
    assert [item["arguments"]["observation_id"] for item in actions] == [
        "observation-1",
        "observation-2",
    ]
    assert [item["arguments"]["action"]["ref"] for item in actions] == [
        "fresh-ref-1",
        "fresh-ref-2",
    ]
    assert len({item["action_id"] for item in actions}) == 2
    assert evidence.data["checks"][-1]["attempt"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,attempts",
    [
        ({"code": "conflict", "effect": "none"}, 3),
        ({"code": "conflict", "effect": "unknown"}, 1),
        ({"code": "backend_failure", "effect": "none"}, 1),
        ({"code": "unauthorized", "effect": "none"}, 1),
        ({"code": "stale_observation", "effect": "none"}, 1),
    ],
)
async def test_semantic_action_is_bounded_and_does_not_retry_other_errors(
    tmp_path, error, attempts
):
    evidence = qualification.Evidence(tmp_path / "evidence", "semantic-action")
    runner = qualification.FullStateQualification(args(tmp_path), None, evidence)
    calls = []

    async def observe(*_):
        return {}, {"observation_id": str(len(calls)), "elements": [{"ref": "target"}]}

    async def tool(*positional, **keyword):
        calls.append((positional, keyword))
        return {"error": error}

    runner.observe, runner.tool = observe, tool
    with pytest.raises(qualification.QualificationFailure) as failure:
        await runner.semantic_action(
            {"binding": {"computer_id": "parent"}},
            "browser",
            lambda view: {"type": "click", "ref": view["elements"][0]["ref"]},
        )
    assert len(calls) == attempts
    assert len({keyword["action_id"] for _, keyword in calls}) == attempts
    assert failure.value.effect == error["effect"]


@pytest.mark.asyncio
async def test_parent_observation_denial_stays_on_direct_nonretrying_path(tmp_path):
    runner = qualification.FullStateQualification(
        args(tmp_path), None, qualification.Evidence(tmp_path / "evidence", "semantic-action")
    )
    calls = []

    async def tool(*positional, **keyword):
        calls.append((positional, keyword))
        return {"error": {"code": "conflict", "effect": "none"}}

    runner.tool = tool
    value = await runner.action_error(
        {"binding": {"computer_id": "child"}},
        {
            "observation_id": "parent-observation",
            "elements": [{"ref": "parent-ref", "label": "Set parent"}],
        },
    )
    qualification.check_parent_observation_denied(value)
    assert len(calls) == 1
    assert calls[0][0][2]["observation_id"] == "parent-observation"


def test_vault_assignment_is_parent_authority_not_cloned_application_state():
    parent = {"credentials": [{"id": "synthetic-credential"}]}
    qualification.check_credential_scope(parent, "synthetic-credential", child=False)
    qualification.check_credential_scope({"credentials": []}, "synthetic-credential", child=True)
    with pytest.raises(qualification.QualificationFailure):
        qualification.check_credential_scope(parent, "synthetic-credential", child=True)
    with pytest.raises(qualification.QualificationFailure):
        qualification.check_credential_scope(
            {"credentials": []}, "synthetic-credential", child=False
        )


def test_editor_oracle_uses_real_mousepad_app_not_browser_text():
    view = {
        "applications": [{"app_id": "m", "name": "Mousepad"}, {"app_id": "b", "name": "Chromium"}],
        "windows": [{"app_id": "m", "name": "*Untitled 1 - Mousepad"}],
        "elements": [
            {"app_id": "b", "ref": "e1", "states": ["editable", "multi_line"], "text": "wrong"},
            {
                "app_id": "m",
                "ref": "e2",
                "states": ["editable", "multi_line"],
                "text": "unsaved checkpoint",
                "text_truncated": False,
            },
        ],
    }
    assert qualification.editor_node(view)["ref"] == "e2"
    view["elements"][1]["text_truncated"] = True
    with pytest.raises(qualification.QualificationFailure):
        qualification.editor_node(view)


@pytest.mark.asyncio
async def test_uncertain_fork_request_is_issued_once_and_has_no_new_grants(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic transport failure", request=request)

    evidence = qualification.Evidence(tmp_path, "offline-only")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = qualification.BrokerAPI(client, "http://127.0.0.1:1", "x" * 40, evidence)
        runner = qualification.FullStateQualification(args(tmp_path), api, evidence)
        source = {"binding": {"computer_id": "source", "lease_id": "lease", "epoch": 1}}
        with pytest.raises(qualification.QualificationFailure) as failure:
            await runner.fork(
                source,
                [
                    {"owner": runner.owner(1), "slot": "main"},
                    {"owner": runner.owner(2), "slot": "main"},
                ],
            )
    assert failure.value.effect == "unknown"
    assert len(calls) == 1 and calls[0].url.path == "/v1/forks"
    payload = json.loads(calls[0].content)
    assert payload["request_id"] == runner.fork_id
    assert payload["source_lease_id"] == "lease" and len(payload["targets"]) == 2
    assert runner.bindings == {}


def test_guest_fixture_sources_are_valid_python_and_never_embed_a_worker_token():
    for source in (
        qualification.browser_fixture_source(43127, 8000, 2),
        qualification.blender_fixture_source("blender-probe"),
    ):
        compile(source, "synthetic-guest-fixture", "exec")
        assert "synthetic-worker-token" not in source


def network_baseline(address="10.10.0.2"):
    return {
        "uid": 0,
        "address": address,
        "interface": "enp5s0",
        "gateway": "10.10.0.1",
        "controls": {
            name: True
            for name in ("loopback_worker", "loopback_fixture", "primary_worker", "primary_fixture")
        },
    }


def test_peer_isolation_requires_root_and_live_primary_listeners_on_every_branch():
    baseline = network_baseline()
    qualification.check_network_baseline(baseline)
    for change in (
        {"uid": 1000},
        {"address": "127.0.0.1"},
        {"controls": {}},
        {"controls": {**baseline["controls"], "primary_fixture": False}},
    ):
        with pytest.raises(qualification.QualificationFailure):
            qualification.check_network_baseline({**baseline, **change})


def test_network_selection_accepts_only_equivalent_defaults_and_keeps_duplicate_evidence():
    defaults = [
        {"dev": "enp5s0", "gateway": "10.10.0.1", "metric": metric, "protocol": protocol}
        for metric, protocol in ((0, "boot"), (1024, "static"))
    ]
    devices = [
        {
            "ifname": "enp5s0",
            "addr_info": [{"scope": "global", "local": "10.10.0.2", "family": "inet"}],
        }
    ]
    selected = qualification.primary_network(defaults, devices)
    assert selected == {
        "address": "10.10.0.2",
        "interface": "enp5s0",
        "gateway": "10.10.0.1",
        "route_count": 2,
        "routes": [
            {
                "interface": "enp5s0",
                "gateway": "10.10.0.1",
                "entry_count": 2,
                "metrics": [0, 1024],
                "protocols": ["boot", "static"],
            }
        ],
    }
    assert qualification.safe_network_baseline(selected) == selected
    for changed in (
        [defaults[0], {**defaults[1], "gateway": "10.10.0.9"}],
        [defaults[0], {**defaults[1], "dev": "enp6s0"}],
    ):
        rejected = qualification.primary_network(changed, devices)
        assert rejected["probe_error"] == "default_route_ambiguous"
        assert "address" not in rejected
    many_addresses = copy.deepcopy(devices)
    many_addresses[0]["addr_info"].append(
        {"scope": "global", "local": "10.10.0.3", "family": "inet"}
    )
    assert (
        qualification.primary_network(defaults, many_addresses)["probe_error"]
        == "primary_ipv4_ambiguous"
    )


def test_generated_network_probe_records_equivalent_routes_and_real_health_controls(monkeypatch):
    defaults = [
        {"dev": "enp5s0", "gateway": "10.10.0.1", "metric": value, "protocol": protocol}
        for value, protocol in ((0, "boot"), (1024, "static"))
    ]
    devices = [{"ifname": "enp5s0", "addr_info": [{"scope": "global", "local": "10.10.0.2"}]}]

    def ip(arguments, **_):
        return json.dumps(defaults if "route" in arguments else devices)

    class Response:
        status = 200

        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return (
                b"fixture-ready"
                if ":43127/" in self.url
                else b'{"protocol_version":1,"ready":true}'
            )

    class Opener:
        def open(self, url, **_):
            return Response(url)

    monkeypatch.setattr("subprocess.check_output", ip)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(qualification.network_baseline_source(43127, 18765), {})  # noqa: S102 - own generated fixture
    value = json.loads(output.getvalue())
    qualification.check_network_baseline(value)
    assert value["route_count"] == 2 and value["routes"][0]["metrics"] == [0, 1024]
    assert value["routes"][0]["protocols"] == ["boot", "static"]
    assert len(value["controls"]) == 4 and all(value["controls"].values())


@pytest.mark.asyncio
async def test_owned_probe_command_failure_records_exit_status_without_raw_output(tmp_path):
    private = "synthetic-sensitive-command-output"
    evidence = qualification.Evidence(tmp_path / "evidence", "network-error")
    evidence.protect(private)
    runner = qualification.FullStateQualification(args(tmp_path), None, evidence)

    async def tool(*_, **__):
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "exit_code": 1,
                            "timed_out": False,
                            "truncated": False,
                            "stdout": private,
                            "stderr": private,
                            "output_bytes": len(private) * 2,
                        }
                    ),
                }
            ]
        }

    runner.tool = tool
    with pytest.raises(qualification.QualificationFailure, match="network_baseline"):
        await runner.root_probe({"binding": {"computer_id": "child"}}, "network_baseline", "pass")
    event = evidence.data["checks"][-1]
    assert event["status"] == "failed" and event["exit_code"] == 1
    assert event["probe"] == "network_baseline" and event["computer_id"] == "child"
    assert private not in (tmp_path / "evidence" / "report.json").read_text()


@pytest.mark.asyncio
async def test_network_probe_failure_records_closed_route_diagnostic_without_arbitrary_metadata(
    tmp_path,
):
    private = "arbitrary-sensitive-output"
    evidence = qualification.Evidence(tmp_path / "evidence", "network-error")
    evidence.protect(private)
    runner = qualification.FullStateQualification(args(tmp_path), None, evidence)
    routes = qualification.primary_network(
        [
            {"dev": "enp5s0", "gateway": "10.10.0.1", "protocol": "boot"},
            {"dev": "enp5s0", "gateway": "10.10.0.9", "protocol": "static"},
        ],
        [],
    )

    async def probe(*_):
        return {**routes, "uid": 0, "stderr": private, "message": private}

    runner.root_probe = probe
    with pytest.raises(qualification.QualificationFailure):
        await runner.network_baseline({"binding": {"computer_id": "child"}}, "before_peer_probes")
    event = evidence.data["checks"][-1]
    assert event["status"] == "failed" and event["probe_error"] == "default_route_ambiguous"
    assert event["route_count"] == 2 and event["routes"] == routes["routes"]
    assert private not in (tmp_path / "evidence" / "report.json").read_text()
    safe = qualification.safe_network_baseline({**routes, "probe_error": private})
    assert safe["probe_error"] == "invalid_probe_output"


def test_generated_network_probe_has_closed_ip_error_and_still_attempts_loopback_controls(
    monkeypatch,
):
    def ip(*_, **__):
        raise OSError("arbitrary-private-error")

    class Opener:
        def open(self, *_args, **_kwargs):
            raise OSError("arbitrary-private-health-error")

    monkeypatch.setattr("subprocess.check_output", ip)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(qualification.network_baseline_source(43127, 18765), {})  # noqa: S102 - own generated fixture
    value = json.loads(output.getvalue())
    assert value == {
        "probe_error": "ip_command_failed",
        "uid": 0,
        "controls": {"loopback_worker": False, "loopback_fixture": False},
    }
    with pytest.raises(qualification.QualificationFailure):
        qualification.check_network_baseline(value)


def test_root_probe_requires_every_peer_and_both_worker_and_fixture_ports_to_be_blocked():
    targets = [
        {"computer_id": "peer-one", "address": "10.20.0.2", "port": 18765},
        {"computer_id": "peer-one", "address": "10.20.0.2", "port": 43127},
    ]
    result = {"uid": 0, "probes": [{**item, "connected": False} for item in targets]}
    qualification.check_peer_denial(result, targets)
    for changed in (
        {"uid": 1000, "probes": result["probes"]},
        {"uid": 0, "probes": result["probes"][:1]},
        {"uid": 0, "probes": [{**item, "connected": True} for item in targets]},
        {"uid": 0, "probes": [{**item, "connected": None} for item in targets]},
    ):
        with pytest.raises(qualification.QualificationFailure):
            qualification.check_peer_denial(changed, targets)
    compile(qualification.network_baseline_source(43127, 18765), "baseline", "exec")
    compile(qualification.peer_probe_source(targets), "peer-probe", "exec")


def test_dry_plan_with_optional_host_sampler_does_not_read_command_file(tmp_path):
    parsed = args(tmp_path, "--host-metrics-command-file", str(tmp_path / "not-created.json"))
    assert qualification.plan(parsed)["phase_host_metrics"] is True
    assert not parsed.host_metrics_command_file.exists()


@pytest.mark.asyncio
async def test_phase_sampler_rejects_a_copied_old_sample(tmp_path):
    command = tmp_path / "host-sample.json"
    command.write_text(
        json.dumps(
            [
                sys.executable,
                "-c",
                "import json; print(json.dumps("
                + repr({"sampled_at_unix_ns": 1, "host_memory": {}, "guests": []})
                + "))",
            ]
        )
    )
    parsed = args(tmp_path, "--host-metrics-command-file", str(command))
    evidence = qualification.Evidence(tmp_path / "evidence", "offline-phase")
    runner = qualification.FullStateQualification(parsed, None, evidence)
    with pytest.raises(qualification.QualificationFailure, match="fresh"):
        await runner.sample_host("children_ready")
