"""Host-side identity attachment using the existing Meerkat SDK agent loop.

Install the optional SDK for this example with:
uv run --with meerkat-sdk==0.9.0 python examples/run_agent.py --help

Use the matching released rkat-rpc 0.9.0 executable with --rpc-binary.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def plain(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


async def run(args):
    from meerkat import MeerkatClient

    url = urlsplit(args.broker_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Use a loopback broker URL, with SSH forwarding for a remote host")
    owner = json.loads(args.owner_file.read_text())
    # This is trusted application configuration, never generated from model output.
    if owner.get("kind") != "identity":
        raise ValueError("This example requires an explicit durable identity owner")
    operator = os.environ[args.operator_token_env]
    if len(operator) < 32:
        raise ValueError("The operator credential must contain at least 32 characters")
    client = MeerkatClient(args.rpc_binary)
    token = None
    with tempfile.TemporaryDirectory(prefix="meerkat-computer-execution-") as temporary:
        directory = Path(temporary)
        async with httpx.AsyncClient(base_url=args.broker_url, timeout=300, trust_env=False,
                                     follow_redirects=False) as http:
            try:
                await client.connect(isolated=True, state_root=str(directory / "runtime"),
                                     context_root=str(directory / "context"),
                                     user_config_root=str(directory / "config"))
                tag = {"provider": "anthropic" if args.provider == "anthropic" else "open_ai"}
                tag["effort" if args.provider == "anthropic" else "reasoning_effort"] = args.effort
                session = await client.create_deferred_session(
                    "Awaiting computer binding.", provider=args.provider, model=args.model,
                    provider_params={"provider_tag": tag}, max_tokens=args.max_output_tokens,
                    enable_builtins=False, enable_shell=False, enable_memory=False,
                    enable_schedule=False, enable_workgraph=False, enable_mob=False,
                    enable_web_search=False, keep_alive=False,
                    system_prompt="Use the assigned computer tools. Observe before interacting. "
                    "After an uncertain effect, observe the result before deciding what to do. "
                    "Use only workspace tools for code or files on the assigned computer.",
                )
                response = await http.post("/v1/attachments", headers={"Authorization": "Bearer " + operator}, json={
                    "owner": owner, "slot": args.slot, "profile": args.profile,
                    "session_id": session.id, "runtime_generation": args.runtime_generation,
                    "ttl_seconds": args.lease_seconds, "takeover": args.takeover,
                })
                if not response.is_success:
                    raise RuntimeError("Computer attachment failed: " + str(response.status_code))
                binding = response.json()
                token = binding["token"]
                path = directory / "binding.json"
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    json.dump(binding, output)
                await client.mcp_add(session.id, {
                    "name": "computer", "command": sys.executable,
                    "args": ["-m", "meerkat_computer.mcp_adapter", "--broker-url", args.broker_url,
                             "--binding-file", str(path)], "connect_timeout_secs": 15,
                }, persisted=False)
                result = await session.start_turn(args.prompt)
                print(json.dumps({"computer_id": binding["computer_id"], "session_id": session.id,
                                  "model": args.model, "text": result.text,
                                  "usage": plain(result.usage), "run_usage": plain(result.run_usage),
                                  "request_usage": plain(result.request_usage),
                                  "terminal_cause_kind": result.terminal_cause_kind,
                                  "tool_calls": result.tool_calls, "turns": result.turns}, indent=2))
            finally:
                try:
                    await client.close()
                finally:
                    if token:
                        released = await http.post("/v1/lease/release", headers={"Authorization": "Bearer " + token})
                        if not released.is_success:
                            print("Control release was not confirmed; lease expiry remains the cleanup bound", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker-url", default="http://127.0.0.1:18760")
    parser.add_argument("--operator-token-env", default="MEERKAT_COMPUTER_OPERATOR_TOKEN")
    parser.add_argument("--owner-file", type=Path, required=True)
    parser.add_argument("--slot", default="main")
    parser.add_argument("--profile", default="browser")
    parser.add_argument("--runtime-generation", type=int, required=True)
    parser.add_argument("--rpc-binary", default="rkat-rpc")
    parser.add_argument("--provider", choices=["anthropic", "openai"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--lease-seconds", type=int, default=3600)
    parser.add_argument("--takeover", action="store_true")
    parser.add_argument("prompt")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
