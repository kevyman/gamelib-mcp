#!/usr/bin/env node
// Screenshot widget preview pages (scripts/preview_*.py output) with headless
// Chromium, one full-page PNG per file x width (x scheme), and print each
// page's measured content height (not floored at the 800px viewport) so
// layout changes show up as numbers.
//
//   node scripts/screenshot_widgets.mjs --out DIR [--img-cache DIR]
//        [--width 360,760] [--scheme light,dark] FILE.html...
//
// Output: DIR/<stem>-<width>.png (or <stem>-<scheme>-<width>.png when more
// than one --scheme is given), and one line per shot:
//   <name> <width> height=<px> scrollWidth=<px>
//
// --scheme emulates prefers-color-scheme (for previews rendered without
// --theme); without it Chromium's default (light) applies.
// --img-cache serves image requests from a local directory instead of the
// network (headless runs are often offline): a request maps to a cache file by
// its basename, then cov_<basename>, then got_<basename>; a YouTube poster
// (i.ytimg.com/vi/<vid>/hqdefault.jpg) by <vid>_hqdefault.jpg plus two known
// aliases. A miss is answered with a 1x1 grey PNG so the layout stays
// measurable. Nothing else is stubbed.
//
// Uses the `playwright` module if it resolves (globally or via NODE_PATH); it
// is deliberately not a project dependency.

import { createRequire } from "node:module";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { basename, extname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);

function usage(message) {
  if (message) console.error(`screenshot_widgets: ${message}`);
  console.error(
    "usage: node scripts/screenshot_widgets.mjs --out DIR [--img-cache DIR] " +
      "[--width 360,760] [--scheme light,dark] FILE.html...",
  );
  process.exit(2);
}

function parseArgs(argv) {
  const opts = { out: null, imgCache: null, widths: [360, 760], schemes: [], files: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const value = () => {
      if (i + 1 >= argv.length) usage(`${arg} needs a value`);
      return argv[++i];
    };
    if (arg === "--out") opts.out = value();
    else if (arg === "--img-cache") opts.imgCache = value();
    else if (arg === "--width") {
      opts.widths = value().split(",").map((w) => Number.parseInt(w, 10));
      if (opts.widths.some((w) => !(w > 0))) usage("--width takes positive integers, e.g. 360,760");
    } else if (arg === "--scheme") {
      opts.schemes = value().split(",");
      if (opts.schemes.some((s) => s !== "light" && s !== "dark")) usage("--scheme takes light and/or dark");
    } else if (arg === "-h" || arg === "--help") usage();
    else if (arg.startsWith("--")) usage(`unknown flag ${arg}`);
    else opts.files.push(arg);
  }
  if (!opts.out) usage("--out is required");
  if (!opts.files.length) usage("no preview files given");
  if (opts.imgCache && !existsSync(opts.imgCache)) usage(`--img-cache ${opts.imgCache} does not exist`);
  return opts;
}

let playwright;
try {
  playwright = require("playwright");
} catch {
  console.error(
    "screenshot_widgets: cannot require('playwright') — install it outside the project " +
      "(npm i -g playwright && npx playwright install chromium) or point NODE_PATH at it",
  );
  process.exit(1);
}

// 1x1 #bdbdbd PNG.
const GREY_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYu3cvAARyAji4ckwGAAAAAElFTkSuQmCC",
  "base64",
);
const YT_ALIASES = { rJrqHyojaa4: "hqdefault.jpg", k3CqzUCNDjU: "got_poster.jpg" };
const CONTENT_TYPES = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif" };

function cacheCandidates(url) {
  const name = basename(url.pathname);
  if (url.hostname === "i.ytimg.com") {
    const match = url.pathname.match(/^\/vi\/([^/]+)\/([^/]+)$/);
    if (match) {
      const out = [`${match[1]}_${match[2]}`];
      if (YT_ALIASES[match[1]]) out.push(YT_ALIASES[match[1]]);
      return out;
    }
  }
  return [name, `cov_${name}`, `got_${name}`];
}

function isImageRequest(request, url) {
  return (
    request.resourceType() === "image" ||
    url.hostname === "images.igdb.com" ||
    url.hostname === "i.ytimg.com" ||
    /\.(jpe?g|png|webp|gif)$/i.test(url.pathname)
  );
}

async function routeImages(page, cacheDir) {
  await page.route(/^https?:\/\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!isImageRequest(request, url)) return route.continue();
    for (const candidate of cacheCandidates(url)) {
      const file = join(cacheDir, candidate);
      if (candidate && existsSync(file)) {
        const type = CONTENT_TYPES[extname(file).toLowerCase()] || "application/octet-stream";
        return route.fulfill({ status: 200, contentType: type, body: readFileSync(file) });
      }
    }
    return route.fulfill({ status: 200, contentType: "image/png", body: GREY_PNG });
  });
}

async function settle(page) {
  // Unreachable non-image requests must not hang the run: networkidle is a
  // best effort after load, then a beat for entrance motion to finish.
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
  await page.evaluate(() => (document.fonts ? document.fonts.ready : null));
  await page.waitForTimeout(800);
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  mkdirSync(opts.out, { recursive: true });
  const schemes = opts.schemes.length ? opts.schemes : [null];
  const browser = await playwright.chromium.launch();
  let failures = 0;
  try {
    for (const file of opts.files) {
      const stem = basename(file, extname(file));
      for (const scheme of schemes) {
        for (const width of opts.widths) {
          const name = schemes.length > 1 ? `${stem}-${scheme}` : stem;
          const context = await browser.newContext({
            viewport: { width, height: 800 },
            deviceScaleFactor: 1,
            ...(scheme ? { colorScheme: scheme } : {}),
          });
          const page = await context.newPage();
          // A widget that throws while starting up still lays out and screenshots
          // fine, so a page error has to count as a failure or the run (and
          // render_all_previews.sh) would report success for a broken widget.
          page.on("pageerror", (err) => {
            failures++;
            console.error(`${name} ${width} pageerror: ${err.message}`);
          });
          try {
            if (opts.imgCache) await routeImages(page, resolve(opts.imgCache));
            await page.goto(pathToFileURL(resolve(file)).href, { waitUntil: "load", timeout: 30000 });
            await settle(page);
            // A scrolled studio timeline must open on a whole mini: nothing in
            // the strip may end within 8px of its left edge (a clipped sliver
            // of the previous card, the artefact the owner flagged).
            const slivers = await page.evaluate(() =>
              [...document.querySelectorAll(".timeline")].flatMap((strip) => {
                const left = strip.getBoundingClientRect().left;
                return [...strip.querySelectorAll("*")]
                  .map((node) => node.getBoundingClientRect())
                  .filter((r) => r.width > 0 && r.right > left && r.right < left + 8)
                  .map((r) => `right=${r.right.toFixed(1)} strip.left=${left.toFixed(1)}`);
              }),
            );
            if (slivers.length) {
              failures++;
              console.error(`${name} ${width} timeline sliver at the left edge: ${slivers.join("; ")}`);
            }
            // The content's own height (what documentElement.scrollHeight,
            // the widgets' size-changed figure, reads in an auto-sized host
            // frame) — scrollHeight here would be floored at the viewport.
            const { height, scrollWidth } = await page.evaluate(() => {
              const body = document.body;
              const bottom = body.getBoundingClientRect().bottom + window.scrollY;
              return {
                height: Math.ceil(bottom + Number.parseFloat(getComputedStyle(body).marginBottom || "0")),
                scrollWidth: document.documentElement.scrollWidth,
              };
            });
            await page.screenshot({ path: join(opts.out, `${name}-${width}.png`), fullPage: true });
            console.log(`${name} ${width} height=${height} scrollWidth=${scrollWidth}`);
          } catch (err) {
            failures++;
            console.error(`${name} ${width} failed: ${err.message}`);
          } finally {
            await context.close();
          }
        }
      }
    }
  } finally {
    await browser.close();
  }
  process.exit(failures ? 1 : 0);
}

main().catch((err) => {
  console.error(`screenshot_widgets: ${err.stack || err}`);
  process.exit(1);
});
