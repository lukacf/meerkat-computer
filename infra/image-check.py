#!/usr/bin/env python3
"""Fail image publication when sandboxed Chromium cannot launch as the worker."""

import argparse

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--desktop", action="store_true")
args = parser.parse_args()
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=not args.desktop,
        chromium_sandbox=True,
        args=["--force-renderer-accessibility"] if args.desktop else [],
    )
    page = browser.new_page()
    page.set_content("<title>Meerkat Computer image check</title>")
    assert page.title() == "Meerkat Computer image check"
    browser.close()
