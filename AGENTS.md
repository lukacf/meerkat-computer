# Meerkat Computer

Use ASCII hyphens. Meerkat owns agent identity, sessions, models and execution.
This optional feature owns computer assignments, control grants, backend effects
and observations. Computer Lab owns benchmark tasks, evaluators, APS and game moves.

Identity ownership is the default. Session ownership is explicit. A runtime
generation or model is never a durable computer owner. Identity references must
come from the trusted host, not tool arguments. Grants are exclusive, expiring and
fenced; a fork needs its own host-issued binding. Never replay an uncertain effect.

Use Make for build/check/test/lint and scripts/repo-cargo for targeted Rust work.
Default tests are local and require no cloud credentials. Keep live Incus/GCP
checks explicit. Never print or commit credentials. Preserve neighboring repos.

Keep one owner for lifecycle decisions. Protocol adapters must be mechanical.
Use the Meerkat machine DSL for generated lifecycle authority. Do not introduce
a parallel handwritten transition table or claim mock tests prove live cloud use.
