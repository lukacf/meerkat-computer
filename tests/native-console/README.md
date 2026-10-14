# Installed Computer package in native Console

## Released consumer availability

The selected consumer is the official Meta `v0.9.0` release, published on
2026-10-14. Its annotated tag resolves to commit
`b275e0ed4fe61cf4ff43d16691c54efb17812919`, and its Core dependencies are exact
registry versions `=0.9.0`. Core `v0.9.0` resolves to
`a00bafaf1d862fb41112ff1f30312b350ad05246`. The native crate is `meerkat-meta`
and its RPC namespace is `meta/`.

Preparation fetches the canonical `https://github.com/lukacf/meerkat-meta.git`
repository and verifies the tag's exact commit, workspace release version,
exact released Core dependencies and native Apps source files. Build and run
checks retain those pins and verify the Computer candidate, consumer fixture,
Console assets and executable hashes. Unreleased or modified upstream builds and historical receipts cannot qualify
a changed candidate. Locally compiling the exact released source is supported.

This is a Computer-owned consumer acceptance harness. It uses an installed
wheel, the real Computer broker, a real Chromium worker and the stock Console
from exact released Meerkat and Meta source pins. A scripted model chooses real tool calls;
the runtime owns execution, results, identity, UI lineage and conversation state.

The local process backend is a test fixture, not VM isolation. It must not be used
as a deployment backend or given untrusted agent instructions. All services bind
loopback and the runner creates a fresh process group and temporary state.

## Historical inputs

- MobKit: `bc18a84ed05ffc5a2fd09048a879592b1e6cf218`.
- Meerkat: `ac411ddf3eec780a74bdd204ffcf0b96b1ff5547`, pinned by that MobKit source.
- Computer: the wheel and broker built from the candidate under test.
- Node, Rust, `uv`, Git and Chromium installed for Playwright.

These are the already merged, then-unreleased native MCP Apps sources used for
the historical receipt. These pins are no longer accepted for the 0.9.0 candidate. This harness does not
adopt a private HomeCore platform candidate or demonstrate deployed HomeCore
acceptance.

The dated passing receipt in [VERIFICATION.md](../../VERIFICATION.md) applies
only to its recorded Computer candidate and consumer inputs. Run this harness
again for a changed candidate; merged upstream source is not evidence that a
deployed consumer has adopted it.

## Prepare and build explicitly

From the Computer repository:

```sh
make package-check PACKAGE_OUTPUT=.state/packages/native-console
make native-console-prepare
make native-console-build NATIVE_CONSOLE_BUILD_ARGS="--output .state/native-console-build"
```

Preparation fetches exact upstream sources into
`.state/native-console-inputs/meta`, installs its locked Console dependencies and matching Chromium,
and adds only this directory's consumer example. It refuses to overwrite an
existing input directory. For local development, pass
`NATIVE_CONSOLE_PREPARE_ARGS="--source /path/to/meerkat-meta --output /fresh/path"`
to fetch from a local repository that has the pinned release tag.

The explicit build target serializes both Rust builds with two jobs and an
8 GiB process RSS bound. It writes `build.json` with source digests, exact
commands, toolchains and binary hashes. Use its binary paths below. Reuse an
existing warm native cache with `--meta-cargo-home` and `--meta-target` only
when it is not owned by another running build. Coordinate shared resources
before starting. `RUST_LANE_ID` selects the broker build lane and defaults to
`native-console`; explicit Meta cache paths are recorded in the build receipt.

## Run

Return to the Computer repository and supply the actual binary paths:

```sh
make smoke-native-console NATIVE_CONSOLE_ARGS="\
  --wheel .state/packages/native-console/meerkat_computer-0.1.0-py3-none-any.whl \
  --build-receipt .state/native-console-build/build.json \
  --broker-binary /computer-target/debug/examples/native_console_broker \
  --consumer-binary /meta-target/debug/examples/computer_native_acceptance \
  --output .state/native-console-acceptance \
  --poll-seconds 66"
```

The output path must be fresh. The runner installs the wheel into its own virtual
environment using hashed runtime requirements exported from `uv.lock`, verifies
that imports resolve there, and records the complete dependency set. It rejects
source or binary drift from the build receipt and stale wheel contents.
It does not build Rust or inject tool results. Without `--poll-seconds`, the local
smoke holds the model for eight seconds; 66 seconds crosses the native host's
action timeout while both preview widgets keep polling.

The test asserts:

- The single native identity loads and invokes `browser_navigate` and
  `computer_observe` through the native tool catalog and dispatcher.
- Two stock inline widgets render the broker's actual saved screenshot while
  the final native model stream is still held.
- Each widget polls through its original tool invocation, and refresh does not
  wait for the model turn to end.
- Polls do not create model requests, and UI-private preview payloads and the
  app-only tool do not enter later model requests.
- Reload reuses the saved capture without replaying the original operation.
- Releasing the control grant clears both previews.

Before reload, the harness waits on the native identity completion cursor.
Fixture-only `GET` and `POST /__acceptance/completion` routes call the released
owning `PublicIdentityRuntime` view for the fixed `router:main` identity. They
serialize its typed completion cursor and wait outcome without inferring
completion or exposing general runtime RPC methods. All app resolution,
polling and resource reads use the stock Console routes.

The first Meta 0.9.0 run exposed a stack overflow when the unoptimized fixture
forwarded a completion read through the broad runtime RPC dispatcher. That
failed run is retained separately. The narrow completion routes avoid this
harness-path incompatibility; this acceptance does not qualify the broad RPC
dispatcher or change the upstream runtime, its stack size, or its behavior.

The harness records any temporary 403 for the original invocation after turn
completion and waits for native invocation availability before reload. Historical
runs observed such an interval while publication caught up. This check does not
certify that an immediate user reload will render the preview.

`result.json`, artifact inputs, HTTP action timing and a real Console screenshot
are retained in the output directory, including diagnostics on failure. This test does not replace Linux desktop,
private credential-entry, RAM-fork, Incus isolation or deployed-consumer checks.
