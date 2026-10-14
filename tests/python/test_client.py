import dataclasses
import json

import httpx
import pytest
from mcp import types
from meerkat_computer.client import (
    ComputerClient,
    ComputerClientError,
    ComputerGrant,
    ComputerLease,
    ComputerView,
    EffectUnknown,
    GrantUnavailable,
)

OPERATOR = "operator-test-secret-" + "o" * 40
CONTROL = "control-test-secret-" + "c" * 40
HOST = "host-test-secret-" + "h" * 40
SENTINEL = "private-diagnostic-sentinel"
ATTACH = {
    "owner": {"kind": "identity", "realm": "test", "namespace": "mob", "identity": "agent"},
    "slot": "main",
    "profile": "desktop",
    "session_id": "session-one",
    "runtime_generation": 7,
    "ttl_seconds": 3600,
}
LEASE = {
    "computer_id": "computer-one",
    "lease_id": "lease-one",
    "epoch": 3,
    "expires_at_ms": 4000000000000,
    "session_id": ATTACH["session_id"],
    "runtime_generation": ATTACH["runtime_generation"],
    "capabilities": ["browser", "desktop", "screenshots", "exec", "files"],
}
COMPUTER = {
    "computer_id": LEASE["computer_id"],
    "owner": ATTACH["owner"],
    "slot": ATTACH["slot"],
    "profile": ATTACH["profile"],
    "phase": "Leased",
    "epoch": LEASE["epoch"],
    "lease": LEASE,
}
TOOL = {
    "name": "computer_observe",
    "description": "Observe the assigned computer",
    "inputSchema": {"type": "object", "properties": {"surface": {"type": "string"}}},
    "_meta": {"example": "retained"},
}
CONTENT = [
    {"type": "text", "text": "Observed"},
    {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="},
]
METRICS = {"delta": {"clicks": 1}, "counts_are_lower_bounds": True}


class Broker:
    def __init__(self, **client_options):
        self.requests = []
        self.attachment = {**LEASE, "token": CONTROL}
        self.reply = httpx.Response(200, json={"tools": [TOOL]})
        self.client = ComputerClient(
            "http://127.0.0.1:18760",
            operator_token=OPERATOR,
            transport=httpx.MockTransport(self.handle),
            **client_options,
        )

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/attachments":
            return httpx.Response(200, json=self.attachment)
        if callable(self.reply):
            return self.reply(request)
        return self.reply

    def attach(self, **overrides):
        return self.client.attach(**{**ATTACH, **overrides})


@pytest.fixture
def broker():
    instance = Broker()
    yield instance
    instance.client.close()


def assert_safe(error):
    assert OPERATOR not in str(error) + repr(error)
    assert CONTROL not in str(error) + repr(error)
    assert HOST not in str(error) + repr(error)
    assert SENTINEL not in str(error) + repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_attach_fields_redaction_and_authority(broker):
    grant = broker.attach()
    assert isinstance(grant, ComputerGrant)
    for field, value in LEASE.items():
        assert getattr(grant, field) == (tuple(value) if field == "capabilities" else value)
    assert grant.token == CONTROL
    assert CONTROL not in repr(grant) and OPERATOR not in repr(broker.client)
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.session_id = "other"
    request = broker.requests[0]
    assert request.headers["authorization"] == "Bearer " + OPERATOR
    assert json.loads(request.content) == {**ATTACH, "takeover": False}


@pytest.mark.parametrize("client_options", [{}, {"host_token": None}])
def test_direct_broker_has_no_host_credential_header(client_options):
    broker = Broker(**client_options)
    with broker.client:
        grant = broker.attach()
        broker.client.list_tools(grant)
    assert all(
        "X-Meerkat-Computer-Host-Token" not in request.headers for request in broker.requests
    )


def test_host_credential_routes_all_operations_without_changing_broker_authority(caplog):
    broker = Broker(host_token=HOST)
    with broker.client:
        grant = broker.attach()
        broker.client.list_tools(grant)
        broker.reply = httpx.Response(200, json={"content": CONTENT, "metrics": METRICS})
        result = broker.client.call_tool(grant, "computer_observe", {}, "gateway-call")
        broker.reply = httpx.Response(200, json={"lease": LEASE})
        renewed = broker.client.renew(grant, 60)
        broker.reply = httpx.Response(200, json={"released": True})
        broker.client.release(renewed)
    assert [request.url.path for request in broker.requests] == [
        "/v1/attachments",
        "/v1/tools",
        "/v1/tools/call",
        "/v1/lease/renew",
        "/v1/lease/release",
    ]
    for index, request in enumerate(broker.requests):
        assert request.url.host == "127.0.0.1" and request.url.port == 18760
        assert request.headers["X-Meerkat-Computer-Host-Token"] == HOST
        assert request.headers["authorization"] == "Bearer " + (CONTROL if index else OPERATOR)
        assert HOST.encode() not in request.content
    assert grant == renewed
    assert HOST not in repr(grant) + repr(broker.client) + result.model_dump_json() + caplog.text


@pytest.mark.parametrize(
    "host_token",
    [
        "",
        "h" * 31,
        "h" * 513,
        True,
        123,
        b"h" * 40,
        "h" * 40 + "\n",
        "h" * 40 + " ",
        "h" * 40 + "\t",
        "h" * 40 + "\x7f",
        "h" * 40 + "\u00e4",
        "h" * 40 + "\r\nInjected: x",
    ],
)
def test_invalid_host_credential_is_a_safe_configuration_error(host_token):
    with pytest.raises(ComputerClientError) as caught:
        ComputerClient("http://localhost", operator_token=OPERATOR, host_token=host_token)
    assert caught.value.code == "invalid_configuration"
    assert caught.value.effect == "none"
    assert_safe(caught.value)


def test_ensure_host_is_a_noop_without_gateway_configuration(broker):
    assert broker.client.ensure_host() is None
    assert broker.requests == []


def test_ensure_host_is_explicit_and_does_not_forward_a_guest_action():
    broker = Broker(host_token=HOST)
    broker.reply = httpx.Response(200, json={"ready": True})
    with broker.client:
        assert broker.client.ensure_host() is None
    assert len(broker.requests) == 1
    request = broker.requests[0]
    assert request.method == "POST" and request.url.path == "/host/wake"
    assert request.headers["authorization"] == "Bearer " + OPERATOR
    assert request.headers["X-Meerkat-Computer-Host-Token"] == HOST
    assert not request.content


@pytest.mark.parametrize("ready", [None, False, 0, 1, "true", [], {}])
def test_ensure_host_requires_an_explicit_ready_receipt(ready):
    broker = Broker(host_token=HOST)
    broker.reply = httpx.Response(200, json={"ready": ready, "message": HOST + SENTINEL})
    with broker.client, pytest.raises(EffectUnknown) as caught:
        broker.client.ensure_host()
    assert caught.value.code == "invalid_host_receipt"
    assert len(broker.requests) == 1
    assert_safe(caught.value)


@pytest.mark.parametrize("effect", ["none", "unknown"])
def test_ensure_host_preserves_gateway_effect_without_retry(effect):
    broker = Broker(host_token=HOST)
    broker.reply = httpx.Response(
        503, json={"error": {"code": "host_unavailable", "effect": effect, "message": HOST}}
    )
    expected = EffectUnknown if effect == "unknown" else ComputerClientError
    with broker.client, pytest.raises(expected) as caught:
        broker.client.ensure_host()
    assert type(caught.value) is expected
    assert caught.value.code == "host_unavailable" and caught.value.effect == effect
    assert len(broker.requests) == 1
    assert_safe(caught.value)


def test_list_computers_returns_typed_token_free_views_and_never_wakes():
    broker = Broker(host_token=HOST)
    sleeping = {
        **COMPUTER,
        "computer_id": "computer-two",
        "owner": {"kind": "session", "realm": "test", "session_id": "other-session"},
        "phase": "FutureBrokerPhase",
        "epoch": 0,
        "lease": None,
    }
    broker.reply = httpx.Response(
        200,
        json={
            "computers": [
                {
                    **COMPUTER,
                    "lease": {**LEASE, "token": CONTROL},
                    "token": HOST,
                    "last_backend_failure": {"message": SENTINEL},
                },
                sleeping,
            ]
        },
    )
    with broker.client:
        result = broker.client.list_computers()
    assert len(result) == 2
    first, second = result
    assert isinstance(first, ComputerView) and isinstance(first.lease, ComputerLease)
    assert not isinstance(first.lease, ComputerGrant)
    for field in ("computer_id", "owner", "slot", "profile", "phase", "epoch"):
        assert getattr(first, field) == COMPUTER[field]
    for field, value in LEASE.items():
        assert getattr(first.lease, field) == (tuple(value) if field == "capabilities" else value)
    assert second.owner == sleeping["owner"] and second.lease is None
    assert second.phase == "FutureBrokerPhase" and second.epoch == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.phase = "Stopped"
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.lease.epoch = 100
    with pytest.raises(TypeError):
        first.owner["identity"] = "other"
    assert CONTROL not in repr(result) and HOST not in repr(result) and SENTINEL not in repr(result)
    assert len(broker.requests) == 1
    request = broker.requests[0]
    assert request.method == "GET" and request.url.path == "/v1/computers"
    assert request.headers["authorization"] == "Bearer " + OPERATOR
    assert request.headers["X-Meerkat-Computer-Host-Token"] == HOST


def test_list_computers_accepts_an_empty_inventory(broker):
    broker.reply = httpx.Response(200, json={"computers": []})
    assert broker.client.list_computers() == []
    assert len(broker.requests) == 1


def test_list_computers_does_not_wake_an_asleep_host():
    broker = Broker(host_token=HOST)
    broker.reply = httpx.Response(
        503, json={"error": {"code": "host_asleep", "effect": "none", "message": SENTINEL}}
    )
    with broker.client, pytest.raises(ComputerClientError) as caught:
        broker.client.list_computers()
    assert type(caught.value) is ComputerClientError
    assert caught.value.code == "host_asleep" and caught.value.effect == "none"
    assert [request.url.path for request in broker.requests] == ["/v1/computers"]
    assert_safe(caught.value)


@pytest.mark.parametrize("operation", ["list_computers", "ensure_host"])
def test_host_management_transport_loss_is_safe_and_not_retried(operation):
    broker = Broker(host_token=HOST)

    def fail(request):
        raise httpx.ReadTimeout(HOST + SENTINEL, request=request)

    broker.reply = fail
    expected = EffectUnknown if operation == "ensure_host" else ComputerClientError
    with broker.client, pytest.raises(expected) as caught:
        getattr(broker.client, operation)()
    assert type(caught.value) is expected
    assert caught.value.effect == ("unknown" if operation == "ensure_host" else "none")
    assert len(broker.requests) == 1
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"computers": None},
        {"computers": {}},
        {"computers": [None]},
        {"computers": [{**COMPUTER, "computer_id": ""}]},
        {"computers": [{**COMPUTER, "owner": {"kind": "identity"}}]},
        {"computers": [{**COMPUTER, "owner": {**ATTACH["owner"], "identity": "\ud800"}}]},
        {"computers": [{**COMPUTER, "slot": 1}]},
        {"computers": [{**COMPUTER, "profile": None}]},
        {"computers": [{**COMPUTER, "phase": [SENTINEL]}]},
        {"computers": [{**COMPUTER, "epoch": True}]},
        {"computers": [{**COMPUTER, "lease": {}}]},
        {"computers": [{**COMPUTER, "lease": {**LEASE, "computer_id": "other"}}]},
        {"computers": [{**COMPUTER, "lease": {**LEASE, "epoch": LEASE["epoch"] + 1}}]},
        {"computers": [{**COMPUTER, "lease": {**LEASE, "expires_at_ms": -1}}]},
    ],
)
def test_list_computers_rejects_malformed_inventory_safely(broker, body):
    broker.reply = httpx.Response(200, content=json.dumps(body))
    with pytest.raises(ComputerClientError) as caught:
        broker.client.list_computers()
    assert type(caught.value) is ComputerClientError
    assert caught.value.code == "invalid_computer_inventory" and caught.value.effect == "none"
    assert len(broker.requests) == 1
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://example.com",
        "http://127.0.0.1.example.com",
        "http://name:secret@localhost",
        "http://localhost/path",
        "http://localhost/?secret=x",
        "http://localhost/#secret",
        "file:///tmp/broker",
        "http://localhost:bad",
        "http://",
    ],
)
def test_broker_origin_fails_closed(url):
    with pytest.raises(ComputerClientError) as caught:
        ComputerClient(url, operator_token=OPERATOR)
    assert caught.value.code == "invalid_configuration"
    assert caught.value.effect == "none"
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "url", ["http://localhost", "http://127.0.0.1:18760/", "http://[::1]:18760"]
)
def test_loopback_origins_are_supported(url):
    with ComputerClient(url, operator_token=OPERATOR) as client:
        assert client is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "other-session"),
        ("runtime_generation", 8),
        ("runtime_generation", True),
        ("epoch", 0),
        ("epoch", True),
        ("computer_id", ""),
        ("lease_id", None),
        ("expires_at_ms", -1),
        ("token", "short"),
        ("token", "x" * 40 + "\n"),
        ("capabilities", ["browser", "browser"]),
        ("capabilities", "browser"),
    ],
)
def test_invalid_attachment_never_exposes_grant(broker, field, value):
    broker.attachment[field] = value
    broker.attachment["diagnostic"] = SENTINEL
    with pytest.raises(EffectUnknown) as caught:
        broker.attach()
    assert caught.value.code == "invalid_attachment"
    assert len(broker.requests) == 1
    assert_safe(caught.value)


def test_list_tools_preserves_official_schema_and_metadata(broker):
    grant = broker.attach()
    result = broker.client.list_tools(grant)
    assert isinstance(result[0], types.Tool)
    assert result[0].model_dump(by_alias=True, exclude_none=True) == TOOL
    assert broker.requests[-1].headers["authorization"] == "Bearer " + CONTROL


def test_call_preserves_content_and_receipt(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(200, json={"content": CONTENT, "metrics": METRICS})
    result = broker.client.call_tool(grant, "computer_observe", {"surface": "desktop"}, "call-one")
    assert isinstance(result, types.CallToolResult) and not result.isError
    assert [
        block.model_dump(by_alias=True, exclude_none=True) for block in result.content
    ] == CONTENT
    assert result.structuredContent == {"action_id": "call-one", "metrics": METRICS}
    request = broker.requests[-1]
    assert request.headers["authorization"] == "Bearer " + CONTROL
    assert json.loads(request.content) == {
        "action_id": "call-one",
        "name": "computer_observe",
        "arguments": {"surface": "desktop"},
    }


def test_generated_action_id_is_returned_and_not_reused(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(200, json={"content": [], "metrics": {}})
    ids = [
        broker.client.call_tool(grant, "computer_observe", {}).structuredContent["action_id"]
        for _ in range(2)
    ]
    assert len(set(ids)) == 2
    assert ids == [json.loads(request.content)["action_id"] for request in broker.requests[1:]]


def test_confirmed_tool_rejection_is_safe_mcp_error(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(
        403,
        json={
            "error": {"code": "capability_denied", "effect": "none", "message": SENTINEL},
            "metrics": METRICS,
        },
    )
    result = broker.client.call_tool(grant, "workspace_exec", {}, "blocked-call")
    assert result.isError
    receipt = json.loads(result.content[0].text)
    assert receipt["error"]["code"] == "capability_denied"
    assert receipt["error"]["effect"] == "none"
    assert result.structuredContent == {"action_id": "blocked-call", "metrics": METRICS}
    assert SENTINEL not in result.model_dump_json()


def test_unknown_effect_never_retries_and_keeps_action_id(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(
        409,
        json={
            "error": {"code": "unknown_effect", "effect": "unknown", "message": SENTINEL},
            "metrics": METRICS,
        },
    )
    with pytest.raises(EffectUnknown) as caught:
        broker.client.call_tool(grant, "computer_act", {}, "uncertain-call")
    assert caught.value.action_id == "uncertain-call"
    assert caught.value.effect == "unknown" and caught.value.status == 409
    assert caught.value.metrics == METRICS
    assert len(broker.requests) == 2
    assert_safe(caught.value)


@pytest.mark.parametrize("operation", ["list", "call", "renew", "release"])
def test_transport_failure_has_safe_effect_and_no_retry(broker, operation):
    grant = broker.attach()

    def fail(request):
        raise httpx.ReadTimeout(SENTINEL + CONTROL, request=request)

    broker.reply = fail
    expected = ComputerClientError if operation == "list" else EffectUnknown
    with pytest.raises(expected) as caught:
        invoke(broker, grant, operation)
    assert caught.value.effect == ("none" if operation == "list" else "unknown")
    assert len(broker.requests) == 2
    assert_safe(caught.value)


def invoke(broker, grant, operation):
    if operation == "list":
        return broker.client.list_tools(grant)
    if operation == "renew":
        return broker.client.renew(grant, 3600)
    if operation == "release":
        return broker.client.release(grant)
    return broker.client.call_tool(grant, "computer_act", {}, "action-one")


@pytest.mark.parametrize("operation", ["list", "call", "renew", "release"])
@pytest.mark.parametrize("effect", ["none", "unknown"])
@pytest.mark.parametrize(
    "code", ["host_unavailable", "host_asleep", "host_busy", "host_effect_unknown"]
)
def test_host_errors_preserve_declared_effect_and_never_retry(broker, operation, effect, code):
    grant = broker.attach()
    broker.reply = httpx.Response(
        503,
        json={
            "error": {"code": code, "effect": effect, "message": HOST + SENTINEL},
            "metrics": METRICS,
        },
    )
    if effect == "none" and operation == "call":
        result = invoke(broker, grant, operation)
        assert result.isError
        assert json.loads(result.content[0].text)["error"] == {"code": code, "effect": effect}
        assert result.structuredContent == {"action_id": "action-one", "metrics": METRICS}
        assert HOST not in result.model_dump_json() and SENTINEL not in result.model_dump_json()
    else:
        expected = EffectUnknown if effect == "unknown" else ComputerClientError
        with pytest.raises(expected) as caught:
            invoke(broker, grant, operation)
        assert type(caught.value) is expected
        assert caught.value.code == code
        assert caught.value.effect == effect
        assert caught.value.status == 503
        assert caught.value.action_id == ("action-one" if operation == "call" else None)
        assert caught.value.metrics == METRICS
        assert_safe(caught.value)
    assert len(broker.requests) == 2


def test_host_failure_before_attach_has_no_effect_and_never_retries():
    requests = []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(
            503,
            json={"error": {"code": "host_unavailable", "effect": "none", "message": HOST}},
        )

    with (
        ComputerClient(
            "http://localhost",
            operator_token=OPERATOR,
            host_token=HOST,
            transport=httpx.MockTransport(unavailable),
        ) as client,
        pytest.raises(ComputerClientError) as caught,
    ):
        client.attach(**ATTACH)
    assert type(caught.value) is ComputerClientError
    assert caught.value.code == "host_unavailable" and caught.value.effect == "none"
    assert len(requests) == 1
    assert_safe(caught.value)


def test_host_credential_is_not_exposed_by_transport_errors(caplog):
    broker = Broker(host_token=HOST)

    def fail(request):
        raise httpx.ReadTimeout(HOST + SENTINEL, request=request)

    with broker.client:
        grant = broker.attach()
        broker.reply = fail
        with pytest.raises(EffectUnknown) as caught:
            broker.client.call_tool(grant, "computer_act", {}, "gateway-unknown")
    assert caught.value.action_id == "gateway-unknown"
    assert len(broker.requests) == 2
    assert HOST not in caplog.text
    assert_safe(caught.value)


def test_host_credential_is_not_forwarded_to_redirect_target():
    broker = Broker(host_token=HOST)
    with broker.client:
        grant = broker.attach()
        broker.reply = httpx.Response(307, headers={"Location": "http://localhost:9999/"})
        with pytest.raises(EffectUnknown) as caught:
            broker.client.call_tool(grant, "computer_act", {}, "gateway-redirect")
    assert len(broker.requests) == 2
    assert all(request.url.port == 18760 for request in broker.requests)
    assert_safe(caught.value)


@pytest.mark.parametrize("operation", ["list", "call", "renew", "release"])
def test_expired_or_replaced_grant_is_unavailable(broker, operation):
    grant = broker.attach()
    broker.reply = httpx.Response(
        401,
        json={
            "error": {"code": "lease_expired", "effect": "none", "message": SENTINEL},
        },
    )
    with pytest.raises(GrantUnavailable) as caught:
        invoke(broker, grant, operation)
    assert caught.value.code == "lease_expired" and caught.value.status == 401
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "payload", [None, [], {"content": "bad"}, {"content": [{"type": "audio"}]}]
)
def test_malformed_action_response_is_unknown(broker, payload):
    grant = broker.attach()
    broker.reply = httpx.Response(200, json=payload)
    with pytest.raises(EffectUnknown) as caught:
        broker.client.call_tool(grant, "computer_act", {}, "malformed-call")
    assert caught.value.action_id == "malformed-call"
    assert_safe(caught.value)


def test_redirect_is_not_followed_and_action_is_unknown(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(307, headers={"Location": "https://example.com/" + SENTINEL})
    with pytest.raises(EffectUnknown):
        broker.client.call_tool(grant, "computer_act", {}, "redirect-call")
    assert len(broker.requests) == 2


def test_renew_preserves_binding_and_token(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(200, json={"lease": {**LEASE, "expires_at_ms": 4000000010000}})
    renewed = broker.client.renew(grant, 60)
    assert renewed == dataclasses.replace(grant, expires_at_ms=4000000010000)
    assert renewed.token == CONTROL
    assert json.loads(broker.requests[-1].content) == {"ttl_seconds": 60}
    assert broker.requests[-1].headers["authorization"] == "Bearer " + CONTROL


@pytest.mark.parametrize(
    "field,value",
    [
        ("computer_id", "other-computer"),
        ("lease_id", "other-lease"),
        ("epoch", 4),
        ("session_id", "other-session"),
        ("runtime_generation", 8),
        ("capabilities", ["browser"]),
    ],
)
def test_renew_refuses_changed_binding(broker, field, value):
    grant = broker.attach()
    broker.reply = httpx.Response(200, json={"lease": {**LEASE, field: value}})
    with pytest.raises(GrantUnavailable) as caught:
        broker.client.renew(grant, 60)
    assert caught.value.code == "binding_mismatch"
    assert_safe(caught.value)


def test_release_and_close_have_separate_responsibilities(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(200, json={"released": True})
    assert broker.client.release(grant) is None
    assert broker.requests[-1].url.path == "/v1/lease/release"
    assert broker.requests[-1].headers["authorization"] == "Bearer " + CONTROL
    count = len(broker.requests)
    broker.client.close()
    broker.client.close()
    assert len(broker.requests) == count


def test_context_exit_never_implicitly_releases(broker):
    with broker.client:
        broker.attach()
    assert [request.url.path for request in broker.requests] == ["/v1/attachments"]


@pytest.mark.parametrize(
    "changes",
    [
        {"runtime_generation": True},
        {"ttl_seconds": 0},
        {"ttl_seconds": 86401},
        {"takeover": 1},
        {"session_id": ""},
        {"owner": {"kind": "identity"}},
    ],
)
def test_invalid_attach_input_has_no_effect(broker, changes):
    with pytest.raises(ComputerClientError) as caught:
        broker.attach(**changes)
    assert caught.value.effect == "none"
    assert not broker.requests
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "status,error",
    [
        (401, {"code": [SENTINEL]}),
        (409, {"code": [SENTINEL], "effect": [SENTINEL]}),
        (409, {"code": SENTINEL, "effect": "unknown", "message": CONTROL}),
    ],
)
def test_malformed_error_is_safe_and_never_retried(broker, status, error):
    grant = broker.attach()
    broker.reply = httpx.Response(status, json={"error": error})
    with pytest.raises(ComputerClientError) as caught:
        broker.client.call_tool(grant, "computer_act", {}, "malformed-error")
    assert caught.value.action_id == "malformed-error"
    assert_safe(caught.value)
    assert len(broker.requests) == 2


def test_unknown_effect_metrics_exclude_untrusted_text_and_invalid_counters(broker):
    grant = broker.attach()
    broker.reply = httpx.Response(
        409,
        json={
            "error": {"code": "unknown_effect", "effect": "unknown"},
            "metrics": {
                "action_id": CONTROL,
                "diagnostic": SENTINEL,
                "duration_ms": 12,
                "delta": {"clicks": 1, "ui_commands": CONTROL, "observations": 10**400},
                "totals": {"clicks": -1, "ui_commands": True},
                "counts_are_lower_bounds": True,
            },
        },
    )
    with pytest.raises(EffectUnknown) as caught:
        broker.client.call_tool(grant, "computer_act", {}, "metrics-call")
    assert caught.value.metrics == {
        "duration_ms": 12,
        "delta": {"clicks": 1},
        "totals": {},
        "counts_are_lower_bounds": True,
    }
    assert_safe(caught.value)


def test_attach_transport_loss_is_unknown_and_never_retried():
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ConnectError(CONTROL + SENTINEL, request=request)

    with (
        ComputerClient(
            "http://localhost", operator_token=OPERATOR, transport=httpx.MockTransport(fail)
        ) as client,
        pytest.raises(EffectUnknown) as caught,
    ):
        client.attach(**ATTACH)
    assert len(requests) == 1
    assert caught.value.action_id is None
    assert_safe(caught.value)


@pytest.mark.parametrize("arguments", [{"bad": "\ud800"}, {"bad": float("nan")}, {"bad": object()}])
def test_unserializable_arguments_fail_safely_before_action(broker, arguments):
    grant = broker.attach()
    with pytest.raises(ComputerClientError) as caught:
        broker.client.call_tool(grant, "workspace_write", arguments, "invalid-json")
    assert caught.value.effect == "none"
    assert len(broker.requests) == 1
    assert_safe(caught.value)


def test_close_prevents_further_requests(broker):
    grant = broker.attach()
    broker.client.close()
    with pytest.raises(ComputerClientError, match="client_closed"):
        broker.client.list_tools(grant)
    assert len(broker.requests) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_workspace_counters_survive_success_and_unknown_effect(broker, unknown):
    grant = broker.attach()
    counters = {
        name: index
        for index, name in enumerate(
            (
                "file_reads",
                "file_writes",
                "file_bytes_read",
                "file_bytes_written",
                "exec_commands",
                "exec_timeouts",
            ),
            start=1,
        )
    }
    metrics = {"delta": counters, "totals": counters}
    body = {"content": CONTENT, "metrics": metrics}
    if unknown:
        body = {"error": {"code": "unknown_effect", "effect": "unknown"}, "metrics": metrics}
    broker.reply = httpx.Response(409 if unknown else 200, json=body)
    if unknown:
        with pytest.raises(EffectUnknown) as caught:
            broker.client.call_tool(grant, "workspace_exec", {}, "workspace-call")
        assert caught.value.metrics == metrics
    else:
        result = broker.client.call_tool(grant, "workspace_exec", {}, "workspace-call")
        assert result.structuredContent["metrics"] == metrics


def test_non_utf8_owner_is_safe_and_never_submitted(broker):
    with pytest.raises(ComputerClientError) as caught:
        broker.attach(owner={**ATTACH["owner"], "identity": "\ud800"})
    assert_safe(caught.value)
    assert not broker.requests
