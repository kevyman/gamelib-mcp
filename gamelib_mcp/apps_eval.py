"""MCP Apps (io.modelcontextprotocol/ui): the evaluation-card widget.

One `ui://` resource renders `record_assessment` results: the full
evaluation package when the response carries one (hero trailer/screenshots,
verdict stamp, why-care eyebrow lines, craft/fit scores, for-you-if bullets,
anchors, lineage, similar games, the "from the studio" pedigree strip,
time/price, flags, past verdicts), and a compact note card for the
bookkeeping-only responses (a plain recorded verdict, or a void).
Clients that don't speak the Apps extension ignore the tool metadata and see
the normal JSON, so attaching ``EVAL_CARD_APP`` to a tool is purely additive.

Every section of the package is optional: an unowned candidate with no appid
and no IGDB match gets a media-less, similar-less card, and each block is
skipped rather than rendered empty.

Visual language is the game-cards widget's "toybox" (see apps.py): thick ink
borders, hard offset shadows, chunky type, pastel stickers, the same CSS
custom-property palette and dark scheme. The verdict is a big rotated stamp
in that same language — buy_now green, wishlist_for_sale amber, try_demo
blue, play_what_you_own pink, skip red.

Media routing follows the 2026-08-28 spike (docs/plans/…-evaluation-package
-design.md): a Steam trailer is a plain ``<video controls preload="none">``
over the constructed legacy mp4 renditions — undocumented Valve surface, so
the widget falls back to poster + link-out on the media error event — while
an IGDB-only candidate gets a click-to-load youtube-nocookie iframe (no
YouTube bytes before the click) plus a link-out pill, because a CSP-blocked
nested frame is not detectable from JS.

The HTML is deliberately dependency-free: the host↔iframe bridge is the
~40-line JSON-RPC postMessage handshake from the MCP Apps spec
(ui/initialize → ui/notifications/initialized → ui/notifications/tool-result)
rather than @modelcontextprotocol/ext-apps, so nothing is fetched from a CDN
and the CSP only has to allow the media hosts below.

For local visual iteration outside any MCP host, the widget renders
``window.__PREVIEW_DATA__`` when present instead of waiting on the bridge —
see scripts/preview_eval_card.py.
"""

import hashlib
from typing import Any

from fastmcp.apps import AppConfig, ResourceCSP

# Media hosts. Per the MCP Apps spec, resource_domains feeds img-src,
# media-src, script-src, style-src and font-src in the host's iframe CSP,
# while frame_domains feeds frame-src — the spike confirmed both, and
# `["https://www.youtube.com"]` is the spec's own frameDomains example.
# Covers: IGDB art, Steam capsules (cdn.*), Steam screenshots and movie
# posters (shared.*, which is where appdetails actually serves them), and
# YouTube thumbnails for IGDB trailers. Everything else stays deny-by-default.
_EVAL_CARD_CSP = ResourceCSP(
    resource_domains=[
        "https://images.igdb.com",
        "https://cdn.cloudflare.steamstatic.com",
        "https://cdn.akamai.steamstatic.com",
        "https://shared.akamai.steamstatic.com",
        "https://shared.cloudflare.steamstatic.com",
        "https://i.ytimg.com",
    ],
    frame_domains=["https://www.youtube-nocookie.com"],
)

EVAL_CARD_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #f5efe2;
    --card: #fffdf6;
    --ink: #17140e;
    --shadow-c: #17140e;
    --muted: #6d6553;
    --good: #1a7f37;
    --ok: #96650a;
    --bad: #b3223c;
    --p1: #cfe6ff;
    --p2: #ffe0b8;
    --p3: #d8f2c4;
    --p4: #ffd6e7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #191610;
      --card: #241f15;
      --ink: #ece5d3;
      --shadow-c: #000000;
      --muted: #a89d86;
      --good: #7ee2a0;
      --ok: #ffd66b;
      --bad: #ff9eb0;
      --p1: #274a68;
      --p2: #6d4d1e;
      --p3: #3d5c2a;
      --p4: #6e3350;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--ink);
    padding: 16px;
    -webkit-font-smoothing: antialiased;
  }
  .eval {
    max-width: 760px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .panel {
    background: var(--card);
    border: 2px solid var(--ink);
    border-radius: 14px;
    box-shadow: 5px 5px 0 var(--shadow-c);
    padding: 16px;
  }
  .section-title {
    font-size: 10.5px;
    font-weight: 800;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 9px;
  }
  .note { font-size: 11.5px; font-weight: 650; color: var(--muted); margin-top: 8px; }
  .empty {
    color: var(--muted);
    font-size: 13px;
    font-weight: 650;
    padding: 20px;
    text-align: center;
  }

  /* ---- shared cover block (same shape as the game-cards widget) ---- */
  .cover-wrap { position: relative; aspect-ratio: 2 / 3; }
  .cover-wrap img, .cover-fallback {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .cover-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: 800;
    line-height: 1.3;
    color: rgba(255, 255, 255, 0.92);
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
    padding: 8px;
    text-align: center;
    overflow: hidden;
    overflow-wrap: anywhere;
  }

  /* ---- hero (trailer / lead screenshot) ---- */
  .hero {
    position: relative;
    border: 2px solid var(--ink);
    border-radius: 14px;
    box-shadow: 5px 5px 0 var(--shadow-c);
    overflow: hidden;
    background: #0d0b07;
    aspect-ratio: 16 / 9;
  }
  .hero-media {
    display: block;
    width: 100%;
    height: 100%;
    border: 0;
    object-fit: cover;
    background: #0d0b07;
  }
  .play-badge {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(12, 10, 6, 0.3);
    border: 0;
    padding: 0;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .play-badge span {
    width: 62px;
    height: 62px;
    border-radius: 999px;
    border: 3px solid var(--ink);
    background: var(--card);
    color: var(--ink);
    box-shadow: 3px 3px 0 var(--shadow-c);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    font-weight: 900;
    padding-left: 5px;
    transition: transform 0.12s ease;
  }
  .play-badge:hover span, .play-badge:focus-visible span { transform: scale(1.06); }
  .hero-pill {
    position: absolute;
    right: 10px;
    bottom: 10px;
    z-index: 2;
    font-size: 10.5px;
    font-weight: 800;
    padding: 3px 9px;
    border-radius: 999px;
    border: 2px solid var(--ink);
    background: var(--card);
    color: var(--ink);
    box-shadow: 2px 2px 0 var(--shadow-c);
    cursor: pointer;
  }
  .hero-missing {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    color: var(--muted);
    font-size: 12px;
    font-weight: 650;
  }

  /* ---- header ---- */
  .head { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
  .head .cover-wrap {
    flex: 0 0 104px;
    border: 2px solid var(--ink);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 4px 4px 0 var(--shadow-c);
    transform: rotate(-1.5deg);
    margin: 4px 6px 8px 2px;
  }
  .head-info { flex: 1 1 220px; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
  .head-info h1 {
    font-size: 21px;
    font-weight: 800;
    line-height: 1.15;
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }
  .sub { font-size: 12.5px; font-weight: 650; color: var(--muted); }
  .one-liner { font-size: 14px; font-weight: 700; line-height: 1.45; margin-top: 14px; }
  .pitch {
    margin-top: 10px;
    font-size: 13px;
    line-height: 1.55;
    font-style: italic;
    background: var(--p1);
    border: 2px solid var(--ink);
    border-left-width: 6px;
    border-radius: 0 12px 12px 0;
    padding: 10px 13px;
  }
  .stamp {
    flex: 0 0 auto;
    align-self: flex-start;
    margin: 4px 2px 0 0;
    font-size: 15px;
    font-weight: 900;
    letter-spacing: 0.03em;
    line-height: 1.1;
    text-align: center;
    max-width: 168px;
    padding: 9px 13px;
    border: 3px solid var(--ink);
    border-radius: 12px;
    box-shadow: 4px 4px 0 var(--shadow-c);
    transform: rotate(-3.5deg);
  }
  .stamp-good { background: var(--good); color: var(--card); }
  .stamp-ok { background: var(--ok); color: var(--card); }
  .stamp-bad { background: var(--bad); color: var(--card); }
  .stamp-p1 { background: var(--p1); color: var(--ink); }
  .stamp-p4 { background: var(--p4); color: var(--ink); }

  /* ---- chips / strips ---- */
  .chips { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 11.5px;
    font-weight: 750;
    padding: 3px 10px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--card);
    color: var(--ink);
    white-space: nowrap;
  }
  .chip .lbl { font-size: 9.5px; font-weight: 800; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
  .chip.oc { font-weight: 800; }
  .oc-mighty { background: #fc430a; color: #17140e; }
  .oc-strong { background: #9e00b4; color: #ffffff; }
  .oc-fair { background: #4aa1ce; color: #17140e; }
  .oc-weak { background: #80b06a; color: #17140e; }
  .fit-good { background: var(--good); color: var(--card); }
  .fit-ok { background: var(--p3); }
  .fit-mid { background: var(--p2); }
  .fit-bad { background: var(--bad); color: var(--card); }
  .traj-good { color: var(--good); }
  .traj-flat { color: var(--muted); }
  .traj-bad { color: var(--bad); }
  .meter {
    width: 64px;
    height: 8px;
    border: 1.5px solid var(--ink);
    border-radius: 999px;
    background: var(--card);
    overflow: hidden;
    display: inline-block;
    flex: none;
  }
  .meter-fill { display: block; height: 100%; background: var(--muted); }
  .craft-hi .meter-fill { background: var(--good); }
  .craft-mid .meter-fill { background: var(--ok); }
  .craft-lo .meter-fill { background: var(--bad); }
  .flag {
    font-size: 11.5px;
    font-weight: 750;
    padding: 3px 10px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--bad);
    color: var(--card);
    transform: rotate(-1deg);
  }
  .flag:nth-child(2n) { transform: rotate(1.2deg); }
  .strip {
    display: flex;
    gap: 10px;
    overflow-x: auto;
    padding: 2px 2px 6px;
    scrollbar-width: thin;
  }
  .shot {
    flex: none;
    padding: 0;
    border: 2px solid var(--ink);
    border-radius: 9px;
    overflow: hidden;
    background: var(--card);
    box-shadow: 2px 2px 0 var(--shadow-c);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    transition: transform 0.12s ease;
  }
  .shot img { display: block; width: 188px; height: 106px; object-fit: cover; }
  .shot:hover { transform: translate(-1px, -1px); }
  .shot:focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
  .more-chip {
    flex: none;
    align-self: center;
    font-size: 11px;
    font-weight: 800;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p2);
    white-space: nowrap;
  }

  /* ---- for you / not for you ---- */
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .col-title { font-size: 12px; font-weight: 800; margin-bottom: 6px; }
  .col.yes .col-title { color: var(--good); }
  .col.no .col-title { color: var(--bad); }
  .bullets { list-style: none; display: flex; flex-direction: column; gap: 6px; }
  .bullets li { display: flex; gap: 7px; font-size: 12.5px; line-height: 1.45; }
  .tick { font-weight: 900; flex: none; }
  .col.yes .tick { color: var(--p3); -webkit-text-stroke: 0.6px var(--ink); }
  .col.no .tick { color: var(--p4); -webkit-text-stroke: 0.6px var(--ink); }

  /* ---- anchors ---- */
  .anchor {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 11.5px;
    font-weight: 700;
    padding: 3px 10px 3px 3px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p3);
    box-shadow: 2px 2px 0 var(--shadow-c);
  }
  .anchor.no-cover { padding-left: 10px; }
  .anchor-cover {
    width: 20px;
    height: 30px;
    border-radius: 999px;
    object-fit: cover;
    border: 1.5px solid var(--ink);
    flex: none;
  }
  .anchor .dim { color: var(--muted); font-weight: 650; }
  .anchor.an-warn { background: var(--bad); color: var(--card); }
  .anchor.an-warn .dim { color: var(--card); opacity: 0.8; }

  /* ---- lineage / comparisons ---- */
  .callout {
    display: flex;
    flex-direction: column;
    gap: 3px;
    border: 2px solid var(--ink);
    border-radius: 12px;
    background: var(--p2);
    box-shadow: 3px 3px 0 var(--shadow-c);
    padding: 10px 12px;
    margin-bottom: 10px;
  }
  .callout-head { font-size: 12.5px; font-weight: 800; }
  .callout-note { font-size: 12px; line-height: 1.45; }
  .lineage { display: flex; gap: 10px; align-items: stretch; flex-wrap: wrap; }
  .lin-col { flex: 1 1 150px; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
  .lin-head {
    font-size: 9.5px;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .lin-arrow { align-self: center; font-size: 17px; font-weight: 900; color: var(--muted); }
  .comp {
    border: 1.5px solid var(--ink);
    border-radius: 10px;
    background: var(--card);
    padding: 7px 9px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .comp-name { font-size: 12.5px; font-weight: 800; line-height: 1.25; overflow-wrap: anywhere; }
  .comp-note { font-size: 11.5px; line-height: 1.4; color: var(--muted); }
  .comp.this-game { background: var(--p1); border-width: 2px; box-shadow: 3px 3px 0 var(--shadow-c); }
  .tags { display: flex; gap: 5px; flex-wrap: wrap; }
  .tag {
    font-size: 9.5px;
    font-weight: 800;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p3);
    white-space: nowrap;
  }
  .tag.owned { background: var(--good); color: var(--card); }
  .tag.unplayed { background: var(--p2); }
  /* Pedigree badges: HIS rating wins the one slot, the critic score only
     stands in when he has none — the studio strip is about his history. */
  .tag.rated { background: var(--good); color: var(--card); }
  .tag.critic { background: var(--card); color: var(--muted); }

  /* ---- "From the studio" (pedigree) ---- */
  .ped-head { font-size: 12.5px; font-weight: 800; line-height: 1.35; }
  .ped-pub { font-size: 11.5px; font-weight: 650; color: var(--muted); margin-top: 3px; }
  .ped-strip { margin-top: 9px; }

  /* ---- why care (model-authored eyebrow lines) ---- */
  .why-care { display: flex; flex-direction: column; gap: 7px; margin-top: 12px; }
  .wc-line { display: flex; gap: 8px; align-items: baseline; font-size: 12.5px; line-height: 1.45; }
  .wc-eyebrow {
    flex: none;
    font-size: 9.5px;
    font-weight: 800;
    letter-spacing: 0.06em;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    white-space: nowrap;
  }
  .wc-people { background: var(--p1); }
  .wc-studio { background: var(--p3); }
  .wc-hype { background: var(--p2); }
  .wc-moment { background: var(--p4); }

  /* ---- similar games ---- */
  .sim {
    flex: none;
    width: 108px;
    border: 2px solid var(--ink);
    border-radius: 11px;
    background: var(--card);
    box-shadow: 3px 3px 0 var(--shadow-c);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .sim .cover-wrap { border-bottom: 2px solid var(--ink); }
  .sim-body { padding: 7px 8px 8px; display: flex; flex-direction: column; gap: 4px; }
  .sim-name {
    font-size: 11.5px;
    font-weight: 800;
    line-height: 1.25;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .sim-year { font-size: 10.5px; font-weight: 650; color: var(--muted); }

  /* ---- past verdicts / errors ---- */
  .timeline { display: flex; gap: 7px; flex-wrap: wrap; }
  .tl-chip {
    font-size: 11px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 999px;
    border: 1.5px dashed var(--ink);
    background: var(--card);
    color: var(--muted);
    white-space: nowrap;
  }
  .errfoot { font-size: 11px; font-weight: 650; color: var(--muted); text-align: center; }
  .note-card { display: flex; gap: 12px; align-items: center; max-width: 560px; margin: 0 auto; }
  .note-text { font-size: 14px; font-weight: 750; line-height: 1.4; overflow-wrap: anywhere; }
  .note-card .stamp { font-size: 12px; max-width: 130px; padding: 7px 10px; margin: 0; }

  /* ---- click-to-enlarge overlay ---- */
  /* Anchored near the clicked thumbnail rather than centered in the (possibly
     very tall) iframe — hosts that don't auto-scroll to modals would otherwise
     open it off-screen. JS sets the panel's top and the overlay's height to
     span the whole document. */
  .overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 10;
    background: rgba(12, 10, 6, 0.5);
    opacity: 0;
    transition: opacity 0.16s ease;
  }
  .overlay.open { opacity: 1; }
  .overlay-panel {
    position: absolute;
    left: 50%;
    width: calc(100% - 28px);
    max-width: 900px;
    border: 2px solid var(--ink);
    border-radius: 14px;
    background: var(--card);
    overflow: hidden;
    transform: translateX(-50%) scale(0.93) translateY(12px);
    transition: transform 0.19s ease;
  }
  .overlay.open .overlay-panel { transform: translateX(-50%); }
  .shot-full { display: block; width: 100%; height: auto; }
  .overlay-close {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 2;
    width: 36px;
    height: 36px;
    border-radius: 999px;
    border: 2px solid var(--ink);
    background: var(--card);
    color: var(--ink);
    font: 800 16px/1 system-ui, sans-serif;
    box-shadow: 2px 2px 0 var(--shadow-c);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .toast {
    position: fixed;
    left: 50%;
    bottom: 14px;
    z-index: 20;
    transform: translateX(-50%) translateY(8px);
    max-width: calc(100% - 28px);
    padding: 8px 12px;
    background: var(--card);
    border: 2px solid var(--ink);
    border-radius: 10px;
    box-shadow: 3px 3px 0 var(--shadow-c);
    font-size: 11.5px;
    font-weight: 650;
    text-align: center;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.15s ease, transform 0.15s ease;
  }
  .toast.show { opacity: 1; transform: translateX(-50%); }

  @media (max-width: 560px) {
    body { padding: 12px; }
    .stamp { font-size: 13px; }
  }
  @media (max-width: 480px) {
    .two-col { grid-template-columns: 1fr; }
    .lin-arrow { display: none; }
    .head .cover-wrap { flex-basis: 84px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .overlay, .overlay-panel, .shot, .play-badge span { transition: none; }
    .shot:hover { transform: none; }
    .play-badge:hover span, .play-badge:focus-visible span { transform: none; }
  }
</style>
</head>
<body>
<div id="root"><div class="empty">Waiting for the evaluation…</div></div>
<script>
(function () {
  "use strict";

  /* ---------- MCP Apps bridge (spec 2026-01-26, hand-rolled) ---------- */
  var nextId = 1;
  var pending = {};
  function post(msg) { window.parent.postMessage(msg, "*"); }
  function request(method, params) {
    return new Promise(function (resolve) {
      var id = nextId++;
      pending[id] = resolve;
      post({ jsonrpc: "2.0", id: id, method: method, params: params });
    });
  }
  function notify(method, params) {
    post({ jsonrpc: "2.0", method: method, params: params || {} });
  }
  window.addEventListener("message", function (ev) {
    var m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined && m.method === undefined) {          // response
      var cb = pending[m.id];
      if (cb) { delete pending[m.id]; cb(m.error ? undefined : m.result); }
      return;
    }
    if (m.method === "ui/notifications/tool-result") {
      handleToolResult(m.params);
      return;
    }
    if (m.id !== undefined) {                                    // unknown host request
      post({ jsonrpc: "2.0", id: m.id,
             error: { code: -32601, message: "Method not found" } });
    }
  });
  /* External links. The sandbox usually lacks allow-popups, so window.open
     and target=_blank fail silently (Firefox included) — the reliable route
     is the host's ui/open-link. Try native only when the host didn't declare
     openLinks (synchronously, inside the click gesture), then always fall
     through to ui/open-link even undeclared: hosts often implement it
     without declaring it, and an unsupporting one just answers
     method-not-found — which we surface as a hint instead of silence. */
  var hostCaps = {};
  function openLink(url) {
    if (!url) return;
    if (!hostCaps.openLinks) {
      try { if (window.open(url, "_blank", "noopener")) return; } catch (e) { /* sandboxed */ }
    }
    Promise.race([
      request("ui/open-link", { url: url }),
      new Promise(function (resolve) { setTimeout(function () { resolve("timeout"); }, 2500); }),
    ]).then(function (res) {
      if (res === undefined) flashLinkHint(); // explicit host error
    });
  }
  var hintTimer = null;
  function flashLinkHint() {
    var t = document.querySelector(".toast");
    if (!t) t = document.body.appendChild(el("div", "toast",
      "This host blocked the link — right-click the pill and choose “Open in new tab”."));
    t.classList.add("show");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { t.classList.remove("show"); }, 3200);
  }
  function resultData(result) {
    var data = result && result.structuredContent;
    if (!data && result && result.content) {
      var text = (result.content.find(function (c) { return c.type === "text"; }) || {}).text;
      try { data = JSON.parse(text); } catch (e) { /* leave undefined */ }
    }
    return data;
  }
  function handleToolResult(result) {
    var data = resultData(result);
    if (data) render(data);
  }

  /* ---------- small helpers ---------- */
  var root = document.getElementById("root");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function list(v) { return Array.isArray(v) ? v : []; }
  function num(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function coverHue(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }
  /* Same cover block as the game-cards widget: real art when we have it, a
     name-seeded gradient plate when we don't. */
  function coverNode(game) {
    var wrap = el("div", "cover-wrap");
    var hue = coverHue(game.name || "?");
    var fallback = el("div", "cover-fallback", game.name || "?");
    fallback.style.background =
      "linear-gradient(160deg, hsl(" + hue + ",45%,38%), hsl(" + ((hue + 40) % 360) + ",50%,22%))";
    if (game.cover_url) {
      var img = document.createElement("img");
      img.alt = game.name ? "Cover art for " + game.name : "";
      img.loading = "lazy";
      img.onerror = function () { img.remove(); wrap.appendChild(fallback); };
      img.src = game.cover_url;
      wrap.appendChild(img);
    } else {
      wrap.appendChild(fallback);
    }
    return wrap;
  }
  function hoursLabel(h) {
    var n = num(h);
    if (n == null) return null;
    return (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10) + "h";
  }
  var CURRENCY_SIGNS = { EUR: "€", USD: "$", GBP: "£", JPY: "¥" };
  function money(amount, currency) {
    var n = num(amount);
    if (n == null) return null;
    var code = String(currency || "").toUpperCase();
    var sign = CURRENCY_SIGNS[code];
    var value = n.toFixed(2);
    if (sign) return sign + value;
    return code ? code + " " + value : value;
  }
  function compactCount(v) {
    var n = num(v);
    if (n == null) return null;
    if (n >= 1000000) return (Math.round(n / 100000) / 10) + "M";
    if (n >= 10000) return Math.round(n / 1000) + "k";
    if (n >= 1000) return (Math.round(n / 100) / 10) + "k";
    return String(Math.round(n));
  }
  function section(parent, title) {
    var box = el("section", "panel");
    if (title) box.appendChild(el("div", "section-title", title));
    parent.appendChild(box);
    return box;
  }

  /* ---------- click-to-enlarge overlay ---------- */
  var overlayState = null; // { node, trigger, keydown }

  function closeOverlay() {
    if (!overlayState) return;
    var s = overlayState;
    overlayState = null;
    document.removeEventListener("keydown", s.keydown);
    s.node.classList.remove("open");
    setTimeout(function () { s.node.remove(); }, 200);
    if (s.trigger && s.trigger.focus) s.trigger.focus();
  }

  function openShot(shot, label, trigger) {
    closeOverlay();
    var overlay = el("div", "overlay");
    var panel = el("div", "overlay-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", label || "Screenshot");
    panel.tabIndex = -1;

    var close = el("button", "overlay-close", "✕");
    close.setAttribute("aria-label", "Close screenshot");
    close.addEventListener("click", closeOverlay);
    panel.appendChild(close);

    var img = document.createElement("img");
    img.className = "shot-full";
    img.alt = label || "Screenshot";
    img.src = shot.full || shot.thumb;
    panel.appendChild(img);

    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeOverlay();
    });
    var keydown = function (ev) { if (ev.key === "Escape") closeOverlay(); };
    document.addEventListener("keydown", keydown);
    overlayState = { node: overlay, trigger: trigger, keydown: keydown };

    document.body.appendChild(overlay);

    // Anchor the panel near the clicked thumbnail, clamped inside the
    // document; stretch the backdrop over the full document height.
    function position() {
      var docH = document.documentElement.scrollHeight;
      var scrollTop = window.scrollY || document.documentElement.scrollTop || 0;
      var anchor = trigger ? trigger.getBoundingClientRect().top + scrollTop - 8 : 12;
      var top = Math.max(12, Math.min(anchor, docH - panel.offsetHeight - 12));
      panel.style.top = top + "px";
      overlay.style.height = Math.max(docH, top + panel.offsetHeight + 14) + "px";
    }
    position();
    img.addEventListener("load", position); // full-size art changes the height
    requestAnimationFrame(function () { overlay.classList.add("open"); });
    panel.focus({ preventScroll: true });
  }

  /* ---------- 1. hero ---------- */
  function playBadge(label) {
    var btn = el("button", "play-badge");
    btn.setAttribute("aria-label", label);
    btn.appendChild(el("span", null, "▶"));
    return btn;
  }
  function linkPill(hero, label, url) {
    var pill = el("button", "hero-pill", label);
    pill.addEventListener("click", function (ev) {
      ev.stopPropagation();
      openLink(url);
    });
    hero.appendChild(pill);
    return pill;
  }
  function posterNode(url, alt) {
    var img = document.createElement("img");
    img.className = "hero-media";
    img.alt = alt || "";
    img.src = url;
    return img;
  }
  function heroNode(media) {
    var shots = list(media.screenshots);
    var trailer = media.trailer;
    var hasTrailer = trailer && (trailer.url || trailer.video_id);
    if (!hasTrailer && !shots.length) return null;
    var hero = el("div", "hero");
    if (hasTrailer && trailer.kind === "mp4" && trailer.url) {
      mp4Hero(hero, trailer);
    } else if (hasTrailer && trailer.kind === "youtube" && trailer.video_id) {
      youtubeHero(hero, trailer);
    } else if (shots.length) {
      hero.appendChild(posterNode(shots[0].full || shots[0].thumb, "Screenshot"));
    } else {
      return null;                                  // trailer of an unknown kind
    }
    return hero;
  }
  function mp4Hero(hero, trailer) {
    var urls = [trailer.url, trailer.hq_url].filter(Boolean);
    var video = document.createElement("video");
    video.className = "hero-media";
    video.controls = true;
    video.preload = "none";     // never autoplay: zero bytes until the user asks
    video.playsInline = true;
    if (trailer.poster) video.poster = trailer.poster;
    video.setAttribute("aria-label", trailer.name || "Trailer");
    urls.forEach(function (u) {
      var src = document.createElement("source");
      src.src = u;
      src.type = "video/mp4";
      video.appendChild(src);
    });
    /* A <video> with <source> children never fires `error` on itself — the
       failures land on the <source> elements and error events don't bubble,
       so catch them on the way down and give up once every rendition failed.
       This is the fallback for Valve dropping the undocumented legacy mp4
       URLs, and equally for a host that strips media-src from the CSP. */
    var failures = 0;
    var done = false;
    video.addEventListener("error", function () {
      failures += 1;
      if (done || failures < urls.length) return;
      done = true;
      posterFallback(hero, trailer);
    }, true);
    hero.appendChild(video);
  }
  function posterFallback(hero, trailer) {
    hero.textContent = "";
    if (trailer.poster) {
      hero.appendChild(posterNode(trailer.poster, trailer.name || "Trailer thumbnail"));
    } else {
      hero.appendChild(el("div", "hero-missing", "Trailer unavailable here"));
    }
    var url = trailer.hq_url || trailer.url;
    var badge = playBadge("Open the trailer" + (trailer.name ? ": " + trailer.name : ""));
    badge.addEventListener("click", function () { openLink(url); });
    hero.appendChild(badge);
  }
  function youtubeHero(hero, trailer) {
    if (trailer.poster) {
      hero.appendChild(posterNode(trailer.poster, trailer.name || "Trailer thumbnail"));
    } else {
      hero.appendChild(el("div", "hero-missing", trailer.name || "Trailer"));
    }
    var watchUrl = "https://www.youtube.com/watch?v=" + trailer.video_id;
    var badge = playBadge("Play trailer" + (trailer.name ? ": " + trailer.name : ""));
    badge.addEventListener("click", function () {
      // Lazy by design: nothing is fetched from YouTube until this click.
      var frame = document.createElement("iframe");
      frame.className = "hero-media";
      frame.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(trailer.video_id);
      frame.setAttribute("allowfullscreen", "");
      frame.setAttribute("title", trailer.name || "Trailer");
      hero.textContent = "";
      hero.appendChild(frame);
      // A CSP-blocked nested frame is not detectable from JS, so keep the
      // link-out route visible even after the embed is swapped in.
      linkPill(hero, "watch ↗", watchUrl);
      reportSize();
    });
    hero.appendChild(badge);
    linkPill(hero, "watch ↗", watchUrl);
  }

  /* ---------- 2. header + verdict stamp ---------- */
  var VERDICTS = {
    buy_now: ["BUY NOW", "stamp-good"],
    wishlist_for_sale: ["WISHLIST FOR SALE", "stamp-ok"],
    try_demo: ["TRY THE DEMO", "stamp-p1"],
    play_what_you_own: ["PLAY WHAT YOU OWN", "stamp-p4"],
    skip: ["SKIP", "stamp-bad"],
  };
  function stampNode(verdict) {
    if (!verdict) return null;
    var known = VERDICTS[verdict];
    var label = known ? known[0] : String(verdict).replace(/_/g, " ").toUpperCase();
    var stamp = el("div", "stamp " + (known ? known[1] : "stamp-p1"), label);
    stamp.setAttribute("role", "img");
    stamp.setAttribute("aria-label", "Verdict: " + label);
    return stamp;
  }
  function headerNode(pkg) {
    var game = pkg.game || {};
    var own = pkg.ownership || {};
    var pres = pkg.presentation || {};
    var box = el("section", "panel");

    var head = el("div", "head");
    head.appendChild(coverNode(game));

    var info = el("div", "head-info");
    info.appendChild(el("h1", null, game.name || "Unknown game"));
    var bits = [];
    if (game.release_year) bits.push(String(game.release_year));
    var platforms = list(own.platforms).filter(Boolean);
    if (platforms.length) bits.push(platforms.join(" · "));
    // Positive hours only: hoursLabel(0) is the truthy string "0h", and
    // "0h played" would contradict the card's own unplayed badges (zero is
    // authoritative NOT-played; null is unknown and says nothing).
    var played = own.playtime_hours > 0 ? hoursLabel(own.playtime_hours) : null;
    if (played) bits.push(played + " played");
    if (own.wishlisted && !own.owned) bits.push("wishlisted");
    if (bits.length) info.appendChild(el("div", "sub", bits.join("  ·  ")));
    head.appendChild(info);

    var stamp = stampNode(pkg.verdict);
    if (stamp) head.appendChild(stamp);
    box.appendChild(head);

    if (pkg.summary) box.appendChild(el("p", "one-liner", pkg.summary));
    if (pres.elevator_pitch) box.appendChild(el("p", "pitch", pres.elevator_pitch));
    whyCareNode(box, pres);
    return box;
  }

  /* why_care: up to three model-authored lines answering "why look at this at
     all" — the editorial counterpart to the server-fetched pedigree strip
     below. Chip per kind, one line each, no paragraphs; absent when the
     recording client authored none. */
  var WHY_CARE_KINDS = {
    people: ["PEOPLE", "wc-people"],
    studio: ["STUDIO", "wc-studio"],
    anticipation: ["HYPE", "wc-hype"],
    moment: ["MOMENT", "wc-moment"],
  };
  function whyCareNode(parent, pres) {
    var entries = list(pres.why_care).filter(function (e) { return e && e.text; });
    if (!entries.length) return;
    var wrap = el("div", "why-care");
    entries.slice(0, 3).forEach(function (entry) {
      var known = WHY_CARE_KINDS[entry.kind];
      var line = el("div", "wc-line");
      line.appendChild(el("span", "wc-eyebrow " + (known ? known[1] : "wc-studio"),
        known ? known[0] : String(entry.kind || "why").toUpperCase()));
      line.appendChild(el("span", null, String(entry.text)));
      wrap.appendChild(line);
    });
    parent.appendChild(wrap);
  }

  /* ---------- 3. screenshot strip ---------- */
  function shotsNode(parent, media, gameName) {
    var shots = list(media.screenshots).filter(function (s) { return s && (s.thumb || s.full); });
    if (!shots.length) return;
    var box = section(parent, "Screenshots");
    var strip = el("div", "strip");
    shots.forEach(function (shot, i) {
      var label = (gameName ? gameName + " " : "") + "screenshot " + (i + 1);
      var btn = el("button", "shot");
      btn.setAttribute("aria-label", "Enlarge " + label);
      var img = document.createElement("img");
      img.alt = "";                       // the button already carries the label
      img.loading = "lazy";
      img.src = shot.thumb || shot.full;
      btn.appendChild(img);
      btn.addEventListener("click", function () { openShot(shot, label, btn); });
      strip.appendChild(btn);
    });
    var total = num(media.screenshot_count);
    if (media.screenshots_truncated && total != null && total > shots.length) {
      strip.appendChild(el("span", "more-chip", "+" + (total - shots.length) + " more"));
    }
    box.appendChild(strip);
  }

  /* ---------- 4. craft / trajectory / critic / fit ---------- */
  /* OpenCritic tiers are percentile-based; approximate from the score, with
     the tier palette from their own stylesheet (same mapping as apps.py). */
  function ocTier(n) {
    return "oc-" + (n >= 84 ? "mighty" : n >= 75 ? "strong" : n >= 65 ? "fair" : "weak");
  }
  var TRAJECTORIES = {
    improving: ["↗", "improving", "traj-good"],
    stable: ["→", "stable", "traj-flat"],
    regressing: ["↘", "regressing", "traj-bad"],
  };
  var FIT_CLASSES = {
    "strong fit": "fit-good",
    "probable fit": "fit-ok",
    "coin flip": "fit-mid",
    "probable miss": "fit-bad",
  };
  /* craft.adjusted is a 0..1 sample-adjusted share; positive_pct is the raw
     review percentage (0..100) and only stands in when there's no adjusted
     figure. */
  function craftPercent(craft) {
    var adjusted = num(craft.adjusted);
    if (adjusted != null) return Math.round(adjusted * 100);
    var raw = num(craft.positive_pct);
    if (raw == null) return null;
    return Math.round(raw <= 1 ? raw * 100 : raw);
  }
  function scoresNode(parent, pkg) {
    var craft = pkg.craft || {};
    var row = el("div", "chips");

    var pct = craftPercent(craft);
    if (pct != null) {
      var tier = pct >= 75 ? "craft-hi" : pct >= 50 ? "craft-mid" : "craft-lo";
      var chip = el("span", "chip " + tier);
      chip.appendChild(el("span", "lbl", "craft"));
      var meter = el("span", "meter");
      var fill = el("span", "meter-fill");
      fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
      meter.appendChild(fill);
      chip.appendChild(meter);
      var n = compactCount(craft.review_count);
      chip.appendChild(el("span", null, pct + "%" + (n ? " · n=" + n : "")));
      var rawPct = num(craft.positive_pct);
      chip.title = "Sample-adjusted review share"
        + (rawPct != null ? " (raw " + Math.round(rawPct <= 1 ? rawPct * 100 : rawPct) + "% positive)" : "");
      row.appendChild(chip);
    }

    var traj = TRAJECTORIES[craft.trajectory];
    if (traj) {
      var tChip = el("span", "chip " + traj[2]);
      tChip.appendChild(el("span", null, traj[0] + " " + traj[1]));
      tChip.title = "Recent review trajectory";
      row.appendChild(tChip);
    }

    var oc = num(craft.opencritic_score);
    if (oc != null && oc >= 0) {           // providers use negatives for "none"
      var ocChip = el("span", "chip oc " + ocTier(oc));
      ocChip.appendChild(el("span", "lbl", "OC"));
      ocChip.appendChild(el("span", null, String(Math.round(oc))));
      ocChip.title = "OpenCritic";
      row.appendChild(ocChip);
    }

    if (pkg.fit_call) {
      var fitChip = el("span", "chip " + (FIT_CLASSES[pkg.fit_call] || ""));
      fitChip.appendChild(el("span", "lbl", "fit"));
      fitChip.appendChild(el("span", null, String(pkg.fit_call)));
      row.appendChild(fitChip);
    }

    if (!row.childNodes.length) return;
    section(parent, "Craft & fit").appendChild(row);
  }

  /* ---------- 5. for you / not for you ---------- */
  function bulletColumn(title, items, kind) {
    var col = el("div", "col " + kind);
    col.appendChild(el("div", "col-title", title));
    var ul = el("ul", "bullets");
    items.forEach(function (text) {
      var li = document.createElement("li");
      li.appendChild(el("span", "tick", kind === "yes" ? "✓" : "✗"));
      li.appendChild(el("span", null, String(text)));
      ul.appendChild(li);
    });
    col.appendChild(ul);
    return col;
  }
  function forYouNode(parent, pres) {
    var yes = list(pres.for_you_if).filter(Boolean);
    var no = list(pres.not_for_you_if).filter(Boolean);
    if (!yes.length && !no.length) return;
    var box = section(parent, "For you / not for you");
    var cols = el("div", "two-col");
    if (yes.length) cols.appendChild(bulletColumn("For you if", yes, "yes"));
    if (no.length) cols.appendChild(bulletColumn("Not for you if", no, "no"));
    // One-sided lists shouldn't leave a dead half-column.
    if (cols.childNodes.length === 1) cols.style.gridTemplateColumns = "1fr";
    box.appendChild(cols);
  }

  /* ---------- 6. anchors ---------- */
  /* An abandoned anchor is negative evidence — styled as a warning, not as
     another green sticker. */
  var COMPLETION = {
    completed: ["✓", "completed", ""],
    evergreen: ["∞", "evergreen", ""],
    playing: ["▶", "playing", ""],
    abandoned: ["⚠", "abandoned", "an-warn"],
  };
  function anchorsNode(parent, anchors) {
    if (!anchors.length) return;
    var box = section(parent, "Grounded in your history");
    var chips = el("div", "chips");
    anchors.forEach(function (a) {
      var state = COMPLETION[a.completion_status];
      var chip = el("div", "anchor" + (state && state[2] ? " " + state[2] : "")
                            + (a.cover_url ? "" : " no-cover"));
      if (a.cover_url) {
        var img = document.createElement("img");
        img.className = "anchor-cover";
        img.alt = "";
        img.loading = "lazy";
        img.onerror = function () { img.remove(); chip.classList.add("no-cover"); };
        img.src = a.cover_url;
        chip.appendChild(img);
      }
      chip.appendChild(el("span", null, a.name || "?"));
      var rating = num(a.rating);
      if (rating != null) chip.appendChild(el("span", "dim", rating + "/10"));
      var hours = hoursLabel(a.playtime_hours);
      if (hours) chip.appendChild(el("span", "dim", hours));
      if (state) {
        var glyph = el("span", null, state[0]);
        glyph.title = state[1];
        chip.appendChild(glyph);
      }
      chips.appendChild(chip);
    });
    box.appendChild(chips);
  }

  /* ---------- 7. lineage / comparisons ---------- */
  var CALLOUT_HEADS = {
    better_version: "A better version exists",
    cheaper_substitute: "Cheaper substitute",
  };
  function comparisonNode(comp, cls) {
    var node = el("div", "comp" + (cls ? " " + cls : ""));
    node.appendChild(el("div", "comp-name", comp.name));
    if (comp.note) node.appendChild(el("div", "comp-note", comp.note));
    var tags = ownershipTags(comp);
    if (tags) node.appendChild(tags);
    return node;
  }
  function ownershipTags(item) {
    var tags = el("div", "tags");
    if (item.owned) tags.appendChild(el("span", "tag owned", "owned"));
    if (item.unplayed) tags.appendChild(el("span", "tag unplayed", "unplayed"));
    var rating = num(item.my_rating);
    if (rating != null) tags.appendChild(el("span", "tag", rating + "/10"));
    var hours = hoursLabel(item.playtime_hours);
    if (hours) tags.appendChild(el("span", "tag", hours));
    return tags.childNodes.length ? tags : null;
  }
  function lineageColumn(title, entries) {
    var col = el("div", "lin-col");
    col.appendChild(el("div", "lin-head", title));
    entries.forEach(function (c) { col.appendChild(comparisonNode(c)); });
    return col;
  }
  function lineageNode(parent, pkg, comps, foldSimilar) {
    var callouts = comps.filter(function (c) { return CALLOUT_HEADS[c.relation]; });
    var ancestors = comps.filter(function (c) { return c.relation === "ancestor"; });
    var descendants = comps.filter(function (c) { return c.relation === "descendant"; });
    var loose = comps.filter(function (c) {
      return !CALLOUT_HEADS[c.relation] && c.relation !== "ancestor" && c.relation !== "descendant";
    });
    if (foldSimilar) loose = loose.filter(function (c) { return c.relation !== "similar"; });
    if (!callouts.length && !ancestors.length && !descendants.length && !loose.length) return;

    var box = section(parent, "Lineage");
    callouts.forEach(function (c) {
      var card = el("div", "callout");
      card.appendChild(el("div", "callout-head", CALLOUT_HEADS[c.relation] + ": " + c.name));
      if (c.note) card.appendChild(el("div", "callout-note", c.note));
      var tags = ownershipTags(c);
      if (tags) card.appendChild(tags);
      box.appendChild(card);
    });

    if (ancestors.length || descendants.length) {
      var strip = el("div", "lineage");
      if (ancestors.length) {
        strip.appendChild(lineageColumn("Ancestors", ancestors));
        strip.appendChild(el("div", "lin-arrow", "→"));
      }
      var mid = el("div", "lin-col");
      mid.appendChild(el("div", "lin-head", "This game"));
      mid.appendChild(comparisonNode({ name: (pkg.game || {}).name || "This game" }, "this-game"));
      strip.appendChild(mid);
      if (descendants.length) {
        strip.appendChild(el("div", "lin-arrow", "→"));
        strip.appendChild(lineageColumn("Descendants", descendants));
      }
      box.appendChild(strip);
    }

    if (loose.length) {
      var rest = el("div", "chips");
      loose.forEach(function (c) { rest.appendChild(comparisonNode(c)); });
      box.appendChild(rest);
    }
  }

  /* ---------- 8. similar games ---------- */
  function similarNode(parent, similar, alsoSimilar) {
    var items = list(similar.items);
    if (!items.length && !alsoSimilar.length) return;
    var box = section(parent, "Similar games");

    if (items.length) {
      var strip = el("div", "strip");
      items.forEach(function (item) {
        var card = el("div", "sim");
        card.appendChild(coverNode(item));
        var body = el("div", "sim-body");
        body.appendChild(el("div", "sim-name", item.name || "?"));
        if (item.release_year) body.appendChild(el("div", "sim-year", String(item.release_year)));
        var tags = ownershipTags(item);
        if (tags) body.appendChild(tags);
        card.appendChild(body);
        strip.appendChild(card);
      });
      var total = num(similar.count);
      if (similar.truncated && total != null && total > items.length) {
        strip.appendChild(el("span", "more-chip", "+" + (total - items.length) + " more"));
      }
      box.appendChild(strip);
      var owned = items.filter(function (i) { return i.owned; }).length;
      // Ownership is annotated only for the SHOWN games — the true total
      // belongs to the "+N more" chip above, never to this denominator.
      box.appendChild(el("div", "note",
        "You own " + owned + " of the " + items.length + " most similar"));
    }

    if (alsoSimilar.length) {
      var chips = el("div", "chips");
      chips.style.marginTop = items.length ? "10px" : "0";
      alsoSimilar.forEach(function (c) { chips.appendChild(comparisonNode(c)); });
      box.appendChild(chips);
    }
  }

  /* ---------- 8b. from the studio (pedigree) ---------- */
  /* Server-fetched and library-annotated (tools/game_media.py): who made this,
     and what they shipped BEFORE it. Under the big-studio damper, or with
     nothing released earlier, only the header line renders — six arbitrary
     posters out of a 500-game catalogue say nothing about this game. Mirrors
     the detail card's implementation (apps.py) by hand, like every other block
     these two widgets share. */
  function pedigreeHeadline(ped) {
    var dev = ped.developer || {};
    var names = list(ped.developer_names).filter(Boolean);
    var parts = [];
    if (names.length) parts.push(names.join(" & "));
    else if (dev.name) parts.push(dev.name);
    var founded = num(dev.founded_year);
    if (founded != null) parts.push("est. " + founded);
    var size = num(ped.catalog_size);
    if (size) parts.push(size + (ped.catalog_truncated ? "+" : "") + " games");
    return parts.join("  ·  ");
  }
  /* ONE badge per poster: his own rating outranks the critic score, which
     only stands in when he hasn't rated it. An owned game he never rated
     still gets its ownership sticker. */
  function pedigreeBadges(item) {
    var tags = el("div", "tags");
    var rating = num(item.my_rating);
    var critic = num(item.critic_score);
    if (item.owned && rating != null) {
      tags.appendChild(el("span", "tag rated", rating + "/10"));
    } else if (critic != null && critic >= 0) {
      tags.appendChild(el("span", "tag critic", String(Math.round(critic))));
    }
    if (item.owned && rating == null) tags.appendChild(el("span", "tag owned", "owned"));
    return tags.childNodes.length ? tags : null;
  }
  function pedigreeNode(parent, ped) {
    if (!ped) return;
    var headline = pedigreeHeadline(ped);
    var items = list(ped.previous_games).filter(function (i) { return i && i.name; });
    if (!headline && !items.length) return;
    var box = section(parent, "From the studio");
    if (headline) box.appendChild(el("div", "ped-head", headline));
    // The publisher is a line of text, never a poster row: a publisher's back
    // catalogue is a distribution list, not a body of work.
    if (ped.publisher_name) {
      box.appendChild(el("div", "ped-pub", "published by " + ped.publisher_name));
    }
    if (!items.length) return;
    var strip = el("div", "strip ped-strip");
    items.forEach(function (item) {
      var card = el("div", "sim");
      card.appendChild(coverNode(item));
      var body = el("div", "sim-body");
      body.appendChild(el("div", "sim-name", item.name || "?"));
      if (item.release_year) body.appendChild(el("div", "sim-year", String(item.release_year)));
      var badges = pedigreeBadges(item);
      if (badges) body.appendChild(badges);
      card.appendChild(body);
      strip.appendChild(card);
    });
    box.appendChild(strip);
    var record = ped.library_track_record;
    if (record) {
      var avg = num(record.avg_my_rating);
      // The track record covers only the annotated (shown) games; when the
      // catalogue runs deeper, "last N" keeps the claim honest.
      var span = ped.previous_truncated
        ? "their last " + items.length + " games"
        : "their " + items.length + " previous games";
      box.appendChild(el("div", "note",
        "You've played " + (num(record.played_count) || 0) + " of "
        + span + (avg != null ? " — avg " + avg + "/10." : ".")));
    }
  }

  /* ---------- 9. time & price ---------- */
  function factChip(row, label, value) {
    var chip = el("span", "chip");
    chip.appendChild(el("span", "lbl", label));
    chip.appendChild(el("span", null, value));
    row.appendChild(chip);
  }
  function timePriceNode(parent, pkg) {
    var time = pkg.time || {};
    var price = pkg.price || {};
    var own = pkg.ownership || {};
    var row = el("div", "chips");

    var main = hoursLabel(time.hltb_main_hours);
    var extra = hoursLabel(time.hltb_extra_hours);
    if (main && extra) factChip(row, "HLTB", "~" + main + " / " + extra);
    else if (main) factChip(row, "HLTB", "~" + main);
    else if (extra) factChip(row, "HLTB", "~" + extra + " to complete");

    var weekly = num(time.recent_weekly_minutes);
    if (weekly != null && weekly > 0) {
      factChip(row, "pace", "your last 30 days: ~" + hoursLabel(weekly / 60) + "/wk");
    }

    var seen = money(price.seen, price.currency);
    if (seen) {
      factChip(row, "price", "seen at " + seen + (price.platform ? " on " + price.platform : ""));
    }
    var target = money(price.target, price.currency);
    if (target) factChip(row, "target", target);

    var paid = money(own.price_paid, own.price_currency);
    if (paid) {
      var how = own.bundle_name ? " in " + own.bundle_name
        : own.purchase_source ? " via " + own.purchase_source : "";
      factChip(row, "owned", "paid " + paid + how);
    }

    if (!row.childNodes.length) return;
    section(parent, "Time & price").appendChild(row);
  }

  /* ---------- 10-12. flags, past verdicts, errors ---------- */
  function flagsNode(parent, flags) {
    if (!flags.length) return;
    var box = section(parent, "Flags");
    var chips = el("div", "chips");
    flags.forEach(function (f) { chips.appendChild(el("span", "flag", String(f))); });
    box.appendChild(chips);
  }
  function pastNode(parent, past) {
    var items = list(past.items).slice();
    if (!items.length) return;
    items.sort(function (a, b) {                    // newest first
      return String(b.assessed_at || "").localeCompare(String(a.assessed_at || ""));
    });
    var box = section(parent, "Past verdicts");
    var line = el("div", "timeline");
    items.forEach(function (p) {
      var parts = [];
      if (p.assessed_at) parts.push(String(p.assessed_at).slice(0, 10));
      // The stored verdict value, not a prettified one: the timeline is a
      // record of what was filed, and it reads next to the current stamp.
      if (p.verdict) parts.push(String(p.verdict));
      var seen = money(p.price_seen, p.price_currency);
      if (seen) parts.push(seen);
      var chip = el("span", "tl-chip", parts.join(" · ") || "earlier verdict");
      if (p.summary) chip.title = p.summary;
      line.appendChild(chip);
    });
    var total = num(past.count);
    if (past.truncated && total != null && total > items.length) {
      line.appendChild(el("span", "tl-chip", "+" + (total - items.length) + " earlier"));
    }
    box.appendChild(line);
  }
  function errorsNode(parent, errors) {
    if (!errors.length) return;
    // Deliberately quiet: a missing trailer is not an incident.
    var note = el("div", "errfoot", "some data unavailable");
    note.title = errors.join("; ");
    parent.appendChild(note);
  }

  /* ---------- card assembly ---------- */
  function evalCard(pkg) {
    var wrap = el("div", "eval");
    var media = pkg.media || {};
    var hero = heroNode(media);
    if (hero) wrap.appendChild(hero);

    wrap.appendChild(headerNode(pkg));

    var game = pkg.game || {};
    shotsNode(wrap, media, game.name);
    scoresNode(wrap, pkg);
    forYouNode(wrap, pkg.presentation || {});
    anchorsNode(wrap, list(pkg.anchors).filter(function (a) { return a && a.name; }));

    var comps = list(pkg.comparisons).filter(function (c) { return c && c.name; });
    var similar = pkg.similar || {};
    var foldSimilar = list(similar.items).length > 0;
    lineageNode(wrap, pkg, comps, foldSimilar);
    similarNode(wrap, similar, foldSimilar
      ? comps.filter(function (c) { return c.relation === "similar"; })
      : []);
    pedigreeNode(wrap, pkg.pedigree);

    timePriceNode(wrap, pkg);
    flagsNode(wrap, list(pkg.flags).filter(Boolean));
    pastNode(wrap, pkg.past || {});
    errorsNode(wrap, list(pkg.errors).filter(Boolean));
    return wrap;
  }

  /* The bookkeeping-only responses: record_assessment without a package, and
     the void mode, which returns no verdict at all. */
  function noteCard(text, verdict) {
    var wrap = el("div", "eval");
    var box = el("div", "panel note-card");
    var stamp = verdict ? stampNode(verdict) : null;
    if (stamp) box.appendChild(stamp);
    box.appendChild(el("div", "note-text", text));
    wrap.appendChild(box);
    return wrap;
  }

  function render(data) {
    root.textContent = "";
    var name = data && data.name ? " — " + data.name : "";
    if (data && data.package) {
      root.appendChild(evalCard(data.package));
    } else if (data && data.voided) {
      root.appendChild(noteCard("Assessment voided" + name, null));
    } else if (data && data.verdict) {
      root.appendChild(noteCard("Recorded: " + data.verdict + name, data.verdict));
    } else {
      root.appendChild(el("div", "empty", "Nothing to display."));
    }
    reportSize();
  }

  /* ---------- sizing ---------- */
  var sizeTimer = null;
  var lastSize = "";
  function reportSize() {
    if (window.__PREVIEW_DATA__) return;
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(function () {
      // Only notify on real changes: some hosts (Android app) get confused
      // by a stream of identical/oscillating size notifications.
      var w = Math.ceil(document.documentElement.scrollWidth);
      var h = Math.ceil(document.documentElement.scrollHeight);
      var key = w + "x" + h;
      if (key === lastSize) return;
      lastSize = key;
      notify("ui/notifications/size-changed", { width: w, height: h });
    }, 120);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  /* ---------- startup ---------- */
  if (window.__PREVIEW_DATA__) {
    render(window.__PREVIEW_DATA__);
  } else {
    request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appCapabilities: {},
      // appInfo per the ext-apps SDK schema (required there); clientInfo kept
      // as a legacy alias — the published 2026-01-26 spec example used it,
      // and schema-validating hosts strip unknown keys.
      appInfo: { name: "gamelib-eval-card", version: "1.0" },
      clientInfo: { name: "gamelib-eval-card", version: "1.0" },
    }).then(function (res) {
      hostCaps = (res && res.hostCapabilities) || {};
      notify("ui/notifications/initialized");
    });
  }
})();
</script>
</body>
</html>
"""


# Hosts cache ui:// resources by URI (and may preload them from tool _meta),
# so a stable URI can pin clients to a stale widget across deploys — claude.ai
# kept rendering an old bundle after the server updated. Hashing the content
# into the URI makes every widget change a URI the host has never cached.
EVAL_CARD_URI = (
    f"ui://gamelib/eval-card-{hashlib.sha1(EVAL_CARD_HTML.encode()).hexdigest()[:8]}.html"
)

# Attached to the tool whose results the widget renders (record_assessment).
EVAL_CARD_APP = AppConfig(resource_uri=EVAL_CARD_URI)


def register_eval_app(mcp: Any) -> None:
    """Register the evaluation-card UI resource on the FastMCP app."""

    @mcp.resource(
        EVAL_CARD_URI,
        name="eval_card_view",
        description="Evaluation-package card UI for assessment results (MCP Apps).",
        app=AppConfig(csp=_EVAL_CARD_CSP),
    )
    def eval_card_view() -> str:
        return EVAL_CARD_HTML
