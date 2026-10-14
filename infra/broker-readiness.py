#!/usr/bin/env python3
"""Check the installed broker's loopback health endpoint without credentials."""

from __future__ import annotations

import argparse
import ipaddress
import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def health_url(listen: str) -> str:
    try:
        parsed = urllib.parse.urlsplit("http://" + listen)
        if (
            not ipaddress.ip_address(parsed.hostname).is_loopback
            or not parsed.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise ValueError("readiness target must be a loopback IP and port") from error
    return "http://" + listen + "/health"


def wait_for_ready(listen: str, timeout_seconds: float = 30) -> bool:
    url = health_url(listen)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            with opener.open(url, timeout=min(2, remaining)) as response:
                body = response.read(4097)
                if response.status == 200 and len(body) <= 4096:
                    value = json.loads(body)
                    if (
                        isinstance(value, dict)
                        and value.get("service") == "meerkat-computer"
                        and type(value.get("protocol_version")) is int
                        and value["protocol_version"] == 1
                    ):
                        return True
        except (OSError, ValueError):
            pass
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        config = tomllib.loads(args.config.read_text())
        ready = wait_for_ready(config["server"]["listen"])
    except (OSError, ValueError, KeyError, TypeError):
        ready = False
    if not ready:
        print("Broker readiness failed. Inspect the meerkat-computer systemd service.")
        return 1
    print("Broker health check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
