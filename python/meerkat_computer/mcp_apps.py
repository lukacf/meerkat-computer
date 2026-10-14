"""Static MCP Apps metadata and bundled preview, independent of computer authority."""

from importlib.resources import files

from mcp import types

PREVIEW_URI = "ui://meerkat-computer/preview-v1.html"
PREVIEW_MIME = "text/html;profile=mcp-app"
PREVIEW_TOOL = "computer_preview"
PREVIEW_TRIGGERS = frozenset({"computer_observe", "computer_act", "browser_navigate"})
RESOURCE_META = {
    "ui": {
        "csp": {
            "connectDomains": [],
            "resourceDomains": [],
            "frameDomains": [],
            "baseUriDomains": [],
        },
        "prefersBorder": True,
    }
}


def load_preview_html() -> str | None:
    """Do not advertise a resource that is absent from the installed package."""
    try:
        html = (
            files("meerkat_computer")
            .joinpath("resources/preview-v1.html")
            .read_text(encoding="utf-8")
        )
        return html if html.strip() else None
    except (OSError, UnicodeError):
        return None


def supports_apps(capabilities: types.ClientCapabilities) -> bool:
    # SDK 1.x retains standard extension capabilities as extra fields.
    extensions = capabilities.model_extra.get("extensions") if capabilities.model_extra else None
    if not isinstance(extensions, dict):
        return False
    ui = extensions.get("io.modelcontextprotocol/ui")
    if not isinstance(ui, dict):
        return False
    mime_types = ui.get("mimeTypes")
    return isinstance(mime_types, list) and PREVIEW_MIME in mime_types


def with_preview(tools: list[types.Tool]) -> list[types.Tool]:
    """The Apps host enforces visibility; the original grant remains authority."""
    result = []
    for tool in tools:
        meta = dict(tool.meta or {})
        ui = {"visibility": ["model"]}
        if tool.name in PREVIEW_TRIGGERS:
            ui["resourceUri"] = PREVIEW_URI
        meta["ui"] = ui
        result.append(tool.model_copy(update={"meta": meta}))
    result.append(
        types.Tool(
            name=PREVIEW_TOOL,
            description="Read the latest saved screenshot for this connection's computer.",
            inputSchema={
                "type": "object",
                "properties": {"after": {"type": "string", "maxLength": 128}},
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
            _meta={"ui": {"visibility": ["app"]}},
        )
    )
    return result


def preview_error(message: str = "Computer preview unavailable.") -> types.CallToolResult:
    return types.CallToolResult(
        isError=True,
        content=[types.TextContent(type="text", text=message)],
    )


def valid_preview_arguments(arguments: dict) -> bool:
    if not isinstance(arguments, dict) or arguments.keys() - {"after"}:
        return False
    if "after" not in arguments:
        return True
    after = arguments["after"]
    if not isinstance(after, str):
        return False
    try:
        return len(after.encode("utf-8")) <= 128
    except UnicodeError:
        return False


def valid_preview_body(body: object) -> bool:
    """Validate only the grant endpoint's wire shape; the broker owns privacy policy."""
    if not (
        isinstance(body, dict)
        and body.keys() == {"computer_id", "status", "reason", "frame"}
        and isinstance(body["computer_id"], str)
        and bool(body["computer_id"])
        and isinstance(body["status"], str)
        and body["status"] in {"available", "unavailable", "withheld"}
    ):
        return False
    if body["status"] == "available":
        return body["reason"] is None and isinstance(body["frame"], dict)
    return isinstance(body["reason"], str) and body["frame"] is None
