"""Safety checks for the explicit deployed-operator qualification controller."""

import importlib.util
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "operator_qualification", ROOT / "scripts/live_operator_qualification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_never_reads_operator_or_restart_command(controller, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_operator_qualification.py",
            "--broker-url",
            "http://127.0.0.1:18760",
            "--profile",
            "short-idle",
            "--report-dir",
            "/does/not/exist",
            "--operator-token-file",
            "/does/not/exist/token",
            "--restart-command-file",
            "/does/not/exist/restart.json",
        ],
    )
    assert controller.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["apply"] is False
    assert plan["requires_exclusive_broker"] is True
    assert "automatic_idle_stop" in plan["scope"]


def test_preview_must_reuse_exact_receipt_and_unchanged_cursor(controller):
    receipt = {
        "content": [
            {"type": "text", "text": json.dumps({"observation_id": "a" * 32})},
            {"type": "image", "data": "agent-captured-pixels"},
        ]
    }
    first = {
        "status": "available",
        "frame": {
            "observation_id": "a" * 32,
            "data": "agent-captured-pixels",
            "received_at_ms": 123,
        },
    }
    unchanged = {"status": "available", "frame": {**first["frame"], "data": None}}
    controller.check_preview_reuse(receipt, first, unchanged)
    for corrupt in [
        {**first, "frame": {**first["frame"], "data": "another-capture"}},
        {**first, "frame": {**first["frame"], "observation_id": "b" * 32}},
    ]:
        with pytest.raises(controller.QualificationFailure):
            controller.check_preview_reuse(receipt, corrupt, unchanged)
    with pytest.raises(controller.QualificationFailure):
        controller.check_preview_reuse(receipt, first, first)


def test_withheld_checks_require_no_pixels_and_active_privacy(controller):
    safe = {"content": [{"type": "text", "text": '{"privacy":{"active":true}}'}]}
    controller.check_withheld(safe, {"status": "withheld", "frame": None})
    for unsafe in [
        {"content": safe["content"] + [{"type": "image", "data": "leak"}]},
        {"content": [{"type": "text", "text": '{"privacy":{"active":false}}'}]},
    ]:
        with pytest.raises(controller.QualificationFailure):
            controller.check_withheld(unsafe, {"status": "withheld", "frame": None})


@pytest.mark.asyncio
async def test_failed_restart_is_not_retried_or_echoed(controller, monkeypatch):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)

        async def communicate():
            return b"protected-output", b"protected-error"

        return SimpleNamespace(communicate=communicate, returncode=1)

    monkeypatch.setattr(controller.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(controller.QualificationFailure) as failure:
        await controller.restart_once(["host-control", "restart"])
    assert calls == [("host-control", "restart")]
    assert "protected" not in str(failure.value)
    assert failure.value.effect == "unknown"


def test_restart_command_file_requires_literal_argv(controller, tmp_path):
    path = tmp_path / "restart.json"
    path.write_text(json.dumps(["gcloud", "compute", "ssh", "owned-host"]))
    assert controller.restart_argv(path) == ["gcloud", "compute", "ssh", "owned-host"]
    for invalid in ["shell text", [], [""], ["command", 42]]:
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError):
            controller.restart_argv(path)


@pytest.mark.asyncio
async def test_expiry_wait_observes_automatic_stop_without_triggering_cleanup(
    controller, monkeypatch
):
    calls, checks = [], []
    phases = iter(["idle", "stopped"])

    async def request(method, path, *args, **kwargs):
        calls.append((method, path))
        if method == "GET":
            return {"computers": [{"computer_id": "own", "phase": next(phases)}]}
        return {}

    async def tool(*args, **kwargs):
        return {"error": {"code": "lease_expired", "effect": "none"}}

    async def sleep(_seconds):
        pass

    monkeypatch.setattr(controller.asyncio, "sleep", sleep)
    runner = controller.OperatorQualification.__new__(controller.OperatorQualification)
    runner.api = SimpleNamespace(request=request)
    runner.tool = tool
    runner.args = SimpleNamespace(idle_wait_seconds=10)
    runner.evidence = SimpleNamespace(check=lambda *args, **kwargs: checks.append((args, kwargs)))
    await runner.automatic_idle_stop({"binding": {"computer_id": "own", "token": "test-token"}})
    assert calls == [
        ("POST", "/v1/lease/renew"),
        ("GET", "/v1/computers"),
        ("GET", "/v1/computers"),
    ]
    assert checks[0][1]["explicit_stop_or_reap"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["leased", "preparing", "idle", "faulted"])
async def test_exclusive_restart_run_refuses_existing_unstopped_computer(controller, phase):
    async def request(*_args):
        return {"computers": [{"computer_id": "another", "phase": phase}]}

    runner = controller.OperatorQualification.__new__(controller.OperatorQualification)
    runner.api = SimpleNamespace(request=request)
    with pytest.raises(controller.QualificationFailure, match="no other active computers"):
        await runner.run()


@pytest.mark.asyncio
async def test_uncertain_ui_save_cleanup_recovers_only_exact_run_assignment(
    controller, monkeypatch
):
    owner = {
        "kind": "identity",
        "realm": "qualification",
        "namespace": "run",
        "identity": "guest-0",
    }
    credential = {
        "id": "owned",
        "label": "Operator qualification abc",
        "realm": "qualification",
        "assignments": [owner],
    }

    async def request(*_args):
        return {
            "credentials": [
                credential,
                {**credential, "id": "another-realm", "realm": "someone-else"},
                {
                    **credential,
                    "id": "another-run",
                    "assignments": [{**owner, "namespace": "other"}],
                },
            ]
        }

    async def parent_cleanup(_self):
        pass

    monkeypatch.setattr(controller.LiveQualification, "cleanup", parent_cleanup)
    runner = controller.OperatorQualification.__new__(controller.OperatorQualification)
    runner.api = SimpleNamespace(request=request)
    runner.evidence = SimpleNamespace(run_id="abc")
    runner.credentials = []
    runner.guests = [{"owner": owner}]
    await runner.cleanup()
    assert runner.credentials == ["owned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation_unknown", [False, True])
async def test_restart_privacy_does_not_require_ephemeral_fixture_or_replay_navigation(
    controller, monkeypatch, navigation_unknown
):
    """A short-idle VM can stop while the broker restarts; its server then disappears."""
    guest = {
        "binding": {"computer_id": "owned", "token": "old-test-grant"},
        "origin": "http://127.0.0.1:18777",
    }
    before = guest["binding"]
    calls, checks = [], []
    fixture_running = True

    async def restart(_argv):
        nonlocal fixture_running
        fixture_running = False

    async def attach(current, generation):
        assert current is guest and generation == 2
        current["binding"] = {"computer_id": "owned", "token": "new-test-grant"}

    async def tool(current, name, arguments):
        assert current is guest
        calls.append((name, arguments))
        if name == "credentials_request":
            value = {"id": "pending"}
        elif name == "credentials_use":
            value = {}
        elif name == "credentials_list":
            value = {"credentials": [{"id": "saved"}]}
        elif name == "browser_navigate":
            if (arguments["url"] == guest["origin"] and not fixture_running) or navigation_unknown:
                raise controller.QualificationFailure(
                    "Navigation outcome unknown", effect="unknown"
                )
            value = {"observation_id": "safe-view"}
        else:
            raise AssertionError(name)
        return {"content": [{"type": "text", "text": json.dumps(value)}]}

    old_grant = AsyncMock(return_value={"error": {"code": "unauthorized", "effect": "none"}})
    requests = AsyncMock(return_value={"requests": [{"id": "pending", "phase": "cancelled"}]})
    runner = controller.OperatorQualification.__new__(controller.OperatorQualification)
    runner.api = SimpleNamespace(
        client=SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200, json=lambda: {"service": "meerkat-computer"}
                )
            )
        ),
        tool=old_grant,
        request=requests,
    )
    runner.args = SimpleNamespace(restart_command_file="unused", broker_url="http://broker.test")
    runner.evidence = SimpleNamespace(check=lambda *args, **kwargs: checks.append((args, kwargs)))
    runner.login_target = AsyncMock(return_value=({"secret_ref": "field"}, "submit"))
    runner.tool = tool
    runner.attach = attach
    runner.guarded = AsyncMock()
    runner.capture_reused = AsyncMock()
    restart_mock = AsyncMock(side_effect=restart)
    monkeypatch.setattr(controller, "restart_argv", lambda _path: ["restart-owned-broker"])
    monkeypatch.setattr(controller, "restart_once", restart_mock)

    if navigation_unknown:
        with pytest.raises(controller.QualificationFailure) as failure:
            await runner.restart_privacy(guest, {"id": "saved"})
        assert failure.value.effect == "unknown"
        assert checks == []
        runner.capture_reused.assert_not_awaited()
    else:
        await runner.restart_privacy(guest, {"id": "saved"})
        runner.capture_reused.assert_awaited_once()
        assert checks == [
            (
                ("broker_restart_identity_vault_pending_input_privacy",),
                {
                    "safe_navigation_url": "about:blank",
                    "fixture_process_continuity_asserted": False,
                },
            )
        ]
    assert [call for call in calls if call[0] == "browser_navigate"] == [
        ("browser_navigate", {"url": "about:blank"})
    ]
    assert runner.guarded.await_count == 2
    old_grant.assert_awaited_once_with(before, "computer_observe", {}, allow_error=True)
    requests.assert_awaited_once_with("GET", "/v1/credential-requests")
    restart_mock.assert_awaited_once()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_preview_evidence_keeps_full_page_and_exact_active_card(controller, tmp_path):
    """The useful derivative must be the active namespace, even with long retained history."""
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7ioAAAAASUVORK5CYII="
    html = f"""<!doctype html><html><body>
      <label><input type="checkbox" checked onchange="document.querySelectorAll('.labels')
        .forEach(label => label.hidden = !this.checked)">Show labels</label>
      <button onclick="document.querySelector('[role=status]').textContent='Checking saved captures'">Refresh</button>
      <button>Lock</button><p role="status">Ready</p>
      <article class="preview-card" style="height:2400px">Other retained identity</article>
      <article class="preview-card" style="width:240px;height:180px;background:green">
        <div class="labels">current-run</div>
        <img alt="Agent screenshot" src="data:image/png;base64,{png}" width="200" height="140">
      </article></body></html>"""
    runner = controller.OperatorQualification.__new__(controller.OperatorQualification)
    runner.namespace = "current-run"
    runner.evidence = SimpleNamespace(directory=tmp_path, check=lambda *_args, **_kwargs: None)
    runner.tool = AsyncMock(return_value={})
    runner.capture_reused = AsyncMock()

    async with controller.async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            page.set_default_timeout(5000)

            async def route(request):
                await request.fulfill(content_type="text/html", body=html)

            await page.route("http://operator.test/**", route)

            async def unlock(current, path, _button):
                await current.goto("http://operator.test" + path)

            runner.unlock = unlock
            await runner.preview_page({}, page)
        finally:
            await browser.close()

    full = tmp_path / "operator-preview-labels-off.png"
    card = tmp_path / "operator-preview-active-card-labels-off.png"
    assert full.is_file()
    assert card.is_file()
    assert struct.unpack(">II", full.read_bytes()[16:24])[1] > 2400
    assert struct.unpack(">II", card.read_bytes()[16:24]) == (240, 180)
    runner.tool.assert_awaited_once_with({}, "computer_observe", {"surface": "browser"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic",
    [
        {
            "code": "backend_failure",
            "effect": "unknown",
            "backend_failure": {"category": "admission_denied"},
        },
        {
            "code": "backend_failure",
            "effect": "unknown",
            "backend_failure": {"category": "command_failed"},
        },
        {
            "code": "unauthorized",
            "effect": "unknown",
            "backend_failure": {"category": "admission_denied"},
        },
        {
            "code": "backend_failure",
            "effect": "none",
            "backend_failure": {"category": "admission_denied"},
        },
        {"code": "backend_failure", "effect": "unknown"},
    ],
)
async def test_admission_uses_closed_diagnostic_and_rejects_message_only(controller, diagnostic):
    valid = diagnostic == {
        "code": "backend_failure",
        "effect": "unknown",
        "backend_failure": {"category": "admission_denied"},
    }
    denied = {
        "error": {
            **diagnostic,
            "message": "backend operation failed; inspect operator diagnostics"
            if valid
            else "admission denied",
        }
    }
    calls, checks = [], []
    guest = {"binding": {"computer_id": "first", "token": "first-test-grant"}}
    second = {"binding": {"computer_id": "second", "token": "second-test-grant"}}

    async def request(method, path, *args, **kwargs):
        calls.append((method, path))
        return denied if path == "/v1/attachments" else {}

    runner = controller.OperatorQualification.__new__(controller.OperatorQualification)
    runner.guests = [guest]
    runner.args = SimpleNamespace(profile="short-idle")
    runner.api = SimpleNamespace(request=request)
    runner.evidence = SimpleNamespace(
        run_id="current-run", check=lambda *args, **kwargs: checks.append((args, kwargs))
    )
    runner.owner = lambda index: {"identity": "guest-" + str(index)}
    runner.attach = AsyncMock()
    runner.prepare = AsyncMock(return_value=second)
    if valid:
        await runner.admission(guest)
        assert calls == [
            ("POST", "/v1/attachments"),
            ("POST", "/v1/lease/release"),
            ("POST", "/v1/computers/second/stop"),
        ]
        assert runner.attach.await_count == 2
        assert checks[0][0] == ("live_pool_admission_limit",)
    else:
        with pytest.raises(controller.QualificationFailure, match="pool admission"):
            await runner.admission(guest)
        assert calls == [("POST", "/v1/attachments")]
        assert runner.attach.await_count == 1
        assert checks == []
