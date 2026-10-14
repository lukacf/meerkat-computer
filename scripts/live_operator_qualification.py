"""Explicit deployed operator, privacy, restart and automatic-idle qualification.

Requires an exclusive broker with a short-idle profile and pool limit of two.
The restart command is an explicit JSON argv file controlled by the operator.
Default execution prints the plan without reading credentials or contacting a host.
"""

import asyncio
import json
import os
import signal
import time
import uuid
from pathlib import Path

import httpx
from live_qualification import (
    BrokerAPI,
    Evidence,
    LiveQualification,
    QualificationFailure,
    authorization_denied,
    read_token,
    require,
    text_receipt,
)
from live_qualification import (
    parser as base_parser,
)
from playwright.async_api import async_playwright, expect


def parser():
    result = base_parser()
    result.description = __doc__
    result.add_argument("--restart-command-file", type=Path, required=True)
    result.add_argument("--idle-wait-seconds", type=int, default=100)
    return result


def restart_argv(path):
    value = json.loads(path.read_text())
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(part, str) or not part or "\0" in part for part in value)
    ):
        raise ValueError("Restart command must be a nonempty JSON argv array")
    return value


async def restart_once(argv):
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=90)
    except (TimeoutError, asyncio.CancelledError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise QualificationFailure("Broker restart outcome is unknown", effect="unknown") from None
    if process.returncode != 0:
        raise QualificationFailure(
            "Broker restart command did not confirm success", effect="unknown"
        )


def check_preview_reuse(receipt, first, unchanged):
    view = text_receipt(receipt)
    images = [block["data"] for block in receipt["content"] if block.get("type") == "image"]
    require(len(images) == 1, "Agent receipt must contain one screenshot")
    require(first.get("status") == "available", "Regular capture did not become available")
    frame = first["frame"]
    require(
        frame.get("observation_id") == view["observation_id"] and frame.get("data") == images[0],
        "Preview differs from the agent's exact capture",
    )
    expected = {**frame, "data": None}
    if "age_ms" in frame:
        expected["age_ms"] = (unchanged.get("frame") or {}).get("age_ms")
    require(
        unchanged.get("status") == "available" and unchanged.get("frame") == expected,
        "Unchanged preview did not preserve its capture and omit repeated pixels",
    )


def check_withheld(receipt, preview):
    require(
        not any(block.get("type") == "image" for block in receipt.get("content", []))
        and text_receipt(receipt).get("privacy", {}).get("active") is True,
        "Private input did not withhold the observation",
    )
    require(
        preview.get("status") == "withheld" and preview.get("frame") is None,
        "Private input did not withhold the operator preview",
    )


class OperatorQualification(LiveQualification):
    async def cleanup(self):
        try:
            if self.guests:
                library = (await self.api.request("GET", "/v1/credentials"))["credentials"]
                for item in library:
                    if (
                        item.get("realm") == "qualification"
                        and item.get("label") == "Operator qualification " + self.evidence.run_id
                        and self.guests[0]["owner"] in item.get("assignments", [])
                        and item["id"] not in self.credentials
                    ):
                        self.credentials.append(item["id"])
        except QualificationFailure:
            self.evidence.check("cleanup_operator_save_discovery", "unknown")
        finally:
            await super().cleanup()

    async def preview(self, guest, after=None):
        path = f"/v1/computers/{guest['binding']['computer_id']}/preview"
        return await self.api.request("GET", path + ("?after=" + after if after else ""))

    async def capture_reused(self, guest, receipt):
        first = await self.preview(guest)
        unchanged = await self.preview(guest, text_receipt(receipt)["observation_id"])
        check_preview_reuse(receipt, first, unchanged)
        self.evidence.check(
            "preview_exact_capture_reused", computer_id=guest["binding"]["computer_id"]
        )

    async def guarded(self, guest):
        for surface in ["browser", "desktop"]:
            receipt = await self.tool(guest, "computer_observe", {"surface": surface})
            check_withheld(receipt, await self.preview(guest))
        self.evidence.check("private_browser_desktop_preview_withheld")

    async def submit(self, guest, button, expected_logins):
        view = text_receipt(await self.tool(guest, "computer_observe", {"surface": "browser"}))
        require(button in view["action_refs"], "Submit continuation was lost")
        receipt = await self.tool(
            guest,
            "computer_act",
            {
                "observation_id": view["observation_id"],
                "action": {"type": "click", "ref": button},
            },
        )
        require(
            "Login verified " + guest["marker"] in text_receipt(receipt)["accessibility"],
            "Website rejected the operator login",
        )
        await self.capture_reused(guest, receipt)
        audit = json.loads(await self.read(guest, guest["fixture"].replace(".py", ".audit.json")))
        require(
            audit.get("login_success") == expected_logins and not audit.get("login_denied"),
            "Independent website login count differs",
        )

    async def unlock(self, page, route, button):
        await page.goto(self.args.broker_url + route)
        await page.get_by_label("Operator token", exact=True).fill(self.api.operator)
        await page.get_by_role("button", name=button, exact=True).click()

    async def manual_login(self, guest, page):
        target, button = await self.login_target(guest)
        request = text_receipt(await self.tool(guest, "credentials_request", {"target": target}))
        await self.unlock(page, "/credentials#" + request["id"], "Unlock credentials")
        card = page.locator("#request-" + request["id"])
        await expect(card).to_be_visible()
        await card.get_by_label("Username", exact=True).fill("qualification-user")
        await card.get_by_label("Password or code", exact=True).fill(self.secret)
        await card.get_by_label("Save for this identity", exact=True).check()
        label = "Operator qualification " + self.evidence.run_id
        await card.get_by_label("Credential label", exact=True).fill(label)
        await card.get_by_role("button", name="Send securely", exact=True).click()
        await expect(card).to_contain_text("Delivered")
        require(self.secret not in await page.content(), "Operator page retained submitted input")
        credentials = text_receipt(await self.tool(guest, "credentials_list", {}))["credentials"]
        saved = [item for item in credentials if item["label"] == label]
        require(len(saved) == 1, "Optional save did not create one reusable assignment")
        self.credentials.append(saved[0]["id"])
        await self.guarded(guest)
        await self.submit(guest, button, 1)
        self.evidence.check("deployed_operator_input_optional_save", website_logins=1)
        return saved[0]

    async def reuse_and_revoke(self, guest, credential, page):
        target, button = await self.login_target(guest)
        await self.tool(
            guest, "credentials_use", {"credential_id": credential["id"], "target": target}
        )
        await self.submit(guest, button, 2)
        await page.get_by_role("button", name="Refresh", exact=True).click()
        await page.get_by_label("Select " + credential["label"], exact=True).check()
        owner = guest["owner"]
        await page.get_by_label("Known agent", exact=True).select_option(
            label=f"{owner['realm']} / {owner['namespace']} / {owner['identity']}"
        )
        await page.get_by_role("button", name="Add selected agent", exact=True).click()
        await page.get_by_role("button", name="Revoke selected", exact=True).click()
        await expect(page.get_by_role("status")).to_contain_text("revoked")
        target, _ = await self.login_target(guest)
        denied = await self.tool(
            guest,
            "credentials_use",
            {
                "credential_id": credential["id"],
                "target": target,
            },
            allow_error=True,
        )
        require(authorization_denied(denied), "Revocation did not deny later delivery")
        await page.get_by_role("button", name="Assign selected", exact=True).click()
        await expect(page.get_by_role("status")).to_contain_text("Assignments saved")
        await page.screenshot(
            path=str(self.evidence.directory / "operator-assignments.png"), full_page=True
        )
        self.evidence.check("deployed_assignment_picker_reuse_revoke", website_logins=2)

    async def preview_page(self, guest, page):
        receipt = await self.tool(guest, "computer_observe", {"surface": "browser"})
        await self.capture_reused(guest, receipt)
        calls = []
        page.on("request", lambda request: calls.append((request.method, request.url)))
        await self.unlock(page, "/preview", "Unlock preview")
        card = page.locator(".preview-card").filter(has_text=self.namespace)
        await expect(card.get_by_role("img", name="Agent screenshot")).to_be_visible()
        await page.get_by_label("Show labels", exact=True).uncheck()
        await expect(card.locator(".labels")).to_be_hidden()
        await card.screenshot(
            path=str(self.evidence.directory / "operator-preview-active-card-labels-off.png")
        )
        await page.get_by_role("button", name="Refresh", exact=True).click()
        await expect(page.get_by_role("status")).to_contain_text("Checking saved captures")
        require(all(method == "GET" for method, _ in calls), "Preview page issued a mutation")
        require(
            await page.evaluate("localStorage.length + sessionStorage.length") == 0,
            "Operator preview persisted browser storage",
        )
        await page.screenshot(
            path=str(self.evidence.directory / "operator-preview-labels-off.png"), full_page=True
        )
        await page.get_by_role("button", name="Lock", exact=True).click()
        self.evidence.check("deployed_preview_optional_labels", requests_read_only=True)

    async def restart_privacy(self, guest, credential):
        target, _ = await self.login_target(guest)
        pending = text_receipt(await self.tool(guest, "credentials_request", {"target": target}))
        await self.tool(
            guest, "credentials_use", {"credential_id": credential["id"], "target": target}
        )
        await self.guarded(guest)
        before = guest["binding"]
        await restart_once(restart_argv(self.args.restart_command_file))
        deadline = time.monotonic() + 30
        while True:
            try:
                health = await self.api.client.get(self.args.broker_url + "/health")
                if health.status_code == 200 and health.json().get("service") == "meerkat-computer":
                    break
            except (httpx.HTTPError, ValueError):
                pass
            require(time.monotonic() < deadline, "Broker did not recover after restart")
            await asyncio.sleep(0.2)
        denied = await self.api.tool(before, "computer_observe", {}, allow_error=True)
        require(authorization_denied(denied), "Broker restart retained the old control grant")
        requests = (await self.api.request("GET", "/v1/credential-requests"))["requests"]
        require(
            any(r["id"] == pending["id"] and r["phase"] == "cancelled" for r in requests),
            "Restart did not cancel pending private input",
        )
        await self.attach(guest, 2)
        require(
            guest["binding"]["computer_id"] == before["computer_id"], "Restart changed identity"
        )
        assigned = text_receipt(await self.tool(guest, "credentials_list", {}))["credentials"]
        require(
            any(item["id"] == credential["id"] for item in assigned), "Vault assignment was lost"
        )
        await self.guarded(guest)
        # The short-idle guest may stop while its broker restarts. Its temporary
        # website process is not part of the durable identity or privacy contract.
        receipt = await self.tool(guest, "browser_navigate", {"url": "about:blank"})
        await self.capture_reused(guest, receipt)
        self.evidence.check(
            "broker_restart_identity_vault_pending_input_privacy",
            safe_navigation_url="about:blank",
            fixture_process_continuity_asserted=False,
        )

    async def automatic_idle_stop(self, guest):
        binding = guest["binding"]
        await self.api.request(
            "POST", "/v1/lease/renew", {"ttl_seconds": 1}, token=binding["token"]
        )
        await asyncio.sleep(1.2)
        denied = await self.tool(guest, "computer_observe", {}, allow_error=True)
        require(authorization_denied(denied), "Expired lease allowed a new operation")
        deadline = time.monotonic() + self.args.idle_wait_seconds
        while True:
            rows = (await self.api.request("GET", "/v1/computers"))["computers"]
            row = next(row for row in rows if row["computer_id"] == binding["computer_id"])
            if row["phase"] == "stopped":
                break
            require(
                time.monotonic() < deadline, "Automatic idle cleanup did not confirm stopped state"
            )
            await asyncio.sleep(1)
        self.evidence.check(
            "automatic_idle_stop", explicit_stop_or_reap=False, expired_grant_denied=True
        )

    async def admission(self, guest):
        await self.attach(guest, 3)
        second = await self.prepare(1)
        third = {"owner": self.owner(2)}
        self.guests.append(third)
        denied = await self.api.request(
            "POST",
            "/v1/attachments",
            {
                "owner": third["owner"],
                "slot": "main",
                "profile": self.args.profile,
                "session_id": self.evidence.run_id + ":capacity",
                "runtime_generation": 1,
                "ttl_seconds": 120,
            },
            attachment=True,
            allow_error=True,
        )
        error = denied.get("error", {})
        diagnostic = error.get("backend_failure")
        require(
            error.get("code") == "backend_failure"
            and error.get("effect") == "unknown"
            and isinstance(diagnostic, dict)
            and diagnostic.get("category") == "admission_denied",
            "Third computer was not denied by pool admission",
        )
        await self.api.request("POST", "/v1/lease/release", token=second["binding"]["token"])
        await self.api.request("POST", f"/v1/computers/{second['binding']['computer_id']}/stop")
        fourth = {"owner": self.owner(3)}
        self.guests.append(fourth)
        await self.attach(fourth, 1)
        self.evidence.check(
            "live_pool_admission_limit",
            configured_limit=2,
            third_denied=True,
            stopped_guest_freed_slot=True,
        )

    async def run(self):
        rows = (await self.api.request("GET", "/v1/computers"))["computers"]
        require(
            all(row["phase"] == "stopped" for row in rows),
            "Operator qualification requires no other active computers",
        )
        guest = await self.prepare(0)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.set_default_timeout(20000)
                credential = await self.manual_login(guest, page)
                await self.reuse_and_revoke(guest, credential, page)
                await self.preview_page(guest, await browser.new_page())
            finally:
                await browser.close()
        await self.restart_privacy(guest, credential)
        await self.automatic_idle_stop(guest)
        await self.admission(guest)


async def apply(args):
    require(
        not args.no_native and not args.no_credentials, "Operator qualification requires full scope"
    )
    restart_argv(args.restart_command_file)
    token = read_token(args.operator_token_env, args.operator_token_file)
    evidence = Evidence(args.report_dir, uuid.uuid4().hex)
    async with httpx.AsyncClient(
        timeout=args.timeout_seconds, trust_env=False, follow_redirects=False
    ) as client:
        runner = OperatorQualification(
            args, BrokerAPI(client, args.broker_url, token, evidence), evidence
        )
        try:
            await runner.run()
            evidence.data["status"] = "passed"
        except Exception as error:  # noqa: BLE001 - keep arbitrary browser/command diagnostics private.
            evidence.data["status"] = "failed"
            evidence.event(
                "operator_qualification",
                "failed",
                error={
                    "code": type(error).__name__,
                    "effect": getattr(error, "effect", "none"),
                },
                reason=str(error)
                if isinstance(error, QualificationFailure)
                else "Qualification failed",
            )
        finally:
            await runner.cleanup()
            if evidence.data["status"] == "passed" and any(
                item["status"] != "passed" for item in evidence.data["checks"]
            ):
                evidence.data["status"] = "partial"
            evidence.write()
    print(
        json.dumps(
            {"status": evidence.data["status"], "report": str(args.report_dir / "report.json")}
        )
    )
    return 0 if evidence.data["status"] == "passed" else 1


def main():
    args = parser().parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "apply": False,
                    "requires_exclusive_broker": True,
                    "required_pool_limit": 2,
                    "required_profile_idle_seconds": 1,
                    "scope": [
                        "operator_secure_input",
                        "reusable_assignments",
                        "preview",
                        "broker_restart",
                        "automatic_idle_stop",
                        "pool_admission",
                    ],
                }
            )
        )
        return 0
    return asyncio.run(apply(args))


if __name__ == "__main__":
    raise SystemExit(main())
