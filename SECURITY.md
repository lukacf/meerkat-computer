# Security

## Report a vulnerability privately

Report a suspected vulnerability through
[GitHub private vulnerability reporting](https://github.com/lukacf/meerkat-computer/security/advisories/new),
or contact the repository owner through an existing trusted private channel.
Do not report vulnerabilities in public issues. Do not post credentials, browser profiles,
screenshots with private content, bindings or raw environment files. Include the
affected commit or artifact version, the violated boundary and a minimal redacted
reproduction. Coordinate any disclosure with the maintainer before sharing it
outside the repository.

## Supported scope

The current line is the 0.1.0 candidate. Use its exact commit and artifact
manifest when reporting a problem. There is no long-term support or response-time
commitment for historical snapshots. Follow [RELEASING.md](RELEASING.md) for
upgrades and rollback, and retain the previous verified artifacts and config.

Profiles can deliberately grant agents administrator access inside their own guests. The
security boundary is the guest, broker and trusted host, not a promise that a
model can only affect a browser tab. Incus containers share the host kernel;
choose a VM profile when your workload requires a separate guest kernel.

The trusted application owns identity, assignment, capabilities, credential
policy and reconnects. Keep operator and gateway host credentials outside model
arguments and guests. Bind control grants to the intended execution, expire and
revoke them through the broker, and do not replay an uncertain action.

Expose the broker only through the intended private network or authenticated
tunnel. Guest-root administration must not imply access to the host, neighboring
computers or their credentials. The included tests and qualification records
document specific checks, not a general security certification.

See [security and administrator access](docs/operations/security.mdx),
[credential delivery](docs/guides/credentials.mdx) and
[host lifecycle](docs/operations/host-lifecycle.mdx) for the enforced contracts
and operator responsibilities.
