"""Secret delivery stays outside model-visible tools, journals and observations."""

import asyncio
import hashlib
import json
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from meerkat_computer.protocol import Call, Lease, WorkerError
from meerkat_computer.secure_input import SecureInputEnvelope, create_secure_app, private_socket
from meerkat_computer.worker import Worker, create_app

TOKEN = "secure-input-fixture-" + "0" * 32
SECRET = "fixture-only-secret-A7!"
USERNAME = "fixture-account"


def lease(epoch=1):
    return Lease(
        lease_id=f"lease-{epoch}",
        epoch=epoch,
        expires_at_ms=9_000_000_000_000,
        capabilities=["browser", "screenshots", "browser_semantics", "exec", "files"],
        observation={"mode": "hybrid", "width": 640, "height": 480},
    )


def call(name, arguments=None, action_id="observation", epoch=1):
    return Call(
        lease_id=f"lease-{epoch}",
        epoch=epoch,
        action_id=action_id,
        name=name,
        arguments=arguments or {},
    )


def envelope(origin="https://example.test", observation_id="observed", **updates):
    body = {
        "lease": lease().model_dump(),
        "action_id": "secret-attempt",
        "target": {
            "origin": origin,
            "observation_id": observation_id,
            "secret_ref": "secret",
            "username_ref": "username",
        },
        "values": {"secret": SECRET, "username": USERNAME},
    }
    body.update(updates)
    return body


def view(result):
    assert result[0] == 200, result
    return json.loads(result[1]["content"][0]["text"])


class EmptyBrowser:
    async def start(self):
        pass

    async def close(self):
        pass

    async def configure(self, _):
        pass

    async def invalidate(self):
        pass

    def ready(self):
        return True


async def test_secret_http_endpoint_is_separate_and_validation_is_redacted(tmp_path):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN, EmptyBrowser())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(worker)), base_url="http://worker"
        ) as client:
            response = await client.post(
                "/v1/secure-input", json=envelope(), headers={"Authorization": "Bearer " + TOKEN}
            )
            assert response.status_code == 404
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_secure_app(worker)),
            base_url="http://guest-local",
        ) as client:
            response = await client.post("/v1/secure-input", json={**envelope(), "bad": SECRET})
            assert response.status_code == 422 and SECRET not in response.text
            response = await client.post("/v1/call", json={})
            assert response.status_code == 404
            response = await client.post("/v1/secure-input", json=envelope())
            assert response.status_code == 409 and SECRET not in response.text
    finally:
        await worker.close()


def test_socket_private_mode_and_refuses_existing_path():
    with tempfile.TemporaryDirectory(prefix="mc-uds-", dir="/tmp") as directory:
        check_private_socket(Path(directory) / "secure-input.sock")


def check_private_socket(path):
    with private_socket(path) as sock:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sock.getsockname() == str(path)
        with pytest.raises(ValueError), private_socket(path):
            pass
    assert not path.exists()
    path.write_text("preserve")
    with pytest.raises(ValueError), private_socket(path):
        pass
    assert path.read_text() == "preserve"


@pytest.fixture
def login_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path == "/done":
                self.wfile.write(b"<html><body><h1>Signed in fixture</h1></body></html>")
            else:
                self.wfile.write(b"""<!doctype html><html><body>
                <form method="post" action="/done"><label>Username<input id="username" name="username"></label>
                <label>Password<input id="password" type="password" name="password"></label>
                <button id="submit" type="button" onclick="location.href='/done'">Sign in</button></form>
                <div id="reflection"></div><script>
                password.addEventListener('input', () => {
                  reflection.textContent = password.value; password.type = 'text';
                });</script></body></html>""")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header("Location", "/done")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def browser_envelope(worker, origin, action_id="navigate"):
    observation = view(await worker.call(call("browser_navigate", {"url": origin}, action_id)))
    refs = {item["label"]: item["ref"] for item in observation["elements"]}
    body = envelope(origin, observation["observation_id"])
    body["target"].update(secret_ref=refs["Password"], username_ref=refs["Username"])
    return SecureInputEnvelope.model_validate(body), refs


@pytest.mark.browser
async def test_real_secure_fill_privacy_continuation_and_no_value_journaling(tmp_path, login_site):
    state = tmp_path / "state"
    worker = Worker(state, tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        request, refs = await browser_envelope(worker, login_site)
        result = await worker.secure_input(request)
        receipt = view(result)
        assert receipt["filled"] is True and receipt["submitted"] is False
        assert receipt["privacy"]["active"] is True
        assert refs["Sign in"] in receipt["action_refs"]
        assert refs["Password"] not in receipt["action_refs"]
        assert result[1]["metrics"]["delta"]["ui_commands"] == 2
        assert result[1]["metrics"]["delta"]["secure_fields_filled"] == 2
        assert await worker.browser.page.locator("#password").input_value() == SECRET
        assert await worker.browser.page.locator("#reflection").inner_text() == SECRET
        assert len(result[1]["content"]) == 1
        assert SECRET not in json.dumps(result) and USERNAME not in json.dumps(result)
        changed = request.model_copy(
            update={"values": request.values.model_copy(update={"secret": "a-different-secret"})}
        )
        assert await worker.secure_input(changed) == result
        assert await worker.browser.page.locator("#password").input_value() == SECRET
        guarded = await worker.call(call("computer_observe", action_id="guarded"))
        assert len(guarded[1]["content"]) == 1
        current = view(guarded)
        assert current["privacy"]["active"] is True
        assert SECRET not in json.dumps(guarded)
        await worker.browser.page.evaluate("history.pushState({}, '', '#same-document')")
        still_guarded = view(await worker.call(call("computer_observe", action_id="hash")))
        assert still_guarded["privacy"]["active"] is True
        clicked = await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": still_guarded["observation_id"],
                    "action": {"type": "click", "ref": refs["Sign in"]},
                },
                "submit",
            )
        )
        assert "Signed in fixture" in view(clicked)["accessibility"]
        assert len(clicked[1]["content"]) == 2
        worker.journal.db.execute("PRAGMA wal_checkpoint(FULL)")
        for path in state.glob("worker.sqlite3*"):
            data = path.read_bytes()
            for value in (SECRET, USERNAME, hashlib.sha256(SECRET.encode()).hexdigest()):
                assert value.encode() not in data
    finally:
        await worker.close()


@pytest.mark.browser
@pytest.mark.parametrize("input_type", ["submit", "button", "image"])
async def test_secure_input_preserves_input_actions(tmp_path, login_site, input_type):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        await browser_envelope(worker, login_site)
        await worker.browser.page.locator("#submit").evaluate(
            """(old, type) => {
                const control = document.createElement('input');
                control.type = type;
                control.value = 'Sign in';
                control.setAttribute('aria-label', 'Sign in');
                if (type === 'image') {
                    control.alt = 'Sign in';
                    control.width = 80;
                    control.height = 24;
                    control.src = 'data:image/svg+xml,' + encodeURIComponent(
                        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="24">' +
                        '<rect width="80" height="24" fill="blue"/></svg>');
                }
                if (type === 'button') {
                    control.addEventListener('click', () => control.form.requestSubmit());
                }
                old.replaceWith(control);
            }""",
            input_type,
        )
        observed = view(await worker.call(call("computer_observe", action_id="input-control")))
        refs = {item["label"]: item["ref"] for item in observed["elements"]}
        body = envelope(login_site, observed["observation_id"])
        body["target"].update(secret_ref=refs["Password"], username_ref=refs["Username"])
        result = await worker.secure_input(SecureInputEnvelope.model_validate(body))
        receipt = view(result)
        assert receipt["privacy"]["active"] is True
        assert len(result[1]["content"]) == 1
        assert SECRET not in json.dumps(result) and USERNAME not in json.dumps(result)
        assert refs["Sign in"] in receipt["action_refs"]
        for editable in ("Password", "Username"):
            assert refs[editable] not in receipt["action_refs"]
            with pytest.raises(WorkerError) as denied:
                await worker.call(
                    call(
                        "computer_act",
                        {
                            "observation_id": receipt["observation_id"],
                            "action": {"type": "click", "ref": refs[editable]},
                        },
                        "denied-" + editable,
                    )
                )
            assert denied.value.code == "stale_ref"
        clicked = await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": receipt["observation_id"],
                    "action": {"type": "click", "ref": refs["Sign in"]},
                },
                "submit-input",
            )
        )
        assert "Signed in fixture" in view(clicked)["accessibility"]
        assert len(clicked[1]["content"]) == 2
    finally:
        await worker.close()


@pytest.mark.browser
async def test_real_secure_validation_is_pre_admission_and_exact_origin(tmp_path, login_site):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        request, _ = await browser_envelope(worker, login_site)
        bad = request.model_copy(
            update={
                "target": request.target.model_copy(update={"origin": "https://elsewhere.test"})
            }
        )
        with pytest.raises(WorkerError) as denied:
            await worker.secure_input(bad)
        assert denied.value.effect == "none"
        assert await worker.browser.page.locator("#password").input_value() == ""
        await worker.browser.page.locator("form").evaluate(
            "el => el.action='https://elsewhere.test'"
        )
        with pytest.raises(WorkerError):
            await worker.secure_input(request)
        observed = view(await worker.call(call("computer_observe", action_id="unsafe-form")))
        body = request.model_dump()
        body["target"].update(
            observation_id=observed["observation_id"],
            secret_ref=next(x["ref"] for x in observed["elements"] if x["label"] == "Password"),
            username_ref=next(x["ref"] for x in observed["elements"] if x["label"] == "Username"),
        )
        with pytest.raises(WorkerError) as denied:
            await worker.secure_input(SecureInputEnvelope.model_validate(body))
        assert denied.value.code == "secure_target_denied"
        assert await worker.browser.page.locator("#password").input_value() == ""
        await worker.grant(lease(2))
        with pytest.raises(WorkerError) as denied:
            await worker.secure_input(request)
        assert denied.value.code == "stale_lease"
    finally:
        await worker.close()


async def test_real_unix_transport_starts_worker_once_and_removes_socket():
    class CountingBrowser(EmptyBrowser):
        starts = 0
        closes = 0

        async def start(self):
            self.starts += 1

        async def close(self):
            self.closes += 1

    with tempfile.TemporaryDirectory(prefix="mc-listen-", dir="/tmp") as directory:
        state = Path(directory) / "state"
        browser = CountingBrowser()
        worker = Worker(state, Path(directory) / "workspace", TOKEN, browser)
        app = create_app(worker)
        async with app.router.lifespan_context(app):
            assert browser.starts == 1
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(state / "secure-input.sock")),
                base_url="http://guest-local",
            ) as client:
                response = await client.post("/v1/secure-input", json=envelope())
                assert response.status_code == 409
                assert SECRET not in response.text
        assert browser.closes == 1
        assert not (state / "secure-input.sock").exists()


@pytest.mark.browser
async def test_secure_input_rechecks_expiry_between_fields(tmp_path, login_site):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        request, refs = await browser_envelope(worker, login_site)
        field = worker.browser.refs[refs["Username"]]
        original = field.fill

        async def expire_after_first(value):
            await original(value)
            worker.clock = lambda: 10_000_000_000

        field.fill = expire_after_first
        status, body = await worker.secure_input(request)
        assert status == 409 and body["error"]["effect"] == "unknown"
        assert await worker.browser.page.locator("#password").input_value() == ""
        assert SECRET not in json.dumps(body) and USERNAME not in json.dumps(body)
    finally:
        await worker.close()


@pytest.mark.browser
async def test_form_submit_override_is_denied(tmp_path, login_site):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        await browser_envelope(worker, login_site)
        await worker.browser.page.locator("#submit").evaluate(
            "el => {el.type='submit';el.setAttribute('formaction','https://elsewhere.test');}"
        )
        observed = view(await worker.call(call("computer_observe", action_id="formaction")))
        refs = {item["label"]: item["ref"] for item in observed["elements"]}
        body = envelope(login_site, observed["observation_id"])
        body["target"].update(secret_ref=refs["Password"], username_ref=refs["Username"])
        with pytest.raises(WorkerError) as denied:
            await worker.secure_input(SecureInputEnvelope.model_validate(body))
        assert denied.value.code == "secure_target_denied"
        assert await worker.browser.page.locator("#password").input_value() == ""
    finally:
        await worker.close()


@pytest.mark.browser
@pytest.mark.parametrize("input_type", ["submit", "image"])
@pytest.mark.parametrize("outside_form", [False, True])
@pytest.mark.parametrize(
    ("attribute", "value"), [("formaction", "https://elsewhere.test"), ("formmethod", "get")]
)
async def test_input_action_destination_override_is_denied(
    tmp_path, login_site, input_type, outside_form, attribute, value
):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        await browser_envelope(worker, login_site)
        await worker.browser.page.locator("#submit").evaluate(
            """(old, options) => {
                const control = document.createElement('input');
                control.type = options.type;
                control.setAttribute(options.attribute, options.value);
                if (options.outside) {
                    old.form.id = 'login';
                    control.setAttribute('form', 'login');
                    document.body.append(control);
                    old.remove();
                } else {
                    old.replaceWith(control);
                }
            }""",
            {"type": input_type, "outside": outside_form, "attribute": attribute, "value": value},
        )
        observed = view(await worker.call(call("computer_observe", action_id="input-override")))
        refs = {item["label"]: item["ref"] for item in observed["elements"]}
        body = envelope(login_site, observed["observation_id"])
        body["target"].update(secret_ref=refs["Password"], username_ref=refs["Username"])
        with pytest.raises(WorkerError) as denied:
            await worker.secure_input(SecureInputEnvelope.model_validate(body))
        assert denied.value.code == "secure_target_denied"
        assert denied.value.effect == "none"
        assert await worker.browser.page.locator("#password").input_value() == ""
        assert await worker.browser.page.locator("#username").input_value() == ""
    finally:
        await worker.close()


@pytest.mark.browser
@pytest.mark.parametrize("override", [False, True])
async def test_get_form_cannot_put_credentials_into_navigation_url(tmp_path, login_site, override):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        await browser_envelope(worker, login_site)
        if override:
            await worker.browser.page.locator("#submit").evaluate(
                "el => {el.type='submit';el.setAttribute('formmethod','get');}"
            )
        else:
            await worker.browser.page.locator("form").evaluate("el => el.method='get'")
        observed = view(await worker.call(call("computer_observe", action_id="get-form")))
        refs = {item["label"]: item["ref"] for item in observed["elements"]}
        body = envelope(login_site, observed["observation_id"])
        body["target"].update(secret_ref=refs["Password"], username_ref=refs["Username"])
        with pytest.raises(WorkerError) as denied:
            await worker.secure_input(SecureInputEnvelope.model_validate(body))
        assert denied.value.code == "secure_target_denied"
        assert await worker.browser.page.locator("#password").input_value() == ""
    finally:
        await worker.close()


@pytest.mark.browser
async def test_pending_secret_is_not_replayed_and_privacy_survives_restart(tmp_path, login_site):
    state = tmp_path / "state"
    worker = Worker(state, tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(lease())
        request, refs = await browser_envelope(worker, login_site)
        secret = worker.browser.refs[refs["Password"]]

        async def cancelled_fill(value):
            raise asyncio.CancelledError

        secret.fill = cancelled_fill
        with pytest.raises(asyncio.CancelledError):
            await worker.secure_input(request)
        with pytest.raises(WorkerError) as pending:
            await worker.secure_input(request)
        assert pending.value.code == "action_effect_unknown"
        assert pending.value.effect == "unknown"
    finally:
        await worker.close()
    restarted = Worker(state, tmp_path / "workspace", TOKEN)
    await restarted.start()
    try:
        with pytest.raises(WorkerError) as pending:
            await restarted.secure_input(request)
        assert pending.value.code == "action_effect_unknown"
        hidden = await restarted.call(call("computer_observe", action_id="after-restart"))
        assert len(hidden[1]["content"]) == 1 and view(hidden)["privacy"]["active"]
        visible = await restarted.call(
            call("browser_navigate", {"url": login_site + "/done"}, "new-doc")
        )
        assert "Signed in fixture" in view(visible)["accessibility"]
    finally:
        await restarted.close()


@pytest.mark.browser
@pytest.mark.parametrize(
    "projection",
    [
        {},
        {
            "detail": "full",
            "max_elements": 300,
            "max_text_chars": 24000,
            "scope_ref": "prior-native-ref",
            "observation_id": "prior-observation",
        },
    ],
)
async def test_desktop_observation_is_withheld_and_errors_never_echo_secret(
    tmp_path, login_site, projection
):
    class SpyDesktop(EmptyBrowser):
        captures = 0

        async def validate(self, *_):
            pass

        async def perform(self, *_):
            self.captures += 1
            raise AssertionError("Desktop must not capture while a secret is visible")

    desktop = SpyDesktop()
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN, desktop=desktop)
    await worker.start()
    try:
        granted = lease().model_copy(
            update={"capabilities": [*lease().capabilities, "desktop", "desktop_semantics"]}
        )
        await worker.grant(granted)
        request, refs = await browser_envelope(worker, login_site)
        request = request.model_copy(update={"lease": granted})
        secret = worker.browser.refs[refs["Password"]]

        async def failed_fill(value):
            raise RuntimeError(f"Unexpected browser diagnostics include {value}")

        secret.fill = failed_fill
        status, body = await worker.secure_input(request)
        assert status == 409 and body["error"]["effect"] == "unknown"
        assert SECRET not in json.dumps(body) and USERNAME not in json.dumps(body)
        observed = await worker.call(
            call("computer_observe", {"surface": "desktop", **projection}, "private-desktop")
        )
        assert len(observed[1]["content"]) == 1 and view(observed)["privacy"]["active"]
        assert desktop.captures == 0
        assert SECRET not in json.dumps(observed) and USERNAME not in json.dumps(observed)
        with pytest.raises(WorkerError):
            await worker.call(
                call(
                    "computer_act",
                    {
                        "observation_id": view(observed)["observation_id"],
                        "action": {"type": "key", "key": "Enter"},
                    },
                    "no-blind-enter",
                )
            )
    finally:
        await worker.close()
