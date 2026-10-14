"""Explicit production-broker qualification. Dry run never reads tokens or contacts a host.

Run on the broker host or over an existing SSH/IAP loopback tunnel. This script
does not provision cloud resources, change network policy, or delete computers.
All credentials and website accounts used by the exercise are synthetic.
"""

import argparse
import asyncio
import base64
import copy
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import stat
import statistics
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class QualificationFailure(Exception):
    def __init__(self, message, *, effect="none"):
        super().__init__(message)
        self.effect = effect


def require(condition, message):
    if not condition:
        raise QualificationFailure(message)


def broker_url(value):
    try:
        url = urlsplit(value)
        address = ipaddress.ip_address(url.hostname or "")
        if (
            not address.is_loopback
            or url.scheme != "http"
            or not url.port
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError
    except ValueError:
        raise ValueError("Broker URL must be literal loopback HTTP with port and no path") from None
    return value.rstrip("/")


def network_target(value):
    try:
        parsed = urlsplit("tcp://" + value)
        address, port = ipaddress.ip_address(parsed.hostname or ""), parsed.port
        if not port or parsed.username or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        return str(address), port
    except ValueError:
        raise ValueError("Network target must be a literal IP address and TCP port") from None


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be at least one")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--broker-url", type=broker_url, required=True)
    result.add_argument("--profile", required=True)
    result.add_argument("--guest-kind", choices=["container", "virtual-machine"])
    result.add_argument("--report-dir", type=Path, required=True)
    result.add_argument("--concurrency", type=positive, default=2)
    result.add_argument("--repeats", type=positive, default=1, help="Cold restart cycles per guest")
    source = result.add_mutually_exclusive_group()
    source.add_argument("--operator-token-env", default="MEERKAT_COMPUTER_OPERATOR_TOKEN")
    source.add_argument("--operator-token-file", type=Path)
    result.add_argument("--timeout-seconds", type=positive, default=300)
    result.add_argument("--fixture-port", type=positive, default=43127)
    result.add_argument("--no-native", action="store_true", help="Record native scope as skipped")
    result.add_argument(
        "--no-credentials", action="store_true", help="Record credentials as skipped"
    )
    result.add_argument("--host-target", type=network_target, action="append", default=[])
    result.add_argument("--peer-target", type=network_target, action="append", default=[])
    result.add_argument("--public-control", type=network_target, default=("1.1.1.1", 443))
    result.add_argument(
        "--host-control-file",
        type=Path,
        help="External TCP control evidence: JSON list of host, port, connected",
    )
    result.add_argument(
        "--host-metrics-file",
        type=Path,
        help="Externally collected host resource evidence, copied as unverified input",
    )
    result.add_argument(
        "--isolation",
        action="store_true",
        help="Guest-root TCP probes; only connects, never requests metadata",
    )
    result.add_argument("--apply", action="store_true")
    return result


def plan(args):
    if args.fixture_port > 65535:
        raise ValueError("Fixture port must be at most 65535")
    if args.isolation and not args.host_target:
        raise ValueError("Isolation requires an explicit --host-target with a real listener")
    if args.isolation and args.concurrency < 2 and not args.peer_target:
        raise ValueError("Isolation requires two guests or an explicit peer target")
    return {
        "apply": args.apply,
        "broker_url": args.broker_url,
        "profile": args.profile,
        "operator_declared_guest_kind": args.guest_kind,
        "concurrency": args.concurrency,
        "repeats": args.repeats,
        "creates_new_scoped_identities": True,
        "scope": [
            "parallel_start",
            "browser",
            "identity_handoff",
            "old_grant_denial",
            "cold_restart",
            "workspace_persistence",
        ]
        + ([] if args.no_native else ["native_accessibility", "desktop_screenshots"])
        + (
            []
            if args.no_credentials
            else ["saved_credentials", "private_input", "cookie_persistence"]
        )
        + (["guest_root_network_isolation"] if args.isolation else []),
        "cleanup": "Release and stop only this run's computers; retain disks and evidence",
        "report_dir": str(args.report_dir),
    }


def read_token(env_name, path):
    if path:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags)
        except OSError:
            raise ValueError("Token file must be an owned regular file, not a symlink") from None
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("Token file must be an owned regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Token file must have permissions 0600")
            token = stream.read(4097).strip()
    else:
        token = os.environ.get(env_name or "MEERKAT_COMPUTER_OPERATOR_TOKEN", "").strip()
    if not 32 <= len(token) <= 4096 or any(char.isspace() for char in token):
        raise ValueError("Operator token is missing or invalid")
    return token


def safe_backend_failure(value):
    """Project only the broker's closed diagnostic enums, never its error text."""
    if not isinstance(value, dict):
        return None
    category = value.get("category")
    if not isinstance(category, str) or category not in {
        "unsupported",
        "invalid_configuration",
        "command_failed",
        "command_timeout",
        "ownership_mismatch",
        "admission_denied",
        "worker_not_ready",
        "invalid_backend_state",
        "io",
    }:
        return None
    result = {"category": category}
    if "operation" in value:
        operation = value["operation"]
        if not isinstance(operation, str) or operation not in {
            "inventory",
            "fork_capability_inspection",
            "fork_prepare",
            "checkpoint_create",
            "checkpoint_inspection",
            "checkpoint_release",
            "fork_copy",
            "fork_network",
            "fork_resume",
            "fork_rebind",
            "image_validation",
            "network_boundary_inspection",
            "network_readiness",
            "create",
            "start",
            "guest_agent_readiness",
            "systemd_manager_readiness",
            "worker_credential_injection",
            "worker_service_start",
            "private_input_delivery",
            "stop",
            "destroy",
        }:
            return None
        result["operation"] = operation
    return result


class Evidence:
    def __init__(self, directory, run_id):
        self.directory, self.run_id = directory, run_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not directory.is_symlink(), "Report directory must not be a symlink")
        require(not (directory / "report.json").exists(), "Report directory already contains a run")
        self.secrets = set()
        self.data = {
            "schema": 1,
            "run_id": run_id,
            "status": "running",
            "transport": "production_broker_http",
            "events": [],
            "checks": [],
            "limits": [
                "No model inference or model benchmark performed",
                "TCP denial alone is not proof against every guest-root bypass",
                "Retained computer disks require operator lifecycle management",
            ],
        }

    def protect(self, value):
        if value:
            self.secrets.add(value)

    def assert_safe(self, value):
        encoded = json.dumps(value, ensure_ascii=True)
        require(
            not any(secret in encoded for secret in self.secrets),
            "Protected secret appeared in a public response",
        )

    def event(self, operation, status, *, error=None, **safe_fields):
        event = {"operation": operation, "status": status, **safe_fields}
        if error:
            event["error"] = {key: error.get(key) for key in ("code", "effect")}
            if error.get("code") in {"backend_failure", "unknown_effect"}:
                diagnostic = safe_backend_failure(error.get("backend_failure"))
                if diagnostic is not None:
                    event["error"]["backend_failure"] = diagnostic
        self.assert_safe(event)
        self.data["events"].append(copy.deepcopy(event))
        self.write()

    def check(self, name, status="passed", **evidence):
        item = {"name": name, "status": status, **evidence}
        self.assert_safe(item)
        self.data["checks"].append(copy.deepcopy(item))
        self.write()

    def write(self):
        self.assert_safe(self.data)
        path = self.directory / "report.json"
        temporary = self.directory / ".report.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)


class BrokerAPI:
    def __init__(self, client, url, operator, evidence):
        self.client, self.url, self.operator, self.evidence = client, url, operator, evidence
        evidence.protect(operator)

    async def request(
        self,
        method,
        path,
        body=None,
        *,
        token=None,
        allow_error=False,
        attachment=False,
        operation=None,
        computer_id=None,
    ):
        started = time.monotonic()
        credential = token or self.operator
        self.evidence.protect(credential)
        operation = operation or path
        try:
            response = await self.client.request(
                method,
                self.url + path,
                json=body,
                headers={"Authorization": "Bearer " + credential},
            )
        except httpx.HTTPError:
            effect = "none" if method == "GET" else "unknown"
            self.evidence.event(operation, "unknown", error={"code": "transport", "effect": effect})
            raise QualificationFailure(
                "Broker transport failed; effect is not retried", effect=effect
            )
        try:
            value = response.json()
        except ValueError:
            raise QualificationFailure(
                "Broker returned an invalid receipt", effect="unknown"
            ) from None
        if attachment and response.is_success:
            self.evidence.protect(value.get("token"))
            self.evidence.assert_safe({k: v for k, v in value.items() if k != "token"})
        else:
            self.evidence.assert_safe(value)
        error = value.get("error")
        self.evidence.event(
            operation,
            "failed" if not response.is_success or error else "passed",
            http_status=response.status_code,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
            error=error,
            computer_id=computer_id,
            metrics=safe_metrics(value.get("metrics") or (error or {}).get("metrics") or {}),
        )
        if not allow_error and (not response.is_success or error):
            raise QualificationFailure(
                "Broker operation failed: " + operation,
                effect=(error or {}).get("effect", "unknown"),
            )
        return value

    async def attach(self, request):
        return await self.request("POST", "/v1/attachments", request, attachment=True)

    async def tool(self, binding, name, arguments, *, allow_error=False, action_id=None):
        return await self.request(
            "POST",
            "/v1/tools/call",
            {"action_id": action_id or uuid.uuid4().hex, "name": name, "arguments": arguments},
            token=binding["token"],
            allow_error=allow_error,
            operation="tool:" + name,
            computer_id=binding["computer_id"],
        )


def text_receipt(body):
    require(not body.get("error"), "Expected successful tool receipt")
    for item in body.get("content", []):
        if item.get("type") == "text":
            return json.loads(item["text"])
    raise QualificationFailure("Tool receipt has no JSON text content")


def authorization_denied(body):
    error = body.get("error", {})
    return error.get("effect") == "none" and error.get("code") in {
        "unauthorized",
        "capability_denied",
        "lease_expired",
        "conflict",
    }


def safe_metrics(metrics):
    names = {
        "tool_calls_admitted",
        "tool_calls_completed",
        "ui_command_attempts",
        "ui_commands",
        "clicks",
        "key_presses",
        "characters_typed",
        "characters_filled",
        "scrolls",
        "drags",
        "observations",
        "observation_attempts",
        "screenshots",
        "screenshot_bytes",
        "navigations",
        "navigation_attempts",
        "exec_commands",
        "exec_timeouts",
        "file_reads",
        "file_writes",
        "file_bytes_read",
        "file_bytes_written",
        "secure_input_attempts",
        "secure_inputs",
        "secure_input_fields",
        "native_invocations",
        "focus_changes",
    }
    result = {}
    for field in ("delta", "totals"):
        if isinstance(metrics.get(field), dict):
            result[field] = {
                key: value
                for key, value in metrics[field].items()
                if key in names and type(value) is int and 0 <= value <= 2**53 - 1
            }
    if type(metrics.get("duration_ms")) is int:
        result["duration_ms"] = metrics["duration_ms"]
    if metrics.get("counts_are_lower_bounds") is True:
        result["counts_are_lower_bounds"] = True
    return result


def fixture_source(marker, port, password_digest):
    config = json.dumps({"marker": marker, "port": port, "password_digest": password_digest})
    return """import hashlib, json, os, sys, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
CONFIG = json.loads(CONFIG_LITERAL)
AUDIT = Path(__file__).with_suffix('.audit.json')
LOCK = threading.Lock()
def audit(key):
    with LOCK:
        value = json.loads(AUDIT.read_text()) if AUDIT.exists() else {}
        value[key] = value.get(key, 0) + 1
        temporary = AUDIT.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(AUDIT)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def reply(self, text, cookie=False):
        data = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', 'qualification=' + CONFIG['marker'] +
                             '; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == '/health':
            return self.reply('fixture-ready')
        if self.path == '/session':
            valid = ('qualification=' + CONFIG['marker']) in self.headers.get('Cookie', '').split('; ')
            audit('session_success' if valid else 'session_denied')
            return self.reply('Session verified ' + CONFIG['marker'] if valid else 'No session')
        if self.path == '/clicked':
            audit('browser_clicks')
            return self.reply('Browser click verified ' + CONFIG['marker'])
        self.reply('<html><title>Qualification</title><body><h1>' + CONFIG['marker'] + '</h1>'
                   '<a href="/clicked">Verify browser click</a>'
                   '<form method="post" action="/login">'
                   '<label>Username<input name="username" autocomplete="username"></label>'
                   '<label>Password<input name="password" type="password" autocomplete="current-password"></label>'
                   '<button type="submit">Sign in</button></form></body></html>')
    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size > 4096: return self.send_error(413)
        fields = parse_qs(self.rfile.read(size).decode())
        valid = (self.path == '/login' and fields.get('username') == ['qualification-user'] and
                 hashlib.sha256(fields.get('password', [''])[0].encode()).hexdigest() ==
                 CONFIG['password_digest'])
        audit('login_success' if valid else 'login_denied')
        self.reply('Login verified ' + CONFIG['marker'] if valid else 'Login denied', cookie=valid)
if '--native' in sys.argv:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    window = Gtk.Window(title='Qualification native fixture')
    window.set_default_size(560, 320)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin=24)
    window.add(box)
    entry = Gtk.Entry()
    entry.get_accessible().set_name('Qualification message')
    box.pack_start(entry, False, False, 0)
    label = Gtk.Label(label='Native result pending')
    button = Gtk.Button(label='Apply qualification')
    def apply(*_):
        Path(__file__).with_suffix('.native.txt').write_text(entry.get_text())
        label.set_text('Native result ' + entry.get_text())
    button.connect('clicked', apply)
    box.pack_start(button, False, False, 0)
    box.pack_start(label, False, False, 0)
    window.connect('destroy', Gtk.main_quit)
    window.show_all()
    Gtk.main()
else:
    ThreadingHTTPServer(('0.0.0.0', CONFIG['port']), Handler).serve_forever()
""".replace("CONFIG_LITERAL", repr(config))


def probe_source(targets, public, fixture_port):
    addresses = {"loopback": ["127.0.0.1", fixture_port], "public": list(public)}
    addresses.update({target["label"]: [target["host"], target["port"]] for target in targets})
    return f"""import json, os, socket
targets = json.loads({json.dumps(addresses)!r})
result = {{'uid': os.geteuid()}}
for label, address in targets.items():
    try:
        with socket.create_connection(tuple(address), timeout=2):
            result[label] = True
    except OSError:
        result[label] = False
print(json.dumps(result))
"""


def isolation_verdict(probes, targets):
    leaked = [item["label"] for item in targets if probes.get(item["label"]) is True]
    missing = [
        item["label"]
        for item in targets
        if not item.get("listener_verified") or probes.get(item["label"]) is not False
    ]
    controls = (
        probes.get("uid") == 0 and probes.get("loopback") is True and probes.get("public") is True
    )
    status = (
        "failed" if leaked else "passed" if targets and controls and not missing else "inconclusive"
    )
    return {
        "status": status,
        "connected_private_targets": leaked,
        "unverified_targets": missing,
        "positive_controls_passed": controls,
    }


def gateway_probe_source(targets):
    # Add only a previously absent /32 route to an explicitly selected peer.
    # Guest-root isolation must hold even when traffic is sent via the bridge.
    return """import ipaddress, json, os, socket, subprocess
targets = json.loads(TARGETS_LITERAL)
result = {'uid': os.geteuid(), 'routes': []}
def command(*args):
    return subprocess.run(['ip', *args], capture_output=True, text=True, timeout=5, check=True)
defaults = json.loads(command('-j', 'route', 'show', 'default').stdout)
for target in targets:
    item = {'label': target['label'], 'installed': False, 'restored': False}
    result['routes'].append(item)
    route = None
    try:
        assert result['uid'] == 0 and len(defaults) == 1
        address = ipaddress.ip_address(target['host'])
        gateway = str(ipaddress.ip_address(defaults[0]['gateway']))
        assert address.version == 4 and ipaddress.ip_address(gateway).version == 4
        destination = str(address) + '/32'
        interface = defaults[0]['dev']
        prior = json.loads(command('-j', 'route', 'show', 'exact', destination).stdout)
        item['preexisting'] = bool(prior)
        if prior:
            continue
        route = [destination, 'via', gateway, 'dev', interface]
        command('route', 'add', *route)
        item['installed'] = True
        selected = json.loads(command('-j', 'route', 'get', str(address)).stdout)
        item['route_verified'] = (len(selected) == 1 and selected[0].get('gateway') == gateway
                                  and selected[0].get('dev') == interface)
        item['gateway'], item['interface'] = gateway, interface
        if item['route_verified']:
            try:
                with socket.create_connection((str(address), target['port']), timeout=2):
                    item['connected'] = True
            except OSError:
                item['connected'] = False
    except Exception as error:
        item['error_class'] = type(error).__name__
    finally:
        if item['installed']:
            try:
                command('route', 'del', *route)
                item['restored'] = True
            except Exception as error:
                item['restore_error_class'] = type(error).__name__
print(json.dumps(result))
""".replace("TARGETS_LITERAL", repr(json.dumps(targets)))


def gateway_verdict(result, controls, targets):
    routes = {item.get("label"): item for item in result.get("routes", [])}
    reached = [label for label, item in routes.items() if item.get("connected") is True]
    complete = (
        bool(targets)
        and result.get("uid") == 0
        and controls.get("uid") == 0
        and controls.get("loopback") is True
        and controls.get("public") is True
        and all(
            target.get("listener_verified") is True
            and all(
                routes.get(target["label"], {}).get(field) is True
                for field in ("installed", "route_verified", "restored")
            )
            and routes.get(target["label"], {}).get("connected") is False
            for target in targets
        )
    )
    return {
        "status": "failed" if reached else "passed" if complete else "inconclusive",
        "connected_private_targets": reached,
    }


def confinement_source():
    return """import json, os, pathlib
maps = {name: [[int(value) for value in line.split()]
               for line in pathlib.Path('/proc/self/' + name).read_text().splitlines()]
        for name in ('uid_map', 'gid_map')}
sockets = ['/var/lib/incus/unix.socket', '/run/incus/unix.socket',
           '/var/lib/lxd/unix.socket', '/var/snap/lxd/common/lxd/unix.socket', '/run/lxd.socket']
mounts = []
for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
    left, right = line.split(' - ', 1)
    mounts.append({'target': left.split()[4], 'filesystem': right.split()[0]})
print(json.dumps(dict(maps, uid=os.geteuid(), mounts=mounts,
      visible_host_control_sockets=[path for path in sockets if os.path.lexists(path)])))
"""


def confinement_verdict(facts, kind):
    mapped_root = any(
        outside == 0 and count > 0
        for name in ("uid_map", "gid_map")
        for _inside, outside, count in facts.get(name, [])
    )
    unsafe = bool(facts.get("visible_host_control_sockets")) or (
        kind == "container" and mapped_root
    )
    complete = (
        kind is not None
        and facts.get("uid") == 0
        and bool(facts.get("uid_map"))
        and bool(facts.get("gid_map"))
        and bool(facts.get("mounts"))
        and "visible_host_control_sockets" in facts
    )
    return {
        "status": "failed" if unsafe else "passed" if complete else "inconclusive",
        "host_root_mapped": mapped_root,
        "operator_declared_guest_kind": kind,
        "host_effective_config_requires_external_verification": True,
    }


class LiveQualification:
    def __init__(self, args, api, evidence):
        self.args, self.api, self.evidence = args, api, evidence
        self.bindings, self.guests, self.credentials = {}, [], []
        self.namespace = "qualification:" + evidence.run_id
        self.secret = secrets.token_urlsafe(48)
        self.evidence.protect(self.secret)

    def owner(self, index):
        return {
            "kind": "identity",
            "realm": "qualification",
            "namespace": self.namespace,
            "identity": "guest-" + str(index),
        }

    async def attach(self, guest, generation, takeover=False):
        started = time.monotonic()
        binding = await self.api.attach(
            {
                "owner": guest["owner"],
                "slot": "main",
                "profile": self.args.profile,
                "session_id": self.evidence.run_id + ":" + str(generation),
                "runtime_generation": generation,
                "ttl_seconds": 3600,
                "takeover": takeover,
            }
        )
        self.bindings[binding["computer_id"]] = binding
        guest["binding"] = binding
        guest.setdefault("start_ms", []).append(round((time.monotonic() - started) * 1000, 2))
        return binding

    async def tool(self, guest, name, arguments, **kwargs):
        return await self.api.tool(guest["binding"], name, arguments, **kwargs)

    async def execute(self, guest, command, timeout=30):
        receipt = text_receipt(
            await self.tool(
                guest, "workspace_exec", {"command": command, "timeout_seconds": timeout}
            )
        )
        require(
            receipt.get("exit_code") == 0
            and not receipt.get("timed_out")
            and not receipt.get("truncated"),
            "Guest command did not finish successfully",
        )
        return receipt["stdout"].strip()

    async def write(self, guest, path, text):
        await self.tool(guest, "workspace_write", {"path": path, "text": text})

    async def read(self, guest, path):
        body = text_receipt(await self.tool(guest, "workspace_read", {"path": path}))
        require(not body.get("truncated"), "Guest evidence file was truncated")
        return body["text"]

    async def launch_fixture(self, guest, native=False):
        filename = shlex.quote(guest["fixture"])
        log = shlex.quote(guest["fixture"] + (".native.log" if native else ".http.log"))
        command = (
            f"nohup /usr/bin/python3 {filename}"
            + (" --native" if native else "")
            + f" >{log} 2>&1 </dev/null &"
        )
        await self.execute(guest, command)
        if not native:
            probe = (
                "import time, urllib.request\n"
                "for attempt in range(80):\n"
                " try:\n"
                f"  r=urllib.request.urlopen({guest['origin'] + '/health'!r}, timeout=1)\n"
                "  assert r.read()==b'fixture-ready'\n  break\n"
                " except Exception:\n  time.sleep(0.1)\n"
                "else:\n raise SystemExit(1)\nprint('fixture-ready')"
            )
            require(
                await self.execute(guest, "/usr/bin/python3 -c " + shlex.quote(probe))
                == "fixture-ready",
                "Guest fixture did not become ready",
            )

    async def prepare(self, index):
        marker = self.evidence.run_id + "-" + str(index)
        guest = {
            "owner": self.owner(index),
            "marker": marker,
            "fixture": "qualification_" + marker + ".py",
            "origin": f"http://127.0.0.1:{self.args.fixture_port}",
        }
        self.guests.append(guest)
        binding = await self.attach(guest, 1)
        required = {"browser", "browser_semantics", "screenshots", "exec", "files"}
        if not self.args.no_native:
            required |= {"desktop", "desktop_semantics"}
        require(
            required <= set(binding["capabilities"]), "Profile lacks required qualification tools"
        )
        access = await self.api.request("GET", f"/v1/computers/{binding['computer_id']}/access")
        self.evidence.check(
            "operator_access",
            computer_id=binding["computer_id"],
            instance_name=access["instance_name"],
            instance_id=access["instance_id"],
            guest_admin=access["guest_admin"],
        )
        await self.write(
            guest,
            guest["fixture"],
            fixture_source(
                marker, self.args.fixture_port, hashlib.sha256(self.secret.encode()).hexdigest()
            ),
        )
        await self.write(guest, guest["fixture"] + ".durable.txt", marker)
        await self.launch_fixture(guest)
        output = await self.execute(
            guest,
            "/usr/bin/python3 -c "
            + shlex.quote(
                "import json, pathlib, socket, subprocess\n"
                "p=pathlib.Path('/sys/fs/cgroup/memory.current')\n"
                "print(json.dumps({'hostname':socket.gethostname(), 'memory_current_bytes':"
                "int(p.read_text()) if p.exists() else None, 'boot_id':"
                "pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'init_start_ticks':"
                "pathlib.Path('/proc/1/stat').read_text().rsplit(')',1)[1].split()[19], 'addresses':"
                "json.loads(subprocess.check_output(['ip','-j','-4','addr']))}))"
            ),
        )
        guest["system"] = json.loads(output)
        await self.browser_smoke(guest)
        if not self.args.no_native:
            await self.native_smoke(guest)
        return guest

    def screenshot(self, guest, body, surface):
        images = [item for item in body.get("content", []) if item.get("type") == "image"]
        require(bool(images), "Expected actual screenshot pixels")
        raw = base64.b64decode(images[0]["data"], validate=True)
        require(
            raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) > 1000,
            "Screenshot is not a valid PNG",
        )
        path = self.evidence.directory / (guest["marker"] + "-" + surface + ".png")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    async def browser_smoke(self, guest):
        body = await self.tool(guest, "browser_navigate", {"url": guest["origin"]})
        view = text_receipt(body)
        require(guest["marker"] in view["accessibility"], "Browser reached the wrong guest fixture")
        pixels = self.screenshot(guest, body, "browser")
        node = next(
            item for item in view["elements"] if item.get("label") == "Verify browser click"
        )
        after = text_receipt(
            await self.tool(
                guest,
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "click", "ref": node["ref"]},
                },
            )
        )
        require(
            "Browser click verified " + guest["marker"] in after["accessibility"],
            "Browser click did not produce the expected page",
        )
        audit = json.loads(await self.read(guest, guest["fixture"].replace(".py", ".audit.json")))
        require(
            audit.get("browser_clicks") == 1,
            "Server did not independently record exactly one click",
        )
        self.evidence.check(
            "browser_interaction",
            computer_id=guest["binding"]["computer_id"],
            server_click_count=audit["browser_clicks"],
            screenshot=pixels,
        )

    async def native_fixture_dependency(self, guest):
        # The production AT-SPI driver uses dbus-next, not Python GTK. Install
        # this small fixture-only dependency explicitly in our owned guest.
        probe = (
            "import json\ntry:\n import gi\n gi.require_version('Gtk','3.0')\n"
            " from gi.repository import Gtk\n print('true')\n"
            "except (ImportError,ValueError):\n print('false')"
        )
        command = "/usr/bin/python3 -c " + shlex.quote(probe)
        present = json.loads(await self.execute(guest, command))
        require(type(present) is bool, "Invalid native fixture dependency probe")
        if not present:
            await self.execute(
                guest,
                "sudo -n apt-get install -y --no-install-recommends python3-gi gir1.2-gtk-3.0",
                timeout=120,
            )
            require(
                json.loads(await self.execute(guest, command)) is True,
                "Native fixture dependency did not become available",
            )
        self.evidence.check(
            "native_fixture_dependencies",
            computer_id=guest["binding"]["computer_id"],
            installed_for_fixture=not present,
        )

    async def native_smoke(self, guest):
        await self.native_fixture_dependency(guest)
        await self.launch_fixture(guest, native=True)
        deadline = time.monotonic() + 30
        while True:
            body = await self.tool(guest, "computer_observe", {"surface": "desktop"})
            view = text_receipt(body)
            nodes = [
                node
                for node in view.get("elements", [])
                if node.get("name") == "Qualification message"
            ]
            if nodes:
                break
            require(time.monotonic() < deadline, "Native GTK fixture did not become accessible")
            await asyncio.sleep(0.25)
        filled = text_receipt(
            await self.tool(
                guest,
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "fill", "ref": nodes[0]["ref"], "text": guest["marker"]},
                },
            )
        )
        button = next(
            node for node in filled["elements"] if node.get("name") == "Apply qualification"
        )
        require(bool(button.get("actions")), "Native button has no AT-SPI action")
        after = await self.tool(
            guest,
            "computer_act",
            {
                "observation_id": filled["observation_id"],
                "action": {
                    "type": "invoke",
                    "ref": button["ref"],
                    "action_name": button["actions"][0]["name"],
                },
            },
        )
        text = await self.read(guest, guest["fixture"].replace(".py", ".native.txt"))
        require(text == guest["marker"], "Independent native callback file has the wrong value")
        self.evidence.check(
            "native_accessibility",
            computer_id=guest["binding"]["computer_id"],
            independent_file_match=True,
            screenshot=self.screenshot(guest, after, "desktop"),
        )

    async def login_target(self, guest):
        view = text_receipt(await self.tool(guest, "browser_navigate", {"url": guest["origin"]}))
        refs = {item["label"]: item["ref"] for item in view["elements"]}
        return {
            "observation_id": view["observation_id"],
            "origin": guest["origin"],
            "username_ref": refs["Username"],
            "secret_ref": refs["Password"],
        }, refs["Sign in"]

    async def credential_flow(self):
        first = self.guests[0]
        credential = await self.api.request(
            "POST",
            "/v1/credentials",
            {
                "realm": "qualification",
                "label": "Qualification " + self.evidence.run_id,
                "origin": first["origin"],
                "username": "qualification-user",
                "secret": self.secret,
            },
        )
        self.credentials.append(credential["id"])
        await self.api.request(
            "POST",
            "/v1/credentials/assign",
            {"credential_ids": self.credentials, "owners": [first["owner"]]},
        )
        if len(self.guests) > 1:
            other = self.guests[1]
            assigned = text_receipt(await self.tool(other, "credentials_list", {}))["credentials"]
            require(not assigned, "Unassigned identity can discover a credential")
            target, _ = await self.login_target(other)
            denied = await self.tool(
                other,
                "credentials_use",
                {"credential_id": credential["id"], "target": target},
                allow_error=True,
            )
            require(authorization_denied(denied), "Unassigned identity was not denied before input")
            self.evidence.check(
                "credential_assignment_denial", computer_id=other["binding"]["computer_id"]
            )
        await self.api.request(
            "POST",
            "/v1/credentials/assign",
            {
                "credential_ids": self.credentials,
                "owners": [guest["owner"] for guest in self.guests],
            },
        )
        await self.parallel(self.login(guest, credential["id"]) for guest in self.guests)

    async def login(self, guest, credential_id):
        target, button = await self.login_target(guest)
        arguments = {"credential_id": credential_id, "target": target}
        action_id = uuid.uuid4().hex
        receipt = await self.tool(guest, "credentials_use", arguments, action_id=action_id)
        view = text_receipt(receipt)
        require(
            view.get("filled") is True
            and view.get("submitted") is False
            and view.get("privacy", {}).get("active") is True,
            "Private fill was not acknowledged",
        )
        repeated = await self.tool(guest, "credentials_use", arguments, action_id=action_id)
        require(repeated == receipt, "Private input duplicate did not preserve its receipt")
        guarded = await self.tool(guest, "computer_observe", {"surface": "browser"})
        require(
            not any(item.get("type") == "image" for item in guarded.get("content", [])),
            "Secret entry produced screenshot pixels",
        )
        view = text_receipt(guarded)
        require(button in view["action_refs"], "Private entry lost the submit continuation")
        after = text_receipt(
            await self.tool(
                guest,
                "computer_act",
                {
                    "observation_id": view["observation_id"],
                    "action": {"type": "click", "ref": button},
                },
            )
        )
        require(
            "Login verified " + guest["marker"] in after["accessibility"], "Website rejected login"
        )
        audit = json.loads(await self.read(guest, guest["fixture"].replace(".py", ".audit.json")))
        require(
            audit.get("login_success") == 1 and not audit.get("login_denied"),
            "Independent website audit did not record exactly one successful POST",
        )
        self.evidence.check(
            "private_credential_login",
            computer_id=guest["binding"]["computer_id"],
            independent_login_count=1,
            duplicate_receipt_equal=True,
            screenshot_suppressed=True,
        )

    async def handoff(self, guest):
        previous = guest["binding"]
        current = await self.attach(guest, 2, takeover=True)
        require(
            current["computer_id"] == previous["computer_id"]
            and current["epoch"] > previous["epoch"],
            "Identity handoff did not retain the computer and advance fencing",
        )
        denied = await self.api.tool(
            previous,
            "workspace_write",
            {"path": guest["fixture"] + ".durable.txt", "text": "stale-controller-corruption"},
            allow_error=True,
        )
        require(authorization_denied(denied), "Old grant accepted a mutating operation")
        require(
            await self.read(guest, guest["fixture"] + ".durable.txt") == guest["marker"],
            "Old controller changed the durable marker",
        )
        self.evidence.check(
            "identity_handoff",
            computer_id=current["computer_id"],
            previous_epoch=previous["epoch"],
            epoch=current["epoch"],
            old_grant_write_denied=True,
            independent_file_unchanged=True,
        )

    async def cold_restart(self, guest, cycle):
        previous = guest["binding"]
        await self.api.request("POST", "/v1/lease/release", token=previous["token"])
        await self.api.request("POST", f"/v1/computers/{previous['computer_id']}/stop")
        current = await self.attach(guest, cycle + 3)
        require(
            current["computer_id"] == previous["computer_id"],
            "Cold start changed durable computer identity",
        )
        identity = json.loads(
            await self.execute(
                guest,
                "/usr/bin/python3 -c "
                + shlex.quote(
                    "import json,pathlib\nprint(json.dumps({'boot_id':"
                    "pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'init_start_ticks':"
                    "pathlib.Path('/proc/1/stat').read_text().rsplit(')',1)[1].split()[19]}))"
                ),
            )
        )
        changed = any(identity[key] != guest["system"][key] for key in identity)
        require(changed, "Guest init/boot identity did not change across claimed cold restart")
        guest["system"].update(identity)
        require(
            await self.read(guest, guest["fixture"] + ".durable.txt") == guest["marker"],
            "Cold restart lost the workspace",
        )
        await self.launch_fixture(guest)
        cookie = None
        if not self.args.no_credentials:
            after = text_receipt(
                await self.tool(guest, "browser_navigate", {"url": guest["origin"] + "/session"})
            )
            require(
                "Session verified " + guest["marker"] in after["accessibility"],
                "Persistent browser cookie was lost during cold restart",
            )
            audit = json.loads(
                await self.read(guest, guest["fixture"].replace(".py", ".audit.json"))
            )
            require(
                audit.get("session_success") == cycle + 1 and not audit.get("session_denied"),
                "Website did not independently validate the resumed cookie",
            )
            cookie = True
        self.evidence.check(
            "cold_restart",
            computer_id=current["computer_id"],
            cycle=cycle,
            workspace_persisted=True,
            cookie_verified_by_website=cookie,
            guest_boot_changed=True,
            boot_evidence=identity,
            ready_ms=guest["start_ms"][-1],
        )

    async def network_checks(self):
        controls = []
        if self.args.host_control_file:
            controls = json.loads(self.args.host_control_file.read_text())
        for guest in self.guests:
            confinement = json.loads(
                await self.execute(
                    guest, "sudo -n /usr/bin/python3 -c " + shlex.quote(confinement_source())
                )
            )
            confinement_result = confinement_verdict(confinement, self.args.guest_kind)
            self.evidence.check(
                "guest_root_confinement",
                computer_id=guest["binding"]["computer_id"],
                facts=confinement,
                **confinement_result,
            )
            require(
                confinement_result["status"] != "failed",
                "Guest exposes host-root mapping or a host-control socket",
            )
            targets = []
            for kind, addresses in (
                ("host", self.args.host_target),
                ("explicit_peer", self.args.peer_target),
                ("metadata", [("169.254.169.254", 80)]),
            ):
                for index, (host, port) in enumerate(addresses):
                    verified = any(
                        item.get("host") == host
                        and item.get("port") == port
                        and item.get("connected") is True
                        for item in controls
                    )
                    targets.append(
                        {
                            "label": f"{kind}_{index}",
                            "host": host,
                            "port": port,
                            "listener_verified": verified,
                            "verification_source": "operator_control_file",
                        }
                    )
            for index, peer in enumerate(self.guests):
                if peer is guest:
                    continue
                # Addresses come only from computers created in this run, not arbitrary discovery.
                for interface in peer["system"]["addresses"]:
                    for address in interface.get("addr_info", []):
                        if address.get("scope") != "global" or address.get("family") != "inet":
                            continue
                        host = address["local"]
                        control = probe_source(
                            [], (host, self.args.fixture_port), self.args.fixture_port
                        )
                        result = json.loads(
                            await self.execute(peer, "/usr/bin/python3 -c " + shlex.quote(control))
                        )
                        targets.append(
                            {
                                "label": f"owned_peer_{index}_{len(targets)}",
                                "host": host,
                                "port": self.args.fixture_port,
                                "listener_verified": result["public"],
                                "verification_source": "peer_guest_own_address_tcp",
                            }
                        )
            source = probe_source(targets, self.args.public_control, self.args.fixture_port)
            probes = json.loads(
                await self.execute(
                    guest,
                    "sudo -n /usr/bin/python3 -c " + shlex.quote(source),
                    timeout=min(120, len(targets) * 3 + 15),
                )
            )
            verdict = isolation_verdict(probes, targets)
            self.evidence.check(
                "guest_root_network_isolation",
                computer_id=guest["binding"]["computer_id"],
                targets=targets,
                probes=probes,
                **verdict,
            )
            require(verdict["status"] != "failed", "Guest root reached a forbidden target")
            peers = [
                target
                for target in targets
                if target["label"].startswith(("owned_peer_", "explicit_peer_"))
            ]
            routed = json.loads(
                await self.execute(
                    guest,
                    "sudo -n /usr/bin/python3 -c " + shlex.quote(gateway_probe_source(peers)),
                    timeout=min(120, len(peers) * 15 + 15),
                )
            )
            routed_verdict = gateway_verdict(routed, probes, peers)
            self.evidence.check(
                "guest_root_gateway_peer_isolation",
                computer_id=guest["binding"]["computer_id"],
                probes=routed,
                **routed_verdict,
            )
            require(
                routed_verdict["status"] != "failed",
                "Guest root reached a peer via the host gateway",
            )

    @staticmethod
    async def parallel(coroutines):
        # Await every submitted effect even when a peer fails; do not leave creation
        # running unobserved while cleanup starts.
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        failures = [value for value in results if isinstance(value, BaseException)]
        if failures:
            raise failures[0]
        return results

    async def run(self):
        started = time.monotonic()
        self.evidence.data["configuration"] = plan(self.args)
        await self.parallel(self.prepare(index) for index in range(self.args.concurrency))
        views = (await self.api.request("GET", "/v1/computers"))["computers"]
        active = [
            view
            for view in views
            if view.get("owner", {}).get("namespace") == self.namespace
            and view.get("phase") == "leased"
            and view.get("lease") is not None
        ]
        require(
            len(active) == self.args.concurrency,
            "Not all requested computers were live simultaneously",
        )
        self.evidence.check(
            "parallel_guests",
            count=len(active),
            initial_batch_workload_ready_ms=round((time.monotonic() - started) * 1000, 2),
            guest_measurements=[
                {"computer_id": guest["binding"]["computer_id"], "system": guest["system"]}
                for guest in self.guests
            ],
        )
        if self.args.isolation:
            await self.network_checks()
        else:
            self.evidence.check("guest_root_network_isolation", "skipped")
        if not self.args.no_credentials:
            await self.credential_flow()
        else:
            self.evidence.check("private_credential_login", "skipped")
        if self.args.no_native:
            self.evidence.check("native_accessibility", "skipped")
        await self.parallel(self.handoff(guest) for guest in self.guests)
        for cycle in range(self.args.repeats):
            await self.parallel(self.cold_restart(guest, cycle) for guest in self.guests)
        if self.args.host_metrics_file:
            raw = self.args.host_metrics_file.read_bytes()
            require(len(raw) < 1_048_576, "Host metrics file is too large")
            metrics = json.loads(raw)
            self.evidence.assert_safe(metrics)
            self.evidence.data["external_host_metrics"] = {
                "verified_by_harness": False,
                "data": metrics,
            }
        times = [guest["start_ms"][0] for guest in self.guests]
        self.evidence.data["timing"] = {
            "initial_attach_ms": times,
            "median_ms": statistics.median(times),
            "max_ms": max(times),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    async def cleanup(self):
        for binding in list(self.bindings.values()):
            try:
                await self.api.request(
                    "POST", "/v1/lease/release", token=binding["token"], allow_error=True
                )
            except QualificationFailure:
                self.evidence.check(
                    "cleanup_release", "unknown", computer_id=binding["computer_id"]
                )
        try:
            computers = (await self.api.request("GET", "/v1/computers"))["computers"]
            for computer in computers:
                owner = computer.get("owner", {})
                if (
                    owner.get("realm") != "qualification"
                    or owner.get("namespace") != self.namespace
                ):
                    continue
                try:
                    await self.api.request("POST", f"/v1/computers/{computer['computer_id']}/stop")
                    self.evidence.check(
                        "cleanup_stopped", computer_id=computer["computer_id"], retained_disk=True
                    )
                except QualificationFailure:
                    self.evidence.check(
                        "cleanup_stopped", "unknown", computer_id=computer["computer_id"]
                    )
        except QualificationFailure:
            self.evidence.check("cleanup_discovery", "unknown")
        for credential in self.credentials:
            try:
                await self.api.request("DELETE", "/v1/credentials/" + credential)
            except QualificationFailure:
                self.evidence.check("cleanup_credential", "unknown", credential_id=credential)


async def apply(args):
    token = read_token(args.operator_token_env, args.operator_token_file)
    evidence = Evidence(args.report_dir, uuid.uuid4().hex)
    async with httpx.AsyncClient(
        timeout=args.timeout_seconds, trust_env=False, follow_redirects=False
    ) as client:
        runner = LiveQualification(
            args, BrokerAPI(client, args.broker_url, token, evidence), evidence
        )
        try:
            await runner.run()
            evidence.data["status"] = "passed"
        except Exception as error:  # noqa: BLE001 - safe boundary never prints arbitrary exception data.
            evidence.data["status"] = "failed"
            # No arbitrary exception repr or guest data enters diagnostics.
            evidence.event(
                "qualification",
                "failed",
                error={"code": type(error).__name__, "effect": getattr(error, "effect", "none")},
                reason=str(error)
                if isinstance(error, QualificationFailure)
                else "Invalid qualification response",
            )
        finally:
            await runner.cleanup()
            states = {item["status"] for item in evidence.data["checks"]}
            if evidence.data["status"] == "passed" and states & {
                "unknown",
                "inconclusive",
                "skipped",
            }:
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
    proposed = plan(args)
    if not args.apply:
        print(json.dumps(proposed, indent=2))
        return 0
    return asyncio.run(apply(args))


if __name__ == "__main__":
    raise SystemExit(main())
