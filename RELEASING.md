# Releasing Meerkat Computer

Meerkat Computer is distributed from source and operator-provided artifacts. A release
candidate contains the Rust operator, Python worker/MCP wheel, Python source
distribution, and a complete source archive. The workflows create downloadable
GitHub Actions artifacts. They do not publish to GitHub Releases, crates.io or
PyPI, change repository visibility, provision guests, or deploy consumers.

The current version is an unreleased 0.1.0 candidate. Keep
[CHANGELOG.md](CHANGELOG.md) aligned with the shipped behavior and known limits.
Record a release date only when a release is actually cut; a successful workflow
produces a candidate, not an implicit publication.

## Supported release surfaces

| Surface | Support and evidence |
| --- | --- |
| Broker/operator | Rust binary built for the target recorded in `manifest.json`; CI produces Linux x86_64, local builds can produce macOS arm64. |
| Worker | Linux guests with Chromium; OS accessibility and native apps require the X11/AT-SPI desktop image. |
| MCP adapter | Python 3.11 or newer. CI checks Python 3.11.13 and 3.13.7. One trusted host-issued binding per connection. |
| Inline preview | Bundled MCP App, enabled with `--mcp-apps` after host capability negotiation. Requires separate native Console acceptance. |
| Source installation | Pinned Cargo, Python and UI dependency locks. The source archive includes the operator's embedded assets and builds without `.git`. |

A local process test does not prove VM/container isolation. Passing package tests
does not prove native Console rendering, a HomeCore deployment, or live GCP/Incus
behavior. Keep those receipts distinct in release notes.

## Prerequisites

- Rust 1.94.1 with rustfmt and Clippy, selected by `rust-toolchain.toml`.
- uv 0.10.9, Python 3.11 or newer, Node.js 22, npm, Git and Make.
- A C compiler and platform linker for the operator build.
- Chromium installed by `make worker-browser`; Linux CI uses
  `uv run --locked playwright install --with-deps chromium` to install OS libraries.
- Docker only for the explicit Linux desktop qualification lane.

Use Make and `scripts/repo-cargo` for Rust work. The wrapper owns external
`CARGO_HOME` and `CARGO_TARGET_DIR` paths. Do not assume `target/release` lives in
the checkout. In a shared checkout, use a distinct `RUST_LANE_ID` and follow the
machine's build-capacity coordination policy before starting heavy builds.

The release profile uses `opt-level = 1` for `meerkat-machine-schema`, matching
the [pinned Meerkat workspace's memory mitigation](https://github.com/lukacf/meerkat/blob/a00bafaf1d862fb41112ff1f30312b350ad05246/Cargo.toml).
Cargo ignores profile settings in dependency manifests, so this consuming root
repeats the override. Upstream recorded 8.7 GB peak RSS at optimization level 3
and 6.3 GB at level 1 on x86_64 with four build jobs on 2026-10-02; increasing
codegen units did not reduce that measurement. Those historical results do not
establish this repository's peak memory on another target. Record elapsed time
and peak memory on the actual release runner, and keep its build concurrency
within the granted capacity. Other release packages retain their normal
optimization settings.

## Validate a candidate

Work from the exact commit intended for distribution, with a clean working tree.
Refresh generated UI only if its source changed, using `make ui-build`, and commit
the resulting packaged HTML. `make ui-check` rejects stale generated content.

```sh
uv sync --locked --group dev --group build
make worker-browser
RUST_LANE_ID=release make release-check
```

`release-check` runs Rust checks/tests/Clippy, Python tests/Ruff, the UI bundle
check, docs compilation and link checks, real Chromium tests, the local
broker/worker/MCP stack, and installed-package validation. Default `make test`
stays local and does not require cloud credentials, native apps or Docker.

An extracted complete source archive can also run `make release-check` and
`make package-check` without `.git`. Its package manifest records unversioned
source provenance, with no commit or clean-tree claim. Creating a distributable
release bundle still requires the actual clean Git checkout; an unrelated parent
checkout does not count as the source repository.

`make package-check` is the smaller packaging lane. It:

1. Verifies matching Rust/Python versions, license metadata, an exact build
   backend, an exact Rust toolchain, and immutable Meerkat machine dependency pins.
2. Builds the wheel and sdist twice with fixed timestamps and compares bytes.
3. Rebuilds the wheel from the sdist and requires identical bytes.
4. Checks package resources, source contents, license and bundled UI notices.
5. Installs locked runtime dependencies and the wheel into a fresh environment,
   then runs the installed CLI entrypoints and actual MCP stdio adapter against a
   deterministic authenticated loopback broker.
6. Verifies Apps negotiation, the actual bundled resource, private preview
   metadata, normal image content, legacy-host behavior, and binding-only preview
   arguments. It records source/artifact hashes and rejects a source change
   during validation.

This fixture exercises the shipping adapter. It does not fake or qualify the
native Console host. `scripts/installed_package_probe.py` documents the boundary.

## Qualify the desktop and consumers

Run the manual **Linux desktop qualification** workflow on the same commit, or
use an authorized Linux Docker host:

```sh
make smoke-desktop
make smoke-pdf
make smoke-desktop-stack
make smoke-desktop-admin
```

The desktop lane installs native apps, exercises accessibility and screenshots,
checks the real broker/worker/MCP stack, and checks guest administrator bootstrap.
It is intentionally separate from the fast CI gate because a cold desktop image
and native stack build are heavy. Its workflow has a 45-minute upper bound. It
does not allocate GCP resources or prove Incus lifecycle/fork semantics.

The [native Console acceptance runbook](tests/native-console/README.md) owns the
exact installed-wheel sequence: `make native-console-prepare`, an explicit
`make native-console-build`, then `make smoke-native-console` with the wheel,
two binary paths and matching build receipt. Use its 66-second run for polling
beyond 60 seconds; the default eight-second smoke establishes a narrower claim.
These targets are outside `release-check`. Preparation and evidence paths must
be fresh, and source/binary/wheel drift requires renewed inputs. Native acceptance
is pending until the candidate produces a passing receipt; this local process
fixture does not establish deployed HomeCore or VM isolation.

For a release claiming native inline Console or HomeCore integration, also attach
receipts from the exact adapter wheel and consumer versions being shipped. Follow
[CONSOLE.md](CONSOLE.md) for the negotiated Apps contract and consumer acceptance,
[HOSTS.md](HOSTS.md) for host ownership and lifecycle qualification, and
[FORKS.md](FORKS.md) for full-state fork coverage. Record commit hashes, artifact
hashes, tested target, and known limitations. Earlier receipts for a fixture or a
different package are useful history, not acceptance of a new candidate.

## Build candidate artifacts

After validation, select a new output directory:

```sh
RUST_LANE_ID=release make release-artifacts \
  RELEASE_OUTPUT=.state/releases/candidate-001
```

The command builds and verifies the Python packages again, builds the release
operator, checks its version/help, and writes:

| File | Purpose |
| --- | --- |
| `meerkat_computer-VERSION-py3-none-any.whl` | Installed worker and MCP adapter, including preview HTML and license. |
| `meerkat_computer-VERSION.tar.gz` | Rebuildable Python sdist. |
| `meerkat-computer-VERSION-source.tar.gz` | Complete source, locks, docs, scripts, tests and assets. |
| `rkat-computer-VERSION-TARGET` | Native operator for the recorded target. |
| `manifest.json` | Source commit, dirty state, per-source hashes, tool versions, validation and artifact hashes. |
| `SHA256SUMS` | Checksums for every artifact and the manifest. |

Output directories must be empty. A failed build can leave partial artifacts;
select a new directory for the next attempt. A manifest exists only after all
checks pass. Source archives exclude `.state`, environment files, caches,
`node_modules`, `.git` and build output; symlinks are rejected. Wheel and sdist
byte reproducibility are checked. The native binary is target/toolchain specific;
the release process does not claim byte-identical Rust builds across machines.

The script rejects modified working trees for release bundles. For a development
bundle only, use `RELEASE_ARGS=--allow-dirty`; the manifest records this state.
Distribute only a candidate whose `working_tree_modified` is `false`, whose commit matches
the green CI run, and whose required consumer/desktop receipts are complete.
The manual **Release candidate** workflow first calls the full CI
workflow on its selected ref, then uploads artifacts with read-only repository
permissions. No release or registry publication occurs automatically.

## Install an artifact

Download the artifact from the intended workflow run or trusted operator. In its directory,
check integrity before extracting or installing:

```sh
sha256sum --check SHA256SUMS
# macOS: shasum -a 256 -c SHA256SUMS
```

Check `manifest.json` for the expected commit and target. The wheel works across
supported Python platforms; the operator executable must match the host. Use the
source archive's `uv.lock` to reproduce the tested runtime dependency versions.
For example, after extracting the complete source archive:

```sh
cd meerkat-computer-0.1.0
uv venv .venv --python 3.11
uv export --locked --no-dev --no-emit-project --format requirements-txt \
  > runtime-requirements.txt
uv pip sync --python .venv/bin/python --require-hashes runtime-requirements.txt
uv pip install --python .venv/bin/python --no-deps \
  ../meerkat_computer-0.1.0-py3-none-any.whl
.venv/bin/meerkat-computer-worker --help
.venv/bin/meerkat-computer-mcp --help
```

Install the matching `rkat-computer-VERSION-TARGET` executable into a directory on
the operator's PATH, preserving executable mode. GitHub's artifact download can
normalize file permissions, so use `install -m 755` or `chmod 755`. Alternatively,
run `make build` from the source archive and locate the binary through
`scripts/repo-cargo --print-env`. Installing the wheel alone does not install
Chromium, an OS desktop, native apps, Incus or an operating broker. Follow the
[README](README.md) and [host guide](HOSTS.md) to configure those components.

## Upgrade and rollback

Keep the prior operator, wheel, source archive, lockfiles and consumer config.
Keep broker state and realm authority with their owner. Drain active operations
before switching the operator/worker version, then reissue fresh bindings and
reconnect resumed sessions using the host's normal attachment path. Do not replay
an operation whose effect was uncertain. Do not copy credentials or control
bindings into a release artifact or a new guest image.

Validate configuration and protocol compatibility against the target version
before rollback. Restore the prior binary/wheel/config as a unit. A release
rollback is not permission to overwrite current broker state or recover an old
grant. See [AUTH.md](AUTH.md) and [CREDENTIALS.md](CREDENTIALS.md) for fencing,
host authority and credential delivery.

## Change versions and dependencies

Update `Cargo.toml` and `pyproject.toml` together. Refresh `Cargo.lock` through
`scripts/repo-cargo` and `uv.lock` with uv, then rerun package validation. The
checker rejects version drift. Meerkat DSL/schema dependencies use full Git
commit pins; review the intended upstream compatibility instead of following a
branch or silently rebuilding the runtime.

UI dependencies are pinned by `ui/mcp-app/package-lock.json`; documentation
dependencies are pinned by `docs/package-lock.json`. Run `make ui-build` after a
UI dependency/source change and commit its generated HTML. Desktop apt packages
and the Ubuntu base image are resolved when building that image. Record its
immutable fingerprint and smoke evidence; do not describe an image rebuild from
mutable OS repositories as byte reproducible.

## CI ownership and failures

CI runs for pull requests and pushes to `main`, avoiding duplicate branch/PR
builds for the same change. The `gate` job requires Rust/stack, both Python
versions, and browser/docs lanes to succeed. Cancellation, skipping and failure do not count as green. Cargo
caches use the wrapper's actual external paths, with lock/toolchain/commit keys;
there is no `target/` cache disconnected from the build's output directory.

While the repository is private, jobs use private Linux x86_64 runners; there
is no paid GitHub-hosted fallback. An authorized private runner can run the same
lanes without GitHub-hosted execution minutes. Set repository variable
`MEERKAT_COMPUTER_RUNNER` to a JSON label array, for example
`["self-hosted", "linux", "x64", "meerkat-computer"]`, only after its registered
runner is online. Optionally set `MEERKAT_COMPUTER_RUST_RUNNER` to a more specific
label array for Rust and release jobs, so they reuse one runner's warm cache.
For a public repository, every lane selects `ubuntu-24.04` regardless of these
variables. This includes the aggregate gate and manual desktop workflow.
The desktop runner also needs Docker. Runner selection does not
change the required commands or let a skipped job count as successful.

For private validation, use dedicated runner users and work directories.
To park temporary capacity, finish the important PR/main/candidate runs and
confirm no jobs remain before stopping its VM. Retain the private runner labels;
clearing routing variables still selects private runners and never enables paid
hosted execution. Start the VM and verify runner health before dispatching new
work. A queued job on a parked runner is unavailable capacity, not a passing gate.

An account spending restriction or a workflow that never starts is not a passing
run. Restore the configured capacity or use an authorized private runner; do not
weaken the gate or publish from a skipped run. A targeted rerun is appropriate for an independently
diagnosed transient infrastructure failure. Source or test changes require a new
commit and validation of that commit.

## Before making the repository public

The current integration targets released Core/SDK 0.9.0 and Meta 0.9.0.
Publication requires the exact candidate to pass the native Console and
released-runtime model exercises above.
Do not substitute an unreleased consumer or reuse a historical receipt.

Audit the complete proposed exposure: tracked files, reachable Git history,
remote branches and tags, screenshots, fixtures, releases, Actions logs and
artifacts. Removing an identifier from the current tree does not remove it from
history or old artifacts. Retain any needed private evidence outside public
refs and assets, and resolve the exposure before changing visibility. If history
must be rewritten, regenerate commit provenance and artifact checksums and
validate the resulting revision again.

Remove this repository's persistent self-hosted runner registrations and routing
variables before changing visibility. Also confirm that no organization runner
group grants it access to trusted runners. A workflow's `runs-on` condition
alone is insufficient: an untrusted pull request can edit its workflow to name
an available runner. The public repository must have no access to those runners.
Finish private validation jobs first, preserve private evidence, and park the
temporary GCP capacity once no other authorized work uses it.

After visibility changes, verify anonymous repository access, an authenticated
public-runner configuration check, a fresh public CI run, and the documented
source-install path without private credentials. This procedure does not
publish packages to registries or deploy or upgrade HomeCore.
