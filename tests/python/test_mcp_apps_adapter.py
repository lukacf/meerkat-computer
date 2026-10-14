"""MCP Apps behavior through the real SDK session and low-level server."""

import json
import sys
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest
from mcp import ClientSession, types
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams
from meerkat_computer.mcp_adapter import BrokerAdapter, create_server

URI = "ui://meerkat-computer/preview-v1.html"
MIME = "text/html;profile=mcp-app"
CAPABILITIES = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [MIME]}}}
TOOLS = [
    {
        "name": name,
        "description": "Existing tool",
        "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
        "_meta": {"existing": {"keep": True}},
    }
    for name in ["computer_observe", "computer_act", "browser_navigate", "workspace_exec"]
]
PREVIEW = {
    "computer_id": "bound-computer",
    "status": "withheld",
    "reason": "private_input",
    "frame": None,
}


@asynccontextmanager
async def connected(adapter, *, mcp_apps=False, capabilities=None):
    server = create_server(adapter, mcp_apps=mcp_apps)
    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as group,
    ):
        group.start_soon(server.run, *server_streams, server.create_initialization_options())
        try:
            async with ClientSession(*client_streams) as session:
                # SDK 1.x has no convenience argument for extension capabilities.
                # Send the standard initialize request over its real transport.
                await session.send_request(
                    types.ClientRequest(
                        types.InitializeRequest(
                            params=types.InitializeRequestParams(
                                protocolVersion=types.LATEST_PROTOCOL_VERSION,
                                capabilities=types.ClientCapabilities.model_validate(
                                    capabilities or {}
                                ),
                                clientInfo=types.Implementation(name="test-host", version="1"),
                            )
                        )
                    ),
                    types.InitializeResult,
                )
                await session.send_notification(
                    types.ClientNotification(types.InitializedNotification())
                )
                yield session
        finally:
            group.cancel_scope.cancel()


@pytest.fixture
def resource(monkeypatch):
    from meerkat_computer import mcp_apps

    html = "<!DOCTYPE html><html><body>My Computer fixture</body></html>"
    monkeypatch.setattr(mcp_apps, "load_preview_html", lambda: html)
    return html


@pytest.fixture
async def broker():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/tools":
            return httpx.Response(200, json={"tools": TOOLS})
        if request.url.path == "/v1/preview":
            return httpx.Response(200, json=PREVIEW)
        if request.url.path == "/v1/tools/call":
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "observed"}],
                    "metrics": {"delta": {"clicks": 1}, "totals": {"clicks": 2}},
                },
            )
        raise AssertionError("Unexpected broker endpoint")

    async with httpx.AsyncClient(
        base_url="http://original-broker",
        headers={"Authorization": "Bearer original-control-binding"},
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        yield BrokerAdapter(client), requests


@pytest.mark.parametrize("capabilities", [{}, CAPABILITIES])
async def test_apps_are_opt_in_and_legacy_tools_are_unchanged(broker, capabilities):
    adapter, requests = broker
    async with connected(adapter, capabilities=capabilities) as session:
        tools = (await session.list_tools()).tools
        assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools] == TOOLS
        result = await session.call_tool("computer_preview", {})
        assert result.isError
        assert not any(request.url.path == "/v1/preview" for request in requests)
        assert not any(request.method == "POST" for request in requests)
        with pytest.raises(McpError):
            await session.read_resource(URI)


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"experimental": {"io.modelcontextprotocol/ui": {"mimeTypes": [MIME]}}},
        {"extensions": None},
        {"extensions": []},
        {"extensions": {"io.modelcontextprotocol/ui": None}},
        {"extensions": {"io.modelcontextprotocol/ui": {}}},
        {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": MIME}}},
        {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html"]}}},
    ],
)
async def test_non_apps_hosts_cannot_discover_or_call_preview(broker, resource, capabilities):
    adapter, requests = broker
    async with connected(adapter, mcp_apps=True, capabilities=capabilities) as session:
        tools = (await session.list_tools()).tools
        assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools] == TOOLS
        assert (await session.list_resources()).resources == []
        with pytest.raises(McpError):
            await session.read_resource(URI)
        assert (await session.call_tool("computer_preview", {})).isError
        assert {request.url.path for request in requests} == {"/v1/tools"}


async def test_apps_metadata_and_real_resource_lifecycle(broker, resource):
    adapter, requests = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        tools = (await session.list_tools()).tools
        ordinary, preview = tools[:-1], tools[-1]
        for source, tool in zip(TOOLS, ordinary, strict=True):
            assert tool.name == source["name"]
            assert tool.inputSchema == source["inputSchema"]
            assert tool.meta["existing"] == {"keep": True}
            expected = {"visibility": ["model"]}
            if tool.name != "workspace_exec":
                expected["resourceUri"] = URI
            assert tool.meta["ui"] == expected
        assert preview.name == "computer_preview"
        assert preview.meta == {"ui": {"visibility": ["app"]}}
        assert preview.inputSchema == {
            "type": "object",
            "properties": {"after": {"type": "string", "maxLength": 128}},
            "additionalProperties": False,
        }
        assert preview.annotations.readOnlyHint is True
        assert preview.annotations.destructiveHint is False
        resources = (await session.list_resources()).resources
        assert len(resources) == 1 and str(resources[0].uri) == URI
        assert resources[0].mimeType == MIME
        result = await session.read_resource(URI)
        content = result.contents[0]
        assert len(result.contents) == 1
        assert str(content.uri) == URI
        assert content.mimeType == MIME and content.text == resource
        assert content.meta == {
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
        assert "original-control-binding" not in result.model_dump_json()
        assert {request.url.path for request in requests} == {"/v1/tools"}


@pytest.mark.parametrize(
    "uri", ["ui://meerkat-computer/other", URI + "?token=secret", "file:///etc/passwd"]
)
async def test_unknown_resources_fail_closed_without_echoing_input(broker, resource, uri):
    adapter, _ = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        with pytest.raises(McpError) as error:
            await session.read_resource(uri)
        assert "secret" not in str(error.value)
        assert "/etc/passwd" not in str(error.value)


async def test_missing_bundle_does_not_advertise_or_execute_preview(broker, monkeypatch):
    from meerkat_computer import mcp_apps

    monkeypatch.setattr(mcp_apps, "load_preview_html", lambda: None)
    adapter, requests = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        tools = (await session.list_tools()).tools
        assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools] == TOOLS
        assert (await session.list_resources()).resources == []
        with pytest.raises(McpError):
            await session.read_resource(URI)
        assert (await session.call_tool("computer_preview", {})).isError
        assert {request.url.path for request in requests} == {"/v1/tools"}


async def test_preview_reuses_original_binding_without_action_ids_or_counters(broker, resource):
    adapter, requests = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        first = await session.call_tool("computer_preview", {})
        second = await session.call_tool("computer_preview", {"after": "opaque:cursor/+?"})
        for result in [first, second]:
            assert not result.isError
            assert result.meta == {"meerkat-computer/preview": PREVIEW}
            assert result.structuredContent is None
            assert len(result.content) == 1 and result.content[0].type == "text"
            assert "original-control-binding" not in result.model_dump_json()
            assert "bound-computer" not in result.content[0].text
            assert "action_id" not in result.model_dump_json()
            assert "metrics" not in result.model_dump_json()
        previews = [request for request in requests if request.url.path == "/v1/preview"]
        assert len(previews) == 2
        assert not previews[0].url.query
        assert dict(previews[1].url.params) == {"after": "opaque:cursor/+?"}
        assert all(request.method == "GET" for request in requests)
        assert all(request.url.host == "original-broker" for request in requests)
        assert all(
            request.headers["authorization"] == "Bearer original-control-binding"
            for request in requests
        )


async def test_gateway_credentials_stay_in_http_headers_not_native_mcp_messages(resource):
    control = "native-control-test-" + "c" * 40
    host = "native-host-test-" + "h" * 40
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/tools":
            return httpx.Response(200, json={"tools": TOOLS})
        if request.url.path == "/v1/preview":
            return httpx.Response(200, json=PREVIEW)
        assert request.url.path == "/v1/tools/call"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Observed"}]})

    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:18761",
            headers={"Authorization": "Bearer " + control, "X-Meerkat-Computer-Host-Token": host},
            transport=httpx.MockTransport(handler),
        ) as client,
        connected(BrokerAdapter(client), mcp_apps=True, capabilities=CAPABILITIES) as session,
    ):
        results = [
            await session.list_tools(),
            await session.call_tool("computer_preview", {}),
            await session.call_tool("computer_observe", {}),
            await session.read_resource(URI),
        ]
    serialized = "".join(result.model_dump_json() for result in results)
    assert control not in serialized and host not in serialized
    assert all(request.url.path != "/host/wake" for request in requests)
    for request in requests:
        assert request.headers["authorization"] == "Bearer " + control
        assert request.headers["X-Meerkat-Computer-Host-Token"] == host
        assert control.encode() not in request.content and host.encode() not in request.content


@pytest.mark.parametrize(
    "arguments",
    [
        {"computer_id": "other-computer"},
        {"identity": "other-agent"},
        {"token": "operator-secret"},
        {"after": "x" * 129},
        {"after": "\u00e9" * 65},
        {"after": None},
        {"after": 1},
        {"after": []},
    ],
)
async def test_invalid_preview_arguments_never_reach_broker(broker, resource, arguments):
    adapter, requests = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        result = await session.call_tool("computer_preview", arguments)
        assert result.isError
        assert "operator-secret" not in result.model_dump_json()
        assert {request.url.path for request in requests} == {"/v1/tools"}


@pytest.mark.parametrize(
    "failure", ["denied", "redirect", "timeout", "invalid_json", "wrong_shape"]
)
async def test_preview_failures_are_safe_and_do_not_retry(resource, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/tools":
            return httpx.Response(200, json={"tools": TOOLS})
        if failure == "timeout":
            raise httpx.ReadTimeout("operator-secret", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="operator-secret")
        if failure == "wrong_shape":
            return httpx.Response(200, json={"token": "operator-secret"})
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "http://other/secret"})
        return httpx.Response(403, json={"error": {"message": "operator-secret"}})

    async with (
        httpx.AsyncClient(
            base_url="http://original-broker", transport=httpx.MockTransport(handler)
        ) as client,
        connected(BrokerAdapter(client), mcp_apps=True, capabilities=CAPABILITIES) as session,
    ):
        result = await session.call_tool("computer_preview", {})
    assert result.isError
    assert "operator-secret" not in result.model_dump_json()
    assert "action_id" not in result.model_dump_json()
    assert "metrics" not in result.model_dump_json()
    assert len([request for request in requests if request.url.path == "/v1/preview"]) == 1
    assert not any(request.method == "POST" for request in requests)


@pytest.mark.parametrize(
    ("status", "reason", "frame"),
    [
        ("available", "private_input", {"data": "private-image-marker"}),
        ("available", None, None),
        ("withheld", "private_input", {"data": "private-image-marker"}),
        ("unavailable", "no_capture", {"data": "private-image-marker"}),
        ("withheld", None, None),
        ("unavailable", None, None),
    ],
)
async def test_inconsistent_preview_fields_never_reach_app(resource, status, reason, frame):
    def handler(request):
        if request.url.path == "/v1/tools":
            return httpx.Response(200, json={"tools": TOOLS})
        return httpx.Response(
            200,
            json={
                "computer_id": "bound-computer",
                "status": status,
                "reason": reason,
                "frame": frame,
            },
        )

    async with (
        httpx.AsyncClient(
            base_url="http://broker", transport=httpx.MockTransport(handler)
        ) as client,
        connected(BrokerAdapter(client), mcp_apps=True, capabilities=CAPABILITIES) as session,
    ):
        result = await session.call_tool("computer_preview", {})
        assert result.isError
        assert result.structuredContent is None
        assert "private-image-marker" not in result.model_dump_json()


async def test_available_preview_preserves_frame_payload(broker, resource, monkeypatch):
    view = {
        "computer_id": "bound-computer",
        "status": "available",
        "reason": None,
        "frame": {"observation_id": "a" * 32, "data": None},
    }
    monkeypatch.setattr(sys.modules[__name__], "PREVIEW", view)
    adapter, _ = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        result = await session.call_tool("computer_preview", {"after": "a" * 32})
        assert not result.isError
        assert result.meta == {"meerkat-computer/preview": view}
        assert result.structuredContent is None


async def test_preview_details_are_only_in_ui_private_metadata(broker, resource, monkeypatch):
    view = {
        "computer_id": "private-computer-marker",
        "status": "available",
        "reason": None,
        "frame": {"observation_id": "b" * 32, "data": "private-pixel-marker"},
    }
    monkeypatch.setattr(sys.modules[__name__], "PREVIEW", view)
    adapter, _ = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        result = await session.call_tool("computer_preview", {})
    assert not result.isError
    assert result.meta == {"meerkat-computer/preview": view}
    assert result.structuredContent is None
    ordinary = result.model_dump_json(by_alias=True, exclude={"meta"})
    for marker in ("private-computer-marker", "private-pixel-marker", "b" * 32):
        assert marker not in ordinary
    assert "original-control-binding" not in result.model_dump_json()


async def test_apps_keep_ordinary_telemetry_and_action_identity(broker, resource):
    adapter, requests = broker
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        result = await session.call_tool("computer_act", {"value": "click"})
        await session.call_tool("computer_preview", {})
    calls = [request for request in requests if request.method == "POST"]
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["name"] == "computer_act" and body["arguments"] == {"value": "click"}
    assert result.structuredContent == {
        "action_id": body["action_id"],
        "metrics": {"delta": {"clicks": 1}, "totals": {"clicks": 2}},
    }
    assert result.content[0].text == "observed"


def test_resource_loader_handles_absent_and_unreadable_bundle(tmp_path, monkeypatch):
    from meerkat_computer import mcp_apps

    monkeypatch.setattr(mcp_apps, "files", lambda _package: tmp_path)
    assert mcp_apps.load_preview_html() is None
    path = tmp_path / "resources" / "preview-v1.html"
    path.parent.mkdir()
    path.write_bytes(b"\xff")
    assert mcp_apps.load_preview_html() is None
    path.write_text("<!doctype html><title>My Computer</title>")
    assert mcp_apps.load_preview_html() == path.read_text()


async def test_packaged_resource_is_served_by_sdk_without_binding_material(broker):
    from meerkat_computer import mcp_apps

    adapter, _ = broker
    expected = mcp_apps.load_preview_html()
    assert expected is not None and "My Computer" in expected
    async with connected(adapter, mcp_apps=True, capabilities=CAPABILITIES) as session:
        resources = (await session.list_resources()).resources
        result = await session.read_resource(resources[0].uri)
        assert result.contents[0].text == expected
        assert "original-control-binding" not in result.model_dump_json()


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_passes_explicit_apps_option(tmp_path, monkeypatch, enabled):
    from meerkat_computer import mcp_adapter

    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"token": "x" * 64}))
    path.chmod(0o600)
    argv = ["meerkat-computer-mcp", "--broker-url", "http://broker", "--binding-file", str(path)]
    if enabled:
        argv.append("--mcp-apps")
    monkeypatch.setattr(sys, "argv", argv)
    calls = []

    async def run(broker_url, binding, *, mcp_apps=False):
        calls.append((broker_url, binding, mcp_apps))

    monkeypatch.setattr(mcp_adapter, "run", run)
    mcp_adapter.main()
    assert calls == [("http://broker", {"token": "x" * 64}, enabled)]


async def test_preview_tool_cache_discovery_errors_are_safe(resource):
    def handler(request):
        raise httpx.ReadTimeout("operator-secret", request=request)

    async with (
        httpx.AsyncClient(
            base_url="http://broker", transport=httpx.MockTransport(handler)
        ) as client,
        connected(BrokerAdapter(client), mcp_apps=True, capabilities=CAPABILITIES) as session,
    ):
        result = await session.call_tool("computer_preview", {})
        assert result.isError
        assert "operator-secret" not in result.model_dump_json()
