# Changelog

Changes are recorded here before a release is cut. A candidate version in
the manifests is not a published release. Exact build inputs and checksums belong
in the artifact's `manifest.json`; qualification belongs in [VERIFICATION.md](VERIFICATION.md).

## 0.1.0 - Unreleased

Initial release candidate:

- Core machine dependencies and the SDK example now target the published
  Meerkat 0.9.0 release. Native Console acceptance targets released Meta 0.9.0
  with its new crate layout, RPC names and owning identity handle.
- Binary host/guest transfers with separate import/export capabilities, private
  staged file IDs, byte quotas, verified exports, operator CLI and Python APIs.
  Browser file-input uploads and recent guest-download listing stay within the
  ordinary browser capability. File bytes are excluded from action receipts.
- Evince PDF viewer and Poppler utilities in both desktop image bundles, with
  a guide for native viewing, text extraction and page rendering. Exact duplicate
  checked-state notifications are deduplicated. When semantics still do not
  settle, hybrid observations return a fresh screenshot without stale refs and
  preserve input validation. Unstable capture errors are classified as conflicts.
- Native input rejection receipts distinguish pixel, topology, focus and
  accessibility changes using bounded counters. Rejected requests are counted
  separately from dispatched input, with durable duplicate receipt handling.
  Guest GTK sessions use stationary scrollbars to avoid cosmetic fade rejections.
- Identity-owned computers, explicit session ownership, named slots, expiring
  exclusive grants and takeover fencing. The trusted host owns attachment and renewal.
- Incus containers and VMs on local Linux or a private GCP host, with persistent
  disks, explicit image provisioning and generated lifecycle authority.
- Browser navigation, bounded semantic and visual observations, native Linux
  X11/AT-SPI accessibility, screenshots, shell, files and guest administration.
- Optional desktop images with Blender and other native apps; Blender can run
  Python and return visual evidence through normal computer tools.
- An encrypted realm credential store, reusable identity assignments and private
  delivery to approved browser fields without adding secret values to model inputs.
- Explicit same-host full-state VM forks, including one checkpoint for multiple
  child computers, with fresh authority and independent networking per child.
- A Python host client and stdio MCP adapter. The optional inline MCP App reads
  saved screenshots over the original connection with private preview metadata.
- An always-on GCP lifecycle gateway that wakes hosts on demand, serializes
  lifecycle effects and suspends idle hosts while preserving RAM state. Uncertain
  operations inhibit suspension until trusted reconciliation.
- Hosted public CI, isolated private validation and reproducible wheel/sdist checks, installed-package probes,
  source archives, target-specific operator binaries, checksums and build manifests.

### Compatibility and known limits

- SDK 0.9.0 can reject `open_ai_assistant_message` metadata when reading session
  history after a successful run. The normal computer-use example does not use
  that history API. `terminal_cause_kind` was null in the released-runtime
  exercises; token usage and tool-call counts were available.

- Native desktop access requires Linux X11/AT-SPI. macOS, Windows and Wayland
  desktop backends are not supplied.
- RAM forks require a compatible same-host Incus VM profile. Agent forks do not
  clone computers automatically; the embedding host calls the fork API.
- Inline Apps need compatible native Core/Meta builds and product policy,
  sandbox and reconnect wiring. They are not established by realm config alone.
- Historical native Console versions could briefly return 403 while committed
  invocation lineage becomes available after turn completion. The acceptance
  test checks eventual availability and records such intervals separately.
- HomeCore Discovery has live computer and host wake/idle evidence. Its deployed
  native inline preview remains unqualified; its current Console shows screenshot
  placeholders. This package does not deploy or upgrade HomeCore.
- Guest images resolve OS packages at build time. Record image fingerprints;
  wheel/sdist reproducibility does not imply reproducible OS images or Rust binaries.

See [compatibility](docs/reference/compatibility.mdx) for the full boundary and
[release instructions](RELEASING.md) for candidate validation and rollback.
