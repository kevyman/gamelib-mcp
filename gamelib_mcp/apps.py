"""MCP Apps (io.modelcontextprotocol/ui): the game-cards widget.

One `ui://` resource serves both card layouts: a cover grid when the tool
result carries a ``results`` array (discover_games) and a single detail card
otherwise (get_game_detail). Clients that don't speak the Apps extension
ignore the tool metadata entirely and see the normal JSON responses, so
attaching ``GAME_CARDS_APP`` to a tool is purely additive.

The HTML is deliberately dependency-free: the host↔iframe bridge is the
~40-line JSON-RPC postMessage handshake from the MCP Apps spec
(ui/initialize → ui/notifications/initialized → ui/notifications/tool-result)
rather than @modelcontextprotocol/ext-apps, so nothing is fetched from a CDN
and the CSP only has to allow the two cover-art image hosts.

For local visual iteration outside any MCP host, the widget renders
``window.__PREVIEW_DATA__`` when present instead of waiting on the bridge —
see scripts/preview_game_cards.py.
"""

from typing import Any

from fastmcp.apps import AppConfig, ResourceCSP

GAME_CARDS_URI = "ui://gamelib/game-cards.html"

# Attached to tools whose results the widget renders.
GAME_CARDS_APP = AppConfig(resource_uri=GAME_CARDS_URI)

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
    --bg: transparent;
    --card: #ffffff;
    --text: #1a1d21;
    --muted: #6b7280;
    --border: rgba(0, 0, 0, 0.08);
    --shadow: 0 1px 3px rgba(0, 0, 0, 0.10), 0 4px 14px rgba(0, 0, 0, 0.06);
    --pill: rgba(0, 0, 0, 0.05);
    --good: #16a34a;
    --ok: #ca8a04;
    --bad: #dc2626;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --card: #23262b;
      --text: #e8eaed;
      --muted: #9aa1ab;
      --border: rgba(255, 255, 255, 0.09);
      --shadow: 0 1px 3px rgba(0, 0, 0, 0.4), 0 4px 14px rgba(0, 0, 0, 0.3);
      --pill: rgba(255, 255, 255, 0.08);
      --good: #4ade80;
      --ok: #facc15;
      --bad: #f87171;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 12px;
    -webkit-font-smoothing: antialiased;
  }

  /* ---- grid mode ---- */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(138px, 1fr));
    gap: 14px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: var(--shadow);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
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
    font-size: 16px;
    font-weight: 650;
    line-height: 1.3;
    color: rgba(255, 255, 255, 0.88);
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
    padding: 12px;
    text-align: center;
    overflow: hidden;
    overflow-wrap: anywhere;
  }
  .score-chip {
    position: absolute;
    top: 7px;
    right: 7px;
    font-size: 11.5px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 999px;
    background: rgba(17, 17, 17, 0.78);
    backdrop-filter: blur(2px);
  }
  .card-body { padding: 9px 10px 10px; display: flex; flex-direction: column; gap: 5px; flex: 1; }
  .title {
    font-size: 13px;
    font-weight: 600;
    line-height: 1.25;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .meta { font-size: 11.5px; color: var(--muted); display: flex; gap: 7px; flex-wrap: wrap; }
  .pills { display: flex; gap: 4px; flex-wrap: wrap; }
  .card-body .pills { margin-top: auto; }
  .pill {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 999px;
    background: var(--pill);
    color: var(--muted);
    white-space: nowrap;
  }
  .pill.match { color: var(--good); font-weight: 600; }

  /* ---- detail mode ---- */
  .detail {
    display: flex;
    gap: 18px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: var(--shadow);
    padding: 16px;
    max-width: 720px;
  }
  .detail .cover-wrap { flex: 0 0 168px; aspect-ratio: 2 / 3; border-radius: 10px; overflow: hidden; align-self: flex-start; }
  .detail-info { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .detail-info h1 { font-size: 19px; font-weight: 700; line-height: 1.2; }
  .sub { font-size: 12.5px; color: var(--muted); }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; }
  .badge {
    font-size: 11.5px;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: 7px;
    background: var(--pill);
  }
  .badge b { font-weight: 700; }
  .desc {
    font-size: 12.5px;
    color: var(--muted);
    line-height: 1.5;
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .rating-row { font-size: 13px; }
  .rating-row b { font-size: 15px; }
  .empty { color: var(--muted); font-size: 13px; padding: 20px; text-align: center; }
  @media (max-width: 460px) {
    .detail { flex-direction: column; }
    .detail .cover-wrap { flex-basis: auto; width: 150px; }
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
      if (cb) { delete pending[m.id]; cb(m.result); }
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
  function handleToolResult(result) {
    var data = result && result.structuredContent;
    if (!data && result && result.content) {
      var text = (result.content.find(function (c) { return c.type === "text"; }) || {}).text;
      try { data = JSON.parse(text); } catch (e) { /* leave undefined */ }
    }
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

  function critic(game) {
    if (game.opencritic_score != null) return { n: game.opencritic_score, src: "OpenCritic" };
    if (game.metacritic_score != null) return { n: game.metacritic_score, src: "Metacritic" };
    return null;
  }
  function scoreColor(n) { return n >= 80 ? "var(--good)" : n >= 60 ? "var(--ok)" : "var(--bad)"; }

  function hoursLabel(h) {
    if (h == null) return null;
    return (h >= 10 ? Math.round(h) : Math.round(h * 10) / 10) + "h";
  }

  function matchedTagNames(game) {
    return (game.matched_tags || []).map(function (t) {
      return typeof t === "string" ? t : t.tag;
    }).filter(Boolean);
  }

  function gridCard(game) {
    var card = el("div", "card");
    var cover = coverNode(game);
    var score = critic(game);
    if (score) {
      var chip = el("span", "score-chip", String(score.n));
      chip.style.color = scoreColor(score.n);
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

    var pills = el("div", "pills");
    if (game.match_score != null) {
      pills.appendChild(el("span", "pill match", Math.round(game.match_score * 100) + "% match"));
    }
    matchedTagNames(game).slice(0, 3).forEach(function (t) {
      pills.appendChild(el("span", "pill", t));
    });
    if (game.value_note) pills.appendChild(el("span", "pill", game.value_note));
    if (pills.childNodes.length) body.appendChild(pills);

    card.appendChild(body);
    return card;
  }

  function badge(parent, label, value, color) {
    if (value === undefined || value === null || value === "") return;
    var b = el("span", "badge");
    b.appendChild(el("span", null, label + " "));
    var v = el("b", null, String(value));
    if (color) v.style.color = color;
    b.appendChild(v);
    parent.appendChild(b);
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
    if (game.opencritic_score != null)
      badge(badges, "OpenCritic", game.opencritic_score, scoreColor(game.opencritic_score));
    if (game.metacritic_score != null)
      badge(badges, "Metacritic", game.metacritic_score, scoreColor(game.metacritic_score));
    badge(badges, "Steam", game.steam_review_desc);
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
