import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { checkRepositoryLinks } from "./repository-links.mjs";

async function fixture(t, files) {
  const root = await mkdtemp(path.join(os.tmpdir(), "computer-doc-links-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const [name, content] of Object.entries(files)) {
    const target = path.join(root, name);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content);
  }
  return root;
}

test("checks repository guides and relative anchors without Git", async t => {
  const root = await fixture(t, {
    "README.md": "[Guide](docs/guide.mdx#setup) [Code](src/) [Help][help]\n\n[help]: CONTRIBUTING.md\n",
    "CONTRIBUTING.md": "# Contributing\n",
    "docs/guide.mdx": '---\ntitle: "Guide"\n---\n\n## Setup\n',
    "src/lib.rs": "",
    "tests/README.md": "[Root](../README.md)\n",
  });
  const result = await checkRepositoryLinks(root);
  assert.deepEqual(result.failures, []);
  assert.equal(result.files, 3);
  assert.equal(result.links, 4);
});

test("fails on a broken target and a stale heading", async t => {
  const root = await fixture(t, {
    "README.md": "[Missing](missing.md) [Old heading](docs/guide.mdx#removed)\n",
    "docs/guide.mdx": "# Current\n",
  });
  const result = await checkRepositoryLinks(root);
  assert.equal(result.failures.length, 2);
  assert.match(result.failures.join("\n"), /missing target: missing.md/);
  assert.match(result.failures.join("\n"), /missing anchor: docs\/guide.mdx#removed/);
});

test("ignores code examples, external links and generated private files", async t => {
  const root = await fixture(t, {
    "README.md": "[Web](https://example.com)\n\n```md\n[fake](missing.md)\n```\n",
    ".state/README.md": "[Private](missing.md)\n",
    "node_modules/pkg/README.md": "[Dependency](missing.md)\n",
    "target/README.md": "[Build](missing.md)\n",
    ".github/PULL_REQUEST_TEMPLATE.md": "[Contribute](../CONTRIBUTING.md)\n",
    "CONTRIBUTING.md": "# Help\n",
  });
  const result = await checkRepositoryLinks(root);
  assert.deepEqual(result.failures, []);
  assert.equal(result.files, 3);
});

test("rejects traversal and private state links even when a target exists", async t => {
  const root = await fixture(t, {
    "README.md": "[Escape](../README.md) [Secret](.state/token) [Encoded](%2estate/token)\n",
    ".state/token": "fixture, not a credential",
  });
  const result = await checkRepositoryLinks(root);
  assert.equal(result.failures.length, 3);
  assert.ok(result.failures.every(message => /outside distributable source/.test(message)));
});
