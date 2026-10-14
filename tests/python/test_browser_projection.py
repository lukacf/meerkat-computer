"""Real Chromium observes bounded semantic context without weakening privacy."""

import asyncio
import json
import secrets

import pytest
from meerkat_computer.browser import TARGET_SNAPSHOT
from meerkat_computer.protocol import WorkerError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from test_browser_freshness import call
from test_browser_freshness import worker as shared_worker

worker = shared_worker
pytestmark = pytest.mark.browser


async def view(worker, **options):
    _, response = await worker.call(call("computer_observe", options, secrets.token_hex(8)))
    return json.loads(response["content"][0]["text"])


async def test_browser_compact_is_bounded_and_does_not_count_hidden_nodes_as_omitted(worker):
    await worker.browser.page.set_content(
        "<style>button{width:20px;height:20px}</style>"
        + "".join(f"<button aria-label='Action {i}'>x</button>" for i in range(150))
        + "<div hidden><button>Invisible</button></div>"
    )
    body = await view(worker)
    assert len(body["elements"]) == 60
    assert body["projection"]["inspected_elements"] == 150
    assert body["projection"]["omitted_elements"] == 90
    assert body["projection"]["text_chars"] <= 6000
    assert body["truncated"]
    full = await view(worker, detail="full")
    assert len(full["elements"]) == 150
    assert full["projection"]["omitted_elements"] == 0


async def test_browser_budget_reports_aria_truncation_and_keeps_usable_refs(worker):
    await worker.browser.page.set_content(
        "<button>Continue</button><p>" + "Document line " * 800 + "</p>"
    )
    body = await view(worker, max_text_chars=256)
    assert body["projection"]["text_chars"] <= 256
    assert body["accessibility_truncated"] is True
    assert body["elements"][0]["label"] == "Continue"
    # The bounded ARIA view must be a complete line prefix, not broken YAML.
    assert not body["accessibility"].endswith("Docu")


async def test_browser_explicit_native_scope_is_rejected(worker):
    body = await view(worker)
    with pytest.raises(WorkerError) as error:
        await view(
            worker, scope_ref=body["elements"][0]["ref"], observation_id=body["observation_id"]
        )
    assert error.value.code == "unsupported_observation_scope"


async def test_browser_override_cannot_exceed_profile(worker):
    worker.browser.lease.observation.max_elements = 8
    with pytest.raises(WorkerError) as error:
        await view(worker, max_elements=9)
    assert error.value.code == "observation_limit_exceeded"


async def test_browser_full_reports_native_label_read_truncation(worker):
    await worker.browser.page.set_content("<button aria-label='" + "x" * 1000 + "'>Go</button>")
    body = await view(worker, detail="full")
    assert len(body["elements"][0]["label"]) == 300
    assert body["elements"][0]["label_truncated"] is True
    assert body["truncated"] is True
    assert body["projection"]["truncated_fields"] >= 1


def track_capture_handles(page, monkeypatch):
    """Retain test references so remote disposal can be checked against Chromium."""
    snapshots, elements = [], []
    evaluate_handle, query_selector_all = page.evaluate_handle, page.query_selector_all

    async def evaluate(expression, *args, **kwargs):
        handle = await evaluate_handle(expression, *args, **kwargs)
        if expression == TARGET_SNAPSHOT:
            snapshots.append(handle)
        return handle

    async def query(*args, **kwargs):
        handles = await query_selector_all(*args, **kwargs)
        elements.extend(handles)
        return handles

    monkeypatch.setattr(page, "evaluate_handle", evaluate)
    monkeypatch.setattr(page, "query_selector_all", query)
    return snapshots, elements


async def assert_remote_handles_released(handles):
    assert handles
    for handle in handles:
        with pytest.raises(PlaywrightError):
            await handle.evaluate("() => true")


@pytest.mark.parametrize("failure", ["size", "element", "screenshot_timeout", "oversize"])
async def test_failed_capture_releases_all_acquired_handles(worker, monkeypatch, failure):
    browser, page = worker.browser, worker.browser.page
    snapshots, elements = track_capture_handles(page, monkeypatch)

    async def fail(*args, **kwargs):
        raise PlaywrightTimeoutError("Synthetic capture timeout")

    if failure == "size":
        evaluate = page.evaluate

        async def fail_size(expression, *args, **kwargs):
            if expression == "({width:innerWidth,height:innerHeight})":
                return await fail()
            return await evaluate(expression, *args, **kwargs)

        monkeypatch.setattr(page, "evaluate", fail_size)
    elif failure == "element":
        query_selector_all = page.query_selector_all

        async def fail_first_element(*args, **kwargs):
            handles = await query_selector_all(*args, **kwargs)
            monkeypatch.setattr(handles[0], "is_visible", fail)
            return handles

        monkeypatch.setattr(page, "query_selector_all", fail_first_element)
    elif failure == "screenshot_timeout":
        monkeypatch.setattr(page, "screenshot", fail)
    else:
        monkeypatch.setattr("meerkat_computer.browser.MAX_SCREENSHOT_BYTES", 1)

    with pytest.raises((PlaywrightTimeoutError, WorkerError)):
        await browser.observe()
    assert not browser.observation_id
    assert browser.target_snapshot is None
    assert browser.refs == {}
    await assert_remote_handles_released(snapshots)
    if failure != "size":
        assert len(elements) >= 2  # Includes the unvisited tail on element failure.
        await assert_remote_handles_released(elements)
    assert await page.evaluate("() => true")  # The page itself remains usable.


async def test_cancelled_capture_releases_handles_and_preserves_cancellation(worker, monkeypatch):
    browser, page = worker.browser, worker.browser.page
    snapshots, elements = track_capture_handles(page, monkeypatch)
    started = asyncio.Event()

    async def wait_for_cancellation(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(page, "screenshot", wait_for_cancellation)
    task = asyncio.create_task(worker.call(call("computer_observe")))
    try:
        await asyncio.wait_for(started.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not browser.observation_id
    await assert_remote_handles_released(snapshots + elements)


async def test_successful_capture_retains_only_published_handles(worker, monkeypatch):
    browser, page = worker.browser, worker.browser.page
    snapshots, elements = track_capture_handles(page, monkeypatch)
    body = await view(worker, max_elements=1)
    assert len(snapshots) == 1 and len(elements) >= 2
    assert browser.target_snapshot is snapshots[0]
    assert set(browser.refs) == {body["elements"][0]["ref"]}
    retained = [snapshots[0], *browser.refs.values()]
    for handle in retained:
        assert await handle.evaluate("() => true")
    await assert_remote_handles_released([handle for handle in elements if handle not in retained])
    await browser.invalidate()
    await assert_remote_handles_released(retained)


async def test_failed_followup_capture_cleans_handles_without_replaying_input(worker, monkeypatch):
    page = worker.browser.page
    body = await view(worker)
    ref = next(item["ref"] for item in body["elements"] if item["label"] == "Continue")
    snapshots, elements = track_capture_handles(page, monkeypatch)

    async def fail(*args, **kwargs):
        raise PlaywrightTimeoutError("Synthetic capture timeout after input")

    monkeypatch.setattr(page, "screenshot", fail)
    action = call(
        "computer_act",
        {"observation_id": body["observation_id"], "action": {"type": "click", "ref": ref}},
        "failed-followup",
    )
    status, result = await worker.call(action)
    assert status == 409 and result["error"]["code"] == "action_effect_unknown"
    assert result["error"]["effect"] == "unknown"
    assert await page.evaluate("window.clicks") == 1
    await assert_remote_handles_released(snapshots + elements)
    assert await worker.call(action) == (status, result)
    assert await page.evaluate("window.clicks") == 1
