"""Explicit Chromium checks for the operator UI using a local, mocked broker."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.browser
UI = Path(__file__).resolve().parents[2] / "ui"
TOKEN = "fixture-operator-token-not-a-real-secret"
SECRET = "fixture-password-never-show-this"
OWNER = {"kind": "identity", "realm": "studio", "namespace": "team", "identity": "artist"}
SECOND_OWNER = {**OWNER, "identity": "writer"}


@pytest.fixture
def credential_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = {
                "/credentials": "credentials.html",
                "/credentials.js": "credentials.js",
                "/credentials.css": "credentials.css",
            }.get(self.path.split("?", 1)[0])
            path = UI / name if name else None
            if path is None or not path.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            content_type = {
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
            }[path.suffix]
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'",
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
        self.credentials = [
            {
                "id": "cred-one",
                "realm": "studio",
                "label": "Design account",
                "origin": "https://design.example",
                "version": 1,
                "assignments": [OWNER],
            },
            {
                "id": "cred-two",
                "realm": "studio",
                "label": '<img src=x onerror="window.injected=1">',
                "origin": "https://notes.example",
                "version": 2,
                "assignments": [],
            },
        ]
        self.requests = [
            {
                "id": "request-one",
                "computer_id": "computer-one",
                "owner": OWNER,
                "target": {
                    "origin": "https://design.example",
                    "observation_id": "view-one",
                    "secret_ref": "field-2",
                    "username_ref": "field-1",
                },
                "expires_at_ms": 9_000_000_000_000,
                "phase": "pending",
            }
        ]
        self.calls = []
        self.fail = None
        self.computers = [
            {"owner": OWNER}, {"owner": OWNER}, {"owner": SECOND_OWNER},
            {"owner": {**OWNER, "namespace": "other-team"}},
            {"owner": {"kind": "session", "realm": "studio", "session_id": "temporary"}},
        ]

    async def handle(self, route):
        request = route.request
        path = request.url.split("/v1/", 1)[1]
        headers = await request.all_headers()
        body = request.post_data_json if request.post_data else None
        self.calls.append((request.method, path, body, headers))
        assert headers.get("authorization") == f"Bearer {TOKEN}"
        assert "cookie" not in headers
        assert "referer" not in headers
        if self.fail == (request.method, path):
            await route.fulfill(status=500, json={"error": SECRET, "token": TOKEN})
            return
        if request.method == "GET":
            result = {
                "credentials": {"credentials": self.credentials},
                "credential-requests": {"requests": self.requests},
                "computers": {"computers": self.computers},
            }[path]
        elif path in {"credentials/assign", "credentials/unassign"}:
            for credential in self.credentials:
                if credential["id"] not in body["credential_ids"]:
                    continue
                for owner in body["owners"]:
                    if path.endswith("/assign") and owner not in credential["assignments"]:
                        credential["assignments"].append(owner)
                    elif path.endswith("/unassign") and owner in credential["assignments"]:
                        credential["assignments"].remove(owner)
            result = {}
        elif path.endswith(("/fulfill", "/cancel")):
            self.requests[0]["phase"] = "delivered" if path.endswith("/fulfill") else "cancelled"
            result = {}
        elif request.method == "POST":
            self.credentials.append(
                {**{k: body[k] for k in ("realm", "label", "origin")},
                 "id": "cred-created", "version": 1, "assignments": []}
            )
            result = self.credentials[-1]
        elif request.method == "PUT":
            self.credentials[0].update({k: body[k] for k in ("realm", "label", "origin")})
            self.credentials[0]["version"] += 1
            result = self.credentials[0]
        else:
            self.credentials = [item for item in self.credentials if item["id"] != path.split("/")[1]]
            result = {}
        await route.fulfill(json=result)


@pytest.fixture
async def operator_page(credential_site):
    broker = Broker()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(5000)
        await page.route("**/v1/**", broker.handle)
        await page.goto(f"{credential_site}/credentials#request-one")
        yield page, broker
        await browser.close()


async def unlock(page):
    await page.get_by_label("Operator token", exact=True).fill(TOKEN)
    await page.get_by_role("button", name="Unlock credentials", exact=True).click()
    await expect(page.get_by_role("button", name="Lock", exact=True)).to_be_visible()
    await expect(page.get_by_text("Design account", exact=True)).to_be_visible()


async def assert_secrets_not_retained(page):
    assert await page.evaluate("JSON.stringify({...localStorage})") == "{}"
    assert await page.evaluate("JSON.stringify({...sessionStorage})") == "{}"
    assert await page.context.cookies() == []
    assert TOKEN not in page.url and SECRET not in page.url
    assert TOKEN not in await page.content() and SECRET not in await page.content()


async def test_bulk_identity_assignment_filter_and_revoke(operator_page):
    page, broker = operator_page
    assert broker.calls == []
    await unlock(page)
    await page.get_by_label("Select Design account", exact=True).check()
    await page.get_by_label('Select <img src=x onerror="window.injected=1">', exact=True).check()
    await page.get_by_label("Owner realm 1", exact=True).fill("studio")
    await page.get_by_label("Owner namespace 1", exact=True).fill("team")
    await page.get_by_label("Owner identity 1", exact=True).fill("artist")
    await page.get_by_role("button", name="Add identity", exact=True).click()
    await page.get_by_label("Owner realm 2", exact=True).fill("studio")
    await page.get_by_label("Owner namespace 2", exact=True).fill("team")
    await page.get_by_label("Owner identity 2", exact=True).fill("writer")
    await page.get_by_role("button", name="Assign selected", exact=True).click()
    await expect(page.get_by_role("status")).to_contain_text("Assignments saved")
    assert next(call[2] for call in broker.calls if call[1] == "credentials/assign") == {
        "credential_ids": ["cred-one", "cred-two"], "owners": [OWNER, SECOND_OWNER]
    }
    await page.get_by_label("Filter identity", exact=True).fill("writer")
    await expect(page.locator(".credential-card")).to_have_count(2)
    await page.get_by_role("button", name="Revoke selected", exact=True).click()
    await expect(page.locator(".credential-card")).to_have_count(0)
    await page.get_by_label("Filter identity", exact=True).fill("")
    await expect(page.locator(".credential-card")).to_have_count(2)
    assert await page.evaluate("window.injected") is None
    assert await page.locator("img").count() == 0
    await assert_secrets_not_retained(page)


async def test_pick_known_agents_without_retyping_scoped_identity(operator_page):
    page, broker = operator_page
    await unlock(page)
    picker = page.get_by_label("Known agent", exact=True)
    await expect(picker.locator("option")).to_have_count(4)  # Placeholder and 3 exact identities.
    await picker.select_option(label="studio / team / writer")
    await page.get_by_role("button", name="Add selected agent", exact=True).click()
    await expect(page.get_by_label("Owner realm 1", exact=True)).to_have_value("studio")
    await expect(page.get_by_label("Owner namespace 1", exact=True)).to_have_value("team")
    await expect(page.get_by_label("Owner identity 1", exact=True)).to_have_value("writer")
    await picker.select_option(label="studio / other-team / artist")
    await page.get_by_role("button", name="Add selected agent", exact=True).click()
    # Selecting the same identity again must not add duplicate assignment rows.
    await picker.select_option(label="studio / team / writer")
    await page.get_by_role("button", name="Add selected agent", exact=True).click()
    await expect(page.locator(".owner-row")).to_have_count(2)
    if artifact_dir := os.environ.get("MEERKAT_CREDENTIAL_UI_ARTIFACT_DIR"):
        output = Path(artifact_dir)
        output.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(output / "known-agents-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "known-agents-mobile.png"), full_page=True)
    await page.get_by_label("Select Design account", exact=True).check()
    await page.get_by_role("button", name="Assign selected", exact=True).click()
    await expect(page.get_by_role("status")).to_contain_text("Assignments saved")
    assert next(call[2] for call in broker.calls if call[1] == "credentials/assign") == {
        "credential_ids": ["cred-one"],
        "owners": [SECOND_OWNER, {**OWNER, "namespace": "other-team"}],
    }
    await page.get_by_role("button", name="Lock", exact=True).click()
    await expect(picker.locator("option")).to_have_count(1)
    await assert_secrets_not_retained(page)


async def test_secure_request_error_clears_secret_and_success_saves_once(operator_page):
    page, broker = operator_page
    await unlock(page)
    card = page.locator("#request-request-one")
    await expect(card).to_be_visible()
    await card.get_by_label("Username", exact=True).fill("agent@example.test")
    await card.get_by_label("Password or code", exact=True).fill(SECRET)
    await card.get_by_label("Save for this identity", exact=True).check()
    await card.get_by_label("Credential label", exact=True).fill("Design login")
    broker.fail = ("POST", "credential-requests/request-one/fulfill")
    await card.get_by_role("button", name="Send securely", exact=True).click()
    await expect(card.get_by_label("Password or code", exact=True)).to_have_value("")
    await expect(card.get_by_role("button", name="Send securely", exact=True)).to_be_disabled()
    await expect(page.get_by_role("status")).to_contain_text("could not be confirmed")
    await assert_secrets_not_retained(page)
    assert len([call for call in broker.calls if call[1].endswith("/fulfill")]) == 1
    broker.fail = None
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await card.get_by_label("Password or code", exact=True).fill(SECRET)
    await card.get_by_label("Username", exact=True).fill("agent@example.test")
    await card.get_by_label("Save for this identity", exact=True).check()
    await card.get_by_label("Credential label", exact=True).fill("Design login")
    await card.get_by_role("button", name="Send securely", exact=True).click()
    await expect(card).to_contain_text("Delivered")
    calls = [call for call in broker.calls if call[1].endswith("/fulfill")]
    assert len(calls) == 2
    assert calls[-1][2] == {
        "values": {"username": "agent@example.test", "secret": SECRET},
        "save": {"label": "Design login"},
    }
    await assert_secrets_not_retained(page)


async def test_add_replace_delete_and_lock_are_outside_chat(operator_page):
    page, broker = operator_page
    await unlock(page)
    await page.get_by_role("button", name="Add credential", exact=True).click()
    dialog = page.get_by_role("dialog")
    await dialog.get_by_label("Realm", exact=True).fill("studio")
    await dialog.get_by_label("Label", exact=True).fill("New login")
    await dialog.get_by_label("Site origin", exact=True).fill("https://new.example")
    await dialog.get_by_label("Username", exact=True).fill("artist")
    await dialog.get_by_label("Password or code", exact=True).fill(SECRET)
    await dialog.get_by_role("button", name="Save credential", exact=True).click()
    await expect(page.get_by_text("New login", exact=True)).to_be_visible()
    card = page.locator(".credential-card").filter(has=page.get_by_text("Design account", exact=True))
    await card.get_by_role("button", name="Replace", exact=True).click()
    await expect(dialog.get_by_label("Password or code", exact=True)).to_have_value("")
    await dialog.get_by_label("Password or code", exact=True).fill(SECRET)
    await dialog.get_by_role("button", name="Save credential", exact=True).click()
    await expect(card).to_contain_text("Version 2")
    await card.get_by_role("button", name="Delete", exact=True).click()
    await expect(page.get_by_text("Design account", exact=True)).to_have_count(0)
    await page.get_by_role("button", name="Lock", exact=True).click()
    await expect(page.get_by_label("Operator token", exact=True)).to_have_value("")
    await expect(page.get_by_text("New login", exact=True)).to_have_count(0)
    await assert_secrets_not_retained(page)
    assert any(call[0] == "PUT" for call in broker.calls)
    assert any(call[0] == "DELETE" for call in broker.calls)


async def test_failed_save_and_unlock_never_render_server_secret_echo(operator_page):
    page, broker = operator_page
    await unlock(page)
    await page.get_by_role("button", name="Add credential", exact=True).click()
    dialog = page.get_by_role("dialog")
    await dialog.get_by_label("Realm", exact=True).fill("studio")
    await dialog.get_by_label("Label", exact=True).fill("New login")
    await dialog.get_by_label("Site origin", exact=True).fill("https://new.example")
    await dialog.get_by_label("Password or code", exact=True).fill(SECRET)
    broker.fail = ("POST", "credentials")
    await dialog.get_by_role("button", name="Save credential", exact=True).click()
    await expect(dialog.get_by_label("Password or code", exact=True)).to_have_value("")
    await expect(page.get_by_role("status")).to_contain_text("could not be confirmed")
    await assert_secrets_not_retained(page)
    assert len([call for call in broker.calls if call[0] == "POST"]) == 1


async def test_terminal_requests_cannot_send_and_session_requests_cannot_save(operator_page):
    page, broker = operator_page
    broker.requests[0]["phase"] = "unknown"
    await unlock(page)
    card = page.locator("#request-request-one")
    await expect(card).to_contain_text("Check the computer")
    await expect(card.get_by_role("button", name="Send securely")).to_have_count(0)
    broker.requests[0]["phase"] = "pending"
    broker.requests[0]["owner"] = {"kind": "session", "realm": "studio", "session_id": "session-1"}
    broker.requests[0]["target"].pop("username_ref")
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await expect(page.get_by_role("status")).to_contain_text("refreshed")
    await expect(card.get_by_label("Save for this identity", exact=True)).to_be_hidden()
    await card.get_by_label("Password or code", exact=True).fill(SECRET)
    await card.get_by_role("button", name="Send securely", exact=True).click()
    await expect(card).to_contain_text("Delivered")
    call = next(call for call in broker.calls if call[1].endswith("/fulfill"))
    assert call[2] == {"values": {"username": None, "secret": SECRET}, "save": None}
    await assert_secrets_not_retained(page)


async def test_request_history_does_not_bury_pending_input_or_reusable_credentials(operator_page):
    page, broker = operator_page
    pending = broker.requests[0]
    terminal_phases = ["delivered", "cancelled", "expired", "failed", "unknown"] * 5
    broker.requests = [
        {**pending, "id": f"past-{index}", "phase": phase}
        for index, phase in enumerate(terminal_phases)
    ] + [pending]
    await page.set_viewport_size({"width": 1280, "height": 900})
    await unlock(page)
    await page.evaluate("window.scrollTo(0, 0)")
    # A growing terminal history must not push the library thousands of pixels down.
    await expect(page.locator("#library-title")).to_be_in_viewport()
    await expect(page.locator("#request-request-one")).to_be_in_viewport()
    await expect(page.locator("#request-count")).to_have_text("1")
    history = page.locator("#request-history")
    await expect(page.locator("#request-history-count")).to_have_text("25")
    assert not await history.evaluate("element => element.open")
    await expect(page.locator("#request-past-0")).to_be_hidden()
    if artifact_dir := os.environ.get("MEERKAT_CREDENTIAL_UI_ARTIFACT_DIR"):
        output = Path(artifact_dir)
        output.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(output / "request-history-collapsed.png"), full_page=True)
    reads_before_expansion = len(broker.calls)
    await history.locator("summary").click()
    for index, phase in enumerate(terminal_phases):
        card = page.locator(f"#request-past-{index}")
        await expect(card).to_be_visible()
        await expect(card.locator(".phase")).to_have_text(phase.capitalize())
        await expect(card.locator("form")).to_have_count(0)
    assert len(broker.calls) == reads_before_expansion  # Disclosure is only a local view.
    await page.get_by_role("button", name="Lock", exact=True).click()
    await expect(page.locator(".request-card")).to_have_count(0)
    await expect(history).to_be_hidden()
    assert not await history.evaluate("element => element.open")
    await expect(page.locator("#request-history-count")).to_have_text("0")
    await assert_secrets_not_retained(page)
