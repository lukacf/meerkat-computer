import json
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from meerkat_computer.mcp_adapter import BrokerAdapter, create_server, read_binding

CONTROL = "adapter-control-test-" + "c" * 40
HOST = "adapter-host-test-" + "h" * 40
SENTINEL = "private-preflight-diagnostic"


def runtime_fixture(monkeypatch, handler, *, exercise=None):
    from meerkat_computer import mcp_adapter

    requests, settings, events, results = [], [], [], []
    async_client = httpx.AsyncClient

    def dispatch(request):
        requests.append(request)
        events.append(request.url.path)
        return handler(request)

    def client_factory(**kwargs):
        settings.append(kwargs)
        return async_client(**kwargs, transport=httpx.MockTransport(dispatch))

    @asynccontextmanager
    async def stdio():
        events.append("stdio")
        yield object(), object()

    class Runtime:
        def __init__(self, adapter):
            self.adapter = adapter

        def create_initialization_options(self):
            return object()

        async def run(self, *_):
            events.append("initialized")
            if exercise is not None:
                results.extend(await exercise(self.adapter))

    monkeypatch.setattr(mcp_adapter.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(mcp_adapter, "stdio_server", stdio)
    monkeypatch.setattr(mcp_adapter, "create_server", lambda adapter, **_: Runtime(adapter))
    return requests, settings, events, results


def ready_response(_):
    return httpx.Response(200, json={"ready": True})


def test_binding_requires_owned_private_regular_file(tmp_path):
    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"token": "x" * 64, "computer_id": "c"}))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_binding(path)
    path.chmod(0o600)
    assert read_binding(path)["computer_id"] == "c"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular file"):
        read_binding(link)


async def test_adapter_forwards_schemas_and_content():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/tools":
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "computer_observe",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "observed"},
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                ],
                "metrics": {"clicks": 1},
            },
        )

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = BrokerAdapter(client)
        assert (await adapter.list_tools())[0].name == "computer_observe"
        result = await adapter.call_tool("computer_observe", {}, "stable-id")
        assert result.content[0].text == "observed"
        assert result.content[1].mimeType == "image/png"
        assert result.structuredContent["metrics"] == {"clicks": 1}
        assert json.loads(requests[-1].content) == {
            "action_id": "stable-id",
            "name": "computer_observe",
            "arguments": {},
        }
        assert create_server(adapter).name == "meerkat-computer"


async def test_transport_failure_never_retries_and_reports_unknown():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret credential details", request=request)

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        result = await BrokerAdapter(client).call_tool("computer_act", {}, "action-123")
    assert len(calls) == 1 and result.isError
    assert "credential" not in result.content[0].text
    assert json.loads(result.content[0].text)["error"]["effect"] == "unknown"


async def test_broker_failure_passes_effect_classification():
    def handler(request):
        return httpx.Response(
            403,
            json={
                "error": {"code": "capability_denied", "effect": "none", "message": "Tool disabled"}
            },
        )

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        result = await BrokerAdapter(client).call_tool("workspace_exec", {})
    assert result.isError
    assert json.loads(result.content[0].text)["error"]["effect"] == "none"


async def test_uncertain_action_preserves_structured_telemetry():
    metrics = {
        "delta": {"ui_command_attempts": 1, "ui_commands": 1, "clicks": 1},
        "totals": {"ui_command_attempts": 3, "ui_commands": 2, "clicks": 2},
        "counts_are_lower_bounds": True,
    }

    def handler(request):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "unknown_effect",
                    "effect": "unknown",
                    "message": "Input completed but capture failed",
                },
                "metrics": metrics,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        result = await BrokerAdapter(client).call_tool("computer_act", {}, "uncertain-click")
    assert result.isError
    assert result.structuredContent == {"action_id": "uncertain-click", "metrics": metrics}
    assert json.loads(result.content[0].text)["metrics"] == metrics


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "private invalid body",
        {"content": [None]},
        {"content": ["private block"]},
        {"error": "private error"},
        {"error": {"code": "private", "effect": []}},
    ],
)
async def test_malformed_action_reply_reports_unknown_without_retry_or_body_leak(body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=json.dumps(body))

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        result = await BrokerAdapter(client).call_tool("computer_act", {}, "uncertain-action")
    assert len(calls) == 1
    assert result.isError
    assert json.loads(result.content[0].text)["error"] == {
        "code": "invalid_broker_response",
        "effect": "unknown",
        "action_id": "uncertain-action",
    }
    assert "private" not in result.model_dump_json()
    assert result.structuredContent == {"action_id": "uncertain-action", "metrics": {}}


async def test_failed_http_status_cannot_be_reported_as_success():
    def handler(request):
        return httpx.Response(503, json={"content": [{"type": "text", "text": "private"}]})

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        result = await BrokerAdapter(client).call_tool("computer_act", {}, "uncertain-action")
    assert result.isError
    assert json.loads(result.content[0].text)["error"]["effect"] == "unknown"
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "host_token,timeout,expected",
    [(None, None, 150), (HOST, None, 900), (HOST, 42.5, 42.5), (None, 60, 60)],
)
async def test_run_uses_gateway_defaults_only_when_configured(
    monkeypatch, host_token, timeout, expected
):
    from meerkat_computer import mcp_adapter

    requests, settings, events, _ = runtime_fixture(monkeypatch, ready_response)
    await mcp_adapter.run(
        "http://127.0.0.1:18761", {"token": CONTROL}, host_token=host_token, timeout=timeout
    )
    assert settings[0]["timeout"] == expected
    assert settings[0]["trust_env"] is False and settings[0]["follow_redirects"] is False
    assert settings[0]["headers"]["Authorization"] == "Bearer " + CONTROL
    if host_token:
        assert len(requests) == 1 and requests[0].method == "POST"
        assert requests[0].headers["X-Meerkat-Computer-Host-Token"] == HOST
        assert not requests[0].content
        assert events == ["/host/wake", "stdio", "initialized"]
    else:
        assert "X-Meerkat-Computer-Host-Token" not in settings[0]["headers"]
        assert not requests
        assert events == ["stdio", "initialized"]


async def test_gateway_preflight_precedes_discovery_and_passive_reads_never_wake_again(
    monkeypatch, caplog
):
    from meerkat_computer import mcp_adapter

    def handler(request):
        if request.url.path == "/host/wake":
            return ready_response(request)
        if request.url.path == "/v1/tools":
            return httpx.Response(
                200,
                json={"tools": [{"name": "computer_observe", "inputSchema": {"type": "object"}}]},
            )
        if request.url.path == "/v1/preview":
            return httpx.Response(
                200,
                json={
                    "computer_id": "bound",
                    "status": "withheld",
                    "reason": "private_input",
                    "frame": None,
                },
            )
        assert request.url.path == "/v1/tools/call"
        assert json.loads(request.content) == {
            "action_id": "once",
            "name": "computer_observe",
            "arguments": {},
        }
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "Observed"}], "metrics": {}}
        )

    async def exercise(adapter):
        results = []
        for _ in range(2):
            results.extend(await adapter.list_tools())
            results.append(await adapter.preview({}))
        results.append(await adapter.call_tool("computer_observe", {}, "once"))
        return results

    requests, _, events, results = runtime_fixture(monkeypatch, handler, exercise=exercise)
    await mcp_adapter.run("http://127.0.0.1:18761", {"token": CONTROL}, host_token=HOST)
    assert [request.url.path for request in requests] == [
        "/host/wake",
        "/v1/tools",
        "/v1/preview",
        "/v1/tools",
        "/v1/preview",
        "/v1/tools/call",
    ]
    assert events[:3] == ["/host/wake", "stdio", "initialized"]
    for request in requests:
        assert request.headers["authorization"] == "Bearer " + CONTROL
        assert request.headers["X-Meerkat-Computer-Host-Token"] == HOST
        assert HOST.encode() not in request.content and CONTROL.encode() not in request.content
    serialized = "".join(result.model_dump_json() for result in results) + caplog.text
    assert HOST not in serialized and CONTROL not in serialized


@pytest.mark.parametrize(
    "failure",
    [
        "transport",
        "redirect",
        "denied",
        "http_error",
        "invalid_json",
        "missing_ready",
        "false_ready",
        "integer_ready",
        "null_body",
        "error_with_ready",
    ],
)
async def test_gateway_preflight_failure_is_safe_and_never_starts_stdio_or_retries(
    monkeypatch, failure, caplog
):
    from meerkat_computer import mcp_adapter

    def handler(request):
        if failure == "transport":
            raise httpx.ReadTimeout(HOST + CONTROL + SENTINEL, request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "http://localhost:9999/" + SENTINEL})
        if failure == "denied":
            return httpx.Response(401, json={"error": {"effect": "none", "message": HOST}})
        if failure == "http_error":
            return httpx.Response(503, json={"ready": True, "message": SENTINEL})
        if failure == "invalid_json":
            return httpx.Response(200, text=HOST + SENTINEL)
        bodies = {
            "missing_ready": {},
            "false_ready": {"ready": False},
            "integer_ready": {"ready": 1},
            "null_body": None,
            "error_with_ready": {"ready": True, "error": None},
        }
        return httpx.Response(200, json=bodies[failure])

    requests, _, events, _ = runtime_fixture(monkeypatch, handler)
    with pytest.raises(RuntimeError) as caught:
        await mcp_adapter.run("http://127.0.0.1:18761", {"token": CONTROL}, host_token=HOST)
    assert len(requests) == 1 and events == ["/host/wake"]
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    diagnostic = str(caught.value) + repr(caught.value) + caplog.text
    assert all(value not in diagnostic for value in (HOST, CONTROL, SENTINEL))


@pytest.mark.parametrize(
    "url",
    [
        "http://broker",
        "https://localhost",
        "http://localhost/path",
        "http://localhost?x=1",
        "http://user:password@localhost",
        "http://localhost:bad",
        "http://localhost:0",
    ],
)
async def test_gateway_origin_is_rejected_before_constructing_transport(monkeypatch, url):
    from meerkat_computer import mcp_adapter

    requests, settings, events, _ = runtime_fixture(monkeypatch, ready_response)
    with pytest.raises(ValueError):
        await mcp_adapter.run(url, {"token": CONTROL}, host_token=HOST)
    assert not requests and not settings and not events


@pytest.mark.parametrize(
    "host_token",
    ["", "h" * 31, "h" * 4097, "h" * 32 + "\n", "h" * 32 + " ", "h" * 32 + "\u00e4", 123],
)
async def test_invalid_host_token_never_constructs_transport(monkeypatch, host_token):
    from meerkat_computer import mcp_adapter

    requests, settings, events, _ = runtime_fixture(monkeypatch, ready_response)
    with pytest.raises(ValueError):
        await mcp_adapter.run("http://localhost", {"token": CONTROL}, host_token=host_token)
    assert not requests and not settings and not events


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, 86401])
async def test_invalid_timeout_never_constructs_transport(monkeypatch, timeout):
    from meerkat_computer import mcp_adapter

    requests, settings, events, _ = runtime_fixture(monkeypatch, ready_response)
    with pytest.raises(ValueError):
        await mcp_adapter.run("http://localhost", {"token": CONTROL}, timeout=timeout)
    assert not requests and not settings and not events


@pytest.mark.parametrize("url", ["http://broker", "https://broker", "http://localhost:18760"])
async def test_direct_adapter_retains_existing_origins_without_preflight(monkeypatch, url):
    from meerkat_computer import mcp_adapter

    requests, settings, events, _ = runtime_fixture(monkeypatch, ready_response)
    await mcp_adapter.run(url, {"token": CONTROL})
    assert settings[0]["timeout"] == 150 and not requests
    assert events == ["stdio", "initialized"]


def test_cli_reads_only_explicit_host_token_env_and_passes_timeout(tmp_path, monkeypatch):
    from meerkat_computer import mcp_adapter

    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"token": CONTROL}))
    path.chmod(0o600)
    monkeypatch.setenv("MC_ADAPTER_TEST_HOST_TOKEN", HOST)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meerkat-computer-mcp",
            "--broker-url",
            "http://127.0.0.1:18761",
            "--binding-file",
            str(path),
            "--host-token-env",
            "MC_ADAPTER_TEST_HOST_TOKEN",
            "--timeout",
            "900",
            "--mcp-apps",
        ],
    )
    calls = []

    async def run(url, binding, **kwargs):
        calls.append((url, binding, kwargs))

    monkeypatch.setattr(mcp_adapter, "run", run)
    mcp_adapter.main()
    assert calls == [
        (
            "http://127.0.0.1:18761",
            {"token": CONTROL},
            {"mcp_apps": True, "host_token": HOST, "timeout": 900.0},
        )
    ]


@pytest.mark.parametrize(
    "name,value",
    [
        ("MC_ADAPTER_TEST_HOST_TOKEN", None),
        ("MC_ADAPTER_TEST_HOST_TOKEN", ""),
        ("MC_ADAPTER_TEST_HOST_TOKEN", "short"),
        ("MC_ADAPTER_TEST_HOST_TOKEN", HOST + "\n"),
        ("bad-name", HOST),
        ("", HOST),
    ],
)
def test_cli_invalid_host_env_fails_safely_before_startup(
    tmp_path, monkeypatch, capsys, name, value
):
    from meerkat_computer import mcp_adapter

    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"token": CONTROL}))
    path.chmod(0o600)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    elif name:
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meerkat-computer-mcp",
            "--broker-url",
            "http://localhost",
            "--binding-file",
            str(path),
            "--host-token-env",
            name,
        ],
    )
    calls = []

    async def run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(mcp_adapter, "run", run)
    with pytest.raises(SystemExit) as caught:
        mcp_adapter.main()
    assert caught.value.code == 2 and not calls
    diagnostic = capsys.readouterr().err
    assert HOST not in diagnostic and CONTROL not in diagnostic
