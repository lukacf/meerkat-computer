#!/usr/bin/env node
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "@mdx-js/mdx";
import remarkGfm from "remark-gfm";
import { checkRepositoryLinks } from "./repository-links.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const failures = [];
const fail = (file, message) => failures.push(path.relative(root, file) + ": " + message);
const config = JSON.parse(await readFile(path.join(root, "docs.json"), "utf8"));
const routes = new Set();
const configured = [];
function collect(value) {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value.pages)) {
    for (const item of value.pages) {
      if (typeof item === "string") configured.push(item);
      else collect(item);
    }
  }
  for (const [key, child] of Object.entries(value)) {
    if (key !== "pages" && child && typeof child === "object") {
      if (Array.isArray(child)) child.forEach(collect);
      else collect(child);
    }
  }
}
collect(config.navigation);
if (!config.name || !config.theme || !config.colors?.primary || !configured.length) {
  fail(path.join(root, "docs.json"), "name, theme, primary color and navigation are required");
}
for (const route of configured) {
  if (routes.has(route)) fail(path.join(root, "docs.json"), "duplicate route " + route);
  if (!/^[a-z0-9/-]+$/.test(route)) fail(path.join(root, "docs.json"), "invalid route " + route);
  routes.add(route);
}
async function walk(directory) {
  const found = [];
  for (const entry of await readdir(directory, {withFileTypes:true})) {
    if (entry.name.startsWith(".") || entry.name === "node_modules") continue;
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) found.push(...await walk(file));
    else if (entry.isFile() && entry.name.endsWith(".mdx")) found.push(file);
  }
  return found;
}
const pages = new Map();
const links = [];
const components = new Set(["Accordion", "AccordionGroup", "Card", "CardGroup", "CodeGroup",
  "Note", "Warning", "Tip", "Info", "Check", "Steps", "Step", "Tabs", "Tab", "ParamField",
  "ResponseField", "Expandable", "Frame", "Snippet", "Update", "UpdateGroup", "Tooltip"]);
function words(node) {
  return typeof node.value === "string" ? node.value : (node.children ?? []).map(words).join("");
}
function slug(value) {
  return value.toLowerCase().trim().replace(/[^\p{L}\p{N}_\s-]/gu, "").replace(/\s/g, "-");
}
function inspect(node, file, anchors, seen) {
  if (node.type === "heading") {
    const base = slug(words(node)), occurrence = seen.get(base) ?? 0;
    seen.set(base, occurrence + 1);
    anchors.add(occurrence ? base + "-" + occurrence : base);
  }
  if (["link", "image", "definition"].includes(node.type)) links.push({file, url:node.url});
  if (node.type?.startsWith("mdxJsx")) {
    if (node.name && /^[A-Z]/.test(node.name) && !components.has(node.name)) {
      fail(file, "unknown documentation component " + node.name);
    }
    for (const attribute of node.attributes ?? []) {
      if (["href", "src"].includes(attribute.name) && typeof attribute.value === "string") {
        links.push({file, url:attribute.value});
      }
      if (attribute.name === "id" && typeof attribute.value === "string") anchors.add(attribute.value);
    }
  }
  for (const child of node.children ?? []) inspect(child, file, anchors, seen);
}
for (const file of await walk(root)) {
  const source = await readFile(file, "utf8");
  const route = path.relative(root, file).replace(/\\/g, "/").replace(/\.mdx$/, "");
  if (!routes.has(route)) fail(file, "page is absent from navigation");
  const header = source.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!header || !/^title:\s*".+"$/m.test(header[1]) || !/^description:\s*".+"$/m.test(header[1])) {
    fail(file, "quoted title and description frontmatter are required");
    continue;
  }
  if (/[\u2013\u2014]/u.test(source)) fail(file, "use ASCII hyphens");
  const anchors = new Set(), seen = new Map();
  const auditPlugin = () => tree => inspect(tree, file, anchors, seen);
  try {
    await compile(source.slice(header[0].length), {remarkPlugins:[remarkGfm, auditPlugin]});
  } catch (error) {
    fail(file, "invalid MDX: " + String(error.message).split("\n")[0]);
  }
  pages.set(route, {file, anchors});
}
for (const route of routes) {
  if (!pages.has(route)) fail(path.join(root, "docs.json"), "missing page " + route);
}
for (const asset of [config.logo?.light, config.logo?.dark, config.favicon]) {
  if (asset) links.push({file:path.join(root,"docs.json"), url:asset});
}
for (const {file, url} of links) {
  if (!url || /^(https?:|mailto:|data:)/.test(url)) continue;
  if (url.startsWith("//")) { fail(file, "use an explicit URL scheme"); continue; }
  const [pathname, fragment] = url.split("#");
  if (pathname.includes(".state") || pathname.includes("..")) {
    fail(file, "local links must stay inside the documentation: " + url); continue;
  }
  const fromRoute = path.relative(root, file).replace(/\\/g, "/").replace(/\.mdx$/, "");
  const route = pathname ? pathname.replace(/^\//, "").replace(/\.mdx$/, "") : fromRoute;
  const target = pages.get(route);
  if (target) {
    if (fragment && !target.anchors.has(decodeURIComponent(fragment))) fail(file, "missing anchor " + url);
    continue;
  }
  try {
    const candidate = path.resolve(root, route);
    if (!candidate.startsWith(root + path.sep) || !(await stat(candidate)).isFile()) throw Error();
  } catch { fail(file, "missing local target " + url); }
}
const repository = await checkRepositoryLinks(path.dirname(root));
failures.push(...repository.failures);
if (failures.length) {
  process.stderr.write(failures.join("\n") + "\n");
  process.exitCode = 1;
} else {
  process.stdout.write("Documentation valid: " + pages.size + " MDX pages, " +
    links.length + " links/assets, " + routes.size + " navigation entries; " +
    repository.files + " repository Markdown files and " + repository.links + " links.\n");
}
