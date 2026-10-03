"""MCP Apps (io.modelcontextprotocol/ui): the game-cards widget.

One `ui://` resource serves both card layouts: a cover grid when the tool
result carries a ``results`` array (discover_games) and a single detail card
otherwise (get_game_detail). Clients that don't speak the Apps extension
ignore the tool metadata entirely and see the normal JSON responses, so
attaching ``GAME_CARDS_APP`` to a tool is purely additive.

Visual language: the host's own theme. Every color, radius, border and font
is a ``--gl-*`` token (apps_shared.TOKENS_CSS) resolving to the host's
``hostContext.styles.variables`` with light/dark fallbacks, on a transparent
page — 0.5px borders, small shadows, 8px radii, three type sizes. The only
art of our own is the name-seeded gradient plate standing in for missing
cover art. Grids whose payload carries an ``offset`` (discover_games —
genuinely rank-ordered) get "#1"-style rank badges numbered globally across
pagination; payloads without that signal (e.g. the detail card) never show a
rank.

Every score renders through the one shared chip (apps_shared.SCORE_CHIP_JS):
color is the quality tier only (good / ok / bad / none, from the host's
success / warning / danger tokens), and the source is the label text —
Metacritic, OpenCritic, Steam (phrase plus a 9-step meter, never the meter
alone). Platform ids render through ``label("platform", …)``.

Grid cards are interactive: clicking (or Enter/Space — cards are keyboard
buttons) opens an overlay that renders instantly from the card's own data,
then upgrades in place when a live ``get_game_detail`` result arrives via an
app-initiated ``tools/call`` through the bridge. If the host denies or does
not support app tool calls, the overlay simply keeps the lite view.

A detail card whose payload carries ``media`` (get_game_detail(media=True) —
which is what that upgrade call asks for) also renders the neutral game
representation: a media panel leading the stack (one 16:9 viewer plus one
thumb strip, trailer first, screenshots opening an edge-to-edge carousel
lightbox), a "similar in your library" row (the owned games sharing this
one's tags), and a "From the studio" strip (the developer, and their previous
games against his library — header line alone for a studio too big for six
posters to describe). Those blocks live in apps_shared.py and are spliced
verbatim into both this widget and the evaluation card (apps_eval.py).

The HTML is deliberately dependency-free: the host↔iframe bridge is the
hand-rolled JSON-RPC postMessage handshake from the MCP Apps spec
(ui/initialize → ui/notifications/initialized → tool-input / tool-result,
host-context-changed, plus app-initiated tools/call) rather than
@modelcontextprotocol/ext-apps, so nothing is fetched from a CDN and the CSP
only has to allow the media hosts and the host's font files.

For local visual iteration outside any MCP host, the widget renders
``window.__PREVIEW_DATA__`` when present instead of waiting on the bridge, and
applies ``window.__PREVIEW_HOST_CONTEXT__`` as if the host had sent it — see
scripts/preview_game_cards.py.
"""

import hashlib
from typing import Any

from fastmcp.apps import AppConfig, ResourceCSP

from . import apps_shared

# Cover art (see tools/common.py cover_url) plus the media hosts a detail card
# needs once get_game_detail(media=True) answers with a trailer and
# screenshots. Per the MCP Apps spec, resource_domains feeds img-src, media-src,
# script-src, style-src and font-src in the host's iframe CSP, while
# frame_domains feeds frame-src — which is what the lazy youtube-nocookie
# embed rides on. Covers: IGDB art, Steam capsules and the constructed mp4
# renditions (cdn.*), Steam screenshots and movie posters (shared.*, where
# appdetails actually serves them), YouTube thumbnails for IGDB trailers, and
# assets.claude.ai for the host fonts hostContext.styles.css.fonts @font-faces.
# Same set as the evaluation card (apps_eval.py); everything else stays
# deny-by-default.
_GAME_CARDS_CSP = ResourceCSP(
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

GAME_CARDS_HTML = (
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
    + r"""
  /* ---- shared cover block ---- */
"""
    + apps_shared.COVER_CSS
    + r"""  /* The gradient plate standing in for missing art (one of the two pieces
     of art of our own; the colors are generated per name in coverNode). */
  .cover-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--gl-h);
    font-weight: var(--gl-strong);
    line-height: var(--gl-h-lh);
    color: rgba(255, 255, 255, 0.92);
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
    padding: 12px;
    text-align: center;
    overflow: hidden;
    overflow-wrap: anywhere;
  }
  .sim .cover-fallback { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); padding: 8px; }
  /* The Metacritic score in the cover's corner: the shared chip, tier-colored. */
  .chip.corner { position: absolute; top: 6px; right: 6px; z-index: 1; }
  .pills { display: flex; gap: 4px; flex-wrap: wrap; }
  .pill {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    padding: 0 7px;
    border-radius: var(--gl-r-full);
    border: var(--gl-bw) solid var(--gl-border);
    background: var(--gl-inset);
    color: var(--gl-text-2);
    white-space: nowrap;
  }
  .pill.match {
    background: var(--gl-inverse-bg);
    color: var(--gl-inverse-text);
    border-color: transparent;
    font-weight: var(--gl-strong);
    font-variant-numeric: tabular-nums;
  }

  /* Nested-content chip (DLC/expansion/bundle/edition/add-on) — a quiet
     identity tag, not a rating, so it carries no tier color. */
  .type-chip, .chip.content-badge {
    align-self: flex-start;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    padding: 0 7px;
    border-radius: var(--gl-r-xs);
    border: var(--gl-bw) solid var(--gl-border-strong);
    background: var(--gl-surface);
    color: var(--gl-text-2);
  }
  .chip.content-badge { padding: 3px 8px; border-radius: var(--gl-r-sm); }
  /* Subtle "part of <base game>" line under the title on nested rows. */
  .parent-sub {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* ---- grid mode ---- */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(142px, 1fr));
    gap: 12px;
  }
  .card {
    background: var(--gl-surface);
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    box-shadow: var(--gl-shadow);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.12s ease, box-shadow 0.12s ease;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  html:not(.no-hover) .card:hover {
    transform: translateY(-1px);
    box-shadow: var(--gl-shadow-md);
  }
  .card .cover-wrap { border-bottom: var(--gl-bw) solid var(--gl-border); }

  /* Rank badge — only on grids whose payload is genuinely rank-ordered
     (render() adds .ranked and seeds the counter from the payload offset).
     Quieter than the score chip on the right: muted text, no tilt. */
  .grid.ranked { counter-reset: rank; }
  .grid.ranked .card { counter-increment: rank; }
  .grid.ranked .cover-wrap::before {
    content: "#" counter(rank);
    position: absolute;
    top: 6px;
    left: 6px;
    z-index: 1;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    font-variant-numeric: tabular-nums;
    padding: 3px 7px;
    border-radius: var(--gl-r-sm);
    background: var(--gl-surface);
    color: var(--gl-muted);
  }

  .card-body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 6px; flex: 1; }
  .title {
    font-size: var(--gl-body);
    font-weight: var(--gl-strong);
    line-height: var(--gl-h-lh);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .meta {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-text-2);
    font-variant-numeric: tabular-nums;
  }
  .card-body .pills { margin-top: auto; padding-top: 2px; }
  .scores { gap: 4px; }

  /* ---- detail mode ---- */
  .detail {
    display: flex;
    gap: 16px;
    background: var(--gl-surface);
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    box-shadow: var(--gl-shadow);
    padding: 14px;
    max-width: 720px;
  }
  .detail .cover-wrap {
    flex: 0 0 120px;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-sm);
    overflow: hidden;
    align-self: flex-start;
  }
  /* On the detail card the h1 sits right beside the plate and already says
     the name, so the plate stamped with it reads as a duplicate title. The
     gradient stays (it is the art stand-in); only the lettering goes. Grid
     cards have no heading beside the cover and keep theirs. */
  .detail .cover-fallback { color: transparent; text-shadow: none; }
  .detail-info { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .detail-info h1 {
    font-size: var(--gl-h);
    line-height: var(--gl-h-lh);
    font-weight: var(--gl-strong);
    overflow-wrap: anywhere;
  }
  .sub {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-text-2);
    font-variant-numeric: tabular-nums;
  }
  .desc {
    color: var(--gl-text-2);
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .rating-row { font-variant-numeric: tabular-nums; }
  .rating-row b { font-weight: var(--gl-strong); }

  /* ---- detail media (get_game_detail(media=True)) ---- */
  /* The detail card becomes a column stack: the media reel, the card itself,
     then the similar-games and studio panels. */
  .detail-stack { display: flex; flex-direction: column; gap: 12px; max-width: 720px; }
  .detail-stack .detail { max-width: none; }

  /* Hero trailer — mp4 with a poster fallback, or a click-to-load embed. */
"""
    + apps_shared.HERO_CSS
    + r"""
  /* Screenshot / similar strips: scroll sideways rather than wrap, so a
     narrow phone card never grows a second row of thumbnails. */
"""
    + apps_shared.STRIP_CSS
    + apps_shared.SHOT_BTN_CSS
    + r"""  /* Best-effort fullscreen: rendered only where the API exists AND the host
     allows it (a sandboxed iframe without allow="fullscreen" reports
     fullscreenEnabled false), and removed on a denied request rather than
     left sitting there doing nothing. */
"""
    + apps_shared.MEDIA_STRIP_CSS
    + r"""
  /* Similar games and the studio strip: mini cover cards. */
"""
    + apps_shared.SIMILAR_CSS
    + apps_shared.TAG_CSS
    + apps_shared.PEDIGREE_CSS
    + r"""
  /* ---- click-to-expand overlays (detail card, screenshot lightbox) ---- */
  /* Anchored near the clicked card rather than centered in the (possibly
     very tall) iframe — hosts that don't auto-scroll to modals would
     otherwise open it off-screen. JS sets the panel's top and the overlay's
     height to span the whole document. */
"""
    + apps_shared.OVERLAY_CSS
    + r"""  .overlay-panel {
    position: absolute;
    left: 50%;
    width: calc(100% - 24px);
    max-width: 720px;
    border-radius: var(--gl-r-lg);
    transform: translateX(-50%) scale(0.97) translateY(8px);
    transition: transform 0.19s ease;
  }
  .overlay.open .overlay-panel { transform: translateX(-50%); }
  .overlay-panel .detail-stack { max-width: none; }
  .overlay-panel .detail { max-width: none; margin: 0; }
  /* The screenshot carousel is a bare panel of its own (the detail overlay
     borrows its frame from the card inside it), and goes edge-to-edge of the
     iframe: on a phone a centered panel wasted a third of the width. */
  .overlay-panel.carousel {
    left: 0;
    width: 100%;
    max-width: none;
    border-top: var(--gl-bw) solid var(--gl-border);
    border-bottom: var(--gl-bw) solid var(--gl-border);
    border-radius: 0;
    background: var(--gl-stage);
    overflow: hidden;
    transform: translateY(14px);
  }
  .overlay.open .overlay-panel.carousel { transform: none; }
"""
    + apps_shared.CAROUSEL_CSS
    + apps_shared.TOAST_CSS
    + r"""  .loading-note { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); color: var(--gl-muted); }
  .loading-note::after {
    content: "…";
    display: inline-block;
    animation: pulse 1.1s ease-in-out infinite;
  }
  @keyframes pulse { 50% { opacity: 0.25; } }

  @media (min-width: 560px) {
    .detail-info h1 { font-size: var(--gl-title); line-height: var(--gl-title-lh); }
  }
  @media (max-width: 460px) {
    .detail { flex-direction: column; }
    .detail .cover-wrap { flex-basis: auto; width: 96px; }
    /* Narrow phone: smaller thumbs so more than one fits before scrolling. */
    .thumb img, .thumb-text { width: 96px; height: 55px; }
    .sim { width: 100px; }
  }
</style>
</head>
<body>
<div id="root"></div>
<script>
"""
    + apps_shared.BRIDGE_JS
    + r"""  /* App-initiated tool call, proxied by the host (MCP Apps shares the core
     tools/call method). Resolves undefined on error, denial, or timeout so
     callers can fall back to the data they already have. */
  function callTool(name, args, timeoutMs) {
    return Promise.race([
      request("tools/call", { name: name, arguments: args }),
      new Promise(function (resolve) { setTimeout(resolve, timeoutMs || 15000); }),
    ]);
  }
"""
    + apps_shared.EXTERNAL_LINK_JS
    + apps_shared.TOOL_RESULT_JS
    + r"""
  /* ---------- rendering ---------- */
"""
    + apps_shared.DOM_HELPERS_JS
    + apps_shared.COMPONENTS_JS
    + r"""  function section(parent, title) {
    var box = el("section", "panel");
    if (title) box.appendChild(el("div", "section-title", title));
    parent.appendChild(box);
    return box;
  }

"""
    + apps_shared.COVER_HUE_JS
    + "\n"
    + apps_shared.COVER_NODE_JS
    + r"""
  function matchedTagNames(game) {
    return (game.matched_tags || []).map(function (t) {
      return typeof t === "string" ? t : t.tag;
    }).filter(Boolean);
  }

  /* Nested content types (data/content.py::NESTED_CONTENT_TYPES) get a human
     badge; primary types (base_game, standalone_expansion, remake, remaster,
     expanded_game, port) are absent from this map and render no badge. */
  var CONTENT_TYPE_LABELS = {
    dlc: "DLC",
    expansion: "Expansion",
    bundle: "Bundle",
    edition: "Edition",
    unknown_addon: "Add-on",
  };
  function contentTypeLabel(game) {
    return CONTENT_TYPE_LABELS[game.content_type] || null;
  }
  /* Grid/search rows carry a flat parent_name; get_game_detail carries a
     parent: {game_id, name} back-pointer on nested rows. Support both. */
  function parentName(game) {
    if (game.parent && game.parent.name) return game.parent.name;
    if (game.parent_name) return game.parent_name;
    return null;
  }

  /* ---- click-to-expand overlays: detail card + screenshot lightbox ---- */
  /* A STACK, not a single slot: a screenshot enlarged from inside a detail
     overlay has to sit on top of it, not replace it. Later overlays are
     appended later in the DOM, so equal z-index already stacks them right. */
  var overlays = []; // [{ node, trigger, keydown, position }] — last is topmost

  function closeOverlay(entry) {
    var s = entry || overlays[overlays.length - 1];
    if (!s) return;
    var i = overlays.indexOf(s);
    if (i < 0) return; // already closed
    overlays.splice(i, 1);
    document.removeEventListener("keydown", s.keydown);
    s.node.classList.remove("open");
    setTimeout(function () { s.node.remove(); }, 200);
    if (s.trigger && s.trigger.focus) s.trigger.focus();
  }
  function closeAllOverlays() {
    while (overlays.length) closeOverlay();
  }

  function closeButton(ariaText, onClose) {
    var btn = el("button", "overlay-close", "✕");
    btn.setAttribute("aria-label", ariaText);
    btn.addEventListener("click", onClose);
    return btn;
  }

  function openOverlay(panel, trigger, onKey) {
    var overlay = el("div", "overlay");
    var entry = { node: overlay, trigger: trigger, keydown: null, position: position };
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeOverlay(entry);
    });
    // Only the topmost overlay answers keys, so closing a lightbox leaves the
    // detail card it was opened from standing — and the carousel's arrow keys
    // never reach a card sitting underneath it.
    entry.keydown = function (ev) {
      if (overlays[overlays.length - 1] !== entry) return;
      if (ev.key === "Escape") {
        closeOverlay(entry);
        return;
      }
      if (onKey) onKey(ev);
    };
    document.addEventListener("keydown", entry.keydown);
    overlays.push(entry);
    document.body.appendChild(overlay);

    // Anchor the panel near whatever was clicked, clamped inside the document;
    // stretch the backdrop over the full document height.
    function position() {
      var docH = document.documentElement.scrollHeight;
      var scrollTop = window.scrollY || document.documentElement.scrollTop || 0;
      var anchor = trigger ? trigger.getBoundingClientRect().top + scrollTop - 8 : 12;
      var top = Math.max(12, Math.min(anchor, docH - panel.offsetHeight - 12));
      panel.style.top = top + "px";
      overlay.style.height = Math.max(docH, top + panel.offsetHeight + 14) + "px";
    }
    position();
    requestAnimationFrame(function () { overlay.classList.add("open"); });
    panel.focus({ preventScroll: true });
    return entry;
  }

  function openDetail(game, trigger) {
    closeAllOverlays();
    var entry;
    var panel = el("div", "overlay-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", game.name || "Game details");
    panel.tabIndex = -1;
    panel.appendChild(closeButton("Close details", function () { closeOverlay(entry); }));

    // Instant view from the data already on the card; the live
    // get_game_detail result replaces it when it arrives.
    var lite = detailCard(game);
    var note = el("div", "loading-note", "Loading full details");
    var liteInfo = lite.querySelector(".detail-info");
    if (liteInfo) liteInfo.appendChild(note);
    panel.appendChild(lite);

    entry = openOverlay(panel, trigger);

    if (window.__PREVIEW_DATA__) { note.remove(); return; }
    // media:true is what turns the upgraded card into the full game
    // representation — trailer, screenshots, the owned games most like it.
    // The grid payload carries none of that.
    // 30s, not callTool's 15s default: a cold click-through runs the full
    // lazy enrichment AND the media lookup's own 8s budget server-side, and a
    // response that loses the race is discarded — the overlay would sit on
    // the lite card forever even though media eventually arrived.
    callTool("get_game_detail", { game_id: game.game_id, media: true }, 30000).then(function (res) {
      if (overlays.indexOf(entry) < 0) return; // closed or superseded
      var data = resultData(res);
      if (data && data.name) {
        var full = detailCard(data);
        panel.replaceChild(full, lite);
        entry.position(); // content height changed
      } else {
        note.remove(); // host declined or timed out: keep the lite view
      }
    });
  }

  /* Best-effort fullscreen: rendered only where the API exists, dropped when
     the host denies the request. A widget iframe is usually sandboxed without
     allow="fullscreen", and an inert ⛶ that does nothing is worse than no ⛶
     at all. */
"""
    + apps_shared.FULLSCREEN_BUTTON_JS
    + "\n"
    + apps_shared.NAV_BUTTON_JS
    + r"""
  /* The screenshot lightbox is a carousel over EVERY delivered screenshot:
     drag/swipe, the two arrow buttons, and the arrow keys all move through it. */
  function openCarousel(shots, startIndex, gameName, trigger) {
    var entry;
    var index = startIndex;
    var panel = el("div", "overlay-panel carousel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", (gameName ? gameName + " " : "") + "screenshots");
    panel.tabIndex = -1;
    panel.appendChild(closeButton("Close screenshots", function () { closeOverlay(entry); }));

"""
    + apps_shared.CAROUSEL_STAGE_JS
    + r"""
    entry = openOverlay(panel, trigger, function (ev) {
      if (ev.key === "ArrowLeft") show(index - 1);
      else if (ev.key === "ArrowRight") show(index + 1);
    });
    img.addEventListener("load", entry.position); // full-size art changes the height
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
    // The cover chip is always Metacritic (the fullest-coverage source in
    // this library) so the top-right corner never switches identity between
    // sources; everything else lives in the scores row below.
    if (realScore(game.metacritic_score)) {
      cover.appendChild(scoreChip({
        label: "Metacritic", value: Math.round(game.metacritic_score),
        tier: mcTier(game.metacritic_score), cls: "corner",
      }));
    }
    card.appendChild(cover);

    var body = el("div", "card-body");
    var typeLabel = contentTypeLabel(game);
    if (typeLabel) body.appendChild(el("span", "type-chip", typeLabel));
    body.appendChild(el("div", "title", game.name));
    var pName = parentName(game);
    if (pName) body.appendChild(el("div", "parent-sub", "⤷ " + pName));

    // One line of text, so a wrap breaks between words, never before a "·".
    var metaBits = [];
    var hltb = hoursLabel(game.hltb_main, true);
    if (hltb) metaBits.push(hltb);
    if (game.suggested_platform) metaBits.push(label("platform", game.suggested_platform));
    var played = num(game.playtime_hours) > 0 ? hoursLabel(game.playtime_hours) : null;
    if (played) metaBits.push(played + " played");
    if (metaBits.length) body.appendChild(el("div", "meta", metaBits.join(" · ")));

    // Secondary ratings: OpenCritic and Steam always live here.
    var scores = el("div", "chips scores");
    if (realScore(game.opencritic_score)) {
      scores.appendChild(scoreChip({
        label: "OpenCritic", value: Math.round(game.opencritic_score),
        tier: ocTier(game.opencritic_score, game.opencritic_tier),
      }));
    }
    var steam = steamChip(game.steam_review_desc);
    if (steam) scores.appendChild(steam);
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

  /* ---- media blocks (get_game_detail(media=True)) ---- */
  /* Spliced from apps_shared.py, verbatim in the evaluation card too. */
"""
    + apps_shared.HERO_MEDIA_JS
    + r"""
  /* The trailer and the screenshots are one reel, shown the way a store page
     shows them: a single 16:9 stage plus one thumb strip, trailer first.
     Clicking a thumb swaps the stage in place; clicking a screenshot IN the
     stage opens the carousel. */
"""
    + apps_shared.MEDIA_PANEL_JS
    + "\n"
    + apps_shared.OWNERSHIP_TAGS_JS
    + r"""
  /* The owned games most like this one, ranked server-side by shared tags
     (tools/game_media.py's similar_in_library) — every cover here is his. */
"""
    + apps_shared.SIMILAR_NODE_JS
    + r"""
  /* The studio behind the game and what it shipped BEFORE it — server-fetched
     and library-annotated (tools/game_media.py). Under the big-studio damper,
     or with nothing released earlier, only the header line renders: six
     arbitrary posters out of a 500-game catalogue say nothing about this game. */
"""
    + apps_shared.PEDIGREE_JS
    + r"""
  function detailCard(game) {
    // A column stack so the media blocks can span the card's full width; with
    // no media it holds the one .detail panel and looks exactly as before.
    var stack = el("div", "detail-stack");
    var media = game.media || {};
    // The media panel LEADS the stack, where the bare trailer hero used to:
    // the trailer and the screenshots are one reel and belong together.
    mediaNode(stack, media, game.name);

    var box = el("div", "detail");
    box.appendChild(coverNode(game));

    var info = el("div", "detail-info");
    info.appendChild(el("h1", null, game.name));
    var pName = parentName(game);
    if (pName) info.appendChild(el("div", "sub parent-sub", "part of " + pName));

    var subBits = [];
    if (game.release_date) subBits.push(String(game.release_date).slice(0, 4));
    var owned = (game.platforms || []).filter(function (p) { return p.owned; })
      .map(function (p) { return label("platform", p.platform); });
    if (owned.length) subBits.push(owned.join(", "));
    var played = num(game.playtime_hours) > 0 ? hoursLabel(game.playtime_hours) : null;
    if (played) subBits.push(played + " played");
    if (game.wishlisted && !game.owned) subBits.push("wishlisted");
    if (subBits.length) info.appendChild(el("div", "sub", subBits.join(" · ")));

    // Metacritic leads (it's the cover-chip source); every chip links out to
    // its source page via the host when a URL is known or derivable.
    var appid = game.steam_appid != null ? game.steam_appid : game.appid;
    var badges = el("div", "chips badges");
    var typeLabel = contentTypeLabel(game);
    if (typeLabel) badges.appendChild(el("span", "chip content-badge", typeLabel));
    if (realScore(game.metacritic_score)) {
      badges.appendChild(scoreChip({
        label: "Metacritic", value: Math.round(game.metacritic_score),
        tier: mcTier(game.metacritic_score), url: game.metacritic_url,
      }));
    }
    if (realScore(game.opencritic_score)) {
      badges.appendChild(scoreChip({
        label: "OpenCritic", value: Math.round(game.opencritic_score),
        tier: ocTier(game.opencritic_score, game.opencritic_tier), url: game.opencritic_url,
      }));
    }
    var steam = steamChip(game.steam_review_desc,
      appid != null ? "https://store.steampowered.com/app/" + appid + "/" : null);
    if (steam) badges.appendChild(steam);
    var hltb = hoursLabel(game.hltb_main, true);
    if (hltb) {
      badges.appendChild(scoreChip({
        label: "HLTB", value: hltb, title: "HowLongToBeat, main story",
        url: game.name ? "https://howlongtobeat.com/?q=" + encodeURIComponent(game.name) : null,
      }));
    }
    if (game.protondb_tier) {
      badges.appendChild(scoreChip({
        label: "ProtonDB", value: label("tier", game.protondb_tier),
        url: appid != null ? "https://www.protondb.com/app/" + appid : null,
      }));
    }
    if (badges.childNodes.length) info.appendChild(badges);

    if (game.my_rating && game.my_rating.normalized_score != null) {
      var r = el("div", "rating-row");
      r.appendChild(el("span", null, "My rating: "));
      r.appendChild(el("b", null, game.my_rating.normalized_score + "/10"));
      info.appendChild(r);
    }

    // media.short_description is the same kind of blurb as the stored one and
    // often literally identical — render one, preferring the library's own.
    var description = game.short_description || media.short_description;
    if (description) info.appendChild(el("p", "desc", description));

    var tags = (game.tags || []).slice(0, 8);
    if (tags.length) {
      var pills = el("div", "pills");
      tags.forEach(function (t) { pills.appendChild(el("span", "pill", t)); });
      info.appendChild(pills);
    }

    // A never-enriched row (an assessment-minted candidate, a fresh wishlist
    // entry) fills nothing but the title, which renders as a card that looks
    // broken. Say so, and — when the response explained which providers were
    // skipped and why (get_game_detail's `enrichment`) — say that too.
    if (info.childNodes.length === 1) {
      var emptyText = "No details fetched yet";
      var why = game.enrichment;
      if (why && typeof why === "object") {
        var parts = [];
        Object.keys(why).forEach(function (k) { parts.push(k + ": " + why[k]); });
        if (parts.length) emptyText += " — " + parts.join(", ");
      }
      info.appendChild(el("div", "sub empty-state", emptyText));
    }

    box.appendChild(info);
    stack.appendChild(box);

    if (game.similar) similarNode(stack, game.similar);
    pedigreeNode(stack, game.pedigree);
    return stack;
  }

  /* get_game_detail is called with a name / game_id / appid; discover_games
     never is — so the arguments tell us which shape to sketch. */
  function skeletonKind() {
    var args = lastToolInput || {};
    return args.name || args.game_id != null || args.appid != null ? "detail" : "grid";
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
        // (page two starts at #21). Payloads without it stay unnumbered.
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

"""
    + apps_shared.SIZING_JS
    + apps_shared.INIT_JS
    + r"""  startWidget("gamelib-game-cards");
  if (window.__PREVIEW_DATA__ && window.__PREVIEW_OPEN_INDEX__ != null) {
    var previewCards = root.querySelectorAll(".card");
    var target = previewCards[window.__PREVIEW_OPEN_INDEX__];
    if (target) target.click();
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
GAME_CARDS_URI = (
    f"ui://gamelib/game-cards-{hashlib.sha1(GAME_CARDS_HTML.encode()).hexdigest()[:8]}.html"
)

# Attached to tools whose results the widget renders.
GAME_CARDS_APP = AppConfig(resource_uri=GAME_CARDS_URI)


def register_apps(mcp: Any) -> None:
    """Register the game-cards UI resource on the FastMCP app."""

    # prefersBorder=False: the widget paints its own panels on a transparent
    # page, so a host-drawn frame around it would double the border.
    @mcp.resource(
        GAME_CARDS_URI,
        name="game_cards_view",
        description="Cover-art card UI for game tool results (MCP Apps).",
        app=AppConfig(csp=_GAME_CARDS_CSP, prefers_border=False),
    )
    def game_cards_view() -> str:
        return GAME_CARDS_HTML
