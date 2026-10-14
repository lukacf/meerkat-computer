import { build } from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const directory = path.dirname(fileURLToPath(import.meta.url));
const result = await build({
  absWorkingDir: directory, entryPoints: ["preview.mjs"], bundle: true,
  write: false, format: "iife", platform: "browser", target: "es2022",
  charset: "ascii", minify: true, legalComments: "inline", metafile: true,
});
const packages = new Set(Object.keys(result.metafile.inputs).flatMap(input => {
  const match = input.match(/^(.*node_modules\/(?:@[^/]+\/)?[^/]+)\//);
  return match ? [match[1]] : [];
}));
const notices = [];
for (const location of [...packages].sort()) {
  const directoryPath = path.resolve(directory, location);
  const metadata = JSON.parse(await readFile(path.join(directoryPath, "package.json"), "utf8"));
  let found = false;
  for (const name of ["LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md", "NOTICE.txt"]) {
    try {
      const text = await readFile(path.join(directoryPath, name), "utf8");
      notices.push(`${metadata.name}@${metadata.version} - ${name}\n${text.replace(/[ \t]+$/gm, "")}`);
      found = true;
    } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  if (!found) throw new Error(`Missing bundled license for ${metadata.name}`);
}
const template = await readFile(path.join(directory, "preview.html"), "utf8");
const license = `/*! Bundled third-party licenses\n${notices.join("\n\n").replaceAll("*/", "* /")}\n*/\n`;
const script = (license + result.outputFiles[0].text).replace(/<\/script/gi, "<\\/script");
const html = template.replace("<!-- APP_SCRIPT -->", () => `<script>${script}</script>`);
const output = path.resolve(directory, "../../python/meerkat_computer/resources/preview-v1.html");
if (process.argv.includes("--check")) {
  if (await readFile(output, "utf8") !== html) throw new Error("Run make ui-build to refresh the MCP App");
  console.log("MCP App bundle is current");
} else {
  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(output, html);
  console.log("Built packaged MCP App preview");
}
