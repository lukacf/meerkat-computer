# Contributing

The maintained [contributor guide](docs/development/contributing.mdx) covers
development, architecture and verification. Use Make for repository lanes and
`scripts/repo-cargo` for targeted Rust commands. Keep credentials and private
qualification outputs out of commits.

Start with the [repository overview](README.md#contributing). Behavioral changes use
failing tests first, then the smallest fix and the relevant verification lane.
Document public contracts in `docs/`, release-visible changes in
[CHANGELOG.md](CHANGELOG.md), and security concerns through [SECURITY.md](SECURITY.md).

## Local documentation

Use Node.js 22 or 24 LTS for the pinned Mintlify preview.

~~~bash
npm --prefix docs ci --ignore-scripts
npm --prefix docs run check
npm --prefix docs run dev
~~~

The checker compiles MDX and validates navigation, local links, anchors,
frontmatter and component names. It also checks repository Markdown entrypoints
and their relative links, including extracted source archives without Git.
The preview uses a pinned Mintlify CLI and
serves locally. It does not deploy a documentation website.

The site is organized in `docs/docs.json`. Author canonical guides there;
root how-to Markdown files remain entrypoints. Preserve the separate historical
[verification record](VERIFICATION.md) and [release procedure](RELEASING.md).
