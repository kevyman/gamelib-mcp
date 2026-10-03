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

Grid mode (spec 2026-10-03 §2.1): a header line says what the grid IS
("12 of 143 games · sorted by taste match · roguelike · unplayed only"),
rebuilt from the tool-input arguments whenever they arrive. Each card leads
with the taste-match bar under the title, then one meta line, one chip row
and the matched tags as plain text. The footer carries at most two actions:
"Show next N" (a chat message asking for the next page) and "Expand" (the
host's fullscreen mode, where the grid widens and the header sticks).

Tapping a card hands control back to the conversation instead of opening an
in-widget overlay: the selection always goes into the model context
(ui/update-model-context); then, on a host that offers fullscreen, the widget
goes fullscreen and drills into a live ``get_game_detail(media=True)`` call
(app-initiated ``tools/call``) behind a "Back to results" bar; elsewhere it
posts "Show me <name>" as the user.

Detail mode leads with the identity panel (cover, title, one sub line, one
chip row, a 3-line description, tags as text), then the media reel (one 16:9
viewer plus one thumb strip, trailer first, screenshots opening a lightbox),
then "Similar games you own · From the studio" — fullscreen on a host that
has it, an in-place disclosure otherwise. Every score renders through the one
shared chip (apps_shared.SCORE_CHIP_JS): color is the quality tier only and
the source is the label text. The media, similar and studio blocks live in
apps_shared.py and are spliced verbatim into both this widget and the
evaluation card (apps_eval.py).

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
  /* The cover carries three things and only three: the rank badge (top
     left), the Metacritic chip (top right, the shared chip, tier-colored)
     and the nested-content type chip (bottom left). */
  .chip.corner { position: absolute; top: 6px; right: 6px; z-index: 1; }

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
  .cover-wrap .type-chip { position: absolute; left: 6px; bottom: 6px; z-index: 1; padding: 2px 7px; }
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
  /* Matched tags (grid) and the game's tags (detail): one muted text line,
     " · "-joined — words, not a row of pills. */
  .tagline {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  /* ---- grid mode ---- */
  /* What the grid is: "12 of 143 games · sorted by taste match · …". */
  .grid-head {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    font-variant-numeric: tabular-nums;
    margin-bottom: 10px;
  }
  .grid-head b { color: var(--gl-text); font-weight: var(--gl-strong); }
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
     (renderGrid() adds .ranked and seeds the counter from the payload offset).
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

  /* Body order: title → match bar (the lead signal) → meta → chips → tags. */
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
  .card .match { margin: 2px 0; }
  .meta {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-text-2);
    font-variant-numeric: tabular-nums;
  }
  .card .chips { gap: 4px; }
  /* A 150px card cannot hold label + meter + "Very positive" on one line, and
     the folded three-line chip read as a block. So the grid's Steam chip is
     label and phrase only (the meter stays on the detail card), and a
     two-word phrase gets a line of its own. */
  .card .chip { padding: 3px 6px; }
  .card .chip.steam-line { flex-basis: 100%; display: block; }
  /* Text flow, not flex: a phrase too long for the line breaks between its
     words ("Steam Overwhelmingly / positive") instead of dropping the whole
     value onto lines of its own. */
  .card .chip.steam-line .lbl { margin-right: 4px; }

  /* At most two actions, bottom right; stacked full width on a phone. */
  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 12px;
  }
  .btn[disabled] { cursor: default; color: var(--gl-muted); border-color: var(--gl-border); }
  html[data-display-mode="fullscreen"] .act-expand { display: none; }

  /* Fullscreen: wider cards, and the header line (or the drill-in's back
     bar) sticks to the top while the grid scrolls under it. */
  html[data-display-mode="fullscreen"] .grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
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
  .topbar .btn { flex: none; padding: 0 12px; }
  .topbar-title {
    min-width: 0;
    font-weight: var(--gl-strong);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  html[data-display-mode="fullscreen"] .detail-stack { margin: 0 auto; }

  /* ---- detail mode ---- */
  /* Identity panel: cover beside the title block; on a wide card the cover
     spans the whole panel, on a phone the chips and text run full width
     under a cover-and-title header. */
  .detail {
    display: grid;
    grid-template-columns: 120px minmax(0, 1fr);
    grid-template-areas: "cover head" "cover body";
    grid-template-rows: auto 1fr;
    column-gap: 16px;
    row-gap: 10px;
    background: var(--gl-surface);
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    box-shadow: var(--gl-shadow);
    padding: 14px;
  }
  .detail > .cover-wrap {
    grid-area: cover;
    align-self: start;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-sm);
    overflow: hidden;
  }
  /* On the detail card the h1 sits right beside the plate and already says
     the name, so the plate stamped with it reads as a duplicate title. The
     gradient stays (it is the art stand-in); only the lettering goes. Grid
     cards have no heading beside the cover and keep theirs. */
  .detail .cover-fallback { color: transparent; text-shadow: none; }
  .detail-head { grid-area: head; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .detail-body { grid-area: body; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .detail h1 {
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
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .desc.open { display: block; overflow: visible; }
  .more-toggle {
    position: relative;
    align-self: flex-start;
    margin-top: -6px;
    padding: 0;
    border: 0;
    background: none;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    color: var(--gl-text-2);
    text-decoration: underline;
    text-underline-offset: 2px;
    cursor: pointer;
  }
  .more-toggle[hidden] { display: none; }
  .more-toggle::after {
    content: "";
    position: absolute;
    left: 50%;
    top: 50%;
    width: max(100%, 32px);
    height: 32px;
    transform: translate(-50%, -50%);
  }
  html.touch .more-toggle::after { width: max(100%, 44px); height: 44px; }
  .detail .notice { text-align: left; padding: 0; }

  /* ---- detail media (get_game_detail(media=True)) ---- */
  /* The detail view is a column stack: the identity panel, the media reel,
     then the similar-games and studio rows. */
  .detail-stack { display: flex; flex-direction: column; gap: 12px; max-width: 720px; }
  .related { display: flex; flex-direction: column; gap: 12px; }
  .disclosure .chev-out { font-weight: var(--gl-regular); }

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
    + r"""  /* Carousel rows (similar, studio): 120px cards that snap card by card,
     the next one peeking past the edge. The snap padding comes from the
     host's safe-area insets and the shared strip's trailing spacer keeps the
     last card reachable. */
  .strip:not(.thumbs) {
    scroll-snap-type: x mandatory;
    overscroll-behavior-x: contain;
    scroll-padding-left: calc(2px + var(--gl-safe-left, 0px));
    scroll-padding-right: calc(2px + var(--gl-safe-right, 0px));
  }
  .sim { width: 120px; scroll-snap-align: start; }

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
  @media (min-width: 560px) {
    .detail h1 { font-size: var(--gl-title); line-height: var(--gl-title-lh); }
  }
  @media (max-width: 559px) {
    .detail {
      grid-template-columns: 84px minmax(0, 1fr);
      grid-template-areas: "cover head" "body body";
      column-gap: 12px;
    }
  }
  @media (max-width: 460px) {
    /* Narrow phone: smaller thumbs so more than one fits before scrolling. */
    .thumb img, .thumb-text { width: 96px; height: 55px; }
  }
  @media (max-width: 419px) {
    .actions { flex-direction: column; }
    .actions .btn { width: 100%; }
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

  /* ---------- view state ---------- */
  var view = "none";              // "grid" | "detail" | "drill" | "none"
  var gridData = null;            // the grid payload Back returns to
  var drillSeq = 0;               // invalidates a superseded detail fetch
  var modeBeforeDrill = "inline";
  var lastSelectedId = null;

  /* ---------- grid: header line ---------- */
  /* get_game_detail is called with a name / game_id / appid / items;
     discover_games never is. */
  function isDetailArgs(args) {
    return !!(args.name || args.game_id != null || args.appid != null || args.items);
  }
  var SORT_LABELS = { match: "taste match", critic: "critic score", value: "value" };
  function headerParts(data) {
    var shown = data.results.length;
    var total = num(data.total_matches);
    var count = total != null && total > shown
      ? shown + " of " + plural(total, "game")
      : plural(shown, "game");
    var parts = [];
    var args = lastToolInput;
    if (args && !isDetailArgs(args)) {
      parts.push("sorted by " + (SORT_LABELS[args.sort_by] || SORT_LABELS.match));
      var vibes = list(args.vibes).filter(Boolean);
      if (vibes.length) parts.push(vibes.join(" + "));
      // discover_games defaults unplayed_only to true: absent means on.
      if (args.unplayed_only !== false) parts.push("unplayed only");
      var maxHours = num(args.max_hltb_hours);
      if (maxHours != null) parts.push("≤ " + maxHours + "h");
      var minScore = num(args.min_score);
      if (minScore != null) parts.push("critics ≥ " + minScore);
      if (args.protondb_min_tier) parts.push("ProtonDB " + label("tier", args.protondb_min_tier) + "+");
    }
    return { count: count, parts: parts };
  }
  function headerLine(data) {
    var parts = headerParts(data);
    var head = el("div", "grid-head");
    head.appendChild(el("b", null, parts.count));
    if (parts.parts.length) head.appendChild(document.createTextNode(" · " + parts.parts.join(" · ")));
    return head;
  }

  /* ---------- grid: footer actions (at most two) ---------- */
  function gridActions(data) {
    var bar = el("div", "actions");
    var shown = data.results.length;
    if (data.has_more && shown) {
      var args = lastToolInput || {};
      var limit = num(args.limit) || shown;
      var offset = typeof data.offset === "number" ? data.offset : 0;
      var next = offset + shown;
      var total = num(data.total_matches);
      var count = total != null && total > next ? Math.min(limit, total - next) : limit;
      var more = el("button", "btn act-more", "Show next " + count);
      more.type = "button";
      more.addEventListener("click", function () {
        sendMessage("Show the next " + count + " recommendations (offset " + next + ")");
        more.disabled = true;
        more.textContent = "Asked for the next " + count;
      });
      bar.appendChild(more);
    }
    if (canFullscreen()) {
      var expand = el("button", "btn act-expand", "Expand");
      expand.type = "button";
      expand.addEventListener("click", function () { requestDisplayMode("fullscreen"); });
      bar.appendChild(expand);
    }
    return bar.childNodes.length ? bar : null;
  }

  /* ---------- grid: cards ---------- */
  /* Label and phrase only — no meter in a 150px card; a phrase of two or more
     words takes a line of its own (.steam-line) rather than folding. */
  function gridSteamChip(desc) {
    if (!desc) return null;
    var phrase = String(desc).toLowerCase();
    phrase = phrase.charAt(0).toUpperCase() + phrase.slice(1);
    var words = phrase.split(/\s+/).filter(Boolean).length;
    return scoreChip({
      label: "Steam", value: phrase, tier: steamTier(desc), title: "Steam reviews",
      cls: words >= 2 ? "steam-line" : "",
    });
  }

  function gridCard(game) {
    var card = el("div", "card");
    if (game.game_id != null) {
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("data-game-id", String(game.game_id));
      card.setAttribute("aria-label", "Show details for " + (game.name || "game"));
      card.addEventListener("click", function () { selectGame(game); });
      card.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectGame(game);
        }
      });
    }
    var cover = coverNode(game);
    // The cover chip is always Metacritic (the fullest-coverage source in
    // this library) so the corner never switches identity between sources;
    // the chip row below carries the others rather than repeating it.
    if (realScore(game.metacritic_score)) {
      cover.appendChild(scoreChip({
        label: "Metacritic", value: Math.round(game.metacritic_score),
        tier: mcTier(game.metacritic_score), cls: "corner",
      }));
    }
    var typeLabel = contentTypeLabel(game);
    if (typeLabel) cover.appendChild(el("span", "type-chip", typeLabel));
    card.appendChild(cover);

    var body = el("div", "card-body");
    body.appendChild(el("div", "title", game.name));
    var pName = parentName(game);
    if (pName) body.appendChild(el("div", "parent-sub", "⤷ " + pName));
    // The lead signal: how well this fits his taste.
    if (game.match_percent != null) body.appendChild(matchBar(game.match_percent));

    // One line of text, so a wrap breaks between words, never before a "·".
    var metaBits = [];
    var hltb = hoursLabel(game.hltb_main, true);
    if (hltb) metaBits.push(hltb);
    if (game.suggested_platform) metaBits.push(label("platform", game.suggested_platform));
    var played = num(game.playtime_hours) > 0 ? hoursLabel(game.playtime_hours) : null;
    if (played) metaBits.push(played + " played");
    if (metaBits.length) body.appendChild(el("div", "meta", metaBits.join(" · ")));

    var scores = el("div", "chips");
    if (realScore(game.opencritic_score)) {
      scores.appendChild(scoreChip({
        label: "OpenCritic", value: Math.round(game.opencritic_score),
        tier: ocTier(game.opencritic_score, game.opencritic_tier),
      }));
    }
    var steam = gridSteamChip(game.steam_review_desc);
    if (steam) scores.appendChild(steam);
    if (scores.childNodes.length) body.appendChild(scores);

    var why = matchedTagNames(game).slice(0, 3);
    if (why.length) body.appendChild(el("div", "tagline", why.join(" · ")));

    card.appendChild(body);
    return card;
  }

  function renderGrid(data) {
    root.appendChild(headerLine(data));
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
    var actions = gridActions(data);
    if (actions) root.appendChild(actions);
  }

  /* ---------- card tap: hand back to the conversation ---------- */
  /* The selection always reaches the model. Then: fullscreen + a live detail
     drill-in where the host offers fullscreen, else a chat message. */
  function selectGame(game) {
    lastSelectedId = game.game_id;
    updateModelContext(
      "User selected " + game.name + " (game_id " + game.game_id + ") from the recommendations",
      { game_id: game.game_id, name: game.name });
    if (!canFullscreen()) {
      sendMessage("Show me " + game.name);
      return;
    }
    var before = currentDisplayMode();
    requestDisplayMode("fullscreen").then(function (mode) {
      if (mode === "fullscreen") openDrill(game, before);
      else sendMessage("Show me " + game.name);  // the host said no after all
    });
  }

  function openDrill(game, before) {
    var seq = ++drillSeq;
    modeBeforeDrill = before;
    view = "drill";
    root.textContent = "";
    var bar = el("div", "topbar");
    var back = el("button", "btn", "← Back to results");
    back.type = "button";
    back.addEventListener("click", backToResults);
    bar.appendChild(back);
    bar.appendChild(el("div", "topbar-title", game.name));
    root.appendChild(bar);
    var holder = el("div");
    holder.appendChild(skeleton("detail"));
    root.appendChild(holder);
    window.scrollTo(0, 0);
    back.focus({ preventScroll: true });
    reportSize();

    function fill(data, failed) {
      if (seq !== drillSeq) return;               // Back, or another tap, won
      holder.textContent = "";
      holder.appendChild(detailCard(data));
      if (failed) notice(holder, "Couldn't load the full details — showing what the list had.");
      reportSize();
    }
    if (window.__PREVIEW_DATA__) { fill(game, false); return; }
    // media:true is what turns the card into the full game representation —
    // trailer, screenshots, the owned games most like it; the grid payload
    // carries none of that. 30s, not callTool's 15s default: a cold
    // click-through runs the full lazy enrichment AND the media lookup's own
    // 8s budget server-side, and a response that loses the race is dropped.
    callTool("get_game_detail", { game_id: game.game_id, media: true }, 30000).then(function (res) {
      var data = resultData(res);
      if (data && data.name) fill(data, false);
      else fill(game, true);                      // declined or timed out
    });
  }

  function backToResults() {
    var restore = modeBeforeDrill;
    if (gridData) render(gridData);
    if (restore !== "fullscreen") requestDisplayMode("inline");
    var cards = root.querySelectorAll(".card");
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
    + r"""
  /* ---------- screenshot lightbox ---------- */
  /* The detail card's own media viewer (not navigation): one slot, fixed over
     the iframe, closed by ✕, Escape or the backdrop; Tab stays inside it and
     focus returns to the stage that opened it. */
  var lightbox = null;
  function closeLightbox() {
    var current = lightbox;
    if (!current) return;
    lightbox = null;
    document.removeEventListener("keydown", current.onKey, true);
    current.overlay.classList.remove("open");
    setTimeout(function () { current.overlay.remove(); }, 200);
    if (current.trigger && current.trigger.focus) current.trigger.focus({ preventScroll: true });
  }
  function keepFocusInside(ev, panel) {
    var buttons = panel.querySelectorAll("button");
    if (!buttons.length) return;
    var first = buttons[0];
    var last = buttons[buttons.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }
  function openCarousel(shots, startIndex, gameName, trigger) {
    closeLightbox();
    var index = startIndex;
    var overlay = el("div", "overlay lightbox");
    var panel = el("div", "lightbox-panel carousel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", (gameName ? gameName + " " : "") + "screenshots");
    panel.tabIndex = -1;
    var closer = el("button", "overlay-close", "✕");
    closer.type = "button";
    closer.setAttribute("aria-label", "Close screenshots");
    closer.addEventListener("click", closeLightbox);
    panel.appendChild(closer);

"""
    + apps_shared.CAROUSEL_STAGE_JS
    + r"""
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeLightbox();
    });
    var onKey = function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); closeLightbox(); }
      else if (ev.key === "ArrowLeft") show(index - 1);
      else if (ev.key === "ArrowRight") show(index + 1);
      else if (ev.key === "Tab") keepFocusInside(ev, panel);
    };
    document.addEventListener("keydown", onKey, true);
    lightbox = { overlay: overlay, trigger: trigger, onKey: onKey };
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
  /* ---------- detail: identity panel ---------- */
  /* get_game_detail's `enrichment` is {provider: reason} for providers that
     were skipped for a structural reason — said in words, never as ids. */
  var ENRICH_SOURCES = { steam_store: "Steam", protondb: "ProtonDB", igdb: "IGDB" };
  var ENRICH_REASONS = {
    no_steam_appid: "no app id",
    no_steam_platform_row: "not owned on Steam",
    unconfigured: "not configured",
    no_match: "no match",
    unresolved: "unresolved",
    link_pending: "still linking",
    failed: "lookup failed",
  };
  function enrichmentReasons(why) {
    if (!why || typeof why !== "object") return [];
    return Object.keys(why).map(function (k) {
      var source = ENRICH_SOURCES[k] || label("provider", k);
      var reason = ENRICH_REASONS[why[k]] || humanize(why[k]).toLowerCase();
      return source + ": " + reason;
    });
  }

  var descSeq = 0;
  function identityPanel(game, media) {
    var box = el("div", "detail");
    box.appendChild(coverNode(game));

    var head = el("div", "detail-head");
    head.appendChild(el("h1", null, game.name));
    var pName = parentName(game);
    if (pName) head.appendChild(el("div", "sub parent-sub", "part of " + pName));
    var subBits = [];
    if (game.release_date) subBits.push(String(game.release_date).slice(0, 4));
    var owned = (game.platforms || []).filter(function (p) { return p.owned; })
      .map(function (p) { return label("platform", p.platform); });
    if (owned.length) subBits.push(owned.join(", "));
    var played = num(game.playtime_hours) > 0 ? hoursLabel(game.playtime_hours) : null;
    if (played) subBits.push(played + " played");
    if (game.wishlisted && !game.owned) subBits.push("wishlisted");
    if (subBits.length) head.appendChild(el("div", "sub", subBits.join(" · ")));
    box.appendChild(head);

    var body = el("div", "detail-body");
    // Metacritic leads (it's the grid's cover-chip source); every chip links
    // out to its source page via the host when a URL is known or derivable.
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
    var mine = game.my_rating ? num(game.my_rating.normalized_score) : null;
    if (mine != null) {
      badges.appendChild(scoreChip({
        label: "Your rating", value: mine + "/10",
        tier: mine >= 7 ? "good" : mine >= 5 ? "ok" : "bad",
      }));
    }
    if (badges.childNodes.length) body.appendChild(badges);

    // media.short_description is the same kind of blurb as the stored one and
    // often literally identical — render one, preferring the library's own.
    var description = game.short_description || media.short_description;
    if (description) {
      var desc = el("p", "desc", description);
      desc.id = "desc-" + (++descSeq);
      body.appendChild(desc);
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
      body.appendChild(more);
      requestAnimationFrame(function () {
        if (desc.scrollHeight > desc.clientHeight + 1) {
          more.hidden = false;
          reportSize();
        }
      });
    }

    var tags = (game.tags || []).filter(Boolean).slice(0, 8);
    if (tags.length) body.appendChild(el("div", "tagline", tags.join(" · ")));

    // A never-enriched row (an assessment-minted candidate, a fresh wishlist
    // entry) fills nothing but the title, which renders as a card that looks
    // broken. Say so, and — when the response explained which providers were
    // skipped and why (get_game_detail's `enrichment`) — say that too.
    var reasons = enrichmentReasons(game.enrichment);
    var filled = head.childNodes.length > 1 || body.childNodes.length > 0;
    if (!filled) {
      var emptyText = "No details fetched yet";
      if (reasons.length) emptyText += " — " + reasons.join(" · ");
      notice(body, emptyText);
    } else if (reasons.length) {
      notice(body, "Not fetched — " + reasons.join(" · "));
    }
    box.appendChild(body);
    return box;
  }

  /* ---------- detail: similar + studio, behind one control ---------- */
  function hasStudio(ped) {
    if (!ped) return false;
    if (pedigreeHeadline(ped)) return true;
    return list(ped.previous_games).some(function (i) { return i && i.name; });
  }
  function relatedBlock(stack, game) {
    var similar = !!(game.similar && list(game.similar.items).some(function (i) { return i && i.name; }));
    var studio = hasStudio(game.pedigree);
    if (!similar && !studio) return;
    var build = function (parent) {
      if (similar) similarNode(parent, game.similar);
      if (studio) pedigreeNode(parent, game.pedigree);
      reportSize();
    };
    var text = [similar ? "Similar games you own" : null, studio ? "From the studio" : null]
      .filter(Boolean).join(" · ");
    // Already fullscreen: everything expanded, no control at all.
    if (currentDisplayMode() === "fullscreen") { build(stack); return; }
    // No fullscreen on this host: the in-place disclosure.
    if (!canFullscreen()) { disclosure(stack, text, build); return; }
    // A fullscreen host: the control opens the big view with the rows
    // expanded; if the host refuses after all, it opens in place instead.
    var wrap = el("div", "related");
    var btn = el("button", "disclosure");
    btn.type = "button";
    btn.appendChild(el("span", null, text));
    var icon = el("span", "chev-out", "⤢");
    icon.setAttribute("aria-hidden", "true");
    btn.appendChild(icon);
    btn.addEventListener("click", function () {
      requestDisplayMode("fullscreen").then(function (mode) {
        wrap.textContent = "";
        if (mode === "fullscreen") build(wrap);
        else disclosure(wrap, text, build).button.click();
      });
    });
    wrap.appendChild(btn);
    stack.appendChild(wrap);
  }

  function detailCard(game) {
    var stack = el("div", "detail-stack");
    var media = game.media || {};
    // Who the game is first, then how it looks, then what it is like.
    stack.appendChild(identityPanel(game, media));
    mediaNode(stack, media, game.name);
    relatedBlock(stack, game);
    return stack;
  }

  /* get_game_detail is called with a name / game_id / appid; discover_games
     never is — so the arguments tell us which shape to sketch. */
  function skeletonKind() {
    var args = lastToolInput || {};
    return args.name || args.game_id != null || args.appid != null ? "detail" : "grid";
  }

  function render(data) {
    drillSeq++;
    root.textContent = "";
    if (data && Array.isArray(data.results)) {
      view = "grid";
      gridData = data;
      renderGrid(data);
    } else if (data && data.name) {
      view = "detail";
      root.appendChild(detailCard(data));
    } else {
      view = "none";
      root.appendChild(el("div", "empty", "Nothing to display."));
    }
    reportSize();
  }

"""
    + apps_shared.SIZING_JS
    + apps_shared.INIT_JS
    + r"""
  /* Tool input can land after the result: the grid's header line and its
     "Show next" count are built from it, so redraw the grid when it does. */
  var baseToolInput = handleToolInput;
  handleToolInput = function (params) {
    baseToolInput(params);
    if (gotResult && view === "grid" && gridData) render(gridData);
  };
  /* The host leaving fullscreen (its own close button) ends a drill-in. */
  var baseApplyHostContext = applyHostContext;
  applyHostContext = function (ctx) {
    baseApplyHostContext(ctx);
    if (view === "drill" && currentDisplayMode() !== "fullscreen" && gridData) render(gridData);
  };

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
