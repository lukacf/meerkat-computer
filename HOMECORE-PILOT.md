# HomeCore Discovery pilot, 2026-10-09

This is a historical record of the 2026-10-09 deployment and its limitations,
not the current installation runbook. Later compact-observation work and the
2026-10-12 wake/idle verification are recorded in [VERIFICATION.md](VERIFICATION.md).
Deployed HomeCore native inline preview remains disabled and unqualified, and
ordinary screenshot results still render as placeholders. Use the current
[host integration guide](docs/integrations/host.mdx) for new integrations.

Status at the time: the live pilot completed. Computer use and reconnection after a
HomeCore deployment restart passed. The Console shows the turns and tool
receipts, but its screenshot rendering check failed: images appear as text
placeholders. Compact observations and Console image rendering were follow-ups.

## Setup

HomeCore owns agent selection, sessions, runtime generations, tool registration
and deployment. Meerkat Computer owns the durable computer and its exclusive
control grant. The pilot uses a fresh desktop VM, with no household website
credentials. The deployed tool census found eight computer tools on
`domain:discovery` and none on the other 18 identities.

- Host: GCP `n2-standard-16`, nested KVM, project identifier retained in private receipts.
- Guest profile: `desktop`, 2 vCPUs, 4 GiB RAM, 24 GiB disk, guest admin enabled.
- Guest reported Ubuntu 24.04.5; browser, X11/AT-SPI and native applications are
  enabled. Model inference runs through HomeCore, outside the guest.
- Computer and slot identifiers are retained in the private pilot receipts.
- HomeCore: MobKit 0.8.47, Meerkat SDK 0.8.51, MCP Python 1.27.1.
- Model: `openai/gpt-6-astra`, using Discovery's existing durable session.
- Client: `f16e4532449d759e45027cbf9b63b527ad43d190`, vendored byte-for-byte
  into HomeCore because the full worker package is incompatible with its older
  dependency lock. Client SHA-256:
  `eba4daaa2b8bab32923e54c7e27e186d95e38ed60b13b4fc9e0c1ae1aa2fd774`.
- Broker source: `c59f5c8fa36bf1c192e66bd3551cbb197c0d41df`.
- Guest image fingerprint:
  `f60a6f6d559ef0a4ca676950b781131f3a61eceaa554a38146cf23496bb85bcc`.

HomeCore's integration was merged in
[PR 484](https://github.com/lukacf/homecore/pull/484). Its callback bridge lazily
attaches on the first real computer call, using the trusted identity, actual
session and current runtime generation. It serializes grant lifecycle and tool
calls and retains in-flight attach/renew work through callback cancellation.
Cancelled tool actions remain unknown and are not replayed; uncertain lifecycle
outcomes block further admission. Tokens are not sent through the agent bus or
included in the receipts.

## Real agent attempts

| Attempt | Outcome | Turn wall time | Computer calls | Known input tokens | Known output tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| Smoke 1 | Incomplete final check/report after compaction lost the request | 185.3 s | 9 | 6,437,061 | 509 |
| Smoke correction 1b | Passed readback, observation and report | 35.0 s | 4 | 869,474 | 414 |
| Public research 2 | Passed, before restart | 57.4 s | 4 | 1,141,728 | 503 |
| Reconnect 3 | Passed read-only state checks after restart | 37.441 s | 3 | 1,044,367 | 274 |

The first attempt navigated to example.com, scrolled through the browser UI,
wrote and read a workspace marker, checked the guest OS and passwordless sudo,
launched Mousepad, observed the desktop through accessibility and a screenshot,
and typed into its text field using an accessibility reference. All nine
computer calls succeeded. Its final explicit observation and report were not
completed because session compaction lost the task. The corrective turn read
and observed the existing state without recreating the marker or retyping the
editor text. HomeCore accepted the corrected report. Smoke APS is therefore
2.00, not 1.00.

The research turn made four successful browser navigations across public
Tekniska Museet, its booking site and Royal Djurgarden pages. No CAPTCHA or
access block was reported. Its task APS is 1.00. This pilot is not an Opus/Astra
comparison or a replay of the deterministic match-3 benchmark.

Independent backend inspection at 13:46:44 UTC verified the exact Incus guest
incarnation, computer, session, runtime generation 1 and epoch-1 lease. The
operator API and durable store agreed. The broker process had not restarted.
The guest's marker was a regular file containing exactly these 75 bytes, with
one internal newline and no trailing newline:

```text
HomeCore Discovery has its own computer.
Pilot marker: HC-COMPUTER-20261009
```

## Restart and grant replacement

The restart uses HomeCore's activation 242 deployment, which also includes a
state clone and an inbound-PDF code change. It is not a pure same-activation
restart. HomeCore reported release of the old lease at 14:05:28 UTC and process
exit at 14:05:29 UTC, before any replacement process started.

Independent inspection at 14:06:24 UTC found the same computer in `idle` phase,
with epoch advanced from 1 to 2, no lease or control grant, and expiry zero.
The operator API and durable store agreed. The exact guest incarnation was
still running, and the broker process had not restarted. This confirms durable
revocation; no direct request using the old bearer was attempted.

Activation 242 opened at 14:11:34 UTC. HomeCore reported the same Discovery
session, runtime generation advanced from 1 to 2, and the eight computer tools
still exclusive to Discovery. Attachment remained lazy. The reconnect turn ran
from 14:16:15.464 to 14:16:52.905 UTC. It read the marker and observed the browser
and desktop, completing all three calls without rewriting or retyping anything.
HomeCore reported the browser still on the Tekniska booking page and the unsaved
Mousepad buffer still containing `Discovery native desktop input works.`
Reconnect APS is 1.00.

Independent inspection at 14:18:28 UTC verified the same computer, session and
guest incarnation, runtime generation 2, epoch 3 and a new lease. The marker
remained exactly 75 bytes. The operator API and durable store agreed, and the
broker process was unchanged. This establishes old lease at epoch 1, durable
revocation at epoch 2, and replacement at epoch 3. The guest stayed running
throughout the HomeCore restart; this is not a guest reboot or RAM restore test.

## Measurement and evidence

Independent receipt audit verified the supplied hashes. Twenty distinct computer
action IDs all completed; no supplied computer receipt reported failure or
unknown effect. Every per-action delta reconciled with cumulative
admitted/completed counters 1-20. Totals were not summed across successive
receipts.

| Counter | Before restart | Reconnect | Total |
| --- | ---: | ---: | ---: |
| Computer invocations | 17 | 3 | 20 |
| UI commands / clicks | 3 / 0 | 0 / 0 | 3 / 0 |
| Observations / navigations | 11 / 5 | 2 / 0 | 13 / 5 |
| Application launches / native focus commands | 1 / 1 | 0 / 0 | 1 / 1 |
| Workspace reads / writes | 2 / 1 | 1 / 0 | 3 / 1 |
| Workspace bytes read / written | 150 / 75 | 75 / 0 | 225 / 75 |
| Guest exec commands / timeouts | 2 / 0 | 0 / 0 | 2 / 0 |
| PNG blocks reported by the redacted extractor | 11 | 2 | 13 |

The independently audited numeric ledgers contain 26 provider completion rows:
9,492,630 input tokens and 1,700 output tokens, totaling 9,494,330. OpenAI input
already includes cached subsets; neither cache counts nor reasoning diagnostics
were added again. Four lifetime-session usage rows and four duplicate summary
rows were excluded. Separate compaction usage was not retained, so this is known
captured request usage, not a complete pilot token bill or cost estimate.

Discovery's first provider call already presented 640,909 input tokens from its
existing session. Mid-turn compaction occurred at 739,794 input tokens, and the
gateway reported bounding a 2,672,260-byte summarization source to 184,400 bytes.
Desktop observations added approximately 45.7k tokens each according to
HomeCore's retained trace: about 118 KB of JSON text, with the PNGs held as blob
references. This exposed a long-session compaction problem. The test preserved
the existing durable session.

The first computer call took 45.786 seconds at the caller and 0.625 seconds in
the worker. The difference includes attachment and other overhead, and is not
an isolated VM cold-start measurement.

Host resource sampling began late. It captured none of the original or
corrective smoke, and only the final 33.3 seconds of research. Six complete CPU
intervals inside research covered 29.9 seconds and averaged 3.84% CPU across
the 16-vCPU host. A separate 265-second post-turn idle interval averaged 1.07%.
Available memory stayed about 59.7 GiB, with no swap. Every five-second inventory
sample observed only the exact Discovery guest running; all other guests were
stopped. These host-wide values include background activity and sampling
overhead, and do not establish full-turn resource use or maximum capacity.

The reconnect sampler enclosed the complete 37.441-second turn. Seven CPU
intervals wholly inside it covered 35.009 seconds, with 2.50% mean CPU across the
host and a maximum five-second interval of 4.86%. The pre-dispatch baseline was
1.21% over 45 seconds. During the turn, available memory was 59.7 GiB and no swap
was used. Only Discovery was running in every inventory sample. Boundary
intervals are reported separately; post-turn samples include independent
verification work and are not an isolated idle benchmark.

## Console result

The coordinator inspected an existing owner Console tab in Chrome without
changing authentication. Discovery's research and reconnect turns, successful
tool cards and a 16:16 turn timestamp were present. Expanded results showed
literal `[image: image/png]` placeholders. The rendered page contained no image
elements, and its tool cards displayed JSON text rather than screenshot pixels.
This is a failed screenshot-rendering check on the deployed 0.8 callback path,
not merely a missing test. No new model or guest action was needed for the check.
The upstream native MCP Apps path remains a separate qualification.

## Evidence locations

Local evidence is retained under `.state/homecore-discovery-20261009/`:

- `homecore-receipts/receipts-241-audit.{json,md}` and the hash-verified receipt
  and numeric-ledger source files.
- `homecore-receipts/pilot-receipts-audit.{json,md}` for the combined action
  counter audit across all four turns.
- `backend/first-turn-20261009T134643Z.json` for independent identity and marker
  verification.
- `backend/old-process-stop-20261009T140624Z.json` for independent grant
  revocation before the replacement HomeCore process attached.
- `backend/reconnect-20261009T141827Z.json` for the new grant and exact marker
  readback after reconnection.
- `backend/resources-20261009T134700Z-summary.json`, its attribution record and
  unchanged raw samples. The raw filename says `corrective`; the attribution
  explicitly corrects that historical filename to partial research coverage.
- `backend/resources-reconnect-20261009T141525Z-summary.json` and raw samples
  for full reconnect coverage with separate boundary intervals.
- `console/inspection-20261009.json` for the Console inspection record. The
  rendered screenshot is in the task transcript; no separate image file was saved.
- `backend/final-readiness-20261009T142045Z.json` and
  `backend/final-gcp-schedule.json` for final inventory and availability.

Shared receipts omit full household transcripts and memory-search content. Their
image counts are extractor metadata. Separately, HomeCore's
operator decoded and viewed the two reconnect images, confirming the Tekniska
page and Mousepad over the browser. That is separate from model perception and
the Console's failure to render those images, described above.

## Qualification and operating limits at the time

- A compact accessibility projection remains a follow-up. The current AT-SPI
  observation traverses up to 300 nodes, including non-showing nodes, and emits
  verbose fields. Its 24,000-character limit applies to node text, not serialized
  JSON. There is no total result-size bound. Meerkat Computer should own a
  configurable compact default and explicit expansion while preserving useful
  references; HomeCore should not silently trim the tool's semantic structure.
- The deployed 0.8 runtime lacks native MCP Apps. This pilot uses ordinary image
  tool results; native inline App admission/rendering is a separate qualification.
- The 19-identity census proves tool registration isolation. Direct invocation
  of another identity's callback outside the SDK scope remains unverified.
- Computer mutations carry R2 metadata. This finite pilot was user-authorized;
  no per-action deterministic approval gate is claimed.
- No automatic screenshot-forwarding path was found in the bridge. Complete
  confinement through shared runtime blob APIs remains unverified.
- Control tokens stay in HomeCore's process. A direct old-bearer replay test is
  not available to the independent reader; durable fencing and replacement are
  reported separately from that unperformed test.
- The dedicated broker endpoint on HomeCore loopback depends on two SSH tunnels
  through the laptop. It is a temporary pilot connection, not permanent hosting.
- Grants last one hour; HomeCore renews on a call when less than ten minutes
  remain. An unleased computer stops after fifteen minutes idle. The successful
  process-restart check did not exercise an expired grant or a stopped guest.
- Final backend inspection at 14:20:45 UTC found one Discovery guest running and
  all 183 retained lab guests present and stopped. The pilot remains enabled,
  with the broker and relay running. The host has a configured STOP at
  20:10:02 UTC / 22:10:02 Stockholm; no disks are scheduled for deletion.
- Hosted package CI could not start because GitHub reported an account billing
  or spending-limit block. Local client and compatibility checks passed as
  documented in [VERIFICATION.md](VERIFICATION.md); CI is not reported green.
