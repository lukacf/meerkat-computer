"""Real Chromium and the official App SDK, with a deterministic protocol host.

This tests the View, not native Meerkat authorization or Console deployment.
"""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "python/meerkat_computer/resources/preview-v1.html"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7ioAAAAASUVORK5CYII="


@pytest.fixture
async def app_page(request):
    html = HTML.read_text()
    options = getattr(request, "param", {})
    delay_initialize = options.get("delay_initialize", False)
    server_tools = options.get("server_tools", True)
    private_metadata = options.get("private_metadata", True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        await page.set_content("<iframe title='My Computer' sandbox='allow-scripts'></iframe>")
        await page.evaluate(
            """({html, png, delayInitialize, serverTools, privateMetadata}) => {
              window.calls = [];
              window.waiting = [];
              window.paused = true;
              window.fail = false;
              window.initializedReady = false;
              window.view = {
                computer_id: 'assigned-computer', status: 'available', reason: null,
                frame: {observation_id: 'a'.repeat(32), surface: 'desktop', width: 1,
                  height: 1, mime_type: 'image/png', data: png,
                  received_at_ms: 1700000000000, age_ms: 4000}
              };
              const frame = document.querySelector('iframe');
              window.reply = (id, result) => frame.contentWindow.postMessage(
                {jsonrpc: '2.0', id, result}, '*');
              window.finish = () => {
                for (const [id, result] of waiting.splice(0)) reply(id, result);
              };
              window.notify = (method, params = {}) => frame.contentWindow.postMessage(
                {jsonrpc: '2.0', method, params}, '*');
              window.addEventListener('message', event => {
                if (event.source !== frame.contentWindow) return;
                const msg = event.data;
                if (msg.method === 'ui/initialize') {
                  const result = {protocolVersion: msg.params.protocolVersion,
                    hostInfo: {name: 'Deterministic fixture', version: '1'},
                    hostCapabilities: serverTools ? {serverTools: {}} : {},
                    hostContext: {theme: 'light', displayMode: 'inline'}};
                  window.initialization = () => reply(msg.id, result);
                  if (!delayInitialize) initialization();
                } else if (msg.method === 'ui/notifications/initialized') {
                  initializedReady = true;
                } else if (msg.method === 'tools/call') {
                  calls.push(msg.params);
                  let result;
                  if (fail) result = {isError: true, content: [{type: 'text', text: 'Denied'}]};
                  else {
                    const current = structuredClone(view);
                    if (current.frame && msg.params.arguments?.after === current.frame.observation_id)
                      current.frame.data = null;
                    result = privateMetadata
                      ? {content: [], _meta: {'meerkat-computer/preview': current}}
                      : {content: [], structuredContent: {preview: current}};
                  }
                  if (paused) waiting.push([msg.id, result]);
                  else reply(msg.id, result);
                }
              });
              frame.srcdoc = html;
            }""",
            {"html": html, "png": PNG, "delayInitialize": delay_initialize,
             "serverTools": server_tools, "privateMetadata": private_metadata},
        )
        await page.wait_for_function(
            "typeof initialization === 'function'" if delay_initialize
            else "calls.length === 1" if server_tools else "initializedReady"
        )
        frame = page.frame_locator("iframe")
        yield page, frame, requests
        await browser.close()


async def release(page):
    await page.evaluate("() => { paused = false; finish(); }")


async def test_app_reads_current_preview_over_original_host_channel(app_page):
    page, frame, requests = app_page
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    # Historical invocation content must not become a current preview.
    await page.evaluate(
        """png => notify('ui/notifications/tool-result', {
          content: [{type: 'image', mimeType: 'image/png', data: png}]
        })""",
        PNG,
    )
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    await release(page)
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_visible()
    await expect(frame.get_by_text("My Computer", exact=True)).to_be_visible()
    await frame.get_by_label("Show label").uncheck()
    await expect(frame.get_by_text("My Computer", exact=True)).to_be_hidden()
    await frame.get_by_role("button", name="Refresh").click()
    await page.wait_for_function("calls.length >= 2")
    calls = await page.evaluate("calls")
    assert all(call["name"] == "computer_preview" for call in calls)
    assert calls[-1]["arguments"] == {"after": "a" * 32}
    assert not any(url.startswith(("http:", "https:")) for url in requests)


async def test_withheld_and_denied_results_clear_existing_image(app_page):
    page, frame, _ = app_page
    await release(page)
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_visible()
    await page.evaluate("() => { view.reason = 'private_input'; }")
    await frame.get_by_role("button", name="Refresh").click()
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    await page.evaluate("() => { view.status = 'withheld'; view.reason = 'private_input'; }")
    await frame.get_by_role("button", name="Refresh").click()
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    await expect(frame.get_by_text("Private input is protected.", exact=False)).to_be_visible()
    await page.evaluate("() => { view.status = 'available'; view.reason = null; }")
    await frame.get_by_role("button", name="Refresh").click()
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_visible()
    await page.evaluate("() => { fail = true; }")
    await frame.get_by_role("button", name="Refresh").click()
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    await expect(frame.get_by_role("status")).to_contain_text("unavailable")


async def test_teardown_discards_inflight_preview_and_stops_polling(app_page):
    page, frame, _ = app_page
    await page.evaluate(
        """() => document.querySelector('iframe').contentWindow.postMessage({
          jsonrpc: '2.0', id: 'teardown', method: 'ui/resource-teardown', params: {}
        }, '*')"""
    )
    await expect(frame.get_by_role("status")).to_contain_text("closed")
    await release(page)
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    assert await page.evaluate("calls.length") == 1


async def test_invalid_png_and_changed_computer_never_render(app_page):
    page, frame, _ = app_page
    await release(page)
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_visible()
    await page.evaluate(
        "() => { view.frame.observation_id = 'b'.repeat(32); view.frame.data = 'https://bad/'; }"
    )
    await frame.get_by_role("button", name="Refresh").click()
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    await page.evaluate(
        "png => { view.frame.data = png; view.computer_id = 'different-computer'; }", PNG
    )
    await frame.get_by_role("button", name="Refresh").click()
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    assert "different-computer" not in await frame.locator("body").inner_text()


@pytest.mark.parametrize("app_page", [{"delay_initialize": True}], indirect=True)
async def test_closed_preview_cannot_reopen_after_late_initialize(app_page):
    page, frame, _ = app_page
    await frame.locator("body").evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    await expect(frame.get_by_role("status")).to_contain_text("closed")
    await page.evaluate("() => initialization()")
    await page.wait_for_function("initializedReady")
    await expect(frame.get_by_role("button", name="Refresh")).to_be_disabled()
    assert await page.evaluate("calls.length") == 0


@pytest.mark.parametrize("app_page", [{"server_tools": False}], indirect=True)
async def test_retained_read_only_view_does_not_request_or_display_a_current_frame(app_page):
    page, frame, _ = app_page
    await expect(frame.get_by_role("status")).to_contain_text("read-only")
    await page.evaluate(
        """png => notify('ui/notifications/tool-result', {
          content: [{type: 'image', mimeType: 'image/png', data: png}],
          _meta: {'meerkat-computer/preview': view}
        })""", PNG,
    )
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
    await expect(frame.get_by_role("button", name="Refresh")).to_be_disabled()
    assert await page.evaluate("calls.length") == 0


@pytest.mark.parametrize("app_page", [{"private_metadata": False}], indirect=True)
async def test_public_result_payload_is_not_a_current_private_preview(app_page):
    page, frame, _ = app_page
    await release(page)
    await expect(frame.get_by_role("status")).to_contain_text("unavailable")
    await expect(frame.get_by_alt_text("Agent screenshot")).to_be_hidden()
