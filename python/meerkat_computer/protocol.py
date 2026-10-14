"""Strict worker boundary contracts, independent of benchmark tasks."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPABILITIES = frozenset(
    {"browser", "desktop", "screenshots", "browser_semantics", "desktop_semantics", "exec", "files",
     "file_import", "file_export"}
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observation(StrictModel):
    surface: Literal["browser", "desktop"] = "browser"
    mode: Literal["visual", "semantic", "hybrid"] = "hybrid"
    width: int = Field(default=1280, ge=160, le=3840)
    height: int = Field(default=800, ge=120, le=2160)
    detail: Literal["compact", "full"] = "compact"
    max_elements: int = Field(default=300, ge=1, le=300)
    max_text_chars: int = Field(default=24000, ge=256, le=24000)


class Lease(StrictModel):
    lease_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=2**63 - 1)
    expires_at_ms: int = Field(ge=0, le=2**63 - 1)
    capabilities: list[str] = Field(max_length=9)
    observation: Observation

    @model_validator(mode="after")
    def validate_capabilities(self):
        caps = set(self.capabilities)
        if self.expires_at_ms == 0 and caps:
            raise ValueError("Revoked fences have no capabilities")
        if caps - CAPABILITIES or len(caps) != len(self.capabilities):
            raise ValueError("Unsupported or duplicate capabilities")
        surfaces = caps & {"browser", "desktop"}
        if "screenshots" in caps and not surfaces:
            raise ValueError("Screenshots require a computer surface")
        if surfaces and self.observation.surface not in surfaces:
            raise ValueError("Default observation surface must be enabled")
        for surface in ("browser", "desktop"):
            semantics = surface + "_semantics"
            if semantics in caps and surface not in caps:
                raise ValueError("Semantic capability requires its surface")
            if surface not in caps:
                continue
            if self.observation.mode in {"visual", "hybrid"} and "screenshots" not in caps:
                raise ValueError("Visual observations require screenshots")
            if self.observation.mode in {"semantic", "hybrid"} and semantics not in caps:
                raise ValueError("Semantic observations require the surface semantics capability")
        return self


class Call(StrictModel):
    lease_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=2**63 - 1)
    action_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]


class ForkCapabilities(StrictModel):
    operation: Literal["fork_capabilities"]


class ForkPrepare(StrictModel):
    operation: Literal["fork_prepare"]
    checkpoint_id: str = Field(min_length=1, max_length=128)


class ForkResume(StrictModel):
    operation: Literal["fork_resume"]
    checkpoint_id: str = Field(min_length=1, max_length=128)


class ForkRebind(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    operation: Literal["fork_rebind"]
    checkpoint_id: str = Field(min_length=1, max_length=128)
    fork_id: str = Field(min_length=1, max_length=128)
    computer_id: str = Field(min_length=1, max_length=128)
    incarnation_id: str = Field(min_length=1, max_length=128)
    worker_token: str = Field(min_length=32, max_length=512, repr=False)

    @model_validator(mode="after")
    def validate_token(self):
        if any(ord(char) < 33 or ord(char) > 126 for char in self.worker_token):
            raise ValueError("Worker token must contain printable ASCII without whitespace")
        return self


ForkControl = Annotated[
    ForkCapabilities | ForkPrepare | ForkResume | ForkRebind, Field(discriminator="operation")
]


class NoArguments(StrictModel):
    pass


class ObserveArguments(StrictModel):
    surface: Literal["browser", "desktop"] | None = None
    detail: Literal["compact", "full"] | None = None
    max_elements: int | None = Field(default=None, ge=1, le=300)
    max_text_chars: int | None = Field(default=None, ge=256, le=24000)
    scope_ref: str | None = Field(default=None, min_length=1, max_length=100)
    observation_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.scope_ref is None) != (self.observation_id is None):
            raise ValueError("Scope requires both scope_ref and its observation_id")
        return self


class Action(StrictModel):
    type: Literal["click", "fill", "type", "key", "scroll", "drag", "focus", "invoke"]
    button: Literal["left", "middle", "right"] = "left"
    click_count: int = Field(default=1, ge=1, le=2)
    action_name: str | None = Field(default=None, min_length=1, max_length=200)
    ref: str | None = Field(default=None, min_length=1, max_length=100)
    x: float | None = None
    y: float | None = None
    text: str | None = Field(default=None, max_length=16384)
    key: str | None = Field(default=None, max_length=100)
    delta_x: float = Field(default=0, ge=-10000, le=10000)
    delta_y: float = Field(default=0, ge=-10000, le=10000)
    end_x: float | None = None
    end_y: float | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("Coordinates require x and y")
        if self.ref and self.x is not None:
            raise ValueError("Choose a ref or coordinates")
        if self.type in {"click", "drag"} and not self.ref and self.x is None:
            raise ValueError("Action requires a target")
        if self.type in {"fill", "type"} and self.text is None:
            raise ValueError("Action requires text")
        if self.type == "key" and not self.key:
            raise ValueError("Action requires key")
        if self.type == "drag" and (self.end_x is None or self.end_y is None):
            raise ValueError("Drag requires end_x and end_y")
        if self.type in {"focus", "invoke"} and not self.ref:
            raise ValueError("Semantic action requires a current ref")
        if self.type == "invoke" and not self.action_name:
            raise ValueError("Invoke requires an advertised action_name")
        if self.action_name is not None and self.type != "invoke":
            raise ValueError("action_name is only valid for invoke")
        return self


class ActArguments(StrictModel):
    observation_id: str = Field(min_length=1, max_length=128)
    action: Action


class NavigateArguments(StrictModel):
    url: str = Field(min_length=1, max_length=8192)


class TabsArguments(StrictModel):
    action: Literal["list", "new", "select", "close"]
    tab_id: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=8192)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.action in {"select", "close"} and not self.tab_id:
            raise ValueError("Tab action requires tab_id")
        if self.action != "new" and self.url is not None:
            raise ValueError("Only new accepts url")
        return self


class ExecArguments(StrictModel):
    command: str = Field(min_length=1, max_length=32768)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class ReadArguments(StrictModel):
    path: str = Field(min_length=1, max_length=4096)


class WriteArguments(ReadArguments):
    text: str = Field(max_length=1_048_576)


class BrowserUploadArguments(StrictModel):
    observation_id: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=100)
    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=16
    )


class TransferImportArguments(ReadArguments):
    file_id: str = Field(min_length=36, max_length=36)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    overwrite: bool = False


class TransferExportArguments(ReadArguments):
    file_id: str = Field(min_length=36, max_length=36)
    max_bytes: int = Field(ge=1, le=16 * 1024 * 1024)


class AppsArguments(StrictModel):
    action: Literal["list", "launch"]
    app_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_app(self):
        if (self.action == "launch") != (self.app_id is not None):
            raise ValueError("Only launch requires app_id")
        return self


TOOL_ARGUMENTS = {
    "computer_observe": (("browser", "desktop"), ObserveArguments),
    "computer_act": (("browser", "desktop"), ActArguments),
    "computer_apps": ("desktop", AppsArguments),
    "browser_navigate": ("browser", NavigateArguments),
    "browser_tabs": ("browser", TabsArguments),
    "browser_upload": ("browser", BrowserUploadArguments),
    "browser_downloads": ("browser", NoArguments),
    "workspace_exec": ("exec", ExecArguments),
    "workspace_read": ("files", ReadArguments),
    "workspace_write": ("files", WriteArguments),
    "transfer_import": ("file_import", TransferImportArguments),
    "transfer_export": ("file_export", TransferExportArguments),
}


STALE_INPUT_REASONS = frozenset({
    "observation_id", "display", "focus", "accessibility_connection",
    "accessibility_structure", "accessibility_events", "visual_unavailable",
    "visual_topology", "visual_pixels", "visual_target_outside", "native_target", "native_window",
})


class WorkerError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        effect: str = "none",
        *,
        reasons: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.code, self.message, self.status, self.effect = code, message, status, effect
        self.reasons = tuple(dict.fromkeys(
            reason for reason in reasons
            if isinstance(reason, str) and reason in STALE_INPUT_REASONS
        )) if isinstance(reasons, tuple) else ()
        self.metrics: dict | None = None

    def response(self) -> dict:
        body = {"error": {"code": self.code, "message": self.message, "effect": self.effect}}
        if self.metrics is not None:
            body["metrics"] = self.metrics
        return body
