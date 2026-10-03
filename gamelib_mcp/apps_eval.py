"""MCP Apps (io.modelcontextprotocol/ui): the evaluation-card widget.

One `ui://` resource renders `record_assessment` results: the full
evaluation package when the response carries one, and a compact note card for
the bookkeeping-only responses (a plain recorded verdict, or a void).

The package card is tiered (spec 2026-10-03 §2.2). INLINE, always rendered
and budgeted at ≤900px tall at 760px wide: one header panel (cover, title, sub
line, verdict stamp, the score chips, the authored craft note, then "the
call" — HLTB, pace, price seen, target, paid and the flags — directly under
the scores), the verdict in words (summary, elevator pitch, why-care eyebrow
lines), the media panel (one viewer plus one thumb strip, trailer first,
screenshots opening an edge-to-edge carousel), one action row ("Full
breakdown", plus "Store page ↗" when the package carries a Steam app id)
and a single notice naming what failed to load. The FULL
BREAKDOWN — for-you-if / not-for-you-if, the anchors it rests on, lineage,
the owned games most like it, the "from the studio" pedigree strip, past
verdicts and the failure detail — opens fullscreen where the host offers it
(the inline card stays on top, the host's close button is the way back) and
as an in-place disclosure where it doesn't.
Clients that don't speak the Apps extension ignore the tool metadata and see
the normal JSON, so attaching ``EVAL_CARD_APP`` to a tool is purely additive.

Every section of the package is optional: an unowned candidate with no appid
and no IGDB match gets a media-less card (and an untagged one no similar
row), and each block is skipped rather than rendered empty.

Visual language: the host's theme, through the shared ``--gl-*`` token layer
(see apps_shared.py and apps.py) on a transparent page. Every score is the
shared chip — color is the quality tier, the source is the label text. The
one deliberate piece of brand left is the verdict stamp: 2px strong border, a
hard 3px shadow, a −3° tilt, filled with its tier (buy_now good,
wishlist_for_sale ok, skip bad, try_demo / play_what_you_own plain), its text
from ``label("verdict", …)``.

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
    + r"""  .eval {
    max-width: 760px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  /* ---- shared cover block (same shape as the game-cards widget) ---- */
"""
    + apps_shared.COVER_CSS
    + r"""  .cover-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: var(--gl-cap-lh);
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
  /* ---- header: cover | title + sub | stamp, the score row under the title ---- */
  /* Grid areas, so the stamp keeps the top-right corner on a wide card and
     drops under the title on a phone (below 420px) without ever covering it,
     and the score row fills the space beside the cover instead of starting
     under it. */
  .head {
    display: grid;
    grid-template-columns: 84px minmax(0, 1fr) auto;
    grid-template-areas: "cover info stamp" "cover scores scores" "cover note note";
    grid-template-rows: auto auto 1fr;
    column-gap: 14px;
    row-gap: 10px;
    align-items: start;
  }
  .head .cover-wrap {
    grid-area: cover;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-sm);
    overflow: hidden;
  }
  .head-info { grid-area: info; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .head .stamp { grid-area: stamp; }
  .head-info h1 {
    font-size: var(--gl-h);
    line-height: var(--gl-h-lh);
    font-weight: var(--gl-strong);
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }
  .sub {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-text-2);
    font-variant-numeric: tabular-nums;
  }
  /* The score chips live INSIDE the header: two lonely chips in a panel of
     their own ("CRAFT & FIT") was the first thing the owner called out. */
  .head-chips { grid-area: scores; }
  /* One model-authored line of craft context under the chips — the spread, the
     recurring knock, the review-bomb caveat a number can't carry. */
  .craft-note { grid-area: note; color: var(--gl-text-2); }
  /* The call: hours and money, directly under the scores. */
  .call {
    margin-top: 12px;
    padding-top: 12px;
    border-top: var(--gl-bw) solid var(--gl-border);
  }
  .call-label {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--gl-muted);
    margin-right: 2px;
  }
  .one-liner { font-weight: var(--gl-strong); }
  .pitch {
    margin-top: 8px;
    color: var(--gl-text-2);
    border-left: 2px solid var(--gl-border-strong);
    padding: 2px 0 2px 12px;
  }
  .pitch:first-child { margin-top: 0; }
  /* The verdict stamp — the card's one deliberate piece of brand: strong 2px
     border, hard 3px shadow, a -3deg tilt, filled with the verdict's tier. */
  .stamp {
    flex: 0 0 auto;
    align-self: flex-start;
    margin: 4px 4px 0 0;
    font-size: var(--gl-body);
    font-weight: var(--gl-strong);
    letter-spacing: 0.04em;
    line-height: 1.15;
    text-transform: uppercase;
    text-align: center;
    max-width: 168px;
    padding: 8px 12px;
    border: 2px solid var(--gl-border-strong);
    border-radius: var(--gl-r-md);
    box-shadow: 3px 3px 0 var(--gl-border-strong);
    transform: rotate(-3deg);
  }
  .stamp-good { background: var(--gl-good); color: var(--gl-surface); }
  .stamp-ok { background: var(--gl-ok); color: var(--gl-surface); }
  .stamp-bad { background: var(--gl-bad); color: var(--gl-surface); }
  .stamp-plain { background: var(--gl-surface); color: var(--gl-text); }
"""
    + apps_shared.STRIP_CSS
    + r"""
  /* ---- for you / not for you ---- */
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .col-title { font-weight: var(--gl-strong); margin-bottom: 6px; }
  .col.yes .col-title, .col.yes .tick { color: var(--gl-good); }
  .col.no .col-title, .col.no .tick { color: var(--gl-bad); }
  .bullets { list-style: none; display: flex; flex-direction: column; gap: 6px; }
  .bullets li { display: flex; gap: 8px; }
  .tick { font-weight: var(--gl-strong); flex: none; }

  /* ---- anchors ---- */
  /* Deliberately NEUTRAL cards: an anchor is evidence, and the live card lit
     up "Cyberpunk 2077 6.6h" — a game he bounced off — in endorsement green.
     The card is plain; its chips are the shared ones ("You 6/10", "Played
     6.6h", "Status Abandoned"), each colored by what it says. */
  .anchors { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
  .anchor {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 8px 6px 6px;
    border-radius: var(--gl-r-sm);
    border: var(--gl-bw) solid var(--gl-border);
    background: var(--gl-inset);
    font-variant-numeric: tabular-nums;
    min-width: 0;
  }
  .anchor.no-cover { padding-left: 8px; }
  .anchor-cover {
    width: 32px;
    height: 48px;
    border-radius: var(--gl-r-xs);
    object-fit: cover;
    flex: none;
  }
  .anchor-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .anchor-name { font-weight: var(--gl-strong); overflow-wrap: anywhere; }
  .anchor .chip.tier-none { background: var(--gl-surface); }

  /* ---- lineage / comparisons ---- */
  .callout {
    display: flex;
    flex-direction: column;
    gap: 4px;
    border: var(--gl-bw) solid var(--gl-border-strong);
    border-radius: var(--gl-r-md);
    background: var(--gl-inset);
    padding: 10px 12px;
    margin-bottom: 10px;
  }
  .callout-head { font-weight: var(--gl-strong); }
  .callout-note { color: var(--gl-text-2); }
  .lineage { display: flex; gap: 10px; align-items: stretch; flex-wrap: wrap; }
  .lin-col { flex: 1 1 150px; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .lin-head {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--gl-muted);
  }
  .lin-arrow { align-self: center; font-size: var(--gl-h); color: var(--gl-muted); }
  .comp {
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    background: var(--gl-surface);
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .comp-name { font-weight: var(--gl-strong); line-height: var(--gl-h-lh); overflow-wrap: anywhere; }
  .comp-note { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); color: var(--gl-text-2); }
  .comp.this-game { background: var(--gl-inset); border-color: var(--gl-border-strong); }
"""
    + apps_shared.TAG_CSS
    + apps_shared.PEDIGREE_CSS
    + r"""
  /* ---- why care (model-authored eyebrow lines) ---- */
  .why-care { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
  .wc-line { display: flex; gap: 10px; align-items: baseline; }
  .wc-eyebrow {
    flex: 0 0 64px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--gl-muted);
  }

  /* ---- similar in your library ---- */
"""
    + apps_shared.SIMILAR_CSS
    + r"""
  /* ---- past verdicts ---- */
  .timeline { display: flex; gap: 6px; flex-wrap: wrap; }
  .tl-chip {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    padding: 3px 8px;
    border-radius: var(--gl-r-sm);
    border: var(--gl-bw) dashed var(--gl-border-strong);
    color: var(--gl-text-2);
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .err-list { list-style: none; display: flex; flex-direction: column; gap: 4px; color: var(--gl-text-2); }
  .err-list b { font-weight: var(--gl-strong); color: var(--gl-text); }

  /* ---- action row, in-place breakdown, fullscreen breakdown ---- */
  .actions { display: flex; gap: 8px; }
  /* The secondary store link sizes to its label; the breakdown takes the rest. */
  .actions .act-store { flex: none; }
  .eval > .disclosure-body { margin-top: 0; }
  /* The stage is self-evident inline; its eyebrow stays for screen readers. */
  .media-slot .section-title {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .media-slot > .panel { position: relative; }
  .fs-breakdown { display: none; flex-direction: column; gap: 12px; }
  html[data-display-mode="fullscreen"] .fs-breakdown { display: flex; }
  /* Fullscreen IS the breakdown, so its button goes — but the store link
     stays: fullscreen is exactly where he has decided to read everything,
     and the next step from there is the store page. */
  html[data-display-mode="fullscreen"] .actions:not(.has-store),
  html[data-display-mode="fullscreen"] .actions .act-breakdown,
  html[data-display-mode="fullscreen"] .eval > .disclosure-body { display: none; }
  html[data-display-mode="fullscreen"] .actions { justify-content: flex-end; }

  .note-card { display: flex; gap: 14px; align-items: center; max-width: 560px; margin: 0 auto; }
  .note-text { font-weight: var(--gl-strong); overflow-wrap: anywhere; }
  .note-card .stamp { font-size: var(--gl-cap); max-width: 130px; padding: 6px 10px; margin: 0; }

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
  /* Wide card: the thumbs stand beside the stage as a 3-column grid (the
     Steam store layout) instead of under it — the strip under a full-width
     16:9 stage cost ~160px of the inline budget, and the trailer plus eight
     screenshots fill the 3x3 grid exactly. contain: size keeps the column
     from growing the row, so the stage sets the height and any overflow
     scrolls inside it. */
  @media (min-width: 600px) {
    .media-slot.has-thumbs > .panel {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 296px;
      column-gap: 10px;
    }
    .media-slot.has-thumbs .section-title { grid-column: 1 / -1; }
    .media-slot.has-thumbs .thumbs {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      align-content: start;
      gap: 6px;
      margin-top: 0;
      contain: size;
      overflow-x: hidden;
      overflow-y: auto;
    }
    .media-slot.has-thumbs .thumb img, .media-slot.has-thumbs .thumb-text {
      width: 100%;
      height: auto;
      aspect-ratio: 16 / 9;
    }
    /* The grid scrolls vertically: no sideways snap, and no trailing spacer
       (it would wrap into a stray grid cell). */
    .media-slot.has-thumbs .thumbs { scroll-snap-type: none; }
    .media-slot.has-thumbs .thumbs::after { content: none; }
  }
  @media (max-width: 419px) {
    .actions { flex-direction: column; }
    .actions .act-store { width: 100%; }
    /* "THE CALL" becomes a label above its chips instead of an eyebrow
       sharing a first line with one lonely chip. */
    .call .call-label { flex-basis: 100%; margin-right: 0; }
    .head {
      grid-template-columns: 84px minmax(0, 1fr);
      grid-template-areas: "cover info" "cover stamp" "scores scores" "note note";
      grid-template-rows: auto 1fr auto auto;
    }
    .head .stamp { justify-self: start; }
  }
  @media (max-width: 480px) {
    .two-col { grid-template-columns: 1fr; }
    .lin-arrow { display: none; }
    /* Narrow phone: smaller thumbs so several fit before scrolling. */
    .thumb img, .thumb-text { width: 96px; height: 55px; }
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
    + apps_shared.COVER_HUE_JS
    + r"""  /* Same cover block as the game-cards widget: real art when we have it, a
     name-seeded gradient plate when we don't. */
"""
    + apps_shared.COVER_NODE_JS
    + r"""  function section(parent, title) {
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
  /* ---------- 2. header: identity, scores, the call ---------- */
  /* The stamp's text is label("verdict", …); this map only picks its fill. */
  var VERDICT_STAMPS = {
    buy_now: "stamp-good",
    wishlist_for_sale: "stamp-ok",
    try_demo: "stamp-plain",
    play_what_you_own: "stamp-plain",
    skip: "stamp-bad",
  };
  function stampNode(verdict) {
    if (!verdict) return null;
    var text = label("verdict", verdict);
    var stamp = el("div", "stamp " + (VERDICT_STAMPS[verdict] || "stamp-plain"), text);
    stamp.setAttribute("role", "img");
    stamp.setAttribute("aria-label", "Verdict: " + text);
    return stamp;
  }
  /* Everything the verdict rests on sits in this one panel, in the order it
     is read: what the game is (cover, title, stamp), how good it is (scores,
     craft note), and what it costs in hours and money (the call — which used
     to close the card 2,400px down and is now the third thing on it). */
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
    var platforms = list(own.platforms).filter(Boolean)
      .map(function (p) { return label("platform", p); });
    if (platforms.length) bits.push(platforms.join(", "));
    // Positive hours only: hoursLabel(0) is the truthy string "0h", and
    // "0h played" would contradict the card's own unplayed badges (zero is
    // authoritative NOT-played; null is unknown and says nothing).
    var played = own.playtime_hours > 0 ? hoursLabel(own.playtime_hours) : null;
    if (played) bits.push(played + " played");
    if (own.wishlisted && !own.owned) bits.push("wishlisted");
    if (bits.length) info.appendChild(el("div", "sub", bits.join(" · ")));
    head.appendChild(info);

    var stamp = stampNode(pkg.verdict);
    if (stamp) head.appendChild(stamp);

    // The score row and the craft note are grid areas of .head: beside the
    // cover on a wide card, full width under it on a phone.
    var chips = scoreChips(pkg);
    if (chips) head.appendChild(chips);
    if (pres.craft_note) head.appendChild(el("div", "craft-note", pres.craft_note));
    box.appendChild(head);

    var call = callNode(pkg);
    if (call) box.appendChild(call);
    return box;
  }

  /* ---------- the score chips (a grid area of the header) ---------- */
  /* Every one is the shared scoreChip: tier color, source as label text. */
  var TRAJECTORIES = {
    improving: ["↗ Improving", "good"],
    stable: ["→ Stable", "none"],
    regressing: ["↘ Regressing", "bad"],
  };
  var FIT_CLASSES = {
    "strong fit": "good",
    "probable fit": "good",
    "coin flip": "ok",
    "probable miss": "bad",
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
      var rawPct = num(craft.positive_pct);
      var count = compactCount(craft.review_count);
      row.appendChild(scoreChip({
        label: "Reviews", value: pct + "% positive", tier: craftTier(pct), meter: pct,
        aux: count,
        title: "Sample-adjusted share of positive reviews"
          + (rawPct != null ? " (raw " + Math.round(rawPct <= 1 ? rawPct * 100 : rawPct) + "% positive)" : "")
          + (count ? ", from " + compactCount(craft.review_count, "review") : ""),
      }));
    }

    var traj = TRAJECTORIES[craft.trajectory];
    if (traj) {
      row.appendChild(scoreChip({
        label: "Trend", value: traj[0], tier: traj[1], title: "Recent review trajectory",
      }));
    }

    // Providers use negative sentinels for "no score yet" — never show those.
    var mc = num(craft.metacritic_score);
    if (realScore(mc)) {
      row.appendChild(scoreChip({ label: "Metacritic", value: Math.round(mc), tier: mcTier(mc) }));
    }

    var oc = num(craft.opencritic_score);
    if (realScore(oc)) {
      row.appendChild(scoreChip({ label: "OpenCritic", value: Math.round(oc), tier: ocTier(oc) }));
    }

    if (pkg.fit_call) {
      // "strong fit" → Fit "Strong": the label already says fit.
      var fit = String(pkg.fit_call).replace(/\s+fit$/i, "");
      row.appendChild(scoreChip({
        label: "Fit", value: fit.charAt(0).toUpperCase() + fit.slice(1),
        tier: FIT_CLASSES[pkg.fit_call] || "none", title: "How well it fits your taste",
      }));
    }

    return row.childNodes.length ? row : null;
  }

  /* ---------- 3. the call: time, price, flags ---------- */
  /* Facts are tier-less chips and the flags are danger chips in the same
     row; the eyebrow rides inline, so the block costs no line of its own. */
  function factChip(row, name, value, aux, title) {
    row.appendChild(scoreChip({ label: name, value: value, aux: aux, title: title }));
  }
  function factChips(pkg, row) {
    var time = pkg.time || {};
    var price = pkg.price || {};
    var own = pkg.ownership || {};

    var main = hoursLabel(time.hltb_main_hours, true);
    var extra = hoursLabel(time.hltb_extra_hours);
    var hltbTitle = "HowLongToBeat: main story, and main + extras (full)";
    if (main) factChip(row, "Time to beat", main, extra ? extra + " full" : null, hltbTitle);
    else if (extra) factChip(row, "Time to beat", "~" + extra, "full", hltbTitle);

    var weekly = num(time.recent_weekly_minutes);
    if (weekly != null && weekly > 0) {
      factChip(row, "Your pace", hoursLabel(weekly / 60, true) + "/wk", "last 30 days",
        "Your average weekly playtime over the last 30 days");
    }

    var seen = money(price.seen, price.currency);
    if (seen) {
      factChip(row, "Price", "seen at " + seen
        + (price.platform ? " on " + label("platform", price.platform) : ""));
    }
    var target = money(price.target, price.currency);
    if (target) factChip(row, "Target", target);

    var paid = money(own.price_paid, own.price_currency);
    if (paid) {
      var how = own.bundle_name ? " in " + own.bundle_name
        : own.purchase_source ? " via " + label("purchase_source", own.purchase_source) : "";
      factChip(row, "Owned", "paid " + paid + how);
    }
  }
  function callNode(pkg) {
    var row = el("div", "chips");
    factChips(pkg, row);
    list(pkg.flags).filter(Boolean).forEach(function (f) {
      row.appendChild(scoreChip({ label: String(f), tier: "bad", cls: "flag" }));
    });
    if (!row.childNodes.length) return null;
    row.insertBefore(el("span", "call-label", "The call"), row.firstChild);
    var call = el("div", "call");
    call.appendChild(row);
    return call;
  }

  /* ---------- 4. the verdict in words ---------- */
  /* The one-liner, the authored pitch, and the why-care eyebrow — one panel,
     because they are one argument. */
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
     all" — the editorial counterpart to the server-fetched pedigree strip in
     the breakdown. An eyebrow per kind, one line each, no paragraphs; absent
     when the recording client authored none. */
  var WHY_CARE_KINDS = {
    people: ["People", "wc-people"],
    studio: ["Studio", "wc-studio"],
    anticipation: ["Hype", "wc-hype"],
    moment: ["Moment", "wc-moment"],
  };
  function whyCareNode(parent, pres) {
    var entries = list(pres.why_care).filter(function (e) { return e && e.text; });
    if (!entries.length) return;
    var wrap = el("div", "why-care");
    entries.slice(0, 3).forEach(function (entry) {
      var known = WHY_CARE_KINDS[entry.kind];
      var line = el("div", "wc-line");
      line.appendChild(el("span", "wc-eyebrow " + (known ? known[1] : "wc-studio"),
        known ? known[0] : humanize(entry.kind || "why")));
      line.appendChild(el("span", null, String(entry.text)));
      wrap.appendChild(line);
    });
    parent.appendChild(wrap);
  }

  /* ---------- 5. media (one viewer + one thumb strip) ---------- */
  /* The shared mediaNode builds the panel; the slot lets the card lay it out
     (thumbs beside the stage on a wide card) without touching shared code. */
  function mediaSlot(parent, media, gameName) {
    var slot = el("div", "media-slot");
    mediaNode(slot, media, gameName);
    if (!slot.childNodes.length) return;
    if (slot.querySelector(".thumbs")) slot.classList.add("has-thumbs");
    parent.appendChild(slot);
  }

  /* ---------- breakdown: for you / not for you ---------- */
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

  /* ---------- breakdown: anchors ---------- */
  /* The card is neutral — an anchor is evidence, and half of them are
     negative ("Cyberpunk 2077, 6.6h"). Its chips are the shared library
     chips: his rating, his hours, and the completion status (Completed /
     Evergreen good, Abandoned bad, Playing plain). */
  function anchorsNode(parent, anchors) {
    if (!anchors.length) return;
    var box = section(parent, "Grounded in your history");
    var grid = el("div", "anchors");
    anchors.forEach(function (a) {
      var card = el("div", "anchor" + (a.cover_url ? "" : " no-cover"));
      if (a.cover_url) {
        var img = document.createElement("img");
        img.className = "anchor-cover";
        img.alt = "";
        img.loading = "lazy";
        img.onerror = function () { img.remove(); card.classList.add("no-cover"); };
        img.src = a.cover_url;
        card.appendChild(img);
      }
      var body = el("div", "anchor-body");
      body.appendChild(el("div", "anchor-name", a.name || "?"));
      var hours = num(a.playtime_hours);
      var chips = chipRow([youChip(a.rating), playedChip(hours, hours === 0),
        statusChip(a.completion_status)]);
      if (chips) body.appendChild(chips);
      card.appendChild(body);
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }

  /* ---------- breakdown: lineage / comparisons ---------- */
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
     similar-notes into the strip below mixed two different things (his model's
     reading vs. his library's tag neighbours) and read as one confused list. */
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
      // Labelled, because these note-cards sit above the library's own
      // similar strip and the two must not read as one list.
      var onlySimilar = loose.every(function (c) { return c.relation === "similar"; });
      var head = el("div", "lin-head", onlySimilar ? "Also similar" : "Other comparisons");
      head.style.marginTop = (callouts.length || ancestors.length || descendants.length)
        ? "12px" : "0";
      box.appendChild(head);
      var rest = el("div", "chips");
      rest.style.marginTop = "6px";
      rest.style.alignItems = "stretch";
      loose.forEach(function (c) { rest.appendChild(comparisonNode(c)); });
      box.appendChild(rest);
    }
  }

  /* ---------- breakdown: similar in your library (tag similarity) ---------- */
"""
    + apps_shared.SIMILAR_NODE_JS
    + r"""
  /* ---------- breakdown: from the studio (pedigree) ---------- */
  /* Server-fetched and library-annotated (tools/game_media.py): who made this,
     and what they shipped BEFORE it. Under the big-studio damper, or with
     nothing released earlier, only the header line renders — six arbitrary
     posters out of a 500-game catalogue say nothing about this game. Spliced
     from apps_shared.py, verbatim in the detail card too. */
"""
    + apps_shared.PEDIGREE_JS
    + r"""
  /* ---------- breakdown: past verdicts ---------- */
  function pastRow(past) {
    var items = list(past.items).slice();
    if (!items.length) return null;
    items.sort(function (a, b) {                    // newest first
      return String(b.assessed_at || "").localeCompare(String(a.assessed_at || ""));
    });
    var line = el("div", "timeline");
    items.forEach(function (p) {
      var parts = [];
      if (p.assessed_at) parts.push(String(p.assessed_at).slice(0, 10));
      if (p.verdict) parts.push(label("verdict", p.verdict));
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
  function pastNode(parent, past) {
    var line = pastRow(past);
    if (line) section(parent, "Past verdicts").appendChild(line);
  }

  /* ---------- 7. what failed: one notice inline, the detail in the breakdown ---------- */
  /* package.errors are "<block>: <reason>" strings ("media: steam: …",
     "igdb: unresolved — …"). The notice names WHAT is missing and from WHERE;
     the raw reasons stay in its tooltip and in the breakdown's last section. */
  var ERROR_WHAT = {
    media: "media", pace: "your pace", similar: "similar games",
    igdb: "studio", steam: "store data", package: "evaluation details", hltb: "time to beat",
  };
  function errorItem(text) {
    var parts = String(text).split(":");
    var key = parts[0].trim().toLowerCase();
    var next = parts.length > 2 ? parts[1].trim().toLowerCase() : "";
    var has = Object.prototype.hasOwnProperty;
    var source = has.call(PROVIDER_LABELS, key) ? PROVIDER_LABELS[key]
      : has.call(PROVIDER_LABELS, next) ? PROVIDER_LABELS[next] : "";
    return { what: ERROR_WHAT[key] || humanize(key).toLowerCase(), source: source };
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
      return !!(ped && (pedigreeHeadline(ped) || named(ped.previous_games).length));
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
    errors.forEach(function (text) {
      var item = errorItem(text);
      var li = document.createElement("li");
      li.appendChild(el("b", null, item.what.charAt(0).toUpperCase() + item.what.slice(1)
        + (item.source ? " (" + item.source + ")" : "")));
      var why = String(text).split(":").slice(1).join(":").trim();
      if (why) li.appendChild(el("span", null, " — " + why));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  /* ---------- the full breakdown (fullscreen, or a disclosure in place) ---------- */
  /* Everything that argues FOR the verdict rather than stating it. Kept out
     of the inline card on purpose: inline, the card is the verdict and its
     facts (≤900px at 760); the evidence is one click further. */
  function named(v) { return list(v).filter(function (i) { return i && i.name; }); }
  function hasBreakdown(pkg) {
    var pres = pkg.presentation || {};
    var ped = pkg.pedigree;
    return !!(list(pres.for_you_if).filter(Boolean).length
      || list(pres.not_for_you_if).filter(Boolean).length
      || named(pkg.anchors).length
      || named(pkg.comparisons).length
      || named((pkg.similar || {}).items).length
      || (ped && (pedigreeHeadline(ped) || named(ped.previous_games).length))
      || list((pkg.past || {}).items).length);
  }
  function breakdownNode(parent, pkg) {
    forYouNode(parent, pkg.presentation || {});
    anchorsNode(parent, named(pkg.anchors));
    lineageNode(parent, pkg, named(pkg.comparisons));
    similarNode(parent, pkg.similar || {});
    pedigreeNode(parent, pkg.pedigree);
    pastNode(parent, pkg.past || {});
    errorDetailNode(parent, packageErrors(pkg));
  }

  /* ---------- 6. the action row ---------- */
  /* At most two actions: "Full breakdown" — fullscreen where the host offers
     it, the shared disclosure in place where it doesn't (or refuses) — when
     there is breakdown content, then the secondary "Store page ↗" when the
     package carries a Steam app id. Either alone renders alone; neither
     renders no row. */
  function storeAppid(pkg) {
    var appid = (pkg.game || {}).steam_appid;
    return typeof appid === "number" && isFinite(appid) && appid > 0 && Math.floor(appid) === appid
      ? appid : null;
  }
  function storeButton(row, appid) {
    var btn = el("button", "btn act-store", "Store page ↗");
    btn.type = "button";
    row.classList.add("has-store");               // keeps the row up in fullscreen
    btn.addEventListener("click", function () {
      openLink("https://store.steampowered.com/app/" + appid + "/");
    });
    row.appendChild(btn);
  }
  function actionsNode(wrap, pkg, more, appid) {
    var row = el("div", "actions");
    if (!more) {
      storeButton(row, appid);
      wrap.appendChild(row);
      return null;
    }
    var d = disclosure(row, "Full breakdown", function (body) { breakdownNode(body, pkg); });
    d.button.classList.add("act-breakdown");
    if (appid) storeButton(row, appid);
    var inPlace = false;                            // the host refused once: stay in place
    var asking = false;
    // Capture on the row, so the disclosure's own click handler only runs
    // when fullscreen is off the table.
    row.addEventListener("click", function (ev) {
      if (!d.button.contains(ev.target)) return;
      if (inPlace || !canFullscreen() || d.button.getAttribute("aria-expanded") === "true") return;
      ev.stopPropagation();
      if (asking) return;
      asking = true;
      requestDisplayMode("fullscreen").then(function (mode) {
        asking = false;
        if (mode === "fullscreen") return;          // syncDisplayMode builds the breakdown
        inPlace = true;
        d.button.click();
      });
    }, true);
    wrap.appendChild(row);
    return d.body;
  }

  /* ---------- fullscreen ---------- */
  /* Fullscreen keeps the inline card on top and puts the breakdown under it;
     CSS on html[data-display-mode] swaps the action row out and the
     breakdown in, so a host-initiated switch — or the host's own close
     button, the only way back — needs no re-render. The breakdown is built
     on first entry only. */
  var fullscreenBreakdown = null;                   // { node, pkg, built }
  function syncDisplayMode() {
    var fs = fullscreenBreakdown;
    if (fs && !fs.built && currentDisplayMode() === "fullscreen") {
      fs.built = true;
      breakdownNode(fs.node, fs.pkg);
    }
  }

  /* ---------- card assembly ---------- */
  /* The inline tier, top to bottom: the header panel (identity, stamp,
     scores, craft note, the call), the verdict in words, the media. */
  function inlineCard(wrap, pkg) {
    wrap.appendChild(headerNode(pkg));
    pitchNode(wrap, pkg);
    mediaSlot(wrap, pkg.media || {}, (pkg.game || {}).name);
  }
  function evalCard(pkg) {
    var wrap = el("div", "eval");
    inlineCard(wrap, pkg);
    var more = hasBreakdown(pkg);
    var appid = storeAppid(pkg);
    var inPlaceBody = more || appid ? actionsNode(wrap, pkg, more, appid) : null;
    errorsNode(wrap, packageErrors(pkg));
    if (inPlaceBody) wrap.appendChild(inPlaceBody);
    if (more) {
      var fs = el("div", "fs-breakdown");
      wrap.appendChild(fs);
      fullscreenBreakdown = { node: fs, pkg: pkg, built: false };
    }
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

  function skeletonKind() { return "eval"; }

  function render(data) {
    root.textContent = "";
    fullscreenBreakdown = null;
    var name = data && data.name ? " · " + data.name : "";
    if (data && data.package) {
      root.appendChild(evalCard(data.package));
      syncDisplayMode();
    } else if (data && data.verdict) {
      root.appendChild(noteCard("Recorded — " + label("verdict", data.verdict) + name, data.verdict));
    } else {
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
     matches the last one sent. The shared observer captured the inline
     reporter by reference, so it is re-armed on the wrapper. */
  var reportInlineSize = reportSize;
  reportSize = function () {
    if (currentDisplayMode() !== "fullscreen") reportInlineSize();
  };
  if (resizeObserver) {
    resizeObserver.disconnect();
    resizeObserver = new ResizeObserver(function () { reportSize(); });
    resizeObserver.observe(document.body);
  }
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
