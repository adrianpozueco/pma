import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// A reviewable, self-contained artifact that opens without a local server.
const dist = fileURLToPath(new URL("../dist/", import.meta.url));
const asset = (url) =>
  readFileSync(path.join(dist, url.replace(/^\//, "")), "utf8");
const html = readFileSync(path.join(dist, "index.html"), "utf8")
  .replace(
    /<script\b[^>]*src="([^"]+)"[^>]*><\/script>/g,
    (_, url) =>
      `<script type="module">${asset(url).replace(/<\/script/gi, "<\\/script")}</script>`,
  )
  .replace(
    /<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g,
    (_, url) =>
      `<style>${asset(url).replace(/<\/style/gi, "<\\/style")}</style>`,
  );
if (/\b(?:src|href)="\/assets\//.test(html))
  throw new Error("Preview still references external build assets.");
writeFileSync(path.join(dist, "preview.html"), html);
console.log("Created dist/preview.html — open directly in a browser.");
