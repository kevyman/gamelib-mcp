"""Blocks shared by the two MCP Apps widgets (apps.py, apps_eval.py).

Each constant is a verbatim slice of widget HTML — CSS rules or JS functions —
that both widgets splice into their own document at the same position. The
widgets stay self-contained ON THE WIRE: every ``ui://`` resource is still one
standalone HTML string with no build step, no CDN and no cross-resource fetch.
Only the Python source is shared, so a fix to the trailer stage or the carousel
lands in both widgets at once instead of being hand-ported (and forgotten).

Splice, never reformat: the constants carry their own indentation and trailing
newline, and a widget's HTML is the literal chunks and these constants
concatenated in order. What is deliberately NOT here is anything the two
widgets genuinely disagree on — the grid's larger cover plates, apps.py's
Steam review-meter palette, its stacking overlay — that stays local to its
widget. ``tests/test_apps_eval.py::WidgetDriftTests`` fails if a block of any
size worth sharing reappears in both files instead.
"""

# ---- Palette, reset and the cover block -------------------------------------
# The light-scheme custom properties both widgets are built on.
PALETTE_LIGHT_CSS = r"""  :root {
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
"""

# The dark-scheme overrides, WITHOUT the wrapping
# ``@media (prefers-color-scheme: dark) { :root { … } }`` — the game-cards widget
# appends three Steam review-meter variables of its own inside the same block.
PALETTE_DARK_VARS = r"""      --bg: #191610;
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
"""

# Box-sizing reset plus the body type/background.
RESET_CSS = r"""  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--ink);
    padding: 16px;
    -webkit-font-smoothing: antialiased;
  }
"""

# The 2:3 cover frame. ``.cover-fallback``'s own type size stays per-widget:
# the grid's plates are larger than the evaluation card's.
COVER_CSS = r"""  .cover-wrap { position: relative; aspect-ratio: 2 / 3; }
  .cover-wrap img, .cover-fallback {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
"""

# ---- Media CSS: hero stage, strips, thumbs ----------------------------------
# The 16:9 trailer/screenshot stage, its poster, play badge and link pill.
HERO_CSS = r"""  .hero {
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
"""

# Sideways-scrolling strip shared by thumbs, similar games and pedigree.
STRIP_CSS = r"""  .strip {
    display: flex;
    gap: 10px;
    overflow-x: auto;
    padding: 2px 2px 6px;
    scrollbar-width: thin;
  }
"""

# The click-to-enlarge screenshot button filling the stage.
SHOT_BTN_CSS = r"""  .shot-btn {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    background: #0d0b07;
    cursor: zoom-in;
    -webkit-tap-highlight-color: transparent;
  }
"""

# The fullscreen button and the thumb strip's thumbnails.
MEDIA_STRIP_CSS = r"""  .fs-btn {
    position: absolute;
    right: 10px;
    top: 10px;
    z-index: 3;
    width: 30px;
    height: 30px;
    border-radius: 8px;
    border: 2px solid var(--ink);
    background: var(--card);
    color: var(--ink);
    font: 800 13px/1 system-ui, sans-serif;
    box-shadow: 2px 2px 0 var(--shadow-c);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .thumbs { margin-top: 10px; }
  .thumb {
    position: relative;
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
  .thumb img { display: block; width: 116px; height: 66px; object-fit: cover; }
  .thumb:hover { transform: translate(-1px, -1px); }
  .thumb:focus-visible { outline: 3px solid var(--ink); outline-offset: 2px; }
  .thumb.sel { border-color: var(--ink); box-shadow: 0 0 0 3px var(--ink); }
  .thumb-play {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(12, 10, 6, 0.34);
    color: #ffffff;
    font-size: 17px;
    font-weight: 900;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
  }
  .thumb-text {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 116px;
    height: 66px;
    font-size: 11px;
    font-weight: 800;
    color: var(--ink);
    background: var(--p2);
  }
"""

# The "+N more" chip closing a truncated strip.
MORE_CHIP_CSS = r"""  .more-chip {
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
"""

# ---- Similar-games CSS ------------------------------------------------------
# Mini cover cards used by both the similar row and the pedigree row.
SIMILAR_CSS = r"""  .sim {
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
"""

# ---- Overlay / carousel / toast CSS -----------------------------------------
# The anchored overlay backdrop (each widget styles its own panel).
OVERLAY_CSS = r"""  .overlay {
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
"""

# The screenshot lightbox: stage, arrows, counter, close button.
CAROUSEL_CSS = r"""  .car-stage {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #0d0b07;
    /* Holds the frame open while the full-res image loads (and if it never
       does) — a zero-height stage would swallow the arrows and the close. */
    min-height: 180px;
    /* Let the browser keep vertical scrolling while horizontal drags are ours. */
    touch-action: pan-y;
  }
  .car-img { display: block; width: 100%; max-height: 74vh; object-fit: contain; }
  .car-nav {
    position: absolute;
    top: 50%;
    z-index: 2;
    transform: translateY(-50%);
    width: 38px;
    height: 38px;
    border-radius: 999px;
    border: 2px solid var(--ink);
    background: var(--card);
    color: var(--ink);
    font: 900 19px/1 system-ui, sans-serif;
    box-shadow: 2px 2px 0 var(--shadow-c);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .car-prev { left: 10px; }
  .car-next { right: 10px; }
  .car-count {
    position: absolute;
    left: 50%;
    bottom: 10px;
    z-index: 2;
    transform: translateX(-50%);
    font-size: 11px;
    font-weight: 800;
    padding: 3px 10px;
    border-radius: 999px;
    border: 2px solid var(--ink);
    background: var(--card);
    color: var(--ink);
  }
  .carousel .fs-btn { right: 56px; }
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
"""

# The bottom toast that explains a host-blocked link.
TOAST_CSS = r"""  .toast {
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
"""

# ---- The host bridge and its link-out fallback ------------------------------
# The hand-rolled MCP Apps postMessage bridge: request/notify plus the
# message listener. Opens the IIFE both widgets live inside.
BRIDGE_JS = r"""(function () {
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
"""

# Link-out through the host, because the widget sandbox usually blocks
# window.open, plus the toast shown when even ui/open-link is refused.
EXTERNAL_LINK_JS = r"""  /* External links. The sandbox usually lacks allow-popups, so window.open
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
"""

# structuredContent-or-text result unwrapping and the result notification.
TOOL_RESULT_JS = r"""  function resultData(result) {
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
"""

# ---- DOM + cover helpers ----------------------------------------------------
# ``root``/``el``/``list``/``num`` — the whole DOM helper vocabulary.
DOM_HELPERS_JS = r"""  var root = document.getElementById("root");

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
"""

# Name-seeded hue for the gradient plate used when there is no art.
COVER_HUE_JS = r"""  function coverHue(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }
"""

# Cover art with the gradient-plate fallback on a missing/broken image.
COVER_NODE_JS = r"""  function coverNode(game) {
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
"""

# ---- Fullscreen and the screenshot carousel ---------------------------------
# Best-effort fullscreen: no button where the API is absent, and the
# button removes itself when the host denies the request.
FULLSCREEN_BUTTON_JS = r"""  function fullscreenButton(target) {
    if (!document.fullscreenEnabled && !document.webkitFullscreenEnabled) return null;
    var req = target.requestFullscreen || target.webkitRequestFullscreen;
    if (!req) return null;
    var btn = el("button", "fs-btn", "⛶");
    btn.setAttribute("aria-label", "Full screen");
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      try {
        var pending = req.call(target);
        if (pending && pending.catch) {
          pending.catch(function () { btn.remove(); });
        }
      } catch (e) {
        btn.remove();
      }
    });
    return btn;
  }
"""

# One carousel arrow.
NAV_BUTTON_JS = r"""  function navButton(cls, glyph, label, onClick) {
    var btn = el("button", "car-nav " + cls, glyph);
    btn.setAttribute("aria-label", label);
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onClick();
    });
    return btn;
  }
"""

# The lightbox stage: image, arrows, counter, wrapping ``show(i)`` and the
# pointer drag/swipe. Spliced INSIDE each widget's own ``openCarousel``,
# which owns the overlay lifecycle (a stack in apps.py, one slot in
# apps_eval.py) — it closes over ``shots``, ``index``, ``panel`` and
# ``gameName`` there.
CAROUSEL_STAGE_JS = r"""    var stage = el("div", "car-stage");
    var img = document.createElement("img");
    img.className = "car-img";
    stage.appendChild(img);
    var counter = el("div", "car-count", "");
    if (shots.length > 1) {
      stage.appendChild(navButton("car-prev", "‹", "Previous screenshot",
        function () { show(index - 1); }));
      stage.appendChild(navButton("car-next", "›", "Next screenshot",
        function () { show(index + 1); }));
      stage.appendChild(counter);
    }
    panel.appendChild(stage);
    var fs = fullscreenButton(stage);
    if (fs) panel.appendChild(fs);

    function show(i) {
      index = ((i % shots.length) + shots.length) % shots.length;   // wraps
      var shot = shots[index];
      img.src = shot.full || shot.thumb;
      img.alt = (gameName ? gameName + " " : "") + "screenshot " + (index + 1);
      counter.textContent = (index + 1) + " / " + shots.length;
    }
    show(index);

    // Drag/swipe. Pointer events cover mouse and touch in one path; a drag
    // shorter than the threshold is a tap and does nothing.
    var startX = null;
    stage.addEventListener("pointerdown", function (ev) { startX = ev.clientX; });
    stage.addEventListener("pointercancel", function () { startX = null; });
    stage.addEventListener("pointerup", function (ev) {
      if (startX === null) return;
      var dx = ev.clientX - startX;
      startX = null;
      if (Math.abs(dx) > 40) show(index + (dx < 0 ? 1 : -1));
    });
"""

# ---- Trailer hero + the media panel (one viewer, one thumb strip) -----------
# The trailer stage: play badge, link pill, poster, the Steam mp4 with its
# per-<source> error fallback, and the click-to-load youtube-nocookie embed.
HERO_MEDIA_JS = r"""  function playBadge(label) {
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
"""

# One viewer plus one thumb strip, trailer first — trailer selection,
# thumb building, and the in-place stage swap.
MEDIA_PANEL_JS = r"""  function trailerEntry(media) {
    var trailer = media.trailer;
    if (!trailer) return null;
    if (trailer.kind === "mp4" && trailer.url) return { kind: "mp4", trailer: trailer };
    if (trailer.kind === "youtube" && trailer.video_id) {
      return { kind: "youtube", trailer: trailer };
    }
    return null;                                    // trailer of an unknown kind
  }
  function shotLabel(gameName, i) {
    return (gameName ? gameName + " " : "") + "screenshot " + (i + 1);
  }
  function showEntry(viewer, entry, shots, gameName) {
    viewer.textContent = "";
    if (entry.kind === "mp4") {
      mp4Hero(viewer, entry.trailer);
      // Native controls carry the browser's own fullscreen button; this is the
      // extra affordance for hosts that surface neither.
      var video = viewer.querySelector("video");
      if (video) {
        var videoFs = fullscreenButton(video);
        if (videoFs) viewer.appendChild(videoFs);
      }
      return;
    }
    if (entry.kind === "youtube") {
      youtubeHero(viewer, entry.trailer);           // the embed handles its own
      return;
    }
    var label = shotLabel(gameName, entry.index);
    var btn = el("button", "shot-btn");
    btn.setAttribute("aria-label", "Enlarge " + label);
    var img = document.createElement("img");
    img.className = "hero-media";
    img.alt = "";                                   // the button carries the label
    img.src = entry.shot.full || entry.shot.thumb;
    btn.appendChild(img);
    btn.addEventListener("click", function () {
      openCarousel(shots, entry.index, gameName, btn);
    });
    viewer.appendChild(btn);
    var fs = fullscreenButton(img);
    if (fs) viewer.appendChild(fs);
  }
  function thumbNode(entry, gameName) {
    var btn = el("button", "thumb");
    if (entry.kind === "shot") {
      btn.setAttribute("aria-label", "Show " + shotLabel(gameName, entry.index));
      var img = document.createElement("img");
      img.alt = "";
      img.loading = "lazy";
      img.src = entry.shot.thumb || entry.shot.full;
      btn.appendChild(img);
      return btn;
    }
    btn.setAttribute("aria-label", "Show the trailer");
    if (entry.trailer.poster) {
      var poster = document.createElement("img");
      poster.alt = "";
      poster.loading = "lazy";
      poster.src = entry.trailer.poster;
      btn.appendChild(poster);
    } else {
      btn.appendChild(el("span", "thumb-text", "TRAILER"));
    }
    btn.appendChild(el("span", "thumb-play", "▶"));
    return btn;
  }
  function mediaNode(parent, media, gameName) {
    var shots = list(media.screenshots).filter(function (s) {
      return s && (s.thumb || s.full);
    });
    var entries = [];
    var trailer = trailerEntry(media);
    if (trailer) entries.push(trailer);
    shots.forEach(function (shot, i) {
      entries.push({ kind: "shot", shot: shot, index: i });
    });
    if (!entries.length) return;

    var box = section(parent, "Media");
    var viewer = el("div", "hero viewer");
    box.appendChild(viewer);

    // `screenshots_truncated` is deliberately NOT surfaced: the extra images
    // are not in the payload, so the old "+N more" chip advertised something
    // nothing could open.
    if (entries.length > 1) {
      var strip = el("div", "strip thumbs");
      var thumbs = entries.map(function (entry) { return thumbNode(entry, gameName); });
      thumbs.forEach(function (btn, i) {
        btn.setAttribute("aria-pressed", "false");
        btn.addEventListener("click", function () { select(i); });
        strip.appendChild(btn);
      });
      box.appendChild(strip);
      var select = function (i) {
        thumbs.forEach(function (btn, j) {
          btn.classList.toggle("sel", i === j);
          btn.setAttribute("aria-pressed", i === j ? "true" : "false");
        });
        showEntry(viewer, entries[i], shots, gameName);
        reportSize();
      };
      select(0);                                    // trailer first when there is one
      return;
    }
    showEntry(viewer, entries[0], shots, gameName);
  }
"""

# ---- Ownership stickers, similar games, studio pedigree ---------------------
# owned / unplayed / rating / hours stickers for a related game.
OWNERSHIP_TAGS_JS = r"""  function ownershipTags(item) {
    var tags = el("div", "tags");
    if (item.owned) tags.appendChild(el("span", "tag owned", "owned"));
    if (item.unplayed) tags.appendChild(el("span", "tag unplayed", "unplayed"));
    var rating = num(item.my_rating);
    if (rating != null) tags.appendChild(el("span", "tag", rating + "/10"));
    var hours = hoursLabel(item.playtime_hours);
    if (hours) tags.appendChild(el("span", "tag", hours));
    return tags.childNodes.length ? tags : null;
  }
"""

# IGDB's similar games as a strip of mini covers with ownership.
SIMILAR_NODE_JS = r"""  function similarNode(parent, similar) {
    var items = list(similar.items).filter(function (i) { return i && i.name; });
    if (!items.length) return;
    var box = section(parent, "Similar games");
    var strip = el("div", "strip");
    // Server-side the items arrive owned-first (tools/game_media.py), so the
    // claim below is visible without scrolling the row.
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
"""

# "From the studio": the ``plural`` helper (a count is singular only when
# it is exactly one AND not a floor), the headline, the per-poster badge
# and the strip with its track-record footer.
PEDIGREE_JS = r"""  function plural(n, word, truncated) {
    return n + (truncated ? "+" : "") + " " + word + (n === 1 && !truncated ? "" : "s");
  }
  function pedigreeHeadline(ped) {
    var dev = ped.developer || {};
    var names = list(ped.developer_names).filter(Boolean);
    var parts = [];
    if (names.length) parts.push(names.join(" & "));
    else if (dev.name) parts.push(dev.name);
    var founded = num(dev.founded_year);
    if (founded != null) parts.push("est. " + founded);
    var size = num(ped.catalog_size);
    if (size) parts.push(plural(size, "game", ped.catalog_truncated));
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
        ? "their last " + plural(items.length, "game")
        : "their " + plural(items.length, "previous game");
      box.appendChild(el("div", "note",
        "You've played " + (num(record.played_count) || 0) + " of "
        + span + (avg != null ? " — avg " + avg + "/10." : ".")));
    }
  }
"""

# ---- Size reporting ---------------------------------------------------------
# Debounced, change-only ui/notifications/size-changed reporting.
SIZING_JS = r"""  /* ---------- sizing ---------- */
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
"""
