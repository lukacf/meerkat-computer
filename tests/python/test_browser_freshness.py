"""Real Chromium freshness checks against synthetic changing pages only."""

import asyncio
import json
from pathlib import Path

import pytest
from meerkat_computer.protocol import Call, Lease, WorkerError
from meerkat_computer.worker import Worker

pytestmark = pytest.mark.browser
HTML = """<!doctype html><style>body{margin:0}button,input{display:block;width:180px;height:40px}
#tick{position:fixed;left:350px;top:10px}#space{height:1800px}</style>
<button id='target' onclick='window.clicks=(window.clicks||0)+1'>Continue</button>
<input aria-label='Name' id='field'><p id='tick'>0</p><div id='space'></div>
<script>window.ticks=0;setInterval(()=>{tick.textContent=++window.ticks},25)</script>"""


@pytest.fixture
async def worker(tmp_path):
    value = Worker(tmp_path / "state", tmp_path / "workspace", "freshness-" + "0" * 40)
    await value.start()
    await value.grant(
        Lease(
            lease_id="freshness",
            epoch=1,
            expires_at_ms=9_000_000_000_000,
            capabilities=["browser", "screenshots", "browser_semantics"],
            observation={"mode": "hybrid", "width": 640, "height": 480},
        )
    )
    await value.browser.page.set_content(HTML)
    try:
        yield value
    finally:
        await value.close()


def call(name, arguments=None, action_id="observe"):
    return Call(
        lease_id="freshness", epoch=1, name=name, arguments=arguments or {}, action_id=action_id
    )


async def observe(worker, action_id="observe"):
    status, body = await worker.call(call("computer_observe", action_id=action_id))
    assert status == 200, body.get("error")
    return json.loads(body["content"][0]["text"])


async def tick(worker):
    before = await worker.browser.page.evaluate("ticks")
    await worker.browser.page.wait_for_function("before => ticks > before", arg=before)


@pytest.mark.parametrize("navigate", [False, True])
async def test_transparent_control_does_not_prevent_browser_observation(worker, navigate):
    html = HTML + """<a href="#content"
        style="position:fixed;left:200px;top:100px;opacity:0">Skip content</a>"""
    if navigate:
        async def page(route):
            await route.fulfill(status=200, content_type="text/html", body=html)

        await worker.browser.page.route("http://capture.invalid/**", page)
        status, body = await worker.call(call(
            "browser_navigate", {"url": "http://capture.invalid/"}, "navigate"
        ))
        assert status == 200, body.get("error")
        assert body["metrics"]["delta"]["navigations"] == 1
        assert body["content"][1]["mimeType"] == "image/png"
        view = json.loads(body["content"][0]["text"])
    else:
        await worker.browser.page.set_content(html)
        view = await observe(worker)
    assert all(item["label"] != "Skip content" for item in view["elements"])
    ref = next(item["ref"] for item in view["elements"] if item["label"] == "Continue")
    status, body = await worker.call(call(
        "computer_act",
        {"observation_id": view["observation_id"], "action": {"type": "click", "ref": ref}},
        "click-visible-control",
    ))
    assert status == 200, body.get("error")
    assert await worker.browser.page.evaluate("window.clicks") == 1
    assert body["metrics"]["delta"]["clicks"] == 1


@pytest.mark.parametrize("target", ["ref", "coordinates", "key", "scroll"])
async def test_unrelated_timer_does_not_stale_observed_input(worker, target):
    if target == "key":
        await worker.browser.page.locator("#field").focus()
    view = await observe(worker)
    await tick(worker)
    ref = next(item["ref"] for item in view["elements"] if item["label"] == "Continue")
    action = {
        "ref": {"type": "click", "ref": ref},
        "coordinates": {"type": "click", "x": 90, "y": 20},
        "key": {"type": "key", "key": "a"},
        "scroll": {"type": "scroll", "delta_y": 220},
    }[target]
    status, body = await worker.call(
        call(
            "computer_act",
            {
                "observation_id": view["observation_id"],
                "action": action,
            },
            "act",
        )
    )
    assert status == 200, body.get("error")
    assert body["metrics"]["delta"]["ui_commands"] == 1
    if target in {"ref", "coordinates"}:
        assert await worker.browser.page.evaluate("window.clicks") == 1
        assert body["metrics"]["delta"]["clicks"] == 1
    elif target == "key":
        assert await worker.browser.page.locator("#field").input_value() == "a"
    else:
        await worker.browser.page.wait_for_function("scrollY > 0")


@pytest.mark.parametrize("target", ["ref", "coordinates"])
@pytest.mark.parametrize(
    "change",
    [
        "target.outerHTML=target.outerHTML",
        "target.textContent='Different action'",
        "target.disabled=true",
        "target.style.transform='translateY(40px)'",
        "document.body.insertAdjacentHTML('beforeend', '<div style=\"position:fixed;inset:0;background:white\">Overlay</div>')",
        "history.pushState({},'', '#new-context')",
    ],
)
async def test_changed_target_or_context_fails_before_admission(worker, target, change):
    view = await observe(worker)
    ref = next(item["ref"] for item in view["elements"] if item["label"] == "Continue")
    await worker.browser.page.evaluate(change)
    action = (
        {"type": "click", "ref": ref} if target == "ref" else {"type": "click", "x": 90, "y": 20}
    )
    with pytest.raises(WorkerError) as error:
        await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": action,
                },
                "stale",
            )
        )
    assert error.value.effect == "none"
    assert worker.journal.metrics().get("ui_command_attempts", 0) == 0
    assert await worker.browser.page.evaluate("window.clicks || 0") == 0


async def test_changed_focus_and_tab_order_fail_before_input(worker):
    await worker.browser.page.locator("#field").focus()
    view = await observe(worker)
    await worker.browser.page.locator("#target").focus()
    with pytest.raises(WorkerError):
        await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "key", "key": "a"},
                },
                "focus-changed",
            )
        )


@pytest.mark.parametrize(
    "change",
    [
        "document.body.insertAdjacentHTML('afterbegin','<button>New first target</button>')",
        "target.setAttribute('tabindex','-1')",
    ],
)
async def test_changed_tab_navigation_context_fails(worker, change):
    view = await observe(worker)
    await worker.browser.page.evaluate(change)
    with pytest.raises(WorkerError):
        await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "key", "key": "Tab"},
                },
                "tab-changed",
            )
        )


@pytest.mark.parametrize("change", ["document", "viewport", "tab"])
async def test_same_url_document_viewport_and_tab_changes_stay_fenced(worker, change):
    view = await observe(worker)
    if change == "document":
        await worker.browser.page.set_content(HTML)
    elif change == "viewport":
        await worker.browser.page.set_viewport_size({"width": 650, "height": 480})
    else:
        worker.browser.page = await worker.browser.context.new_page()
        await worker.browser.page.set_content(HTML)
    with pytest.raises(WorkerError):
        await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "click", "x": 90, "y": 20},
                },
                "context-changed",
            )
        )


async def test_unrelated_layout_below_viewport_does_not_stale_target(worker):
    view = await observe(worker)
    await worker.browser.page.evaluate("space.style.height='2000px'")
    status, body = await worker.call(
        call(
            "computer_act",
            {
                "observation_id": view["observation_id"],
                "action": {"type": "click", "x": 90, "y": 20},
            },
            "click-stable",
        )
    )
    assert status == 200, body.get("error")
    assert await worker.browser.page.evaluate("clicks") == 1


async def test_browser_download_available_in_observed_guest_directory(worker):
    await worker.browser.page.set_content(
        '<a download="example.txt" href="data:text/plain,synthetic-download">Download</a>'
    )
    view = await observe(worker)
    ref = next(item["ref"] for item in view["elements"] if item["label"] == "Download")
    async with worker.browser.page.expect_download() as event:
        status, body = await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "click", "ref": ref},
                },
                "download",
            )
        )
    assert status == 200, body.get("error")
    download = await event.value
    path = await download.path()
    assert path.parent == Path(view["downloads_directory"])
    assert path.read_text() == "synthetic-download"
    assert body["metrics"]["delta"]["clicks"] == 1


async def test_native_label_change_fences_fill(worker):
    await worker.browser.page.set_content(
        '<label for="field" style="position:fixed;left:300px">First name</label><input id="field">'
    )
    view = await observe(worker)
    ref = next(item["ref"] for item in view["elements"] if item["label"] == "First name")
    await worker.browser.page.locator("label").evaluate("el => el.textContent='Other name'")
    with pytest.raises(WorkerError) as error:
        await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "fill", "ref": ref, "text": "Alice"},
                },
                "label-changed",
            )
        )
    assert error.value.effect == "none"
    assert await worker.browser.page.locator("#field").input_value() == ""


async def test_completed_downloads_have_safe_unique_durable_paths(worker):
    await worker.browser.page.set_content(
        '<a download="../../report.txt" href="data:text/plain,synthetic-download">Download</a>'
    )
    for index in range(2):
        view = await observe(worker, f"observe-download-{index}")
        ref = next(item["ref"] for item in view["elements"] if item["label"] == "Download")
        status, body = await worker.call(
            call(
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "click", "ref": ref},
                },
                f"persist-download-{index}",
            )
        )
        assert status == 200, body.get("error")
    await asyncio.wait_for(asyncio.gather(*worker.browser.download_tasks), 5)
    # Observe directly because the test's observation Call action_id is deliberately reused.
    view = json.loads((await worker.browser.observe())[0]["text"])
    paths = [Path(item["path"]) for item in view["downloads"]]
    assert len(paths) == 2 and len(set(paths)) == 2
    assert all(path.parent == Path(view["downloads_directory"]) for path in paths)
    assert all(path.suffix == ".txt" and ".." not in path.name for path in paths)
    await worker.browser.close()
    assert all(path.read_text() == "synthetic-download" for path in paths)


async def test_unpointed_scroll_uses_the_validated_position_after_navigation(worker):
    await worker.browser.page.mouse.move(80, 80)
    await worker.browser.page.goto("about:blank?new-document")
    await worker.browser.page.set_content(
        "<style>body{height:2000px}#box{position:fixed;left:40px;top:40px;"
        "width:150px;height:150px;overflow:auto}</style>"
        '<div id="box"><div style="height:1200px">Nested scroll target</div></div>'
    )
    view = await observe(worker)
    status, body = await worker.call(
        call(
            "computer_act",
            {
                "observation_id": view["observation_id"],
                "action": {"type": "scroll", "delta_y": 200},
            },
            "scroll-new-document",
        )
    )
    assert status == 200, body.get("error")
    await worker.browser.page.wait_for_function("scrollY > 0 || box.scrollTop > 0")
    assert await worker.browser.page.evaluate("scrollY > 0 && box.scrollTop === 0")


async def test_close_cancels_partial_download_without_publishing_it(worker):
    started, cancelled = asyncio.Event(), asyncio.Event()

    class IncompleteDownload:
        suggested_filename = "unfinished.txt"

        async def save_as(self, path):
            path.write_text("partial-data")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    worker.browser.download_started(IncompleteDownload())
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(worker.browser.close(), 3)
    assert cancelled.is_set()
    assert worker.browser.completed_downloads == []
    assert list(worker.browser.downloads_directory.iterdir()) == []
