# Verification

This is a dated evidence ledger, not a claim that every historical test was rerun
for every commit. The [release procedure](RELEASING.md) defines the candidate gate;
the [compatibility guide](docs/reference/compatibility.mdx) defines supported scope.
Paths under `.state/` name private operator receipts and are intentionally absent
from distributed archives. Historical PR and CI links below refer to the private
development archive and require access to that repository. Public source starts
with a new Git history; those private commit IDs are historical evidence only.

## Released Core, SDK and Meta 0.9 integration, 2026-10-14

This candidate uses published Core `v0.9.0` at
`a00bafaf1d862fb41112ff1f30312b350ad05246`, PyPI `meerkat-sdk==0.9.0`,
and published Meta `v0.9.0` at `b275e0ed4fe61cf4ff43d16691c54efb17812919`.
The lifecycle machine DSL/schema and locks use the released Core revision.
Version-1 persisted snapshots, recovery, fencing and unknown outcomes passed
across the dependency update. No private upstream build is substituted.

The native consumer now uses the released `meerkat-meta` crate and identity
APIs. Its test-only completion endpoint calls the owning identity runtime;
stock Meta continues to own MCP Apps routing. A broad test-only RPC forwarding
route was removed after it overflowed the stack on a completion query. The
passing fixture qualifies the typed identity route, not that broad dispatcher.

The local release check passed 179 Rust tests, 880 offline Python/infrastructure
tests, 96 real Chromium tests and all three broker/worker/MCP stack cases. Four
Linux-only browser shutdown cases were skipped on macOS. Check, lint, generated
UI, documentation/link checks and reproducible installed-package validation
passed. The final offline Python/infrastructure run passed 881 tests after
adding a regression for restrictive source permissions in the Linux stack
harness; final CI includes that additional case. A test-only readiness
fixture uses its existing five-second allowance so preparatory subprocesses can
reach the manager probe under concurrent testing. Every readiness safety
assertion remains in place; all Rust tests then passed at default concurrency.

### Installed native Console

Fresh acceptance used the installed Computer wheel with SHA-256
`cf05bca2c25fa54350d5efad95f81308399767174af096d18bfdb27285f1ed53` and binaries
built against the released versions above. Both inline previews remained live
for 67.118 seconds with 67 completed polls; the slowest poll took 23 ms. First
render took 839 ms and refresh took 190 ms. Reload did not replay tool actions,
revocation cleared the previews, and private preview data stayed out of model
context. Cleanup independently verified both listeners closed, runtime state
removed and no owned processes remaining.

The acceptance broker SHA-256 is
`b87dcd8ae84d1052629a6fe1008f5c0af4ca75931bdad684a0f0eaff66913782`; the
consumer SHA-256 is
`7b6fd75ac3ad69afb4cfd6af0f20d99e658cbd41b200b4bcd98693125b60005f`.
All 55 broker source inputs were compared with the clean integration commit
`6ab7839c2d1c7fc9dcaff2755c710b9075ce8d99`. The clean package check reproduced
the same wheel bytes. Choices are scripted; execution, tool dispatch, Console
rendering and polling are real. This is a local process fixture, not a deployed
HomeCore or VM isolation claim.

The current evidence is retained privately under
`.state/release-meta-09-20261014/`, including native `acceptance-final/` and
`build-renewal-final/`. Historical results below remain scoped to their dated
inputs. The public source repository starts with a fresh root; its candidate
manifest records the public commit and artifact checksums. The original Git,
PR and private CI history is preserved in a separate private repository.

### Linux desktop and guest qualification

The native suite passed GTK/MCP interaction, Inkscape, GIMP, LibreOffice and
Blender Python editing, with screenshots inspected. The PDF suite independently
verified Evince's page field and blue-to-orange rendered-page transition. The
real Linux broker stack passed browser/desktop/workspace/exec, preview reuse,
identity handoff, credential rotation and denial, duplicate suppression, optional
save and reconnect. The admin suite verified UID 10002 could use sudo as UID 0,
then revoke access without damaging the worker credential.

| Lane | Immutable image SHA-256 | Command time |
| --- | --- | --- |
| Native desktop | `30e75ae25940a73ecee2ae3a885f5314ed50cdac02d40db16f52c093e26dc73a` | 318.4 s |
| PDF viewer | `c35eeb784aa93cdb6fcd731a546394812390d4f8967c199711d912f5bd172e54` | 36.1 s |
| Desktop stack | `9bf5f7298cd069b88c39b3a10879394277d0210548047b1c1b75520475dc1e16` | 12.2 s for final run |
| Guest administration | `5230e3005ec83f55dc1b2efb0fbb38164f24765a3b5f794e4994237ea3ef1937` | 12.7 s |

Native and PDF ran at `29c0955`; stack and admin ran at `396a2243`.
The retained mapping verifies all 36 native/PDF behavior inputs remained
identical. One copied but unused stack Dockerfile changed; neither native nor
PDF executes that file. The stack used a previously compiled image with the
current source overlay, not a fresh build of the amended Dockerfile.

Initial stack attempts exposed two harness defects: restrictive host modes
survived `docker cp`, and two Python smoke helpers were missing from the image
inputs. TDD regressions now check readable staged copies without changing host
permissions, and derive required helpers from the actual Rust test invocations
to check build context, Dockerfile COPY and overlay completeness. Failures are
retained separately from the final passing receipts. No production authority,
isolation rule or safety assertion was weakened.

### Real released-runtime exercise

Astra (`gpt-6-astra`, medium reasoning) used the exact installed candidate wheel,
official `rkat-rpc` 0.9.0 and PyPI SDK 0.9.0 to drive a fresh Docker guest. It imported
the full 7,370,286-byte *Sparks of Artificial General Intelligence* v5 PDF,
inspected physical page 17 in native Evince, used Poppler for precise text,
inspected the browser and exported a 2,301-byte Markdown summary. Independent
comparison confirmed all five summary points and Evince's `17 of 155` indicator.
The host verified export SHA-256
`b5103fd0a8a5cd23ccda63b6445841d9d1d8c5dfb895dc581b829d758c3413b4`.

The task took 139.20 seconds, with 26 turns, 25 runtime tool calls and 21 Computer
MCP calls (18 successful, three rejected). Usage was 306,981 input tokens,
including 266,243 cache-read tokens, and 1,981 output tokens. Four UI commands
succeeded, including three clicks; two inputs were rejected before effect.
One task attempt succeeded (APS 1). Sampled peak guest memory was 494.8 MiB;
this excludes model-provider memory. The rejected URL scheme, unsupported key
spelling and click outside the focused visual target are preserved in the trace,
as is a separate nonzero guest shell command. This is a functional sample,
not a model comparison or latency/capacity distribution.

The actual checked-in `examples/run_agent.py` then ran on a new runtime
generation with the released SDK/runtime and exited zero in 17.24 seconds:
four turns, three tool calls, 5,207 input tokens and 111 output tokens. The old
primary grant returned 401 after release. The owned guest was removed afterward;
the broker exited zero and both listeners closed.
Source hashes, HEAD and clean status were identical before and after both runs.

The live guest reused the recorded PDF base image with the exact candidate wheel
installed. AT-SPI was enabled; `GTK_OVERLAY_SCROLLING` was unset in that session,
so this live run does not independently qualify the launcher's scrolling default.

The public SDK's post-task `read_session_history` still rejects the runtime's
`open_ai_assistant_message` metadata. The private driver records that error
separately from completed task/export verification; raw diagnostic RPC is not
claimed as public SDK history compatibility. Both run results reported
`terminal_cause_kind=null`. No upstream patch or replacement runtime was used.
This exact-wheel exercise does not deploy HomeCore or establish Incus isolation.
Private receipts, source comparison, screenshots and cleanup are under `live/`
in the evidence root above.


## File transfers and PDF tooling, 2026-10-13

The file-transfer implementation adds private operator staging, separate import
and export capabilities, verified binary transport and browser file inputs.
Tests were written before the worker, broker and host-client implementations.
Further regressions caught computer ID normalization, staged-blob cleanup after
rejected imports, blocking special-file reads and incorrect effect classification
after a host storage mutation.

| Local check | Result |
| --- | --- |
| Rust | 170 tests in the full run, followed by the final 14-case transfer suite including one added FIFO regression; 171 distinct default cases passed |
| Operator CLI | Binary staging, verified private output, overwrite refusal, checksum failure, symlink/FIFO refusal and loopback-only connection checks passed |
| Offline Python and infrastructure | 792 passed, including host-client transfers, desktop package selection and 33 checked-state event regressions |
| Real Chromium | 96 passed; four existing Linux-only shutdown cases skipped on macOS |
| Broker/worker/MCP | Both existing stack cases and the new binary transfer stack passed |
| Linux PDF viewer | `make smoke-pdf` passed: Evince discovery and launch, 166 native controls, two rendered pages, keyboard navigation and Poppler inspection |
| Native desktop regression | Existing suite passed in 103.956 seconds on the same image: GTK actions, MCP surface switching, installed apps, Blender Python and lease fencing |
| Formatting and lint | Rust formatting, all-target Clippy with warnings denied and Python Ruff passed |
| Package check | Repeated wheel/sdist builds, wheel rebuilt from sdist and installed-package probes passed |

The transfer stack carries 3,145,728 bytes containing every byte value from the
trusted Python host client through operator HTTP staging and a metadata-only MCP
import into the guest workspace. Chromium uploads the file to a local test site,
downloads its copy, and reports the completed guest path. An explicit MCP export
and verified host-client download return the original bytes and SHA-256. The
test checks transfer counters, confirms that a grant token cannot use operator
file routes, and deletes the staged artifacts. Its final run took 3.00 seconds;
this is one functional test, not a throughput benchmark.

The stack uses local worker processes and a synthetic website. It does not
establish VM isolation, deploy HomeCore, use a model provider or qualify a new
native Console build. Existing guest images need rebuilding or upgrading to
receive the newly added PDF packages. The separate `make smoke-pdf` check uses a
disposable Docker desktop. Independent display-pixel assertions and saved
screenshots verified the blue first page and orange second page after a real
`computer_act` keyboard command. `pdfinfo`, `pdftotext` and `pdftoppm` ran through
`workspace_exec`. The launch returned an uncertain post-action observation;
the test observed the existing viewer successfully without replaying the launch.
The PDF case qualifies opening and navigation, not window-close behavior.

The viewer test exposed duplicate `checked` notifications emitted while reading
Evince menus. A focused event-cache regression first reproduced the resulting
capture starvation. The repair ignores exact duplicate values only. First
notifications, real transitions, other state events, lifecycle discontinuities
and cache eviction still invalidate observations. Stable native state queries
discard contradictory cached values; stale input rejection remains covered.

PDF and native receipts, package versions, screenshots and the immutable image
ID are retained under `.state/qualification-pdf-20261013/`. Both suites used the
same Linux arm64 Docker image. The full desktop bundle was exercised; minimal
and headless package selection was checked offline. Temporary test containers
were removed after evidence capture.

### Live PDF agent exercise, 2026-10-13

A native Meerkat 0.8.51 agent using `gpt-6-astra` completed a summary of page 17
of *Sparks of Artificial General Intelligence*, arXiv v5, in 100.275 seconds.
The agent imported the complete 7,370,286-byte paper, opened Evince at page 17,
used Poppler for precise text, observed the rendered page and exported a
1,780-byte summary. Independent source comparison verified all five summary
points and the visible page number; the host verified the exported checksum.
The path used native stdio MCP, the real broker and production worker in an
isolated local Docker desktop, with no operator UI intervention during the run.

The run used 23 model turns and 22 native tool calls, including three catalog
calls. The 19 Computer calls included seven shell calls, seven observations
and one rejected keyboard action; there were no clicks. Input usage was 207,834
tokens including 185,742 cache-read tokens, with 1,838 output tokens. Peak sampled
guest memory was 756.3 MiB. One task attempt succeeded; this is a functional
sample, not a latency distribution, capacity estimate or model comparison.

**Open finding:** native accessibility did not work for this full document.
Four observations failed to stabilize. The agent recovered by reopening Evince
with `NO_AT_BRIDGE=1`, then used screenshots and Poppler. The successful semantic
tree did not contain Evince. A stale keyboard action was rejected with effect
`none`. The earlier two-page smoke remains valid for its limited fixture but
does not qualify full-document PDF accessibility. The cause is unproven; no
production repair was made during this exercise.

Private evidence is retained under `.state/pdf-agent-20261013/`, including
`REPORT.md`, `result.json`, tool receipts, a verified exported summary and the
rendered page screenshot. The model run duration excludes setup troubleshooting.
Resource samples cover the guest only, not the host runtime or remote inference.
The separate streaming event file is empty due to a driver filter mismatch;
the native history and transparent tool recorder retain the actual call trace.

### Unstable accessibility repair, 2026-10-13

A follow-up trace reproduced eight failed full-paper captures. Evince emitted
checked-state notifications during child traversal that its later stable state
queries contradicted. The cache correctly discarded those contradictory values;
repeated traversal then invalidated every capture. The failure was not caused
by the initially suspected child-event cache-clear cascade.

Hybrid mode now returns a fresh screenshot after three unsuccessful semantic
attempts, with an empty tree, no refs and an explicit `unstable_tree` issue.
Fallback input still checks the focused visual target, display and accessibility
event state. Missing or unstable visual fences are marked `observe_only`.
Semantic-only mode remains unchanged. The broker maps no-effect unstable
captures to `conflict`, preserving uncertain-effect behavior.

TDD reproduced the failures before implementation. The final offline suite
passed 820 Python/infrastructure cases, including 28 new fallback cases. Three
new Rust worker-error cases passed, along with Python lint, Rust formatting,
all-target Clippy and documentation checks. The real native desktop suite passed
in 108.15 seconds. On the same normally configured Evince process, the old driver
failed and the new driver returned screenshots, completed PageDown from page 17
to 18, and rejected reuse of the consumed observation.

A fresh native Astra run completed the identical summary task in 93.335 seconds,
with 19 model turns and 18 native tool calls. All seven screenshot-producing
calls succeeded without disabling accessibility. The three successful UI actions
were clicks; two keyboard actions were rejected as stale, after which the agent
used its permitted guest shell. The exported 1,681-byte summary and visible page
17 were independently checked. Input usage was 146,476 tokens including 130,303
cache-read tokens; output usage was 1,483 tokens. Peak sampled guest memory was
425.9 MiB. This is one successful attempt, not a performance distribution.

Evince semantics remain partial for this document; the repair qualifies usable
screenshots and preserved input checks, not a reliable full-document semantic
tree. Private evidence is under `.state/pdf-repair-20261013/`, including
`REPORT.md`, source manifests, event traces, tool receipts and cleanup receipts.
No deployed guest, HomeCore or upstream runtime was changed.

### Native input freshness diagnostics, 2026-10-13

A controlled full-paper Evince test reproduced a rejected `f` key with the
latest observation ID. Only the focused window's RGB hash changed: 206 pixels
at the right-edge scrollbar, bounding box `[1275,113,1280,155]`. Focus, display,
the 182-window X11 topology and both accessibility revisions stayed unchanged.
The pointer stayed fixed between the returned observation and validation.
This proves that scrollbar fading alone can reject keyboard input. It matches
the earlier screenshot difference, but the two original failed calls did not
retain validation captures, so their individual causes remain unproven.

The provided guest session now sets `GTK_OVERLAY_SCROLLING=0`. Freshness checks
retain their previous rejection conditions. The change avoids supported GTK
overlay animations; it does not make arbitrary animated content stable or
establish a reliable Evince accessibility tree for this paper.

A settled non-overlay Evince test at page 17 and 100% zoom passed delayed key
validation with unchanged pixels and fences. An actual `f` action after 3.2
seconds was acknowledged and visibly changed the zoom to fit-page. This test
omitted the repeated pre-capture pointer stimulus used in the failing test, so
it qualifies settled operation, not a matched A/B proof. An earlier attempt
with Chromium in the foreground is excluded from the qualification.

Pre-admission input rejections now carry bounded reason counters and an atomic
durable receipt. Duplicate requests and worker restarts do not increment them
again. Rejection deltas cannot inherit a previous action's clicks. Existing
dispatch, acknowledgement and uncertain-effect meanings remain unchanged.
The Python host client preserves the allowed counters, and native `stale_ref`
errors map to the broker's existing `conflict` classification.

TDD recorded failures before implementation for 33 new Python/infrastructure
cases. The final offline suite passed 853 cases; four Rust HTTP error/metric
tests passed. The full Linux native/MCP suite passed in 125.553 seconds,
including native keyboard input, installed apps and Blender's Python console.
The PDF worker suite opened Evince and verified the second rendered page after
keyboard navigation. Python lint, Rust formatting and documentation checks
passed. These are tool-level checks, not a new model benchmark.

Private screenshots, numeric fence traces, source manifests, qualification
exclusions and cleanup evidence are retained in `.state/pdf-input-20261013/`.
The changes are local; deployed guests need the updated worker/session launcher
and a desktop restart. No HomeCore or upstream runtime was changed.

## Private release preparation, 2026-10-12

[Private archive PR 3](https://github.com/lukacf/meerkat-computer-private-history/pull/3) tracks the final source
revision, CI runs, private artifact build and renewed installed native Console
qualification for this cleanup. The release work adds repository-wide Markdown
validation, honest source-archive provenance, and current onboarding and gateway
references. It also repairs a recurring Linux fixture failure: temporary Python
bodies are read by a stable launcher, with deterministic held-writer regressions;
production command execution is unchanged. Use the PR's final evidence and the
candidate manifest together, rather than treating older receipts as qualification
of a new build.

## Host wake, RAM retention and HomeCore adoption, 2026-10-11 to 2026-10-12

The gateway shipped in [private archive PR 2](https://github.com/lukacf/meerkat-computer-private-history/pull/2),
merge `38ca3410a5da0d225e48ec8df983fc0393921cc7`. The deployed gateway was built
from `12e2c4db00c700d5ef4b176931c72acbb470c85d`, with the same source tree.
[Private PR CI](https://github.com/lukacf/meerkat-computer-private-history/actions/runs/38135254594) and
[private main CI](https://github.com/lukacf/meerkat-computer-private-history/actions/runs/38135762179)
passed all required lanes. Local Python/infra validation passed 590 tests; CI
included 151 Rust tests and two explicit broker/worker/MCP stack cases.

| Live check | Observed result |
| --- | --- |
| Automatic GCP suspension, October 11 | Passed with a temporary 60-second policy, then twice with the final 300-second policy |
| RAM retention across suspend/resume | A normal shell request completed in 23.002 seconds; a RAM-only nonce, process ID and guest boot ID matched |
| Existing guest continuity | Same assignment, guest incarnation, journal and browser profile; browser observation and navigation passed after resume |
| Gateway shutdown | Real SIGTERM released its owned tunnel and listening ports |
| Deployed HomeCore, October 12 | Activation 259, source `24be1daff41972275bd80c7c2a7139ce7c00a1d2`, with gateway integration from HomeCore PR 500 |
| Deployed idle suspension | Provider reported SUSPENDED at 12:24:07.733 UTC after the five-minute idle policy |
| Normal Console request from suspended state | Discovery navigated to example.com and observed successfully; the complete agent turn displayed 52 seconds |
| Binding continuity after wake | Same computer, lease epoch 13 and runtime generation 18; gateway settled awake with zero active operations and no inhibit |

The 52-second measurement includes model and host wake time; it is not a pure
provider-resume benchmark. Worker durations were 440 ms for navigation and 132 ms
for observation. One production turn does not establish a latency distribution
or fleet capacity.

Initial deployment exposed a disabled broker boot service and a guest worker
with an older observation schema. Both were repaired before qualification while
preserving the existing guest. The first HomeCore wake attempt on October 11
returned `host_unavailable` with effect `none`; no attach/action was forwarded.
Its exact cause remains unproven because provider child diagnostics were not
retained. The October 12 test followed trusted state reconciliation. These
successful runs do not establish automatic recovery from every failed wake.

The main CI browser lane initially hit Ubuntu's unattended-upgrade dpkg lock. A
single rerun passed after the lock was confirmed released. This was dependency
installation contention, not a suppressed test failure.

**Consumer boundary:** the inspected HomeCore build used Core SDK 0.8.51 and
MobKit 0.8.47. Tool results and timestamps rendered, but screenshot content showed
`[image: image/png]`. Native inline Apps were disabled and remain unqualified in
that deployment. [Core PR 1843](https://github.com/lukacf/meerkat/pull/1843) and
[Meta PR 634](https://github.com/lukacf/meerkat-mobkit/pull/634) are merged; their
release/adoption and HomeCore's native MCP, sandbox, authorization and audit wiring
remain separate work. The native post-completion 403 interval documented below
also remains a known upstream limitation.

Private receipts are retained under `.state/wake-on-use-20261011/` and
`.state/homecore-acceptance-20261012/`, including `RESULTS.md`,
`ram-resume-verification.json`, `auto-suspend.json`, `final-idle-state.json`,
`deployment-and-first-test.json`, `idle-suspension.json` and `console-wake-test.json`.
The tested gateway runs on an always-on trusted controller outside the GCP host.
Its private route to HomeCore is an operator-managed prerequisite, not a hosted
service provided by this package.

## Release candidate qualification, 2026-10-10

This candidate adds bounded browser and native accessibility observations,
freshness and private-input regressions, malformed MCP reply handling, repeatable
private packaging, and a 29-page Mintlify documentation site. Compact observation
defaults are 60 elements and 6,000 application text characters; full detail and
native subtree expansion remain bounded by the configured profile ceilings.
These are character and element limits, not measurements of billed model tokens.

| 2026-10-10 local lane | Result |
| --- | --- |
| Rust checks and tests | 124 passed; two explicit stack cases tested separately |
| Rust formatting and all-target Clippy | Passed |
| Offline Python | 448 passed; 91 browser cases deselected |
| Real Chromium | 87 passed; four Linux-only cases skipped |
| Real broker/worker/MCP stack | Both identity/preview and private-credential cases passed |
| Linux desktop | Current desktop source passed; 76 recorded events, including native subtree action and Blender Python verification |
| Documentation | 29 pages and 118 links/assets validated; desktop/mobile visual review passed |
| Packaged UI | Generated bundle freshness passed |
| Installed native Console | Two views; 67 polls over 66.899 seconds; maximum poll latency 20 ms |

The local lane receipts are retained under `.state/release-ready-20261010`.
Linux desktop evidence is in
`.state/release-observations-20261010/linux-current/report.json`; docs visual
evidence is in `.state/docs-qa/receipt.json`. The Linux run used current desktop
code; the later browser handle cleanup fix is covered by the final Chromium lane.
The Rust gate's measured peak was 3,947,600 KiB across the initial compile and test
run; final lint/stack took 9.42 seconds with a 1,920,192 KiB peak. Builds used two
jobs and existing isolated caches. No GCP instance was started for these checks.

The installed native Console harness is separate from the deterministic MCP
package probe and from the historical generic upstream fixture. It uses a
fresh wheel installation, the real broker and Chromium worker, stock native
Console assets and the original MCP connection. Its scripted model chooses tool
calls; native Meerkat owns execution, results, identities and replay. It does not
measure model quality, establish VM isolation or deploy HomeCore.

The selected native consumer is MobKit
`bc18a84ed05ffc5a2fd09048a879592b1e6cf218`, with Core
`ac411ddf3eec780a74bdd204ffcf0b96b1ff5547`. The Computer broker's machine protocol
dependency remains independently pinned to Core
`c380a1955e2d65cbb6e9de76cfa25fb4397ef94b`.

Early native harness attempts failed and remain recorded. The test initially
used streamed final text as its completion barrier; original widget resolution
could still be unavailable at that point. The final harness waits for native
completion and for both original invocations to resolve before reloading.
The pinned Console RPC facade does not dispatch the completion methods, so a
fixture-only allowlisted route forwards those reads to the real native RPC
dispatcher. All preview requests still use the stock Console routes. Test cleanup
also preserves primary failures and reports an unsettled private directory rather
than allowing a cleanup race to hide the cause.

The final installed receipt is
`.state/native-console-acceptance-20261010-release/result.json`. First render took
846 ms and manual refresh took 166 ms. Both views completed fresh polls after 60
seconds while the native model stream remained held. Native completion advanced
from turn zero to one in the same epoch. Reopening the Console reused both
original invocations and the same saved observation; the later model request
contained no UI-private preview data. Revoking the grant cleared both images.
Orderly broker-owned worker shutdown completed, all fixture services exited, and
the runtime directory was absent after cleanup. The wheel SHA256 is
`d0d234a9e7e7da3ef7e4555a665311fbd76c7493bbb77672c8686a1bee4bf7c6`.

One upstream timing limitation remains explicit: original invocation resolution
can briefly return 403 after native completion while committed lineage becomes
available. The harness records this response and waits for both original resolves
before testing reload. A user reloading in that interval can see a fallback view.
This was reported to the native release coordinator; the passing receipt is an
eventual availability check, not a claim of zero transient native errors.

Deployed HomeCore native Apps acceptance remains a separate adoption milestone.
Historical pilot and live infrastructure receipts below retain their original
scope and versions. See [RELEASING.md](RELEASING.md) for clean artifact and CI
requirements, and [the native runbook](tests/native-console/README.md) to reproduce
the installed consumer test.

## Live HomeCore Discovery pilot, 2026-10-09

The deployed Discovery agent used browser, workspace, guest-admin and native
accessibility tools, then reconnected after HomeCore activation 241 was replaced
by 242. Independent broker evidence verified the same computer and file, old
grant revocation and a new lease at the new runtime generation. All 20 supplied
computer receipts completed, with counters and known token usage independently
audited. The first smoke task needed a corrective turn after compaction lost its
request; that failure remains in APS. Actual Console inspection found tool cards
and timestamps but text placeholders instead of screenshot pixels.

[HOMECORE-PILOT.md](HOMECORE-PILOT.md) records the outcomes, measurements,
deployment scope, remaining image-rendering and observation-size issues, and
temporary hosting limits. This is an Astra integration pilot, not a new model
comparison or maximum-capacity benchmark.

## Host-side Python client, 2026-10-09

The synchronous `ComputerClient` provides explicit attach, renew, release,
tool discovery and tool calls for application-owned callback bridges. It does
not add a second identity policy or renewal scheduler. [HOSTS.md](HOSTS.md)
describes the contract and the distinction from native MCP Apps integration.

TDD first failed on the absent client, then reproduced malformed broker errors,
unsafe numeric metrics, missing workspace counters and invalid UTF-8 inputs.
The final focused suite passed 71 cases. The full offline Python lane passed
342 tests with 48 browser cases deselected in 6.30 seconds; repository-wide Ruff
and diff whitespace checks passed. The full lane output is retained at
`.state/homecore-discovery-20261009/test-python.log`.

An isolated compatibility run also passed all 71 client cases against HomeCore's
locked MCP 1.27.1 and httpx 0.28.1 in 0.59 seconds. Its output is retained at
`.state/homecore-discovery-20261009/client-mcp-1.27.1.log`. This covers the
standalone client module, not installation of the full worker package or native
MCP Apps support on those older dependencies.

Independent source review covered grant binding, credential separation, unknown
effects, content/receipt conversion and explicit cleanup. Its workspace-counter
finding was fixed and covered in success and uncertain-effect receipts. These
tests use a deterministic HTTP transport and do not prove a deployed HomeCore
session or native Console integration. The unchanged Rust and browser paths
were not rerun for this Python client addition.

## Realm configuration and inline MCP App, 2026-10-09

The Console integration now consists of ordinary Meerkat realm configuration and
an optional inline MCP App. The earlier custom sidebar module, product-service
helper and their tests were removed because that developer extensibility is not
a plugin surface. The retained adapter uses standard MCP Apps metadata, the
official JavaScript App SDK and the original grant-bound Python connection.
The Python SDK floor is 1.30, matching the resource metadata and client capability
API exercised here.

TDD for the retained implementation first reproduced missing-route/adapter
behavior, then privacy and lifecycle regressions. In particular, a late
initialization response could restart a closed view. The regression was
reproduced before its fix and remains covered.

| Check after removal | Result |
| --- | --- |
| All offline Python tests | 271 passed, 48 browser tests deselected |
| Chromium inline app and existing operator preview tests | 9 passed |
| Repository Python Ruff | Passed |
| Bundled UI freshness and wheel package inclusion | Passed |
| Read-only dependency review | No retained implementation uses the removed service or panel |

The offline suite completed in 8.34 seconds; its output is retained at
`.state/console-integration/realm-only-python.log`. The nine browser cases
completed in 10.76 seconds, recorded in `realm-only-browser.log` in the same
directory. The inline fixture uses a sandboxed opaque-origin iframe, the actual
bundled SDK and a deterministic Apps protocol host. It covers current-preview
reads, optional labels, no network access, cursor reuse, withholding, malformed
images, changed computer IDs, teardown and late initialization.

The rebuilt wheel in `.state/console-integration/realm-only-dist` contains
byte-exact copies of the adapter, support module and bundled HTML with
third-party notices. The removed product-service helper is absent. The unchanged
Rust core, credential broker and preview code previously passed 51 targeted tests,
formatting and all-target Clippy at `e3227e5`; those lanes were not rerun for this
Python deletion and documentation correction.

Static inspection verified the realm config schema and raw-head patch semantics.
It did not establish that realm persistence alone starts the adapter in the
ordinary CLI: the current discovery/build paths still require explicit server
wiring. [CONSOLE.md](CONSOLE.md) documents that boundary and the existing trusted
per-session attachment path.

No model inference, cloud restart, guest activity or upstream source edits were
needed. These checks do not establish a deployed Console/HomeCore integration
or replace native MCP Apps admission tests in an Apps-capable upstream build.

## Full-state fork qualification, 2026-10-09

The full-state fork implementation adds an explicit trusted-host broker API and
CLI. It copies a running Incus VM's RAM, CPU/device state and writable disks,
then assigns independent control authority and networking to each child. One
multi-target request creates every council computer from the same checkpoint.
Meerkat and MobKit agent orchestration remains owned upstream; an embedding host
must invoke this API and attach each child before starting its agent. See
[FORKS.md](FORKS.md) for the integration contract and recovery behavior.

| Local check | Result |
| --- | --- |
| Rust test suite | 118 passed; two opt-in stack cases excluded |
| Offline Python suite | 229 passed; 43 browser cases deselected |
| Real Chromium suite | 39 passed; four Linux-only cases skipped on macOS |
| Formatting and lint | Rust formatting, Clippy and repository-wide Ruff passed |
| Independent source review | No material findings in fork authority, worker rebind, shutdown or network-verifier changes |

The Rust suite and lint logs are retained in
`.state/full-state-fork-20261009/tests-final-v6.log` and
`lint-final-v6.log`. The final Python changes passed in
`tests-python-final-v9.log` and `ruff-final-v9.log`. The 39-case Chromium result
was observed in the tool transcript; it has no separate saved log and must not
be confused with the earlier 36-case smoke result below.

The final GCP run passed all 45 qualification checks through the production
broker. The parent and both children retained their live application state,
then independently changed browser memory, editor buffers, Blender objects and
writable files. All 12 directed guest-root peer connection attempts were denied,
with successful local-listener controls on every branch. Old child worker tokens
and superseded grants were denied. All three guests stopped through the broker
without operator intervention.

| Live measurement | Result |
| --- | --- |
| One checkpoint to both children ready | 81.252 seconds |
| Qualification through final authority checks, excluding cleanup | 242.28 seconds |
| Running computers | One parent and two children |
| Resources configured per computer | Two vCPUs, 4 GiB RAM, 24 GiB root disk |
| Raw host resource samples | 138 samples, nominal two-second cadence |

| Resource phase | Running guests | Host used-memory estimate |
| --- | --- | --- |
| Before provisioning | 0 | 1.548 GiB |
| Parent ready for checkpoint | 1 | 3.848 GiB |
| Both children ready | 3 | 14.273 GiB |
| After independent edits | 3 | 13.800 GiB |
| After guest cleanup | 0 | 1.586 GiB |

Host used memory is `MemTotal - MemAvailable`, not the sum of guest RSS. The
sampled high-water value was 14.649 GiB, and no host swap was configured or used.
The parent-to-children difference includes checkpoint activity and host overhead;
it is not a measurement of memory deduplication or a per-child reservation.
Across the 82.819-second checkpoint-ready to children-ready sampling window, host
CPU busy time averaged 11.524% of the 16-vCPU host. This uses the first eight
`/proc/stat` counters with idle and I/O wait excluded from busy time. The window
includes controller and sampling work, so it does not isolate pure fork CPU.

These are measurements from one fully passing run, not a reliability estimate
or a maximum-density benchmark. Earlier development runs are retained separately
and are excluded from this table. The final report is
`.state/full-state-fork-20261009/qualification-v9/report.json`; its source hashes
are alongside it. Raw samples are in
`remote-evidence/host-samples-v9.jsonl`, with SHA256
`22a6b2a293563abad6fac3b79d2f1098c45eec2753c64628116b33dab66dcad2`.
The deployed broker SHA256 was
`fac80d0b6895fcf450fbe878d14aa1dbafc0bb4ed1d6d368166485036113a7f2`;
its 28 Rust/Python production source files matched the local source. The fork VM
image fingerprint was
`f60a6f6d559ef0a4ca676950b781131f3a61eceaa554a38146cf23496bb85bcc`.

An independent audit verified all 45 checks, all 21 screenshot hashes and sizes,
the report's four source hashes, the exact three cleanup identities and the raw
resource measurements. The report SHA256 is
`ab25dd979eb3bd56efe275f6704edbcd05851e9345919b54e5e5c22ebdeacb9a`.
The eight failed HTTP events were expected authority/fencing denials with
`effect=none`. Screenshot integrity was audited separately from the application
state oracles; it is not a rendered-output quality assessment.

The final host inventory contained 183 unique retained Incus project/name entries,
all stopped. GCP then confirmed the qualified host was `TERMINATED`, with its disks
retained. Exact host identifiers remain in the private cleanup receipts. Cleanup receipts
are in `.state/full-state-fork-20261009/remote-evidence/final-host-receipt.json`
and `.state/full-state-fork-20261009/gcp-final-stop.json`.

The live qualification uses the production broker, a parent and two children,
each configured with two vCPUs and 4 GiB RAM. Its application oracles cover a
browser nonce held only in JavaScript memory, an unsaved native Mousepad buffer,
and an unsaved Blender object inspected through a live Python callback with a
fresh challenge. Boot IDs, process IDs and process start ticks establish process
continuity. The test changes all three branches before rereading them, so equal
initial state alone cannot satisfy the independence check. Screenshots accompany
the structured application evidence.

Authority checks require new child grants and worker tokens, denial of inherited
observation references and superseded grants, fresh child metrics, and no copied
vault assignment. Guest-root isolation checks attempt every directed peer pair
against both the worker and fixture ports. Every guest's loopback and primary-IP
listeners must respond before and after those probes. Cleanup uses the ordinary
broker stop API and retains disks.

TDD regressions cover unsupported backends, immutable target profiles and realm
bindings, duplicate request IDs, interrupted effects, partial child admission,
source-control recovery, worker hot rebind, preserved private-input guards and
fork-of-fork behavior. Live discovery also added Incus regressions for
all-project enumeration, complete inherited NIC overrides, QEMU socket path
limits and restored-guest shutdown. A branch receives one guest-agent power-off
request; only independently observed stop of its exact incarnation confirms
completion, including when the acknowledgement is lost.

The verifier accepts multiple default routes only when they identify the same
interface and gateway, and retains their metrics and protocols in the report.
Conflicting routes and multiple primary addresses still fail. Probe diagnostics
contain bounded network facts and closed error categories, not arbitrary guest
command output.

Semantic UI operations reobserve and select a fresh target after an explicit
public `conflict` with `effect: none`, up to three attempts with distinct action
IDs. Other failures, including unknown effects and lost responses, end the run.
The inherited-parent-observation denial check always makes one direct attempt.

This qualification exercises local full-state forks, not cross-host migration,
memory deduplication, maximum density or model performance. Each running child
needs its own RAM budget. Application login state already in memory or on disk
is copied; vault assignments are separate. External services do not acquire a
second independent connection merely because local TCP state was copied.

## V1 completion qualification, 2026-10-08 and 2026-10-09

Qualification now includes broker revision `f7bc8e6` and operator harness
revision `92360f7`, including runtime revision `31adc30`, shutdown revision
`886945e`, the backend and preview work in `4889e06`, infrastructure fixes through
`677e66d`, and private continuation fixes in `58314fd`. The older sections below
retain their original scope and counts. The final paired model comparison passed
all 48 attempts. Concurrent full-app container cold-start reliability at eight
and twelve guests remains unqualified, as detailed below.

| Check | Result |
| --- | --- |
| `make test` at `31adc30` | 87 Rust and 157 offline Python tests passed |
| `make lint` at `31adc30` | Rust formatting, Clippy and Python lint passed |
| `make smoke` | 36 real Chromium tests passed in 61.55 seconds; four headed Linux cases were skipped on macOS and covered by the separate Linux matrix |
| `make smoke-stack` | Both real broker/worker/MCP flows passed in 6.76 seconds, including the cached preview and three server-verified credential logins |
| Extra Linux admission case | The Linux-only concurrent cold-start single-slot test passed on the GCP host |
| Linux broker tests at `31adc30` | All 26 library tests passed on the real GCP host, including bootstrap publication and service-manager readiness |
| Request-history UI at `f7bc8e6` | Seven real Chromium credential UI tests passed; terminal history is collapsed, pending input and the library remain visible, and Lock clears the view |
| Operator harness at `92360f7` | Nineteen focused cases passed, including real Chromium capture, restart checks and typed admission failures |
| Broker installation | Initial install and health check passed on the real Linux host |
| Broker reinstallation | PID changed from 30767 to 31054, health returned, and the existing operator credential was preserved; the check took 0.736 seconds |
| Initial deployed operator qualification | Passed against the real GCP broker and initial VM image: input/save, assignments, revocation, preview, restart, expiry, idle stop and admission |
| Shutdown regression matrix at `886945e` | Eight real Linux headless/headed cases passed in 51.91 seconds; 144 offline Python tests also passed |
| GCP host cold stop/start | All 18 stopped Incus guests, seven images, broker binary, configuration, operator credential and vault key were preserved; all 14 broker records remained stopped and unleased |
| Independent static review at `d4c668a` | No material findings in the documented identity, credential, operator, guest-root, private-input, preview or standalone integration boundaries |
| Final two-VM lifecycle and isolation | 24 checks passed, including browser/native callbacks, private credentials, exact identity handoff and real cold restart |
| Final two-container lifecycle and isolation | 24 checks passed, including shifted UID/GID mappings, private credentials, browser/native callbacks and real cold restart |

New TDD regressions cover streamed Incus output limits, blocked stdin, process
group cancellation/reaping, independent guest readiness with serialized pool
admission, Incus 6 query/project creation, persistent bridge netfilter setup,
Linux compiler prerequisites and canonical root ownership in worker archives.
The archive regression reproduces GCP OS Login IDs outside an unprivileged
container's ID mapping.

Backend diagnostic regressions first demonstrated missing durable failure
evidence and arbitrary backend text leaking into broker errors. The broker now
stores a closed failure category, an optional known operation, the lifecycle
epoch and the time alongside the failed lifecycle transition and grant removal.
Authenticated operator responses and qualification reports retain this diagnostic;
raw backend text is excluded. The record survives stop, restart and reattachment,
and old state files still load. The change does not retry backend operations or
alter their deadlines. Focused Rust and Python RED/GREEN evidence, including
automatic reaping, is retained under `.state/operator-diagnostics/`.

The operator credential picker is tested in desktop and mobile Chromium. It
selects existing identity owners without retyping their scope, deduplicates slots,
preserves explicit entry, and clears its in-memory roster on lock. Preview tests
verify that reading a saved screenshot performs no capture, state write or cursor
change; private input, unknown effects and invalid receipts withhold the frame.
The real stack test renders that saved frame with optional labels.

Final visual inspection with dozens of historical requests exposed an operator
usability problem: completed login requests pushed the reusable credential library
thousands of pixels down the page. A real Chromium regression first reproduced
that behavior with 25 terminal requests and one pending request. Terminal history
now sits behind a counted disclosure; pending and in-flight requests remain
visible. Expansion makes no server request, and Lock clears the history and
disclosure state. Root inspected the resulting screenshot with the library
heading visible near the first viewport. Only embedded UI assets changed.

Private continuation tests first failed for input submit, button and image
controls. The fix permits those noneditable controls while still denying editable
fields. A second failing regression found that HTML `form.elements` omits image
submitters; checks now include their destination/method overrides and external
`form=` associations. All 19 private-input browser cases passed after the fix.

The dedicated live host is an `n2-standard-16` in `europe-west4-a`, with 64 GiB RAM,
a 250 GB persistent disk, Ubuntu 24.04 and Incus 6.0.0. Nested KVM is present. It
has no external IP or service account; the broker is reached through IAP and
binds loopback. The Incus project uses an owned Btrfs pool and private bridge.
The final full-app images published after real unprivileged Chromium sandbox and
desktop image checks. Their shared source archive SHA256 is
`b98d3b9cf6e960baff9fbc8d0e6d07a0b4e06ee8e749e6f39a0abbda7d470de2`.

| Final image | Immutable fingerprint |
| --- | --- |
| Incus container | `25993ecc0b16bd6812988420454422ac1052ca2c6d7147bbe56dad9d28689248` |
| Incus VM | `ce38c6f44bae4c01c1984ed85fffc6f16419b60d60498f49c333408f2fa51e86` |

The complete host cold restart preserved the image inventory and private-file
hashes. A new boot ID confirmed the restart; KVM, the broker, Incus, the optional
NSS cache and both bridge netfilter hooks were healthy afterward. The same GCP
instance ID retained its disk. Its automatic stop guard was extended from four
to eight hours while it was terminated, before starting final live qualification.
These checks prove host persistence and provisioning. Subsequent two-guest runs
passed for both final images. Guest root could reach its own fixture and the
public network, while direct and gateway-routed peer attempts and attempts to
reach the host and metadata listener were denied. Each denied destination had a
separate successful TCP reachability control. Host-side effective configuration
and installed-module evidence also passed. These are observed confinement checks,
not proof against all hostile-kernel or application attacks. The density and
final model-task results below retain their separate revision and workload scopes.

The first two-VM live run passed browser and native interactions, credential
login, identity handoff and direct network-denial checks, but failed persistent
browser-cookie recovery after a real cold restart. This was reproduced with the
Linux desktop stack before changing the shutdown code. The supervisor now stops
the worker before Chromium, X11 and D-Bus. Public and private request draining
are bounded so an unfinished request cannot consume the browser flush window.
The eight-case regression matrix covers idle shutdown, a completed in-flight
command, a cancelled long command and an incomplete private request in both
headless and headed modes. Restart reuses a completed receipt and refuses to
replay an uncertain action. A separate read-only review found no material issue;
forced termination after the grace period still cannot guarantee browser-state
persistence. The final VM and container images subsequently passed fresh live
cold-restart qualification: all four guests retained their workspace and their
browser login, verified independently by the fixture website. VM reattachment
took 24.75-25.83 seconds; container reattachment took 7.64-7.78 seconds in these
two-guest runs. First-use VM attachment took 115.2-116.6 seconds and included
materializing the new image; it is reported separately from cold reattachment.

The completed density runs below used broker `448868c` and fresh identities with two vCPUs, 4096 MiB
RAM and 24 GiB disk each. Every guest passed a server-observed browser click,
native AT-SPI callback, private credential login, identity handoff, one cold
restart with retained workspace and website-verified cookie, and stop/release
cleanup. Actual sampled concurrency matched the requested count, with no
inventory errors or unexpected running guests. The two-guest runs include the
full isolation matrix; later density runs intentionally skip that matrix and
retain a `partial` overall harness status while all required workload checks pass.

| Guest type | Concurrent guests | Peak host used GiB | Above quiet baseline GiB | Median initial attach seconds | Median cold reattach seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| VM | 2 | 5.21 | 4.01 | 115.89 | 25.29 |
| VM | 4 | 9.40 | 7.80 | 61.75 | 32.03 |
| VM | 8 | 17.22 | 15.59 | 88.64 | 49.79 |
| VM | 12 | 24.91 | 23.27 | 111.59 | 71.80 |
| Container | 2 | 2.92 | 1.63 | 55.84 | 7.71 |
| Container | 4 | 3.93 | 2.24 | 36.36 | 11.63 |
| Container | 8 | 6.01 | 4.03 | 70.94 | 19.87 |

The twelve-container attempt failed: eleven attachments succeeded and one
returned HTTP 502 with `backend_failure` and an unknown effect after 93.10 seconds.
Eleven guests completed browser/native preparation, but the run did not reach
credential login, handoff or cold restart. Cleanup stopped all twelve owned
records. This result does not qualify twelve containers or establish a hard
capacity ceiling. Peak host use was 8.20 GiB, with 54.59 GiB still available and
a sampled CPU peak of 33.43 percent. The guest journal records a worker start and
no startup crash or OOM. The exact failed backend operation was not retained;
the cause remains unknown. The successful-run timing table above excludes that
failed phase, rather than averaging only its eleven successful starts into a
purported twelve-container result.

A fresh twelve-container run on `05f8e4a` reproduced one failed attachment after
106.13 seconds. The new diagnostic retained `command_failed` with operation
`worker_service_start`, both in the HTTP evidence and after cleanup in the durable
computer record. The other eleven guests passed browser/native preparation;
the full parallel lifecycle workload did not run. The failed guest's service was
healthy six seconds later. Its journal records D-Bus readiness and worker start
after the command failure, with no missing environment file or service crash.
The discarded command stderr means the historical result cannot distinguish
an early systemd-manager failure from an Incus exec transport failure. Peak host
use was 8.83 GiB, leaving 53.95 GiB available. All twelve owned records were
stopped and unleased afterward; all 82 retained Incus instances were stopped.
Both failed runs remain intact as evidence.

Revision `31adc30` fixes an independently demonstrated bootstrap hazard: the
old program published the activation token before the required environment file.
The new program validates established values before any mutation, makes the
complete environment file and directory durable, then publishes and syncs the
token. Eight actual-program tests cover activation visibility, interruption,
resuming a partial install, mismatched existing state and legacy environment
upgrade. A separate read-only systemd manager-property probe now gates bootstrap
under the existing overall deadline. Three subprocess-fixture tests cover delayed
or unavailable readiness and a failed service start. Service starts still execute
once; a later healthy worker cannot turn an uncertain start into a successful
attachment. These tests establish the corrected contracts without claiming a
conclusive cause for the historical Incus command failure.

The fresh twelve-container run on `31adc30` also failed qualification. Nine
attachments returned HTTP 200 and three ended with transport errors and unknown
effects. One admitted browser click received an unknown-effect response when its
follow-up observation did not complete; the fixture independently recorded
exactly one click. Eight guests passed browser/native preparation, and the run
never reached the full parallel lifecycle workload. The retained journal does
not identify the observation exception, and these results are not diagnosed as
timeouts solely from their duration. Peak sampled host use was 8.13 GiB; maximum
observed running concurrency was ten. Cleanup verified twelve stopped, unleased
broker records, eleven present and stopped Incus guests, one absent guest and
all 93 retained host instances stopped. No effects were replayed. All three
failed twelve-container runs remain separate from successful capacity results.

A final eight-container repeat on the same revision also failed. Seven initial
attachments succeeded in 48.05-284.32 seconds and passed both browser and native
checks; one attachment ended with a transport error and unknown effect. No full
lifecycle barrier was reached. All eight guests were subsequently stopped and
unleased, and all 101 retained host instances were stopped. Peak sampled host use
was 7.90 GiB, leaving 54.89 GiB available. Thus the earlier passing eight-container
row is historical evidence, not a successful final-repeat result. Concurrent
container cold-start reliability at eight and twelve remains unqualified; memory
headroom alone is insufficient to choose a supported pool size.

Memory is host `MemTotal - MemAvailable`, sampled approximately every five seconds; short peaks
can be missed. The first run of each guest type includes initial image
materialization. Later runs reuse that image cache. These bounded lifecycle
workloads establish tested concurrency, not maximum capacity, sustained model
throughput or heavy simultaneous Blender rendering. Model inference runs outside
the host. CPU summaries cover the broader sampled phase, including startup and
cleanup, and are not steady-state measurements.

An independent cleanup audit of the earlier `448868c` density batch verified
all 52 qualification identities stopped and unleased, all 70 then-retained Incus
instances stopped, no remaining
qualification credentials or pending operations, and matching effective resource
profiles for all 52 guests. No guest disks were destroyed. This cleanup result
does not change the failed twelve-container outcome.

The deployed operator run uses the real private-input page with synthetic values,
saves the login for its requesting identity, reuses it, revokes access through
the known-agent picker, verifies denial, and reassigns it. Browser, native desktop
and operator preview observations withhold input. Safe preview pixels exactly
match the agent receipt; an unchanged cursor omits repeated image data. Root
visually reviewed both the assignment page and the page with preview labels off.
Broker restart invalidated old grants and cancelled pending input while retaining
the same computer, vault assignment and privacy guard. A one-second lease expired
and the automatic reaper stopped the computer without an explicit stop/reap call.
With a temporary running limit of two, a third attachment was denied and a new
attachment succeeded after freeing one slot. Cleanup left all 14 broker records
stopped and unleased, removed the run's credential, and restored the normal pool
configuration. Its private report is `qualification-operator-v1/report.json`.

The final-image operator-v3 run passed credential saving, reuse, revocation,
reassignment, browser/desktop privacy and exact preview checks, then failed a
navigation after broker restart. Incus evidence supports a second VM start during
reattachment; the exact stop cause and navigation exception were not retained.
The harness incorrectly depended on its temporary website process surviving a
possible guest stop. New RED/GREEN tests move only the post-restart safe preview
check to `about:blank`, after verifying identity, vault assignment, cancelled input,
revoked old grants and the persisted privacy guard. They make no claim that a
temporary process survives. The harness also now checks the closed admission
diagnostic rather than obsolete human-readable error text. The failed v3 report
remains unchanged. Fresh operator-v4 qualification on broker `f7bc8e6` and
harness `92360f7` passed all 25 checks, including real private input/save,
assignment/reuse/revocation, privacy, exact preview reuse, broker restart recovery,
automatic idle stop, two-slot admission and all four owned-computer cleanups.
Root visually reviewed the deployed assignment page and compact active preview
card with optional labels hidden. Its private report is
`qualification-operator-v4/report.json`; image hashes and review observations are
retained alongside it.

Local private evidence is retained under `.state/live-v1-20261008/`, including
binary provenance and the reinstall receipt. Operator credentials and vault keys
are excluded from reports. No upstream Meerkat or MobKit source was changed.

The first final model comparison was halted and explicitly invalidated after
the GTK task exposed a benchmark-fixture defect. The image had Python's `gi`
module but lacked Gtk3 introspection. HTTP health alone allowed dispatch without
the intended native window. The lab now prepares those benchmark dependencies
explicitly and requires a live process plus a matching mapped-window marker
before dispatch. These setup operations are outside model timing and counters.
The two dialog failures remain raw evidence but are not model scores. A later
Opus editor attempt saved the correct file; it is functional corroboration only,
not part of the replacement paired comparison. The next Astra attachment was
interrupted before any model call, then reconciled against an observed stopped
guest. All owned computers were stopped and unleased before further checks.

Lab reporting regressions also demonstrated lost known usage when another stage
had unknown accounting, missing observation evidence turning unknown task
outcomes into failed outcomes, and uncertain tool failures being omitted from
counter limits.
The corrected reporting keeps known stage subtotals, unknown outcomes and
lower-bound counts separate. A new stop-file mechanism ends dispatch between
attempts after normal cleanup. These changes passed RED/GREEN tests without
changing the benchmark tasks, model prompts, resources or limits.

Four additional RED/GREEN tests cover the GTK fixture's nonblocking dialog
lifecycle. Lab revision `fc10689` replaces the nested loop with a response callback: Cancel and
window close submit nothing; Apply reads the actual entry once, saves once and
closes. All 127 focused qualification tests passed, with five explicitly gated
live tests skipped in that offline invocation. A fresh unscored VM check then
opened the dialog through AT-SPI, filled its editable field, invoked Apply and
passed the independent saved-result evaluator. Root reviewed the retained filled
entry and completed-status screenshots. The check made no provider calls, and
release/stop both returned HTTP 200. Earlier partial checks remain separate,
including one with a rejected Apply command whose cause was not retained.

### Final paired model comparison

Lab revision `fc10689b551ba17ad006d3e914ef1faee0bb667a` completed the frozen
`package-final-20261009-v2` plan: twelve task types, two deterministic seeds and
two models at medium effort. Each attempt used a fresh identity-owned VM with
two vCPUs, 4096 MiB RAM and a 24 GiB disk, on the same final image and broker.
Attempts ran serially with balanced model order. The two-stage identity tests
used distinct runtime sessions around a real guest stop/start, giving 52 native
sessions across the 48 attempts. Model inference ran outside the GCP host.

All 44 non-Blender attempts passed their independent objective evaluators. All
four Blender files passed independent scene-property inspection, and root
reviewed the final recorded desktop screenshots for visible colored geometry and
useful framing. Those four positive visual verdicts were recorded only after the
controller loop exited. The resulting score is 48/48, with no task failures,
setup failures, provider crashes, unknown outcomes or unresolved cleanup in this
replacement run. The invalidated first run remains separate evidence.

| Model | Successes / attempts | APS | Input + output tokens | Submitted production commands | Native-run seconds | Full-attempt seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Opus 5.5 | 24/24 | 1.00 | 15,959,058 | 176 | 804.214 | 2,271.758 |
| Astra 6 | 24/24 | 1.00 | 8,646,807 | 166 | 988.564 | 2,444.345 |

| Model | Recorded pointer clicks | Recorded UI commands | Attempts with complete execution counters | Separate catalog/discovery calls | Screenshots returned |
| --- | ---: | ---: | ---: | ---: | ---: |
| Opus 5.5 | >=37 | >=89 | 14/24 | 52 | 119 |
| Astra 6 | >=42 | >=80 | 14/24 | 86 | 112 |

Token accounting reconciles all 513 selected provider requests exactly once.
Presented input includes repeated cached context: Opus reports 13,470,425
cache-read and 2,455,809 cache-creation tokens within its 15,926,860 input tokens;
Astra reports 6,931,382 and 1,644,929 within its 8,633,104 input tokens. These
subsets are already included and must not be added again. Output totals are
32,198 and 13,703 respectively. Token totals are accounting measurements, not
cost estimates or a claim that the two providers tokenize images identically.

There was no cumulative token cap. Each stage allowed 120 model turns, 100 tool
calls and 360 seconds, with a 32,768-token per-response output limit. Native
compaction history retention was configured as `history_retained_rewrites = 121`.
Native-run time includes MCP setup, the model/tool
loop and accounting recovery. Full-attempt time includes guest setup, evaluation
and cleanup. The 182 controller operations per model are excluded from model
command counters. All 342 submitted production commands have unique action IDs.
All 34 error receipts report `effect=none`, but missing numeric execution evidence
keeps ten attempts per model and the aggregate click/UI totals as lower bounds.
Game moves are not applicable to this package qualification suite.

An independent raw-evidence audit passed 4,681 checks across 691 hashed inputs,
with no discrepancies. All final inputs stayed stable during capture, and all
374 stage artifacts shared with the earlier interim audit were unchanged. This
small deterministic suite establishes functional qualification on the specified
configuration, not broad model superiority, maximum density or sustained
multi-agent throughput. Detailed per-task and per-attempt results are in
the operator's separate benchmark checkout at
`meerkat-computer-lab/results/2026-10-09-package-qualification.md`.
That private lab report is not included in this package's source archive.

The final read-only cleanup audit verified all 48 expected owners and guest
incarnations, stopped/unleased records and credential deletion. Globally, all
159 broker records had no active grants, the vault had no remaining credentials,
no private input was pending or in flight, and all 161 retained Incus guests were
stopped with no pending non-websocket operations. Binary, configuration and image
profile hashes still matched the frozen provenance. Private cleanup and audit
receipts are retained under `.state/live-v1-20261008/`.
The owned GCP host was then stopped and independently
verified as `TERMINATED` at 2026-10-09 02:06 UTC with the same instance ID and
ownership label. Its persistent disk and retained guest disks were preserved;
no resources were destroyed.

## Reusable website credentials, 2026-10-08

Implemented using test-driven development. The first broker tests failed on the
missing tools and operator routes; vault, private worker and operator UI tests
also failed before their implementations. Further RED-to-GREEN regressions
covered atomic save-and-assign, normalized-origin bypasses, lease expiry between
fields, GET form destinations, and unsafe private receipts. The final receipt
regression first failed on lost error telemetry, then passed with bounded numeric
counters and an explicit lower-bound marker for uncertain effects.

Final qualification ran locally on macOS arm64 with synthetic credentials:

| Check | Result |
| --- | --- |
| `make test-rust` | 60 passed: 27 existing backend/core, 13 vault, 13 broker and 7 private receipt tests |
| `make test-python` | 88 Python/infrastructure tests passed |
| `make smoke` | 16 real Chromium tests passed, including 8 secure input and 5 operator UI cases |
| `make smoke-stack` | Both real broker/worker/stdio MCP flows passed in 4.05 seconds |
| `make lint` | Rust formatting, Clippy with warnings denied and Python lint passed |
| `make build` and CLI | Build, key command help, private key creation, overwrite refusal and enabled configuration validation passed |
| Patch whitespace | `git diff --check` passed |

The credential stack case uses the real Rust HTTP broker, encrypted vault, private
stdin helper, guest-local Unix socket, Python worker, Chromium and official MCP
client/server SDK. Three server-verified POST logins cover the initial saved
credential, its rotated value and one-time operator input with optional saving.
It verifies denial for another identity, no duplicate delivery, withheld
observations, absence of fixture passwords from model responses and persisted
broker/worker journals, and the existing browser session after MCP reconnect.
The browser profile is deliberately excluded from the plaintext-state assertion:
the authorized browser and website receive the credential. This new stack case
does not claim browser-cookie persistence across a worker restart.

Separate tests cover exact realm/namespace/identity assignments, revocation,
session-only one-time input, expired and replaced control grants, cancellation
on broker restart, unknown in-flight outcomes, privacy after worker restart,
cross-origin operator requests, validation errors, hostile metadata in the UI,
and filtering guest diagnostics and receipt metadata. UI tests use real Chromium
against synthetic API fixtures; the stack case separately exercises the real API.
Desktop and mobile fixture previews are retained in the ignored local directory
`.state/qualification-credentials-ui/`.

No upstream Meerkat or MobKit files changed. No live Incus/GCP host, external
website account or model provider was used in this qualification. The stack
backend runs local processes, so these results do not establish VM/container
isolation. A guest administrator can inspect its own delivered values and
website sessions. See [CREDENTIALS.md](CREDENTIALS.md) for setup and boundaries.

## Native desktop extension, 2026-10-08

The extension adds Linux X11/AT-SPI, a shared browser/desktop observation cursor,
installed-app launch and an optional full desktop image. The local macOS checks
remain credential-free; real native execution uses an isolated Ubuntu 24.04
arm64 Docker container, not the host desktop.

| Check | Result |
| --- | --- |
| `make test` | 27 Rust tests and 85 Python/infrastructure tests passed |
| `make smoke` | 3 real Chromium tests passed |
| `make smoke-stack` | Real Rust broker, Python worker and official MCP browser flow passed |
| Linux native smoke | GTK native edits/actions, menus/dialogs, pointer/keyboard input, app launch and MCP flow passed in 84.05 seconds |
| Linux desktop stack | Real Rust broker, desktop worker and MCP passed, including operator access and identity handoff |
| Linux guest administration | Exact production sudo bootstrap passed enable/disable and credential preservation checks |
| Blender Python workflow | Script file, running-app Python execution, exact scene receipt, saved .blend and viewport screenshot passed |
| `make lint` | Rust formatting, Clippy with warnings denied, and Python lint passed |
| `make build` and CLI | Executable built; `access --help` and example configuration validation passed |
| Shell syntax and patch whitespace | Passed |

Administrator regressions verify immutable privilege policy, operator/control
credential separation, maintenance target incarnation checks and host stop without
worker consent. The exact production guest bootstrap was also executed in a
separate disposable Linux container: ordinary UID 10002, `sudo -n id -u` returned
0 when enabled, and sudo was denied after the managed rule was removed. Worker
credentials were preserved. This test does not establish Incus isolation.

The native unit cases cover bounded/partial accessibility, absent accessibility,
password text omission, native actions and their original AT-SPI indices,
unsupported protocol properties, stale target/tree/focus, inactive-window refs,
lease revocation and renewal, double-click/right-drag metrics and uncertain input.
Worker regressions also cover cross-surface refs and invalidation by guest shell
commands or writes that could redraw an inaccessible canvas.

The full Linux test image installs Blender 4.0.2, Inkscape 1.2.2, GIMP 2.10.36,
LibreOffice 24.2.7, Mousepad and Thunar. AT-SPI is 2.52.0 and Xvfb is 21.1.12.
Versions are resolved from the configured Ubuntu repositories at image build;
production Incus builds are identified by their resulting image fingerprint.

The native run rendered all four large apps and confirmed keyboard window closure.
Bounded AT-SPI observations returned 266 Inkscape elements, 250 GIMP elements and
161 LibreOffice elements, honestly marked partial/truncated. Blender returned no
useful semantic nodes; its rendered cube/grid and keyboard control were checked
through the screenshot/input channel. GTK fixture checks separately verified
semantic fill/invoke and foreground reference-based click/key/type. A LibreOffice
launch returned an uncertain post-launch observation; the test observed its
existing window and did not replay the launch.

A subsequent focused run verified Python as Blender's primary control path.
`workspace_write` saved a scene script; `computer_act` opened Blender's Python
Console and executed it, then returned to the viewport. The cube was renamed
`Meerkat Python Cube`, scaled to 1.8/1.0/0.6 and rotated 0.6 radians around Z.
Its interpreter receipt matched those values, a 919312-byte `.blend` was saved,
and the viewport screenshot visibly showed the changed geometry and transforms.
The focused run, including GTK/MCP/fencing checks, passed in 45.96 seconds.

This exercise found that zero-delay X11 typing dropped repeated characters in
Blender. A retained console screenshot shows the missing closing parenthesis.
The driver now uses a 2 ms typing delay and a length-scaled, bounded timeout;
the real run passed an exact `ee))` probe plus the script execution. Unit cases
also cover command timeout bounds and cleanup of timed-out subprocesses.

The accepted report, screenshots, package versions and sudo receipt are retained
locally in `.state/qualification-20261008/` with hashes. They are ignored by Git.
The native worker/MCP smoke and the real Rust broker stack are separate lanes;
the native smoke's binding bridge is only a test fixture. No model agent completed
a native-app benchmark during this qualification.

## Initial browser package

Verified locally on macOS arm64 on 2026-10-08.

| Check | Result |
| --- | --- |
| `make test` | 20 Rust tests and 33 Python/infrastructure tests passed |
| `make smoke` | 3 real Chromium tests passed |
| `make smoke-stack` | 1 broker/worker/MCP/Chromium integration test passed |
| `make lint` | Rust formatting, Clippy with warnings denied, and Python lint passed |
| `make build` and CLI validation | Executable built and example configuration passed structural validation |
| Shell syntax | Installer, worker provisioning and Rust wrapper scripts passed `bash -n` |
| Python entry points | Worker, MCP adapter and example runner help commands passed |

The combined stack test used the real Rust broker HTTP routes, real Python
worker, sandboxed Chromium and the official MCP client/server SDK. Its backend
started local processes solely for testing. It verified browser observation and
clicks, click counters, stale-observation rejection, workspace file operations,
guest command execution, path rejection, operator/control credential separation,
exclusive attachment, identity handoff, old-grant rejection and broker restart.

Core regression tests additionally cover scoped identity/session separation,
concurrent attachment, expiry, renewal failure, persistent action deduplication,
unknown outcomes, cancellation recovery, private persisted credentials, state
locking, immutable profiles, capability checks and cleanup across busy or failed
computers. Infrastructure tests check command construction and saved ownership;
they do not execute cloud or container operations.

No Incus guest or GCP host was created for this verification. Incus image
construction, container/VM isolation, real cold start/stop, host capacity and GCP
provisioning still require a live qualification run. No model inference or match-3
benchmark was run for this package. The SDK example is supplied as an integration
path; it is not a recorded provider run. Hosted CI has not run because this is a
local repository with no remote configured.

The deployment guide includes the live image check: publication requires a real
Chromium launch with its sandbox enabled as the unprivileged worker user. The
configured pool limit is an admission setting, not a measured capacity result.
