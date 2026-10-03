"""MCP Apps (io.modelcontextprotocol/ui): the evaluation-card widget.

One `ui://` resource renders `record_assessment` results: the full
evaluation package when the response carries one, and a compact note card for
the bookkeeping-only responses (a plain recorded verdict, or a void).

The package card is a Binder card (spec 2026-10-04, artboards B / C / G). The
FRAME is the verdict: its rarity border is the verdict's tier (buy_now and
play_what_you_own good, wishlist_for_sale and try_demo ok, skip bad), the
cover art window carries the critic badge (OpenCritic, else Metacritic), the
plate names the game and its card number (the assessment id, else the game
id), the stat block is the call — pace, price seen, target, length, paid, fit
pips — and the straddling ribbon states the verdict in words with its one
mono note ("wait for ~€40"). Under it, on the ground, in reading order: the
candidate / ownership line, the summary, the WEAKNESS traits, the pitch, the
why-care abilities, the craft note as flavor text, the media reel (one stage
plus one thumb strip, trailer first, screenshots opening an edge-to-edge
carousel), the IN YOUR LIBRARY strip, one action row ("Full breakdown", plus
"Store page" when the package carries a Steam app id), the provenance line
and a single notice naming what failed to load. From 560px the frame stands
left of the ground and the reel spans both columns below.

The FULL BREAKDOWN — for-you-if / not-for-you-if, the anchors it rests on,
lineage, the studio, past verdicts and the failure detail — opens fullscreen
where the host offers it (the card stays left, the breakdown joins the right
column, the host's close button is the way back) and as an in-place
disclosure where it doesn't. Clients that don't speak the Apps extension
ignore the tool metadata and see the normal JSON, so attaching
``EVAL_CARD_APP`` to a tool is purely additive.

Every section of the package is optional: an unowned candidate with no appid
and no IGDB match gets a media-less card (and an untagged one no library
strip), and each block is skipped rather than rendered empty.

Visual language: the host's theme, through the shared ``--gl-*`` token layer
and the Binder components of apps_shared.py, on a transparent page. Colour is
the quality tier only. Motion is the shared deal-in (MOTION.md M1: the card,
the ribbon stamp, the badge pop, the pips and the stat leaders, then the
ribbon's 2px settle), once per render, resolving out of the skeleton.

Media routing follows the 2026-08-28 spike (docs/plans/…-evaluation-package
-design.md): a Steam trailer is a plain ``<video controls preload="none">``
over the constructed legacy mp4 renditions — undocumented Valve surface, so
the widget falls back to poster + link-out on the media error event — while
an IGDB-only candidate gets a click-to-load youtube-nocookie iframe (no
YouTube bytes before the click) plus a link-out pill, because a CSP-blocked
nested frame is not detectable from JS.

The HTML is deliberately dependency-free: the host↔iframe bridge is the
hand-rolled JSON-RPC postMessage handshake from the MCP Apps spec
(ui/initialize → ui/notifications/initialized → tool-input / tool-result,
host-context-changed) rather than @modelcontextprotocol/ext-apps, so nothing
is fetched from a CDN and the CSP only has to allow the media hosts below.

For local visual iteration outside any MCP host, the widget renders
``window.__PREVIEW_DATA__`` when present instead of waiting on the bridge, and
applies ``window.__PREVIEW_HOST_CONTEXT__`` as if the host had sent it — see
scripts/preview_eval_card.py.
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
# posters (shared.*, which is where appdetails actually serves them),
# YouTube thumbnails for IGDB trailers, and assets.claude.ai for the host
# fonts. Everything else stays deny-by-default.
_EVAL_CARD_CSP = ResourceCSP(
    resource_domains=[
        "https://images.igdb.com",
        "https://cdn.cloudflare.steamstatic.com",
        "https://cdn.akamai.steamstatic.com",
        "https://shared.akamai.steamstatic.com",
        "https://shared.cloudflare.steamstatic.com",
        "https://i.ytimg.com",
        "https://assets.claude.ai",
    ],
    frame_domains=["https://www.youtube-nocookie.com"],
)

EVAL_CARD_HTML = (
    r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>
"""
    + apps_shared.TOKENS_CSS
    + apps_shared.RESET_CSS
    + apps_shared.A11Y_CSS
    + apps_shared.PANEL_CSS
    + apps_shared.COMPONENTS_CSS
    + apps_shared.BINDER_CSS
    + r"""  .eval {
    max-width: 760px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  /* ---- shared cover block (same shape as the game-cards widget) ---- */
"""
    + apps_shared.COVER_CSS
    + r"""  .cover-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--gl-h);
    font-weight: var(--gl-heavy);
    line-height: var(--gl-h-lh);
    color: var(--gl-plate-ink);
    text-shadow: 0 1px 3px var(--gl-plate-shadow);
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
"""
    + apps_shared.SHOT_BTN_CSS
    + r"""  /* Best-effort fullscreen. The button is only rendered where the API exists
     AND the host allows it — a sandboxed iframe without allow="fullscreen"
     reports fullscreenEnabled false, and a denied request removes the button
     rather than pretending anything happened. */
"""
    + apps_shared.MEDIA_STRIP_CSS
    + r"""
  /* ---- the card: frame (verdict tier), art + badge, plate, stats, ribbon ---- */
  /* 300px wide, or narrower so the straddling ribbon's 20px overhang on each
     side still fits the page; the wrap's bottom padding is the ribbon's
     20px below the frame (plus room for its settle). */
  .ev-top { display: flex; flex-direction: column; gap: 14px; }
  .ev-left { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .ev-cardwrap { padding: 4px 0 26px; }
  .ev-card { width: min(300px, 100% - 40px); margin: 0 auto; padding-bottom: 28px; }
  /* The mock-up's crop: a fixed window over the cover, pinned to its top so
     the wordmark survives, the badge low on the left clear of it. */
  .ev-card > .art { aspect-ratio: auto; height: 230px; }
  .ev-card > .art .cover-wrap img { object-position: 50% 0; }
  .ev-card .stat-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .ev-card .ev-scores { padding: 8px 12px 0; }
  .ev-fit { display: inline-flex; align-items: center; gap: 8px; }
"""
    + apps_shared.STRIP_CSS
    + apps_shared.TAG_CSS
    + apps_shared.PEDIGREE_CSS
    + r"""
  /* ---- the ground: candidate line, summary, traits, pitch, abilities ---- */
  .ev-flow { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .ev-cand {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px 8px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
  }
  .ev-cand > svg { width: 14px; height: 14px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.5; }
  .ev-cand.is-candidate > svg { stroke-dasharray: 2.2 1.6; }
  .ev-sum {
    margin-top: -4px;
    font-size: var(--gl-h);
    line-height: var(--gl-h-lh);
    font-weight: var(--gl-strong);
    overflow-wrap: anywhere;
  }
  .ev-weak, .ev-pitch { margin-top: 6px; }
  .ev-pitch { color: var(--gl-text-2); }
  .ev-abil { display: flex; flex-direction: column; gap: 10px; }
  .ev-prov {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 10px;
    margin-top: -6px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
  }
  /* ---- one copy of every part, placed per display mode ---- */
  /* The candidate line and the provenance are built once, in .ev-left under
     the card, which is where fullscreen shows them. Inline, .ev-top and
     .ev-left dissolve (display: contents) so the card, the candidate line,
     the ground and the provenance are the page's own flex items (grid items
     from 560px): the candidate line reads under the card (atop the ground
     from 560px) and `order` moves the provenance after the action row.
     Fullscreen reads the library strip before the reel. */
  html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-top,
  html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-left { display: contents; }
  .ev-pkg .ev-prov, .ev-pkg > .notice, .ev-pkg > .disclosure-body { order: 1; }
  html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-cand,
  html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-flow { margin-top: -6px; }
  html[data-display-mode="fullscreen"] .ev-pkg > .ev-top { order: -2; }
  html[data-display-mode="fullscreen"] .ev-pkg > .ev-lib { order: -1; }
  html[data-display-mode="fullscreen"] .ev-left > .ev-cand, html[data-display-mode="fullscreen"] .ev-left > .ev-prov { padding: 0 20px; }
  /* Sections are eyebrow + content on the ground, never boxed panels. */
  .eval .panel { background: none; border: 0; box-shadow: none; padding: 0; }

  /* ---- the reel: one stage, thumbs 3-up (4-up from 560px), the rest scroll ---- */
  .media-slot > .panel { position: relative; }
  .media-slot .thumbs { gap: 6px; }
  .media-slot .thumb { flex: 0 0 calc((100% - 12px) / 3); }
  .media-slot .thumb img, .media-slot .thumb-text { width: 100%; height: auto; aspect-ratio: 16 / 9; }

  /* ---- breakdown: trait columns, minis, lineage, the past-verdict ledger ---- */
  .ev-bd > .disclosure-inner { gap: 24px; }
  .ev-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
  .ev-pair.ev-one { grid-template-columns: minmax(0, 1fr); }
  .ev-lin-col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .ev-lin-head { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); font-weight: var(--gl-strong); color: var(--gl-text-2); }
  .ev-note { margin-top: 6px; font-size: var(--gl-cap); line-height: var(--gl-cap-lh); color: var(--gl-text-2); }
  .eyebrow-sec > .ped-head { margin-bottom: 10px; }
  .ev-past { width: 100%; border-collapse: collapse; font-size: var(--gl-cap); line-height: var(--gl-cap-lh); }
  .ev-past th {
    padding: 0 10px 6px 0;
    text-align: left;
    font-weight: var(--gl-strong);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--gl-muted);
  }
  .ev-past td {
    padding: 10px 10px 10px 0;
    border-top: 1px solid var(--gl-border);
    border-bottom: 1px solid var(--gl-border);
    vertical-align: middle;
    white-space: nowrap;
    color: var(--gl-text-2);
  }
  .ev-past th:last-child, .ev-past td:last-child { padding-right: 0; text-align: right; }
  .ev-past .v { font-family: var(--gl-mono); }
  .ev-past .ribbon { display: inline-flex; vertical-align: middle; }
  .ev-stack { display: flex; flex-direction: column; align-items: flex-start; }
  .ev-past td:last-child .ev-stack { align-items: flex-end; }
  .err-list { list-style: none; display: flex; flex-direction: column; gap: 4px; color: var(--gl-text-2); }
  .err-list b { font-weight: var(--gl-strong); color: var(--gl-text); }

  /* ---- action row, in-place breakdown, fullscreen breakdown ---- */
  .actions { display: flex; gap: 10px; }
  .actions > .btn, .actions > .disclosure {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    font-size: var(--gl-h);
  }
  .eval > .disclosure-body { margin-top: 0; }
  .fs-breakdown { display: none; flex-direction: column; gap: 24px; }
  html[data-display-mode="fullscreen"] .fs-breakdown { display: flex; }
  /* Fullscreen IS the breakdown, so its button goes — but the store link
     stays: fullscreen is exactly where he has decided to read everything,
     and the next step from there is the store page. The candidate line and
     the provenance stand under the card; the breakdown reads after the
     pitch and before the weaknesses. */
  html[data-display-mode="fullscreen"] .actions:not(.has-store),
  html[data-display-mode="fullscreen"] .actions .act-breakdown,
  html[data-display-mode="fullscreen"] .eval > .disclosure-body { display: none; }
  html[data-display-mode="fullscreen"] .actions { justify-content: flex-end; }
  html[data-display-mode="fullscreen"] .ev-flow > .ev-sum { order: 1; margin-top: 0; }
  html[data-display-mode="fullscreen"] .ev-flow > .ev-pitch { order: 2; margin-top: -4px; }
  html[data-display-mode="fullscreen"] .ev-flow > .fs-breakdown { order: 3; margin: 10px 0; }
  html[data-display-mode="fullscreen"] .ev-flow > .ev-weak { order: 4; }
  html[data-display-mode="fullscreen"] .ev-flow > .ev-abil { order: 5; }
  html[data-display-mode="fullscreen"] .ev-flow > .flavor { order: 6; }

  /* ---- note cards: a horizontal small frame, its ribbon, one caption ---- */
  .ev-notes { max-width: 560px; gap: 10px; }
  .ev-nc { flex-direction: row; align-items: center; gap: 12px; padding: 10px; }
  .ev-nc > .art { flex: none; width: 48px; height: 64px; aspect-ratio: auto; border-radius: var(--gl-r-sm); }
  .ev-nc .cover-fallback { color: transparent; text-shadow: none; }
  .ev-nc-body { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
  .ev-nc-cap { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); color: var(--gl-muted); }
  .ev-nc-body > .sub {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 8px;
    font-size: var(--gl-cap);
    line-height: 20px;
    color: var(--gl-muted);
  }
  .ev-nc-body > .sub > .v { font-family: var(--gl-mono); color: var(--gl-text-2); }
  .ev-void .ribbon { background: var(--gl-inset); color: var(--gl-text); box-shadow: none; }

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
    width: calc(100% - 24px);
    max-width: 900px;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-lg);
    background: var(--gl-surface);
    overflow: hidden;
    transform: translateX(-50%) scale(0.97) translateY(8px);
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
    background: var(--gl-stage);
    transform: translateY(14px);
  }
  .overlay.open .overlay-panel.carousel { transform: none; }
"""
    + apps_shared.CAROUSEL_CSS
    + apps_shared.TOAST_CSS
    + r"""
  /* Container ≥560px: the frame stands left (300px + the ribbon's overhang),
     the ground reads right, and the reel spans both columns below. */
  @media (min-width: 560px) {
    .ev-top {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      column-gap: 24px;
      align-items: start;
    }
    .ev-flow { padding-top: 4px; }
    html[data-display-mode="fullscreen"] .ev-left { position: sticky; top: 12px; }
    /* Inline, the page itself is the two-column grid: the card down the
       left, spanning the 1fr row that absorbs whatever it is taller than
       the right column; the candidate line and the ground on the right;
       everything after them across both columns. Spacing is margins (no
       row gap), so an empty track adds nothing. */
    html:not([data-display-mode="fullscreen"]) .ev-pkg {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      grid-template-rows: auto auto 1fr;
      column-gap: 24px;
      row-gap: 0;
      align-items: start;
    }
    html:not([data-display-mode="fullscreen"]) .ev-pkg > *, html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-prov { grid-column: 1 / -1; margin-top: 20px; }
    html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-prov { margin-top: 14px; }
    html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-cardwrap { grid-column: 1; grid-row: 1 / 4; }
    html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-cand { grid-column: 2; grid-row: 1; margin: 4px 0 10px; }
    html:not([data-display-mode="fullscreen"]) .ev-pkg .ev-flow { grid-column: 2; grid-row: 2; margin-top: 0; }
    .media-slot .thumb { flex-basis: calc((100% - 18px) / 4); }
  }
  @media (max-width: 479px) {
    .ev-pair { grid-template-columns: minmax(0, 1fr); }
  }
</style>
</head>
<body>
<div id="root"></div>
<script>
"""
    + apps_shared.BRIDGE_JS
    + apps_shared.EXTERNAL_LINK_JS
    + apps_shared.TOOL_RESULT_JS
    + r"""
  /* ---------- small helpers ---------- */
"""
    + apps_shared.DOM_HELPERS_JS
    + apps_shared.COMPONENTS_JS
    + apps_shared.BINDER_JS
    + apps_shared.COVER_HUE_JS
    + r"""  /* Same cover block as the game-cards widget: real art when we have it, a
     name-seeded gradient plate when we don't. */
"""
    + apps_shared.COVER_NODE_JS
    + r"""
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
  var overlayState = null; // { node, trigger, keydown, focusin }

  function closeOverlay() {
    if (!overlayState) return;
    var s = overlayState;
    overlayState = null;
    document.removeEventListener("keydown", s.keydown, true);
    document.removeEventListener("focusin", s.focusin, true);
    s.node.classList.remove("open");
    setTimeout(function () { s.node.remove(); }, 200);
    if (s.trigger && s.trigger.focus) s.trigger.focus({ preventScroll: true });
  }

"""
    + apps_shared.NAV_BUTTON_JS
    + apps_shared.LIGHTBOX_CHROME_JS
    + r"""
  /* Edge-to-edge, and every way through the set a phone or a keyboard would
     try: drag/swipe, the two arrow buttons, and the arrow keys. The dialog
     chrome — ✕, focus trap, Escape and arrow routing — is the shared one. */
  function openCarousel(shots, startIndex, gameName, trigger) {
    closeOverlay();
    var index = startIndex;
    var overlay = el("div", "overlay");
    var chrome = lightboxPanel("overlay-panel carousel", gameName, closeOverlay);
    var panel = chrome.panel;

"""
    + apps_shared.CAROUSEL_STAGE_JS
    + r"""
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeOverlay();
    });
    var keydown = lightboxKeys(panel, function (delta) { show(index + delta); }, closeOverlay);
    var focusin = focusGuard(panel);
    document.addEventListener("keydown", keydown, true);
    document.addEventListener("focusin", focusin, true);
    overlayState = { node: overlay, trigger: trigger, keydown: keydown, focusin: focusin };

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
    chrome.closer.focus({ preventScroll: true });
  }

  /* ---------- media: one viewer + one thumb strip ---------- */
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
  /* ---------- small local helpers ---------- */
  /* The verdict's tier (spec 2026-10-04 §1.3): the frame's rarity border and
     the ribbon's fill. The ribbon's words are label("verdict", …). */
  var VERDICT_TIERS = {
    buy_now: "good",
    play_what_you_own: "good",
    wishlist_for_sale: "ok",
    try_demo: "ok",
    skip: "bad",
  };
  function verdictTier(verdict) {
    return Object.prototype.hasOwnProperty.call(VERDICT_TIERS, verdict) ? VERDICT_TIERS[verdict] : "none";
  }
  function capFirst(text) {
    var t = String(text);
    return t.charAt(0).toUpperCase() + t.slice(1);
  }
  function ratedTier(rating) {
    var n = num(rating);
    return n == null ? "none" : ratingTier(n);
  }

  /* ---------- 1. the frame: art + badge, plate, stats, ribbon ---------- */
  /* The plate: title and card number, then the sub spans — the developer,
     the release year, the platform lozenge (where the price was seen, else
     the first platform he owns it on) — gap-separated, never middots. */
  function plateNode(pkg, no) {
    var game = pkg.game || {};
    var dev = (pkg.pedigree || {}).developer || {};
    var own = pkg.ownership || {};
    var price = pkg.price || {};
    var plate = el("div", "plate");
    var row = el("div", "plate-row");
    row.appendChild(el("h2", "plate-title", game.name || "Unknown game"));
    if (no) row.appendChild(el("span", "card-no", no));
    plate.appendChild(row);
    var sub = el("div", "sub");
    if (dev.name) sub.appendChild(el("span", null, String(dev.name)));
    if (game.release_year) sub.appendChild(el("span", null, String(game.release_year)));
    var platform = price.platform || list(own.platforms).filter(Boolean)[0];
    if (platform) sub.appendChild(el("span", "loz", label("platform_short", platform)));
    if (sub.childNodes.length) plate.appendChild(sub);
    return plate;
  }
  /* Fit as pips: the word ("Strong", "Probable", "Coin flip", "Probable
     miss" — the label already says fit) and 1–3 lit diamonds in its tier. */
  var FIT_PIPS = {
    "strong fit": [3, "good"],
    "probable fit": [2, "good"],
    "coin flip": [1, "ok"],
    "probable miss": [1, "bad"],
  };
  function fitStat(call) {
    var has = Object.prototype.hasOwnProperty.call(FIT_PIPS, call);
    var pips = has ? FIT_PIPS[call] : [0, "none"];
    var fit = String(call).replace(/\s+fit$/i, "");
    var value = el("span", "ev-fit");
    value.appendChild(el("span", "pips-word", capFirst(fit)));
    value.appendChild(pipsNode(pips[0], 3, pips[1]));
    var row = statRow({ label: "Fit", value: value });
    row.classList.add("tier-" + pips[1]);
    row.title = "How well it fits your taste";
    return row;
  }
  /* The call, as a stat block (at most six rows): his pace, the price seen,
     the target (the key figure), the length, what he paid, the fit. */
  var STATS_CAP = 6;
  function statsNode(pkg) {
    var time = pkg.time || {};
    var price = pkg.price || {};
    var own = pkg.ownership || {};
    var rows = [];
    var weekly = num(time.recent_weekly_minutes);
    if (weekly != null && weekly > 0) {
      rows.push(statRow({ label: "Pace", note: "last 30d", value: hoursLabel(weekly / 60, true) + "/wk" }));
    }
    var seen = money(price.seen, price.currency);
    if (seen) {
      rows.push(statRow({
        label: "Seen", note: price.platform ? "on " + label("platform", price.platform) : null, value: seen,
      }));
    }
    var target = money(price.target, price.currency);
    if (target) rows.push(statRow({ label: "Target", value: target, key: true }));
    var main = hoursLabel(time.hltb_main_hours);
    var extra = hoursLabel(time.hltb_extra_hours);
    if (main) rows.push(statRow({ label: "Length", note: "main story", value: main }));
    else if (extra) rows.push(statRow({ label: "Length", note: "with extras", value: extra }));
    var paid = money(own.price_paid, own.price_currency);
    if (paid) {
      var how = own.bundle_name ? "in " + own.bundle_name
        : own.purchase_source ? "via " + label("purchase_source", own.purchase_source) : null;
      rows.push(statRow({ label: "Paid", note: how, value: paid }));
    }
    if (pkg.fit_call) rows.push(fitStat(pkg.fit_call));
    if (!rows.length) return null;
    var box = el("div", "stats");
    rows.slice(0, STATS_CAP).forEach(function (r) { box.appendChild(r); });
    return box;
  }

  /* The secondary scores, as the shared chips under the stats: the review
     share, a moving trend (a stable one says nothing), and the critic score
     the badge does not already show. */
  var TRAJECTORIES = {
    improving: ["↗ Improving", "good"],
    regressing: ["↘ Regressing", "bad"],
  };
  /* The stored ranges (tools/assessment.py's _check_range): craft.adjusted
     is a 0..1 sample-adjusted share, rescaled here; positive_pct is the raw
     review percentage, already 0..100, so 1 means 1% — never rescaled. It
     only stands in when there's no adjusted figure. */
  function craftPercent(craft) {
    var adjusted = num(craft.adjusted);
    if (adjusted != null) return Math.round(adjusted * 100);
    var raw = num(craft.positive_pct);
    return raw == null ? null : Math.round(raw);
  }
  function scoreChips(pkg) {
    var craft = pkg.craft || {};
    var badge = leadCritic(craft);
    var row = el("div", "chips tags ev-scores");

    var pct = craftPercent(craft);
    if (pct != null) {
      var rawPct = num(craft.positive_pct);
      var count = compactCount(craft.review_count, "review");      // "114k reviews"
      row.appendChild(scoreChip({
        label: "Reviews", value: pct + "% positive", tier: craftTier(pct), meter: pct,
        aux: count,
        title: "Sample-adjusted share of positive reviews"
          + (rawPct != null ? " (raw " + Math.round(rawPct) + "% positive)" : "")
          + (count ? ", from " + count : ""),
      }));
    }

    var traj = TRAJECTORIES[craft.trajectory];
    if (traj) {
      row.appendChild(scoreChip({
        label: "Trend", value: traj[0], tier: traj[1], title: "Recent review trajectory",
      }));
    }

    var mc = num(craft.metacritic_score);
    if (realScore(mc) && !(badge && badge.source === "Metacritic")) {
      row.appendChild(scoreChip({ label: "Metacritic", value: Math.round(mc), tier: mcTier(mc) }));
    }
    return row.childNodes.length ? row : null;
  }

  /* The ribbon's one note: the price to wait for, "try first", or nothing. */
  function ribbonNote(pkg) {
    var price = pkg.price || {};
    if (pkg.verdict === "wishlist_for_sale") {
      var target = money(price.target, price.currency);
      return target ? "wait for ~" + target.replace(/\.00(?!\d)/, "") : null;
    }
    return pkg.verdict === "try_demo" ? "try first" : null;
  }
  function cardNode(pkg, no) {
    var game = pkg.game || {};
    var tier = verdictTier(pkg.verdict);
    var verdict = label("verdict", pkg.verdict);
    var frame = frameNode("article", "ev-card", tier);
    frame.setAttribute("aria-label", (game.name || "Unknown game") + (verdict ? ": " + verdict : ""));

    var art = el("div", "art");
    art.appendChild(coverNode(game));
    // One overall badge: the shared lead critic (OpenCritic, else
    // Metacritic); none when neither has spoken.
    var critic = leadCritic(pkg.craft);
    var badge = critic ? badgeNode({ value: critic.value, tag: critic.source, tier: critic.tier }) : null;
    if (badge) {
      badge.classList.add("badge-on-art");
      badge.classList.add("badge-low");
      art.appendChild(badge);
    }
    frame.appendChild(art);
    frame.appendChild(plateNode(pkg, no));
    var stats = statsNode(pkg);
    if (stats) frame.appendChild(stats);
    var chips = scoreChips(pkg);
    if (chips) frame.appendChild(chips);
    if (verdict) frame.appendChild(ribbonNode(verdict, tier, ribbonNote(pkg), "straddle"));
    return frame;
  }

  /* ---------- 2. the ground: candidate line, summary, weakness, pitch ---------- */
  var CAND_ICON = [["circle", { cx: 7, cy: 7, r: 5.5 }]];
  /* "Candidate  Not owned, not wishlisted" (a dashed ring) or "Owned  PS5,
     Steam  52h played" — separate spans, never middots. Positive hours only:
     zero is authoritative NOT-played; null is unknown and says nothing. */
  function candidateNode(pkg) {
    var own = pkg.ownership;
    if (!own) return null;
    var line = el("div", "ev-cand");
    var icon = iconNode("0 0 14 14", CAND_ICON);
    if (icon) line.appendChild(icon);
    var parts;
    if (own.owned) {
      line.classList.add("is-owned");
      var platforms = list(own.platforms).filter(Boolean)
        .map(function (p) { return label("platform", p); });
      var played = own.playtime_hours > 0 ? hoursLabel(own.playtime_hours) : null;
      parts = ["Owned", platforms.join(", "),
        played ? played + " played" : num(own.playtime_hours) === 0 ? "unplayed" : null];
    } else {
      line.classList.add("is-candidate");
      parts = ["Candidate", own.wishlisted ? "On your wishlist" : "Not owned, not wishlisted"];
    }
    parts.filter(Boolean).forEach(function (p) { line.appendChild(el("span", null, p)); });
    return line;
  }
  function traitsNode(head, tier, kind, items, cls) {
    var box = el("div", "traits" + (cls ? " " + cls : ""));
    box.appendChild(el("div", "traits-head tier-" + tier, head));
    items.forEach(function (text) { box.appendChild(traitNode(kind, capFirst(text))); });
    return box;
  }
  /* why_care: up to three model-authored run-ins answering "why look at this
     at all" — the editorial counterpart to the server-fetched studio strip
     in the breakdown. Absent when the recording client authored none. */
  var WHY_CARE_KINDS = {
    people: "People",
    studio: "Studio",
    anticipation: "Anticipation",
    moment: "Moment",
  };
  function abilitiesNode(pres) {
    var entries = list(pres.why_care).filter(function (e) { return e && e.text; });
    if (!entries.length) return null;
    var box = el("div", "ev-abil");
    entries.slice(0, 3).forEach(function (entry) {
      var known = Object.prototype.hasOwnProperty.call(WHY_CARE_KINDS, entry.kind);
      box.appendChild(abilityNode(known ? WHY_CARE_KINDS[entry.kind] : humanize(entry.kind || "why"),
        String(entry.text)));
    });
    return box;
  }
  function groundNode(flow, pkg) {
    var pres = pkg.presentation || {};
    if (pkg.summary) flow.appendChild(el("p", "ev-sum", pkg.summary));
    var flags = list(pkg.flags).filter(Boolean);
    if (flags.length) flow.appendChild(traitsNode("Weakness", "bad", "minus", flags, "ev-weak"));
    if (pres.elevator_pitch) flow.appendChild(el("p", "ev-pitch", pres.elevator_pitch));
    var abilities = abilitiesNode(pres);
    if (abilities) flow.appendChild(abilities);
    if (pres.craft_note) flow.appendChild(flavorNode(pres.craft_note));
  }

  /* ---------- 3. media (one viewer + one thumb strip) ---------- */
  /* The shared mediaNode builds the reel; the slot lets the card size the
     thumbs (3-up, 4-up from 560px) without touching shared code. */
  function mediaSlot(parent, media, gameName) {
    var slot = el("div", "media-slot");
    mediaNode(slot, media, gameName);
    if (!slot.childNodes.length) return;
    var title = slot.querySelector(".section-title");
    if (title) title.classList.add("sr-only");
    if (slot.querySelector(".thumbs")) slot.classList.add("has-thumbs");
    parent.appendChild(slot);
  }

  /* ---------- 4. mini-card strips: library, history, studio ---------- */
  /* Every mini reads the shared miniLines format; a library item's
     similarity and shared tags are its hover text. similar_in_library
     carries no completion status or platform, so its minis read hours
     ("50h" "played") or "unplayed" only. */
  function similarTitle(item) {
    var sim = num(item.similarity);
    var why = list(item.shared_tags).filter(Boolean).slice(0, 3);
    var parts = [];
    if (sim != null) parts.push(Math.round(sim * 100) + "% similar");
    if (why.length) parts.push("shares " + why.join(", "));
    return parts.length ? parts.join("; ") : null;
  }
  var STRIP_CAP = 8;
  function ministrip(box, items, toMini) {
    var strip = el("div", "strip ministrip");
    items.slice(0, STRIP_CAP).forEach(function (item) { strip.appendChild(miniCard(toMini(item))); });
    box.appendChild(strip);
  }
  function libraryNode(parent, similar) {
    var items = named((similar || {}).items);
    if (!items.length) return;
    var box = el("section", "ev-lib");
    box.appendChild(el("div", "section-title", "In your library"));
    ministrip(box, items, function (item) {
      return { name: item.name, cover_url: item.cover_url, tier: ratedTier(item.my_rating),
        title: similarTitle(item),
        lines: miniLines({ rating: item.my_rating, hours: item.playtime_hours, unplayed: item.unplayed,
          year: item.release_year }) };
    });
    parent.appendChild(box);
  }
"""
    + apps_shared.PEDIGREE_JS
    + r"""
  /* ---------- 6. what failed: one notice inline, the detail in the breakdown ---------- */
  /* package.errors are "<block>: <reason>" strings ("media: steam: …",
     "igdb: unresolved — …"). The notice names WHAT is missing and from WHERE
     ("Couldn't load: similar games (library)"); the reasons stay in its
     tooltip and in the breakdown's last section. Each known block maps to
     [what, where]; "media" takes its source from the provider prefix of its
     reason, and an unknown block is its humanized key with no source. */
  var ERROR_BLOCKS = {
    media: ["media", null],
    similar: ["similar games", "library"],
    anchors: ["your history", "library"],
    pace: ["your pace", "library"],
    pedigree: ["studio", "IGDB"],
    studio: ["studio", "IGDB"],
    igdb: ["studio", "IGDB"],
    steam: ["store data", "Steam"],
    hltb: ["time to beat", "HowLongToBeat"],
    package: ["evaluation details", null],
  };
  var ERROR_REASON_CAP = 120;
  function errorItem(text) {
    var raw = String(text);
    var cut = raw.indexOf(":");
    var key = (cut < 0 ? raw : raw.slice(0, cut)).trim().toLowerCase();
    var why = cut < 0 ? "" : raw.slice(cut + 1).trim();
    var has = Object.prototype.hasOwnProperty;
    var known = has.call(ERROR_BLOCKS, key) ? ERROR_BLOCKS[key] : null;
    var source = known ? known[1] : null;
    if (key === "media") {
      // "media: steam: fetch failed" → (Steam), and the reason loses the prefix.
      var sub = why.indexOf(":");
      var prefix = sub < 0 ? "" : why.slice(0, sub).trim().toLowerCase();
      if (has.call(PROVIDER_LABELS, prefix)) {
        source = PROVIDER_LABELS[prefix];
        why = why.slice(sub + 1).trim();
      }
    }
    if (why.length > ERROR_REASON_CAP) why = why.slice(0, ERROR_REASON_CAP - 1).trim() + "…";
    return {
      what: known ? known[0] : humanize(key || "details").toLowerCase(),
      source: source || "",
      why: why,
    };
  }
  /* An error whose data is on the card anyway says nothing true: "couldn't
     load time to beat" beside "~18h" reads as a contradiction. Drop it. */
  function errorHasData(text, pkg) {
    var key = String(text).split(":")[0].trim().toLowerCase();
    var time = pkg.time || {};
    var media = pkg.media || {};
    var ped = pkg.pedigree;
    if (key === "hltb") return num(time.hltb_main_hours) != null || num(time.hltb_extra_hours) != null;
    if (key === "igdb" || key === "studio" || key === "pedigree") {
      return !!(ped && (pedigreeHeadline(ped).length || named(ped.previous_games).length));
    }
    if (key === "media") {
      return !!(trailerEntry(media) || list(media.screenshots).some(function (s) {
        return s && (s.thumb || s.full);
      }));
    }
    return false;
  }
  function packageErrors(pkg) {
    return list(pkg.errors).filter(function (e) { return e && !errorHasData(e, pkg); });
  }
  function errorsNode(parent, errors) {
    if (!errors.length) return;
    // Deliberately quiet: a missing trailer is not an incident.
    var node = notice(parent, errors.map(errorItem));
    if (node) node.title = errors.join("; ");
  }
  function errorDetailNode(parent, errors) {
    if (!errors.length) return;
    var box = section(parent, "Couldn't load");
    var ul = el("ul", "err-list");
    // "Media (Steam) — fetch failed": the block in words, never its key,
    // then the server's own reason, capped.
    errors.forEach(function (text) {
      var item = errorItem(text);
      var li = document.createElement("li");
      li.appendChild(el("b", null, item.what.charAt(0).toUpperCase() + item.what.slice(1)
        + (item.source ? " (" + item.source + ")" : "")));
      if (item.why) li.appendChild(el("span", null, " — " + item.why));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  function named(v) { return list(v).filter(function (i) { return i && i.name; }); }
  /* ---------- breakdown sections ---------- */
  /* For you if / not for you if, as two trait columns (one when one-sided). */
  function forYouNode(parent, pres) {
    var yes = list(pres.for_you_if).filter(Boolean);
    var no = list(pres.not_for_you_if).filter(Boolean);
    if (!yes.length && !no.length) return;
    var pair = el("section", "ev-pair");
    if (yes.length) pair.appendChild(traitsNode("For you if", "good", "plus", yes));
    if (no.length) pair.appendChild(traitsNode("Not for you if", "bad", "minus", no));
    if (pair.childNodes.length === 1) pair.classList.add("ev-one");
    parent.appendChild(pair);
  }
  /* The anchors the verdict rests on: his own games, his rating as pips. */
  function anchorsNode(parent, anchors) {
    if (!anchors.length) return;
    var box = eyebrowSection(parent, "Grounded in your history");
    ministrip(box, anchors, function (a) {
      return { name: a.name, cover_url: a.cover_url, tier: ratedTier(a.rating),
        lines: miniLines({ rating: a.rating, hours: a.playtime_hours, status: a.completion_status,
          year: a.release_year, platform: a.platform }) };
    });
  }
  /* Lineage: a column per relation (Ancestors, Descendants, the call-outs,
     Similar), each comparison a mini with its authored note under it. */
  var LINEAGE_COLUMNS = [
    ["ancestor", "Ancestors"],
    ["descendant", "Descendants"],
    ["better_version", "A better version exists"],
    ["cheaper_substitute", "Cheaper substitute"],
    ["similar", "Similar"],
  ];
  function lineageNode(parent, comps) {
    if (!comps.length) return;
    var known = LINEAGE_COLUMNS.map(function (col) { return col[0]; });
    var columns = LINEAGE_COLUMNS.concat([["", "Other comparisons"]]);
    var pair = el("div", "ev-pair");
    columns.forEach(function (col) {
      var entries = comps.filter(function (c) {
        return col[0] ? c.relation === col[0] : known.indexOf(c.relation) < 0;
      });
      if (!entries.length) return;
      var column = el("div", "ev-lin-col");
      column.appendChild(el("div", "ev-lin-head", col[1]));
      entries.forEach(function (c) {
        var item = el("div", "ev-lin-item");
        item.appendChild(miniCard({ name: c.name, tier: ratedTier(c.my_rating),
          lines: miniLines({ rating: c.my_rating, hours: c.playtime_hours, owned: c.owned,
            year: c.release_year, platform: c.platform }) }));
        if (c.note) item.appendChild(el("p", "ev-note", String(c.note)));
        column.appendChild(item);
      });
      pair.appendChild(column);
    });
    if (pair.childNodes.length === 1) pair.classList.add("ev-one");
    eyebrowSection(parent, "Lineage").appendChild(pair);
  }
  /* From the studio: the headline (names, founding year, catalogue size —
     under the big-studio damper it is all that renders), what they shipped
     before as minis, and his track record with them. */
  function studioNode(parent, ped) {
    if (!ped) return;
    var head = pedigreeHead(ped);
    var items = named(ped.previous_games);
    if (!head && !items.length) return;
    var box = eyebrowSection(parent, "From the studio");
    if (head) box.appendChild(head);
    if (!items.length) return;
    ministrip(box, items, function (item) {
      return { name: item.name, cover_url: item.cover_url,
        tier: item.owned ? ratedTier(item.my_rating) : "none",
        lines: miniLines({ rating: item.owned ? item.my_rating : null, critic: item.critic_score,
          hours: item.owned ? item.playtime_hours : null,
          owned: !!item.owned, year: item.release_year, platform: item.platform }) };
    });
    var record = ped.library_track_record;
    if (record) {
      var avg = num(record.avg_my_rating);
      // The track record covers only the annotated (shown) games; when the
      // catalogue runs deeper, "last N" keeps the claim honest.
      var span = ped.previous_truncated
        ? "their last " + plural(items.length, "game")
        : "their " + plural(items.length, "previous game");
      box.appendChild(el("div", "note", "You've played " + (num(record.played_count) || 0)
        + " of " + span + (avg != null ? " — avg " + avg + "/10." : ".")));
    }
  }
  /* Past verdicts: a small ledger, newest first — the day, the verdict as a
     slim ribbon in its tier, the price seen then. */
  function pastNode(parent, past) {
    var items = list(past.items).slice();
    if (!items.length) return;
    items.sort(function (a, b) {
      return String(b.assessed_at || "").localeCompare(String(a.assessed_at || ""));
    });
    var table = el("table", "ev-past");
    var head = el("tr");
    ["Date", "Verdict", "Price"].forEach(function (name) {
      var th = el("th", null, name);
      th.setAttribute("scope", "col");
      head.appendChild(th);
    });
    var thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    var body = el("tbody");
    items.forEach(function (p) {
      var tr = el("tr");
      if (p.summary) tr.title = String(p.summary);
      tr.appendChild(el("td", null, dayMonthYear(p.assessed_at) || "earlier"));
      var verdict = el("td");
      if (p.verdict) verdict.appendChild(ribbonNode(label("verdict", p.verdict), verdictTier(p.verdict), null, "s"));
      tr.appendChild(verdict);
      var cost = el("td");
      var seen = money(p.price_seen, p.price_currency);
      if (seen) {
        var stack = el("div", "ev-stack");
        stack.appendChild(el("span", null, "seen"));
        stack.appendChild(el("span", "v", seen));
        cost.appendChild(stack);
      }
      tr.appendChild(cost);
      body.appendChild(tr);
    });
    table.appendChild(body);
    var box = eyebrowSection(parent, "Past verdicts");
    box.appendChild(table);
    var total = num(past.count);
    if (past.truncated && total != null && total > items.length) {
      box.appendChild(el("div", "note", "+" + (total - items.length) + " earlier"));
    }
  }

  /* ---------- the full breakdown (fullscreen, or a disclosure in place) ---------- */
  /* Everything that argues FOR the verdict rather than stating it. Kept out
     of the inline card on purpose: inline, the card is the verdict and its
     facts; the evidence is one click further. The library strip is never
     part of it: it is built once, after the reel, in both display modes. */
  function hasBreakdown(pkg) {
    var pres = pkg.presentation || {};
    var ped = pkg.pedigree;
    return !!(list(pres.for_you_if).filter(Boolean).length
      || list(pres.not_for_you_if).filter(Boolean).length
      || named(pkg.anchors).length
      || named(pkg.comparisons).length
      || (ped && (pedigreeHeadline(ped).length || named(ped.previous_games).length))
      || list((pkg.past || {}).items).length);
  }
  function breakdownNode(parent, pkg) {
    forYouNode(parent, pkg.presentation || {});
    anchorsNode(parent, named(pkg.anchors));
    lineageNode(parent, named(pkg.comparisons));
    studioNode(parent, pkg.pedigree);
    pastNode(parent, pkg.past || {});
    errorDetailNode(parent, packageErrors(pkg));
  }

  /* ---------- 5. the action row ---------- */
  /* At most two actions: "Full breakdown" — fullscreen where the host offers
     it, the shared disclosure in place where it doesn't (or refuses) — when
     there is breakdown content, then the secondary "Store page" (leading
     external-arrow glyph) when the package carries a Steam app id. Either
     alone renders alone; neither renders no row. */
  function storeAppid(pkg) {
    var appid = (pkg.game || {}).steam_appid;
    return typeof appid === "number" && isFinite(appid) && appid > 0 && Math.floor(appid) === appid
      ? appid : null;
  }
  function storeButton(row, appid) {
    var pill = storePill("https://store.steampowered.com/app/" + appid + "/", "Store page");
    pill.classList.add("act-store");
    row.classList.add("has-store");               // keeps the row up in fullscreen
    row.appendChild(pill);
  }
  function actionsNode(wrap, pkg, more, appid) {
    var row = el("div", "actions");
    if (!more) {
      storeButton(row, appid);
      wrap.appendChild(row);
      return null;
    }
    // The shared control, worn as the primary pill. On a fullscreen grant
    // there is nothing to open in place: syncDisplayMode builds the
    // breakdown beside the card.
    var d = fullscreenOrDisclosure(row, "Full breakdown", function (body) { breakdownNode(body, pkg); },
      function () {});
    d.button.classList.add("act-breakdown");
    d.button.classList.add("btn");
    d.button.classList.add("primary");
    if (appid) storeButton(row, appid);
    wrap.appendChild(row);
    return d.body;
  }

  /* ---------- fullscreen ---------- */
  /* Fullscreen keeps the card on the left and puts the breakdown in the right
     column, after the pitch; CSS on html[data-display-mode] swaps the action
     row's button out and the breakdown in, so a host-initiated switch — or
     the host's own close button, the only way back — needs no re-render. The
     breakdown is built on first entry only. */
  var fullscreenBreakdown = null;                   // { node, pkg, built }
  function syncDisplayMode() {
    var fs = fullscreenBreakdown;
    if (fs && !fs.built && currentDisplayMode() === "fullscreen") {
      fs.built = true;
      breakdownNode(fs.node, fs.pkg);
      reportSize();
    }
  }

  /* ---------- card assembly ---------- */
  /* Publisher, declared methodology where the response carries it, the day
     it was assessed — the cap line under the actions. */
  function provenanceNode(pkg, data) {
    var ped = pkg.pedigree || {};
    var line = el("div", "ev-prov");
    if (ped.publisher_name) line.appendChild(el("span", null, "Published by " + ped.publisher_name));
    var when = dayMonthYear(data.assessed_at);
    if (when) line.appendChild(el("span", null, "assessed " + when));
    return line.childNodes.length ? line : null;
  }
  /* Top to bottom inline: the frame, the candidate line, the ground
     (summary, weakness, pitch, abilities, flavor), the reel, the library
     strip, the action row, the provenance, the failure notice. Every part
     is built ONCE: the candidate line and the provenance live in .ev-left
     under the card — where fullscreen shows them — and inline, CSS
     dissolves .ev-top/.ev-left (display: contents) and places them with
     `order` and grid areas, so no display mode hides a second copy. */
  function evalCard(pkg, data) {
    var game = pkg.game || {};
    var wrap = el("div", "eval ev-pkg");
    var top = el("div", "ev-top");
    var left = el("div", "ev-left");
    var cardwrap = el("div", "ev-cardwrap");
    var frame = cardNode(pkg, cardNo(data.assessment_id != null ? data.assessment_id : game.game_id));
    cardwrap.appendChild(frame);
    left.appendChild(cardwrap);
    var cand = candidateNode(pkg);
    if (cand) left.appendChild(cand);
    var prov = provenanceNode(pkg, data);
    if (prov) left.appendChild(prov);
    top.appendChild(left);
    var flow = el("div", "ev-flow");
    groundNode(flow, pkg);
    top.appendChild(flow);
    wrap.appendChild(top);

    mediaSlot(wrap, pkg.media || {}, game.name);
    libraryNode(wrap, pkg.similar);
    var more = hasBreakdown(pkg);
    var appid = storeAppid(pkg);
    var inPlaceBody = more || appid ? actionsNode(wrap, pkg, more, appid) : null;
    errorsNode(wrap, packageErrors(pkg));
    if (inPlaceBody) {
      inPlaceBody.classList.add("ev-bd");
      wrap.appendChild(inPlaceBody);
    }
    if (more) {
      var fs = el("div", "fs-breakdown");
      flow.appendChild(fs);
      fullscreenBreakdown = { node: fs, pkg: pkg, built: false };
    }
    return { node: wrap, frame: frame };
  }

  /* The bookkeeping-only responses, as a small horizontal card: a recorded
     verdict without a package (its ribbon in the verdict's tier), and a void
     (a plain ribbon, no tier). */
  /* The note card's sub line — hours played and the platform lozenge — only
     from what the response carries; today's record / void responses carry
     neither, so nothing is invented and no line renders. */
  function noteSub(data) {
    var line = el("div", "sub");
    var hours = num(data.playtime_hours);
    if (hours != null && hours > 0) {
      line.appendChild(el("span", "v", hoursLabel(hours)));
      line.appendChild(el("span", null, "played"));
    }
    var platform = data.platform || list(data.platforms).filter(Boolean)[0];
    if (platform) line.appendChild(el("span", "loz", label("platform_short", platform)));
    return line.childNodes.length ? line : null;
  }
  function noteCard(opts) {
    var wrap = el("div", "eval ev-notes");
    var frame = frameNode("article", "frame-s ev-nc" + (opts.voided ? " ev-void" : ""), opts.tier);
    frame.setAttribute("aria-label", (opts.name || "Unknown game") + ": " + opts.ribbon);
    var art = el("div", "art");
    art.appendChild(coverNode({ name: opts.name }));
    frame.appendChild(art);
    var body = el("div", "ev-nc-body");
    body.appendChild(el("h2", "plate-title-s", opts.name || "Unknown game"));
    var sub = noteSub(opts.facts || {});
    if (sub) body.appendChild(sub);
    body.appendChild(ribbonNode(opts.ribbon, opts.tier, null, "s"));
    frame.appendChild(body);
    wrap.appendChild(frame);
    if (opts.caption) wrap.appendChild(el("div", "ev-nc-cap", opts.caption));
    return { node: wrap, frame: frame };
  }
  function recordedCard(data) {
    var when = dayMonthYear(data.assessed_at);
    return noteCard({
      name: data.name, tier: verdictTier(data.verdict), ribbon: label("verdict", data.verdict),
      caption: "Recorded" + (when ? " " + when : ""), facts: data,
    });
  }
  /* The void response names the verdict it deleted and the day that verdict
     was recorded — not the day of the void, which it does not carry. */
  function voidCard(data) {
    var no = cardNo(data.assessment_id);
    var when = dayMonthYear(data.assessed_at);
    return noteCard({
      name: data.name, tier: "none", ribbon: "Voided", voided: true, facts: data,
      caption: "Verdict" + (no ? " " + no : "") + (when ? " of " + when : "") + " voided",
    });
  }

  function skeletonKind() { return "eval"; }

  /* M1 + M5: the card is dealt in once per PAYLOAD — out of the skeleton on
     the first result (the ground is in place at once; only the frame moves),
     directly otherwise. A host re-delivering the same tool-result, or a
     re-render of it, redraws the card still: the deal is keyed on what the
     card IS (game, assessment, verdict, and which kind of response). */
  var dealtKey = null;
  function renderKey(data, kind) {
    var pkg = data.package || {};
    var game = pkg.game || {};
    var gameId = game.game_id != null ? game.game_id : data.game_id;
    return [kind, gameId, data.assessment_id, pkg.verdict || data.verdict].join("|");
  }
  function place(built, key) {
    var fresh = key !== dealtKey;
    dealtKey = key;
    var first = root.firstElementChild;
    var skel = first && !first.nextElementSibling && first.classList.contains("skel") ? first : null;
    if (skel) {
      resolveSkeleton(skel, function () { return built.node; });
      built.node.classList.remove("deal");
    } else {
      root.textContent = "";
      root.appendChild(built.node);
    }
    if (fresh) dealIn(built.frame, 0);
  }

  function render(data) {
    fullscreenBreakdown = null;
    if (data && data.package) {
      place(evalCard(data.package, data), renderKey(data, "package"));
      syncDisplayMode();
    } else if (data && data.voided) {
      place(voidCard(data), renderKey(data, "voided"));
    } else if (data && data.verdict) {
      place(recordedCard(data), renderKey(data, "recorded"));
    } else {
      dealtKey = null;
      root.textContent = "";
      root.appendChild(el("div", "empty", "Nothing to display."));
    }
    reportSize();
  }

"""
    + apps_shared.SIZING_JS
    + apps_shared.INIT_JS
    + r"""
  /* Fullscreen hands the frame's size to the host, so size-changed stays
     quiet there; leaving it re-announces the inline height even when it
     matches the last one sent. */
  hooks.shouldReportSize = function () { return currentDisplayMode() !== "fullscreen"; };
  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (currentDisplayMode() !== "fullscreen") lastSize = "";
      syncDisplayMode();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-display-mode"] });
  }

  startWidget("gamelib-eval-card");
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

    # prefersBorder=False: the card paints its own panels on a transparent
    # page, so a host-drawn frame around it would double the border.
    @mcp.resource(
        EVAL_CARD_URI,
        name="eval_card_view",
        description="Evaluation-package card UI for assessment results (MCP Apps).",
        app=AppConfig(csp=_EVAL_CARD_CSP, prefers_border=False),
    )
    def eval_card_view() -> str:
        return EVAL_CARD_HTML
