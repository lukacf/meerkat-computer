"""Local harness tests. Fake HTTP transport never establishes live pool qualification."""

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

OPERATOR = "synthetic-operator-" + "f" * 40

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_qualification.py"
spec = importlib.util.spec_from_file_location("live_qualification", SCRIPT)
qualification = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = qualification
spec.loader.exec_module(qualification)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:18760",
        "http://localhost:18760",
        "http://127.0.0.1:18760/path",
        "http://user:password@127.0.0.1:18760",
        "http://127.0.0.1:18760?token=x",
        "http://127.1:18760",
    ],
)
def test_broker_must_be_literal_loopback_without_credentials_or_path(url):
    with pytest.raises(ValueError, match="loopback"):
        qualification.broker_url(url)


def test_broker_ipv4_and_ipv6_loopback_are_allowed():
    assert qualification.broker_url("http://127.0.0.1:18760/") == "http://127.0.0.1:18760"
    assert qualification.broker_url("http://[::1]:18760") == "http://[::1]:18760"


def test_plan_does_not_read_credentials_or_connect(tmp_path, monkeypatch):
    monkeypatch.delenv("MEERKAT_COMPUTER_OPERATOR_TOKEN", raising=False)
    args = qualification.parser().parse_args(
        [
            "--broker-url",
            "http://127.0.0.1:18760",
            "--profile",
            "desktop",
            "--report-dir",
            str(tmp_path / "evidence"),
        ]
    )
    plan = qualification.plan(args)
    assert plan["apply"] is False and plan["creates_new_scoped_identities"] is True
    assert plan["concurrency"] == 2 and plan["repeats"] == 1
    assert not (tmp_path / "evidence").exists()


def test_private_token_file_rejects_symlink_and_open_permissions(tmp_path):
    path = tmp_path / "token"
    path.write_text("synthetic-operator-" + "a" * 32)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        qualification.read_token(None, path)
    path.chmod(0o600)
    assert qualification.read_token(None, path) == path.read_text()
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        qualification.read_token(None, link)


def test_network_target_accepts_only_literal_addresses_and_port():
    assert qualification.network_target("10.211.0.1:43127") == ("10.211.0.1", 43127)
    assert qualification.network_target("[::1]:43127") == ("::1", 43127)
    for value in ("metadata.google.internal:80", "10.211.0.1", "1.1.1.1:99999", "a:1;true"):
        with pytest.raises(ValueError):
            qualification.network_target(value)


def test_isolation_needs_both_positive_controls_and_verified_target_listener():
    targets = [{"label": "peer", "host": "10.211.0.2", "port": 43127, "listener_verified": True}]
    probes = {"uid": 0, "loopback": True, "public": True, "peer": False}
    assert qualification.isolation_verdict(probes, targets)["status"] == "passed"
    for field in ("loopback", "public"):
        bad = {**probes, field: False}
        assert qualification.isolation_verdict(bad, targets)["status"] == "inconclusive"
    assert (
        qualification.isolation_verdict(probes, [{**targets[0], "listener_verified": False}])[
            "status"
        ]
        == "inconclusive"
    )
    assert qualification.isolation_verdict({**probes, "peer": True}, targets)["status"] == "failed"
    assert (
        qualification.isolation_verdict({**probes, "uid": 1000}, targets)["status"]
        == "inconclusive"
    )


def test_evidence_never_serializes_tokens_or_arbitrary_guest_error_text(tmp_path):
    report = qualification.Evidence(tmp_path, "test-run")
    secret = "a-synthetic-secret"
    report.protect(secret)
    with pytest.raises(qualification.QualificationFailure, match="secret"):
        report.assert_safe({"content": [{"text": secret}]})
    report.event(
        "call", "failed", error={"code": "unknown_effect", "message": secret, "effect": "unknown"}
    )
    report.write()
    payload = (tmp_path / "report.json").read_text()
    assert secret not in payload
    assert "unknown_effect" in payload and "unknown" in payload
    assert os.stat(tmp_path / "report.json").st_mode & 0o777 == 0o600


@pytest.mark.parametrize("operation", ["worker_service_start", "systemd_manager_readiness"])
def test_evidence_keeps_only_typed_backend_diagnostic_fields(tmp_path, operation):
    evidence = qualification.Evidence(tmp_path, "safe-diagnostics")
    secret = "synthetic-private-diagnostic"
    evidence.protect(secret)
    evidence.event(
        "attach",
        "failed",
        error={
            "code": "backend_failure",
            "effect": "unknown",
            "message": secret,
            "backend_failure": {
                "category": "command_failed",
                "operation": operation,
                "message": secret,
                "stderr": secret,
                "token": secret,
            },
        },
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["events"][0]["error"] == {
        "code": "backend_failure",
        "effect": "unknown",
        "backend_failure": {"category": "command_failed", "operation": operation},
    }
    assert secret not in json.dumps(report)


@pytest.mark.parametrize(
    "diagnostic",
    [
        None,
        "synthetic-private-text",
        [],
        {},
        {"category": "synthetic-private-text"},
        {"category": {"secret": "synthetic-private-text"}},
        {"category": "command_failed", "operation": "synthetic-private-text"},
        {"category": "command_failed", "operation": ["worker_service_start"]},
        {"category": "command_failed", "operation": None},
    ],
)
def test_unknown_or_untyped_backend_diagnostics_are_omitted(diagnostic):
    assert qualification.safe_backend_failure(diagnostic) is None


def test_unknown_fork_effect_keeps_closed_backend_diagnostic_and_discards_metadata(tmp_path):
    evidence = qualification.Evidence(tmp_path, "unknown-fork")
    private = "synthetic-private-backend-detail"
    evidence.protect(private)
    evidence.event(
        "fork_one_checkpoint",
        "failed",
        error={
            "code": "unknown_effect",
            "effect": "unknown",
            "message": private,
            "metadata": {"token": private},
            "backend_failure": {
                "category": "command_failed",
                "operation": "checkpoint_create",
                "stderr": private,
                "metadata": {"token": private},
            },
        },
    )
    recorded = json.loads((tmp_path / "report.json").read_text())["events"][0]["error"]
    assert recorded == {
        "code": "unknown_effect",
        "effect": "unknown",
        "backend_failure": {"category": "command_failed", "operation": "checkpoint_create"},
    }
    assert private not in (tmp_path / "report.json").read_text()


@pytest.mark.parametrize(
    "operation",
    [
        "fork_capability_inspection",
        "fork_prepare",
        "checkpoint_create",
        "checkpoint_inspection",
        "checkpoint_release",
        "fork_copy",
        "fork_network",
        "fork_resume",
        "fork_rebind",
    ],
)
def test_new_fork_diagnostics_preserve_only_closed_typed_fields(operation):
    diagnostic = {
        "category": "unsupported",
        "operation": operation,
        "stderr": "private backend text",
        "token": "private token",
    }
    assert qualification.safe_backend_failure(diagnostic) == {
        "category": "unsupported",
        "operation": operation,
    }


def test_overall_startup_deadline_does_not_infer_a_failed_suboperation(tmp_path):
    evidence = qualification.Evidence(tmp_path, "overall-timeout")
    evidence.event(
        "attach",
        "failed",
        error={
            "code": "backend_failure",
            "effect": "unknown",
            "backend_failure": {"category": "worker_not_ready"},
        },
    )
    assert evidence.data["events"][0]["error"]["backend_failure"] == {
        "category": "worker_not_ready"
    }


@pytest.mark.asyncio
async def test_failed_attach_keeps_typed_diagnostic_and_latency_without_retry(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            502,
            json={
                "error": {
                    "code": "backend_failure",
                    "effect": "unknown",
                    "message": "discarded arbitrary response text",
                    "backend_failure": {
                        "category": "command_timeout",
                        "operation": "worker_credential_injection",
                    },
                }
            },
        )

    evidence = qualification.Evidence(tmp_path, "failed-start")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = qualification.BrokerAPI(client, "http://127.0.0.1:1", OPERATOR, evidence)
        with pytest.raises(qualification.QualificationFailure) as failure:
            await api.attach({"owner": {"identity": "test"}})
    assert failure.value.effect == "unknown"
    assert len(calls) == 1
    event = evidence.data["events"][0]
    assert event["http_status"] == 502 and event["elapsed_ms"] >= 0
    assert event["error"]["backend_failure"] == {
        "category": "command_timeout",
        "operation": "worker_credential_injection",
    }
    assert "discarded arbitrary" not in (tmp_path / "report.json").read_text()


@pytest.mark.asyncio
async def test_unknown_tool_effect_is_not_automatically_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            409,
            json={"error": {"code": "unknown_effect", "effect": "unknown", "message": "private"}},
        )

    evidence = qualification.Evidence(tmp_path, "test-run")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = qualification.BrokerAPI(client, "http://127.0.0.1:1", OPERATOR, evidence)
        with pytest.raises(qualification.QualificationFailure) as failed:
            await api.tool(
                {"token": "lease-token", "computer_id": "test"},
                "computer_act",
                {"action": {"type": "click", "x": 1, "y": 1}},
            )
    assert failed.value.effect == "unknown" and len(calls) == 1
    evidence.write()
    assert "lease-token" not in (tmp_path / "report.json").read_text()
    assert "private" not in (tmp_path / "report.json").read_text()


@pytest.mark.asyncio
async def test_attachment_token_is_kept_out_of_report(tmp_path):
    token = "synthetic-control-token-" + "c" * 32
    response = {"computer_id": "computer-one", "lease_id": "lease-one", "epoch": 1, "token": token}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        evidence = qualification.Evidence(tmp_path, "test-run")
        api = qualification.BrokerAPI(client, "http://127.0.0.1:1", OPERATOR, evidence)
        assert (await api.attach({"owner": {"identity": "test"}}))["token"] == token
        evidence.write()
    assert token not in (tmp_path / "report.json").read_text()


def test_fixture_does_not_embed_password_and_uses_post_and_persistent_cookie():
    source = qualification.fixture_source("scoped-run", 43127, "a" * 64)
    compile(source, "fixture.py", "exec")
    assert 'method="post"' in source
    assert "Max-Age=86400" in source
    assert "sha256" in source
    assert "def log_message" in source
    assert "password-never-embedded" not in source


def test_guest_probe_never_requests_metadata_content():
    source = qualification.probe_source(
        [{"label": "metadata", "host": "169.254.169.254", "port": 80}],
        ("1.1.1.1", 443),
        43127,
    )
    compile(source, "probe.py", "exec")
    assert "create_connection" in source
    assert "urlopen" not in source and ".send(" not in source and ".recv(" not in source


@pytest.mark.parametrize("preexisting", [False, True])
def test_peer_gateway_probe_restores_only_its_new_route(monkeypatch, capsys, preexisting):
    import socket

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:4] == ["-j", "route", "show"]:
            value = (
                [{"gateway": "10.211.0.1", "dev": "eth0"}]
                if command[-1] == "default" or preexisting
                else []
            )
        elif command[1:4] == ["-j", "route", "get"]:
            value = [{"gateway": "10.211.0.1", "dev": "eth0"}]
        else:
            value = []
        return subprocess.CompletedProcess(command, 0, json.dumps(value))

    def blocked(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    source = qualification.gateway_probe_source(
        [{"label": "owned_peer_1", "host": "10.211.0.3", "port": 43127}]
    )
    exec(compile(source, "gateway-probe.py", "exec"), {})  # noqa: S102 - owned generator; effects mocked
    result = json.loads(capsys.readouterr().out)["routes"][0]
    route = ["10.211.0.3/32", "via", "10.211.0.1", "dev", "eth0"]
    mutations = [
        command for command in calls if command[1:3] in (["route", "add"], ["route", "del"])
    ]
    if preexisting:
        assert mutations == [] and result["preexisting"] is True
        assert result.get("connected") is None
    else:
        assert mutations == [["ip", "route", "add", *route], ["ip", "route", "del", *route]]
        assert result["connected"] is False
        assert result["route_verified"] is True and result["restored"] is True
    assert ".send(" not in source and ".recv(" not in source


def test_gateway_denial_requires_verified_route_and_successful_restore():
    targets = [{"label": "peer", "listener_verified": True}]
    probes = {"uid": 0, "loopback": True, "public": True}
    result = {
        "uid": 0,
        "routes": [
            {
                "label": "peer",
                "connected": False,
                "installed": True,
                "route_verified": True,
                "restored": True,
            }
        ],
    }
    assert qualification.gateway_verdict(result, probes, targets)["status"] == "passed"
    for field in ("installed", "route_verified", "restored"):
        changed = {**result, "routes": [{**result["routes"][0], field: False}]}
        assert qualification.gateway_verdict(changed, probes, targets)["status"] == "inconclusive"
    reached = {**result, "routes": [{**result["routes"][0], "connected": True}]}
    assert qualification.gateway_verdict(reached, probes, targets)["status"] == "failed"


def test_container_confinement_requires_unmapped_host_root_and_no_host_socket():
    facts = {
        "uid": 0,
        "uid_map": [[0, 1065536, 65536]],
        "gid_map": [[0, 1065536, 65536]],
        "visible_host_control_sockets": [],
        "mounts": [{"target": "/", "filesystem": "btrfs"}],
    }
    assert qualification.confinement_verdict(facts, "container")["status"] == "passed"
    bad = {**facts, "uid_map": [[0, 0, 4294967295]]}
    assert qualification.confinement_verdict(bad, "container")["status"] == "failed"
    assert qualification.confinement_verdict(bad, "virtual-machine")["status"] == "passed"
    for mapping in ("uid_map", "gid_map"):
        bad = {**facts, mapping: [[0, 1065536, 5000], [5000, 0, 1]]}
        assert qualification.confinement_verdict(bad, "container")["status"] == "failed"
    bad = {**facts, "visible_host_control_sockets": ["/var/lib/incus/unix.socket"]}
    assert qualification.confinement_verdict(bad, "virtual-machine")["status"] == "failed"
    assert qualification.confinement_verdict(facts, None)["status"] == "inconclusive"


def test_evidence_checks_freeze_nested_measurements_before_later_guest_updates(tmp_path):
    evidence = qualification.Evidence(tmp_path, "stable-evidence")
    system = {"boot_id": "initial"}
    evidence.check("parallel_guests", guest_measurements=[{"system": system}])
    system["boot_id"] = "restarted"
    evidence.write()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["checks"][0]["guest_measurements"][0]["system"]["boot_id"] == "initial"


def test_unrelated_worker_failure_is_not_accepted_as_authorization_denial():
    assert qualification.authorization_denied({"error": {"code": "unauthorized", "effect": "none"}})
    assert qualification.authorization_denied(
        {"error": {"code": "capability_denied", "effect": "none"}}
    )
    for code, effect in (
        ("backend_failure", "none"),
        ("worker_unavailable", "none"),
        ("unauthorized", "unknown"),
    ):
        assert not qualification.authorization_denied({"error": {"code": code, "effect": effect}})


@pytest.mark.asyncio
async def test_missing_native_fixture_dependency_is_installed_only_after_explicit_probe(tmp_path):
    args = qualification.parser().parse_args(
        [
            "--broker-url",
            "http://127.0.0.1:1",
            "--profile",
            "desktop",
            "--report-dir",
            str(tmp_path),
            "--apply",
        ]
    )
    evidence = qualification.Evidence(tmp_path, "dependency-test")
    runner = qualification.LiveQualification(args, None, evidence)
    commands = []
    outputs = iter(["false", "installed", "true"])

    async def execute(guest, command, timeout=30):
        commands.append(command)
        return next(outputs)

    runner.execute = execute
    await runner.native_fixture_dependency({"binding": {"computer_id": "created-guest"}})
    assert len(commands) == 3
    assert (
        commands[1]
        == "sudo -n apt-get install -y --no-install-recommends python3-gi gir1.2-gtk-3.0"
    )
    assert evidence.data["checks"][-1]["installed_for_fixture"] is True


@pytest.mark.asyncio
async def test_existing_native_fixture_dependency_does_not_mutate_guest(tmp_path):
    args = qualification.parser().parse_args(
        [
            "--broker-url",
            "http://127.0.0.1:1",
            "--profile",
            "desktop",
            "--report-dir",
            str(tmp_path),
            "--apply",
        ]
    )
    evidence = qualification.Evidence(tmp_path, "dependency-test")
    runner = qualification.LiveQualification(args, None, evidence)
    commands = []

    async def execute(guest, command, timeout=30):
        commands.append(command)
        return "true"

    runner.execute = execute
    await runner.native_fixture_dependency({"binding": {"computer_id": "created-guest"}})
    assert len(commands) == 1 and "apt-get" not in commands[0]
    assert evidence.data["checks"][-1]["installed_for_fixture"] is False


@pytest.mark.asyncio
async def test_parallel_effects_are_all_observed_before_cleanup():
    effects = []

    async def failed():
        raise qualification.QualificationFailure("fixture failure")

    async def slow():
        await asyncio.sleep(0.01)
        effects.append("completed")

    with pytest.raises(qualification.QualificationFailure):
        await qualification.LiveQualification.parallel([failed(), slow()])
    assert effects == ["completed"]


def test_fixture_independently_checks_password_and_browser_cookie(tmp_path):
    """Real local fixture server only. This is not a broker/Incus/cloud test."""
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    secret = "password-never-embedded"
    path = tmp_path / "fixture.py"
    source = qualification.fixture_source(
        "owned-test", port, hashlib.sha256(secret.encode()).hexdigest()
    )
    assert secret not in source
    path.write_text(source)
    process = subprocess.Popen(
        [sys.executable, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            for _ in range(60):
                try:
                    if client.get("/health").is_success:
                        break
                except httpx.HTTPError:
                    time.sleep(0.05)
            else:
                pytest.fail("Local fixture did not start")
            bad = client.post(
                "/login", data={"username": "qualification-user", "password": "wrong"}
            )
            assert "Login denied" in bad.text
            assert "No session" in client.get("/session").text
            good = client.post(
                "/login", data={"username": "qualification-user", "password": secret}
            )
            assert "Login verified owned-test" in good.text
            assert "Session verified owned-test" in client.get("/session").text
            audit = json.loads(path.with_suffix(".audit.json").read_text())
            assert audit == {
                "login_denied": 1,
                "session_denied": 1,
                "login_success": 1,
                "session_success": 1,
            }
            assert secret not in path.with_suffix(".audit.json").read_text()
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_cleanup_only_stops_created_run_scope_and_does_not_delete_disks(tmp_path):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path == "/v1/computers":
            return httpx.Response(
                200,
                json={
                    "computers": [
                        {
                            "computer_id": "ours",
                            "owner": {
                                "kind": "identity",
                                "realm": "qualification",
                                "namespace": "qualification:test-run",
                                "identity": "guest-0",
                            },
                            "phase": "cold",
                        },
                        {
                            "computer_id": "unrelated",
                            "owner": {
                                "kind": "identity",
                                "realm": "qualification",
                                "namespace": "somebody-else",
                                "identity": "guest-0",
                            },
                            "phase": "ready",
                        },
                    ]
                },
            )
        return httpx.Response(200, json={})

    args = qualification.parser().parse_args(
        [
            "--broker-url",
            "http://127.0.0.1:1",
            "--profile",
            "desktop",
            "--report-dir",
            str(tmp_path),
            "--apply",
        ]
    )
    evidence = qualification.Evidence(tmp_path, "test-run")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = qualification.LiveQualification(
            args, qualification.BrokerAPI(client, args.broker_url, OPERATOR, evidence), evidence
        )
        runner.bindings["ours"] = {"computer_id": "ours", "token": "our-token"}
        await runner.cleanup()
    assert ("POST", "/v1/lease/release", "Bearer our-token") in requests
    assert ("POST", "/v1/computers/ours/stop", "Bearer " + OPERATOR) in requests
    assert not any("unrelated" in path or method == "DELETE" for method, path, _ in requests)
