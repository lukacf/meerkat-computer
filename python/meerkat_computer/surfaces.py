"""One observation cursor over the browser and the optional OS desktop."""

from .apps import Applications
from .browser import text_block
from .protocol import WorkerError


class ComputerSurfaces:
    persists_metrics = True

    def __init__(self, browser, desktop=None, applications=None):
        self.browser, self.desktop = browser, desktop
        self.applications = applications or Applications()
        self.lease = None
        self.active = None
        self.current_target = None
        self.local_metrics = {}
        self.fallback_metrics = {}
        self.metric_sink = None

    @property
    def operation_metrics(self):
        values = dict(self.local_metrics)
        if self.current_target is not None:
            for name, amount in getattr(
                self.current_target, "operation_metrics", self.fallback_metrics
            ).items():
                values[name] = values.get(name, 0) + amount
        return values

    def record(self, name):
        self.local_metrics[name] = self.local_metrics.get(name, 0) + 1
        if self.metric_sink:
            self.metric_sink(name, 1)

    async def start(self):
        await self.browser.start()
        if self.desktop is not None:
            await self.desktop.start()

    async def close(self):
        try:
            if self.desktop is not None:
                await self.desktop.close()
        finally:
            await self.browser.close()

    def ready(self):
        return self.browser.ready() and (self.desktop is None or self.desktop.ready())

    async def configure(self, lease):
        if "desktop" in lease.capabilities and self.desktop is None:
            raise WorkerError(
                "desktop_unavailable", "This worker image has no desktop channel", 503
            )
        changed = self.lease is None or (self.lease.lease_id, self.lease.epoch) != (
            lease.lease_id,
            lease.epoch,
        )
        if changed:
            await self.invalidate()
        await self.browser.configure(lease)
        if self.desktop is not None:
            await self.desktop.configure(lease)
        self.lease = lease

    async def invalidate(self):
        self.active = None
        await self.browser.invalidate()
        if self.desktop is not None:
            await self.desktop.invalidate()

    def target(self, surface):
        if self.lease is None or surface not in self.lease.capabilities:
            raise WorkerError("capability_denied", "This observation surface is not enabled", 403)
        target = self.browser if surface == "browser" else self.desktop
        if target is None:
            raise WorkerError("desktop_unavailable", "Worker has no desktop channel", 503)
        return target

    def surface_for(self, name, arguments):
        if name == "computer_observe":
            return arguments.surface or self.lease.observation.surface
        if name == "computer_apps":
            return "desktop"
        if name.startswith("browser_"):
            return "browser"
        if self.active is None:
            raise WorkerError("stale_observation", "Observe the desired surface before acting")
        return self.active

    async def validate(self, name, arguments):
        surface = self.surface_for(name, arguments)
        target = self.target(surface)
        if name == "computer_apps":
            if arguments.action == "launch":
                self.applications.resolve(arguments.app_id)
            return
        await target.validate(name, arguments)

    async def perform(self, name, arguments):
        self.local_metrics = {}
        self.fallback_metrics = {}
        self.current_target = None
        surface = self.surface_for(name, arguments)
        target = self.target(surface)
        if name == "computer_apps" and arguments.action == "list":
            return [text_block(self.applications.list())], {}
        passive_browser = name == "browser_downloads" or (
            name == "browser_tabs" and arguments.action == "list"
        )
        if not passive_browser:
            other = self.desktop if surface == "browser" else self.browser
            if other is not None:
                await other.invalidate()
            self.active = surface
        if name == "computer_apps":
            await target.invalidate()
            self.record("application_launch_attempts")
            self.record("ui_command_attempts")
            await self.applications.launch(arguments.app_id)
            self.record("application_launches")
            self.record("ui_commands")
            from .protocol import ObserveArguments

            self.current_target = target
            content, _ = await target.perform(
                "computer_observe", ObserveArguments(surface="desktop")
            )
            return [
                text_block({"app_id": arguments.app_id, "launch_submitted": True}),
                *content,
            ], self.operation_metrics
        self.current_target = target
        content, delta = await target.perform(name, arguments)
        if not getattr(target, "persists_metrics", False):
            self.fallback_metrics = delta
            if self.metric_sink:
                for key, value in delta.items():
                    self.metric_sink(key, value)
        return content, self.operation_metrics
