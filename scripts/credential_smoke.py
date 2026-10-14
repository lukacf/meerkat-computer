"""Real stdio MCP login proof. All passwords here are synthetic fixture values."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PASSWORDS = [
    "fixture-password-first-A7!",
    "fixture-password-rotated-B8!",
    "fixture-password-once-C9!",
]


def safe(value):
    text = json.dumps(value)
    assert all(secret not in text for secret in PASSWORDS), (
        "Fixture secret entered a public response"
    )
    return value


def observation(result):
    safe(result.model_dump(mode="json"))
    assert not result.isError, "MCP operation failed"
    return json.loads(next(block.text for block in result.content if block.type == "text"))


@asynccontextmanager
async def connect(args, binding):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "meerkat_computer.mcp_adapter",
            "--broker-url",
            args.broker_url,
            "--binding-file",
            binding,
        ],
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def login_target(session, url):
    before = observation(await session.call_tool("browser_navigate", {"url": url}))
    refs = {node["label"]: node["ref"] for node in before["elements"]}
    parts = urlsplit(url)
    return {
        "origin": f"{parts.scheme}://{parts.netloc}",
        "observation_id": before["observation_id"],
        "secret_ref": refs["Password"],
        "username_ref": refs["Username"],
    }, refs["Sign in"]


async def submit(session, continuation, submit_ref, expected):
    assert continuation["filled"] and not continuation["submitted"]
    assert continuation["privacy"]["active"] and submit_ref in continuation["action_refs"]
    guarded = await session.call_tool("computer_observe", {})
    assert not any(block.type == "image" for block in guarded.content)
    continuation = observation(guarded)
    after = observation(
        await session.call_tool(
            "computer_act",
            {
                "observation_id": continuation["observation_id"],
                "action": {"type": "click", "ref": submit_ref},
            },
        )
    )
    assert f"Signed in: {expected}" in after["accessibility"]


async def run(args):
    token = os.environ["MEERKAT_CREDENTIAL_SMOKE_OPERATOR"]
    owner = {
        "kind": "identity",
        "realm": "secure-test",
        "namespace": "mob:credential-smoke",
        "identity": "assigned-agent",
    }
    first_binding = json.loads(Path(args.binding_file).read_text())
    async with httpx.AsyncClient(
        base_url=args.broker_url, headers={"Authorization": "Bearer " + token}, trust_env=False
    ) as operator:

        async def api(method, path, body=None):
            result = await operator.request(method, path, json=body)
            safe(result.json())
            assert result.is_success, "Operator credential API refused fixture operation"
            return result.json()

        draft = {
            "realm": "secure-test",
            "label": "Assigned fixture login",
            "origin": args.broker_url,
            "username": "fixture-account",
            "secret": PASSWORDS[0],
        }
        credential = await api("POST", "/v1/credentials", draft)
        await api(
            "POST",
            "/v1/credentials/assign",
            {"credential_ids": [credential["id"]], "owners": [owner]},
        )
        async with (
            connect(args, args.binding_file) as session,
            connect(args, args.other_binding_file) as other,
        ):
            catalog = await session.list_tools()
            assert {
                "credentials_list",
                "credentials_use",
                "credentials_request",
                "credentials_status",
            } <= {item.name for item in catalog.tools}
            assigned = observation(await session.call_tool("credentials_list", {}))["credentials"]
            assert len(assigned) == 1 and assigned[0]["id"] == credential["id"]
            assert observation(await other.call_tool("credentials_list", {}))["credentials"] == []
            other_target, _ = await login_target(other, args.fixture_url)
            denied = await other.call_tool(
                "credentials_use", {"credential_id": credential["id"], "target": other_target}
            )
            safe(denied.model_dump(mode="json"))
            assert denied.isError
            target, button = await login_target(session, args.fixture_url)
            arguments = {"credential_id": credential["id"], "target": target}
            first = await session.call_tool("credentials_use", arguments)
            continuation = observation(first)
            assert not any(block.type == "image" for block in first.content)
            rotated = await api(
                "PUT", "/v1/credentials/" + credential["id"], {**draft, "secret": PASSWORDS[1]}
            )
            assert rotated["version"] == credential["version"] + 1
            duplicate = await operator.post(
                "/v1/tools/call",
                headers={"Authorization": "Bearer " + first_binding["token"]},
                json={
                    "action_id": first.structuredContent["action_id"],
                    "name": "credentials_use",
                    "arguments": arguments,
                },
            )
            safe(duplicate.json())
            assert duplicate.is_success
            assert json.loads(duplicate.json()["content"][0]["text"]) == continuation
            await submit(session, continuation, button, "first")
            target, button = await login_target(session, args.fixture_url)
            changed = observation(
                await session.call_tool(
                    "credentials_use", {"credential_id": credential["id"], "target": target}
                )
            )
            await submit(session, changed, button, "rotated")
            target, button = await login_target(session, args.fixture_url)
            pending = observation(
                await session.call_tool("credentials_request", {"target": target})
            )
            assert pending["phase"] == "pending"
            inaccessible = await other.call_tool(
                "credentials_status", {"request_id": pending["id"]}
            )
            assert inaccessible.isError
            safe(inaccessible.model_dump(mode="json"))
            fulfilled = await api(
                "POST",
                f"/v1/credential-requests/{pending['id']}/fulfill",
                {
                    "values": {"username": "fixture-account", "secret": PASSWORDS[2]},
                    "save": {"label": "Saved by one-time fixture"},
                },
            )
            assert json.loads(fulfilled["content"][0]["text"])["filled"]
            status = observation(
                await session.call_tool("credentials_status", {"request_id": pending["id"]})
            )
            assert status["phase"] == "delivered"
            await submit(
                session, json.loads(status["receipt"]["content"][0]["text"]), button, "once"
            )
            repeated = await operator.post(
                f"/v1/credential-requests/{pending['id']}/fulfill",
                json={
                    "values": {"username": "fixture-account", "secret": PASSWORDS[0]},
                    "save": None,
                },
            )
            safe(repeated.json())
            assert repeated.status_code == 409
            assert (
                len(observation(await session.call_tool("credentials_list", {}))["credentials"])
                == 2
            )
        # New MCP process, same identity-owned browser profile and server session cookie.
        async with connect(args, args.binding_file) as resumed:
            persisted = observation(
                await resumed.call_tool(
                    "browser_navigate", {"url": args.broker_url + "/fixture/session"}
                )
            )
            assert "Signed in: once" in persisted["accessibility"]
    print(
        json.dumps(
            {
                "credential_mcp": "passed",
                "private_uds": "passed",
                "logins": 3,
                "identity_denial": True,
                "rotation": True,
                "duplicate_not_replayed": True,
                "one_time_optional_save": True,
                "mcp_reconnect_session": True,
                "model_responses_exclude_fixture_passwords": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--binding-file", required=True)
    parser.add_argument("--other-binding-file", required=True)
    parser.add_argument("--fixture-url", required=True)
    asyncio.run(run(parser.parse_args()))
