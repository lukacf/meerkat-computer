import asyncio
import json

import httpx
import pytest
from meerkat_computer.browser import text_block
from meerkat_computer.protocol import Call, Lease, WorkerError
from meerkat_computer.worker import Worker, create_app, load_token

TOKEN = "test-worker-token-" + "0" * 32


class FakeBrowser:
    def __init__(self):
        self.calls = 0
        self.invalidations = 0
        self.failure = None
        self.configured = None

    async def start(self):
        pass

    async def close(self):
        pass

    def ready(self):
        return True

    async def configure(self, lease):
        self.configured = lease

    async def invalidate(self):
        self.invalidations += 1

    async def validate(self, name, arguments):
        pass

    async def perform(self, name, arguments):
        self.calls += 1
        if self.failure:
            raise self.failure
        return [text_block({"fake_observation": self.calls})], {"observations": 1}


def lease(epoch=1, lease_id="lease-a", capabilities=None, **kwargs):
    return Lease(
        lease_id=lease_id,
        epoch=epoch,
        expires_at_ms=2_000_000,
        capabilities=capabilities
        or ["browser", "screenshots", "browser_semantics", "exec", "files"],
        observation={"mode": "hybrid", "width": 640, "height": 480},
        **kwargs,
    )


def call(name="computer_observe", arguments=None, action_id="a", epoch=1, lease_id="lease-a"):
    return Call(
        lease_id=lease_id, epoch=epoch, action_id=action_id, name=name, arguments=arguments or {}
    )


@pytest.fixture
async def worker(tmp_path):
    worker = Worker(
        tmp_path / "state", tmp_path / "workspace", TOKEN, FakeBrowser(), clock=lambda: 1000
    )
    await worker.start()
    yield worker
    await worker.close()


async def test_http_auth_and_validation_do_not_echo_secrets(worker):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(worker)), base_url="http://worker"
    ) as client:
        assert (await client.get("/health")).json() == {
            "protocol_version": 1,
            "ready": True,
            "surfaces": ["browser"],
        }
        response = await client.post("/v1/lease", json=lease().model_dump())
        assert response.status_code == 401
        bad = {"secret": "private-token-content"}
        response = await client.post(
            "/v1/call", json=bad, headers={"Authorization": "Bearer " + TOKEN}
        )
        assert response.status_code == 422
        assert "private-token-content" not in response.text
        response = await client.post(
            "/v1/lease", json=lease().model_dump(), headers={"Authorization": "Bearer " + TOKEN}
        )
        assert response.status_code == 200


async def test_epoch_fence_and_renewal(worker):
    await worker.grant(lease())
    await worker.grant(lease())
    with pytest.raises(WorkerError, match="fenced"):
        await worker.grant(lease(lease_id="other"))
    await worker.grant(lease(epoch=2, lease_id="new"))
    with pytest.raises(WorkerError, match="fenced"):
        await worker.grant(lease())
    with pytest.raises(WorkerError, match="not current"):
        await worker.call(call())


async def test_renewal_cannot_change_capabilities(worker):
    await worker.grant(lease())
    changed = lease().model_copy(
        update={"capabilities": ["browser", "screenshots", "browser_semantics"]}
    )
    with pytest.raises(WorkerError, match="cannot change"):
        await worker.grant(changed)


async def test_failed_configuration_fences_old_lease_and_blocks_new_calls(worker):
    await worker.grant(lease())
    configure = worker.browser.configure

    async def fail(lease):
        raise RuntimeError("private browser details")

    worker.browser.configure = fail
    with pytest.raises(WorkerError, match="configuration failed"):
        await worker.grant(lease(epoch=2))
    with pytest.raises(WorkerError, match="not current"):
        await worker.call(call())
    with pytest.raises(WorkerError, match="not applied"):
        await worker.call(call(epoch=2))
    worker.browser.configure = configure
    await worker.grant(lease(epoch=2))
    assert (await worker.call(call(epoch=2)))[0] == 200


async def test_expired_lease_rejected_before_action(worker):
    await worker.grant(lease())
    worker.clock = lambda: 3000
    with pytest.raises(WorkerError, match="expired"):
        await worker.call(call())
    assert worker.browser.calls == 0


async def test_revocation_persists_fence_and_disables_calls(worker):
    await worker.grant(lease())
    revoked = lease(epoch=2, lease_id="revoked").model_copy(
        update={"expires_at_ms": 0, "capabilities": []}
    )
    await worker.grant(revoked)
    with pytest.raises(WorkerError, match="expired"):
        await worker.call(call(epoch=2, lease_id="revoked"))
    with pytest.raises(WorkerError, match="not current"):
        await worker.call(call())
    with pytest.raises(WorkerError, match="fenced"):
        await worker.grant(lease())


async def test_duplicate_returns_durable_receipt_without_reexecuting(worker):
    await worker.grant(lease())
    first = await worker.call(call())
    assert await worker.call(call()) == first
    assert worker.browser.calls == 1
    with pytest.raises(WorkerError, match="different arguments"):
        await worker.call(call(name="browser_navigate", arguments={"url": "https://example.test"}))


async def test_failure_after_effect_admission_is_unknown_and_never_replayed(worker):
    await worker.grant(lease())
    worker.browser.failure = RuntimeError("sensitive browser message")
    request = call("browser_navigate", {"url": "https://example.test"})
    status, body = await worker.call(request)
    assert status == 409 and body["error"]["effect"] == "unknown"
    assert "sensitive" not in json.dumps(body)
    assert await worker.call(request) == (status, body)
    assert worker.browser.calls == 1


async def test_read_failure_has_no_effect(worker):
    await worker.grant(lease())
    worker.browser.failure = RuntimeError("private content")
    status, body = await worker.call(call())
    assert status == 500 and body["error"]["effect"] == "none"


async def test_schema_and_capability_checks_happen_before_admission(worker):
    await worker.grant(lease(capabilities=["browser", "screenshots", "browser_semantics"]))
    with pytest.raises(WorkerError) as denied:
        await worker.call(call("workspace_exec", {"command": "true"}))
    assert denied.value.status == 403
    with pytest.raises(WorkerError) as invalid:
        await worker.call(call(arguments={"owner": "different-agent"}))
    assert invalid.value.status == 422
    assert worker.journal.metrics() == {}


async def test_restart_keeps_fence_receipts_and_unknown_admissions(tmp_path):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    worker = Worker(state, workspace, TOKEN, FakeBrowser(), clock=lambda: 1000)
    await worker.start()
    await worker.grant(lease(epoch=4))
    saved = await worker.call(call(epoch=4))
    worker.journal.admit(call(action_id="pending", epoch=4))
    await worker.close()
    restarted = Worker(state, workspace, TOKEN, FakeBrowser(), clock=lambda: 1000)
    await restarted.start()
    try:
        assert await restarted.call(call(epoch=4)) == saved
        with pytest.raises(WorkerError) as unknown:
            await restarted.call(call(action_id="pending", epoch=4))
        assert unknown.value.effect == "unknown"
        with pytest.raises(WorkerError, match="fenced"):
            await restarted.grant(lease(epoch=3))
        assert restarted.browser.calls == 0
    finally:
        await restarted.close()


async def test_single_process_owns_worker_journal(worker, tmp_path):
    with pytest.raises(RuntimeError, match="Another worker"):
        Worker(tmp_path / "state", tmp_path / "workspace", TOKEN, FakeBrowser())


async def test_cancellation_leaves_nonreplayable_admission(worker):
    await worker.grant(lease())
    worker.browser.failure = asyncio.CancelledError()
    request = call("browser_navigate", {"url": "https://example.test"})
    with pytest.raises(asyncio.CancelledError):
        await worker.call(request)
    with pytest.raises(WorkerError) as unknown:
        await worker.call(request)
    assert unknown.value.effect == "unknown"


async def test_workspace_paths_outputs_and_environment(worker, tmp_path, monkeypatch):
    await worker.grant(lease())
    outside = tmp_path / "outside"
    outside.write_text("private")
    (worker.workspace.root / "escape").symlink_to(outside)
    for index, path in enumerate(["../outside", str(outside), "escape"]):
        with pytest.raises(WorkerError):
            await worker.call(call("workspace_read", {"path": path}, action_id=f"bad{index}"))
    status, _ = await worker.call(
        call("workspace_write", {"path": "hello.txt", "text": "hello"}, action_id="write")
    )
    assert status == 200
    _, result = await worker.call(call("workspace_read", {"path": "hello.txt"}, action_id="read"))
    assert json.loads(result["content"][0]["text"])["text"] == "hello"
    monkeypatch.setenv("MEERKAT_COMPUTER_WORKER_TOKEN", "should-not-be-inherited")
    _, result = await worker.call(
        call(
            "workspace_exec",
            {"command": "printf '%s' \"$MEERKAT_COMPUTER_WORKER_TOKEN\""},
            action_id="env",
        )
    )
    assert json.loads(result["content"][0]["text"])["stdout"] == ""
    _, result = await worker.call(
        call(
            "workspace_exec",
            {"command": "head -c 100000 /dev/zero", "timeout_seconds": 2},
            action_id="large",
        )
    )
    output = json.loads(result["content"][0]["text"])
    assert output["truncated"] is True and len(output["stdout"]) == 65536


async def test_exec_timeout_is_explicit_and_stops_process_group(worker):
    await worker.grant(lease())
    _, result = await worker.call(
        call("workspace_exec", {"command": "sleep 30", "timeout_seconds": 1})
    )
    output = json.loads(result["content"][0]["text"])
    assert output["timed_out"] is True and output["exit_code"] != 0


def test_private_token_file(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    monkeypatch.setenv("MEERKAT_COMPUTER_WORKER_TOKEN_FILE", str(path))
    with pytest.raises(ValueError, match="private"):
        load_token()
    path.chmod(0o600)
    assert load_token() == TOKEN
