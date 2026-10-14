"""Explicit full-state VM fork qualification through the production broker.

Dry run is inert. Apply creates one fresh parent and at least two children in a
unique qualification namespace, then stops only that namespace and retains disks.
No cloud provisioning, model calls, upstream edits, or automatic fork retries.
"""

import argparse
import asyncio
import inspect
import ipaddress
import json
import os
import re
import shlex
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
    broker_url,
    positive,
    read_token,
    require,
    text_receipt,
)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--broker-url", type=broker_url, required=True)
    result.add_argument("--profile", required=True, help="Full desktop VM profile with guest admin")
    result.add_argument("--report-dir", type=Path, required=True)
    result.add_argument("--children", type=positive, default=2)
    result.add_argument("--fixture-port", type=positive, default=43127)
    result.add_argument("--worker-port", type=positive, default=18765)
    result.add_argument("--timeout-seconds", type=positive, default=600)
    result.add_argument("--host-metrics-file", type=Path)
    result.add_argument(
        "--host-metrics-command-file",
        type=Path,
        help="Trusted JSON argv for read-only host samples at phase boundaries",
    )
    source = result.add_mutually_exclusive_group()
    source.add_argument("--operator-token-env", default="MEERKAT_COMPUTER_OPERATOR_TOKEN")
    source.add_argument("--operator-token-file", type=Path)
    result.add_argument("--apply", action="store_true")
    return result


def plan(args):
    if not 2 <= args.children <= 16:
        raise ValueError("Full-state qualification needs 2-16 children from one checkpoint")
    if max(args.fixture_port, args.worker_port) > 65535 or args.fixture_port == args.worker_port:
        raise ValueError("Fixture and worker ports must be distinct valid TCP ports")
    return {
        "apply": args.apply,
        "broker_url": args.broker_url,
        "profile": args.profile,
        "children": args.children,
        "checkpoint_count": 1,
        "phase_host_metrics": args.host_metrics_command_file is not None,
        "scope": [
            "browser_js_ram",
            "unsaved_editor",
            "unsaved_blender",
            "same_checkpoint",
            "independent_divergence",
            "worker_token_rotation",
            "broker_access_denial",
            "fresh_child_metrics",
            "old_observation_denial",
            "old_grant_denial",
            "vault_assignment_not_inherited",
            "guest_root_all_peer_network_denial",
            "latency_and_guest_resources",
        ],
        "cleanup": "Release and stop only this run's namespace; retain disks and evidence",
        "report_dir": str(args.report_dir),
    }


def check_fork_view(view, request_id, source, children):
    ids = view.get("children", [])
    require(
        view.get("request_id") == request_id
        and view.get("source_computer_id") == source
        and view.get("phase") == "completed"
        and view.get("ready_children") == children
        and not view.get("error")
        and len(ids) == children
        and all(isinstance(value, str) and value for value in ids)
        and len(set(ids)) == children
        and source not in ids,
        "Fork did not confirm the requested source and every independent child",
    )


def check_blender_response(value, challenge):
    require(value.get("request_id") == challenge, "Blender has not answered the fresh challenge")
    require(
        isinstance(value.get("nonce"), str)
        and len(value["nonce"]) == 32
        and type(value.get("pid")) is int
        and value["pid"] > 0
        and value.get("start_ticks")
        and value.get("filepath") == ""
        and value.get("object") == "MC_FORK_CUBE"
        and len(value.get("location", [])) == 3,
        "Blender did not prove a live unsaved scene",
    )


def check_identity(before, after):
    require(
        before["browser"]["nonce"] == after["browser"]["nonce"]
        and all(before["editor"][key] == after["editor"][key] for key in ("pid", "start_ticks"))
        and all(
            before["blender"][key] == after["blender"][key]
            for key in ("nonce", "pid", "start_ticks", "object", "filepath")
        )
        and after["editor"]["unsaved"] is True
        and after["blender"]["filepath"] == ""
        and all(
            before["system"][key] == after["system"][key] for key in ("boot_id", "init_start_ticks")
        ),
        "Browser, editor, Blender, or guest process identity was not preserved",
    )


def check_restored(before, after):
    check_identity(before, after)
    require(
        before["browser"] == after["browser"]
        and before["editor"] == after["editor"]
        and before["blender"]["location"] == after["blender"]["location"]
        and before["disk"] == after["disk"],
        "A restored branch differs from the shared checkpoint's application state",
    )


def check_divergence(before, states, labels):
    require(len(states) == len(labels) and len(labels) >= 3, "Missing independent branch evidence")
    for index, (state, label) in enumerate(zip(states, labels, strict=True), 1):
        check_identity(before, state)
        require(
            state["browser"]["value"] == label
            and state["disk"] == label
            and state["editor"]["text"] == before["editor"]["text"].replace("checkpoint", label)
            and state["blender"]["location"] == [float(index), 0.0, 0.0],
            "A branch mutation leaked or its independent application state was lost",
        )


def check_worker_auth(value, *, child):
    require(
        value.get("old_status") == (401 if child else 422)
        and value.get("current_status") == 422
        and value.get("token_changed") is child,
        "Worker credential rotation or its positive authorization control failed",
    )


def check_parent_observation_denied(value):
    # The public broker maps the worker's stale_observation to Conflict. Fresh
    # observations and divergent actions later prove the child grant still works.
    error = value.get("error", {})
    require(
        error.get("code") == "conflict" and error.get("effect") == "none",
        "Inherited observation did not produce the expected public no-effect conflict",
    )


def check_credential_scope(value, credential_id, *, child):
    credentials = value.get("credentials")
    require(isinstance(credentials, list), "Credential list did not confirm assignment scope")
    ids = [item.get("id") for item in credentials]
    require(
        (not ids) if child else (credential_id in ids),
        "Vault assignment was inherited by a child or lost by the parent",
    )


def check_network_baseline(value):
    try:
        address = ipaddress.IPv4Address(value.get("address", ""))
        valid = not (
            address.is_loopback
            or address.is_unspecified
            or address.is_multicast
            or address.is_link_local
        )
    except ipaddress.AddressValueError:
        valid = False
    require(
        valid
        and not value.get("probe_error")
        and value.get("uid") == 0
        and value.get("interface")
        and all(
            value.get("controls", {}).get(name) is True
            for name in ("loopback_worker", "loopback_fixture", "primary_worker", "primary_fixture")
        ),
        "Root network baseline lacks a primary IPv4 or a live local listener",
    )


def primary_network(defaults, devices):
    """Select one path, retaining equivalent kernel/networkd route entries."""
    import ipaddress
    import re

    result = {"route_count": 0, "routes": []}

    def failed(code):
        return dict(result, probe_error=code)

    def ipv4(value):
        try:
            address = ipaddress.IPv4Address(value)
            if address.is_unspecified or address.is_multicast or address.is_loopback:
                return None
            return str(address)
        except (ipaddress.AddressValueError, TypeError):
            return None

    if not isinstance(defaults, list) or len(defaults) > 32:
        return failed("default_route_invalid")
    result["route_count"] = len(defaults)
    groups = {}
    for route in defaults:
        if not isinstance(route, dict):
            return failed("default_route_invalid")
        interface = route.get("dev")
        gateway = ipv4(route.get("gateway"))
        metric = route.get("metric", 0)
        if (
            not isinstance(interface, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface)
            or gateway is None
            or type(metric) is not int
            or not 0 <= metric <= 4_294_967_295
            or route.get("type", "unicast") != "unicast"
            or route.get("dst", "default") != "default"
            or "nexthops" in route
        ):
            return failed("default_route_invalid")
        protocol = route.get("protocol", "unspecified")
        # Protocol names are kernel metadata, never arbitrary diagnostic text.
        if protocol not in (
            "unspecified",
            "redirect",
            "kernel",
            "boot",
            "static",
            "dhcp",
            "ra",
            "gated",
            "mrouted",
            "bird",
            "babel",
            "zebra",
            "bgp",
            "isis",
            "ospf",
            "rip",
        ):
            protocol = str(protocol) if type(protocol) is int and 0 <= protocol <= 255 else "other"
        key = interface, gateway
        group = groups.setdefault(
            key,
            {
                "interface": interface,
                "gateway": gateway,
                "entry_count": 0,
                "metrics": [],
                "protocols": [],
            },
        )
        group["entry_count"] += 1
        group["metrics"] = sorted(set(group["metrics"] + [metric]))
        group["protocols"] = sorted(set(group["protocols"] + [protocol]))
    result["routes"] = [groups[key] for key in sorted(groups)]
    if not groups:
        return failed("default_route_missing")
    if len(groups) != 1:
        return failed("default_route_ambiguous")
    interface, gateway = next(iter(groups))
    if not isinstance(devices, list) or len(devices) > 128:
        return failed("primary_ipv4_invalid")
    addresses = set()
    for device in devices:
        if not isinstance(device, dict):
            return failed("primary_ipv4_invalid")
        if device.get("ifname") != interface:
            continue
        entries = device.get("addr_info", [])
        if not isinstance(entries, list) or len(entries) > 128:
            return failed("primary_ipv4_invalid")
        for entry in entries:
            if not isinstance(entry, dict):
                return failed("primary_ipv4_invalid")
            if entry.get("scope") != "global" or entry.get("family", "inet") != "inet":
                continue
            address = ipv4(entry.get("local"))
            if address is None:
                return failed("primary_ipv4_invalid")
            addresses.add(address)
    if len(addresses) != 1:
        return failed("primary_ipv4_ambiguous" if addresses else "primary_ipv4_missing")
    return dict(result, address=addresses.pop(), interface=interface, gateway=gateway)


def safe_network_baseline(value):
    """Project only bounded network facts from our diagnostic JSON into evidence."""
    result = {}
    if type(value.get("uid")) is int and 0 <= value["uid"] <= 4_294_967_295:
        result["uid"] = value["uid"]
    for name in ("address", "gateway"):
        try:
            result[name] = str(ipaddress.IPv4Address(value.get(name, "")))
        except (ipaddress.AddressValueError, TypeError):
            pass
    interface = value.get("interface")
    if isinstance(interface, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
        result["interface"] = interface
    count = value.get("route_count")
    if type(count) is int and 0 <= count <= 32:
        result["route_count"] = count
    routes = value.get("routes")
    if isinstance(routes, list) and len(routes) <= 32:
        normalized = []
        for route in routes:
            if not isinstance(route, dict):
                continue
            # Reuse the same normalizer rather than copying arbitrary route fields.
            selected = primary_network(
                [{"dev": route.get("interface"), "gateway": route.get("gateway")}], []
            )
            if not selected["routes"]:
                continue
            item = selected["routes"][0]
            entries, metrics, protocols = (
                route.get(key) for key in ("entry_count", "metrics", "protocols")
            )
            if not (
                type(entries) is int
                and 1 <= entries <= 32
                and isinstance(metrics, list)
                and len(metrics) <= 32
                and all(type(metric) is int and 0 <= metric <= 4_294_967_295 for metric in metrics)
                and isinstance(protocols, list)
                and len(protocols) <= 32
            ):
                continue
            item["entry_count"], item["metrics"] = entries, sorted(set(metrics))
            item["protocols"] = sorted(
                {
                    primary_network(
                        [
                            {
                                "dev": item["interface"],
                                "gateway": item["gateway"],
                                "protocol": protocol,
                            }
                        ],
                        [],
                    )["routes"][0]["protocols"][0]
                    for protocol in protocols
                }
            )
            normalized.append(item)
        result["routes"] = normalized
    controls = value.get("controls")
    if isinstance(controls, dict):
        result["controls"] = {
            key: controls.get(key) is True
            for key in ("loopback_worker", "loopback_fixture", "primary_worker", "primary_fixture")
        }
    error = value.get("probe_error")
    if error is not None:
        result["probe_error"] = (
            error
            if error
            in (
                "default_route_invalid",
                "default_route_missing",
                "default_route_ambiguous",
                "primary_ipv4_invalid",
                "primary_ipv4_missing",
                "primary_ipv4_ambiguous",
                "ip_command_timeout",
                "ip_command_failed",
                "ip_json_invalid",
            )
            else "invalid_probe_output"
        )
    return result


def check_peer_denial(value, targets):
    def key(item):
        return item.get("computer_id"), item.get("address"), item.get("port")

    probes = value.get("probes", [])
    require(
        value.get("uid") == 0
        and targets
        and len(probes) == len(targets)
        and len({key(item) for item in probes}) == len(probes)
        and {key(item) for item in targets} == {key(item) for item in probes}
        and all(item.get("connected") is False for item in probes),
        "Root peer probe reached another branch or did not test every requested listener",
    )


def network_baseline_source(fixture_port, worker_port):
    return (
        inspect.getsource(primary_network)
        + """\nimport json, os, subprocess, urllib.request
def ip(*args):
    raw = subprocess.check_output(['ip','-j',*args], text=True, timeout=5, stderr=subprocess.DEVNULL)
    if len(raw) > 65536: raise ValueError()
    return json.loads(raw)
try:
    result = primary_network(ip('-4','route','show','default'), ip('-4','addr','show'))
except subprocess.TimeoutExpired:
    result = {'probe_error': 'ip_command_timeout'}
except (OSError, subprocess.CalledProcessError):
    result = {'probe_error': 'ip_command_failed'}
except ValueError:
    result = {'probe_error': 'ip_json_invalid'}
result.update(uid=os.geteuid(), controls={})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
hosts = [('loopback','127.0.0.1')]
if 'address' in result: hosts.append(('primary',result['address']))
for label, host in hosts:
    for kind, port in [('worker',WORKER_PORT),('fixture',FIXTURE_PORT)]:
        try:
            with opener.open('http://'+host+':'+str(port)+'/health', timeout=2) as response:
                data = response.read(4097)
                good = (data == b'fixture-ready') if kind == 'fixture' else (
                    json.loads(data).get('protocol_version') == 1 and json.loads(data).get('ready') is True)
                result['controls'][label+'_'+kind] = response.status == 200 and good
        except (OSError, ValueError, AttributeError):
            result['controls'][label+'_'+kind] = False
print(json.dumps(result))
""".replace("WORKER_PORT", str(worker_port)).replace("FIXTURE_PORT", str(fixture_port))
    )


def peer_probe_source(targets):
    return """import concurrent.futures, json, os, socket
targets = json.loads(TARGETS_LITERAL)
def probe(target):
    try:
        with socket.create_connection((target['address'], target['port']), timeout=2):
            return dict(target, connected=True)
    except OSError:
        return dict(target, connected=False)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    probes = list(pool.map(probe, targets))
print(json.dumps({'uid': os.geteuid(), 'probes': probes}))
""".replace("TARGETS_LITERAL", repr(json.dumps(targets)))


def editor_node(view):
    apps = {
        app["app_id"]
        for app in view.get("applications", [])
        if "mousepad" in app.get("name", "").lower()
    }
    nodes = [
        node
        for node in view.get("elements", [])
        if node.get("app_id") in apps
        and {"editable", "multi_line"} <= set(node.get("states", []))
        and isinstance(node.get("text"), str)
        and not node.get("text_truncated")
    ]
    require(len(nodes) == 1, "Expected one complete native Mousepad editable buffer")
    return nodes[0]


def browser_state(view):
    matches = re.findall(
        r"MC_BROWSER ([0-9a-f]{32}) ([a-z0-9-]+) END_MC_BROWSER", view.get("accessibility", "")
    )
    require(
        bool(matches) and len(set(matches)) == 1, "Browser RAM sentinel is missing or ambiguous"
    )
    return dict(zip(("nonce", "value"), matches[0], strict=True))


def browser_fixture_source(port, worker_port, children):
    # The page's nonce exists only in JS memory. The worker token is retained only
    # in this fixture process's memory; neither token nor hash enters any receipt.
    labels = ["parent"] + ["child-" + str(index) for index in range(1, children + 1)]
    buttons = "".join(
        f"<button onclick=\"setValue('{label}')\">Set {label}</button>" for label in labels
    )
    html = """<!doctype html><meta charset="utf-8"><title>Full state fork</title>
<style>body{font:24px sans-serif;padding:40px;background:#eef3fb}button{margin:8px;padding:16px}</style>
<h1>Unsaved browser memory</h1><p id="state"></p>BUTTONS
<script>
window.forkState = {nonce: crypto.randomUUID().replaceAll('-', ''), value: 'checkpoint'};
function setValue(value) { window.forkState.value=value; render(); }
function render() { document.getElementById('state').textContent =
 'MC_BROWSER '+window.forkState.nonce+' '+window.forkState.value+' END_MC_BROWSER'; }
render();
</script>""".replace("BUTTONS", buttons)
    return (
        """import hmac, json, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
TOKEN_PATH = Path('/var/lib/meerkat-computer/worker.token')
ORIGINAL_TOKEN = TOKEN_PATH.read_text().strip()
HTML = HTML_LITERAL.encode()
def auth_status(token):
    request = urllib.request.Request(WORKER_URL, data=b'{}', method='POST',
              headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.path == '/health':
            data, kind = b'fixture-ready', 'text/plain'
        elif self.path == '/auth':
            current = TOKEN_PATH.read_text().strip()
            result = {'old_status': auth_status(ORIGINAL_TOKEN),
                      'current_status': auth_status(current),
                      'token_changed': not hmac.compare_digest(ORIGINAL_TOKEN, current)}
            data, kind = json.dumps(result).encode(), 'application/json'
        elif self.path == '/':
            data, kind = HTML, 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)
ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
""".replace("HTML_LITERAL", repr(html))
        .replace("WORKER_URL", repr(f"http://127.0.0.1:{worker_port}/v1/call"))
        .replace("PORT", str(port))
    )


def blender_fixture_source(prefix):
    # Polling is performed on Blender's main thread. A copied response file cannot
    # satisfy a new controller challenge, and no .blend is written or reloaded.
    return """import bpy, json, os, uuid
from pathlib import Path
PREFIX = Path(PREFIX_LITERAL)
NONCE = uuid.uuid4().hex
PID = os.getpid()
START = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
LAST = None
bpy.ops.wm.read_factory_settings(use_empty=False)
OBJECT = bpy.data.objects['Cube']
OBJECT.name = 'MC_FORK_CUBE'
OBJECT.location = (0, 0, 0)
def tick():
    global LAST
    request = PREFIX.with_suffix('.request.json')
    if not request.exists(): return 0.1
    try:
        value = json.loads(request.read_text())
    except (OSError, ValueError):
        return 0.1
    if value['request_id'] == LAST: return 0.1
    if 'x' in value:
        OBJECT.location.x = float(value['x'])
        bpy.context.view_layer.update()
    result = {'request_id': value['request_id'], 'nonce': NONCE, 'pid': PID,
              'start_ticks': START, 'filepath': bpy.data.filepath,
              'object': OBJECT.name, 'location': list(OBJECT.location)}
    temporary = PREFIX.with_suffix('.response.tmp')
    temporary.write_text(json.dumps(result))
    temporary.replace(PREFIX.with_suffix('.response.json'))
    LAST = value['request_id']
    return 0.1
bpy.app.timers.register(tick, first_interval=0.1, persistent=True)
""".replace("PREFIX_LITERAL", repr(prefix))


def resource_source():
    return """import json, pathlib
p = pathlib.Path
memory = {line.split(':')[0]: int(line.split()[1])*1024
          for line in p('/proc/meminfo').read_text().splitlines() if ' kB' in line}
processes = []
for entry in p('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try:
        name = (entry/'comm').read_text().strip()
        if name not in {'blender', 'mousepad', 'chrome', 'chromium', 'python3', 'python'}: continue
        fields = (entry/'stat').read_text().rsplit(')',1)[1].split()
        processes.append({'pid': int(entry.name), 'name': name, 'start_ticks': fields[19],
                          'rss_bytes': int(fields[21])*4096,
                          'cpu_ticks': int(fields[11])+int(fields[12])})
    except (OSError, IndexError, ValueError): pass
print(json.dumps({'boot_id': p('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'init_start_ticks': p('/proc/1/stat').read_text().rsplit(')',1)[1].split()[19],
 'memory_total_bytes': memory.get('MemTotal'), 'memory_available_bytes': memory.get('MemAvailable'),
 'uptime_seconds': float(p('/proc/uptime').read_text().split()[0]), 'processes': processes}))
"""


class FullStateQualification(LiveQualification):
    def __init__(self, args, api, evidence):
        super().__init__(args, api, evidence)
        self.fork_id = str(uuid.uuid4())
        self.prefix = "full-state-" + evidence.run_id
        self.editor_text = "MC_EDITOR_" + uuid.uuid4().hex + " checkpoint"
        self.last_host_sample_ns = 0

    async def python(self, guest, source, timeout=30):
        return await self.execute(guest, "/usr/bin/python3 -c " + shlex.quote(source), timeout)

    async def root_python(self, guest, source, timeout=30):
        return await self.execute(
            guest, "sudo -n /usr/bin/python3 -c " + shlex.quote(source), timeout
        )

    async def root_probe(self, guest, probe, source, timeout=30):
        require(probe in {"network_baseline", "peer_tcp"}, "Unknown guest root probe")
        receipt = text_receipt(
            await self.tool(
                guest,
                "workspace_exec",
                {
                    "command": "sudo -n /usr/bin/python3 -c " + shlex.quote(source),
                    "timeout_seconds": timeout,
                },
            )
        )
        diagnostic = {
            key: receipt[key]
            for key in ("timed_out", "truncated")
            if type(receipt.get(key)) is bool
        }
        if type(receipt.get("exit_code")) is int:
            diagnostic["exit_code"] = receipt["exit_code"]
        reason = "command_failed"
        if (
            receipt.get("exit_code") == 0
            and not receipt.get("timed_out")
            and not receipt.get("truncated")
        ):
            output = receipt.get("stdout")
            if isinstance(output, str) and len(output) <= 65_536:
                try:
                    value = json.loads(output)
                    if isinstance(value, dict):
                        return value
                except ValueError:
                    pass
            reason = "invalid_probe_json"
        self.evidence.check(
            "guest_root_probe_command",
            "failed",
            probe=probe,
            computer_id=guest["binding"]["computer_id"],
            reason=reason,
            **diagnostic,
        )
        raise QualificationFailure("Guest root probe " + probe + " failed: " + reason)

    async def sample_host(self, phase):
        if self.args.host_metrics_command_file is None:
            return
        raw = self.args.host_metrics_command_file.read_bytes()
        require(len(raw) <= 16_384, "Host sampler argv file is too large")
        argv = json.loads(raw)
        require(
            isinstance(argv, list)
            and argv
            and all(isinstance(value, str) and value and "\0" not in value for value in argv),
            "Host sampler must be an explicit JSON argv array",
        )
        started = time.monotonic()
        wall_started = time.time_ns()
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        except (TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            raise QualificationFailure("Read-only host resource sample did not finish") from None
        require(
            process.returncode == 0 and len(output) <= 1_048_576,
            "Read-only host resource sample failed or exceeded its output bound",
        )
        value = json.loads(output)
        require(
            isinstance(value, dict)
            and type(value.get("sampled_at_unix_ns")) is int
            and isinstance(value.get("host_memory"), dict)
            and isinstance(value.get("guests"), list),
            "Invalid host resource sample",
        )
        stamp = value["sampled_at_unix_ns"]
        require(
            wall_started - 60_000_000_000 <= stamp <= time.time_ns() + 60_000_000_000
            and stamp > self.last_host_sample_ns,
            "Host resource sample is not fresh for this phase",
        )
        self.last_host_sample_ns = stamp
        self.evidence.check(
            "phase_host_resources",
            phase=phase,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
            data=value,
            source="operator_configured_read_only_command",
        )

    async def observe(self, guest, surface):
        body = await self.tool(guest, "computer_observe", {"surface": surface})
        return body, text_receipt(body)

    async def action(self, guest, view, action):
        return await self.tool(
            guest, "computer_act", {"observation_id": view["observation_id"], "action": action}
        )

    async def semantic_action(self, guest, surface, select):
        # Only an explicit public no-effect conflict permits a fresh attempt.
        # The inherited observation fence deliberately uses action_error instead.
        for attempt in range(1, 4):
            _, view = await self.observe(guest, surface)
            body = await self.tool(
                guest,
                "computer_act",
                {"observation_id": view["observation_id"], "action": select(view)},
                allow_error=True,
                action_id=uuid.uuid4().hex,
            )
            error = body.get("error")
            if not error:
                text_receipt(body)
                return body
            if error.get("code") != "conflict" or error.get("effect") != "none":
                raise QualificationFailure(
                    "Semantic action failed without an explicit no-effect conflict",
                    effect=error.get("effect", "unknown"),
                )
            self.evidence.check(
                "semantic_action_no_effect_conflict",
                computer_id=guest["binding"]["computer_id"],
                surface=surface,
                attempt=attempt,
            )
        raise QualificationFailure("Semantic action conflicted after three fresh observations")

    async def editor_view(self, guest, wait=False):
        deadline = time.monotonic() + (30 if wait else 0)
        while True:
            body, view = await self.observe(guest, "desktop")
            try:
                return body, view, editor_node(view)
            except QualificationFailure:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.25)

    async def blender(self, guest, x=None):
        challenge = uuid.uuid4().hex
        request = {"request_id": challenge}
        if x is not None:
            request["x"] = x
        await self.write(guest, self.prefix + ".request.json", json.dumps(request))
        result = json.loads(
            await self.python(
                guest,
                """import json, time
from pathlib import Path
path = Path(PATH_LITERAL)
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    try:
        value = json.loads(path.read_text())
        if value.get('request_id') == CHALLENGE_LITERAL:
            print(json.dumps(value))
            break
    except (OSError, ValueError): pass
    time.sleep(0.1)
else: raise SystemExit(1)
""".replace("PATH_LITERAL", repr(self.prefix + ".response.json")).replace(
                    "CHALLENGE_LITERAL", repr(challenge)
                ),
                timeout=55,
            )
        )
        check_blender_response(result, challenge)
        return result

    async def prepare_parent(self):
        guest = {
            "owner": self.owner(0),
            "marker": self.evidence.run_id + "-parent",
            "fixture": self.prefix + ".py",
            "origin": f"http://127.0.0.1:{self.args.fixture_port}",
        }
        self.guests.append(guest)
        binding = await self.attach(guest, 1)
        require(
            {
                "browser",
                "browser_semantics",
                "desktop",
                "desktop_semantics",
                "screenshots",
                "exec",
                "files",
            }
            <= set(binding["capabilities"]),
            "Profile lacks full application qualification tools",
        )
        await self.write(
            guest,
            guest["fixture"],
            browser_fixture_source(
                self.args.fixture_port, self.args.worker_port, self.args.children
            ),
        )
        await self.write(guest, self.prefix + ".disk", "checkpoint")
        await self.launch_fixture(guest)
        body = await self.tool(guest, "browser_navigate", {"url": guest["origin"]})
        browser_state(text_receipt(body))
        await self.execute(
            guest,
            "nohup mousepad --disable-server >"
            + shlex.quote(self.prefix + ".mousepad.log")
            + " 2>&1 </dev/null &",
        )
        await self.editor_view(guest, wait=True)
        await self.semantic_action(
            guest,
            "desktop",
            lambda view: {
                "type": "fill",
                "ref": editor_node(view)["ref"],
                "text": self.editor_text,
            },
        )
        blender_file = self.prefix + ".blender.py"
        await self.write(guest, blender_file, blender_fixture_source(self.prefix))
        await self.execute(
            guest,
            "nohup blender --factory-startup --python "
            + shlex.quote(blender_file)
            + " >"
            + shlex.quote(self.prefix + ".blender.log")
            + " 2>&1 </dev/null &",
        )
        await self.blender(guest)
        credential = await self.api.request(
            "POST",
            "/v1/credentials",
            {
                "realm": "qualification",
                "label": "Full state fork " + self.evidence.run_id,
                "origin": guest["origin"],
                "username": "synthetic-fork-user",
                "secret": self.secret,
            },
        )
        self.credentials.append(credential["id"])
        await self.api.request(
            "POST",
            "/v1/credentials/assign",
            {
                "credential_ids": self.credentials,
                "owners": [guest["owner"]],
            },
        )
        return guest

    async def capture(self, guest, stage):
        browser_body, browser = await self.observe(guest, "browser")
        editor_body = await self.semantic_action(
            guest, "desktop", lambda view: {"type": "focus", "ref": editor_node(view)["ref"]}
        )
        desktop = text_receipt(editor_body)
        node = editor_node(desktop)
        names = [
            window.get("name", "")
            for window in desktop.get("windows", [])
            if window.get("app_id") == node["app_id"]
        ]
        unsaved = any("untitled" in name.lower() for name in names)
        system = json.loads(await self.python(guest, resource_source()))
        editor_processes = [item for item in system["processes"] if item["name"] == "mousepad"]
        require(len(editor_processes) == 1 and unsaved, "Editor is not one live unsaved document")
        blender = await self.blender(guest)
        state = {
            "browser": browser_state(browser),
            "editor": {
                "pid": editor_processes[0]["pid"],
                "start_ticks": editor_processes[0]["start_ticks"],
                "text": node["text"],
                "unsaved": unsaved,
            },
            "blender": blender,
            "system": system,
            "disk": await self.read(guest, self.prefix + ".disk"),
        }
        # Blender does not expose a full semantic tree. Bring its real window to
        # the front for visual corroboration of the Python scene receipt.
        await self.python(
            guest,
            """import subprocess
result = subprocess.run(['xdotool','search','--onlyvisible','--class','[Bb]lender'],
                        capture_output=True, text=True, check=True)
windows = result.stdout.split()
assert windows
subprocess.run(['xdotool','windowactivate','--sync',windows[-1]], check=True)
""",
        )
        blender_body, _ = await self.observe(guest, "desktop")
        self.evidence.check(
            "application_state",
            computer_id=guest["binding"]["computer_id"],
            stage=stage,
            state=state,
            screenshots={
                "browser": self.screenshot(guest, browser_body, stage + "-browser"),
                "editor": self.screenshot(guest, editor_body, stage + "-editor"),
                "blender": self.screenshot(guest, blender_body, stage + "-blender"),
            },
        )
        return state

    async def fork(self, source, targets):
        binding = source["binding"]
        self.evidence.data["fork_request_id"] = self.fork_id
        self.evidence.write()
        started = time.monotonic()
        view = await self.api.request(
            "POST",
            "/v1/forks",
            {
                "request_id": self.fork_id,
                "source_computer_id": binding["computer_id"],
                "source_lease_id": binding["lease_id"],
                "source_epoch": binding["epoch"],
                "targets": targets,
            },
            operation="fork_one_checkpoint",
        )
        check_fork_view(view, self.fork_id, binding["computer_id"], len(targets))
        self.evidence.check(
            "one_checkpoint_all_children_ready",
            fork=view,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return view

    async def check_auth(self, guest, *, child):
        output = await self.python(
            guest,
            "import urllib.request\nprint(urllib.request.urlopen("
            + repr(guest["origin"] + "/auth")
            + ",timeout=15).read().decode())",
        )
        result = json.loads(output)
        check_worker_auth(result, child=child)
        self.evidence.check(
            "worker_old_bearer_denied" if child else "parent_worker_bearer_retained",
            computer_id=guest["binding"]["computer_id"],
            **result,
        )

    async def child_fences(self, child, old_browser):
        # This must be the child's first tool receipt, before any observations.
        first = await self.tool(child, "workspace_read", {"path": self.prefix + ".disk"})
        require(text_receipt(first)["text"] == "checkpoint", "Child disk checkpoint was lost")
        totals = first.get("metrics", {}).get("totals", {})
        require(
            totals.get("tool_calls_admitted") == 1
            and totals.get("tool_calls_completed") == 1
            and totals.get("file_reads") == 1
            and all(
                totals.get(field, 0) == 0
                for field in ("exec_commands", "ui_commands", "navigations", "observations")
            ),
            "Child inherited parent action metrics",
        )
        self.evidence.check(
            "child_metrics_started_fresh",
            computer_id=child["binding"]["computer_id"],
            totals=totals,
        )
        old = await self.action_error(child, old_browser)
        check_parent_observation_denied(old)
        self.evidence.check(
            "parent_observation_denied", computer_id=child["binding"]["computer_id"]
        )

    async def action_error(self, guest, view):
        node = next(item for item in view["elements"] if item.get("label") == "Set parent")
        return await self.tool(
            guest,
            "computer_act",
            {
                "observation_id": view["observation_id"],
                "action": {"type": "focus", "ref": node["ref"]},
            },
            allow_error=True,
        )

    async def broker_access(self):
        for index, guest in enumerate(self.guests):
            other = self.guests[(index + 1) % len(self.guests)]
            denied = await self.api.request(
                "GET",
                "/v1/computers/" + other["binding"]["computer_id"] + "/access",
                token=guest["binding"]["token"],
                allow_error=True,
            )
            require(authorization_denied(denied), "Agent grant received operator access to a peer")
            self.evidence.check(
                "broker_peer_access_denied",
                computer_id=guest["binding"]["computer_id"],
                peer_computer_id=other["binding"]["computer_id"],
            )

    async def network_baseline(self, guest, phase):
        value = safe_network_baseline(
            await self.root_probe(
                guest,
                "network_baseline",
                network_baseline_source(self.args.fixture_port, self.args.worker_port),
            )
        )
        try:
            check_network_baseline(value)
        except QualificationFailure:
            self.evidence.check(
                "guest_root_listener_controls",
                "failed",
                phase=phase,
                computer_id=guest["binding"]["computer_id"],
                **value,
            )
            raise
        self.evidence.check(
            "guest_root_listener_controls",
            phase=phase,
            computer_id=guest["binding"]["computer_id"],
            **value,
        )
        return value

    async def peer_probe(self, guest):
        targets = [
            {
                "computer_id": other["binding"]["computer_id"],
                "address": other["network"]["address"],
                "port": port,
            }
            for other in self.guests
            if other is not guest
            for port in (self.args.worker_port, self.args.fixture_port)
        ]
        result = await self.root_probe(guest, "peer_tcp", peer_probe_source(targets))
        # Keep actual reached/blocked results in evidence even on a failed assertion.
        try:
            check_peer_denial(result, targets)
        except QualificationFailure:
            self.evidence.check(
                "guest_root_all_peer_tcp_probes",
                "failed",
                computer_id=guest["binding"]["computer_id"],
                result=result,
            )
            raise
        self.evidence.check(
            "guest_root_all_peer_tcp_probes",
            computer_id=guest["binding"]["computer_id"],
            result=result,
        )

    async def network_checks(self):
        before = await self.parallel(
            self.network_baseline(guest, "before_peer_probes") for guest in self.guests
        )
        require(
            len({item["address"] for item in before}) == len(self.guests),
            "Independent branch primary IPv4 addresses are not unique",
        )
        for guest, value in zip(self.guests, before, strict=True):
            guest["network"] = value
        await self.parallel(self.peer_probe(guest) for guest in self.guests)
        after = await self.parallel(
            self.network_baseline(guest, "after_peer_probes") for guest in self.guests
        )
        require(
            all(old["address"] == new["address"] for old, new in zip(before, after, strict=True)),
            "A listener address changed while isolation was being tested",
        )
        self.evidence.check(
            "guest_root_all_peer_network_denial",
            branches=len(self.guests),
            directed_connections_tested=len(self.guests) * (len(self.guests) - 1) * 2,
            positive_controls="Loopback and primary IPv4 HTTP health before and after",
        )

    async def mutate(self, guest, label, index):
        await self.semantic_action(
            guest,
            "browser",
            lambda view: {
                "type": "click",
                "ref": next(
                    item for item in view["elements"] if item.get("label") == "Set " + label
                )["ref"],
            },
        )
        await self.semantic_action(
            guest,
            "desktop",
            lambda view: {
                "type": "fill",
                "ref": editor_node(view)["ref"],
                "text": self.editor_text.replace("checkpoint", label),
            },
        )
        await self.blender(guest, x=index)
        await self.write(guest, self.prefix + ".disk", label)

    async def run(self):
        started = time.monotonic()
        self.evidence.data["configuration"] = plan(self.args)
        await self.sample_host("before_parent")
        source = await self.prepare_parent()
        before = await self.capture(source, "checkpoint")
        await self.sample_host("checkpoint_ready")
        await self.check_auth(source, child=False)
        _, old_browser = await self.observe(source, "browser")
        targets = [
            {"owner": self.owner(index), "slot": "main"}
            for index in range(1, self.args.children + 1)
        ]
        view = await self.fork(source, targets)
        await self.sample_host("children_ready")
        inspected = await self.api.request("GET", "/v1/forks/" + self.fork_id)
        check_fork_view(inspected, self.fork_id, source["binding"]["computer_id"], len(targets))
        require(
            inspected["children"] == view["children"], "Fork inspection changed child identities"
        )
        for index, (target, computer_id) in enumerate(
            zip(targets, view["children"], strict=True), 1
        ):
            child = {
                "owner": target["owner"],
                "marker": self.evidence.run_id + "-child-" + str(index),
                "origin": source["origin"],
            }
            self.guests.append(child)
            binding = await self.attach(child, 1)
            require(binding["computer_id"] == computer_id, "Attach replaced the restored computer")
            require(
                binding["lease_id"] != source["binding"]["lease_id"]
                and binding["token"] != source["binding"]["token"],
                "Child inherited a parent grant",
            )
            await self.child_fences(child, old_browser)
        require(
            len({guest["binding"]["token"] for guest in self.guests}) == len(self.guests),
            "Branches share a controller grant",
        )
        for index, guest in enumerate(self.guests):
            state = await self.capture(guest, "restored")
            check_restored(before, state)
            await self.check_auth(guest, child=index > 0)
            assigned = text_receipt(await self.tool(guest, "credentials_list", {}))
            check_credential_scope(assigned, self.credentials[0], child=index > 0)
            self.evidence.check(
                "vault_assignment_scope",
                computer_id=guest["binding"]["computer_id"],
                inherited=False,
                is_parent=index == 0,
            )
        self.evidence.check("all_apps_preserved_from_one_checkpoint", children=self.args.children)
        await self.broker_access()
        await self.network_checks()
        labels = ["parent"] + ["child-" + str(index) for index in range(1, self.args.children + 1)]
        for index, (guest, label) in enumerate(zip(self.guests, labels, strict=True), 1):
            await self.mutate(guest, label, index)
        # Read every branch after all writes; checking only immediately after each
        # write would miss a later mutation leaking back to the parent or a peer.
        states = [await self.capture(guest, "diverged") for guest in self.guests]
        check_divergence(before, states, labels)
        await self.sample_host("after_divergence")
        self.evidence.check("independent_application_and_disk_divergence", branches=len(states))
        for guest in self.guests:
            old = guest["binding"]
            await self.attach(guest, 2, takeover=True)
            denied = await self.api.tool(
                old, "workspace_read", {"path": self.prefix + ".disk"}, allow_error=True
            )
            require(authorization_denied(denied), "Superseded branch grant remained usable")
            self.evidence.check("superseded_grant_denied", computer_id=old["computer_id"])
        if self.args.host_metrics_file:
            raw = self.args.host_metrics_file.read_bytes()
            require(len(raw) < 1_048_576, "Host resource evidence is too large")
            value = json.loads(raw)
            self.evidence.assert_safe(value)
            self.evidence.data["external_host_metrics"] = {
                "verified_by_harness": False,
                "data": value,
            }
        self.evidence.data["elapsed_seconds"] = round(time.monotonic() - started, 2)


async def apply(args):
    token = read_token(args.operator_token_env, args.operator_token_file)
    evidence = Evidence(args.report_dir, uuid.uuid4().hex)
    evidence.data["limits"] = [
        "No model inference or model benchmark performed",
        "Live network connections to external services are not duplicated by a VM checkpoint",
        "Guest RSS is not host physical RAM consumption or a memory sharing measurement",
        "Retained computer disks require operator lifecycle management",
    ]
    async with httpx.AsyncClient(
        timeout=args.timeout_seconds, trust_env=False, follow_redirects=False
    ) as client:
        runner = FullStateQualification(
            args, BrokerAPI(client, args.broker_url, token, evidence), evidence
        )
        try:
            await runner.run()
            evidence.data["status"] = "passed"
        except Exception as error:  # noqa: BLE001 - do not expose arbitrary guest or token text.
            evidence.data["status"] = "failed"
            evidence.event(
                "full_state_qualification",
                "failed",
                error={"code": type(error).__name__, "effect": getattr(error, "effect", "none")},
                reason=str(error)
                if isinstance(error, QualificationFailure)
                else "Invalid qualification response",
            )
        finally:
            await runner.cleanup()
            try:
                await runner.sample_host("after_cleanup")
            except Exception:  # noqa: BLE001 - preserve primary failure and never print sampler data.
                evidence.check("phase_host_resources", "unknown", phase="after_cleanup")
            if evidence.data["status"] == "passed" and any(
                check["status"] != "passed" for check in evidence.data["checks"]
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
    proposed = plan(args)
    if not args.apply:
        print(json.dumps(proposed, indent=2))
        return 0
    return asyncio.run(apply(args))


if __name__ == "__main__":
    raise SystemExit(main())
