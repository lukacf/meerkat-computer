"""Real operator HTTP + metadata-only stdio MCP binary transfer qualification."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from meerkat_computer.client import ComputerClient


def body(result, *, metadata_only=False):
    assert not result.isError, "MCP transfer smoke operation failed"
    if metadata_only:
        assert all(block.type == "text" for block in result.content), "File bytes entered MCP"
        assert len(result.model_dump_json()) < 16_384, "File content entered a metadata response"
    value = json.loads(next(block.text for block in result.content if block.type == "text"))
    return value


async def run(args):
    binding = json.loads(Path(args.binding_file).read_text())
    computer_id = binding["computer_id"]
    base = f"/v1/computers/{computer_id}/files"
    # Includes every byte value, exceeds Axum's normal 2 MiB body limit, and is
    # generated only on the trusted side. Agent tool arguments contain no bytes.
    payload = bytes(range(256)) * (3 * 1024 * 1024 // 256)
    expected_hash = hashlib.sha256(payload).hexdigest()
    parameters = StdioServerParameters(
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
    async with httpx.AsyncClient(
        base_url=args.broker_url,
        headers={"Authorization": "Bearer " + os.environ["MEERKAT_TRANSFER_SMOKE_OPERATOR"]},
        timeout=60,
        trust_env=False,
    ) as operator:
        denied = await operator.post(
            base,
            params={"filename": "forbidden.bin"},
            headers={"Authorization": "Bearer " + binding["token"]},
            content=b"agent tokens cannot stage host files",
        )
        assert denied.status_code == 401
        with ComputerClient(args.broker_url, operator_token=os.environ[
            "MEERKAT_TRANSFER_SMOKE_OPERATOR"
        ]) as host:
            staged = await asyncio.to_thread(
                host.stage_file, computer_id, "binary-fixture.bin", payload
            )
        imported = asdict(staged)
        assert imported["direction"] == "import"
        assert imported["size_bytes"] == len(payload) and imported["sha256"] == expected_hash
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {item.name for item in (await session.list_tools()).tools}
            assert {
                "computer_files",
                "computer_upload",
                "computer_download",
                "browser_upload",
                "browser_downloads",
            } <= tools
            listed = body(await session.call_tool("computer_files", {}), metadata_only=True)
            assert any(item["file_id"] == imported["file_id"] for item in listed["files"])
            received_result = await session.call_tool(
                "computer_upload",
                {"file_id": imported["file_id"], "path": "guest-transfer.bin"},
            )
            received = body(received_result, metadata_only=True)
            assert received_result.structuredContent["metrics"]["delta"]["file_imports"] == 1
            assert received_result.structuredContent["metrics"]["delta"]["file_imports_bytes"] == len(payload)
            assert received["sha256"] == expected_hash and received["size_bytes"] == len(payload)
            checked = body(
                await session.call_tool(
                    "workspace_exec",
                    {
                        "command": "python3 -c 'import hashlib,json; from pathlib import Path; "
                        'p=Path("guest-transfer.bin"); '
                        'print(json.dumps({"path":str(p.resolve()),'
                        '"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}))\'',
                        "timeout_seconds": 10,
                    },
                )
            )
            assert checked["exit_code"] == 0
            guest = json.loads(checked["stdout"])
            assert guest["sha256"] == expected_hash
            page = body(await session.call_tool("browser_navigate", {"url": args.fixture_url}))
            target = next(item for item in page["elements"] if item["label"] == "Website document")
            uploaded = await session.call_tool(
                "browser_upload",
                {
                    "observation_id": page["observation_id"],
                    "ref": target["ref"],
                    "paths": [guest["path"]],
                },
            )
            view = body(uploaded)
            assert uploaded.structuredContent["metrics"]["delta"]["uploads"] == 1
            for _ in range(30):
                if f"Website received {len(payload)} bytes" in view["accessibility"]:
                    break
                await asyncio.sleep(0.05)
                view = body(await session.call_tool("computer_observe", {"surface": "browser"}))
            else:
                raise AssertionError("Website did not acknowledge the guest file upload")
            downloads = body(await session.call_tool("browser_downloads", {}), metadata_only=True)
            assert downloads["pending_count"] == 0 and downloads["downloads"] == []
            link = next(item for item in view["elements"] if item["label"] == "Download website copy")
            body(await session.call_tool("computer_act", {
                "observation_id": view["observation_id"],
                "action": {"type": "click", "ref": link["ref"]},
            }))
            for _ in range(100):
                downloads = body(await session.call_tool("browser_downloads", {}), metadata_only=True)
                if not downloads["pending_count"] and len(downloads["downloads"]) == 1:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Website download did not complete")
            downloaded = downloads["downloads"][0]
            assert downloaded["size_bytes"] == len(payload)
            exported_result = await session.call_tool(
                "computer_download",
                {"path": downloaded["path"], "filename": "returned-fixture.bin"},
            )
            exported = body(exported_result, metadata_only=True)
            assert exported_result.structuredContent["metrics"]["delta"]["file_exports"] == 1
            assert exported_result.structuredContent["metrics"]["delta"]["file_exports_bytes"] == len(payload)
            assert exported["direction"] == "export"
            assert exported["size_bytes"] == len(payload) and exported["sha256"] == expected_hash
            outbox = body(await session.call_tool("computer_files", {}), metadata_only=True)
            assert any(item["file_id"] == exported["file_id"] for item in outbox["files"])
            raw_route = base + "/" + exported["file_id"]
            denied = await operator.get(
                raw_route, headers={"Authorization": "Bearer " + binding["token"]}
            )
            assert denied.status_code == 401
            with ComputerClient(args.broker_url, operator_token=os.environ[
                "MEERKAT_TRANSFER_SMOKE_OPERATOR"
            ]) as host:
                fetched = await asyncio.to_thread(host.download_file, computer_id, exported["file_id"])
                assert fetched == payload and hashlib.sha256(fetched).hexdigest() == expected_hash
                operator_files = await asyncio.to_thread(host.list_files, computer_id)
                assert any(item.file_id == exported["file_id"] for item in operator_files)
                for item in (imported, exported):
                    await asyncio.to_thread(host.delete_file, computer_id, item["file_id"])
            assert (
                body(await session.call_tool("computer_files", {}), metadata_only=True)["files"]
                == []
            )
    print(
        json.dumps(
            {
                "transfer_stack": "passed",
                "size_bytes": len(payload),
                "sha256": expected_hash,
                "binary_roundtrip": True,
                "website_upload": True,
                "website_download": True,
                "trusted_host_sdk": True,
                "transfer_counters": True,
                "metadata_only_mcp": True,
                "operator_routes_reject_agent_grant": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--binding-file", required=True)
    parser.add_argument("--fixture-url", required=True)
    asyncio.run(run(parser.parse_args()))
