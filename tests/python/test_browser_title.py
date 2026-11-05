"""The active document's title, from real local Chromium and synthetic pages only.

The title is exposed only where the lease already exposes the page's URL and text
(semantic and hybrid), only for the active tab, and never while secure input is
protecting a document. Background tabs are never read.
"""

import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from meerkat_computer.protocol import Call, Lease
from meerkat_computer.worker import Worker
from test_secure_input import browser_envelope
from test_secure_input import lease as secure_lease

pytestmark = pytest.mark.browser
TOKEN = "browser-title-fixture-" + "0" * 32
LOGIN_FORM = """<form method="post" action="/done"><label>Username<input id="username"
  name="username"></label><label>Password<input id="password" type="password"
  name="password"></label><button id="submit" type="button"
  onclick="location.href='/done'">Sign in</button></form>"""
PAGES = {
    "/titled": "<title>  Window \n\t Title  </title><h1>Page Heading</h1>"
    "<button onclick=\"document.title='Changed  by script'\">Rename</button>",
    "/second": "<title>Second tab title</title><h1>Second heading</h1>",
    "/none": "<h1>No title element</h1>",
    "/empty": "<title></title><h1>Empty title</h1>",
    "/spaces": "<title> \n\t&nbsp; </title><h1>Whitespace title</h1>",
    "/long": "<title>" + "word " * 2000 + "</title><h1>Long title</h1>",
    "/emoji": "<title>" + "\U0001f600" * 400 + "</title><h1>Wide characters</h1>",
    "/": "<title>Sign-in window</title>" + LOGIN_FORM,
    "/done": "<title>Done window</title><h1>Signed in fixture</h1>",
}


@pytest.fixture
def site():
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests[self.path] += 1
            body = PAGES.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<!doctype html><html><body>{body or ''}</body></html>".encode())

        def do_POST(self):
            requests["POST " + self.path] += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header("Location", "/done")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def grant(mode="hybrid", epoch=1, **observation):
    caps = {
        "visual": ["browser", "screenshots"],
        "semantic": ["browser", "browser_semantics"],
        "hybrid": ["browser", "screenshots", "browser_semantics"],
    }[mode]
    return Lease(
        lease_id=f"lease-{epoch}",
        epoch=epoch,
        expires_at_ms=9_000_000_000_000,
        capabilities=caps,
        observation={"mode": mode, "width": 640, "height": 480, **observation},
    )


class Session:
    def __init__(self, worker, epoch=1):
        self.worker, self.epoch, self.count = worker, epoch, 0

    async def raw(self, name, arguments=None):
        self.count += 1
        return await self.worker.call(
            Call(
                lease_id=f"lease-{self.epoch}",
                epoch=self.epoch,
                name=name,
                arguments=arguments or {},
                action_id=f"{name}-{self.count}",
            )
        )

    async def body(self, name, arguments=None):
        status, response = await self.raw(name, arguments)
        assert status == 200, response
        return json.loads(response["content"][0]["text"])

    async def observe(self, **options):
        return await self.body("computer_observe", options)

    async def navigate(self, url):
        return await self.body("browser_navigate", {"url": url})

    async def tabs(self):
        return await self.body("browser_tabs", {"action": "list"})


@pytest.fixture
async def worker(tmp_path):
    value = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await value.start()
    try:
        yield value
    finally:
        await value.close()


async def session(worker, mode="hybrid", **observation):
    await worker.grant(grant(mode, **observation))
    return Session(worker)


def active(tabs):
    (entry,) = [tab for tab in tabs["tabs"] if tab["active"]]
    return entry


def assert_document(document, title):
    assert document["title"] == title
    assert document["title_truncated"] is False
    assert document["ready_state"] in {"interactive", "complete"}
    assert re.fullmatch(r"[0-9a-f]{32}", document["document_id"])
    assert isinstance(document["observed_at_ms"], int) and document["observed_at_ms"] > 0


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_title_is_the_document_title_not_the_heading(worker, site, mode):
    origin, _ = site
    view = await (await session(worker, mode)).navigate(origin + "/titled")
    assert_document(view["document"], "Window Title")  # Whitespace runs collapse.
    assert "Page Heading" in view["accessibility"]
    assert view["document"]["title"] != "Page Heading"
    entry = active(await Session(worker).tabs())
    assert entry["title"] == "Window Title"
    assert entry["document_id"] == view["document"]["document_id"]
    assert entry["url"] == view["url"] == origin + "/titled"


@pytest.mark.parametrize("path", ["/none", "/empty", "/spaces"])
async def test_missing_or_blank_title_is_null(worker, site, path):
    origin, _ = site
    browser = await session(worker)
    view = await browser.navigate(origin + path)
    assert view["document"]["title"] is None
    assert view["document"]["title_truncated"] is False
    entry = active(await browser.tabs())
    assert entry["title"] is None and entry["document_id"] == view["document"]["document_id"]


async def test_script_title_change_keeps_the_document_identity(worker, site):
    origin, _ = site
    browser = await session(worker)
    first = await browser.navigate(origin + "/titled")
    rename = next(e["ref"] for e in first["elements"] if e["label"] == "Rename")
    changed = await browser.body(
        "computer_act",
        {"observation_id": first["observation_id"], "action": {"type": "click", "ref": rename}},
    )
    assert changed["document"]["title"] == "Changed by script"
    assert changed["document"]["document_id"] == first["document"]["document_id"]
    assert active(await browser.tabs())["title"] == "Changed by script"
    other = await browser.navigate(origin + "/second")
    assert other["document"]["title"] == "Second tab title"
    assert other["document"]["document_id"] != first["document"]["document_id"]


async def test_long_title_is_bounded_and_charged_to_the_text_budget(worker, site):
    origin, _ = site
    browser = await session(worker)
    compact = await browser.navigate(origin + "/long")
    assert len(compact["document"]["title"]) == 160
    assert compact["document"]["title"] == ("word " * 40).strip()[:160]
    assert compact["document"]["title_truncated"] is True and compact["truncated"] is True
    full = await browser.observe(detail="full")
    assert len(full["document"]["title"]) == 300 and full["document"]["title_truncated"]
    tight = await browser.observe(max_text_chars=256)
    assert len(tight["document"]["title"]) == 160
    assert tight["projection"]["text_chars"] <= 256
    assert tight["projection"]["truncated_fields"] >= 1
    text = tight["accessibility"] + "".join(
        str(e.get(f, "")) for e in tight["elements"] for f in ("role", "label", "value")
    )
    assert len(text) + len(tight["document"]["title"]) == tight["projection"]["text_chars"]
    entry = active(await browser.tabs())
    assert len(entry["title"]) == 160 and entry["title_truncated"] is True


async def test_title_is_cut_on_code_points(worker, site):
    origin, _ = site
    browser = await session(worker)
    view = await browser.navigate(origin + "/emoji")
    assert view["document"]["title"] == "\U0001f600" * 160  # No half surrogate pair.
    full = await browser.observe(detail="full")
    assert full["document"]["title"] == "\U0001f600" * 300
    assert full["document"]["title_truncated"] is True


def instrument(page, monkeypatch):
    calls = Counter()
    for name in (
        "evaluate",
        "evaluate_handle",
        "title",
        "content",
        "bring_to_front",
        "goto",
        "reload",
        "query_selector_all",
        "locator",
        "screenshot",
        "focus",
        "wait_for_load_state",
    ):
        original = getattr(page, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(page, name, wrapper)
    return calls


async def test_background_tab_is_never_read_and_keeps_its_shape(worker, site, monkeypatch):
    origin, requests = site
    browser = await session(worker)
    await browser.navigate(origin + "/titled")
    background_page = worker.browser.page
    await browser.body("browser_tabs", {"action": "new", "url": origin + "/second"})
    calls = instrument(background_page, monkeypatch)
    before = dict(requests)
    listed = await browser.tabs()
    assert dict(requests) == before  # Listing makes no request in any tab.
    assert sum(calls.values()) == 0  # No evaluate, focus or navigation in the background.
    assert worker.browser.page is not background_page
    (background,) = [tab for tab in listed["tabs"] if not tab["active"]]
    key = next(k for k, page in worker.browser.tabs.items() if page is background_page)
    expected = {"tab_id": key, "url": origin + "/titled", "active": False}
    assert json.dumps(background) == json.dumps(expected)
    assert active(listed)["title"] == "Second tab title"
    assert "Window Title" not in json.dumps(listed)


async def test_visual_lease_exposes_no_url_or_title(worker, site, monkeypatch):
    origin, requests = site
    browser = await session(worker, "visual")
    status, response = await browser.raw("browser_navigate", {"url": origin + "/titled"})
    assert status == 200
    view = json.loads(response["content"][0]["text"])
    assert "url" not in view and "document" not in view and "title" not in view
    assert "Window" not in response["content"][0]["text"]
    assert response["content"][1]["type"] == "image"
    calls = instrument(worker.browser.page, monkeypatch)
    before = dict(requests)
    listed = await browser.tabs()
    assert sum(calls.values()) == 0 and dict(requests) == before
    assert [set(tab) for tab in listed["tabs"]] == [{"tab_id", "url", "active"}]


async def protected(worker, origin):
    """Fill a synthetic login form so secure input protects that document."""
    await worker.grant(secure_lease())  # Secure input needs its exact hybrid grant.
    request, _ = await browser_envelope(worker, origin)
    status, receipt = await worker.secure_input(request)
    assert status == 200 and json.loads(receipt["content"][0]["text"])["privacy"]["active"]


def assert_withheld(text):
    assert '"privacy"' in text and '"active": true' in text
    for leaked in ("Sign-in window", "Window Title", "Second tab title", "document_id", "url"):
        assert leaked not in text


async def test_secure_input_withholds_titles_on_every_tab_and_listing(worker, site):
    origin, _ = site
    await protected(worker, origin)
    browser = Session(worker)
    status, response = await browser.raw(
        "browser_tabs", {"action": "new", "url": origin + "/titled"}
    )
    assert status == 200 and len(response["content"]) == 1
    assert_withheld(response["content"][0]["text"])
    for name, arguments in (("computer_observe", {}), ("browser_tabs", {"action": "list"})):
        status, response = await browser.raw(name, arguments)
        assert status == 200 and len(response["content"]) == 1
        assert_withheld(response["content"][0]["text"])


@pytest.mark.parametrize("path", ["observe", "list"])
async def test_protection_starting_during_capture_discards_it(worker, site, monkeypatch, path):
    origin, _ = site
    browser = await session(worker)
    await browser.navigate(origin + "/titled")
    real = worker.browser.document_snapshot

    async def protect_mid_capture(page):
        result = await real(page)
        await worker.secure.protect()  # Protection starts after the title was read.
        return result

    monkeypatch.setattr(worker.browser, "document_snapshot", protect_mid_capture)
    name, arguments = (
        ("computer_observe", {}) if path == "observe" else ("browser_tabs", {"action": "list"})
    )
    status, response = await browser.raw(name, arguments)
    assert status == 200 and len(response["content"]) == 1  # No screenshot either.
    assert_withheld(response["content"][0]["text"])
    if path == "observe":
        assert worker.browser.target_snapshot is None and worker.browser.refs == {}


@pytest.mark.parametrize("path", ["observe", "list"])
async def test_failed_privacy_check_after_capture_fails_closed(worker, site, monkeypatch, path):
    origin, _ = site
    browser = await session(worker)
    await browser.navigate(origin + "/titled")
    real_snapshot, real_guarded = worker.browser.document_snapshot, worker.secure.guarded
    captured = False

    async def snapshot(page):
        nonlocal captured
        captured = True
        return await real_snapshot(page)

    async def guarded():
        if captured:
            raise RuntimeError("synthetic privacy check failure")
        return await real_guarded()

    monkeypatch.setattr(worker.browser, "document_snapshot", snapshot)
    monkeypatch.setattr(worker.secure, "guarded", guarded)
    name, arguments = (
        ("computer_observe", {}) if path == "observe" else ("browser_tabs", {"action": "list"})
    )
    status, response = await browser.raw(name, arguments)
    assert status != 200 and response["error"]["code"] == "privacy_guarded"
    text = json.dumps(response)
    assert "Window Title" not in text and "synthetic" not in text and "content" not in response
    if path == "observe":
        assert worker.browser.observation_id == "" and worker.browser.target_snapshot is None


async def test_unverifiable_protected_document_stays_withheld(worker, site, monkeypatch):
    origin, _ = site
    await protected(worker, origin)
    browser = Session(worker)
    status, _ = await browser.raw("browser_tabs", {"action": "new", "url": origin + "/titled"})
    assert status == 200

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic evaluate failure")

    # An evaluate error in the protected (now background) tab is not proof that
    # the protected document is gone, so the active tab stays withheld too.
    monkeypatch.setattr(worker.secure.page, "evaluate", broken)
    for name, arguments in (("computer_observe", {}), ("browser_tabs", {"action": "list"})):
        status, response = await browser.raw(name, arguments)
        assert status == 200
        assert_withheld(response["content"][0]["text"])


async def test_restart_restores_protection_until_a_new_document(tmp_path, site):
    origin, _ = site
    first = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await first.start()
    try:
        await protected(first, origin)
    finally:
        await first.close()
    restarted = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await restarted.start()
    try:
        browser = Session(restarted)
        for name, arguments in (("computer_observe", {}), ("browser_tabs", {"action": "list"})):
            status, response = await browser.raw(name, arguments)
            assert status == 200
            assert_withheld(response["content"][0]["text"])
        released = await browser.navigate(origin + "/done")
        assert released["document"]["title"] == "Done window"
    finally:
        await restarted.close()


async def test_incomplete_restore_stays_withheld_even_after_navigation(tmp_path, site):
    origin, _ = site
    first = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await first.start()
    try:
        await protected(first, origin)
    finally:
        await first.close()
    restarted = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await restarted.start()
    try:
        # The restore never anchored a document: that is not proof the old one is gone.
        restarted.secure.page = restarted.secure.marker = None
        browser = Session(restarted)
        for name, arguments in (
            ("computer_observe", {}),
            ("browser_tabs", {"action": "list"}),
            ("browser_navigate", {"url": origin + "/done"}),
            ("browser_tabs", {"action": "list"}),
        ):
            status, response = await browser.raw(name, arguments)
            assert status == 200
            assert_withheld(response["content"][0]["text"])
            assert "Done window" not in response["content"][0]["text"]
    finally:
        await restarted.close()


async def test_leaving_the_protected_document_releases_titles(worker, site):
    origin, _ = site
    await protected(worker, origin)
    browser = Session(worker)
    view = await browser.navigate(origin + "/done")
    assert "privacy" not in view
    assert_document(view["document"], "Done window")
    assert active(await browser.tabs())["title"] == "Done window"


@pytest.mark.parametrize("path", ["observe", "list"])
async def test_navigation_before_the_title_read_never_publishes_a_stale_title(
    worker, site, monkeypatch, path
):
    origin, _ = site
    browser = await session(worker)
    await browser.navigate(origin + "/titled")
    real = worker.browser.document_snapshot
    navigations = []

    async def navigate_then_read(page):
        if not navigations:
            # The fence was opened on /titled; the title is read from /second.
            navigations.append(page.url)
            await page.goto(origin + "/second", wait_until="domcontentloaded")
        return await real(page)

    monkeypatch.setattr(worker.browser, "document_snapshot", navigate_then_read)
    if path == "observe":
        view = await browser.observe()
        document, url = view["document"], view["url"]
    else:
        document = active(await browser.tabs())
        url = document["url"]
    assert navigations == [origin + "/titled"]
    assert document["title"] == "Second tab title" and url == origin + "/second"
    monkeypatch.undo()
    assert document["document_id"] == (await browser.observe())["document"]["document_id"]


@pytest.mark.parametrize("path", ["observe", "list"])
async def test_navigation_after_the_title_read_is_never_published(worker, site, monkeypatch, path):
    origin, _ = site
    browser = await session(worker)
    await browser.navigate(origin + "/titled")
    real = worker.browser.document_snapshot
    navigations = []

    async def read_then_navigate(page):
        result = await real(page)
        if not navigations:
            navigations.append(page.url)
            await page.goto(origin + "/second", wait_until="domcontentloaded")
        return result

    monkeypatch.setattr(worker.browser, "document_snapshot", read_then_navigate)
    if path == "observe":
        # The old document's capture handles die with it: the read fails closed.
        status, response = await browser.raw("computer_observe")
        assert status != 200 and "content" not in response
        assert "Window Title" not in json.dumps(response)
    else:
        # The after-fence sees the new document and reads again.
        entry = active(await browser.tabs())
        assert entry["title"] == "Second tab title" and entry["url"] == origin + "/second"
    assert navigations == [origin + "/titled"]


@pytest.mark.parametrize("path", ["observe", "list"])
async def test_document_that_keeps_changing_publishes_no_title(worker, site, monkeypatch, path):
    origin, _ = site
    browser = await session(worker)
    await browser.navigate(origin + "/titled")
    real = worker.browser.document_snapshot
    targets = iter([origin + "/second", origin + "/titled"] * 3)

    async def always_navigating(page):
        await page.goto(next(targets), wait_until="domcontentloaded")
        return await real(page)

    monkeypatch.setattr(worker.browser, "document_snapshot", always_navigating)
    if path == "observe":
        status, response = await browser.raw("computer_observe")
        assert status != 200 and response["error"]["code"] == "observation_unstable"
        assert "Second tab title" not in json.dumps(response)
    else:
        entry = active(await browser.tabs())
        assert entry == {
            "tab_id": entry["tab_id"],
            "url": entry["url"],
            "active": True,
            "title": None,
            "title_unavailable": "document_changed",
        }
