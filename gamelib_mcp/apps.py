"""MCP Apps (io.modelcontextprotocol/ui): the game-cards widget.

One `ui://` resource serves both card layouts: a binder page of cards when the
tool result carries a ``results`` array (discover_games) and a single detail
card otherwise (get_game_detail). Clients that don't speak the Apps extension
ignore the tool metadata entirely and see the normal JSON responses, so
attaching ``GAME_CARDS_APP`` to a tool is purely additive.

Visual language: "The Binder" (docs/specs/2026-10-04-binder-design-language.md)
on the host's own theme. Every game is a collectible card built from the
shared primitives in apps_shared.py — the ``.frame`` (its rarity border is the
one tier the card is about), the grain, the ``.art`` window, the ``.badge``,
the title ``.plate``, ``.stats``, ``.pips``, the ``.ribbon`` and ``.mini``
cards — on a transparent page; every color is a ``--gl-*`` token. This module
only adds layout glue (grid columns, the set line, the top bar, the detail
flow). Grids whose payload carries an ``offset`` (discover_games — genuinely
rank-ordered) number their cards "No. 1"-style, globally across pagination;
payloads without that signal never show a rank.

Grid mode: the set line says what the page IS — "8 of 2090" and the facets
(sort, vibes, "unplayed", "≤ 10h") as lozenges — rebuilt from the tool-input
arguments whenever they arrive. Each card is a ``<button class="frame
frame-s">``: the cover with the badge (taste match, or the critic score on a
critic/value sort), an art-edge ribbon only for TOP MATCH / CRITICS' PICK, the
plate (title, hours, platform, rank), one row of at most two chips and the
matched tags. Its frame tier is the match (>=70 good, 50-69 ok) or, sorted by
critics or value, the critic tier. The footer carries at most two actions:
"Show next N" (a chat message asking for the next page) and "Open full screen"
(the host's fullscreen mode: four columns, the set line sticks and carries
"Show next N").

Tapping a card hands control back to the conversation instead of opening an
in-widget overlay: the selection always goes into the model context
(ui/update-model-context); then, on a host that offers fullscreen, the widget
goes fullscreen and drills into a live ``get_game_detail(media=True)`` call
(app-initiated ``tools/call``) behind a "Back to results" pill; elsewhere it
posts "Show me <name>" as the user.

Detail mode is the big card — frame tier from his rating (else the critic
tier), badge "8 /10" YOUR RATING, the status ribbon on the art's edge, the
plate, the stat block (PLAYED, LAST, LENGTH or the three HowLongToBeat
lengths, PAID, the score chips, RATING pips) — then the ground: the 3-line
description, his review as a quote, The Game Awards lines, the genres and
tags, the media reel (one 16:9 viewer plus one thumb strip, trailer first,
screenshots opening a lightbox), IN YOUR LIBRARY and FROM THE STUDIO (the
lead developer, founding year and publisher, then the studio's releases
around this game with a hairline at its year) strips of mini cards, any
enrichment notice ("Not fetched — Steam: no app id"), and the store link
when one is derivable. The media blocks live in apps_shared.py and
are spliced verbatim into both this widget and the evaluation card
(apps_eval.py).

Motion is the shared MOTION_CSS/MOTION_JS: a card is dealt in once when its
result arrives (grid cards 40ms apart), a skeleton resolves into the result,
and a press is CSS. Nothing else moves.

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
    + apps_shared.BINDER_CSS
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
    color: var(--gl-plate-ink);
    text-shadow: 0 1px 3px var(--gl-plate-shadow);
    padding: 12px;
    text-align: center;
    overflow: hidden;
    overflow-wrap: anywhere;
  }
  /* The detail card's plate sits right under the art and already says the
     name, so the art stand-in lettered with it reads as a duplicate title. The
     gradient stays; grid cards (title-s under a small cover) keep theirs. */
  .dt-card .cover-fallback { color: transparent; text-shadow: none; }

  /* ---- layout glue (everything visual is the shared Binder primitives) ---- */
  /* "part of <base game>" under the title on nested rows. */
  .parent-sub {
    margin-top: 2px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* A tag line: gap-separated words in the tertiary cap style, never a
     middot string; the grid's matched tags get a 4px diamond bullet. */
  .tagline {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 10px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
  }
  .gc-tags { gap: 0 10px; margin-top: auto; padding: 8px 8px 10px; }
  .gc-tags > span::before {
    content: "";
    display: inline-block;
    width: 6px;
    height: 6px;
    margin: 0 7px 1px 1px;
    transform: rotate(45deg);
    background: var(--gl-muted);
  }

  /* ---- grid mode ---- */
  /* The set line: what this binder page IS — the count, then the facets as
     lozenges (sentence case: they are words, not platform labels). */
  .grid-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-bottom: 14px;
  }
  .grid-head > b {
    margin-right: 4px;
    font-size: var(--gl-h);
    font-weight: var(--gl-strong);
    line-height: var(--gl-h-lh);
    color: var(--gl-text);
    font-variant-numeric: tabular-nums;
  }
  .grid-head > .loz { text-transform: none; letter-spacing: 0; font-weight: var(--gl-regular); }
  .grid-head > .btn { margin-left: auto; }
  /* Two cards a row on a phone, three from 560px, four in fullscreen. */
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  @media (min-width: 560px) {
    .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    html[data-display-mode="fullscreen"] .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  }
  .gc-card { -webkit-tap-highlight-color: transparent; }
  /* A 12px bottom inset: nothing the card ends on (chip row, tag line, the
     Steam phrase chip) may sit on the frame's edge. */
  .frame.gc-card { padding-bottom: 12px; }
  .gc-card > :last-child { padding-bottom: 0; margin-bottom: 0; }
  .gc-card .plate { border: 0; padding: 8px 8px 0; }
  /* One row, always: the hours and the lozenge give way (ellipsis) before
     "No. N" would wrap onto a line of its own. */
  .gc-card .plate .sub { gap: 4px 6px; flex-wrap: nowrap; }
  .gc-card .sub > span { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .gc-card .sub > .loz { padding: 0 5px; display: block; line-height: 18px; }
  .gc-card .card-no { margin-left: auto; flex-shrink: 0; overflow: visible; }
  .gc-chips { padding: 8px 8px 0; }
  .gc-nochip {
    padding: 8px 8px 0;
    font-size: var(--gl-cap);
    line-height: 24px;
    color: var(--gl-muted);
  }
  /* The Steam chip on a small card: the phrase, then its meter; the brand
     name (an .sr-only label) is heard, and is in the card's label. */
  .gc-card .chip.gc-steam b { order: -1; }
  .gc-card .chip.gc-steam { flex-wrap: nowrap; }

  /* At most two pills, bottom right; side by side, wrapping only when they
     cannot share a line. */
  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    align-items: center;
    gap: 8px;
    margin-top: 16px;
  }
  /* On a phone the two pills share the row (the mock-up); wider, they keep
     their own width at the right. */
  @media (max-width: 559px) {
    .grid-actions > .btn { flex: 1 1 auto; }
  }
  .btn[disabled] { cursor: default; color: var(--gl-muted); border-color: var(--gl-border); }
  html[data-display-mode="fullscreen"] .act-expand { display: none; }

  /* Fullscreen: the set line (or the drill-in's top bar) sticks to the top
     while the cards scroll under it. */
  html[data-display-mode="fullscreen"] .grid-head, .topbar {
    position: sticky;
    top: 0;
    z-index: 5;
    margin: -12px -12px 12px;
    padding: 12px;
    background: var(--gl-surface);
    border-bottom: var(--gl-bw) solid var(--gl-border);
  }
  .topbar { display: flex; align-items: center; gap: 12px; }
  .topbar > .btn { flex: none; padding: 0 18px 0 12px; }

  /* ---- detail mode ---- */
  /* The big card, then the ground under it. From 560px the card (300px)
     sits left and the ground runs beside it. */
  .detail-stack { display: flex; flex-direction: column; max-width: 760px; }
  html[data-display-mode="fullscreen"] .detail-stack { margin: 0 auto; }
  .dt-card .plate-title { margin: 0; }
  .dt-card .stats { padding-bottom: 12px; }
  .dt-card .stats > .chips { padding: 6px 0 4px; }
  .dt-flow { display: flex; flex-direction: column; min-width: 0; }
  .dt-flow > * { margin-top: 20px; }
  .dt-flow > .more-toggle, .dt-flow > .tagline { margin-top: 0; }
  .dt-flow > .tagline { padding-top: 14px; }
  .dt-flow > .panel { background: none; border: 0; box-shadow: none; padding: 0; border-radius: 0; }
  .dt-abil { display: flex; flex-direction: column; gap: 10px; }
  .eyebrow-sec > .notice { padding: 0; }
  .desc {
    color: var(--gl-text);
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .desc.open { display: block; overflow: visible; }
  .more-toggle {
    position: relative;
    align-self: flex-start;
    min-height: 44px;
    margin: -6px 0 -10px;
    padding: 0;
    border: 0;
    background: none;
    font-size: var(--gl-body);
    font-weight: var(--gl-strong);
    color: var(--gl-text);
    cursor: pointer;
  }
  .more-toggle[hidden] { display: none; }
  .dt-actions { justify-content: flex-start; }
  @media (min-width: 560px) {
    .detail-stack {
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      column-gap: 24px;
      align-items: start;
    }
    .dt-flow > :first-child { margin-top: 0; }
  }

  /* ---- detail media (get_game_detail(media=True)) ---- */
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
    + apps_shared.TAG_CSS
    + apps_shared.PEDIGREE_CSS
    + r"""  /* Mini-card strips (similar, studio): cards that snap card by card, the
     next one peeking past the edge. The snap padding comes from the host's
     safe-area insets and the shared strip's trailing spacer keeps the last
     card reachable. */
  .strip:not(.thumbs) {
    scroll-snap-type: x mandatory;
    overscroll-behavior-x: contain;
    scroll-padding-left: calc(4px + var(--gl-safe-left, 0px));
    scroll-padding-right: calc(4px + var(--gl-safe-right, 0px));
  }

  /* ---- screenshot lightbox (the detail card's own media viewer) ---- */
  /* Fixed over the iframe's viewport — the whole document in an auto-sized
     inline frame, the screen in fullscreen — with the panel anchored at the
     stage that opened it so it never lands off-screen. */
"""
    + apps_shared.OVERLAY_CSS
    + r"""  .overlay.lightbox { position: fixed; inset: 0; }
  .lightbox-panel {
    position: absolute;
    left: 0;
    right: 0;
    background: var(--gl-stage);
    border-top: var(--gl-bw) solid var(--gl-border);
    border-bottom: var(--gl-bw) solid var(--gl-border);
    transform: translateY(14px);
    transition: transform 0.19s ease;
  }
  .overlay.open .lightbox-panel { transform: none; }
"""
    + apps_shared.CAROUSEL_CSS
    + apps_shared.TOAST_CSS
    + r"""
  @media (max-width: 460px) {
    /* Narrow phone: smaller thumbs so more than one fits before scrolling. */
    .thumb img, .thumb-text { width: 96px; height: 55px; }
  }
</style>
</head>
<body>
<div id="root"></div>
<script>
"""
    + apps_shared.BRIDGE_JS
    + r"""  /* App-initiated tool call, proxied by the host (MCP Apps shares the core
     tools/call method). Resolves the tool result (which may carry isError),
     undefined on a host error or denial, or TIMED_OUT when nothing answered
     within timeoutMs — so a caller can say which one happened and fall back
     to the data it already has. The bridge request itself expires a second
     later, which drops its pending entry. */
  var TIMED_OUT = { timedOut: true };
  function callTool(name, args, timeoutMs) {
    var ms = timeoutMs || 15000;
    return Promise.race([
      request("tools/call", { name: name, arguments: args }, ms + 1000),
      new Promise(function (resolve) { setTimeout(function () { resolve(TIMED_OUT); }, ms); }),
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
    + apps_shared.BINDER_JS
    + "\n"
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
  /* The nested-content type is a quiet identity lozenge on the plate's sub
     line, not a rating, so it carries no tier color. */
  function typeLoz(game) {
    var typeLabel = contentTypeLabel(game);
    return typeLabel ? el("span", "loz type-loz", typeLabel) : null;
  }
  /* Grid/search rows carry a flat parent_name; get_game_detail carries a
     parent: {game_id, name} back-pointer on nested rows. Support both. */
  function parentName(game) {
    if (game.parent && game.parent.name) return game.parent.name;
    if (game.parent_name) return game.parent_name;
    return null;
  }
  /* The critic tier of a game: the shared lead critic's (OpenCritic, else
     Metacritic), else none. */
  function criticTier(game) {
    var lead = leadCritic(game);
    return lead ? lead.tier : "none";
  }

  /* ---------- view state ---------- */
  var view = "none";              // "grid" | "detail" | "drill" | "none"
  var gridData = null;            // the grid payload Back returns to
  var gridMode = null;            // the display mode the grid was laid out for
  var dealtKey = null;            // the page (offset + game ids) whose cards were dealt in
  var drillSeq = 0;               // invalidates a superseded detail fetch
  var modeBeforeDrill = "inline";
  var lastSelectedId = null;

  /* ---------- grid: the set line ---------- */
  /* get_game_detail is called with a name / game_id / appid / items;
     discover_games never is. */
  function isDetailArgs(args) {
    return !!(args.name || args.game_id != null || args.appid != null || args.items);
  }
  var SORT_LABELS = { match: "taste match", critic: "critic score", value: "value" };
  /* The page's sort as the tool ran it: discover_games defaults to match. */
  function gridSort() {
    var args = lastToolInput;
    if (!args || isDetailArgs(args)) return "match";
    return SORT_LABELS[args.sort_by] ? args.sort_by : "match";
  }
  function headerParts(data) {
    var shown = data.results.length;
    var total = num(data.total_matches);
    var count = total != null && total > shown
      ? shown + " of " + total
      : plural(shown, "game");
    var facets = [];
    var args = lastToolInput;
    if (args && !isDetailArgs(args)) {
      facets.push(SORT_LABELS[args.sort_by] || SORT_LABELS.match);
      // Each vibe is a facet of its own; every filter says what it filters.
      list(args.vibes).filter(Boolean).forEach(function (v) { facets.push(String(v)); });
      // discover_games defaults unplayed_only to true: absent means on.
      if (args.unplayed_only !== false) facets.push("unplayed");
      var maxHours = num(args.max_hltb_hours);
      if (maxHours != null) facets.push("≤ " + maxHours + "h");
      var minScore = num(args.min_score);
      if (minScore != null) facets.push("critics ≥ " + minScore);
      if (args.protondb_min_tier) {
        facets.push("ProtonDB " + String(args.protondb_min_tier).toLowerCase() + "+");
      }
    }
    return { count: count, facets: facets };
  }
  function headerLine(data) {
    var parts = headerParts(data);
    var head = el("div", "grid-head");
    head.appendChild(el("b", null, parts.count));
    parts.facets.forEach(function (facet) { head.appendChild(el("span", "loz", facet)); });
    return head;
  }

  /* ---------- grid: actions (at most two) ---------- */
  /* "Show next N" (or null when there is no next page). */
  function showNextButton(data) {
    var shown = data.results.length;
    if (!data.has_more || !shown) return null;
    var args = lastToolInput || {};
    var limit = num(args.limit) || shown;
    var offset = typeof data.offset === "number" ? data.offset : 0;
    var next = offset + shown;
    var total = num(data.total_matches);
    var count = total != null && total > next ? Math.min(limit, total - next) : limit;
    var idle = "Show next " + count;
    var more = el("button", "btn act-more", idle);
    more.type = "button";
    more.addEventListener("click", function () {
      sendMessage("Show the next " + count + " recommendations (offset " + next + ")");
      more.disabled = true;
      more.textContent = "Asked the chat for the next " + count + "…";
      // A lost message must be retryable: the button comes back after 8s.
      setTimeout(function () {
        more.disabled = false;
        more.textContent = idle;
      }, 8000);
    });
    return more;
  }
  /* The footer: "Show next N" (secondary) and "Open full screen" (primary).
     In fullscreen there is no footer: "Show next N" rides on the sticky set
     line (renderGrid) and the grid is already full screen. */
  function gridActions(data) {
    var bar = el("div", "actions grid-actions");
    var more = showNextButton(data);
    if (more) bar.appendChild(more);
    // An empty page has nothing to show bigger.
    if (canFullscreen() && data.results.length) {
      var expand = el("button", "btn primary act-expand", "Open full screen");
      expand.type = "button";
      // A grant through this request lays the grid out for fullscreen at
      // once (the sticky "Show next", no footer), as the drill-in does —
      // the host may never send a host-context-changed for it.
      expand.addEventListener("click", function () {
        requestDisplayMode("fullscreen").then(function (mode) {
          if (mode === "fullscreen" && view === "grid" && gridData && gridMode !== mode) render(gridData);
        });
      });
      bar.appendChild(expand);
    }
    return bar.childNodes.length ? bar : null;
  }

  /* ---------- grid: cards ---------- */
  /* What a card shows besides its cover, title and matched tags, in reading
     order: the badge ("73% match", or the lead critic — "OpenCritic 84",
     else "Metacritic 88" — on a critic or value sort), the ribbon ("Top match", "Critics' pick"), the plate's hours
     ("~4h to beat" / "2.3h played") and platform, then at most two score
     chips (Metacritic and OpenCritic drawn as MC / OC, or the Steam phrase).
     gridCard renders these and cardLabel reads them, so what is heard is
     what is shown — with the full names a small card abbreviates. ctx:
     {index, offset, sort} of the card on its page (all optional). */
  var CRITICS_PICK = 85;
  var TOP_MATCH = 70;
  function cardBits(game, ctx) {
    var c = ctx || {};
    var sort = c.sort || "match";
    var bits = [];
    var mc = realScore(game.metacritic_score) ? Math.round(game.metacritic_score) : null;
    var oc = realScore(game.opencritic_score) ? Math.round(game.opencritic_score) : null;
    var pct = game.match_percent != null
      ? Math.max(0, Math.min(100, Math.round(num(game.match_percent) || 0))) : null;
    var badgeSource = null;
    var lead = leadCritic(game);
    if (pct != null && sort === "match") {
      bits.push({ part: "badge", value: pct, suffix: "%", tag: "Match",
                  tier: pct >= TOP_MATCH ? "good" : pct >= 50 ? "ok" : "none", text: pct + "% match" });
    } else if (lead) {
      badgeSource = lead.source;
      bits.push({ part: "badge", value: lead.value, tag: lead.source, tier: lead.tier,
                  text: lead.source + " " + lead.value });
    }
    if (pct != null && pct >= TOP_MATCH && sort === "match" && c.index === 0 && c.offset === 0) {
      bits.push({ part: "ribbon", text: "Top match" });
    } else if (mc != null && mc >= CRITICS_PICK) {
      bits.push({ part: "ribbon", text: "Critics' pick" });
    }
    var played = num(game.playtime_hours) > 0 ? hoursLabel(game.playtime_hours) : null;
    var hltb = hoursLabel(game.hltb_main, true);
    if (played) bits.push({ part: "hours", value: played, title: "Your playtime", text: played + " played" });
    else if (hltb) bits.push({ part: "hours", value: hltb, title: "HowLongToBeat, main story", text: hltb + " to beat" });
    if (game.suggested_platform) {
      bits.push({ part: "platform", text: label("platform", game.suggested_platform),
        short: label("platform_short", game.suggested_platform) });
    }
    var chips = [];
    if (mc != null && badgeSource !== "Metacritic") {
      chips.push({ part: "chip", label: "MC", value: mc, tier: mcTier(mc), title: "Metacritic",
                   text: "Metacritic " + mc });
    }
    if (oc != null && badgeSource !== "OpenCritic") {
      chips.push({ part: "chip", label: "OC", value: oc, tier: ocTier(oc, game.opencritic_tier),
                   title: "OpenCritic", text: "OpenCritic " + oc });
    }
    var steam = steamChip(game.steam_review_desc, null);
    if (steam) {
      steam.classList.add("gc-steam");
      var steamLbl = steam.querySelector(".lbl");
      if (steamLbl) steamLbl.classList.add("sr-only");
      chips.push({ part: "chip", chip: steam,
                   text: "Steam " + (steam.querySelector("b") || steam).textContent });
    }
    return bits.concat(chips.slice(0, 2));
  }
  /* What a screen reader hears for a card: the game, then cardBits. */
  function cardLabel(game, bits) {
    var text = (bits || cardBits(game)).map(function (b) { return b.text; });
    var name = game.name || "Untitled game";
    return text.length ? name + ": " + text.join(", ") : name;
  }

  function gridCard(game, ctx) {
    var c = ctx || {};
    var bits = cardBits(game, c);
    function partsOf(part) { return bits.filter(function (b) { return b.part === part; }); }
    var badgeBit = partsOf("badge")[0] || null;
    // The frame is the tier the card is about: the match, or (sorted by
    // critics or value) the critic tier the badge shows.
    // A card with no game_id has nothing to open: a plain frame, not a button.
    var tappable = game.game_id != null;
    var card = frameNode(tappable ? "button" : "div", "frame-s gc-card", badgeBit ? badgeBit.tier : "none");
    var titleId = "gc-title-" + (typeof c.index === "number" ? c.index : 0);
    if (tappable) {
      card.type = "button";
      card.setAttribute("data-game-id", String(game.game_id));
      card.addEventListener("click", function () { selectGame(game); });
      // The button is named by the game and what the card shows (no
      // aria-labelledby: the name must carry the scores, not the title alone).
      card.setAttribute("aria-label", cardLabel(game, bits));
    } else {
      // A plain card is a group named by its own title, its content read as
      // it stands; an aria-label on a bare <div> would be ignored or override it.
      card.setAttribute("role", "group");
      card.setAttribute("aria-labelledby", titleId);
    }

    // The art: the cover, the badge top-left, the ribbon on its bottom edge.
    var art = el("div", "art");
    art.appendChild(coverNode(game));
    if (badgeBit) {
      var badge = badgeNode({ value: badgeBit.value, suffix: badgeBit.suffix, tag: badgeBit.tag,
                              tier: badgeBit.tier });
      badge.classList.add("badge-on-art");
      art.appendChild(badge);
    }
    partsOf("ribbon").forEach(function (b) { art.appendChild(ribbonNode(b.text, "good", null, "s art")); });
    card.appendChild(art);

    // The plate: title-s, then the sub line of separate spans — hours, the
    // platform lozenge, the type lozenge, and the rank on the right.
    var plate = el("div", "plate");
    var title = el("div", "plate-title-s", game.name);
    title.id = titleId;
    plate.appendChild(title);
    var pName = parentName(game);
    if (pName) plate.appendChild(el("div", "parent-sub", "⤷ " + pName));
    var sub = el("div", "sub");
    partsOf("hours").forEach(function (b) {
      var span = el("span", null, b.value);
      span.title = b.title;
      sub.appendChild(span);
    });
    partsOf("platform").forEach(function (b) { sub.appendChild(el("span", "loz", b.short)); });
    var type = typeLoz(game);
    if (type) sub.appendChild(type);
    // Rank only when the payload is explicitly rank-ordered: discover_games
    // sends its pagination offset, so numbering is global (page two starts
    // at No. 21). Payloads without it stay unnumbered. A rank is a position,
    // not an id, so it stays plain ("No. 1") — cardNo's zero-padding is for
    // ids (the detail card's "No. 046").
    if (typeof c.offset === "number" && typeof c.index === "number") {
      sub.appendChild(el("span", "card-no", "No. " + (c.offset + c.index + 1)));
    }
    if (sub.childNodes.length) plate.appendChild(sub);
    card.appendChild(plate);

    // One chip row of at most two, or the honest absence.
    var chips = partsOf("chip");
    if (chips.length) {
      var row = el("div", "chips gc-chips");
      chips.forEach(function (b) {
        if (b.chip) { row.appendChild(b.chip); return; }
        var chip = scoreChip({ label: b.label, value: b.value, tier: b.tier, title: b.title });
        chip.setAttribute("aria-label", b.text);
        row.appendChild(chip);
      });
      card.appendChild(row);
    } else if (!badgeBit || badgeBit.tag === "Match") {
      card.appendChild(el("div", "gc-nochip", "No critic scores"));
    }

    // Why it matched: one tertiary line of words, at most three.
    var why = matchedTagNames(game).slice(0, 3);
    if (why.length) {
      var tags = el("div", "tagline gc-tags");
      why.forEach(function (t) { tags.appendChild(el("span", null, t)); });
      card.appendChild(tags);
    }
    return card;
  }

  function gridKey(data) {
    var ids = list(data.results).map(function (g) { return g && g.game_id != null ? g.game_id : g && g.name; });
    return (typeof data.offset === "number" ? data.offset : "") + ":" + ids.join(",");
  }
  function renderGrid(data) {
    var page = el("div", "grid-page");
    var head = headerLine(data);
    page.appendChild(head);
    gridMode = currentDisplayMode();
    // Deal the cards in once per PAGE: a redraw of the same page (the tool
    // input landing late, Back from a drill-in, a display-mode change, or a
    // host re-delivering the same result as a new object) keeps them still.
    // The key is what the page shows — its offset and its game ids — never
    // the payload object's identity.
    var key = gridKey(data);
    var fresh = dealtKey !== key;
    dealtKey = key;
    if (fresh) fadeIn(head);
    if (!data.results.length) {
      var none = el("div", "empty");
      none.appendChild(el("span", null, "No games match."));
      none.appendChild(document.createTextNode(" "));
      none.appendChild(el("span", null, "Try fewer vibes or filters."));
      page.appendChild(none);
    } else {
      var grid = el("div", "grid");
      var ctx = { sort: gridSort(), offset: typeof data.offset === "number" ? data.offset : null };
      data.results.forEach(function (g, i) {
        var card = gridCard(g, { index: i, offset: ctx.offset, sort: ctx.sort });
        if (fresh) dealIn(card, i);
        grid.appendChild(card);
      });
      page.appendChild(grid);
    }
    if (gridMode === "fullscreen") {
      var more = showNextButton(data);
      if (more) head.appendChild(more);
    } else {
      var actions = gridActions(data);
      if (actions) page.appendChild(actions);
    }
    return page;
  }

  /* ---------- card tap: hand back to the conversation ---------- */
  /* The selection always reaches the model. Then: fullscreen + a live detail
     drill-in where the host offers fullscreen, else a chat message — and
     never both. While the fullscreen request is open the tap is PENDING: a
     grant (the answer, or a host-context-changed to fullscreen that beats
     it) drills in; another mode, or 8s of silence, sends the message. The
     first of those to land settles it and the others are ignored. */
  var pendingSelection = null;                      // { game, before } while asking
  function selectGame(game) {
    lastSelectedId = game.game_id;
    updateModelContext(
      "User selected " + game.name + " (game_id " + game.game_id + ") from the recommendations",
      { game_id: game.game_id, name: game.name });
    if (!canFullscreen()) {
      sendMessage("Show me " + game.name);
      return;
    }
    var selection = { game: game, before: currentDisplayMode() };
    pendingSelection = selection;
    requestDisplayMode("fullscreen", 8000).then(function (mode) {
      if (pendingSelection !== selection) return;   // a late grant (or a newer tap) won
      pendingSelection = null;
      if (mode === "fullscreen") openDrill(game, selection.before);
      else sendMessage("Show me " + game.name);     // refused, or no answer in 8s
    });
  }
  /* hooks.afterHostContext: the host can grant fullscreen by context change
     before (or instead of) answering the request; leaving fullscreen through
     its own close button ends a drill-in; a grid whose display mode changed
     is laid out again (four columns and the sticky "Show next" in
     fullscreen) without dealing its cards a second time. */
  function hostContextChanged(ctx) {
    if (pendingSelection && ctx && ctx.displayMode === "fullscreen") {
      var selection = pendingSelection;
      pendingSelection = null;
      openDrill(selection.game, selection.before);
      return;
    }
    if (view === "drill" && currentDisplayMode() !== "fullscreen" && gridData) render(gridData);
    else if (view === "grid" && gridData && currentDisplayMode() !== gridMode) render(gridData);
  }

  var BACK_GLYPH = [["path", { d: "m12.5 4.5-5.5 5.5 5.5 5.5" }]];
  function openDrill(game, before) {
    var seq = ++drillSeq;
    modeBeforeDrill = before;
    view = "drill";
    root.textContent = "";
    var bar = el("div", "topbar");
    var back = el("button", "btn");
    back.type = "button";
    var glyph = iconNode("0 0 20 20", BACK_GLYPH);
    if (glyph) back.appendChild(glyph);
    back.appendChild(el("span", null, "Back to results"));
    back.addEventListener("click", backToResults);
    bar.appendChild(back);
    root.appendChild(bar);
    var holder = el("div", "drill");
    holder.appendChild(skeleton("detail"));
    root.appendChild(holder);
    window.scrollTo(0, 0);
    back.focus({ preventScroll: true });
    reportSize();

    // The skeleton resolves into the card (M5): it fades out while the
    // detail deals in on top of it.
    function fill(data, failure) {
      if (seq !== drillSeq) return;               // Back, or another tap, won
      var skel = holder.firstElementChild;
      if (skel && skel.classList.contains("skel")) {
        gcResolve(skel, detailCard(data));
      } else {
        holder.textContent = "";
        holder.appendChild(detailCard(data));
      }
      if (failure) notice(holder, failure + " Showing what the list had.");
      reportSize();
    }
    if (window.__PREVIEW_DATA__) { fill(game, null); return; }
    // media:true is what turns the card into the full game representation —
    // trailer, screenshots, the owned games most like it; the grid payload
    // carries none of that. 30s, not callTool's 15s default: a cold
    // click-through runs the full lazy enrichment AND the media lookup's own
    // 8s budget server-side, and a response that loses the race is dropped.
    callTool("get_game_detail", { game_id: game.game_id, media: true }, 30000).then(function (res) {
      if (res === TIMED_OUT) { fill(game, "The library didn't answer in 30s."); return; }
      if (res && res.isError) { fill(game, toolErrorText(res, "get_game_detail")); return; }
      var data = resultData(res);
      if (data && data.name) fill(data, null);
      else if (res === undefined) fill(game, "The host didn't run the lookup.");
      else fill(game, "The details came back unreadable.");
    });
  }

  function backToResults() {
    var restore = modeBeforeDrill;
    if (gridData) render(gridData);
    if (restore !== "fullscreen") requestDisplayMode("inline");
    var cards = root.querySelectorAll(".gc-card");
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute("data-game-id") === String(lastSelectedId)) {
        cards[i].focus({ preventScroll: false });
        break;
      }
    }
  }
  /* Best-effort fullscreen: rendered only where the API exists, dropped when
     the host denies the request. A widget iframe is usually sandboxed without
     allow="fullscreen", and an inert ⛶ that does nothing is worse than no ⛶
     at all. */
"""
    + apps_shared.FULLSCREEN_BUTTON_JS
    + "\n"
    + apps_shared.NAV_BUTTON_JS
    + apps_shared.LIGHTBOX_CHROME_JS
    + r"""
  /* ---------- screenshot lightbox ---------- */
  /* The detail card's own media viewer (not navigation): one slot, fixed over
     the iframe, closed by ✕, Escape or the backdrop; Tab stays inside it and
     focus returns to the stage that opened it (the chrome is shared). */
  var lightbox = null;
  function closeLightbox() {
    var current = lightbox;
    if (!current) return;
    lightbox = null;
    document.removeEventListener("keydown", current.onKey, true);
    document.removeEventListener("focusin", current.onFocus, true);
    current.overlay.classList.remove("open");
    setTimeout(function () { current.overlay.remove(); }, 200);
    if (current.trigger && current.trigger.focus) current.trigger.focus({ preventScroll: true });
  }
  function openCarousel(shots, startIndex, gameName, trigger) {
    closeLightbox();
    var index = startIndex;
    var overlay = el("div", "overlay lightbox");
    var chrome = lightboxPanel("lightbox-panel carousel", gameName, closeLightbox);
    var panel = chrome.panel;
    var closer = chrome.closer;

"""
    + apps_shared.CAROUSEL_STAGE_JS
    + r"""
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeLightbox();
    });
    var onKey = lightboxKeys(panel, function (delta) { show(index + delta); }, closeLightbox);
    var onFocus = focusGuard(panel);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("focusin", onFocus, true);
    lightbox = { overlay: overlay, trigger: trigger, onKey: onKey, onFocus: onFocus };
    document.body.appendChild(overlay);

    // Viewport coordinates (the overlay is fixed): start at the stage that
    // was clicked, clamped so the whole panel stays on screen.
    var place = function () {
      var viewH = window.innerHeight || document.documentElement.clientHeight;
      var anchor = trigger ? trigger.getBoundingClientRect().top - 8 : 12;
      panel.style.top = Math.max(12, Math.min(anchor, viewH - panel.offsetHeight - 12)) + "px";
    };
    place();
    img.addEventListener("load", place);          // full-size art changes the height
    requestAnimationFrame(function () { overlay.classList.add("open"); });
    closer.focus({ preventScroll: true });
  }

  /* ---- media blocks (get_game_detail(media=True)) ---- */
  /* Spliced from apps_shared.py, verbatim in the evaluation card too. */
"""
    + apps_shared.HERO_MEDIA_JS
    + r"""
  /* The trailer and the screenshots are one reel, shown the way a store page
     shows them: a single 16:9 stage plus one thumb strip, trailer first.
     Clicking a thumb swaps the stage in place; clicking a screenshot IN the
     stage opens the lightbox. */
"""
    + apps_shared.MEDIA_PANEL_JS
    + r"""
  /* The studio behind the game and what it shipped BEFORE it — server-fetched
     and library-annotated (tools/game_media.py). Under the big-studio damper,
     or with nothing released earlier, only the header line renders: six
     arbitrary posters out of a 500-game catalogue say nothing about this game. */
"""
    + apps_shared.PEDIGREE_JS
    + r"""
  /* ---------- detail: the big card ---------- */
  /* get_game_detail's `enrichment` is {provider: reason} for providers that
     were skipped for a structural reason — said in words, never as ids. */
  /* In plain words, one sentence: what is missing and why ("No Steam page
     for this game, so Steam reviews and ProtonDB are unavailable"). The
     providers sharing a reason are named together; a reason that names its
     own provider ("No IGDB match for this game") needs no "so" clause. */
  var ENRICH_SOURCES = { steam_store: "Steam", protondb: "ProtonDB", igdb: "IGDB" };
  var ENRICH_MISSING = { steam_store: "Steam reviews", protondb: "ProtonDB", igdb: "IGDB details" };
  var ENRICH_REASONS = {
    no_steam_appid: "No Steam page for this game",
    no_steam_platform_row: "Not owned on Steam",
    unconfigured: "{src} is not configured",
    no_match: "No {src} match for this game",
    unresolved: "{src} has not matched this game yet",
    link_pending: "{src} is still linking this game",
    failed: "The {src} lookup failed",
  };
  function andList(items) {
    return items.length < 2 ? items.join("") : items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }
  function enrichmentReasons(why) {
    if (!why || typeof why !== "object") return [];
    var groups = [];
    var byCause = {};
    Object.keys(why).forEach(function (k) {
      var source = ENRICH_SOURCES[k] || label("provider", k);
      var known = Object.prototype.hasOwnProperty.call(ENRICH_REASONS, why[k]);
      var template = known ? ENRICH_REASONS[why[k]] : "{src}: " + humanize(why[k]).toLowerCase();
      var cause = template.replace("{src}", source);
      if (!byCause[cause]) {
        byCause[cause] = { cause: cause, named: template.indexOf("{src}") >= 0, missing: [] };
        groups.push(byCause[cause]);
      }
      byCause[cause].missing.push(ENRICH_MISSING[k] || source);
    });
    return groups.map(function (g) {
      if (g.named) return g.cause;
      return g.cause + ", so " + andList(g.missing) + (g.missing.length > 1 ? " are" : " is") + " unavailable";
    });
  }
  /* Mid-sentence, a clause opening on a plain word drops its capital; a
     provider name ("IGDB", "ProtonDB") keeps its own. */
  function midSentence(text) {
    return /^(No|Not|The) /.test(text) ? text.charAt(0).toLowerCase() + text.slice(1) : text;
  }
  function enrichmentSentence(reasons) {
    return reasons.map(function (r, i) { return i ? midSentence(r) : r; }).join("; ");
  }

  function myRating(game) {
    return game.my_rating ? num(game.my_rating.normalized_score) : null;
  }
  function ownedPlatforms(game) {
    return list(game.platforms).filter(function (p) { return p && p.owned; });
  }
  /* Detail tier: his rating (>=7 good, >=5 ok, else bad); unrated, the
     critic tier; else a common card. */
  function detailTier(game) {
    var mine = myRating(game);
    return mine != null ? ratingTier(mine) : criticTier(game);
  }
  /* "8 /10" YOUR RATING, else the lead critic (leadCritic) and its source. */
  function detailBadge(game) {
    var mine = myRating(game);
    var lead = mine == null ? leadCritic(game) : null;
    var badge = null;
    if (mine != null) {
      badge = badgeNode({ value: mine, suffix: "/10", tag: "Your rating", tier: ratingTier(mine) });
    } else if (lead) {
      badge = badgeNode({ value: lead.value, tag: lead.source, tier: lead.tier });
    }
    if (badge) badge.classList.add("badge-on-art");
    return badge;
  }
  /* The art-edge ribbon: his completion status in STATUS_CHIPS's words and
     tier (with his hours as the note), "Unplayed" when an owned game's hours
     are a measured zero, and nothing when the state is unknown (null hours
     say nothing). */
  function statusRibbon(game) {
    var hours = num(game.playtime_hours);
    var note = hours != null && hours > 0 ? hoursLabel(hours) : null;
    var status = Object.prototype.hasOwnProperty.call(STATUS_CHIPS, game.completion_status)
      ? STATUS_CHIPS[game.completion_status] : null;
    if (status) return ribbonNode(status[0], status[1], note, "art");
    if (hours === 0 && game.owned) return ribbonNode("Unplayed", "none", null, "art");
    return null;
  }
  /* PAID: the first owned platform row with a known price — "Free" for a
     zero, the purchase source (its article dropped) as the note. */
  function paidRow(game) {
    var row = ownedPlatforms(game).filter(function (p) { return num(p.price_paid) != null; })[0];
    if (!row) return null;
    var amount = num(row.price_paid);
    var source = row.purchase_source ? label("purchase_source", row.purchase_source) : "";
    return statRow({
      label: "Paid", note: source.replace(/^(a|an)\s+/i, "") || null,
      value: amount === 0 ? "Free" : money(amount, row.price_currency),
    });
  }
  /* The score chips (all four: chipRow's limit is 4 here): Metacritic,
     OpenCritic, Steam (phrase + meter), ProtonDB; each links out via the
     host when a URL is known or derivable. Time to beat is the LENGTH row
     above them. */
  function detailChips(game) {
    var appid = game.steam_appid != null ? game.steam_appid : game.appid;
    var chips = [];
    if (realScore(game.metacritic_score)) {
      chips.push(scoreChip({
        label: "Metacritic", value: Math.round(game.metacritic_score),
        tier: mcTier(game.metacritic_score), url: game.metacritic_url,
      }));
    }
    if (realScore(game.opencritic_score)) {
      chips.push(scoreChip({
        label: "OpenCritic", value: Math.round(game.opencritic_score),
        tier: ocTier(game.opencritic_score, game.opencritic_tier), url: game.opencritic_url,
      }));
    }
    chips.push(steamChip(game.steam_review_desc,
      appid != null ? "https://store.steampowered.com/app/" + appid + "/" : null));
    if (game.protondb_tier) {
      chips.push(scoreChip({
        label: "ProtonDB", value: label("tier", game.protondb_tier),
        tier: protonTier(game.protondb_tier), title: "ProtonDB: how well it runs on Linux / Steam Deck",
        url: appid != null ? "https://www.protondb.com/app/" + appid : null,
      }));
    }
    return chipRow(chips, 4);
  }
  /* Time to beat: the three HowLongToBeat lengths as their own rows when the
     longer two are known (this card's stat block may then reach seven rows);
     with only the main story, the one LENGTH row. */
  function lengthRows(game) {
    var main = hoursLabel(game.hltb_main, true);
    var extra = hoursLabel(game.hltb_extra, true);
    var complete = hoursLabel(game.hltb_complete, true);
    if (!extra && !complete) {
      return main ? [statRow({ label: "Length", note: "main story", value: main })] : [];
    }
    return [["Main story", main], ["Main + extras", extra], ["Completionist", complete]]
      .filter(function (l) { return l[1]; })
      .map(function (l) { return statRow({ label: l[0], value: l[1] }); });
  }
  function detailStats(game) {
    var stats = el("div", "stats");
    var hours = num(game.playtime_hours);
    if (hours != null && hours > 0) {
      stats.appendChild(statRow({ label: "Played", value: hoursLabel(hours), key: true }));
    }
    var last = monthYear(game.last_played_date);
    if (last) stats.appendChild(statRow({ label: "Last", value: last }));
    lengthRows(game).forEach(function (row) { stats.appendChild(row); });
    var paid = paidRow(game);
    if (paid) stats.appendChild(paid);
    var chips = detailChips(game);
    if (chips) stats.appendChild(chips);
    var mine = myRating(game);
    if (mine != null) {
      var tier = ratingTier(mine);
      var rating = statRow({ label: "Rating", value: pipsNode(mine, 10, tier) });
      rating.classList.add("tier-" + tier);
      stats.appendChild(rating);
    }
    return stats.childNodes.length ? stats : null;
  }

  /* The big card: frame (tier = his rating), grain, the cover at full card
     width with the badge and the status ribbon, the plate, the stat block.
     Returns {frame, filled}: filled is false for a never-enriched row. */
  function identityPanel(game, media) {
    var frame = frameNode("article", "dt-card", detailTier(game));
    var art = el("div", "art");
    art.appendChild(coverNode(game));
    var badge = detailBadge(game);
    if (badge) art.appendChild(badge);
    var ribbon = statusRibbon(game);
    if (ribbon) art.appendChild(ribbon);
    frame.appendChild(art);

    var plate = el("div", "plate");
    var row = el("div", "plate-row");
    row.appendChild(el("h2", "plate-title", game.name));
    var no = cardNo(game.game_id);
    if (no) row.appendChild(el("span", "card-no", no));
    plate.appendChild(row);
    var pName = parentName(game);
    if (pName) plate.appendChild(el("div", "parent-sub", "part of " + pName));
    // Separate spans, never a middot string: studio, year, platforms.
    var sub = el("div", "sub");
    var studio = leadStudio(game.pedigree);
    if (studio) sub.appendChild(el("span", null, studio));
    if (game.release_date) sub.appendChild(el("span", null, String(game.release_date).slice(0, 4)));
    ownedPlatforms(game).forEach(function (p) {
      sub.appendChild(el("span", "loz", label("platform_short", p.platform)));
    });
    var type = typeLoz(game);
    if (type) sub.appendChild(type);
    if (game.wishlisted && !game.owned) sub.appendChild(el("span", null, "Wishlisted"));
    if (sub.childNodes.length) plate.appendChild(sub);
    frame.appendChild(plate);

    var stats = detailStats(game);
    if (stats) frame.appendChild(stats);
    return { frame: frame, filled: !!stats || sub.childNodes.length > 0 || !!media.short_description };
  }

  /* ---------- detail: the ground under the card ---------- */
  /* The Game Awards from `features` ("the game awards - best narrative -
     nominee"): one line for the wins, one for the nominations (three names
     at most, the headline categories first); nothing when there are none. */
  var AWARD_RE = /^the game awards - (.+) - (winner|nominee)$/i;
  var AWARD_SMALL_WORDS = ["of", "the", "and", "for", "in", "a", "an", "to", "on"];
  var AWARD_FIRST = ["game of the year", "best game direction", "best narrative"];
  function awardName(raw) {
    return String(raw).split(/\s+/).map(function (w, i) {
      if (i > 0 && AWARD_SMALL_WORDS.indexOf(w) >= 0) return w;
      return w.split("-").map(function (p) { return p.charAt(0).toUpperCase() + p.slice(1); }).join("-");
    }).join(" ");
  }
  function awardRank(c) {
    var i = AWARD_FIRST.indexOf(c);
    return i < 0 ? AWARD_FIRST.length : i;
  }
  function awardLines(features) {
    var won = [], nominated = [];
    list(features).forEach(function (f) {
      var m = AWARD_RE.exec(String(f || "").trim());
      if (!m) return;
      var category = m[1].trim().toLowerCase().replace(/\s+/g, " ");
      (m[2].toLowerCase() === "winner" ? won : nominated).push(category);
    });
    nominated = nominated.filter(function (c) { return won.indexOf(c) < 0; });
    function names(cats) {
      return cats.map(function (c, i) { return [awardRank(c), i, c]; })
        .sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; })
        .slice(0, 3).map(function (r) { return awardName(r[2]); });
    }
    var lines = [];
    if (won.length) {
      var w = names(won);
      var listed = w.length > 1 ? w.slice(0, -1).join(", ") + " and " + w[w.length - 1] : w[0];
      lines.push("The Game Awards — " + listed + ", winner");
    }
    if (nominated.length) lines.push("Nominee for " + names(nominated).join(", "));
    return lines;
  }
  function tagLine(tags, cls) {
    var line = el("div", "tagline" + (cls ? " " + cls : ""));
    tags.forEach(function (t) { line.appendChild(el("span", null, t)); });
    return line;
  }

  var descSeq = 0;
  function groundNode(game, media) {
    var flow = el("div", "dt-flow");
    // media.short_description is the same kind of blurb as the stored one and
    // often literally identical — render one, preferring the library's own.
    var description = game.short_description || media.short_description;
    if (description) {
      var desc = el("p", "desc", description);
      desc.id = "desc-" + (++descSeq);
      flow.appendChild(desc);
      var more = el("button", "more-toggle", "More");
      more.type = "button";
      more.hidden = true;                         // only when the clamp bites
      more.setAttribute("aria-controls", desc.id);
      more.setAttribute("aria-expanded", "false");
      more.addEventListener("click", function () {
        var open = desc.classList.toggle("open");
        more.textContent = open ? "Less" : "More";
        more.setAttribute("aria-expanded", open ? "true" : "false");
        reportSize();
      });
      flow.appendChild(more);
      requestAnimationFrame(function () {
        if (desc.scrollHeight > desc.clientHeight + 1) {
          more.hidden = false;
          reportSize();
        }
      });
    }
    // His own review, as the card's quote.
    var review = game.my_rating && game.my_rating.review_text;
    if (review) flow.appendChild(flavorNode(String(review), true));
    var awards = awardLines(game.features);
    if (awards.length) {
      var abil = el("div", "dt-abil");
      awards.forEach(function (text) { abil.appendChild(abilityNode("Award", text)); });
      flow.appendChild(abil);
    }
    // One line of what it is: the genres first, then the community tags.
    var tags = list(game.genres).concat(list(game.tags)).filter(function (t, i, all) {
      return t && all.indexOf(t) === i;
    }).slice(0, 8);
    if (tags.length) flow.appendChild(tagLine(tags));
    return flow;
  }

  /* ---------- detail: library + studio strips ---------- */
  /* A mini's tier is his rating of that game (none when unrated); its lines
     are the shared miniLines format: pips, then "50h" "played" (or
     "unplayed"), then the year. similar_in_library carries no completion
     status or platform, so none is passed. */
  function similarStrip(parent, similar) {
    var items = list(similar && similar.items).filter(function (i) { return i && i.name; }).slice(0, 8);
    if (!items.length) return;
    var sec = eyebrowSection(parent, "In your library");
    var strip = el("div", "strip ministrip");
    items.forEach(function (item) {
      var rating = num(item.my_rating);
      var why = list(item.shared_tags).filter(Boolean);
      strip.appendChild(miniCard({
        name: item.name, cover_url: item.cover_url,
        tier: rating != null ? ratingTier(rating) : "none",
        title: why.length ? "Shares: " + why.join(", ") : null,
        lines: miniLines({ rating: rating, hours: item.playtime_hours, unplayed: item.unplayed,
          year: item.release_year }),
      }));
    });
    sec.appendChild(strip);
  }
  /* The store page, when one is derivable (Steam, from the app id): the
     shared store pill. */
  function storeLink(game) {
    var appid = game.steam_appid != null ? game.steam_appid : game.appid;
    if (appid == null) return null;
    return storePill("https://store.steampowered.com/app/" + appid + "/", "Open on Steam");
  }

  function detailCard(game) {
    var stack = el("div", "detail-stack");
    var media = game.media || {};
    // Who the game is first (the card), then what it is like, how it looks,
    // where it sits in his library, and what to do next.
    var panel = identityPanel(game, media);
    stack.appendChild(panel.frame);
    dealIn(panel.frame, 0);
    var flow = groundNode(game, media);
    // A never-enriched row (an assessment-minted candidate, a fresh wishlist
    // entry) fills nothing but the title, which renders as a card that looks
    // broken. Say so, and — when the response explained which providers were
    // skipped and why (get_game_detail's `enrichment`) — say that too. The
    // notice is bookkeeping, not copy: it reads last, just before the
    // actions row, never between the description and the strips.
    var reasons = enrichmentReasons(game.enrichment);
    var noticeText = null;
    if (!panel.filled && !flow.childNodes.length) {
      var emptyText = "No details fetched yet";
      if (reasons.length) emptyText += ": " + midSentence(enrichmentSentence(reasons));
      noticeText = emptyText;
    } else if (reasons.length) {
      noticeText = enrichmentSentence(reasons);
    }
    var count = flow.childNodes.length;
    mediaNode(flow, media, game.name);
    if (flow.childNodes.length > count) {
      var reel = flow.childNodes[count];
      reel.classList.add("reel");
      var reelTitle = reel.querySelector(".section-title");
      if (reelTitle) reelTitle.classList.add("sr-only");
    }
    similarStrip(flow, game.similar);
    // FROM THE STUDIO (the shared builder): the headline, then the studio's
    // releases around this one. The lengths, genres, studio and publisher
    // all sit on the card or here, so the actions row is the store link alone.
    studioStrip(flow, game.pedigree, game.release_date ? String(game.release_date).slice(0, 4) : null);
    var actions = el("div", "actions dt-actions");
    flow.appendChild(actions);
    if (noticeText) flow.insertBefore(notice(flow, noticeText), actions);
    var store = storeLink(game);
    if (store) actions.appendChild(store);
    if (!actions.childNodes.length) flow.removeChild(actions);
    stack.appendChild(flow);
    return stack;
  }

  /* Neutral until the tool input arrives; then get_game_detail sketches the
     detail card and every list tool the grid. The tool's name decides when
     the host says it; otherwise the arguments do — get_game_detail is called
     with a name / game_id / appid, discover_games never is. */
  function skeletonKind() {
    if (!lastToolInput) return "neutral";
    var tool = toolName();
    if (tool) return tool === "get_game_detail" ? "detail" : "grid";
    var args = lastToolInput;
    return args.name || args.game_id != null || args.appid != null ? "detail" : "grid";
  }

  /* M5 for a whole view: resolveSkeleton swaps it in for the skeleton and
     fades the skeleton out; the view's own cards carry the deal-in (each
     grid card, the detail frame), so the wrapper itself is not dealt. */
  function gcResolve(skel, node) {
    resolveSkeleton(skel, function () { return node; });
    node.classList.remove("deal");
    return node;
  }

  function render(data) {
    drillSeq++;
    // The startup / tool-input skeleton, when it is all that is on screen.
    var only = root.children.length === 1 ? root.firstElementChild : null;
    var skel = only && only.classList.contains("skel") ? only : null;
    var node = null;
    if (data && Array.isArray(data.results)) {
      view = "grid";
      gridData = data;
      node = renderGrid(data);
    } else if (data && data.name) {
      view = "detail";
      node = detailCard(data);
    }
    if (!node) {
      view = "none";
      root.textContent = "";
      root.appendChild(el("div", "empty", "Nothing to display."));
    } else if (skel) {
      gcResolve(skel, node);
    } else {
      root.textContent = "";
      root.appendChild(node);
    }
    reportSize();
  }

"""
    + apps_shared.SIZING_JS
    + apps_shared.INIT_JS
    + r"""
  /* Before the result, the input swaps the neutral skeleton for the shape
     of the tool that ran. It can also land after the result: the grid's
     header line and its "Show next" count are built from it, so redraw the
     grid when it does. */
  hooks.afterToolInput = function () {
    if (!gotResult) showSkeleton();
    else if (view === "grid" && gridData) render(gridData);
  };
  hooks.afterHostContext = hostContextChanged;

  // Preview only: the arguments the preview script called the tool with, so
  // the header line can be judged offline.
  if (window.__PREVIEW_TOOL_INPUT__) lastToolInput = window.__PREVIEW_TOOL_INPUT__;
  startWidget("gamelib-game-cards");
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
