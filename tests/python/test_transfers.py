"""Guest transfer boundary: binary bodies stay outside action receipts."""

import hashlib
import json
import os
import uuid

import httpx
import pytest
from meerkat_computer.protocol import WorkerError
from meerkat_computer.worker import Worker, create_app
from test_worker import TOKEN, FakeBrowser, call, lease


@pytest.fixture
async def transfer_worker(tmp_path):
    root = tmp_path.resolve()
    worker = Worker(root / "state", root / "workspace", TOKEN, FakeBrowser(), clock=lambda: 1000)
    await worker.start()
    await worker.grant(lease(capabilities=["files", "file_import", "file_export"]))
    try:
        yield worker
    finally:
        await worker.close()


def headers(epoch=1):
    return {
        "Authorization": "Bearer " + TOKEN,
        "X-Meerkat-Lease-Id": "lease-a",
        "X-Meerkat-Epoch": str(epoch),
        "Content-Type": "application/octet-stream",
    }


def connection(worker):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(worker)), base_url="http://worker"
    )


async def stage(client, data):
    file_id = str(uuid.uuid4())
    response = await client.put("/v1/transfers/" + file_id, content=data, headers=headers())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "file_id": file_id, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()
    }
    return file_id


async def test_binary_roundtrip_receipts_and_replay(transfer_worker):
    worker = transfer_worker
    data = b"PRIVATE-BINARY-CONTENT\x00\xff\x80" * 37
    async with connection(worker) as client:
        incoming = await stage(client, data)
        request = call("transfer_import", {
            "file_id": incoming, "path": "input.bin", "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "overwrite": False,
        }, "import")
        status, receipt = await worker.call(request)
        assert status == 200, receipt
        assert (worker.workspace.root / "input.bin").read_bytes() == data
        assert await worker.call(request) == (status, receipt)
        # An imported blob is consumed, and cannot be read back over the export route.
        assert (await client.get("/v1/transfers/" + incoming, headers=headers())).status_code == 404
        outgoing = str(uuid.uuid4())
        status, receipt = await worker.call(call("transfer_export", {
            "file_id": outgoing, "path": "input.bin", "max_bytes": len(data),
        }, "export"))
        assert status == 200, receipt
        meta = json.loads(receipt["content"][0]["text"])
        assert meta["sha256"] == hashlib.sha256(data).hexdigest()
        assert meta["size_bytes"] == len(data)
        assert "PRIVATE-BINARY" not in json.dumps(receipt)
        response = await client.get("/v1/transfers/" + outgoing, headers=headers())
        assert response.content == data
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["cache-control"] == "no-store"
        assert (await client.delete("/v1/transfers/" + outgoing, headers=headers())).status_code == 200
        assert (await client.get("/v1/transfers/" + outgoing, headers=headers())).status_code == 404
        saved = worker.journal.db.execute("SELECT receipt FROM actions").fetchall()
        assert all("PRIVATE-BINARY" not in row[0] for row in saved)


async def test_blob_routes_require_current_authority(transfer_worker):
    async with connection(transfer_worker) as client:
        file_id = str(uuid.uuid4())
        url = "/v1/transfers/" + file_id
        for method in ("PUT", "GET", "DELETE"):
            assert (await client.request(method, url)).status_code == 401
            assert (await client.request(method, url, headers=headers(2))).status_code == 409
        incoming = await stage(client, b"secret")
        assert (await client.get("/v1/transfers/" + incoming, headers=headers())).status_code == 404
        await transfer_worker.grant(lease(epoch=2, capabilities=["files", "file_export"]))
        assert (await client.put(url, headers=headers(2), content=b"x")).status_code == 403


async def test_import_hash_mismatch_never_writes(transfer_worker):
    async with connection(transfer_worker) as client:
        file_id = await stage(client, b"original")
        with pytest.raises(WorkerError) as error:
            await transfer_worker.call(call("transfer_import", {
                "file_id": file_id, "path": "bad.bin", "size_bytes": 8, "sha256": "0" * 64,
            }))
        assert error.value.effect == "none"
        assert not (transfer_worker.workspace.root / "bad.bin").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "parent_symlink", "traversal"])
async def test_export_refuses_unsafe_paths(transfer_worker, kind):
    root = transfer_worker.workspace.root
    (root / "good").write_bytes(b"sensitive")
    target = root / "bad"
    if kind == "symlink":
        target.symlink_to(root / "good")
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "parent_symlink":
        target.symlink_to(root, target_is_directory=True)
        target = target / "good"
    else:
        target = root / ".." / "workspace" / "good"
    with pytest.raises(WorkerError) as error:
        await transfer_worker.call(call("transfer_export", {
            "file_id": str(uuid.uuid4()), "path": str(target), "max_bytes": 100,
        }))
    assert error.value.effect == "none"
    assert transfer_worker.journal.metrics().get("tool_calls_admitted", 0) == 0


async def test_no_clobber_and_explicit_replace(transfer_worker):
    root = transfer_worker.workspace.root
    (root / "existing").write_bytes(b"old")
    async with connection(transfer_worker) as client:
        file_id = await stage(client, b"new")
        args = {"file_id": file_id, "path": "existing", "size_bytes": 3,
                "sha256": hashlib.sha256(b"new").hexdigest()}
        with pytest.raises(WorkerError):
            await transfer_worker.call(call("transfer_import", args, "no-clobber"))
        assert (root / "existing").read_bytes() == b"old"
        status, _ = await transfer_worker.call(call("transfer_import", {**args, "overwrite": True}))
        assert status == 200
        assert (root / "existing").read_bytes() == b"new"


async def test_blob_quota_and_export_size_are_bounded(transfer_worker, monkeypatch):
    monkeypatch.setattr(transfer_worker.transfers, "max_stored_bytes", 5)
    async with connection(transfer_worker) as client:
        await stage(client, b"12345")
        response = await client.put("/v1/transfers/" + str(uuid.uuid4()),
                                    content=b"6", headers=headers())
        assert response.status_code == 413
    (transfer_worker.workspace.root / "large").write_bytes(b"1234")
    with pytest.raises(WorkerError):
        await transfer_worker.call(call("transfer_export", {
            "file_id": str(uuid.uuid4()), "path": "large", "max_bytes": 3,
        }))


async def test_new_lease_cannot_use_prior_staging(transfer_worker):
    async with connection(transfer_worker) as client:
        file_id = await stage(client, b"old lease")
        await transfer_worker.grant(lease(epoch=2, capabilities=["file_import", "file_export"]))
        with pytest.raises(WorkerError):
            await transfer_worker.call(call("transfer_import", {
                "file_id": file_id, "path": "old", "size_bytes": 9,
                "sha256": hashlib.sha256(b"old lease").hexdigest(),
            }, epoch=2))


async def test_oversize_and_interrupted_body_leaves_no_blob(transfer_worker, monkeypatch):
    monkeypatch.setattr("meerkat_computer.transfers.MAX_FILE_BYTES", 4)
    async with connection(transfer_worker) as client:
        response = await client.put("/v1/transfers/" + str(uuid.uuid4()),
                                    content=b"12345", headers=headers())
        assert response.status_code == 413
        assert transfer_worker.transfers.usage() == (0, 0)

        async def interrupted():
            yield b"123"
            raise RuntimeError("simulated disconnect")

        response = await client.put("/v1/transfers/" + str(uuid.uuid4()),
                                    content=interrupted(), headers=headers())
        assert response.status_code == 400
        assert "simulated disconnect" not in response.text
        assert transfer_worker.transfers.usage() == (0, 0)


async def test_expiry_during_body_does_not_stage(transfer_worker):
    async def expires():
        yield b"before"
        transfer_worker.clock = lambda: 3000
        yield b"after"

    async with connection(transfer_worker) as client:
        response = await client.put("/v1/transfers/" + str(uuid.uuid4()),
                                    content=expires(), headers=headers())
        assert response.status_code == 409
        assert transfer_worker.transfers.usage() == (0, 0)


async def test_overwrite_replaces_inode_without_modifying_hardlink(transfer_worker):
    root = transfer_worker.workspace.root
    (root / "original").write_bytes(b"preserved")
    os.link(root / "original", root / "target")
    async with connection(transfer_worker) as client:
        file_id = await stage(client, b"new")
        status, _ = await transfer_worker.call(call("transfer_import", {
            "file_id": file_id, "path": "target", "size_bytes": 3,
            "sha256": hashlib.sha256(b"new").hexdigest(), "overwrite": True,
        }))
        assert status == 200
    assert (root / "original").read_bytes() == b"preserved"
    assert (root / "target").read_bytes() == b"new"


async def test_lost_import_receipt_does_not_repeat_effect(transfer_worker):
    worker = transfer_worker
    async with connection(worker) as client:
        file_id = await stage(client, b"once")
        original = worker.transfers.perform
        attempts = 0

        async def lose_receipt(name, args):
            nonlocal attempts
            attempts += 1
            await original(name, args)
            raise RuntimeError("private diagnostic must not leak")

        worker.transfers.perform = lose_receipt
        request = call("transfer_import", {
            "file_id": file_id, "path": "once", "size_bytes": 4,
            "sha256": hashlib.sha256(b"once").hexdigest(),
        })
        status, body = await worker.call(request)
        assert status == 409 and body["error"]["effect"] == "unknown"
        assert "private diagnostic" not in json.dumps(body)
        assert await worker.call(request) == (status, body)
        assert attempts == 1


async def test_staged_exports_survive_worker_restart(tmp_path):
    root = tmp_path.resolve()
    worker = Worker(root / "state", root / "workspace", TOKEN, FakeBrowser(), clock=lambda: 1000)
    await worker.start()
    await worker.grant(lease(capabilities=["file_export"]))
    (worker.workspace.root / "source").write_bytes(b"persisted")
    file_id = str(uuid.uuid4())
    request = call("transfer_export", {"file_id": file_id, "path": "source", "max_bytes": 100})
    receipt = await worker.call(request)
    await worker.close()
    restarted = Worker(root / "state", root / "workspace", TOKEN, FakeBrowser(), clock=lambda: 1000)
    await restarted.start()
    try:
        assert await restarted.call(request) == receipt
        async with connection(restarted) as client:
            response = await client.get("/v1/transfers/" + file_id, headers=headers())
            assert response.content == b"persisted"
    finally:
        await restarted.close()
