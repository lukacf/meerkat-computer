"""Trusted host transfers use bounded bytes, never agent-selected host paths."""

import dataclasses
import hashlib

import httpx
import pytest
from meerkat_computer.client import (
    ComputerClient,
    ComputerClientError,
    ComputerGrant,
    EffectUnknown,
    GrantUnavailable,
)

COMPUTER = "11cf8191-112f-4c5b-ab7f-48dc3874faf7"
FILE = "53e71164-49ec-42e9-bd98-5811af180f5a"
FOREIGN = "c9077091-a8cd-49b9-8cce-e44014793688"
BASE = f"/v1/computers/{COMPUTER}/files"
OPERATOR = "transfer-operator-" + "o" * 40
HOST = "transfer-host-" + "h" * 40
PRIVATE = "private-response-or-request-diagnostic"
DATA = b"\x00\xffsynthetic file bytes\n"
MAX_FILE_BYTES = 16 * 1024 * 1024


def metadata(**overrides):
    return {
        "file_id": FILE,
        "filename": "example.bin",
        "direction": "export",
        "size_bytes": len(DATA),
        "sha256": hashlib.sha256(DATA).hexdigest(),
        "created_at_ms": 1000,
        **overrides,
    }


class Broker:
    def __init__(self):
        self.requests = []
        self.replies = []
        self.client = ComputerClient(
            "http://127.0.0.1:18760",
            operator_token=OPERATOR,
            host_token=HOST,
            transport=httpx.MockTransport(self.handle),
        )

    def handle(self, request):
        self.requests.append(request)
        assert self.replies, "unexpected request, including any retry"
        reply = self.replies.pop(0)
        return reply(request) if callable(reply) else reply

    def inventory(self, *files):
        self.replies.append(httpx.Response(200, json={"files": list(files)}))

    def download(self, data=DATA, *, headers=None, stream=None):
        response_headers = {"Content-Type": "application/octet-stream", **(headers or {})}
        self.replies.append(
            httpx.Response(200, headers=response_headers, stream=stream)
            if stream is not None
            else httpx.Response(200, headers=response_headers, content=data)
        )


@pytest.fixture
def broker():
    instance = Broker()
    yield instance
    instance.client.close()


def assert_safe(error):
    for secret in (OPERATOR, HOST, PRIVATE):
        assert secret not in str(error) + repr(error) + repr(vars(error))
    assert error.__cause__ is None
    assert error.__context__ is None


def test_stage_bytes_filename_encoding_and_private_headers(broker, caplog):
    name = "r\u00e9sum\u00e9 & 2026.bin"
    broker.replies.append(httpx.Response(201, json=metadata(direction="import", filename=name)))
    item = broker.client.stage_file(COMPUTER, name, DATA)
    assert dataclasses.asdict(item) == metadata(direction="import", filename=name)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.filename = "changed"
    request = broker.requests[0]
    assert request.method == "POST" and request.url.path == BASE
    assert dict(request.url.params) == {"filename": name}
    assert request.headers["Content-Type"] == "application/octet-stream"
    assert request.headers["Authorization"] == "Bearer " + OPERATOR
    assert request.headers["X-Meerkat-Computer-Host-Token"] == HOST
    assert request.content == DATA
    assert OPERATOR not in repr(item) + caplog.text
    assert HOST not in repr(item) + caplog.text


def test_list_metadata_is_typed_and_drops_unknown_private_fields(broker):
    broker.inventory(metadata(diagnostic=PRIVATE, token=OPERATOR))
    items = broker.client.list_files(COMPUTER)
    assert [dataclasses.asdict(item) for item in items] == [metadata()]
    assert PRIVATE not in repr(items) and OPERATOR not in repr(items)
    assert broker.requests[0].method == "GET" and broker.requests[0].url.path == BASE


def test_download_looks_up_exact_export_and_verifies_bytes(broker):
    broker.inventory(metadata(file_id=FOREIGN), metadata())
    broker.download(headers={"X-Content-SHA256": metadata()["sha256"]})
    assert broker.client.download_file(COMPUTER, FILE) == DATA
    assert [request.url.path for request in broker.requests] == [BASE, BASE + "/" + FILE]
    assert all(request.method == "GET" for request in broker.requests)
    assert broker.requests[-1].headers["Accept-Encoding"] == "identity"
    assert all(
        request.headers["Authorization"] == "Bearer " + OPERATOR for request in broker.requests
    )
    assert all(
        request.headers["X-Meerkat-Computer-Host-Token"] == HOST for request in broker.requests
    )


def test_delete_requires_exact_acknowledgement(broker):
    broker.replies.append(httpx.Response(200, json={"deleted": FILE}))
    assert broker.client.delete_file(COMPUTER, FILE) is None
    assert broker.requests[0].method == "DELETE"
    assert broker.requests[0].url.path == BASE + "/" + FILE
    broker.replies.append(httpx.Response(200, json={"deleted": FOREIGN}))
    with pytest.raises(EffectUnknown) as caught:
        broker.client.delete_file(COMPUTER, FILE)
    assert caught.value.code == "invalid_file_receipt"
    assert len(broker.requests) == 2


@pytest.mark.parametrize(
    "value",
    [
        "../host",
        "/tmp/file",
        "https://example.test/file",
        FILE.upper(),
        "g" * 32,
        "",
        1,
        None,
    ],
)
@pytest.mark.parametrize("method", ["stage", "list", "download", "delete"])
def test_invalid_computer_id_is_rejected_without_network(broker, value, method):
    with pytest.raises(ComputerClientError) as caught:
        if method == "stage":
            broker.client.stage_file(value, "example.bin", DATA)
        elif method == "list":
            broker.client.list_files(value)
        elif method == "download":
            broker.client.download_file(value, FILE)
        else:
            broker.client.delete_file(value, FILE)
    assert caught.value.effect == "none"
    assert broker.requests == []


@pytest.mark.parametrize("method", ["stage", "list", "download", "delete"])
def test_existing_broker_computer_ids_are_preserved_exactly(broker, method):
    # Broker.attach and fork use UUID.simple(), while file IDs use UUID.hyphenated().
    computer = COMPUTER.replace("-", "")
    if method == "stage":
        broker.replies.append(httpx.Response(201, json=metadata(direction="import")))
        broker.client.stage_file(computer, "example.bin", DATA)
    elif method == "list":
        broker.inventory(metadata())
        broker.client.list_files(computer)
    elif method == "download":
        broker.inventory(metadata())
        broker.download()
        broker.client.download_file(computer, FILE)
    else:
        broker.replies.append(httpx.Response(200, json={"deleted": FILE}))
        broker.client.delete_file(computer, FILE)
    assert all(
        request.url.path.startswith(f"/v1/computers/{computer}/files")
        for request in broker.requests
    )


@pytest.mark.parametrize(
    "value",
    [
        "../host",
        "/tmp/file",
        "https://example.test/file",
        FILE.upper(),
        FILE.replace("-", ""),
        "",
        1,
        None,
    ],
)
@pytest.mark.parametrize("method", ["download_file", "delete_file"])
def test_invalid_file_id_is_rejected_without_network(broker, value, method):
    with pytest.raises(ComputerClientError):
        getattr(broker.client, method)(COMPUTER, value)
    assert broker.requests == []


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../secrets",
        "/tmp/file",
        "C:\\secret",
        "https://example.test/file",
        "x\x00y",
        "x\ny",
        "x\x85y",
        "x" * 256,
        "\u00e9" * 128,
        "\ud800",
        None,
    ],
)
def test_stage_rejects_paths_and_invalid_filenames(broker, name):
    with pytest.raises(ComputerClientError) as caught:
        broker.client.stage_file(COMPUTER, name, DATA)
    assert caught.value.effect == "none"
    assert broker.requests == []


@pytest.mark.parametrize(
    "data",
    ["/tmp/secret", bytearray(DATA), memoryview(DATA), None, b"x" * (MAX_FILE_BYTES + 1)],
    ids=["path", "bytearray", "memoryview", "none", "oversized"],
)
def test_stage_accepts_only_bounded_bytes(broker, data):
    with pytest.raises(ComputerClientError) as caught:
        broker.client.stage_file(COMPUTER, "example.bin", data)
    assert caught.value.effect == "none" and broker.requests == []


@pytest.mark.parametrize("size", [0, MAX_FILE_BYTES], ids=["empty", "maximum"])
def test_exact_size_boundaries_round_trip(broker, size):
    data = b"x" * size
    item = metadata(size_bytes=size, sha256=hashlib.sha256(data).hexdigest())
    broker.replies.append(httpx.Response(201, json={**item, "direction": "import"}))
    assert broker.client.stage_file(COMPUTER, "example.bin", data).size_bytes == size
    broker.inventory(item)
    broker.download(data=data)
    assert broker.client.download_file(COMPUTER, FILE) == data


@pytest.mark.parametrize(
    "change",
    [
        {"direction": "export"},
        {"filename": "other.bin"},
        {"size_bytes": len(DATA) + 1},
        {"sha256": "0" * 64},
        {"file_id": "../host"},
    ],
)
def test_stage_malformed_or_unbound_receipt_has_unknown_effect(broker, change):
    broker.replies.append(httpx.Response(201, json=metadata(**{"direction": "import", **change})))
    with pytest.raises(EffectUnknown) as caught:
        broker.client.stage_file(COMPUTER, "example.bin", DATA)
    assert caught.value.code == "invalid_file_receipt" and len(broker.requests) == 1


@pytest.mark.parametrize(
    "files",
    [
        None,
        {},
        [None],
        [metadata(), metadata()],
        [metadata(size_bytes=True)],
        [metadata(size_bytes=MAX_FILE_BYTES + 1)],
        [metadata(created_at_ms=-1)],
        [metadata(sha256="A" * 64)],
        [metadata(filename="../escape")],
        [metadata(direction="both")],
    ],
)
def test_list_rejects_malformed_inventory(broker, files):
    broker.replies.append(httpx.Response(200, json={"files": files}))
    with pytest.raises(ComputerClientError) as caught:
        broker.client.list_files(COMPUTER)
    assert caught.value.code == "invalid_file_inventory" and caught.value.effect == "none"


@pytest.mark.parametrize("files", [[], [metadata(file_id=FOREIGN)], [metadata(direction="import")]])
def test_foreign_or_import_file_never_reaches_download_endpoint(broker, files):
    broker.inventory(*files)
    with pytest.raises(ComputerClientError) as caught:
        broker.client.download_file(COMPUTER, FILE)
    assert caught.value.effect == "none" and len(broker.requests) == 1


@pytest.mark.parametrize(
    "data,headers",
    [
        (b"wrong file", {}),
        (DATA[:-1], {}),
        (DATA + b"x", {}),
        (DATA, {"Content-Type": "text/html"}),
        (DATA, {"Content-Encoding": "gzip"}),
        (DATA, {"Content-Length": str(MAX_FILE_BYTES + 1)}),
        (DATA, {"X-Content-SHA256": "0" * 64}),
    ],
)
def test_download_rejects_content_size_and_digest_mismatch(broker, data, headers):
    broker.inventory(metadata())
    # Avoid MockTransport's eager Content-Encoding decode: use a streaming body.
    broker.download(headers=headers, stream=httpx.ByteStream(data))
    with pytest.raises(ComputerClientError) as caught:
        broker.client.download_file(COMPUTER, FILE)
    assert caught.value.effect == "none"
    assert len(broker.requests) == 2
    assert_safe(caught.value)


class Overflow(httpx.SyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    def __iter__(self):
        self.reads += 1
        yield DATA + b"overflow"
        self.reads += 1
        raise AssertionError("client continued beyond declared metadata size")

    def close(self):
        self.closed = True


def test_stream_stops_at_bound_and_closes_response(broker):
    stream = Overflow()
    broker.inventory(metadata())
    broker.download(stream=stream)
    with pytest.raises(ComputerClientError):
        broker.client.download_file(COMPUTER, FILE)
    assert stream.reads == 1 and stream.closed


def test_interrupted_stream_never_returns_partial_bytes_or_retains_http_exception(broker):
    class Broken(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield DATA[:3]
            raise httpx.ReadError(PRIVATE + OPERATOR + HOST)

        def close(self):
            self.closed = True

    stream = Broken()
    broker.inventory(metadata())
    broker.download(stream=stream)
    with pytest.raises(ComputerClientError) as caught:
        broker.client.download_file(COMPUTER, FILE)
    assert caught.value.code == "transport_unavailable" and caught.value.effect == "none"
    assert len(broker.requests) == 2 and stream.closed
    assert_safe(caught.value)


@pytest.mark.parametrize("method", ["stage", "list", "download", "delete"])
def test_transport_failures_are_sanitized_and_never_retried(broker, method):
    def fail(request):
        raise httpx.ReadError(PRIVATE + OPERATOR + HOST, request=request)

    if method == "download":
        broker.inventory(metadata())
    broker.replies.append(fail)
    with pytest.raises(ComputerClientError) as caught:
        if method == "stage":
            broker.client.stage_file(COMPUTER, "example.bin", DATA)
        elif method == "list":
            broker.client.list_files(COMPUTER)
        elif method == "download":
            broker.client.download_file(COMPUTER, FILE)
        else:
            broker.client.delete_file(COMPUTER, FILE)
    assert caught.value.effect == ("unknown" if method in {"stage", "delete"} else "none")
    assert len(broker.requests) == (2 if method == "download" else 1)
    assert_safe(caught.value)


@pytest.mark.parametrize(
    "status,error,effect",
    [
        (401, {"code": PRIVATE, "effect": "unknown"}, "none"),
        (503, {"code": "host_unavailable", "effect": "none"}, "none"),
        (502, {"code": "host_effect_unknown", "effect": "unknown"}, "unknown"),
        (500, {"code": PRIVATE, "effect": "unknown"}, "unknown"),
    ],
)
@pytest.mark.parametrize("method", ["stage", "download"])
def test_errors_keep_only_safe_broker_codes_and_effects(broker, status, error, effect, method):
    if method == "download":
        broker.inventory(metadata())
    broker.replies.append(
        httpx.Response(status, json={"error": {**error, "message": PRIVATE + OPERATOR + HOST}})
    )
    with pytest.raises(ComputerClientError) as caught:
        if method == "stage":
            broker.client.stage_file(COMPUTER, "example.bin", DATA)
        else:
            broker.client.download_file(COMPUTER, FILE)
    assert caught.value.effect == effect and caught.value.status == status
    if status == 401:
        assert isinstance(caught.value, GrantUnavailable)
    assert_safe(caught.value)


@pytest.mark.parametrize("method", ["stage", "download"])
def test_redirects_are_not_followed_or_forwarded_credentials(broker, method):
    if method == "download":
        broker.inventory(metadata())
    broker.replies.append(
        httpx.Response(307, headers={"Location": "https://attacker.example/collect"})
    )
    with pytest.raises(ComputerClientError):
        if method == "stage":
            broker.client.stage_file(COMPUTER, "example.bin", DATA)
        else:
            broker.client.download_file(COMPUTER, FILE)
    assert len(broker.requests) == (2 if method == "download" else 1)
    assert all(request.url.host == "127.0.0.1" for request in broker.requests)


def test_grant_parser_accepts_transfer_capabilities_without_making_them_operator_authority(broker):
    broker.replies.append(
        httpx.Response(
            200,
            json={
                "computer_id": COMPUTER,
                "lease_id": FILE,
                "epoch": 1,
                "expires_at_ms": 4000000000000,
                "session_id": "session",
                "runtime_generation": 1,
                "capabilities": ["file_import", "file_export"],
                "token": "grant-token-" + "g" * 40,
            },
        )
    )
    grant = broker.client.attach(
        owner={"kind": "identity", "realm": "test", "namespace": "mob", "identity": "agent"},
        slot="main",
        profile="desktop",
        session_id="session",
        runtime_generation=1,
        ttl_seconds=60,
    )
    assert grant.capabilities == ("file_import", "file_export")
    broker.inventory()
    assert broker.client.list_files(COMPUTER) == []
    assert broker.requests[-1].headers["Authorization"] == "Bearer " + OPERATOR


@pytest.mark.parametrize("failed", [False, True], ids=["confirmed", "unknown"])
def test_transfer_metrics_survive_without_untrusted_fields(broker, failed):
    grant = ComputerGrant(
        computer_id=COMPUTER,
        lease_id=FILE,
        epoch=1,
        expires_at_ms=4000000000000,
        session_id="session",
        runtime_generation=1,
        capabilities=("file_import", "file_export"),
        token="grant-token-" + "g" * 40,
    )
    counters = {
        "file_imports": 1,
        "file_imports_bytes": 23,
        "file_exports": 2,
        "file_exports_bytes": 42,
    }
    metrics = {
        **counters,
        "action_id": "transfer-metrics",
        "delta": {**counters, "diagnostic": PRIVATE},
        "totals": {**counters, "token": OPERATOR},
        "counts_are_lower_bounds": failed,
    }
    body = {"content": [], "metrics": metrics}
    if failed:
        body["error"] = {"code": "unknown_effect", "effect": "unknown"}
    broker.replies.append(httpx.Response(409 if failed else 200, json=body))
    if failed:
        with pytest.raises(EffectUnknown) as caught:
            broker.client.call_tool(grant, "computer_upload", {}, "transfer-metrics")
        result = caught.value.metrics
        assert_safe(caught.value)
    else:
        receipt = broker.client.call_tool(grant, "computer_upload", {}, "transfer-metrics")
        result = receipt.structuredContent["metrics"]
    assert {key: result[key] for key in counters} == counters
    assert result["delta"] == counters
    assert result["totals"] == counters
    assert result["counts_are_lower_bounds"] is failed
    assert PRIVATE not in repr(result) and OPERATOR not in repr(result)
