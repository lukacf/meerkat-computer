#!/usr/bin/env python3
"""Exercise the installed wheel over MCP stdio against a local fixture broker.

This is packaging acceptance. It does not qualify native Console, guests, browser
execution or HomeCore, and the fixture is deliberately not a substitute adapter.
"""

import asyncio
import hashlib
import importlib.metadata
import json
import secrets
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from meerkat_computer import mcp_adapter, mcp_apps, worker

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jGmQAAAAASUVORK5CYII="
PREVIEW = {
    "computer_id": "fixture-computer",
    "status": "withheld",
    "reason": "private_input",
    "frame": None,
}
TOOL = {
    "name": "computer_observe",
    "description": "Packaging fixture observation",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


async def initialize(session, apps):
    capabilities = (
        {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [mcp_apps.PREVIEW_MIME]}}}
        if apps
        else {}
    )
    initialized = await session.send_request(
        types.ClientRequest(
            types.InitializeRequest(
                params=types.InitializeRequestParams(
                    protocolVersion=types.LATEST_PROTOCOL_VERSION,
                    capabilities=types.ClientCapabilities.model_validate(capabilities),
                    clientInfo=types.Implementation(name="installed-package-check", version="1"),
                )
            )
        ),
        types.InitializeResult,
    )
    assert initialized.serverInfo.version == importlib.metadata.version("meerkat-computer")
    await session.send_notification(types.ClientNotification(types.InitializedNotification()))


async def exercise(parameters, requests, expected_html):
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await initialize(session, True)
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert tools["computer_observe"].meta["ui"] == {
            "resourceUri": mcp_apps.PREVIEW_URI,
            "visibility": ["model"],
        }
        assert tools["computer_preview"].meta["ui"] == {"visibility": ["app"]}
        resource = (await session.read_resource(mcp_apps.PREVIEW_URI)).contents[0]
        assert resource.text == expected_html and resource.mimeType == mcp_apps.PREVIEW_MIME
        assert resource.meta == mcp_apps.RESOURCE_META
        observation = await session.call_tool("computer_observe", {})
        assert not observation.isError
        assert observation.content[1].type == "image" and observation.content[1].data == PNG
        assert observation.structuredContent["metrics"]["totals"]["clicks"] == 0
        assert len(observation.structuredContent["action_id"]) == 32
        for arguments in ({}, {"after": "fixture-cursor"}):
            preview = await session.call_tool("computer_preview", arguments)
            assert not preview.isError and preview.meta == {"meerkat-computer/preview": PREVIEW}
            assert preview.structuredContent is None
            assert "fixture-computer" not in preview.content[0].text
        before = len(requests)
        assert (await session.call_tool("computer_preview", {"computer_id": "other"})).isError
        assert len(requests) == before
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await initialize(session, False)
        assert [tool.name for tool in (await session.list_tools()).tools] == [TOOL["name"]]
        assert (await session.list_resources()).resources == []
        before = [entry for entry in requests if entry[1] != "/v1/tools"]
        assert (await session.call_tool("computer_preview", {})).isError
        assert [entry for entry in requests if entry[1] != "/v1/tools"] == before


def main():
    expected_version, wheel_sha256 = sys.argv[1:]
    assert importlib.metadata.version("meerkat-computer") == expected_version
    assert all(
        Path(module.__file__).is_relative_to(sys.prefix)
        for module in (mcp_adapter, mcp_apps, worker)
    )
    html = mcp_apps.load_preview_html()
    assert html and "Bundled third-party licenses" in html
    binaries = Path(sys.executable).parent
    for command in ("meerkat-computer-worker", "meerkat-computer-mcp"):
        subprocess.run([binaries / command, "--help"], capture_output=True, check=True, timeout=20)
    requests = []
    token = secrets.token_hex(32)

    class Broker(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, status=200):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def record(self):
            if self.headers.get("Authorization") != "Bearer " + token:
                self.respond({"error": "fixture_authentication_failed"}, 401)
                return False
            requests.append((self.command, self.path))
            return True

        def do_GET(self):
            if not self.record():
                return
            path = urlsplit(self.path).path
            if path == "/v1/tools":
                self.respond({"tools": [TOOL]})
            elif path == "/v1/preview":
                self.respond(PREVIEW)
            else:
                self.respond({"error": "unexpected_fixture_endpoint"}, 404)

        def do_POST(self):
            if not self.record():
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/tools/call" or body.get("name") != "computer_observe":
                self.respond({"error": "unexpected_fixture_action"}, 400)
                return
            self.respond(
                {
                    "content": [
                        {"type": "text", "text": "Fixture observation"},
                        {"type": "image", "data": PNG, "mimeType": "image/png"},
                    ],
                    "metrics": {"delta": {"clicks": 0}, "totals": {"clicks": 0}},
                }
            )

    with tempfile.TemporaryDirectory(prefix="installed-computer-") as directory:
        binding = Path(directory) / "binding.json"
        binding.touch(mode=0o600)
        binding.write_text(json.dumps({"token": token}))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        parameters = StdioServerParameters(
            command=str(binaries / "meerkat-computer-mcp"),
            args=[
                "--broker-url",
                f"http://127.0.0.1:{server.server_port}",
                "--binding-file",
                str(binding),
                "--mcp-apps",
            ],
        )
        try:
            asyncio.run(asyncio.wait_for(exercise(parameters, requests, html), timeout=45))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    assert sum(method == "POST" for method, _ in requests) == 1
    assert sum(path.startswith("/v1/preview") for _, path in requests) == 2
    print(
        json.dumps(
            {
                "version": expected_version,
                "wheel_sha256": wheel_sha256,
                "resource_sha256": hashlib.sha256(html.encode()).hexdigest(),
                "installed_cli_help": True,
                "mcp_stdio": True,
                "apps_negotiation": True,
                "private_preview": True,
                "normal_image_content": True,
                "legacy_host": True,
                "rejected_cross_computer_arguments": True,
                "authenticated_fixture_requests": len(requests),
            }
        )
    )


if __name__ == "__main__":
    main()
