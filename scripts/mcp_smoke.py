"""Exercise the real stdio MCP adapter against a running broker and browser."""

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def observation(result):
    assert not result.isError, result
    return json.loads(next(block.text for block in result.content if block.type == "text"))


async def run(args):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "meerkat_computer.mcp_adapter",
            "--broker-url",
            args.broker_url,
            "--binding-file",
            args.binding_file,
        ],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {"computer_act", "workspace_write"} <= {tool.name for tool in listed.tools}
        start = await session.call_tool("browser_navigate", {"url": args.fixture_url})
        assert any(block.type == "image" for block in start.content)
        before = observation(start)
        button = next(node for node in before["elements"] if node["label"] == "Increment")
        result = await session.call_tool(
            "computer_act",
            {
                "observation_id": before["observation_id"],
                "action": {"type": "click", "ref": button["ref"]},
            },
        )
        after = observation(result)
        assert "Count: 1" in after["accessibility"]
        assert result.structuredContent["metrics"]["delta"]["clicks"] == 1
        stale = await session.call_tool(
            "computer_act",
            {
                "observation_id": before["observation_id"],
                "action": {"type": "click", "ref": button["ref"]},
            },
        )
        assert stale.isError
        assert (
            "Count: 1"
            in observation(
                await session.call_tool(
                    "computer_observe",
                    {
                        "surface": "browser",
                    },
                )
            )["accessibility"]
        )
        desktop = os.environ.get("MEERKAT_COMPUTER_DESKTOP_TEST") == "1"
        if desktop:
            assert "computer_apps" in {tool.name for tool in listed.tools}
            catalog = observation(await session.call_tool("computer_apps", {"action": "list"}))
            assert any(app["app_id"] == "blender.desktop" for app in catalog["applications"])
            native = await session.call_tool("computer_observe", {"surface": "desktop"})
            view = observation(native)
            assert view["surface"] == "desktop" and view["coordinate_space"] == "screen"
            assert any(block.type == "image" for block in native.content)
            assert view["accessibility"]["status"] in {"available", "partial"}
            assert view["applications"], view
            switched = await session.call_tool(
                "computer_act",
                {
                    "observation_id": after["observation_id"],
                    "action": {"type": "click", "ref": button["ref"]},
                },
            )
            assert switched.isError
        assert not (
            await session.call_tool(
                "workspace_write",
                {
                    "path": "identity.txt",
                    "text": "persistent identity workspace\n",
                },
            )
        ).isError
        readback = observation(await session.call_tool("workspace_read", {"path": "identity.txt"}))
        assert readback["text"] == "persistent identity workspace\n"
        result = await session.call_tool(
            "workspace_exec",
            {
                "command": "python3 -c 'print(6 * 7)'",
                "timeout_seconds": 5,
            },
        )
        command = observation(result)
        assert command["exit_code"] == 0 and "42" in command["stdout"]
        denied = await session.call_tool("workspace_read", {"path": "../outside"})
        assert denied.isError
        print(
            json.dumps(
                {
                    "mcp": "passed",
                    "browser_clicks": 1,
                    "stale_action_rejected": True,
                    "desktop": "passed" if desktop else "not_requested",
                    "workspace": "passed",
                    "exec": "passed",
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--binding-file", required=True)
    parser.add_argument("--fixture-url", required=True)
    asyncio.run(run(parser.parse_args()))
