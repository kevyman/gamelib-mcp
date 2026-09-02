"""MCP Apps (io.modelcontextprotocol/ui): the evaluation-card widget.

One `ui://` resource renders `record_assessment` results: the full
evaluation package when the response carries one, and a compact note card for
the bookkeeping-only responses (a plain recorded verdict, or a void).

The package layout reads top to bottom as one argument: a header panel (cover,
title, verdict stamp, the score chips and the authored craft note), the pitch
panel (one-liner, elevator pitch, why-care eyebrow lines), the media panel (one
viewer plus one thumb strip, trailer first, screenshots opening an edge-to-edge
carousel), for-you-if / not-for-you-if, the anchors it rests on, lineage,
IGDB's similar games, the "from the studio" pedigree strip, and one closing
"the call" panel holding time, price, flags and past verdicts.
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

from . import apps_shared

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

EVAL_CARD_HTML = (
    r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
"""
    + apps_shared.PALETTE_LIGHT_CSS
    + r"""  @media (prefers-color-scheme: dark) {
    :root {
"""
    + apps_shared.PALETTE_DARK_VARS
    + r"""    }
  }
"""
    + apps_shared.RESET_CSS
    + r"""  .eval {
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
"""
    + apps_shared.COVER_CSS
    + r"""  .cover-fallback {
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
"""
    + apps_shared.HERO_CSS
    + r"""
  /* ---- media panel: one viewer + one thumb strip (the Steam shape) ---- */
  /* The trailer and the screenshots are the same reel, so they share one
     16:9 stage and one strip; clicking a thumb swaps the stage in place. */
  .viewer { box-shadow: 3px 3px 0 var(--shadow-c); }
"""
    + apps_shared.SHOT_BTN_CSS
    + r"""  /* Best-effort fullscreen. The button is only rendered where the API exists
     AND the host allows it — a sandboxed iframe without allow="fullscreen"
     reports fullscreenEnabled false, and a denied request removes the button
     rather than pretending anything happened. */
"""
    + apps_shared.MEDIA_STRIP_CSS
    + r"""
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
  /* The score chips live INSIDE the header now: two lonely chips in a panel of
     their own ("CRAFT & FIT") was the first thing the owner called out. */
  .head-chips { margin-top: 13px; }
  /* One model-authored line of craft context under the chips — the spread, the
     recurring knock, the review-bomb caveat a number can't carry. */
  .craft-note {
    margin-top: 8px;
    font-size: 11.5px;
    font-weight: 650;
    line-height: 1.45;
    color: var(--muted);
  }
  .one-liner { font-size: 14px; font-weight: 700; line-height: 1.45; }
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
  .pitch:first-child { margin-top: 0; }
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
  /* On a brand-coloured chip the muted label colour is unreadable (dark-scheme
     beige on Metacritic green) — inherit the chip's own text colour instead. */
  .chip.oc .lbl, .chip.mc .lbl { color: inherit; opacity: 0.7; }
  /* Metacritic's metascore is a square box in its own green/yellow/red at the
     games thresholds (75/50) — same brand mapping as the game-cards widget. */
  .chip.mc { font-weight: 800; border-radius: 5px; }
  .mc-hi { background: #6c3; color: #17140e; }
  .mc-mid { background: #fc3; color: #17140e; }
  .mc-lo { background: #f00; color: #17140e; }
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
"""
    + apps_shared.STRIP_CSS
    + apps_shared.MORE_CHIP_CSS
    + r"""
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
  /* Deliberately NEUTRAL pills: an anchor is evidence, and the live card lit
     up "Cyberpunk 2077 6.6h" — a game he bounced off — in endorsement green.
     The STATUS glyph carries the colour instead: completed/evergreen good,
     abandoned bad, everything else plain. */
  .anchor {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 11.5px;
    font-weight: 700;
    padding: 3px 10px 3px 3px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--card);
    box-shadow: 2px 2px 0 var(--shadow-c);
    transform: rotate(-0.8deg);
  }
  .anchor:nth-child(2n) { transform: rotate(0.9deg); }
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
  .an-state {
    flex: none;
    font-size: 10px;
    font-weight: 900;
    line-height: 1.4;
    padding: 0 6px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--card);
    color: var(--muted);
  }
  .an-state.an-good { background: var(--good); color: var(--card); }
  .an-state.an-bad { background: var(--bad); color: var(--card); }

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
  /* Sticker-like, so they tilt like the game-cards widget's pills; the data
     chips (score meter, pace/price) stay straight for legibility. */
  .tag {
    font-size: 9.5px;
    font-weight: 800;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p3);
    white-space: nowrap;
    transform: rotate(-1.2deg);
  }
  .tag:nth-child(2n) { transform: rotate(1.1deg); }
  .tag:nth-child(3n) { transform: rotate(-0.7deg); }
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
    transform: rotate(-1.4deg);
  }
  .wc-line:nth-child(2n) .wc-eyebrow { transform: rotate(1.2deg); }
  .wc-people { background: var(--p1); }
  .wc-studio { background: var(--p3); }
  .wc-hype { background: var(--p2); }
  .wc-moment { background: var(--p4); }

  /* ---- similar games ---- */
"""
    + apps_shared.SIMILAR_CSS
    + r"""
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
    transform: rotate(-0.9deg);
  }
  .tl-chip:nth-child(2n) { transform: rotate(0.8deg); }
  .errfoot { font-size: 11px; font-weight: 650; color: var(--muted); text-align: center; }
  .note-card { display: flex; gap: 12px; align-items: center; max-width: 560px; margin: 0 auto; }
  .note-text { font-size: 14px; font-weight: 750; line-height: 1.4; overflow-wrap: anywhere; }
  .note-card .stamp { font-size: 12px; max-width: 130px; padding: 7px 10px; margin: 0; }

  /* ---- click-to-enlarge overlay ---- */
  /* Anchored near the clicked thumbnail rather than centered in the (possibly
     very tall) iframe — hosts that don't auto-scroll to modals would otherwise
     open it off-screen. JS sets the panel's top and the overlay's height to
     span the whole document. */
"""
    + apps_shared.OVERLAY_CSS
    + r"""  .overlay-panel {
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
  /* The screenshot carousel goes edge-to-edge of the iframe: on a phone the
     old centered panel wasted a third of the width on backdrop. */
  .overlay-panel.carousel {
    left: 0;
    width: 100%;
    max-width: none;
    border-radius: 0;
    border-left: 0;
    border-right: 0;
    background: #0d0b07;
    transform: translateY(14px);
  }
  .overlay.open .overlay-panel.carousel { transform: none; }
"""
    + apps_shared.CAROUSEL_CSS
    + apps_shared.TOAST_CSS
    + r"""
  @media (max-width: 560px) {
    body { padding: 12px; }
    .stamp { font-size: 13px; }
  }
  @media (max-width: 480px) {
    .two-col { grid-template-columns: 1fr; }
    .lin-arrow { display: none; }
    .head .cover-wrap { flex-basis: 84px; }
    /* Narrow phone: smaller thumbs so several fit before scrolling. */
    .thumb img, .thumb-text { width: 96px; height: 55px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .overlay, .overlay-panel, .thumb, .play-badge span { transition: none; }
    .thumb:hover { transform: none; }
    .play-badge:hover span, .play-badge:focus-visible span { transform: none; }
  }
</style>
</head>
<body>
<div id="root"><div class="empty">Waiting for the evaluation…</div></div>
<script>
"""
    + apps_shared.BRIDGE_JS
    + apps_shared.EXTERNAL_LINK_JS
    + apps_shared.TOOL_RESULT_JS
    + r"""
  /* ---------- small helpers ---------- */
"""
    + apps_shared.DOM_HELPERS_JS
    + apps_shared.COVER_HUE_JS
    + r"""  /* Same cover block as the game-cards widget: real art when we have it, a
     name-seeded gradient plate when we don't. */
"""
    + apps_shared.COVER_NODE_JS
    + r"""  function hoursLabel(h) {
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

  /* ---------- best-effort fullscreen ---------- */
  /* A widget iframe is usually sandboxed, and many hosts don't grant
     allow="fullscreen" — there the API is either absent or the request is
     rejected. Both cases DROP the affordance instead of faking one: an
     inert ⛶ that does nothing is worse than no ⛶ at all. Where the host does
     allow it (and for the <video>, via its own native controls), it works. */
"""
    + apps_shared.FULLSCREEN_BUTTON_JS
    + r"""
  /* ---------- screenshot carousel (lightbox) ---------- */
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

"""
    + apps_shared.NAV_BUTTON_JS
    + r"""
  /* Edge-to-edge, and every way through the set a phone or a keyboard would
     try: drag/swipe, the two arrow buttons, and the arrow keys. */
  function openCarousel(shots, startIndex, gameName, trigger) {
    closeOverlay();
    var index = startIndex;
    var overlay = el("div", "overlay");
    var panel = el("div", "overlay-panel carousel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", (gameName ? gameName + " " : "") + "screenshots");
    panel.tabIndex = -1;

    var close = el("button", "overlay-close", "✕");
    close.setAttribute("aria-label", "Close screenshots");
    close.addEventListener("click", closeOverlay);
    panel.appendChild(close);

"""
    + apps_shared.CAROUSEL_STAGE_JS
    + r"""
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeOverlay();
    });
    var keydown = function (ev) {
      if (ev.key === "Escape") closeOverlay();
      else if (ev.key === "ArrowLeft") show(index - 1);
      else if (ev.key === "ArrowRight") show(index + 1);
    };
    document.addEventListener("keydown", keydown);
    overlayState = { node: overlay, trigger: trigger, keydown: keydown };

    document.body.appendChild(overlay);

    // Anchor the panel near whatever was clicked, clamped inside the
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

  /* ---------- 1. media: one viewer + one thumb strip ---------- */
"""
    + apps_shared.HERO_MEDIA_JS
    + r"""
  /* The trailer and the screenshots are one reel, shown the way a store page
     shows them: a single 16:9 stage plus one thumb strip, trailer first.
     Clicking a thumb swaps the stage in place; clicking a screenshot IN the
     stage opens the carousel over every delivered screenshot. */
"""
    + apps_shared.MEDIA_PANEL_JS
    + r"""
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

    // The scores belong to the identity block, not to a panel of their own:
    // "CRAFT & FIT" held two chips and a lot of air on the live card.
    var chips = scoreChips(pkg);
    if (chips) box.appendChild(chips);
    if (pres.craft_note) box.appendChild(el("div", "craft-note", pres.craft_note));
    return box;
  }

  /* The verdict in words: the one-liner, the authored pitch, and the why-care
     eyebrow — one panel, because they are one argument. */
  function pitchNode(parent, pkg) {
    var pres = pkg.presentation || {};
    var hasWhyCare = list(pres.why_care).filter(function (e) { return e && e.text; }).length;
    if (!pkg.summary && !pres.elevator_pitch && !hasWhyCare) return;
    var box = el("section", "panel");
    if (pkg.summary) box.appendChild(el("p", "one-liner", pkg.summary));
    if (pres.elevator_pitch) box.appendChild(el("p", "pitch", pres.elevator_pitch));
    whyCareNode(box, pres);
    parent.appendChild(box);
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

  /* ---------- 3. the score chips (rendered inside the header) ---------- */
  /* OpenCritic tiers are percentile-based; approximate from the score, with
     the tier palette from their own stylesheet (same mapping as apps.py). */
  function ocTier(n) {
    return "oc-" + (n >= 84 ? "mighty" : n >= 75 ? "strong" : n >= 65 ? "fair" : "weak");
  }
  /* Metacritic's games thresholds: green >=75, yellow 50-74, red <50. */
  function mcTier(n) { return n >= 75 ? "mc-hi" : n >= 50 ? "mc-mid" : "mc-lo"; }
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
  function scoreChips(pkg) {
    var craft = pkg.craft || {};
    var row = el("div", "chips head-chips");

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

    // Providers use negative sentinels for "no score yet" — never show those.
    var mc = num(craft.metacritic_score);
    if (mc != null && mc >= 0) {
      var mcChip = el("span", "chip mc " + mcTier(mc));
      mcChip.appendChild(el("span", "lbl", "MC"));
      mcChip.appendChild(el("span", null, String(Math.round(mc))));
      mcChip.title = "Metacritic";
      row.appendChild(mcChip);
    }

    var oc = num(craft.opencritic_score);
    if (oc != null && oc >= 0) {
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

    return row.childNodes.length ? row : null;
  }

  /* ---------- 4. for you / not for you ---------- */
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

  /* ---------- 5. anchors ---------- */
  /* The PILL is neutral — an anchor is evidence, and half of them are
     negative ("Cyberpunk 2077, 6.6h"). Only the status glyph is coloured:
     completed/evergreen good, abandoned bad, anything else plain. */
  var COMPLETION = {
    completed: ["✓", "completed", "an-good"],
    evergreen: ["∞", "evergreen", "an-good"],
    playing: ["▶", "playing", ""],
    abandoned: ["⚠", "abandoned", "an-bad"],
  };
  function anchorsNode(parent, anchors) {
    if (!anchors.length) return;
    var box = section(parent, "Grounded in your history");
    var chips = el("div", "chips");
    anchors.forEach(function (a) {
      var state = COMPLETION[a.completion_status];
      var chip = el("div", "anchor" + (a.cover_url ? "" : " no-cover"));
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
        var glyph = el("span", "an-state" + (state[2] ? " " + state[2] : ""), state[0]);
        glyph.title = state[1];
        chip.appendChild(glyph);
      }
      chips.appendChild(chip);
    });
    box.appendChild(chips);
  }

  /* ---------- 6. lineage / comparisons ---------- */
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
"""
    + apps_shared.OWNERSHIP_TAGS_JS
    + r"""  function lineageColumn(title, entries) {
    var col = el("div", "lin-col");
    col.appendChild(el("div", "lin-head", title));
    entries.forEach(function (c) { col.appendChild(comparisonNode(c)); });
    return col;
  }
  /* Every comparison lives here, "similar" included: folding the authored
     similar-notes into the IGDB strip below mixed two different things (his
     model's reading vs. IGDB's neighbours) and read as one confused list. */
  function lineageNode(parent, pkg, comps) {
    var callouts = comps.filter(function (c) { return CALLOUT_HEADS[c.relation]; });
    var ancestors = comps.filter(function (c) { return c.relation === "ancestor"; });
    var descendants = comps.filter(function (c) { return c.relation === "descendant"; });
    var loose = comps.filter(function (c) {
      return !CALLOUT_HEADS[c.relation] && c.relation !== "ancestor" && c.relation !== "descendant";
    });
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
      // Labelled, because these note-cards now sit above IGDB's own similar
      // strip and the two must not read as one list.
      var onlySimilar = loose.every(function (c) { return c.relation === "similar"; });
      var head = el("div", "lin-head", onlySimilar ? "Also similar" : "Other comparisons");
      head.style.marginTop = (callouts.length || ancestors.length || descendants.length)
        ? "12px" : "0";
      box.appendChild(head);
      var rest = el("div", "chips");
      rest.style.marginTop = "7px";
      loose.forEach(function (c) { rest.appendChild(comparisonNode(c)); });
      box.appendChild(rest);
    }
  }

  /* ---------- 7. similar games (IGDB only) ---------- */
"""
    + apps_shared.SIMILAR_NODE_JS
    + r"""
  /* ---------- 8. from the studio (pedigree) ---------- */
  /* Server-fetched and library-annotated (tools/game_media.py): who made this,
     and what they shipped BEFORE it. Under the big-studio damper, or with
     nothing released earlier, only the header line renders — six arbitrary
     posters out of a 500-game catalogue say nothing about this game. Mirrors
     the detail card's implementation (apps.py) by hand, like every other block
     these two widgets share. */
  /* "1 games" was on the live card. A count is only singular when it is
     exactly one AND not a floor ("1+ games" stays plural). */
"""
    + apps_shared.PEDIGREE_JS
    + r"""
  /* ---------- 9. the call: time, price, flags, past verdicts ---------- */
  /* One closing panel instead of three: three boxes holding a chip apiece was
     the other half of the "too busy" complaint. */
  function factChip(row, label, value) {
    var chip = el("span", "chip");
    chip.appendChild(el("span", "lbl", label));
    chip.appendChild(el("span", null, value));
    row.appendChild(chip);
  }
  function factChips(pkg) {
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

    return row.childNodes.length ? row : null;
  }

  function flagsRow(flags) {
    if (!flags.length) return null;
    var chips = el("div", "chips");
    chips.style.marginTop = "10px";
    flags.forEach(function (f) { chips.appendChild(el("span", "flag", String(f))); });
    return chips;
  }
  function pastRow(past) {
    var items = list(past.items).slice();
    if (!items.length) return null;
    items.sort(function (a, b) {                    // newest first
      return String(b.assessed_at || "").localeCompare(String(a.assessed_at || ""));
    });
    var line = el("div", "timeline");
    line.style.marginTop = "10px";
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
    return line;
  }
  function closingNode(parent, pkg) {
    var facts = factChips(pkg);
    var flags = flagsRow(list(pkg.flags).filter(Boolean));
    var past = pastRow(pkg.past || {});
    if (!facts && !flags && !past) return;
    var box = section(parent, "The call");
    if (facts) box.appendChild(facts);
    [flags, past].forEach(function (row) {
      if (!row) return;
      if (!box.querySelector(".chips, .timeline")) row.style.marginTop = "0";
      box.appendChild(row);
    });
  }
  function errorsNode(parent, errors) {
    if (!errors.length) return;
    // Deliberately quiet: a missing trailer is not an incident.
    var note = el("div", "errfoot", "some data unavailable");
    note.title = errors.join("; ");
    parent.appendChild(note);
  }

  /* ---------- card assembly ---------- */
  /* Read top to bottom: what it is (header + scores), what it says (pitch),
     what it looks like (media), whether it is for HIM (for-you, anchors),
     where it comes from (lineage, similar, studio), and the call. */
  function evalCard(pkg) {
    var wrap = el("div", "eval");
    var game = pkg.game || {};

    wrap.appendChild(headerNode(pkg));
    pitchNode(wrap, pkg);
    mediaNode(wrap, pkg.media || {}, game.name);
    forYouNode(wrap, pkg.presentation || {});
    anchorsNode(wrap, list(pkg.anchors).filter(function (a) { return a && a.name; }));

    var comps = list(pkg.comparisons).filter(function (c) { return c && c.name; });
    lineageNode(wrap, pkg, comps);
    similarNode(wrap, pkg.similar || {});
    pedigreeNode(wrap, pkg.pedigree);

    closingNode(wrap, pkg);
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
    } else if (data && data.verdict) {
      root.appendChild(noteCard("Recorded: " + data.verdict + name, data.verdict));
    } else {
      root.appendChild(el("div", "empty", "Nothing to display."));
    }
    reportSize();
  }

"""
    + apps_shared.SIZING_JS
    + r"""
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
)


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
