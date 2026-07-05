"""MCP Apps (io.modelcontextprotocol/ui): the game-cards widget.

One `ui://` resource serves both card layouts: a cover grid when the tool
result carries a ``results`` array (discover_games) and a single detail card
otherwise (get_game_detail). Clients that don't speak the Apps extension
ignore the tool metadata entirely and see the normal JSON responses, so
attaching ``GAME_CARDS_APP`` to a tool is purely additive.

Visual language ("toybox"): thick ink borders, hard offset shadows, chunky
type, pastel tag stickers, adapting to the viewer's light/dark scheme. Grids
whose payload carries an ``offset`` (discover_games — genuinely rank-ordered)
get "№ 01"-style rank badges numbered globally across pagination; payloads
without that signal (e.g. the detail card) never show a rank.

Rating chips borrow each source's own visual identity (colors verified
against the sites' stylesheets): Metacritic scores render as its square
metascore box (green/yellow/red at the games thresholds 75/50), OpenCritic
as its round score in the tier palette (mighty #fc430a, strong #9e00b4,
fair #4aa1ce, weak #80b06a), and Steam summaries in Steam's text colors
(#66c0f4 positive / #b9a074 mixed / #c85e2d negative) plus a 9-step fill
meter of our own — Steam itself colors all four positive tiers identically,
so the meter is what makes "Very" vs "Overwhelmingly" visually distinct.

Grid cards are interactive: clicking (or Enter/Space — cards are keyboard
buttons) opens an overlay that renders instantly from the card's own data,
then upgrades in place when a live ``get_game_detail`` result arrives via an
app-initiated ``tools/call`` through the bridge. If the host denies or does
not support app tool calls, the overlay simply keeps the lite view.

The HTML is deliberately dependency-free: the host↔iframe bridge is the
~40-line JSON-RPC postMessage handshake from the MCP Apps spec
(ui/initialize → ui/notifications/initialized → ui/notifications/tool-result,
plus app-initiated tools/call) rather than @modelcontextprotocol/ext-apps, so
nothing is fetched from a CDN and the CSP only has to allow the two cover-art
image hosts.

For local visual iteration outside any MCP host, the widget renders
``window.__PREVIEW_DATA__`` when present instead of waiting on the bridge —
see scripts/preview_game_cards.py.
"""

import hashlib
from typing import Any

from fastmcp.apps import AppConfig, ResourceCSP

# Cover art hosts (see tools/common.py cover_url). resource_domains feeds
# img-src in the host's iframe CSP; everything else stays deny-by-default.
_GAME_CARDS_CSP = ResourceCSP(
    resource_domains=[
        "https://images.igdb.com",
        "https://cdn.cloudflare.steamstatic.com",
    ],
)

GAME_CARDS_HTML = r"""<!doctype html>
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
  :root {
    --steam-pos: #d6edfd;
    --steam-mixed: #ede3c9;
    --steam-neg: #f7d8c2;
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
      --steam-pos: #1e4c6d;
      --steam-mixed: #57492a;
      --steam-neg: #63351b;
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

  /* ---- shared cover block ---- */
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
    font-size: 15px;
    font-weight: 800;
    line-height: 1.3;
    color: rgba(255, 255, 255, 0.92);
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
    padding: 12px;
    text-align: center;
    overflow: hidden;
    overflow-wrap: anywhere;
  }
  .score-chip {
    position: absolute;
    top: 8px;
    right: 8px;
    z-index: 1;
    font-size: 12.5px;
    font-weight: 800;
    padding: 3px 8px;
    border-radius: 999px;
    background: var(--card);
    border: 2px solid var(--ink);
    box-shadow: 2px 2px 0 var(--shadow-c);
    transform: rotate(4deg);
  }
  /* Source-shaped score chips: Metacritic's metascore is a square box,
     OpenCritic's score is round. Colors are each site's own (metascore
     green/yellow/red at the games thresholds; OpenCritic tier palette from
     their stylesheet). Text is fixed near-black/white per background for
     contrast — brand hexes don't shift with our light/dark scheme. */
  .score-chip.mc { border-radius: 5px; }
  .score-chip.oc { border-radius: 999px; }
  .mc-hi { background: #6c3; color: #17140e; }
  .mc-mid { background: #fc3; color: #17140e; }
  .mc-lo { background: #f00; color: #17140e; }
  .oc-mighty { background: #fc430a; color: #17140e; }
  .oc-strong { background: #9e00b4; color: #ffffff; }
  .oc-fair { background: #4aa1ce; color: #17140e; }
  .oc-weak { background: #80b06a; color: #17140e; }
  .pills { display: flex; gap: 5px; flex-wrap: wrap; }
  .pill {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p1);
    color: var(--ink);
    white-space: nowrap;
    transform: rotate(-1.2deg);
  }
  .pill:nth-child(2n) { background: var(--p2); transform: rotate(1.1deg); }
  .pill:nth-child(3n) { background: var(--p3); transform: rotate(-0.8deg); }
  .pill:nth-child(4n) { background: var(--p4); transform: rotate(1.4deg); }
  .pill.match { background: var(--good); color: var(--card); border-color: var(--ink); }

  /* ---- grid mode ---- */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(142px, 1fr));
    gap: 18px;
  }
  .card {
    background: var(--card);
    border: 2px solid var(--ink);
    border-radius: 12px;
    box-shadow: 4px 4px 0 var(--shadow-c);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.12s ease, box-shadow 0.12s ease;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .card:hover {
    transform: translate(-2px, -2px);
    box-shadow: 7px 7px 0 var(--shadow-c);
  }
  .card:focus-visible {
    outline: 3px solid var(--ink);
    outline-offset: 2px;
  }
  .card .cover-wrap { border-bottom: 2px solid var(--ink); }

  /* Rank badge — only on grids whose payload is genuinely rank-ordered
     (render() adds .ranked and seeds the counter from the payload offset). */
  .grid.ranked { counter-reset: rank; }
  .grid.ranked .card { counter-increment: rank; }
  /* Quieter than the score chip on the right: tucked into the corner,
     smaller type, thinner border, shallower shadow. */
  .grid.ranked .cover-wrap::before {
    content: "№ " counter(rank, decimal-leading-zero);
    position: absolute;
    top: 5px;
    left: 5px;
    z-index: 1;
    font-size: 9px;
    font-weight: 750;
    padding: 1.5px 6px;
    border-radius: 999px;
    background: var(--card);
    color: var(--muted);
    border: 1.5px solid var(--ink);
    box-shadow: 1.5px 1.5px 0 var(--shadow-c);
    transform: rotate(-3deg);
  }

  .card-body { padding: 10px 11px 12px; display: flex; flex-direction: column; gap: 6px; flex: 1; }
  .title {
    font-size: 13.5px;
    font-weight: 800;
    line-height: 1.25;
    letter-spacing: -0.01em;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .meta {
    font-size: 11px;
    font-weight: 650;
    color: var(--muted);
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .card-body .pills { margin-top: auto; padding-top: 2px; }
  /* Secondary-ratings row on grid cards: mini branded chips. */
  .scores { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
  .mini {
    font-size: 10px;
    font-weight: 800;
    padding: 1.5px 6px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    box-shadow: 1.5px 1.5px 0 var(--shadow-c);
  }
  .mini.mc { border-radius: 4px; }
  .mini.steam { display: inline-flex; align-items: center; padding: 3px 6px; }
  .mini.steam-pos { background: var(--steam-pos); }
  .mini.steam-mixed { background: var(--steam-mixed); }
  .mini.steam-neg { background: var(--steam-neg); }
  .scores .meter { width: 34px; height: 6px; }

  /* ---- detail mode ---- */
  .detail {
    display: flex;
    gap: 20px;
    background: var(--card);
    border: 2px solid var(--ink);
    border-radius: 14px;
    box-shadow: 5px 5px 0 var(--shadow-c);
    padding: 18px;
    max-width: 720px;
  }
  .detail .cover-wrap {
    flex: 0 0 168px;
    aspect-ratio: 2 / 3;
    border: 2px solid var(--ink);
    border-radius: 10px;
    overflow: hidden;
    align-self: flex-start;
    box-shadow: 4px 4px 0 var(--shadow-c);
    transform: rotate(-1.5deg);
    margin: 4px 6px 8px 2px;
  }
  .detail-info { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .detail-info h1 { font-size: 20px; font-weight: 800; line-height: 1.15; letter-spacing: -0.01em; }
  .sub { font-size: 12.5px; font-weight: 650; color: var(--muted); }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; }
  .badge {
    font-size: 11.5px;
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p1);
    color: var(--ink);
  }
  .badge:nth-child(2n) { background: var(--p2); }
  .badge:nth-child(3n) { background: var(--p3); }
  .badge:nth-child(4n) { background: var(--p4); }
  .badge b { font-weight: 800; }
  /* Brand-colored rating badges override the pastel nth-child cycle. */
  .badges .badge.mc-hi { background: #6c3; color: #17140e; }
  .badges .badge.mc-mid { background: #fc3; color: #17140e; }
  .badges .badge.mc-lo { background: #f00; color: #17140e; }
  .badges .badge.mc { border-radius: 7px; }
  .badges .badge.oc-mighty { background: #fc430a; color: #17140e; }
  .badges .badge.oc-strong { background: #9e00b4; color: #ffffff; }
  .badges .badge.oc-fair { background: #4aa1ce; color: #17140e; }
  .badges .badge.oc-weak { background: #80b06a; color: #17140e; }
  /* Steam styles its review summaries as colored text only (one blue for
     every positive tier); the fill meter is our addition — same palette,
     but it makes Very vs Overwhelmingly Positive visually distinct. */
  .badges .badge.steam { display: inline-flex; align-items: center; gap: 7px; }
  .badges .badge.steam-pos { background: var(--steam-pos); }
  .badges .badge.steam-mixed { background: var(--steam-mixed); }
  .badges .badge.steam-neg { background: var(--steam-neg); }
  .badges .badge.steam-none { background: var(--card); color: var(--muted); }
  .meter {
    width: 44px;
    height: 8px;
    border: 1.5px solid var(--ink);
    border-radius: 999px;
    background: var(--card);
    overflow: hidden;
    display: inline-block;
    flex: none;
  }
  .meter-fill { display: block; height: 100%; }
  .steam-pos .meter-fill { background: #66c0f4; }
  .steam-mixed .meter-fill { background: #b9a074; }
  .steam-neg .meter-fill { background: #c85e2d; }
  .desc {
    font-size: 12.5px;
    color: var(--muted);
    line-height: 1.55;
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .rating-row { font-size: 13px; font-weight: 650; }
  .rating-row b { font-size: 16px; font-weight: 800; }
  .empty { color: var(--muted); font-size: 13px; font-weight: 650; padding: 20px; text-align: center; }

  /* ---- click-to-expand overlay ---- */
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 10;
    background: rgba(12, 10, 6, 0.5);
    display: flex;
    align-items: safe center;
    justify-content: center;
    padding: 14px;
    opacity: 0;
    transition: opacity 0.16s ease;
  }
  .overlay.open { opacity: 1; }
  .overlay-panel {
    position: relative;
    width: 100%;
    max-width: 720px;
    max-height: 100%;
    overflow-y: auto;
    border-radius: 16px;
    transform: scale(0.93) translateY(12px);
    transition: transform 0.19s ease;
  }
  .overlay.open .overlay-panel { transform: none; }
  .overlay-panel .detail { max-width: none; box-shadow: none; margin: 0; }
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
  .loading-note { font-size: 11.5px; font-weight: 650; color: var(--muted); }
  .loading-note::after {
    content: "…";
    display: inline-block;
    animation: pulse 1.1s ease-in-out infinite;
  }
  @keyframes pulse { 50% { opacity: 0.25; } }

  @media (max-width: 480px) {
    body { padding: 12px; }
    .grid { gap: 14px; }
  }
  @media (max-width: 460px) {
    .detail { flex-direction: column; }
    .detail .cover-wrap { flex-basis: auto; width: 150px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .card, .overlay, .overlay-panel { transition: none; }
    .card:hover { transform: none; box-shadow: 4px 4px 0 var(--shadow-c); }
    .loading-note::after { animation: none; }
  }
</style>
</head>
<body>
<div id="root"><div class="empty">Waiting for game data…</div></div>
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
  /* App-initiated tool call, proxied by the host (MCP Apps shares the core
     tools/call method). Resolves undefined on error, denial, or timeout so
     callers can fall back to the data they already have. */
  function callTool(name, args, timeoutMs) {
    return Promise.race([
      request("tools/call", { name: name, arguments: args }),
      new Promise(function (resolve) { setTimeout(resolve, timeoutMs || 15000); }),
    ]);
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

  /* ---------- rendering ---------- */
  var root = document.getElementById("root");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function coverHue(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }

  function coverNode(game) {
    var wrap = el("div", "cover-wrap");
    var hue = coverHue(game.name || "?");
    var fallback = el("div", "cover-fallback", game.name || "?");
    fallback.style.background =
      "linear-gradient(160deg, hsl(" + hue + ",45%,38%), hsl(" + ((hue + 40) % 360) + ",50%,22%))";
    if (game.cover_url) {
      var img = document.createElement("img");
      img.alt = game.name || "";
      img.loading = "lazy";
      img.onerror = function () { img.remove(); wrap.appendChild(fallback); };
      img.src = game.cover_url;
      wrap.appendChild(img);
    } else {
      wrap.appendChild(fallback);
    }
    return wrap;
  }

  /* Providers use negative sentinels for "no score yet" — never show those. */
  function realScore(n) { return n != null && n >= 0; }
  function critic(game) {
    if (realScore(game.opencritic_score)) return { n: game.opencritic_score, src: "OpenCritic" };
    if (realScore(game.metacritic_score)) return { n: game.metacritic_score, src: "Metacritic" };
    return null;
  }
  /* Metacritic's games thresholds: green >=75, yellow 50-74, red <50. */
  function mcTier(n) { return n >= 75 ? "mc-hi" : n >= 50 ? "mc-mid" : "mc-lo"; }
  /* OpenCritic tiers are percentile-based; prefer the real tier when the
     payload has one, else approximate from the score. */
  function ocTier(game, n) {
    var t = String(game.opencritic_tier || "").toLowerCase();
    if (["mighty", "strong", "fair", "weak"].indexOf(t) < 0) {
      t = n >= 84 ? "mighty" : n >= 75 ? "strong" : n >= 65 ? "fair" : "weak";
    }
    return "oc-" + t;
  }
  /* Steam's nine summary tiers, most-specific phrases first. */
  var STEAM_TIERS = [
    ["overwhelmingly positive", 9], ["very positive", 8], ["mostly positive", 6],
    ["positive", 7], ["mixed", 5], ["overwhelmingly negative", 1],
    ["very negative", 2], ["mostly negative", 4], ["negative", 3],
  ];
  function steamTier(desc) {
    var d = String(desc || "").toLowerCase();
    for (var i = 0; i < STEAM_TIERS.length; i++) {
      if (d.indexOf(STEAM_TIERS[i][0]) >= 0) return STEAM_TIERS[i][1];
    }
    return null;
  }

  function hoursLabel(h) {
    if (h == null) return null;
    return (h >= 10 ? Math.round(h) : Math.round(h * 10) / 10) + "h";
  }

  function matchedTagNames(game) {
    return (game.matched_tags || []).map(function (t) {
      return typeof t === "string" ? t : t.tag;
    }).filter(Boolean);
  }

  /* ---- click-to-expand: grid card -> live detail overlay ---- */
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

  function openDetail(game, trigger) {
    closeOverlay();
    var overlay = el("div", "overlay");
    var panel = el("div", "overlay-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", game.name || "Game details");
    panel.tabIndex = -1;

    var close = el("button", "overlay-close", "✕");
    close.setAttribute("aria-label", "Close details");
    close.addEventListener("click", closeOverlay);
    panel.appendChild(close);

    // Instant view from the data already on the card; the live
    // get_game_detail result replaces it when it arrives.
    var lite = detailCard(game);
    var note = el("div", "loading-note", "loading full details");
    var liteInfo = lite.querySelector(".detail-info");
    if (liteInfo) liteInfo.appendChild(note);
    panel.appendChild(lite);

    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeOverlay();
    });
    var keydown = function (ev) { if (ev.key === "Escape") closeOverlay(); };
    document.addEventListener("keydown", keydown);
    overlayState = { node: overlay, trigger: trigger, keydown: keydown };

    document.body.appendChild(overlay);
    requestAnimationFrame(function () { overlay.classList.add("open"); });
    panel.focus({ preventScroll: true });

    if (window.__PREVIEW_DATA__) { note.remove(); return; }
    callTool("get_game_detail", { game_id: game.game_id }).then(function (res) {
      if (overlayState && overlayState.node !== overlay) return; // superseded
      var data = resultData(res);
      if (data && data.name) {
        var full = detailCard(data);
        panel.replaceChild(full, lite);
      } else {
        note.remove(); // host declined or timed out: keep the lite view
      }
    });
  }

  function gridCard(game) {
    var card = el("div", "card");
    if (game.game_id != null) {
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", "Show details for " + (game.name || "game"));
      card.addEventListener("click", function () { openDetail(game, card); });
      card.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openDetail(game, card);
        }
      });
    }
    var cover = coverNode(game);
    var score = critic(game);
    if (score) {
      var cls = score.src === "OpenCritic"
        ? "score-chip oc " + ocTier(game, score.n)
        : "score-chip mc " + mcTier(score.n);
      var chip = el("span", cls, String(score.n));
      chip.title = score.src;
      cover.appendChild(chip);
    }
    card.appendChild(cover);

    var body = el("div", "card-body");
    body.appendChild(el("div", "title", game.name));

    var meta = el("div", "meta");
    var hltb = hoursLabel(game.hltb_main);
    if (hltb) meta.appendChild(el("span", null, "~" + hltb));
    if (game.suggested_platform) meta.appendChild(el("span", null, game.suggested_platform));
    if (game.playtime_hours) meta.appendChild(el("span", null, game.playtime_hours + "h played"));
    if (meta.childNodes.length) body.appendChild(meta);

    // Secondary ratings: whatever the cover chip doesn't show. All available
    // sources stay visible per card without stacking chips on the artwork.
    var scores = el("div", "scores");
    if (score && score.src === "OpenCritic" && realScore(game.metacritic_score)) {
      var mini = el("span", "mini mc " + mcTier(game.metacritic_score),
                    String(game.metacritic_score));
      mini.title = "Metacritic";
      scores.appendChild(mini);
    }
    var st = steamTier(game.steam_review_desc);
    if (st != null) {
      var sCls = st >= 6 ? "steam-pos" : st === 5 ? "steam-mixed" : "steam-neg";
      var sChip = el("span", "mini steam " + sCls);
      var meter = el("span", "meter");
      var fill = el("span", "meter-fill");
      fill.style.width = Math.round((st / 9) * 100) + "%";
      meter.appendChild(fill);
      sChip.appendChild(meter);
      sChip.title = "Steam: " + game.steam_review_desc;
      scores.appendChild(sChip);
    }
    if (scores.childNodes.length) body.appendChild(scores);

    var pills = el("div", "pills");
    if (game.match_percent != null) {
      pills.appendChild(el("span", "pill match", game.match_percent + "% match"));
    }
    matchedTagNames(game).slice(0, 3).forEach(function (t) {
      pills.appendChild(el("span", "pill", t));
    });
    if (game.value_note) pills.appendChild(el("span", "pill", game.value_note));
    if (pills.childNodes.length) body.appendChild(pills);

    card.appendChild(body);
    return card;
  }

  function badge(parent, label, value, cls) {
    if (value === undefined || value === null || value === "") return;
    var b = el("span", "badge" + (cls ? " " + cls : ""));
    b.appendChild(el("span", null, label + " "));
    b.appendChild(el("b", null, String(value)));
    parent.appendChild(b);
    return b;
  }

  function steamBadge(parent, desc) {
    if (!desc) return;
    var tier = steamTier(desc);
    var cls = tier == null ? "steam-none"
      : tier >= 6 ? "steam-pos" : tier === 5 ? "steam-mixed" : "steam-neg";
    var b = badge(parent, "Steam", desc, "steam " + cls);
    if (tier != null) {
      var meter = el("span", "meter");
      var fill = el("span", "meter-fill");
      fill.style.width = Math.round((tier / 9) * 100) + "%";
      meter.appendChild(fill);
      b.appendChild(meter);
      b.title = tier + "/9 on Steam's review-summary scale";
    }
  }

  function detailCard(game) {
    var box = el("div", "detail");
    box.appendChild(coverNode(game));

    var info = el("div", "detail-info");
    info.appendChild(el("h1", null, game.name));

    var subBits = [];
    if (game.release_date) subBits.push(String(game.release_date).slice(0, 4));
    var owned = (game.platforms || []).filter(function (p) { return p.owned; })
      .map(function (p) { return p.platform; });
    if (owned.length) subBits.push(owned.join(" · "));
    if (game.playtime_hours) subBits.push(game.playtime_hours + "h played");
    if (game.wishlisted && !game.owned) subBits.push("wishlisted");
    if (subBits.length) info.appendChild(el("div", "sub", subBits.join("  ·  ")));

    var badges = el("div", "badges");
    if (realScore(game.opencritic_score))
      badge(badges, "OpenCritic", game.opencritic_score,
            "oc " + ocTier(game, game.opencritic_score));
    if (realScore(game.metacritic_score))
      badge(badges, "Metacritic", game.metacritic_score,
            "mc " + mcTier(game.metacritic_score));
    steamBadge(badges, game.steam_review_desc);
    badge(badges, "HLTB", hoursLabel(game.hltb_main));
    badge(badges, "ProtonDB", game.protondb_tier);
    if (badges.childNodes.length) info.appendChild(badges);

    if (game.my_rating && game.my_rating.normalized_score != null) {
      var r = el("div", "rating-row");
      r.appendChild(el("span", null, "My rating: "));
      r.appendChild(el("b", null, game.my_rating.normalized_score + "/10"));
      info.appendChild(r);
    }

    if (game.short_description) info.appendChild(el("p", "desc", game.short_description));

    var tags = (game.tags || []).slice(0, 8);
    if (tags.length) {
      var pills = el("div", "pills");
      tags.forEach(function (t) { pills.appendChild(el("span", "pill", t)); });
      info.appendChild(pills);
    }

    box.appendChild(info);
    return box;
  }

  function render(data) {
    root.textContent = "";
    if (data && Array.isArray(data.results)) {
      if (!data.results.length) {
        root.appendChild(el("div", "empty", "No games matched."));
      } else {
        var grid = el("div", "grid");
        // Rank badges only when the payload is explicitly rank-ordered:
        // discover_games sends its pagination offset, so numbering is global
        // (page two starts at № 21). Payloads without it stay unnumbered.
        if (typeof data.offset === "number") {
          grid.classList.add("ranked");
          grid.style.counterReset = "rank " + data.offset;
        }
        data.results.forEach(function (g) { grid.appendChild(gridCard(g)); });
        root.appendChild(grid);
      }
    } else if (data && data.name) {
      root.appendChild(detailCard(data));
    } else {
      root.appendChild(el("div", "empty", "Nothing to display."));
    }
    reportSize();
  }

  /* ---------- sizing ---------- */
  var sizeTimer = null;
  function reportSize() {
    if (window.__PREVIEW_DATA__) return;
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(function () {
      notify("ui/notifications/size-changed", {
        width: document.documentElement.scrollWidth,
        height: document.documentElement.scrollHeight,
      });
    }, 60);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  /* ---------- startup ---------- */
  if (window.__PREVIEW_DATA__) {
    render(window.__PREVIEW_DATA__);
    if (window.__PREVIEW_OPEN_INDEX__ != null) {
      var previewCards = root.querySelectorAll(".card");
      var target = previewCards[window.__PREVIEW_OPEN_INDEX__];
      if (target) target.click();
    }
  } else {
    request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appCapabilities: {},
      clientInfo: { name: "gamelib-game-cards", version: "1.0" },
      capabilities: {},
    }).then(function () { notify("ui/notifications/initialized"); });
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
GAME_CARDS_URI = (
    f"ui://gamelib/game-cards-{hashlib.sha1(GAME_CARDS_HTML.encode()).hexdigest()[:8]}.html"
)

# Attached to tools whose results the widget renders.
GAME_CARDS_APP = AppConfig(resource_uri=GAME_CARDS_URI)


def register_apps(mcp: Any) -> None:
    """Register the game-cards UI resource on the FastMCP app."""

    @mcp.resource(
        GAME_CARDS_URI,
        name="game_cards_view",
        description="Cover-art card UI for game tool results (MCP Apps).",
        app=AppConfig(csp=_GAME_CARDS_CSP),
    )
    def game_cards_view() -> str:
        return GAME_CARDS_HTML
