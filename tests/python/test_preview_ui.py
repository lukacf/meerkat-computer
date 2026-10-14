"""Real Chromium tests for the read-only cached screenshot preview."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Error, async_playwright, expect

pytestmark = pytest.mark.browser
UI = Path(__file__).resolve().parents[2] / "ui"
TOKEN = "synthetic-preview-operator-token-not-a-real-secret"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7ioAAAAASUVORK5CYII="
OWNER = {
    "kind": "identity",
    "realm": "studio",
    "namespace": "team",
    "identity": '<img src=x onerror="window.injected=1">',
}


@pytest.fixture
def preview_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = {
                "/preview": "preview.html",
                "/preview.js": "preview.js",
                "/preview.css": "preview.css",
            }.get(self.path)
            path = UI / name if name else None
            if path is None or not path.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[path.suffix],
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(path.read_bytes())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class Broker:
    def __init__(self):
        self.computer = {
            "computer_id": "computer-one",
            "owner": OWNER,
            "slot": "main",
            "profile": "desktop",
            "phase": "leased",
            "epoch": 1,
            "lease": {"expires_at_ms": 9_000_000_000_000},
        }
        self.frame = {
            "observation_id": "a" * 32,
            "surface": "desktop",
            "width": 1,
            "height": 1,
            "mime_type": "image/png",
            "data": PNG,
            "received_at_ms": 1_700_000_000_000,
            "age_ms": 4000,
        }
        self.status = "available"
        self.reason = None
        self.calls = []
        self.polled = asyncio.Event()
        self.entered = asyncio.Event()
        self.gate = None

    async def handle(self, route):
        request = route.request
        url = urlsplit(request.url)
        headers = await request.all_headers()
        assert request.method == "GET"
        assert headers.get("authorization") == f"Bearer {TOKEN}"
        assert "cookie" not in headers and "referer" not in headers
        self.calls.append((url.path, parse_qs(url.query)))
        if url.path == "/v1/computers":
            body = {"computers": [self.computer]}
        else:
            assert url.path == "/v1/computers/computer-one/preview"
            frame = dict(self.frame)
            if parse_qs(url.query).get("after") == [frame["observation_id"]]:
                frame["data"] = None
                self.polled.set()
            body = {
                "computer": self.computer,
                "status": self.status,
                "reason": self.reason,
                "frame": frame if self.status == "available" else None,
            }
            if self.gate:
                self.entered.set()
                await self.gate.wait()
        try:
            await route.fulfill(json=body)
        except Error:
            pass  # Lock aborts the in-flight read.


@pytest.fixture
async def preview_page(preview_site):
    broker = Broker()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(5000)
        await page.route("**/v1/**", broker.handle)
        await page.goto(f"{preview_site}/preview")
        yield page, broker
        if broker.gate:
            broker.gate.set()
        await browser.close()


async def unlock(page):
    await page.get_by_label("Operator token", exact=True).fill(TOKEN)
    await page.get_by_role("button", name="Unlock preview", exact=True).click()
    await expect(page.get_by_role("img", name="Agent screenshot", exact=True)).to_be_visible()


async def test_labels_are_optional_and_token_stays_only_in_memory(preview_page):
    page, broker = preview_page
    assert broker.calls == []
    await unlock(page)
    await expect(page.get_by_text(OWNER["identity"], exact=True)).to_be_visible()
    await expect(page.get_by_text("Capture received", exact=False)).to_be_visible()
    await page.get_by_label("Show labels", exact=True).uncheck()
    await expect(page.get_by_text(OWNER["identity"], exact=True)).to_be_hidden()
    assert await page.evaluate("window.injected") is None
    assert await page.locator("img").count() == 1
    assert await page.evaluate("JSON.stringify({...localStorage})") == "{}"
    assert await page.evaluate("JSON.stringify({...sessionStorage})") == "{}"
    assert await page.context.cookies() == []
    assert TOKEN not in page.url and TOKEN not in await page.content()
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


async def test_polling_reuses_capture_cursor_without_control_calls(preview_page):
    page, broker = preview_page
    await unlock(page)
    source = await page.get_by_role("img", name="Agent screenshot").get_attribute("src")
    await asyncio.wait_for(broker.polled.wait(), timeout=5)
    assert await page.get_by_role("img", name="Agent screenshot").get_attribute("src") == source
    assert all(path == "/v1/computers" or path.endswith("/preview") for path, _ in broker.calls)


async def test_withholding_and_malformed_images_remove_old_frame(preview_page):
    page, broker = preview_page
    await unlock(page)
    broker.status, broker.reason = "withheld", "private_input"
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await expect(page.locator("img[src]")).to_have_count(0)
    await expect(page.get_by_text("Private input is protected", exact=False)).to_be_visible()
    broker.status, broker.reason = "available", None
    broker.frame.update(observation_id="b" * 32, data="https://untrusted.example/password")
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await expect(page.locator("img[src]")).to_have_count(0)
    broker.frame.update(observation_id="c" * 32, data=PNG)
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await expect(page.get_by_role("img", name="Agent screenshot")).to_be_visible()


async def test_lock_cancels_reads_and_late_response_cannot_restore_images(preview_page):
    page, broker = preview_page
    await unlock(page)
    broker.gate = asyncio.Event()
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await asyncio.wait_for(broker.entered.wait(), timeout=5)
    await page.get_by_role("button", name="Lock", exact=True).click()
    broker.gate.set()
    await expect(page.get_by_role("button", name="Unlock preview", exact=True)).to_be_visible()
    await expect(page.locator("img")).to_have_count(0)
    await expect(page.get_by_label("Operator token", exact=True)).to_have_value("")
