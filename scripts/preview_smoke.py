"""Render the actual broker preview of a saved real worker screenshot."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def run(args):
    token = os.environ.pop("MEERKAT_PREVIEW_SMOKE_OPERATOR")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        await page.goto(f"{args.broker_url}/preview")
        await page.get_by_label("Operator token", exact=True).fill(token)
        await page.get_by_role("button", name="Unlock preview", exact=True).click()
        screenshot = page.get_by_role("img", name="Agent screenshot", exact=True)
        await expect(screenshot).to_be_visible()
        await page.wait_for_function("document.querySelector('.capture img')?.naturalWidth > 100")
        await expect(page.get_by_text("Capture received", exact=False)).to_be_visible()
        source = await screenshot.get_attribute("src")
        assert source.startswith("data:image/png;base64,")
        await page.get_by_role("button", name="Refresh", exact=True).click()
        await expect(screenshot).to_have_attribute("src", source)
        assert token not in page.url and token not in await page.content()
        artifact_dir = os.environ.get("MEERKAT_PREVIEW_SMOKE_ARTIFACT_DIR")
        if artifact_dir:
            output = Path(artifact_dir)
            output.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(output / "desktop.png"), full_page=True)
        await page.get_by_label("Show labels", exact=True).uncheck()
        await expect(page.locator(".labels")).to_be_hidden()
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if artifact_dir:
            await page.screenshot(path=str(output / "mobile.png"), full_page=True)
        await page.get_by_role("button", name="Lock", exact=True).click()
        await expect(page.locator("img")).to_have_count(0)
        await browser.close()
        print(
            json.dumps({"preview": "passed", "real_capture_reused": True, "optional_labels": True})
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    asyncio.run(run(parser.parse_args()))
