"""Qualify Evince through worker tools inside an isolated Linux desktop.

Run through infra/desktop-session.py in the desktop qualification container.
This never attaches to the host desktop or requires a model/provider key.
"""

import argparse
import asyncio
import base64
import json
import secrets
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import mss
from meerkat_computer.browser import Browser
from meerkat_computer.desktop import Desktop
from meerkat_computer.worker import Worker, create_app


def text_result(body):
    return [json.loads(item["text"]) for item in body["content"] if item["type"] == "text"]


def observation(body):
    return next(item for item in text_result(body) if "observation_id" in item)


def rendered_page(rgb, other_rgb):
    # The fixture deliberately uses flat colors. Counting actual display pixels
    # independently verifies that opening/navigation rendered the expected page.
    with mss.MSS() as display:
        frame = display.grab(display.monitors[1])
        pixels = frame.rgb
        samples = [pixels[offset : offset + 3] for offset in range(0, len(pixels), 3 * 17)]
        primary = samples.count(bytes(rgb))
        secondary = samples.count(bytes(other_rgb))
        # Continuous scrolling may show part of the neighboring page. Requiring
        # dominance verifies real navigation, not merely any second-page pixels.
        return primary > 1000 and primary > secondary * 2


async def qualify(evidence, fixture):
    evidence.mkdir(parents=True, exist_ok=True)
    assert fixture.is_file(), fixture
    package_versions = (
        await asyncio.to_thread(
            subprocess.run,
            ["dpkg-query", "-W", "-f=${Package} ${Version}\n", "evince", "poppler-utils"],
            check=True,
            capture_output=True,
            text=True,
        )
    ).stdout
    (evidence / "packages.txt").write_text(package_versions)
    events = []
    with tempfile.TemporaryDirectory(prefix="meerkat-pdf-") as temporary:
        root = Path(temporary)
        token = secrets.token_urlsafe(32)
        worker = Worker(
            root / "state",
            root / "workspace",
            token,
            browser=Browser(root / "browser-profile", headless=False),
            desktop=Desktop(root / "desktop-state"),
        )
        app = create_app(worker)
        lease = {
            "lease_id": uuid.uuid4().hex,
            "epoch": 1,
            "expires_at_ms": int(time.time() * 1000) + 600_000,
            "capabilities": [
                "browser",
                "browser_semantics",
                "desktop",
                "desktop_semantics",
                "screenshots",
                "exec",
            ],
            "observation": {"surface": "desktop", "mode": "hybrid", "width": 1280, "height": 720},
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://worker",
                headers={"Authorization": "Bearer " + token},
            ) as client,
        ):
            response = await client.post("/v1/lease", json=lease)
            assert response.is_success, response.text

            async def call(
                name,
                arguments,
                *,
                allow_uncertain_launch=False,
                allow_unstable=False,
            ):
                response = await client.post(
                    "/v1/call",
                    json={
                        "lease_id": lease["lease_id"],
                        "epoch": lease["epoch"],
                        "action_id": uuid.uuid4().hex,
                        "name": name,
                        "arguments": arguments,
                    },
                )
                body = response.json()
                events.append(
                    {
                        "tool": name,
                        "status": response.status_code,
                        "metrics": body.get("metrics"),
                        "error": body.get("error"),
                    }
                )
                (evidence / "events.json").write_text(json.dumps(events, indent=2))
                if not response.is_success:
                    if allow_unstable and body["error"]["code"] == "observation_unstable":
                        assert body["error"]["effect"] == "none", body
                        return body
                    assert allow_uncertain_launch, body
                    assert body["error"]["effect"] == "unknown", body
                    assert body["metrics"]["delta"].get("application_launches") == 1, body
                return body

            async def observe(label=None):
                deadline = time.monotonic() + 25
                while True:
                    body = await call(
                        "computer_observe",
                        {"surface": "desktop", "detail": "full"},
                        allow_unstable=True,
                    )
                    if "error" not in body:
                        break
                    assert time.monotonic() < deadline, body
                    await asyncio.sleep(0.3)
                view = observation(body)
                (evidence / "latest-observation.json").write_text(json.dumps(view, indent=2))
                if label:
                    (evidence / f"{label}.json").write_text(json.dumps(view, indent=2))
                    screenshot = next(item for item in body["content"] if item["type"] == "image")
                    (evidence / f"{label}.png").write_bytes(base64.b64decode(screenshot["data"]))
                return view

            async def wait_page(rgb, other_rgb, label):
                deadline = time.monotonic() + 25
                while not await asyncio.to_thread(rendered_page, rgb, other_rgb):
                    await observe()
                    assert time.monotonic() < deadline, f"PDF page did not render: {label}"
                    await asyncio.sleep(0.3)
                return await observe(label)

            listed = await call("computer_apps", {"action": "list"})
            viewer = next(
                entry
                for entry in text_result(listed)[0]["applications"]
                if "evince" in entry["app_id"].lower()
            )
            launched = await call(
                "computer_apps",
                {"action": "launch", "app_id": viewer["app_id"]},
                allow_uncertain_launch=True,
            )
            # Startup can race the observation returned after an admitted launch.
            # Observe the existing process after it settles; never replay launch.
            await asyncio.sleep(1)

            quoted = shlex.quote(str(fixture))
            checked = await call(
                "workspace_exec",
                {
                    "command": f"pdfinfo {quoted} && pdftotext {quoted} - && "
                    f"pdftoppm -f 1 -singlefile -r 72 -png {quoted} "
                    f"{shlex.quote(str(evidence / 'poppler-page-1'))}",
                    "timeout_seconds": 30,
                },
            )
            checked = text_result(checked)[0]
            assert checked["exit_code"] == 0 and not checked["timed_out"], checked
            assert "Pages:           2" in checked["stdout"]
            assert "BLUE PAGE ONE" in checked["stdout"]
            assert "ORANGE PAGE TWO" in checked["stdout"]
            assert (evidence / "poppler-page-1.png").is_file()
            (evidence / "poppler.txt").write_text(checked["stdout"])

            opened = await call(
                "workspace_exec",
                {
                    "command": f"evince {quoted} >/tmp/meerkat-pdf-evince.log 2>&1 &",
                    "timeout_seconds": 10,
                },
            )
            assert text_result(opened)[0]["exit_code"] == 0
            first = await wait_page((21, 89, 162), (193, 91, 20), "page-1")
            evince_ids = {
                entry["app_id"]
                for entry in first["applications"]
                if "evince" in entry["name"].lower()
            }
            accessible = [entry for entry in first["elements"] if entry.get("app_id") in evince_ids]
            assert accessible, "Evince must expose native accessibility controls"
            navigated = await call(
                "computer_act",
                {
                    "observation_id": first["observation_id"],
                    "action": {"type": "key", "key": "Control+End"},
                },
            )
            assert navigated["metrics"]["delta"]["ui_commands"] >= 1
            await wait_page((193, 91, 20), (21, 89, 162), "page-2")
            # Worker lifespan and the private desktop container own teardown.
            # Native window-close semantics are covered by the desktop suite.
            report = {
                "viewer_app_id": viewer["app_id"],
                "uncertain_launch_receipt": "error" in launched,
                "native_launch": True,
                "accessible_elements": len(accessible),
                "pdf_pages": 2,
                "first_page_rendered": True,
                "keyboard_navigation_rendered_second_page": True,
                "pdfinfo_pdftotext_pdftoppm": True,
                "worker_tools": True,
            }
            (evidence / "result.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=Path("/tmp/desktop-evidence/pdf"))
    parser.add_argument(
        "--fixture", type=Path, default=Path(__file__).parent / "fixtures/pdf-viewer.pdf"
    )
    options = parser.parse_args()
    asyncio.run(qualify(options.evidence, options.fixture.resolve()))
