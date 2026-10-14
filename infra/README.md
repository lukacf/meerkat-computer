# Local Linux and GCP deployment

The Rust broker and Incus daemon run on the same Linux host. GCP supplies a host;
the same Incus backend supplies the computers on GCP and on a home server. No
provider key or Meerkat model runtime is installed in a guest.

These are deployment tools, not a record of a deployed service. The repository's
default tests do not contact Incus or GCP. A live host/image/browser qualification
is still required before treating a new host as production capacity.

Start with the audience-oriented [Linux installation guide](../docs/deployment/local-linux.mdx)
or [GCP installation guide](../docs/deployment/gcp.mdx). This reference retains
implementation and operator details. The optional always-on
[demand gateway](../docs/operations/host-lifecycle.mdx) owns host wake and idle
suspension; it does not change the broker's guest authority.

## Prepare an existing Linux host

Use Ubuntu 24.04, Python 3.11 or newer, and an Incus release supporting bridged NIC
port isolation and filtering. The supplied installer uses the Ubuntu repository's
Incus package. Containers require no nested virtualization. VM profiles require a
usable `/dev/kvm`; on GCP choose a compatible machine type and enable nested
virtualization when creating the host.

The host installer includes Git, Make/C/C++ build prerequisites and kernel module
tools. It loads `br_netfilter` and persists it in
`/etc/modules-load.d/meerkat-computer.conf`, because Incus bridge filtering requires
that module. A module-loading failure stops installation. Install the selected
Rust toolchain separately before building the broker.

Copy this repository onto the host without `.state`, `.env`, `.venv`, local
credentials or benchmark data. After committing the initial repository, a Git
archive is a convenient source-only payload:

```bash
git archive HEAD --format=tar.gz --output /tmp/meerkat-computer-source.tgz
```

Unpack the archive in a dedicated source directory. The repository's Rust wrapper
supports source archives directly, without `.git`, and keeps build output in its
external cache. Install the toolchain selected by `rust-toolchain.toml` on the
Linux build host and install `uv` separately. From the source directory, run:

```bash
sudo bash infra/install-host.sh --apply
uv sync --locked
sudo python3 infra/host-setup.py --project meerkat-computer --storage-gib 100 --apply
sudo python3 infra/build-image.py --project meerkat-computer --name mc-browser-v1 --apply
make build
```

Omitting `--apply` from the deployment scripts prints the plan and performs no
infrastructure commands. The scripts save ownership intents before effects and
reject existing resources with another marker. Keep the host setup and image
build state files; they are needed to distinguish owned resources from name
collisions after an interruption. A stopped image builder is deliberately retained
for inspection. The scripts never format an existing block device.

The host setup creates a separate Incus project, a private IPv4 NAT bridge, an
owned network ACL, and a Btrfs loop-backed storage pool. The profile enables MAC,
IPv4 and IPv6 filtering plus port isolation between guests. The ACL rejects new
guest connections to RFC1918 networks, link-local addresses (including cloud
metadata), carrier-grade NAT, loopback and multicast/reserved ranges. Other IPv4
outbound traffic is allowed. New inbound connections are restricted to the
bridge's host address; Incus supplies established connection replies and baseline
DHCP/DNS exceptions. IPv6 is neither assigned nor allowed from guests.

Port isolation closes the direct bridge path; the ACL also closes a route through
the host gateway. Guest root cannot edit this host-side policy. The backend
verifies the bridge and ACL before starting a computer. There are no proxy devices
or public worker ports. Choose a non-overlapping private range with `--address`; the default
is `10.211.0.1/24`. Keep disk headroom outside the pool. For a larger installation,
move to operator-provisioned Btrfs/ZFS storage after measuring the workload.

These policies do not permit guest access to home-server or VPC services. Local
services inside the same guest remain usable on its loopback interface. A future
private-service exception must be an explicit host policy change, not a guest
route or model-selected option. Existing pools created without this policy fail
closed: provision a fresh project, bridge, storage name and setup state file.
The scripts do not claim to revoke existing conntrack sessions by editing a live
bridge. Cross-guest, host and metadata denial must still be qualified on a real
Incus host before deploying this configuration as capacity.

See [Incus network ACL semantics](https://linuxcontainers.org/incus/docs/main/howto/network_acls/)
and the [Incus 6.0 firewall implementation](https://github.com/lxc/incus/blob/v6.0.4/internal/server/firewall/drivers/drivers_nftables_templates.go#L112-L174)
for the bridge boundary and baseline service rules.

The image builder installs the package with `uv sync --frozen`, installs Chromium,
and publishes a private immutable image. Python, Git, curl, Node.js/npm and the
basic C/C++ build tools are included. Language-specific toolchains such as Rust
are not preinstalled. It copies only Python source, the project file, lockfile
and provisioning helpers. No token, lease, browser profile or user
workspace is included. The final JSON contains the full image fingerprint. Put it
in every matching `profiles.<name>.image` in a copy of `configs/local.toml`.
Container and VM images are separate: pass `--kind virtual-machine`, a fresh
builder name and a separate `--state` file for a VM image. Its disk profile must
be at least 16 GiB. Base image aliases resolve during build; the resolved base
fingerprint and source hash are saved in build state. Package resolution is
locked, while OS package installation still uses the configured Ubuntu mirror.

## Guest administration

`profiles.<name>.guest_admin = true` grants the worker user passwordless sudo
inside that computer. The supplied coder and desktop profiles enable it. The
default for profiles that omit the field is false. The worker and Chromium still
start as the ordinary `meerkat-computer` user; an agent uses `sudo -n` through its
existing exec capability when it needs administration.

New images include the sudo package without a grant. After verifying the exact
computer ownership and incarnation, the backend writes and validates its owned
`/etc/sudoers.d/meerkat-computer` file with root ownership and mode 0440. The grant
choice is recorded in host-owned Incus metadata and must match the immutable
computer profile on recovery. An admin computer cannot become a restricted
computer by changing a profile flag: create a different computer for that policy.
Rebuild images to include the sudo dependency before enabling this setting.

Containers use `security.privileged=false` and `security.idmap.isolated=true`,
which allocates non-overlapping host UID/GID ranges. The host needs enough
subordinate IDs for its container count. No Incus socket, broker state or host
filesystem is mounted in the computer. Guest root owns its own files, worker
token and runtime state, so the guest worker is not a security boundary against
that guest's administrator. Host authorization, peer filtering and resource
controls remain outside the guest. Containers share the host kernel; use VM
profiles when independently administered guests need a stronger kernel boundary.
See [Incus container security](https://linuxcontainers.org/incus/docs/main/explanation/security/#container-security).

## Optional native desktop image

Build a separate image for full computer use, with a fresh builder and state:

```bash
sudo python3 infra/build-image.py --project meerkat-computer --name mc-desktop-v1 --state .state/desktop-image.json --desktop --apply
```

The desktop uses Xvfb and Openbox with one private D-Bus session, AT-SPI bus and
registry per computer. Browser and native apps share its display and accessibility
tree. X11 accepts no TCP connections and requires a private authentication cookie.
There is no VNC server or public desktop endpoint. The worker's `--desktop` mode
is set by the image service, and the backend requires the image's desktop marker
when the profile requests the `desktop` capability.

Both desktop bundles include Mousepad, Thunar, the Evince PDF viewer and
`poppler-utils` (`pdfinfo`, `pdftotext`, `pdftoppm`). The default `--apps full` bundle also
installs Blender, Inkscape, GIMP, LibreOffice Writer/Calc/Impress with the GTK3
integration, and additional fonts. Use `--apps minimal` to keep the editor, file
manager, PDF viewer and PDF utilities. Omit `--desktop` for the headless browser image;
neither desktop bundle is installed there. Image fingerprints and saved build
intents include the desktop choice and app bundle. Do not reuse a builder state
when changing these options. Existing guests do not acquire new packages
automatically; build and qualify a new image to make the PDF tools available.

Blender uses Mesa's software renderer in this Xvfb session. This makes the image
usable without a GPU, but demanding 3D work will consume CPU and should influence
pool admission settings. The apps are installed for general native computer use;
installation alone does not establish their accessibility coverage. Some controls
may require screenshots and coordinates. Chromium still runs with its sandbox
enabled, headed, and with renderer accessibility enabled.

The broker writes screen width and height from the computer profile to the private
worker environment. The desktop starts at that exact fixed size, and the worker
rejects a lease whose observation dimensions disagree. Changing the screen size
requires a different computer profile, not an implicit live desktop resize.

`infra/desktop-session.py` supervises Xvfb, the buses, registry, window manager and
worker. Startup is bounded to 30 seconds; shutdown sends TERM then KILL to managed
process groups. The guest systemd service also kills its entire control group,
including native apps, within 12 seconds. Persistent workspace and browser state
remain outside the temporary display and bus runtime directory.

The same launcher can qualify an isolated Linux container without systemd:

```bash
python3 infra/desktop-session.py --width 1280 --height 720 -- .venv/bin/python infra/image-check.py --desktop
```

Run it as the guest's unprivileged worker user, with the installed dependencies
and `PLAYWRIGHT_BROWSERS_PATH` set. It refuses to attach to an existing display.
Use a dedicated container for qualification so its runtime also cleans up any
application that deliberately starts a new process session. This command exercises
the real headed Chromium sandbox. A failing sandbox remains a qualification
failure; the launcher does not add `--no-sandbox` or weaken kernel policy.

The private bus follows the [D-Bus session model](https://dbus.freedesktop.org/doc/dbus-daemon.1.html),
and native semantics use [AT-SPI](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/index.html).

Ubuntu 24.04 restricts unprivileged user namespaces. The image includes an
application-specific AppArmor profile granting `userns` to each exact installed
Chromium executable, following the pattern already qualified in Computer Lab.
This does not alter host sysctls or the outer Incus profile. The image builder
requires a real Chromium launch with `chromium_sandbox=True` as the unprivileged
worker user before publication. Profile-loading or sandbox failures stop the
build; there is no fallback that disables Chromium's sandbox. See the
[Ubuntu restriction notes](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions).
For containers, the host must support AppArmor stacking with a delegated
container namespace. Incus enables that namespace independently of
`security.nesting`; enabling nested Incus is unnecessary. The
[Incus profile template](https://github.com/lxc/incus/blob/v6.0.4/internal/server/apparmor/instance_lxc.profile.go#L425-L488)
keeps namespace policy access separate from nesting permissions. A host without
this support fails the image build's profile-loading check.

Install the broker after editing the profiles and host capacity settings:

```bash
./scripts/repo-cargo --print-env
# Use the printed CARGO_TARGET_DIR/debug/rkat-computer as the binary argument.
sudo bash infra/install-broker.sh /absolute/path/to/rkat-computer /absolute/path/to/computer.toml --apply
```

The service runs as `meerkat-computer-broker` with Incus administration access.
Treat that account and operator authorization as host administration privileges.
The broker listens on loopback only. It reads a private operator credential from
`/etc/meerkat-computer/operator.env`; the installer creates it once and preserves
it on later runs. Make the same credential available privately to the trusted
controller process using the configured environment variable. Never give it to
the model or put it in a model-facing tool configuration. Use the root README's
attachment flow to issue a narrower control binding for a specific execution.

Reinstalling restarts the broker with the new binary and configuration, then
checks its loopback `/health` response. Installation reports failure if the
restarted service does not become ready within the readiness window.

Run one broker process per Incus pool. The broker locks its state directory;
independent brokers with different state directories are not a distributed
admission controller. Keep one durable copy of broker state and its worker
credentials across restarts. Do not start cloned copies of that state.

## Private GCP host

Copy `gcp.example.toml` to a private operator configuration and select an existing
network and private subnet. That subnet needs Cloud NAT or another outbound route
for Ubuntu, Python, Rust and browser downloads. The helper creates a VM and one
SSH ingress rule; it does not create or modify the network, subnet or NAT.

Every gcloud command includes the configured project, account and zone. The helper
does not log in, read shell profiles or change the active gcloud configuration.
Replace the example project and account with your own values. The account must
already have the required permissions.

```bash
uv run --locked python infra/gcp-host.py plan --config /private/gcp.toml --name mc-host-01
uv run --locked python infra/gcp-host.py create --config /private/gcp.toml --name mc-host-01 --apply
uv run --locked python infra/gcp-host.py status
```

The VM has no external IP and no attached service account. The ingress rule
permits only IAP SSH, with the VM's specific network tag. Inspect pre-existing VPC
firewall policy as part of deployment; this helper cannot make a shared network's
other rules disappear. `max_run_seconds` sets GCE's maximum run duration and STOP
termination action. Stopped guests and stopped hosts retain disk charges.

Use `gcloud compute scp` and `gcloud compute ssh` with the explicit project,
account, saved host name, zone and `--tunnel-through-iap` to transfer the source
archive and run the Linux setup above. For example:

```bash
gcloud --project YOUR_GCP_PROJECT_ID --account operator@example.com compute scp /tmp/meerkat-computer-source.tgz mc-host-01: --zone europe-west4-a --tunnel-through-iap
gcloud --project YOUR_GCP_PROJECT_ID --account operator@example.com compute ssh mc-host-01 --zone europe-west4-a --tunnel-through-iap
```

Forward the loopback broker to the controller using the saved host identity:

```bash
uv run --locked python infra/gcp-host.py tunnel --apply
```

The default local and remote broker ports are both 18760; override them with
`--local-port` and `--broker-port`. Tunnel creation verifies the saved VM resource
ID and ownership label before connecting. No worker or broker firewall rule is
opened. Cloud start, stop and delete are plan-only unless `--apply` is supplied:

```bash
uv run --locked python infra/gcp-host.py stop --apply
uv run --locked python infra/gcp-host.py start --apply
uv run --locked python infra/gcp-host.py delete
```

Deletion removes the exact owned VM and its auto-delete boot disk, then the owned
SSH rule. Back up wanted computers and broker state first. Network and NAT
resources are outside this helper's ownership.

`stop` loses the host's RAM state. For on-demand wake and RAM-preserving idle
suspension, use the [gateway runbook](../docs/operations/host-lifecycle.mdx),
including its exact-instance state file, separate host credential, supervisor
cleanup and uncertain-effect recovery. Its `ensure-running` action starts or
resumes as appropriate, and both it and `suspend` reconcile saved power requests.
Remove any competing scheduled STOP deliberately before adopting that policy; do not treat a GCE
maximum run duration as an autosuspend setting.

### GCP OS Login and container image export

On the qualified Ubuntu/Incus 6 host, exporting an unprivileged container repeatedly
looked up unmapped host UIDs and GIDs through OS Login. This starved the archive
producer even though compression was mostly idle. A bounded negative NSS cache
removed that bottleneck without changing OS Login or the guest ID mappings.

For this observed case, install Ubuntu's `nscd` package and configure its existing
`/etc/nscd.conf` as follows, retaining a backup of the prior configuration:

| Setting | Qualified value |
| --- | --- |
| Enabled caches | `passwd`, `group` only |
| Positive TTL for both | 60 seconds |
| Negative TTL for both | 20 seconds |
| `check-files`, `shared` for both | `yes` |
| `persistent` for both | `no` |
| `max-db-size` for each | 1048576 bytes |
| `threads`, `max-threads`, `reload-count` | 4, 16, 0 |
| Daemon user | Distribution default |

Restart `nscd`, verify repeated mapped-UID/GID lookup times, and verify a fresh IAP
SSH login and sudo access. Keep `nsswitch.conf`, OS Login, SSH policy and
`subuid`/`subgid` unchanged. This is an explicit host optimization, not a guest
requirement. See the [Ubuntu cache configuration reference](https://manpages.ubuntu.com/manpages/noble/man5/nscd.conf.5.html).
The [Google guest agent](https://github.com/GoogleCloudPlatform/guest-agent/blob/main/google_guest_agent/oslogin.go)
also restarts an existing `nscd` service when applying OS Login configuration.

## Runtime behavior and measurements

The broker creates a fresh instance from the pinned image, admits it against the
configured running limit and available host memory, starts it, waits for the Incus
guest agent, injects its worker credential through stdin into private files, then
waits for the worker's protocol-1 readiness response. Incus RUNNING alone does not
mean the browser is ready. Startup commands and readiness have bounded timeouts.

The worker uses `/workspace` for persistent work, `/var/lib/meerkat-computer/state`
for receipts and browser state, and a separate private worker credential file.
Ordinary idle stop/start preserves these disks but does not preserve processes or
promise exact continuation of open pages. A separate explicit full-state fork
copies disk and RAM state for supported same-host Incus VMs; see
[fork requirements and recovery](../docs/guides/forks.mdx). A timeout does not
trigger a blind destructive retry.

`workspace_exec` grants command execution as the guest worker's Unix user. It is
not a security boundary against that user reading or modifying its own worker
state. Containers and VMs isolate different computers, with different underlying
kernel boundaries; neither claim has been live-qualified by unit tests here.
Use only trusted worker images and treat a guest with shell capability as owning
its guest environment. Never mount host credentials or broker state into guests.

For resource samples on the Linux host:

```bash
python3 infra/host-metrics.py --project meerkat-computer
```

This read-only command returns actual host memory, cumulative CPU ticks, load and
Incus guest CPU/memory counters. Take timestamped samples to compute CPU deltas;
do not interpret cumulative CPU time as instantaneous utilization. Guest metrics
may be unavailable in some states and remain null. This tool does not compute
benchmark success, APS, game moves or model token usage.

## Live qualification

The explicit controller below attaches new, run-scoped identities through the
production broker. It verifies browser and native actions against independent
fixture state, credential delivery, identity handoff, stale-grant denial and
workspace/browser-session persistence after a real cold restart. It releases and
stops only its own computers and removes its own synthetic saved credentials;
their disks remain available for inspection.

```bash
uv run --locked python scripts/live_qualification.py \
  --broker-url http://127.0.0.1:18760 --profile desktop \
  --guest-kind virtual-machine --concurrency 2 --repeats 2 \
  --operator-token-file /private/operator.token \
  --report-dir .state/qualification-vm --apply
```

Repeat with a container profile and `--guest-kind container` to qualify that
backend separately. Use a fresh report directory for each run. The token file must
be owned by the invoking user and have mode 0600. Omitting `--apply` prints a plan
without reading the credential or contacting the broker. Isolation probes require
explicit host listeners and positive-control evidence; see the controller's
`--help`. Do not interpret a run with skipped isolation checks as isolation proof.

The separate operator controller covers the real secure-input and assignment UI,
optional preview labels, exact screenshot reuse, broker restart, natural lease
expiry, automatic idle stop and denial when the configured pool is full. Run it
only on an exclusive broker with all existing computers stopped, a running limit
of two, and a dedicated profile whose `idle_stop_seconds` is one:

```bash
uv run --locked python scripts/live_operator_qualification.py \
  --broker-url http://127.0.0.1:18760 --profile operator-check \
  --guest-kind virtual-machine --operator-token-file /private/operator.token \
  --restart-command-file /private/restart-command.json \
  --report-dir .state/qualification-operator --apply
```

The restart file is an operator-controlled JSON array of literal command arguments,
for example `["sudo", "systemctl", "restart", "meerkat-computer"]` when the
controller runs on the broker host. It runs once without a shell; failure is an
unknown outcome and is never automatically retried. Restore the normal pool and
idle settings after the qualification. Reports contain public receipts, counters
and screenshots, with no operator token or synthetic input values. Keep them
private because screenshots can contain application content.

Offline infrastructure contracts are included in `make test`. They cover explicit
cloud identity, private networking arguments, saved ownership/incarnation checks,
non-adoption, archive allowlisting and permission-error handling. For a direct run:

```bash
uv run --locked --group dev python -m unittest discover -s infra/tests -v
```

Incus references used for the adapter contract: [instance creation](https://linuxcontainers.org/incus/docs/main/howto/instances_create/),
[NIC isolation and filtering](https://linuxcontainers.org/incus/docs/main/reference/devices_nic/),
[image publication](https://linuxcontainers.org/incus/docs/main/reference/manpages/incus/publish/),
and [Btrfs storage](https://linuxcontainers.org/incus/docs/main/reference/storage_btrfs/).
