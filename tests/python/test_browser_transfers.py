"""Guest file upload validation and explicit real Chromium website transfers."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from meerkat_computer.browser import Browser
from meerkat_computer.protocol import Lease, WorkerError
from meerkat_computer.surfaces import ComputerSurfaces


def lease():
    return Lease(
        lease_id="transfers",
        epoch=1,
        expires_at_ms=9_000_000_000_000,
        capabilities=["browser", "browser_semantics", "desktop", "desktop_semantics"],
        observation={"mode": "semantic", "width": 640, "height": 480},
    )


def upload_arguments(body, paths, label="Documents"):
    return SimpleNamespace(
        observation_id=body["observation_id"],
        ref=next(item["ref"] for item in body["elements"] if item["label"] == label),
        paths=[str(path) for path in paths],
    )


def test_upload_payloads_are_bytes_and_do_not_expose_guest_directories(tmp_path):
    from meerkat_computer.browser_transfers import read_upload_payloads

    root = tmp_path.resolve()
    source = root / "report.txt"
    source.write_bytes(b"guest contents\x00\xff")
    assert read_upload_payloads([str(source)]) == [
        {"name": "report.txt", "mimeType": "text/plain", "buffer": b"guest contents\x00\xff"}
    ]


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "parent_symlink", "fifo"])
def test_upload_rejects_non_regular_or_symlink_paths_without_blocking(tmp_path, kind):
    from meerkat_computer.browser_transfers import read_upload_payloads

    root = tmp_path.resolve()
    actual = root / "actual.txt"
    actual.write_text("private")
    source = root / "source"
    if kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        source.symlink_to(actual)
    elif kind == "parent_symlink":
        source.symlink_to(root, target_is_directory=True)
        source /= "actual.txt"
    elif kind == "fifo":
        os.mkfifo(source)
    with pytest.raises(WorkerError) as error:
        read_upload_payloads([str(source)])
    assert error.value.effect == "none"


@pytest.mark.parametrize("paths", [[], ["relative.txt"], ["/dev/null"], ["/a/../b"], ["/x"] * 17])
def test_upload_rejects_invalid_guest_path_requests(paths):
    from meerkat_computer.browser_transfers import read_upload_payloads

    with pytest.raises(WorkerError):
        read_upload_payloads(paths)


def test_upload_aggregate_limit_is_checked_across_files(tmp_path, monkeypatch):
    import meerkat_computer.browser_transfers as transfers

    monkeypatch.setattr(transfers, "MAX_UPLOAD_BYTES", 8)
    paths = [tmp_path.resolve() / "one", tmp_path.resolve() / "two"]
    paths[0].write_bytes(b"1234")
    paths[1].write_bytes(b"5678")
    assert b"".join(item["buffer"] for item in transfers.read_upload_payloads(paths)) == b"12345678"
    paths[1].write_bytes(b"56789")
    with pytest.raises(WorkerError, match="limit"):
        transfers.read_upload_payloads(paths)


async def test_download_listing_is_passive_and_only_returns_existing_completed_files(tmp_path):
    browser = Browser(tmp_path.resolve() / "profile")
    browser.downloads_directory.mkdir()
    ready = browser.downloads_directory / "saved.txt"
    ready.write_bytes(b"ready")
    link = browser.downloads_directory / "linked.txt"
    link.symlink_to(ready)
    browser.completed_downloads = [
        {"filename": path.name, "path": str(path)}
        for path in (ready, link, browser.downloads_directory / "missing")
    ]
    browser.download_tasks = {Mock(done=lambda: False), Mock(done=lambda: True)}
    browser.observation_id = "browser-observation"
    desktop = SimpleNamespace(invalidate=AsyncMock())
    surfaces = ComputerSurfaces(browser, desktop)
    surfaces.lease = browser.lease = lease()
    surfaces.active = "desktop"
    await surfaces.validate("browser_downloads", SimpleNamespace())
    content, metrics = await surfaces.perform("browser_downloads", SimpleNamespace())
    body = json.loads(content[0]["text"])
    assert body == {
        "downloads_directory": str(browser.downloads_directory),
        "downloads": [{"filename": "saved.txt", "path": str(ready), "size_bytes": 5}],
        "pending_count": 1,
    }
    assert metrics == {}
    assert browser.observation_id == "browser-observation" and surfaces.active == "desktop"
    desktop.invalidate.assert_not_awaited()


async def test_failed_upload_acknowledgement_consumes_prepared_data_and_cannot_replay(tmp_path):
    browser = Browser(tmp_path / "profile")
    arguments = SimpleNamespace(observation_id="observed", ref="file-ref", paths=["/guest.txt"])
    browser.observation_id = arguments.observation_id
    browser.prepared_upload = browser.upload_key(arguments), [{"buffer": b"guest"}]
    handle = SimpleNamespace(set_input_files=AsyncMock(side_effect=OSError("acknowledgement lost")))
    browser.upload_target = AsyncMock(return_value=handle)
    with pytest.raises(OSError):
        await browser.perform("browser_upload", arguments)
    assert browser.operation_metrics == {"upload_attempts": 1, "ui_command_attempts": 1}
    assert browser.prepared_upload is None and not browser.observation_id
    with pytest.raises(WorkerError) as error:
        await browser.perform("browser_upload", arguments)
    assert error.value.code == "upload_not_prepared"
    handle.set_input_files.assert_awaited_once()


async def test_upload_requires_semantic_capability_before_reading_guest_files(tmp_path):
    browser = Browser(tmp_path / "profile")
    browser.lease = lease().model_copy(update={"capabilities": ["browser", "screenshots"]})
    arguments = SimpleNamespace(observation_id="observed", ref="ref", paths=["/missing"])
    with pytest.raises(WorkerError) as error:
        await browser.validate("browser_upload", arguments)
    assert error.value.code == "capability_denied"


@pytest.fixture
def transfer_site():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/download":
                self.send_response(200)
                self.send_header("Content-Disposition", 'attachment; filename="download.txt"')
                self.end_headers()
                self.wfile.write(b"downloaded bytes")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!doctype html><html><body>
<label>Documents <input id="files" type="file" multiple></label>
<label>Single file <input id="single" type="file"></label>
<button>Ordinary button</button><a href="/download">Download</a>
<script>files.onchange = async () => {
  const payload = await Promise.all(Array.from(files.files, async f =>
    ({name:f.name, content:await f.text()})));
  await fetch('/upload', {method:'POST', body:JSON.stringify(payload)});
  window.uploadDone = true;
};</script></body></html>""")

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
async def real_browser(tmp_path, transfer_site):
    browser = Browser(tmp_path.resolve() / "profile")
    await browser.start()
    try:
        await browser.configure(lease())
        await browser.page.goto(transfer_site[0])
        yield browser
    finally:
        await browser.close()


@pytest.mark.browser
async def test_website_upload_uses_staged_bytes_and_reports_acknowledged_metrics(
    real_browser, tmp_path, transfer_site
):
    browser = real_browser
    source = tmp_path.resolve() / "guest.txt"
    source.write_text("original guest bytes")
    body = json.loads((await browser.observe())[0]["text"])
    arguments = upload_arguments(body, [source])
    await browser.validate("browser_upload", arguments)
    # A changed pathname must not redirect the already validated upload.
    source.unlink()
    source.symlink_to(tmp_path.resolve() / "missing-private-file")
    content, metrics = await browser.perform("browser_upload", arguments)
    await browser.page.wait_for_function("window.uploadDone === true")
    assert transfer_site[1] == [[{"name": "guest.txt", "content": "original guest bytes"}]]
    assert json.loads(content[0]["text"])["observation_id"] != body["observation_id"]
    assert metrics["upload_attempts"] == metrics["uploads"] == 1
    assert metrics["ui_command_attempts"] == metrics["ui_commands"] == 1


@pytest.mark.browser
@pytest.mark.parametrize("change", ["detach", "replace", "disable", "wrong_type", "stale_id"])
async def test_upload_revalidates_target_after_file_read_and_before_input(
    real_browser, tmp_path, transfer_site, change
):
    browser = real_browser
    source = tmp_path.resolve() / "guest.txt"
    source.write_text("must not leave guest")
    body = json.loads((await browser.observe())[0]["text"])
    arguments = upload_arguments(body, [source])
    await browser.validate("browser_upload", arguments)
    if change == "stale_id":
        arguments.observation_id = "old"
    else:
        await browser.page.evaluate(
            {
                "detach": "files.remove()",
                "replace": "files.replaceWith(files.cloneNode())",
                "disable": "files.disabled = true",
                "wrong_type": "files.type = 'text'",
            }[change]
        )
    with pytest.raises(WorkerError):
        await browser.perform("browser_upload", arguments)
    assert not transfer_site[1]
    assert browser.operation_metrics.get("upload_attempts", 0) == 0


@pytest.mark.browser
async def test_upload_rejects_wrong_ref_and_multiple_files_for_single_input(real_browser, tmp_path):
    browser = real_browser
    source = tmp_path.resolve() / "guest.txt"
    source.write_text("guest")
    body = json.loads((await browser.observe())[0]["text"])
    for label, paths in [("Ordinary button", [source]), ("Single file", [source, source])]:
        with pytest.raises(WorkerError):
            await browser.validate("browser_upload", upload_arguments(body, paths, label))
    browser.secure = SimpleNamespace(guarded=AsyncMock(return_value=True))
    with pytest.raises(WorkerError) as error:
        await browser.validate("browser_upload", upload_arguments(body, [source]))
    assert error.value.code == "privacy_guarded"


@pytest.mark.browser
async def test_lease_change_discards_prepared_upload(real_browser, tmp_path, transfer_site):
    browser = real_browser
    source = tmp_path.resolve() / "guest.txt"
    source.write_text("old lease data")
    arguments = upload_arguments(json.loads((await browser.observe())[0]["text"]), [source])
    await browser.validate("browser_upload", arguments)
    await browser.configure(lease().model_copy(update={"epoch": 2, "lease_id": "new-lease"}))
    assert browser.prepared_upload is None
    with pytest.raises(WorkerError):
        await browser.perform("browser_upload", arguments)
    assert not transfer_site[1]


@pytest.mark.browser
async def test_completed_website_download_metadata_is_passive(real_browser):
    browser = real_browser
    await browser.page.get_by_text("Download", exact=True).click()
    for _ in range(100):
        if browser.completed_downloads and not browser.download_tasks:
            break
        await asyncio.sleep(0.02)
    observed = json.loads((await browser.observe())[0]["text"])
    content, metrics = await browser.perform("browser_downloads", SimpleNamespace())
    body = json.loads(content[0]["text"])
    assert body["pending_count"] == 0 and len(body["downloads"]) == 1
    item = body["downloads"][0]
    assert item["size_bytes"] == len(b"downloaded bytes")
    assert Path(item["path"]).read_bytes() == b"downloaded bytes"
    assert browser.observation_id == observed["observation_id"] and metrics == {}
