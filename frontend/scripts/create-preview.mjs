import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// A reviewable, self-contained artifact that opens without a local server.
const dist = fileURLToPath(new URL("../dist/", import.meta.url));
const asset = (url) =>
  readFileSync(path.join(dist, url.replace(/^\//, "")), "utf8");
// Binary assets (fonts/images) must be read without a text encoding, or
// their bytes get corrupted before they're re-encoded to base64.
const assetBinary = (url) => readFileSync(path.join(dist, url.replace(/^\//, "")));

const MIME_BY_EXT = {
  woff2: "font/woff2",
  woff: "font/woff",
  svg: "image/svg+xml",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
};
const mimeFor = (assetUrl) => {
  const ext = assetUrl.split(".").pop().toLowerCase();
  const mime = MIME_BY_EXT[ext];
  if (!mime) throw new Error(`Unknown asset extension for inlining: ${assetUrl}`);
  return mime;
};
const dataUriFor = (assetUrl) =>
  `data:${mimeFor(assetUrl)};base64,${assetBinary(assetUrl).toString("base64")}`;

// Vite emits CSS `url(/assets/<name>-<hash>.<ext>)` for assets over 4 KB
// (fonts, the hero photo, swoosh/dot/grid SVGs). Inline every one as a data
// URI so the preview makes zero network requests over file://.
const inlineCssAssetUrls = (css) =>
  css.replace(
    /url\(\s*(['"]?)(\/assets\/[^'")\s?#]+)\1\s*\)/g,
    (_, _quote, assetUrl) => `url("${dataUriFor(assetUrl)}")`,
  );
// Safety net in case a component ever references an asset path directly from
// TS/TSX as a quoted string literal instead of through CSS.
const inlineJsAssetLiterals = (js) =>
  js.replace(
    /(["'`])(\/assets\/[^"'`\s?#]+\.(?:woff2?|svg|png|jpe?g|webp))\1/g,
    (_, quote, assetUrl) => {
      try {
        return `${quote}${dataUriFor(assetUrl)}${quote}`;
      } catch {
        throw new Error(
          `Preview inlining: asset ${assetUrl} referenced in JS not found in dist/`,
        );
      }
    },
  );

const html = readFileSync(path.join(dist, "index.html"), "utf8")
  .replace(
    /<script\b[^>]*src="([^"]+)"[^>]*><\/script>/g,
    (_, url) =>
      `<script type="module">${inlineJsAssetLiterals(asset(url)).replace(/<\/script/gi, "<\\/script")}</script>`,
  )
  .replace(
    /<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g,
    (_, url) =>
      `<style>${inlineCssAssetUrls(asset(url)).replace(/<\/style/gi, "<\\/style")}</style>`,
  );
if (
  /\b(?:src|href)="\/assets\//.test(html) ||
  /url\(\s*['"]?\/assets\//.test(html) ||
  /https?:\/\/(fonts\.(googleapis|gstatic)\.com|cdn\.jsdelivr\.net)/.test(html)
)
  throw new Error("Preview still references external build assets.");
writeFileSync(path.join(dist, "preview.html"), html);
console.log("Created dist/preview.html — open directly in a browser.");
