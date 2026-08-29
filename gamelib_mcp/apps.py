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

A detail card whose payload carries ``media`` (get_game_detail(media=True) —
which is what that upgrade call asks for) also renders the neutral game
representation: a hero trailer, a screenshot strip with a click-to-enlarge
lightbox, a similar-games row annotated with what he owns, and a "From the
studio" strip (the developer, and their previous games against his
library — header line alone for a studio too big for six posters to describe).
The hero and
lightbox mirror the evaluation card's implementations (apps_eval.py) rather
than sharing code with it: each widget is one self-contained HTML resource
with no build step and no CDN, so that duplication is deliberate — change a
media pattern in one and port it to the other.

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

# Cover art (see tools/common.py cover_url) plus the media hosts a detail card
# needs once get_game_detail(media=True) answers with a trailer and
# screenshots. Per the MCP Apps spec, resource_domains feeds img-src, media-src,
# script-src, style-src and font-src in the host's iframe CSP, while
# frame_domains feeds frame-src — which is what the lazy youtube-nocookie
# embed rides on. Covers: IGDB art, Steam capsules and the constructed mp4
# renditions (cdn.*), Steam screenshots and movie posters (shared.*, where
# appdetails actually serves them), and YouTube thumbnails for IGDB trailers.
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
    ],
    frame_domains=["https://www.youtube-nocookie.com"],
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

  /* Nested-content chip (DLC/expansion/bundle/edition/add-on) — a quiet
     identity tag, not a rating, so it stays out of the .pill/.mini rotation
     and always renders the same color. */
  .type-chip {
    align-self: flex-start;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.02em;
    padding: 2px 7px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--p1);
    color: var(--ink);
  }
  /* Subtle "part of <base game>" line under the title on nested rows. */
  .parent-sub {
    font-size: 11px;
    font-weight: 650;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

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
  a.badge { text-decoration: none; color: inherit; }
  .badge[data-link] { cursor: pointer; transition: transform 0.1s ease, box-shadow 0.1s ease; }
  .badge[data-link]:hover, .badge[data-link]:focus-visible {
    transform: translate(-1px, -1px);
    box-shadow: 2px 2px 0 var(--shadow-c);
  }
  .badge .ext { font-size: 9px; margin-left: 3px; opacity: 0.65; }
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
  /* Nested-content badge — always the same quiet color, regardless of
     position among the other (position-cycled) badges. */
  .badges .badge.content-badge { background: var(--p1); }
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

  /* ---- detail media (get_game_detail(media=True)) ---- */
  /* The detail card becomes a column stack: hero trailer, the card itself,
     then the screenshot and similar-games panels. Every block keeps its own
     border so the stack still reads as toybox parts, not one long sheet. */
  .detail-stack { display: flex; flex-direction: column; gap: 14px; max-width: 720px; }
  .detail-stack .detail { max-width: none; }
  .panel {
    background: var(--card);
    border: 2px solid var(--ink);
    border-radius: 14px;
    box-shadow: 5px 5px 0 var(--shadow-c);
    padding: 14px 16px;
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

  /* Hero trailer — mp4 with a poster fallback, or a click-to-load embed. */
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

  /* Screenshot / similar strips: scroll sideways rather than wrap, so a
     narrow phone card never grows a second row of thumbnails. */
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

  /* Similar games: mini cover cards with ownership stickers. */
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

  /* ---- click-to-expand overlays (detail card, screenshot lightbox) ---- */
  /* Anchored near the clicked card rather than centered in the (possibly
     very tall) iframe — hosts that don't auto-scroll to modals would
     otherwise open it off-screen. JS sets the panel's top and the overlay's
     height to span the whole document. */
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
    max-width: 720px;
    border-radius: 16px;
    transform: translateX(-50%) scale(0.93) translateY(12px);
    transition: transform 0.19s ease;
  }
  .overlay.open .overlay-panel { transform: translateX(-50%); }
  .overlay-panel .detail-stack { max-width: none; }
  .overlay-panel .detail { max-width: none; margin: 0; }
  /* On the backdrop the blocks carry no drop shadow — the panel already
     floats. */
  .overlay-panel .detail, .overlay-panel .hero, .overlay-panel .panel {
    box-shadow: none;
  }
  /* The screenshot lightbox is a bare panel of its own (the detail overlay
     borrows its frame from the card inside it). */
  .overlay-panel.shot-panel {
    max-width: 900px;
    border: 2px solid var(--ink);
    border-radius: 14px;
    background: var(--card);
    overflow: hidden;
  }
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
    /* Narrow phone: smaller thumbs so more than one fits before scrolling. */
    .shot img { width: 148px; height: 84px; }
    .sim { width: 96px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .card, .overlay, .overlay-panel, .shot, .play-badge span { transition: none; }
    .card:hover { transform: none; box-shadow: 4px 4px 0 var(--shadow-c); }
    .shot:hover { transform: none; }
    .play-badge:hover span, .play-badge:focus-visible span { transform: none; }
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

  /* ---------- rendering ---------- */
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
  function section(parent, title) {
    var box = el("section", "panel");
    if (title) box.appendChild(el("div", "section-title", title));
    parent.appendChild(box);
    return box;
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

  function closeButton(label, onClose) {
    var btn = el("button", "overlay-close", "✕");
    btn.setAttribute("aria-label", label);
    btn.addEventListener("click", onClose);
    return btn;
  }

  function openOverlay(panel, trigger) {
    var overlay = el("div", "overlay");
    var entry = { node: overlay, trigger: trigger, keydown: null, position: position };
    overlay.appendChild(panel);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) closeOverlay(entry);
    });
    // Only the topmost overlay answers Escape, so closing a lightbox leaves
    // the detail card it was opened from standing.
    entry.keydown = function (ev) {
      if (ev.key === "Escape" && overlays[overlays.length - 1] === entry) {
        closeOverlay(entry);
      }
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
    var note = el("div", "loading-note", "loading full details");
    var liteInfo = lite.querySelector(".detail-info");
    if (liteInfo) liteInfo.appendChild(note);
    panel.appendChild(lite);

    entry = openOverlay(panel, trigger);

    if (window.__PREVIEW_DATA__) { note.remove(); return; }
    // media:true is what turns the upgraded card into the full game
    // representation — trailer, screenshots, similar games he owns. The grid
    // payload carries none of that.
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

  function openShot(shot, label, trigger) {
    var entry;
    var panel = el("div", "overlay-panel shot-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", label || "Screenshot");
    panel.tabIndex = -1;
    panel.appendChild(closeButton("Close screenshot", function () { closeOverlay(entry); }));

    var img = document.createElement("img");
    img.className = "shot-full";
    img.alt = label || "Screenshot";
    img.src = shot.full || shot.thumb;
    panel.appendChild(img);

    entry = openOverlay(panel, trigger);
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
      var chip = el("span", "score-chip mc " + mcTier(game.metacritic_score),
                    String(game.metacritic_score));
      chip.title = "Metacritic";
      cover.appendChild(chip);
    }
    card.appendChild(cover);

    var body = el("div", "card-body");
    var typeLabel = contentTypeLabel(game);
    if (typeLabel) body.appendChild(el("span", "type-chip", typeLabel));
    body.appendChild(el("div", "title", game.name));
    var pName = parentName(game);
    if (pName) body.appendChild(el("div", "parent-sub", "⤷ " + pName));

    var meta = el("div", "meta");
    var hltb = hoursLabel(game.hltb_main);
    if (hltb) meta.appendChild(el("span", null, "~" + hltb));
    if (game.suggested_platform) meta.appendChild(el("span", null, game.suggested_platform));
    if (game.playtime_hours) meta.appendChild(el("span", null, game.playtime_hours + "h played"));
    if (meta.childNodes.length) body.appendChild(meta);

    // Secondary ratings: OpenCritic and Steam always live here.
    var scores = el("div", "scores");
    if (realScore(game.opencritic_score)) {
      var mini = el("span", "mini oc " + ocTier(game, game.opencritic_score),
                    String(game.opencritic_score));
      mini.title = "OpenCritic";
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

  function badge(parent, label, value, cls, url) {
    if (value === undefined || value === null || value === "") return;
    var b = el(url ? "a" : "span", "badge" + (cls ? " " + cls : ""));
    b.appendChild(el("span", null, label + " "));
    b.appendChild(el("b", null, String(value)));
    if (url) {
      b.href = url;
      b.setAttribute("data-link", "");
      b.appendChild(el("span", "ext", "↗"));
      b.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        openLink(url);
      });
    }
    parent.appendChild(b);
    return b;
  }

  function steamBadge(parent, desc, url) {
    if (!desc) return;
    var tier = steamTier(desc);
    var cls = tier == null ? "steam-none"
      : tier >= 6 ? "steam-pos" : tier === 5 ? "steam-mixed" : "steam-neg";
    var b = badge(parent, "Steam", desc, "steam " + cls, url);
    if (tier != null) {
      var meter = el("span", "meter");
      var fill = el("span", "meter-fill");
      fill.style.width = Math.round((tier / 9) * 100) + "%";
      meter.appendChild(fill);
      b.insertBefore(meter, b.querySelector(".ext")); // meter before the link arrow
      b.title = tier + "/9 on Steam's review-summary scale";
    }
  }

  /* ---- media blocks (get_game_detail(media=True)) ---- */
  /* These mirror the evaluation card's implementations (apps_eval.py) on
     purpose: each widget is one self-contained HTML resource — no build step,
     no CDN, nothing shared at runtime — so a media pattern changed in one
     must be ported to the other by hand. */
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
  /* A hero needs a TRAILER: this card already leads with the cover, so a
     game with screenshots alone gets the strip below and no hero. */
  function heroNode(media) {
    var trailer = media.trailer;
    if (!trailer || !(trailer.url || trailer.video_id)) return null;
    var hero = el("div", "hero");
    if (trailer.kind === "mp4" && trailer.url) {
      mp4Hero(hero, trailer);
    } else if (trailer.kind === "youtube" && trailer.video_id) {
      youtubeHero(hero, trailer);
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

  /* IGDB's similar games, already annotated server-side with what he owns —
     the point of the row is the ownership stickers, not the neighbours. */
  function similarNode(parent, similar) {
    var items = list(similar.items).filter(function (i) { return i && i.name; });
    if (!items.length) return;
    var box = section(parent, "Similar games");
    var strip = el("div", "strip");
    items.forEach(function (item) {
      var card = el("div", "sim");
      card.appendChild(coverNode(item));
      var body = el("div", "sim-body");
      body.appendChild(el("div", "sim-name", item.name));
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

  /* The studio behind the game and what it shipped BEFORE it — server-fetched
     and library-annotated (tools/game_media.py). Under the big-studio damper,
     or with nothing released earlier, only the header line renders: six
     arbitrary posters out of a 500-game catalogue say nothing about this game.
     Mirrors the evaluation card's implementation (apps_eval.py) by hand, like
     every other block these two widgets share. */
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
      body.appendChild(el("div", "sim-name", item.name));
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

  function detailCard(game) {
    // A column stack so the media blocks can span the card's full width; with
    // no media it holds the one .detail panel and looks exactly as before.
    var stack = el("div", "detail-stack");
    var media = game.media || {};
    var hero = heroNode(media);
    if (hero) stack.appendChild(hero);

    var box = el("div", "detail");
    box.appendChild(coverNode(game));

    var info = el("div", "detail-info");
    info.appendChild(el("h1", null, game.name));
    var pName = parentName(game);
    if (pName) info.appendChild(el("div", "sub parent-sub", "part of " + pName));

    var subBits = [];
    if (game.release_date) subBits.push(String(game.release_date).slice(0, 4));
    var owned = (game.platforms || []).filter(function (p) { return p.owned; })
      .map(function (p) { return p.platform; });
    if (owned.length) subBits.push(owned.join(" · "));
    if (game.playtime_hours) subBits.push(game.playtime_hours + "h played");
    if (game.wishlisted && !game.owned) subBits.push("wishlisted");
    if (subBits.length) info.appendChild(el("div", "sub", subBits.join("  ·  ")));

    // Metacritic leads (it's the cover-chip source); every pill links out to
    // its source page via the host when a URL is known or derivable.
    var appid = game.steam_appid != null ? game.steam_appid : game.appid;
    var badges = el("div", "badges");
    var typeLabel = contentTypeLabel(game);
    if (typeLabel) badges.appendChild(el("span", "badge content-badge", typeLabel));
    if (realScore(game.metacritic_score))
      badge(badges, "Metacritic", game.metacritic_score,
            "mc " + mcTier(game.metacritic_score), game.metacritic_url);
    if (realScore(game.opencritic_score))
      badge(badges, "OpenCritic", game.opencritic_score,
            "oc " + ocTier(game, game.opencritic_score), game.opencritic_url);
    steamBadge(badges, game.steam_review_desc,
               appid != null ? "https://store.steampowered.com/app/" + appid + "/" : null);
    badge(badges, "HLTB", hoursLabel(game.hltb_main), null,
          game.name ? "https://howlongtobeat.com/?q=" + encodeURIComponent(game.name) : null);
    badge(badges, "ProtonDB", game.protondb_tier, null,
          appid != null ? "https://www.protondb.com/app/" + appid : null);
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

    box.appendChild(info);
    stack.appendChild(box);

    shotsNode(stack, media, game.name);
    if (game.similar) similarNode(stack, game.similar);
    pedigreeNode(stack, game.pedigree);
    return stack;
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
    if (window.__PREVIEW_OPEN_INDEX__ != null) {
      var previewCards = root.querySelectorAll(".card");
      var target = previewCards[window.__PREVIEW_OPEN_INDEX__];
      if (target) target.click();
    }
  } else {
    request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appCapabilities: {},
      // appInfo per the ext-apps SDK schema (required there); clientInfo kept
      // as a legacy alias — the published 2026-01-26 spec example used it,
      // and schema-validating hosts strip unknown keys.
      appInfo: { name: "gamelib-game-cards", version: "1.0" },
      clientInfo: { name: "gamelib-game-cards", version: "1.0" },
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
