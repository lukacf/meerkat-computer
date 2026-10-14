import json

import pytest
from meerkat_computer.apps import Applications
from meerkat_computer.browser import text_block
from meerkat_computer.protocol import Call, Lease, WorkerError
from meerkat_computer.worker import Worker
from pydantic import ValidationError


class Surface:
    persists_metrics = True

    def __init__(self, name):
        self.name, self.observation_id = name, ""
        self.count = 0
        self.operation_metrics = {}
        self.fail = False

    async def start(self):
        pass

    async def close(self):
        pass

    def ready(self):
        return True

    async def configure(self, lease):
        self.lease = lease

    async def invalidate(self):
        self.observation_id = ""

    async def validate(self, name, args):
        if name == "computer_act" and (
            not self.observation_id or args.observation_id != self.observation_id
        ):
            raise WorkerError("stale_observation", "Observation is not current")

    async def perform(self, name, args):
        self.operation_metrics = {}
        if name == "computer_act":
            self.observation_id = ""
            self.operation_metrics = {"clicks": 1, "ui_commands": 1}
            for key, value in self.operation_metrics.items():
                self.metric_sink(key, value)
            if self.fail:
                raise RuntimeError("capture failed after input")
        self.count += 1
        self.observation_id = self.name + str(self.count)
        return [
            text_block({"surface": self.name, "observation_id": self.observation_id})
        ], self.operation_metrics


def grant(epoch=1, caps=None):
    return Lease(
        lease_id=f"lease-{epoch}",
        epoch=epoch,
        expires_at_ms=2_000_000,
        capabilities=caps
        or ["browser", "desktop", "screenshots", "browser_semantics", "desktop_semantics"],
        observation={"surface": "desktop", "mode": "hybrid", "width": 640, "height": 480},
    )


async def send(worker, name, arguments=None, epoch=1):
    send.number += 1
    return await worker.call(
        Call(
            lease_id=f"lease-{epoch}",
            epoch=epoch,
            action_id=str(send.number),
            name=name,
            arguments=arguments or {},
        )
    )


send.number = 0


def observation(body):
    return json.loads(body["content"][0]["text"])


async def test_one_observation_cursor_across_browser_desktop_and_takeover(tmp_path):
    browser, desktop = Surface("browser"), Surface("desktop")
    worker = Worker(
        tmp_path / "state",
        tmp_path / "work",
        "x" * 64,
        browser,
        desktop=desktop,
        clock=lambda: 1000,
    )
    await worker.start()
    try:
        await worker.grant(grant())
        _, body = await send(worker, "computer_observe")
        old = observation(body)
        assert old["surface"] == "desktop"
        _, body = await send(worker, "computer_observe", {"surface": "browser"})
        web = observation(body)
        assert desktop.observation_id == ""
        with pytest.raises(WorkerError, match="not current"):
            await send(
                worker,
                "computer_act",
                {
                    "observation_id": old["observation_id"],
                    "action": {"type": "click", "x": 1, "y": 1},
                },
            )
        await send(
            worker,
            "computer_act",
            {"observation_id": web["observation_id"], "action": {"type": "click", "x": 1, "y": 1}},
        )
        await worker.grant(grant(2))
        assert worker.computer.active is None
        with pytest.raises(WorkerError, match="not current"):
            await send(worker, "computer_observe")
        _, body = await send(worker, "computer_observe", epoch=2)
        assert observation(body)["surface"] == "desktop"
    finally:
        await worker.close()


async def test_uncertain_native_input_preserves_counters_and_invalidates_both_views(tmp_path):
    browser, desktop = Surface("browser"), Surface("desktop")
    worker = Worker(
        tmp_path / "state",
        tmp_path / "work",
        "x" * 64,
        browser,
        desktop=desktop,
        clock=lambda: 1000,
    )
    await worker.start()
    try:
        await worker.grant(grant())
        _, body = await send(worker, "computer_observe")
        desktop.fail = True
        status, body = await send(
            worker,
            "computer_act",
            {
                "observation_id": observation(body)["observation_id"],
                "action": {"type": "click", "x": 1, "y": 1},
            },
        )
        assert status == 409 and body["error"]["effect"] == "unknown"
        assert body["metrics"]["delta"]["clicks"] == 1
        assert body["metrics"]["totals"]["ui_commands"] == 1
        assert worker.computer.active is None
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("workspace_exec", {"command": "true"}),
        ("workspace_write", {"path": "watched.txt", "text": "changed"}),
    ],
)
async def test_workspace_effects_invalidate_ui_even_without_accessibility_events(
    tmp_path, name, arguments
):
    browser, desktop = Surface("browser"), Surface("desktop")
    worker = Worker(
        tmp_path / "state",
        tmp_path / "work",
        "x" * 64,
        browser,
        desktop=desktop,
        clock=lambda: 1000,
    )
    await worker.start()
    try:
        lease = grant()
        lease.capabilities += ["exec", "files"]
        await worker.grant(lease)
        _, body = await send(worker, "computer_observe")
        old = observation(body)["observation_id"]
        status, _ = await send(worker, name, arguments)
        assert status == 200
        assert worker.computer.active is None and not desktop.observation_id
        with pytest.raises(WorkerError, match="Observe"):
            await send(
                worker,
                "computer_act",
                {
                    "observation_id": old,
                    "action": {"type": "click", "x": 1, "y": 1},
                },
            )
    finally:
        await worker.close()


async def test_browser_only_worker_refuses_desktop_grant_and_surface(tmp_path):
    worker = Worker(
        tmp_path / "state", tmp_path / "work", "x" * 64, Surface("browser"), clock=lambda: 1000
    )
    await worker.start()
    try:
        lease = grant().model_copy(
            update={
                "capabilities": ["browser", "screenshots", "browser_semantics"],
                "observation": grant().observation.model_copy(update={"surface": "browser"}),
            }
        )
        await worker.grant(lease)
        with pytest.raises(WorkerError, match="not enabled"):
            await send(worker, "computer_observe", {"surface": "desktop"})
        with pytest.raises(WorkerError, match="configuration failed"):
            await worker.grant(grant(2))
        assert worker.applied_lease is None
        with pytest.raises(WorkerError, match="not current"):
            await send(worker, "computer_observe")
    finally:
        await worker.close()


def test_desktop_semantic_capability_and_default_surface_are_checked():
    with pytest.raises(ValidationError):
        grant(caps=["desktop", "screenshots"])
    with pytest.raises(ValidationError):
        grant(caps=["browser", "screenshots", "browser_semantics"])


def test_app_registry_accepts_only_registered_visible_native_entries(tmp_path):
    (tmp_path / "editor.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Editor\nExec=editor %F\n"
    )
    (tmp_path / "hidden.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hidden\nExec=hidden\nNoDisplay=true\n"
    )
    (tmp_path / "alias.desktop").symlink_to(tmp_path / "editor.desktop")
    apps = Applications([tmp_path])
    assert [item["app_id"] for item in apps.list()["applications"]] == ["editor.desktop"]
    for invalid in ("../editor.desktop", "/bin/sh", "hidden.desktop", "alias.desktop"):
        with pytest.raises(WorkerError):
            apps.resolve(invalid)
