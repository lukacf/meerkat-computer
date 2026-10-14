# Implementation contracts

The maintained contracts are in the documentation site:

- [Broker HTTP API](docs/reference/http-api.mdx)
- [Worker and backend protocol](docs/reference/worker-protocol.mdx)
- [Computer tools](docs/reference/tools.mdx)
- [Broker configuration](docs/reference/configuration.mdx)
- [Architecture and boundaries](docs/concepts/architecture.mdx)

The Rust broker owns lifecycle authority. The MCP adapter and worker protocol
project that authority; they do not independently choose computer ownership.
