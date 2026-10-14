import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { createProcessor } from "@mdx-js/mdx";
import remarkGfm from "remark-gfm";

const excluded = new Set(["node_modules", "target", "__pycache__"]);
const parser = createProcessor({ format: "md", remarkPlugins: [remarkGfm] });
const walkNodes = (node, visit) => {
  visit(node);
  for (const child of node.children ?? []) walkNodes(child, visit);
};
const words = node => node.value ?? (node.children ?? []).map(words).join("");
const slug = value => value.toLowerCase().trim()
  .replace(/[^\p{L}\p{N}_\s-]/gu, "").replace(/\s/g, "-");

async function markdownFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (excluded.has(entry.name) || (entry.name.startsWith(".") && entry.name !== ".github")) continue;
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await markdownFiles(file));
    else if (entry.isFile() && entry.name.endsWith(".md")) files.push(file);
  }
  return files;
}

export async function checkRepositoryLinks(root) {
  root = path.resolve(root);
  const files = await markdownFiles(root);
  const failures = [], links = [], anchorCache = new Map();
  const fail = (file, message) => failures.push(path.relative(root, file) + ": " + message);
  for (const file of files) {
    const source = await readFile(file, "utf8");
    if (/[\u2013\u2014]/u.test(source)) fail(file, "use ASCII hyphens");
    walkNodes(parser.parse(source), node => {
      if (["link", "image", "definition"].includes(node.type)) links.push({ file, url: node.url });
    });
  }
  for (const { file, url } of links) {
    if (/^(https?:|mailto:|data:)/.test(url)) continue;
    let pathname, fragment;
    try {
      const split = url.split("#");
      pathname = decodeURIComponent(split[0].split("?")[0]);
      fragment = split[1] ? decodeURIComponent(split[1]) : "";
    } catch {
      fail(file, "malformed link: " + url);
      continue;
    }
    const target = pathname ? path.resolve(path.dirname(file), pathname) : file;
    const relative = path.relative(root, target);
    if (relative.startsWith(".." + path.sep) || relative === ".." || path.isAbsolute(relative)
        || relative.split(path.sep).some(part =>
          excluded.has(part) || (part.startsWith(".") && part !== ".github"))) {
      fail(file, "link is outside distributable source: " + url);
      continue;
    }
    let info;
    try { info = await stat(target); }
    catch { fail(file, "missing target: " + url); continue; }
    if (!fragment || !info.isFile() || !/\.mdx?$/.test(target)) continue;
    if (!anchorCache.has(target)) {
      const anchors = new Set(), seen = new Map();
      const source = (await readFile(target, "utf8")).replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "");
      walkNodes(parser.parse(source), node => {
        if (node.type !== "heading") return;
        const base = slug(words(node)), count = seen.get(base) ?? 0;
        seen.set(base, count + 1);
        anchors.add(count ? base + "-" + count : base);
      });
      anchorCache.set(target, anchors);
    }
    if (!anchorCache.get(target).has(fragment)) fail(file, "missing anchor: " + url);
  }
  return { failures, files: files.length, links: links.length };
}
