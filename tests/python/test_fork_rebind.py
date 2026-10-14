"""Full-state clones retain applications while receiving independent authority."""

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from meerkat_computer.protocol import Call, Lease, WorkerError
from meerkat_computer.secure_input import create_secure_app
from meerkat_computer.worker import Worker, create_app, load_token

OLD_TOKEN = "synthetic-parent-token-" + "p" * 32
NEW_TOKEN = "synthetic-child-token-" + "c" * 32


class LiveSurface:
    def __init__(self):
        self.starts = self.closes = self.calls = 0
        self.unsaved_document = {"text": "an unsaved draft", "revision": 17}
        self.observation_id = "parent-observation"
        self.refs = {"parent-ref": object()}
        self.operation_metrics = {"ui_commands": 9}
        self.lease = None

    async def start(self):
        self.starts += 1

    async def close(self):
        self.closes += 1

    def ready(self):
        return self.starts == 1 and self.closes == 0

    async def configure(self, lease):
        self.lease = lease

    async def invalidate(self):
        self.observation_id = ""
        self.refs = {}

    async def validate(self, name, arguments):
        pass

    async def perform(self, name, arguments):
        self.calls += 1
        return [{"type": "text", "text": str(self.calls)}], {"observations": 1}


class PrivatePage:
    def is_closed(self):
        return False

    async def evaluate(self, *_):
        return True


def lease(lease_id="parent-lease", epoch=42):
    return Lease(
        lease_id=lease_id,
        epoch=epoch,
        expires_at_ms=9_000_000_000_000,
        capabilities=["browser", "screenshots", "browser_semantics", "exec", "files"],
        observation={"mode": "hybrid", "width": 640, "height": 480},
    )


def call(lease_id="parent-lease", epoch=42, action_id="same-action"):
    return Call(
        lease_id=lease_id,
        epoch=epoch,
        action_id=action_id,
        name="computer_observe",
        arguments={},
    )


def rebind(**changes):
    return {
        "operation": "fork_rebind",
        "checkpoint_id": "checkpoint-one",
        "fork_id": "fork-one",
        "computer_id": "child-computer",
        "incarnation_id": "child-incarnation",
        "worker_token": NEW_TOKEN,
        **changes,
    }


async def control(worker, operation="fork_prepare", **changes):
    request = {"operation": operation, "checkpoint_id": "checkpoint-one", **changes}
    if operation == "fork_capabilities":
        request.pop("checkpoint_id")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_secure_app(worker)),
        base_url="http://guest-local",
    ) as client:
        return await client.post("/v1/fork-control", json=request)


@pytest.fixture
async def worker(tmp_path):
    token_file = tmp_path / "worker.token"
    token_file.write_text(OLD_TOKEN)
    token_file.chmod(0o600)
    worker = Worker(
        tmp_path / "state", tmp_path / "workspace", OLD_TOKEN,
        LiveSurface(), desktop=LiveSurface(),
    )
    worker.token_file = token_file
    await worker.start()
    await worker.grant(lease())
    yield worker
    await worker.close()


async def test_fork_control_only_exists_on_private_socket_and_redacts_bad_input(worker):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(worker)), base_url="http://worker"
    ) as client:
        response = await client.post(
            "/v1/fork-control", json=rebind(),
            headers={"Authorization": "Bearer " + OLD_TOKEN},
        )
        assert response.status_code == 404
    response = await control(worker, "fork_capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": 1, "full_state_fork": True, "prepared_checkpoint_id": None,
    }
    response = await control(worker, **rebind(worker_token="secret-too-short", bad=NEW_TOKEN))
    assert response.status_code == 422
    assert "secret-too-short" not in response.text and NEW_TOKEN not in response.text
    assert worker.journal.lease() == lease()


async def test_prepared_checkpoint_blocks_all_effect_channels_until_exact_resume(worker):
    first = await control(worker)
    assert first.status_code == 200
    assert (await control(worker)).json() == first.json()
    assert (await control(worker, "fork_capabilities")).json()["prepared_checkpoint_id"] == (
        "checkpoint-one"
    )
    for operation, argument in ((worker.grant, lease()), (worker.call, call())):
        with pytest.raises(WorkerError) as blocked:
            await operation(argument)
        assert blocked.value.code == "fork_prepared"
    from meerkat_computer.secure_input import SecureInputEnvelope

    private_request = SecureInputEnvelope.model_validate({
        "lease": lease().model_dump(), "action_id": "private-action",
        "target": {"observation_id": "old", "origin": "https://example.test", "secret_ref": "x"},
        "values": {"secret": "a-synthetic-secret"},
    })
    with pytest.raises(WorkerError) as blocked:
        await worker.secure_input(private_request)
    assert blocked.value.code == "fork_prepared"
    assert (await control(worker, "fork_resume", checkpoint_id="wrong")).status_code == 409
    assert (await control(worker, "fork_resume")).status_code == 200
    assert (await control(worker, "fork_resume")).status_code == 200
    assert (await worker.call(call()))[0] == 200
    assert worker.browser.starts == 1 and worker.browser.closes == 0
    assert (await control(worker)).status_code == 409  # Closed IDs cannot prepare again.


async def test_prepare_waits_for_completed_action_and_rejects_unresolved_admission(worker):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = worker.browser.perform

    async def slow(*args):
        entered.set()
        await finish.wait()
        return await original(*args)

    worker.browser.perform = slow
    running = asyncio.create_task(worker.call(call()))
    await asyncio.wait_for(entered.wait(), 5)
    prepare = asyncio.create_task(control(worker))
    try:
        await asyncio.sleep(0)
        assert not prepare.done()
    finally:
        finish.set()
    assert (await running)[0] == 200
    assert (await prepare).status_code == 200
    assert (await control(worker, "fork_resume")).status_code == 200
    worker.journal.admit(call(action_id="unresolved"))
    response = await control(worker, checkpoint_id="checkpoint-two")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "fork_action_unresolved"


async def test_rebind_preserves_live_objects_and_guard_but_clears_inherited_authority(worker):
    await worker.call(call())
    browser, desktop, computer, secure = (
        worker.browser, worker.computer.desktop, worker.computer, worker.secure,
    )
    document = browser.unsaved_document
    # The page marker and persistent privacy bit must survive the authority reset.
    secure.active, secure.page, secure.marker = True, PrivatePage(), "private-page-marker"
    with worker.journal.db:
        worker.journal.db.execute("INSERT INTO secure_input_privacy VALUES (1)")
    assert (await control(worker)).status_code == 200
    result = await control(worker, **rebind())
    assert result.status_code == 200, result.text
    assert result.json() == {
        "protocol_version": 1, "operation": "fork_rebind", "checkpoint_id": "checkpoint-one",
        "fork_id": "fork-one", "computer_id": "child-computer",
        "incarnation_id": "child-incarnation", "rebound": True,
    }
    assert worker.browser is browser and worker.computer is computer and worker.secure is secure
    assert worker.computer.desktop is desktop and browser.unsaved_document is document
    assert document == {"text": "an unsaved draft", "revision": 17}
    assert browser.starts == desktop.starts == 1 and browser.closes == desktop.closes == 0
    assert secure.active and secure.marker == "private-page-marker"
    assert worker.journal.db.execute("SELECT * FROM secure_input_privacy").fetchone() == (1,)
    assert worker.journal.lease() is None and worker.applied_lease is None
    assert worker.journal.receipt(call()) is None and worker.journal.metrics() == {}
    assert browser.refs == desktop.refs == {}
    assert browser.observation_id == desktop.observation_id == ""
    assert browser.lease is desktop.lease is computer.lease is None
    assert computer.active is None and computer.operation_metrics == {}
    assert browser.operation_metrics == desktop.operation_metrics == {}
    assert worker.token_file.read_text() == NEW_TOKEN
    assert stat.S_IMODE(worker.token_file.stat().st_mode) == 0o600
    assert NEW_TOKEN not in result.text
    dump = "\n".join(worker.journal.db.iterdump())
    assert OLD_TOKEN not in dump and NEW_TOKEN not in dump
    with pytest.raises(WorkerError) as denied:
        worker.authorize("Bearer " + OLD_TOKEN)
    assert denied.value.code == "unauthorized"
    worker.authorize("Bearer " + NEW_TOKEN)
    with pytest.raises(WorkerError) as denied:
        await worker.call(call())
    assert denied.value.code == "stale_lease"
    # Native and browser objects still live after the fresh child grant.
    await worker.grant(lease("child-lease", 1))
    assert browser.unsaved_document is document and secure.active
    with pytest.raises(WorkerError) as stale:
        await worker.call(Call(
            lease_id="child-lease", epoch=1, action_id="old-observation",
            name="computer_act",
            arguments={"observation_id": "parent-observation", "action": {"type": "key", "key": "a"}},
        ))
    assert stale.value.code == "stale_observation"


async def test_exact_rebind_replay_never_resets_child_progress_and_conflict_is_fenced(worker):
    await worker.call(call())
    assert (await control(worker)).status_code == 200
    first = await control(worker, **rebind())
    assert first.status_code == 200
    await worker.grant(lease("child-lease", 1))
    child = await worker.call(call("child-lease", 1))
    assert child[1]["content"][0]["text"] == "2"  # Not the parent's saved receipt.
    before_metrics = worker.journal.metrics()
    replay = await control(worker, **rebind())
    assert replay.status_code == 200 and replay.json() == first.json()
    assert worker.journal.lease() == lease("child-lease", 1)
    assert worker.journal.metrics() == before_metrics
    assert await worker.call(call("child-lease", 1)) == child
    for changes in ({"worker_token": OLD_TOKEN}, {"computer_id": "other-child"}):
        response = await control(worker, **rebind(**changes))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "fork_rebind_conflict"
    assert (await control(worker, "fork_resume")).status_code == 409


async def test_rotation_failure_leaves_checkpoint_gated_and_exact_retry_recovers(worker, monkeypatch):
    assert (await control(worker)).status_code == 200
    original = worker.persist_token

    def fail(_token):
        raise OSError("private diagnostics containing " + NEW_TOKEN)

    monkeypatch.setattr(worker, "persist_token", fail)
    response = await control(worker, **rebind())
    assert response.status_code == 503
    assert NEW_TOKEN not in response.text
    assert response.json()["error"]["effect"] == "unknown"
    with pytest.raises(WorkerError) as denied:
        await worker.grant(lease("child-lease", 1))
    assert denied.value.code == "fork_prepared"
    assert (await control(worker, "fork_resume")).status_code == 409
    monkeypatch.setattr(worker, "persist_token", original)
    assert (await control(worker, **rebind())).status_code == 200
    await worker.grant(lease("child-lease", 1))
    assert (await worker.call(call("child-lease", 1)))[0] == 200


@pytest.mark.parametrize("failure_point", ["replace", "directory_sync", "journal_commit"])
async def test_rebind_partial_publication_never_restores_parent_control(worker, monkeypatch, failure_point):
    assert (await control(worker)).status_code == 200
    with monkeypatch.context() as patch:
        def failure(*_args):
            raise OSError("private token publication failed: " + NEW_TOKEN)

        if failure_point == "replace":
            patch.setattr("meerkat_computer.worker.os.replace", failure)
        elif failure_point == "directory_sync":
            original = os.fsync

            def fail_directory(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    failure()
                return original(descriptor)

            patch.setattr("meerkat_computer.worker.os.fsync", fail_directory)
        else:
            patch.setattr(worker.journal, "finish_rebind", failure)
        response = await control(worker, **rebind())
    assert response.status_code == 503 and NEW_TOKEN not in response.text
    expected = OLD_TOKEN if failure_point == "replace" else NEW_TOKEN
    assert worker.token_file.read_text() == expected
    assert list(worker.token_file.parent.glob(".worker-token-*")) == []
    assert worker.journal.lease() is None
    for method, argument in ((worker.grant, lease()), (worker.call, call())):
        with pytest.raises(WorkerError) as blocked:
            await method(argument)
        assert blocked.value.code == "fork_prepared"
    assert (await control(worker, "fork_resume")).status_code == 409
    assert (await control(worker, **rebind())).status_code == 200
    await worker.grant(lease("child-lease", 1))
    assert (await worker.call(call("child-lease", 1)))[0] == 200


async def test_old_resume_and_rebind_replays_cannot_reopen_a_later_checkpoint(worker):
    assert (await control(worker)).status_code == 200
    assert (await control(worker, "fork_resume")).status_code == 200
    assert (await control(worker, checkpoint_id="checkpoint-two")).status_code == 200
    assert (await control(worker, "fork_resume")).status_code == 200  # Replay does nothing.
    with pytest.raises(WorkerError) as blocked:
        await worker.call(call())
    assert blocked.value.code == "fork_prepared"
    first_child = rebind(checkpoint_id="checkpoint-two")
    assert (await control(worker, **first_child)).status_code == 200
    await worker.grant(lease("child-lease", 1))
    assert (await control(worker, checkpoint_id="checkpoint-three")).status_code == 200
    grandchild = rebind(
        checkpoint_id="checkpoint-three", fork_id="fork-two", computer_id="grandchild",
        incarnation_id="grandchild-incarnation", worker_token="grandchild-token-" + "g" * 32,
    )
    assert (await control(worker, **grandchild)).status_code == 200
    assert (await control(worker, **first_child)).status_code == 409
    assert (await control(worker, "fork_resume", checkpoint_id="checkpoint-three")).status_code == 409
    assert worker.journal.lease() is None
    with pytest.raises(WorkerError):
        worker.authorize("Bearer " + NEW_TOKEN)


@pytest.mark.parametrize("kind", ["absent", "symlink", "public"])
async def test_fork_refuses_nonprivate_or_nonpersistent_token_files_without_gating(worker, kind):
    token_file = worker.token_file
    if kind == "absent":
        worker.token_file = None
    elif kind == "symlink":
        link = token_file.with_suffix(".link")
        link.symlink_to(token_file)
        worker.token_file = link
    else:
        token_file.chmod(0o644)
    response = await control(worker)
    assert response.status_code == 503
    assert worker.journal.prepared_checkpoint() is None
    assert worker.journal.lease() == lease()
    assert (await worker.call(call()))[0] == 200


async def test_queued_http_request_rechecks_authorization_after_waiting_on_effect_lock(worker):
    authorized = asyncio.Event()
    original = worker.authorize

    def observed_authorize(value):
        original(value)
        authorized.set()

    worker.authorize = observed_authorize
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(worker)), base_url="http://worker"
    ) as client:
        async with worker.lock:
            request = asyncio.create_task(client.post(
                "/v1/lease", json=lease().model_dump(),
                headers={"Authorization": "Bearer " + OLD_TOKEN},
            ))
            await authorized.wait()
            worker.token = NEW_TOKEN  # Rotation completes before this queued request owns the lock.
        response = await request
        assert response.status_code == 401


async def test_fork_gate_and_rebind_survive_worker_restart_without_reusing_parent_receipts(tmp_path, monkeypatch):
    state, workspace, token_file = tmp_path / "state", tmp_path / "workspace", tmp_path / "token"
    token_file.write_text(OLD_TOKEN)
    token_file.chmod(0o600)
    monkeypatch.setenv("MEERKAT_COMPUTER_WORKER_TOKEN_FILE", str(token_file))
    first = Worker(state, workspace, load_token(), LiveSurface())
    await first.start()
    await first.grant(lease())
    await first.call(call())
    assert (await control(first)).status_code == 200
    await first.close()
    restarted = Worker(state, workspace, load_token(), LiveSurface())
    await restarted.start()
    try:
        with pytest.raises(WorkerError) as blocked:
            await restarted.call(call())
        assert blocked.value.code == "fork_prepared"
        assert (await control(restarted, **rebind())).status_code == 200
    finally:
        await restarted.close()
    assert load_token() == NEW_TOKEN
    child = Worker(state, workspace, load_token(), LiveSurface())
    await child.start()
    try:
        assert child.journal.lease() is None
        assert child.journal.receipt(call()) is None
        assert (await control(child, **rebind())).status_code == 200
        await child.grant(lease("child-lease", 1))
        assert (await child.call(call("child-lease", 1)))[0] == 200
    finally:
        await child.close()


async def test_private_pipe_transport_never_needs_tokens_in_process_arguments():
    with tempfile.TemporaryDirectory(prefix="mc-fork-", dir="/tmp") as temporary:
        directory = Path(temporary)
        token_file = directory / "worker.token"
        token_file.write_text(OLD_TOKEN)
        token_file.chmod(0o600)
        worker = Worker(
            directory / "state", directory / "workspace", OLD_TOKEN, LiveSurface(),
            token_file=token_file,
        )
        app = create_app(worker)
        async with app.router.lifespan_context(app):
            helper = Path(__file__).parents[2] / "python/meerkat_computer/private_client.py"
            for request, field in (
                ({"operation": "fork_capabilities"}, "full_state_fork"),
                ({"operation": "fork_prepare", "checkpoint_id": "checkpoint-one"}, "prepared"),
                (rebind(), "rebound"),
            ):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(helper), str(worker.state_dir / "secure-input.sock"),
                    "fork_control", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await process.communicate(json.dumps(request).encode())
                assert process.returncode == 0 and error == b""
                assert json.loads(output)[field] is True
                assert OLD_TOKEN.encode() not in output and NEW_TOKEN.encode() not in output
            assert worker.browser.starts == 1 and worker.browser.closes == 0
            assert worker.token == token_file.read_text() == NEW_TOKEN


@pytest.mark.browser
async def test_real_chromium_hot_rebind_retains_js_unsaved_dom_and_private_page(tmp_path):
    token_file = tmp_path / "worker.token"
    token_file.write_text(OLD_TOKEN)
    token_file.chmod(0o600)
    worker = Worker(tmp_path / "state", tmp_path / "workspace", OLD_TOKEN)
    worker.token_file = token_file
    await worker.start()
    try:
        await worker.grant(lease())
        page, context, driver = worker.browser.page, worker.browser.context, worker.browser.playwright
        await page.set_content('<textarea aria-label="Draft"></textarea><button>Keep working</button>')
        await page.locator("textarea").fill("unsaved application state")
        await page.evaluate("window.inMemoryOnly = {revision: 73, values: [2, 3, 5]}")
        await worker.call(call())
        await worker.secure.protect()
        guard, marker = worker.secure, worker.secure.marker
        assert (await control(worker)).status_code == 200
        response = await control(worker, **rebind())
        assert response.status_code == 200, response.text
        assert worker.browser.page is page and worker.browser.context is context
        assert worker.browser.playwright is driver
        assert await page.evaluate("window.inMemoryOnly") == {"revision": 73, "values": [2, 3, 5]}
        assert await page.locator("textarea").input_value() == "unsaved application state"
        assert worker.secure is guard and guard.marker == marker and await guard.guarded()
        await worker.grant(lease("child-lease", 1))
        observed = await worker.call(call("child-lease", 1))
        assert observed[0] == 200
        body = json.loads(observed[1]["content"][0]["text"])
        assert body["privacy"]["active"] and body["action_refs"] == []
        assert "unsaved application state" not in json.dumps(observed)
        assert await page.evaluate("window.inMemoryOnly.revision") == 73
        await page.evaluate("window.inMemoryOnly.revision++")
        assert await page.evaluate("window.inMemoryOnly.revision") == 74
        assert len(context.pages) == 1 and not page.is_closed()
    finally:
        await worker.close()
