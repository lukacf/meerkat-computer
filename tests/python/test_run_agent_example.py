"""Example lifecycle and telemetry checks without a provider or broker."""

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "run_agent.py"
spec = importlib.util.spec_from_file_location("computer_run_agent_example", SCRIPT)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


@dataclasses.dataclass
class Usage:
    input_tokens: int
    output_tokens: int


def setup_run(tmp_path, monkeypatch, *, request_usage, fail_mcp=False):
    calls = []
    binding_paths = []
    operator = "synthetic-operator-" + "a" * 32
    grant = "synthetic-control-" + "b" * 32
    owner = {"kind": "identity", "realm": "test", "namespace": "agents", "identity": "reader"}
    owner_file = tmp_path / "owner.json"
    owner_file.write_text(json.dumps(owner))
    args = SimpleNamespace(
        broker_url="http://127.0.0.1:18760",
        owner_file=owner_file,
        operator_token_env="EXAMPLE_OPERATOR_TOKEN",
        rpc_binary="test-rkat-rpc",
        provider="openai",
        model="test-model",
        effort="medium",
        max_output_tokens=8192,
        slot="main",
        profile="desktop",
        runtime_generation=7,
        lease_seconds=3600,
        takeover=False,
        prompt="Read the assigned document",
    )
    monkeypatch.setenv(args.operator_token_env, operator)

    async def turn(prompt):
        calls.append(("turn", prompt))
        return SimpleNamespace(
            text="Document checked",
            usage=Usage(300, 40),
            run_usage=Usage(200, 30),
            request_usage=request_usage,
            terminal_cause_kind="completed",
            tool_calls=2,
            turns=2,
        )

    class Client:
        def __init__(self, binary):
            assert binary == args.rpc_binary

        async def connect(self, **options):
            assert options["isolated"] is True

        async def create_deferred_session(self, prompt, **options):
            assert options["enable_shell"] is False
            assert options["enable_builtins"] is False
            return SimpleNamespace(id="runtime-session", start_turn=turn)

        async def mcp_add(self, session_id, server, *, persisted):
            assert session_id == "runtime-session" and persisted is False
            path = Path(server["args"][server["args"].index("--binding-file") + 1])
            binding_paths.append(path)
            assert path.stat().st_mode & 0o777 == 0o600
            assert json.loads(path.read_text())["token"] == grant
            if fail_mcp:
                raise RuntimeError("synthetic MCP setup failure")

        async def close(self):
            calls.append(("close",))

    def handler(request):
        calls.append((request.url.path,))
        if request.url.path == "/v1/attachments":
            assert request.headers["Authorization"] == "Bearer " + operator
            body = json.loads(request.content)
            assert body["owner"] == owner
            assert body["session_id"] == "runtime-session" and body["runtime_generation"] == 7
            return httpx.Response(200, json={"token": grant, "computer_id": "assigned-computer"})
        assert request.url.path == "/v1/lease/release"
        assert request.headers["Authorization"] == "Bearer " + grant
        return httpx.Response(200, json={"released": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        example.httpx,
        "AsyncClient",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setitem(sys.modules, "meerkat", SimpleNamespace(MeerkatClient=Client))
    return args, calls, binding_paths, operator, grant


@pytest.mark.parametrize("request_usage", [[Usage(120, 20), Usage(80, 10)], None])
async def test_example_reports_run_request_accounting_and_terminality(
    tmp_path,
    monkeypatch,
    capsys,
    request_usage,
):
    args, calls, paths, operator, grant = setup_run(
        tmp_path,
        monkeypatch,
        request_usage=request_usage,
    )
    await example.run(args)
    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert receipt["request_usage"] == (
        [dataclasses.asdict(row) for row in request_usage] if request_usage is not None else None
    )
    assert receipt["terminal_cause_kind"] == "completed"
    assert receipt["run_usage"] == {"input_tokens": 200, "output_tokens": 30}
    assert receipt["usage"] == {"input_tokens": 300, "output_tokens": 40}
    assert calls[-2:] == [("close",), ("/v1/lease/release",)]
    assert all(not path.exists() for path in paths)
    assert operator not in output and grant not in output


async def test_example_releases_control_if_mcp_setup_fails(tmp_path, monkeypatch):
    args, calls, paths, _, _ = setup_run(
        tmp_path,
        monkeypatch,
        request_usage=None,
        fail_mcp=True,
    )
    with pytest.raises(RuntimeError, match="synthetic MCP setup failure"):
        await example.run(args)
    assert calls[-2:] == [("close",), ("/v1/lease/release",)]
    assert not any(call[0] == "turn" for call in calls)
    assert all(not path.exists() for path in paths)
