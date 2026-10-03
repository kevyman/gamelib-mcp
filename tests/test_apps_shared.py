"""The shared widget module (apps_shared.py) on its own terms.

Spec docs/specs/2026-10-03-widget-ux-redesign.md §1: host theming, touch, hit
areas, the bridge lifecycle and the failure notices — pinned on the constants
both widgets splice in. Most tests are source assertions (the widgets ship
as hand-written JS/CSS strings); ``BridgeBehaviourTests`` additionally runs
the bridge under Node against a tiny DOM stand-in when Node is installed.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from gamelib_mcp import apps, apps_eval, apps_shared


def shared_css_blocks() -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in sorted(vars(apps_shared).items())
        if name.endswith("_CSS") and not name.startswith("_") and isinstance(value, str)
    ]


def shared_blocks() -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in sorted(vars(apps_shared).items())
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    ]


def css_rule(css: str, selector: str, exact: bool = False) -> str:
    """The declaration block of the first rule whose selector contains (or,
    with ``exact``, equals) the given text."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        found = match.group(1).strip()
        if (found == selector) if exact else (selector in found):
            return match.group(2)
    raise AssertionError(f"no rule matching {selector!r}")


INTERACTIVE = (
    "a.chip", ".btn", ".disclosure", ".fs-btn", ".car-nav",
    ".overlay-close", ".hero-pill",
    '.stamp[role="button"]', "button.stamp",
)
# Large enough on their own, and their overflow: hidden would clip an
# extension anyway — so they carry none (an inert rule is a false promise).
NO_EXTENSION = ('.card[role="button"]', ".thumb")


class HostContextTests(unittest.TestCase):
    """§1.1.4: applyHostContext — fonts, variables, sizes, touch, safe areas."""

    def test_host_fonts_are_injected_once_per_distinct_string(self) -> None:
        js = apps_shared.BRIDGE_JS
        self.assertIn(
            'if (styles.css && typeof styles.css.fonts === "string" && styles.css.fonts !== hostFontsCss) {',
            js,
        )
        self.assertIn("hostFontsCss = styles.css.fonts;", js)
        # still exactly one <style id="host-fonts">, found before it is made
        self.assertEqual(js.count('fonts.id = "host-fonts";'), 1)
        self.assertIn('var fonts = document.getElementById("host-fonts");', js)

    def test_partial_variable_updates_merge_instead_of_wiping_the_theme(self) -> None:
        js = apps_shared.BRIDGE_JS
        # Only an explicit null / "" removes a variable; absence keeps it.
        self.assertIn('if (value === null || value === "") docEl.style.removeProperty(name);', js)
        self.assertIn("else if (value !== undefined) docEl.style.setProperty(name, String(value));", js)
        for gone in ("appliedHostVars", "HOST_TOKEN_PREFIXES", "isHostToken", "nextVars"):
            self.assertNotIn(gone, js)

    def test_a_host_ping_is_answered(self) -> None:
        self.assertIn(
            'case "ping":                                               // liveness check\n'
            '        if (m.id !== undefined) post({ jsonrpc: "2.0", id: m.id, result: {} });',
            apps_shared.BRIDGE_JS,
        )

    def test_size_tokens_are_clamped_at_12px(self) -> None:
        css = apps_shared.TOKENS_CSS
        for token, source, px in (
            ("h", "heading-md", 16), ("body", "text-sm", 14), ("cap", "text-xs", 12),
        ):
            with self.subTest(token=token):
                self.assertIn(f"--gl-{token}: max(12px, var(--font-{source}-size, {px}px));", css)
        # no size token escapes the clamp, and the fourth (title) size is gone
        self.assertEqual(re.findall(r"--gl-(?:title|h|body|cap): var\(", css), [])
        self.assertNotIn("--gl-title", css)

    def test_touch_falls_back_to_media_queries_without_device_capabilities(self) -> None:
        js = apps_shared.BRIDGE_JS
        self.assertIn('docEl.classList.toggle("touch", mediaQueryMatches("(pointer: coarse)"));', js)
        self.assertIn('docEl.classList.toggle("no-hover", mediaQueryMatches("(hover: none)"));', js)
        # the host's answer wins whenever it sent one
        self.assertIn('docEl.classList.toggle("touch", deviceCaps.touch);', js)
        self.assertIn("} else if (!hostContext.deviceCapabilities) {", js)
        # and the fallback runs before any host context arrives
        self.assertIn("\n  applyInputFallback();\n", js)

    def test_safe_area_insets_drive_the_strip_scroll_padding(self) -> None:
        self.assertIn('docEl.style.setProperty("--gl-safe-" + side[0], extra + "px");', apps_shared.BRIDGE_JS)
        self.assertIn('if (typeof sent === "number" && isFinite(sent)) safeInsets[side[0]] = Math.max(0, sent);',
                      apps_shared.BRIDGE_JS)
        self.assertIn("--gl-safe-left: 0px;", apps_shared.TOKENS_CSS)
        self.assertIn("--gl-safe-right: 0px;", apps_shared.TOKENS_CSS)

    def test_only_the_host_frame_is_heard(self) -> None:
        self.assertIn("if (ev.source !== window.parent) return;", apps_shared.BRIDGE_JS)

    def test_requests_expire_and_drop_their_pending_entry(self) -> None:
        js = apps_shared.BRIDGE_JS
        self.assertIn("var REQUEST_TIMEOUT_MS = 30000;", js)
        self.assertIn("function request(method, params, timeoutMs) {", js)
        self.assertIn("delete pending[id];\n        resolve(undefined);\n      }, timeoutMs || REQUEST_TIMEOUT_MS);", js)

    def test_an_unknown_theme_falls_back_to_the_viewer_preference(self) -> None:
        js = apps_shared.BRIDGE_JS
        self.assertIn("} else if (ctx.theme !== undefined) {", js)
        self.assertIn("delete docEl.dataset.theme;", js)
        self.assertIn('docEl.style.colorScheme = "";', js)

    def test_root_declares_both_color_schemes(self) -> None:
        self.assertRegex(apps_shared.TOKENS_CSS, r":root \{\s*color-scheme: light dark;")


class HitAreaTests(unittest.TestCase):
    """§1.2: >=32px / >=44px targets that never overlap a sibling's box."""

    def test_every_interactive_class_carries_the_extension(self) -> None:
        css = apps_shared.A11Y_CSS
        base = css_rule(css, "a.chip::after, .btn::after")
        self.assertIn('content: "";', base)
        self.assertIn("position: absolute;", base)
        self.assertIn("inset: -4px;", base)
        touch = css_rule(css, "html.touch a.chip::after")
        self.assertIn("inset: -6px;", touch)
        base_selectors = css.split("a.chip::after, .btn::after", 1)[1].split("{", 1)[0]
        touch_selectors = css.split("html.touch a.chip::after", 1)[1].split("{", 1)[0]
        for cls in INTERACTIVE:
            with self.subTest(cls=cls):
                self.assertIn(f"{cls}::after", "a.chip::after, .btn::after" + base_selectors)
                self.assertIn(f"html.touch {cls}::after", "html.touch a.chip::after" + touch_selectors)
        for cls in NO_EXTENSION:
            with self.subTest(cls=cls):
                self.assertNotIn(f"{cls}::after", css)
        self.assertNotIn('.card[role="button"] {', css)       # no containing-block rule left for it

    def test_chip_rows_keep_extensions_within_half_the_gap(self) -> None:
        a11y, chips = apps_shared.A11Y_CSS, apps_shared.CHIP_CSS
        # pointer: 8px rows / 6px columns against -4px / -3px
        self.assertIn(".chips { display: flex; gap: 8px 6px;", chips)
        self.assertIn(".chips a.chip::after { inset: -4px -3px; }", a11y)
        # touch: 32px chips + 2 x 6px = 44px, rows 12px apart, columns 8px
        self.assertIn("html.touch .chips { gap: 12px 8px; }", chips)
        self.assertIn("html.touch .chips a.chip::after { inset: -6px -4px; }", a11y)
        self.assertIn("html.touch .chip, html.touch .hero-pill { min-height: 32px; }", a11y)
        # pointer: a link chip is >=24px tall, so 24 + 2 x 4 = 32px
        self.assertIn("a.chip { cursor: pointer; min-height: 24px; }", chips)

    def test_extension_hosts_are_containing_blocks(self) -> None:
        self.assertIn("position: relative;", css_rule(apps_shared.CONTROLS_CSS, ".btn, .disclosure", exact=True))
        self.assertIn("position: relative;", css_rule(apps_shared.CHIP_CSS, ".chip", exact=True))
        self.assertIn("position: absolute;", css_rule(apps_shared.HERO_CSS, ".hero-pill", exact=True))

    def test_reduced_motion_wildcard(self) -> None:
        css = apps_shared.A11Y_CSS
        self.assertIn("@media (prefers-reduced-motion: reduce) {", css)
        self.assertIn("*, *::before, *::after {", css)
        self.assertIn("animation: none !important;", css)
        self.assertIn("transition: none !important;", css)


class SharedCssHygieneTests(unittest.TestCase):
    def test_no_raw_color_outside_the_token_layer(self) -> None:
        # Raw colors live in TOKENS_CSS only (the media stage and the cover
        # plate's ink are tokens there); a cover-plate rule is the documented
        # exception if one ever moves here. tests/test_apps.py pins the whole
        # widget CSS, stamp included.
        for name, css in shared_css_blocks():
            if name == "TOKENS_CSS":
                continue
            css = re.sub(r"\.cover-fallback[^{}]*\{[^{}]*\}", "", css)
            with self.subTest(block=name):
                self.assertEqual(re.findall(r"rgba?\(|#[0-9a-fA-F]{3,8}\b", css), [])

    def test_thumb_play_shadow_uses_the_ink_token(self) -> None:
        self.assertIn("text-shadow: 0 1px 3px var(--gl-shadow-ink);", apps_shared.MEDIA_STRIP_CSS)
        tokens = apps_shared.TOKENS_CSS
        self.assertIn("--gl-shadow-ink: rgba(13, 11, 7, 0.5);", tokens)
        self.assertIn("--gl-shadow-ink: color-mix(in srgb, var(--gl-stage) 50%, transparent);", tokens)
        self.assertIn("@supports (color: color-mix(in srgb, red 50%, transparent))", tokens)

    def test_the_only_font_shorthand_is_button_inherit(self) -> None:
        # The 3-size / 2-weight rule (tests/test_apps.py) bans the `font:`
        # shorthand except `font: inherit`; the shared CSS uses exactly that.
        found = []
        for _name, css in shared_css_blocks():
            found += re.findall(r"(?<![-\w])font:\s*([^;}]+)", css)
        self.assertEqual(found, ["inherit"])
        self.assertIn("button { font: inherit; color: inherit; }", apps_shared.RESET_CSS)
        self.assertEqual(re.findall(r"(?<![-\w])font:(?!\s*inherit)", "button { font: inherit; }"), [])

    def test_strips_snap_and_leave_room_for_the_last_item(self) -> None:
        strip = css_rule(apps_shared.STRIP_CSS, ".strip", exact=True)
        for decl in (
            "scroll-snap-type: x proximity;",
            # 4px: the focus ring's reach (2px ring + 2px offset), never clipped
            "padding: 4px 4px 6px;",
            "scroll-padding-inline: calc(4px + var(--gl-safe-left)) calc(4px + var(--gl-safe-right));",
            "overscroll-behavior-x: contain;",
        ):
            self.assertIn(decl, strip)
        self.assertIn(".strip > * { scroll-snap-align: start; }", apps_shared.STRIP_CSS)
        self.assertIn('.strip::after { content: ""; flex: 0 0 max(4px, var(--gl-safe-right)); }',
                      apps_shared.STRIP_CSS)


class LifecycleTests(unittest.TestCase):
    """§1.4: teardown, unreadable results, cancellation, notices."""

    def test_teardown_flag_guards_report_size_and_host_context(self) -> None:
        sizing = apps_shared.SIZING_JS
        self.assertIn("tornDown = true;", sizing)
        self.assertIn("if (tornDown || window.__PREVIEW_DATA__ || !hooks.shouldReportSize()) return;", sizing)
        self.assertIn("sizeTimer = null;", sizing)
        self.assertIn('if (tornDown || !ctx || typeof ctx !== "object") return;', apps_shared.BRIDGE_JS)

    def test_unparseable_result_replaces_the_skeleton_with_a_notice(self) -> None:
        js = apps_shared.TOOL_RESULT_JS
        body = js.split("function handleToolResult(result) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("if (!rootShowsContent()) root.textContent = \"\";", body)
        # an errored result says its own cause; only an unreadable one says so
        self.assertIn(
            'notice(root, result && result.isError ? toolErrorText(result) : "Couldn\'t read the result");',
            body,
        )
        self.assertIn('if (text.length > 160) text = text.slice(0, 159).trim() + "…";', js)

    def test_cancel_after_render_keeps_the_content(self) -> None:
        js = apps_shared.TOOL_RESULT_JS
        body = js.split("function handleToolCancelled() {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('if (!rootShowsContent()) root.textContent = "";', body)
        self.assertIn('notice(root, "Cancelled before the result arrived.");', body)
        self.assertNotIn('\n    root.textContent = "";', body)

    def test_notice_paths(self) -> None:
        js = apps_shared.NOTICE_JS
        self.assertIn('if (typeof items === "string") {', js)
        self.assertIn('text = "Couldn\'t load: " + parts.join(", ");', js)
        self.assertIn("if (!it.what) return it.source ? String(it.source) : null;", js)


NODE = shutil.which("node")

# A DOM stand-in just wide enough for the bridge, tool-result, notice and
# sizing blocks; the script under test is spliced exactly as the widgets do.
DOM_SHIM = r"""
var posted = [];
var timers = [];
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout(id) { if (id) timers[id - 1] = null; }
function flush() { var q = timers; timers = []; q.forEach(function (f) { if (f) f(); }); }
function FakeNode(tag) {
  this.tagName = tag; this.childNodes = []; this.className = ""; this.attrs = {};
  this._text = ""; this.style = {}; this.dataset = {};
}
Object.defineProperty(FakeNode.prototype, "textContent", {
  get: function () { return this._text + this.childNodes.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) { this._text = String(v); this.childNodes = []; this.writes = (this.writes || 0) + 1; },
});
Object.defineProperty(FakeNode.prototype, "firstElementChild", {
  get: function () { return this.childNodes[0] || null; },
});
Object.defineProperty(FakeNode.prototype, "classList", {
  get: function () {
    var node = this;
    return { contains: function (c) { return node.className.split(" ").indexOf(c) >= 0; } };
  },
});
FakeNode.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
FakeNode.prototype.appendChild = function (c) {
  var self = this;
  c.parent = self;
  Object.defineProperty(c, "nextElementSibling", {
    configurable: true,
    get: function () { var i = self.childNodes.indexOf(c); return self.childNodes[i + 1] || null; },
  });
  this.childNodes.push(c);
  return c;
};
var props = {}, removed = [], classes = {}, fontNodes = [];
var docEl = {
  dataset: {}, scrollWidth: 300, scrollHeight: 200,
  style: {
    setProperty: function (k, v) { props[k] = v; },
    removeProperty: function (k) { delete props[k]; removed.push(k); },
  },
  classList: { toggle: function (c, on) { classes[c] = !!on; } },
  setAttribute: function () {},
};
var rootNode = new FakeNode("div");
var document = {
  documentElement: docEl,
  head: { appendChild: function (n) { fontNodes.push(n); } },
  body: { style: {}, appendChild: function () {} },
  createElement: function (t) { return new FakeNode(t); },
  getElementById: function (id) {
    if (id === "root") return rootNode;
    return id === "host-fonts" ? (fontNodes[0] || null) : null;
  },
  querySelector: function () { return null; },
};
var listeners = {};
var hostFrame = { postMessage: function (m) { posted.push(m); } };
var window = {
  parent: hostFrame,
  addEventListener: function (k, f) { listeners[k] = f; },
  matchMedia: function (q) { return { matches: q === "(pointer: coarse)" }; },
};
var rendered = [];
function render(d) { rendered.push(d); rootNode.textContent = ""; rootNode.appendChild(new FakeNode("div")); }
function skeletonKind() { return "eval"; }
"""

PROBE = r"""
  var out = {};
  out.fallbackTouch = classes.touch; out.fallbackNoHover = classes["no-hover"];

  /* B1: initialize with ten variables, then a partial update with one. */
  var initial = {};
  ["text-primary", "text-secondary", "text-tertiary", "background-primary",
   "background-secondary", "border-primary", "border-tertiary", "text-success",
   "text-warning", "text-danger"].forEach(function (n, i) { initial["--color-" + n] = "#00" + i; });
  applyHostContext({ styles: { variables: initial, css: { fonts: "@font-face{a}" } } });
  applyHostContext({ styles: { variables: { "--color-text-primary": "#222" },
                               css: { fonts: "@font-face{a}" } } });
  out.fontNodes = fontNodes.length; out.fontText = fontNodes[0].textContent;
  out.propsAfterPartial = JSON.parse(JSON.stringify(props)); out.removedAfterPartial = removed.slice();
  applyHostContext({ styles: { variables: { "--color-text-danger": null, "--color-text-warning": "" } } });
  out.propsAfterNull = Object.keys(props).length; out.removedAfterNull = removed.slice();
  applyHostContext({ styles: { css: { fonts: "@font-face{b}" } } });
  out.fontText2 = fontNodes[0].textContent; out.fontNodes2 = fontNodes.length;
  out.fontWrites = fontNodes[0].writes;
  applyHostContext({ deviceCapabilities: { touch: false, hover: true } });
  out.hostTouch = classes.touch;
  applyHostContext({ theme: "dark" });
  out.touchAfterPartial = classes.touch;
  applyHostContext({ deviceCapabilities: { touch: true } });
  applyHostContext({ deviceCapabilities: { hover: false } });   // partial: no `touch` key
  out.touchAfterHoverOnly = classes.touch; out.noHoverAfterHoverOnly = classes["no-hover"];
  out.mergedCaps = hostContext.deviceCapabilities;
  out.themeDark = docEl.dataset.theme;
  applyHostContext({ theme: "sepia" });
  out.themeAfterUnknown = docEl.dataset.theme === undefined ? null : docEl.dataset.theme;
  out.schemeAfterUnknown = docEl.style.colorScheme;

  /* P1: ui/message carries a ContentBlock[]. */
  posted = [];
  sendMessage("Show me Hades II");
  out.message = posted[0];
  /* P2: only the host frame is heard; P3: an unanswered request expires. */
  var answers = [];
  posted = [];
  request("ping", {}).then(function (r) { answers.push(["ping", r === undefined ? null : r]); });
  var pingId = posted[0].id;
  listeners.message({ source: {}, data: { jsonrpc: "2.0", id: pingId, result: { spoofed: true } } });
  out.pendingAfterSpoof = !!pending[pingId];
  listeners.message({ source: hostFrame, data: { jsonrpc: "2.0", id: pingId, result: { ok: true } } });
  out.pendingAfterAnswer = !!pending[pingId];
  request("never", {}, 50).then(function (r) { answers.push(["never", r === undefined ? null : r]); });
  var neverId = posted[posted.length - 1].id;
  out.pendingBeforeTimeout = !!pending[neverId];
  flush();
  out.pendingAfterTimeout = !!pending[neverId];

  var p = new FakeNode("div");
  notice(p, "Cancelled");
  notice(p, [{ what: "trailer", source: "Steam" }, { source: "IGDB" }, { foo: 1 }, "studio", null]);
  out.notices = p.childNodes.map(function (n) { return n.textContent; });
  out.emptyNotice = notice(new FakeNode("div"), [{ foo: 1 }, {}]);

  rootNode.textContent = ""; var sk = new FakeNode("div"); sk.className = "skel"; rootNode.appendChild(sk);
  handleToolResult({ content: [{ type: "text", text: "not json" }] });
  out.afterBadResult = rootNode.childNodes.map(function (n) { return n.className + ":" + n.textContent; });

  rootNode.textContent = ""; var card = new FakeNode("div"); card.className = "card"; rootNode.appendChild(card);
  handleToolCancelled();
  out.afterCancel = rootNode.childNodes.map(function (n) { return n.className; });

  /* F3: an errored tool result shows its own text, capped at 160 chars. */
  rootNode.textContent = ""; var sk2 = new FakeNode("div"); sk2.className = "skel"; rootNode.appendChild(sk2);
  handleToolResult({ isError: true, content: [{ type: "text", text: "Game not found: 'Hadess'" }] });
  out.afterError = rootNode.childNodes.map(function (n) { return n.className + ":" + n.textContent; });
  rootNode.textContent = "";
  handleToolResult({ isError: true, content: [{ type: "text", text: new Array(60).join("too long ") }] });
  out.longError = rootNode.childNodes[0].textContent;
  /* Item 6: the error names its tool — the host's toolInfo, else the
     tool-input's _meta. */
  rootNode.textContent = "";
  handleToolInput({ arguments: {}, _meta: { toolName: "discover_games" } });
  handleToolResult({ isError: true, content: [{ type: "text", text: "IGDB is down" }] });
  out.metaNamedError = rootNode.childNodes[0].textContent;
  rootNode.textContent = "";
  applyHostContext({ toolInfo: { tool: { name: "get_game_detail" } } });
  handleToolResult({ isError: true, content: [{ type: "text", text: "Game not found: 'Hadess'" }] });
  out.namedError = rootNode.childNodes[0].textContent;
  out.overriddenError = toolErrorText({ content: [{ type: "text", text: "x" }] }, "record_assessment");
  hostContext.toolInfo = undefined; lastToolMeta = null; lastToolInput = null;

  /* Safe-area insets merge per side: an absent side keeps its value. */
  applyHostContext({ safeAreaInsets: { top: 10, left: 5, right: 3 } });
  applyHostContext({ safeAreaInsets: { left: 8, bottom: "7" } });
  out.insets = {
    top: document.body.style.paddingTop, left: document.body.style.paddingLeft,
    right: document.body.style.paddingRight, bottom: document.body.style.paddingBottom,
    safeLeft: props["--gl-safe-left"], safeRight: props["--gl-safe-right"],
    stored: hostContext.safeAreaInsets,
  };

  /* B3: a host ping is answered with an empty result. */
  posted = [];
  listeners.message({ source: hostFrame, data: { jsonrpc: "2.0", id: 77, method: "ping" } });
  out.pingAnswer = posted[0];

  /* B5: the hooks are called at their points. */
  var calls = [];
  hooks.afterToolInput = function (args) { calls.push(["input", args]); };
  hooks.afterToolResult = function (data) { calls.push(["result", data]); };
  hooks.afterHostContext = function (ctx) { calls.push(["context", ctx.displayMode]); };
  handleToolInput({ arguments: { vibes: ["roguelike"] } });
  handleToolResult({ structuredContent: { results: [] } });
  applyHostContext({ displayMode: "fullscreen" });
  out.hookCalls = calls;
  flush(); posted = [];
  hooks.shouldReportSize = function () { return false; };
  docEl.scrollHeight = 999;
  reportSize(); flush();
  out.postedWhileSuppressed = posted.length;
  hooks.shouldReportSize = function () { return true; };
  reportSize(); flush();
  out.postedWhenAllowed = posted.map(function (m) { return m.method; });

  flush(); posted = [];
  reportSize(); teardown(); flush();
  reportSize(); flush();
  applyHostContext({ theme: "light" });
  out.postedAfterTeardown = posted.length;
  out.themeAfterTeardown = docEl.dataset.theme || null;
  Promise.resolve().then(function () {
    out.answers = answers;
    console.log(JSON.stringify(out));
  });
})();
"""


@unittest.skipUnless(NODE, "node is not installed")
class BridgeBehaviourTests(unittest.TestCase):
    """The same guarantees, executed: the shared blocks run under Node."""

    @classmethod
    def setUpClass(cls) -> None:
        script = (
            DOM_SHIM
            + apps_shared.BRIDGE_JS
            + "  function showSkeleton() {}\n"
            + apps_shared.EXTERNAL_LINK_JS
            + apps_shared.TOOL_RESULT_JS
            + apps_shared.DOM_HELPERS_JS
            + apps_shared.NOTICE_JS
            + apps_shared.MODEL_CONTEXT_JS
            + apps_shared.SIZING_JS
            + PROBE
        )
        assert NODE is not None
        proc = subprocess.run(
            [NODE, "-e", script], capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout)

    def test_input_fallback_reads_media_queries(self) -> None:
        self.assertTrue(self.out["fallbackTouch"])
        self.assertFalse(self.out["fallbackNoHover"])

    def test_fonts_injected_once_then_replaced_on_change(self) -> None:
        self.assertEqual(self.out["fontNodes"], 1)
        self.assertEqual(self.out["fontText"], "@font-face{a}")
        self.assertEqual(self.out["fontNodes2"], 1)
        self.assertEqual(self.out["fontText2"], "@font-face{b}")
        # two identical payloads + one change = two writes, not three
        self.assertEqual(self.out["fontWrites"], 2)

    def test_a_partial_variable_update_keeps_the_rest_of_the_theme(self) -> None:
        props = self.out["propsAfterPartial"]
        self.assertEqual(len(props), 10)                     # 10 remain…
        self.assertEqual(props["--color-text-primary"], "#222")  # …1 updated
        self.assertEqual(props["--color-text-danger"], "#009")
        self.assertEqual(self.out["removedAfterPartial"], [])

    def test_only_an_explicit_null_or_empty_value_removes_a_variable(self) -> None:
        self.assertEqual(self.out["propsAfterNull"], 8)
        self.assertEqual(sorted(self.out["removedAfterNull"]), ["--color-text-danger", "--color-text-warning"])

    def test_an_errored_result_shows_its_own_text(self) -> None:
        self.assertEqual(self.out["afterError"], ["notice:The tool failed: Game not found: 'Hadess'."])
        self.assertLessEqual(len(self.out["longError"]), 160)
        self.assertTrue(self.out["longError"].endswith("…"))

    def test_an_errored_result_names_its_tool_when_known(self) -> None:
        self.assertEqual(self.out["namedError"], "get_game_detail failed: Game not found: 'Hadess'.")
        self.assertEqual(self.out["metaNamedError"], "discover_games failed: IGDB is down.")
        self.assertEqual(self.out["overriddenError"], "record_assessment failed: x.")

    def test_safe_area_insets_merge_per_side(self) -> None:
        insets = self.out["insets"]
        # top and right were only in the first update; left changed; a
        # non-number bottom is ignored (it keeps 0).
        self.assertEqual(
            {k: insets[k] for k in ("top", "left", "right", "bottom")},
            {"top": "22px", "left": "20px", "right": "15px", "bottom": "12px"},
        )
        self.assertEqual((insets["safeLeft"], insets["safeRight"]), ("8px", "3px"))
        self.assertEqual(insets["stored"], {"top": 10, "right": 3, "bottom": 0, "left": 8})

    def test_a_host_ping_gets_an_empty_result(self) -> None:
        self.assertEqual(self.out["pingAnswer"], {"jsonrpc": "2.0", "id": 77, "result": {}})

    def test_hooks_run_at_their_points(self) -> None:
        self.assertEqual(
            self.out["hookCalls"],
            [["input", {"vibes": ["roguelike"]}], ["result", {"results": []}], ["context", "fullscreen"]],
        )
        self.assertEqual(self.out["postedWhileSuppressed"], 0)
        self.assertEqual(self.out["postedWhenAllowed"], ["ui/notifications/size-changed"])

    def test_host_device_capabilities_win_and_survive_partial_updates(self) -> None:
        self.assertFalse(self.out["hostTouch"])
        self.assertFalse(self.out["touchAfterPartial"])

    def test_a_partial_device_update_keeps_the_touch_state(self) -> None:
        # Codex review on #191: {deviceCapabilities: {hover: false}} after
        # touch: true must not drop html.touch (and the 44px hit areas).
        self.assertTrue(self.out["touchAfterHoverOnly"])
        self.assertTrue(self.out["noHoverAfterHoverOnly"])
        self.assertEqual(self.out["mergedCaps"], {"touch": True, "hover": False})

    def test_notice_string_list_and_missing_what(self) -> None:
        self.assertEqual(
            self.out["notices"],
            ["Cancelled", "Couldn't load: trailer (Steam), IGDB, studio"],
        )
        self.assertIsNone(self.out["emptyNotice"])

    def test_unparseable_result_and_cancel_after_render(self) -> None:
        self.assertEqual(self.out["afterBadResult"], ["notice:Couldn't read the result"])
        self.assertEqual(self.out["afterCancel"], ["card", "notice"])

    def test_an_unknown_theme_drops_the_forced_scheme(self) -> None:
        self.assertEqual(self.out["themeDark"], "dark")
        self.assertIsNone(self.out["themeAfterUnknown"])
        self.assertEqual(self.out["schemeAfterUnknown"], "")

    def test_ui_message_content_is_an_array_of_blocks(self) -> None:
        msg = self.out["message"]
        self.assertEqual(msg["method"], "ui/message")
        self.assertEqual(
            msg["params"],
            {"role": "user", "content": [{"type": "text", "text": "Show me Hades II"}]},
        )

    def test_only_the_host_frame_answers_and_requests_expire(self) -> None:
        self.assertTrue(self.out["pendingAfterSpoof"])      # a foreign frame is ignored
        self.assertFalse(self.out["pendingAfterAnswer"])
        self.assertTrue(self.out["pendingBeforeTimeout"])
        self.assertFalse(self.out["pendingAfterTimeout"])   # the timeout deletes the entry
        self.assertEqual(self.out["answers"], [["ping", {"ok": True}], ["never", None]])

    def test_nothing_reported_or_applied_after_teardown(self) -> None:
        self.assertEqual(self.out["postedAfterTeardown"], 0)
        self.assertNotEqual(self.out["themeAfterTeardown"], "light")


_WIDGET_SOURCES = {
    name: (Path(apps_shared.__file__).parent / name).read_text()
    for name in ("apps_shared.py", "apps.py", "apps_eval.py")
}


class ImageFallbackTests(unittest.TestCase):
    """Item 6 (F1): no broken-image glyph, no alt text over the stage."""

    def test_every_img_element_has_an_error_fallback(self) -> None:
        sites = 0
        for name, source in _WIDGET_SOURCES.items():
            for match in re.finditer(r'var (\w+) = document\.createElement\("img"\);', source):
                sites += 1
                var = match.group(1)
                # the handler is attached before src is set, within the builder
                window = source[match.end():match.end() + 2000]
                with self.subTest(module=name, var=var, at=match.start()):
                    self.assertRegex(window, rf"\b{var}\.onerror = function")
                    self.assertLess(window.index(f"{var}.onerror"), window.index(f"{var}.src"))
        self.assertGreaterEqual(sites, 6)   # cover, poster, stage, thumb, carousel, anchor

    def test_the_fallbacks_are_the_neutral_tiles(self) -> None:
        self.assertIn('el("span", "thumb-text", text)', apps_shared.MEDIA_PANEL_JS)
        self.assertIn('el("div", "hero-missing", "Screenshot unavailable")', apps_shared.MEDIA_PANEL_JS)
        self.assertIn('el("div", "hero-missing below-badge", missingText || "Trailer")', apps_shared.HERO_MEDIA_JS)
        self.assertIn('var missing = el("div", "hero-missing", "Screenshot unavailable");',
                      apps_shared.CAROUSEL_STAGE_JS)
        self.assertIn('coverPlate(a.name, "anchor-cover")', apps_eval.EVAL_CARD_HTML)


# Just enough DOM for the media builders: parent links, replaceChild, remove.
_MEDIA_SHIM = r"""
function FakeNode(tag) {
  this.tagName = tag; this.childNodes = []; this.className = ""; this.attrs = {};
  this.style = {}; this.parentNode = null; this._text = "";
}
Object.defineProperty(FakeNode.prototype, "textContent", {
  get: function () { return this._text + this.childNodes.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) { this._text = String(v); this.childNodes = []; },
});
FakeNode.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
FakeNode.prototype.addEventListener = function () {};
FakeNode.prototype.appendChild = function (c) { c.parentNode = this; this.childNodes.push(c); return c; };
FakeNode.prototype.replaceChild = function (n, o) {
  var i = this.childNodes.indexOf(o); this.childNodes[i] = n; n.parentNode = this; o.parentNode = null; return o;
};
FakeNode.prototype.remove = function () {
  if (!this.parentNode) return;
  var kids = this.parentNode.childNodes; kids.splice(kids.indexOf(this), 1); this.parentNode = null;
};
var document = { createElement: function (t) { return new FakeNode(t); } };
function el(tag, cls, text) {
  var n = new FakeNode(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n;
}
function list(v) { return Array.isArray(v) ? v : []; }
function openLink() {} function reportSize() {} function openCarousel() {}
function fullscreenButton() { return el("button", "fs-btn"); }
function shape(n) {
  return { tag: n.tagName, cls: n.className, text: n._text,
           kids: n.childNodes.map(shape) };
}
"""

_MEDIA_PROBE = r"""
var out = {};
var thumb = thumbNode({ kind: "shot", shot: { thumb: "https://x/1.jpg" }, index: 2 }, "Hades II");
thumb.childNodes[0].onerror();
out.thumb = shape(thumb);
var trailerThumb = thumbNode({ kind: "mp4", trailer: { poster: "https://x/p.jpg" } }, "Hades II");
trailerThumb.childNodes[0].onerror();
out.trailerThumb = shape(trailerThumb);
var hero = el("div", "hero");
hero.appendChild(posterNode("https://x/poster.jpg", "Trailer thumbnail", "Trailer unavailable here"));
hero.childNodes[0].onerror();
out.poster = shape(hero);
var viewer = el("div", "hero viewer");
showEntry(viewer, { kind: "shot", shot: { full: "https://x/2.jpg" }, index: 0 }, [], "Hades II");
viewer.childNodes[0].childNodes[0].onerror();
out.stage = shape(viewer);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ImageFallbackBehaviourTests(unittest.TestCase):
    """F1, executed: a failed thumb, poster and stage image become text tiles."""

    @classmethod
    def setUpClass(cls) -> None:
        assert NODE is not None
        proc = subprocess.run(
            [NODE, "-e", _MEDIA_SHIM + apps_shared.HERO_MEDIA_JS + apps_shared.MEDIA_PANEL_JS + _MEDIA_PROBE],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout)

    def test_a_failed_screenshot_thumb_becomes_a_text_tile(self) -> None:
        self.assertEqual(self.out["thumb"]["kids"], [{"tag": "span", "cls": "thumb-text",
                                                       "text": "Screenshot 3", "kids": []}])

    def test_a_failed_trailer_thumb_becomes_one_text_tile(self) -> None:
        # the tile carries its own ▶; the overlay glyph would sit on the word
        self.assertEqual([k["cls"] for k in self.out["trailerThumb"]["kids"]], ["thumb-text"])
        self.assertEqual(self.out["trailerThumb"]["kids"][0]["text"], "▶ Trailer")

    def test_a_failed_poster_shows_the_stage_line(self) -> None:
        self.assertEqual(self.out["poster"]["kids"], [{"tag": "div", "cls": "hero-missing below-badge",
                                                       "text": "Trailer unavailable here", "kids": []}])

    def test_a_failed_stage_screenshot_drops_its_fullscreen_button(self) -> None:
        stage = self.out["stage"]["kids"]
        self.assertEqual([k["cls"] for k in stage], ["shot-btn"])
        self.assertEqual(stage[0]["kids"][0]["cls"], "hero-missing")


class HookTests(unittest.TestCase):
    """B5: widgets assign hooks; they never reassign a shared function."""

    def test_no_widget_reassigns_a_shared_function(self) -> None:
        shared_names = set()
        for _name, block in shared_blocks():
            shared_names |= set(re.findall(r"\bfunction (\w+)\(", block))
        self.assertIn("applyHostContext", shared_names)
        self.assertIn("reportSize", shared_names)
        for module in ("apps.py", "apps_eval.py"):
            source = _WIDGET_SOURCES[module]
            with self.subTest(module=module):
                reassigned = [
                    name for name in re.findall(r"(?<![.\w])(\w+)\s*=\s*function\b", source)
                    if name in shared_names
                ]
                self.assertEqual(reassigned, [])
                self.assertNotIn("new ResizeObserver", source)

    def test_the_hooks_are_declared_once_and_called_by_the_shared_code(self) -> None:
        self.assertIn("var hooks = {", apps_shared.BRIDGE_JS)
        self.assertIn("hooks.afterHostContext(ctx);", apps_shared.BRIDGE_JS)
        self.assertIn("hooks.afterToolInput(lastToolInput);", apps_shared.TOOL_RESULT_JS)
        self.assertIn("hooks.afterToolResult(data);", apps_shared.TOOL_RESULT_JS)
        self.assertIn("!hooks.shouldReportSize()", apps_shared.SIZING_JS)
        self.assertIn("hooks.afterToolInput = function () {", apps.GAME_CARDS_HTML)
        self.assertIn("hooks.afterHostContext = hostContextChanged;", apps.GAME_CARDS_HTML)
        self.assertIn("hooks.shouldReportSize = function () {", apps_eval.EVAL_CARD_HTML)


class OneCopyHelperTests(unittest.TestCase):
    """D2: one section(), one steamChip, one rating tier — no local twins."""

    def test_helpers_live_once_in_the_shared_blocks(self) -> None:
        self.assertIn("function section(parent, title) {", apps_shared.DOM_HELPERS_JS)
        for name, html in (("game-cards", apps.GAME_CARDS_HTML), ("eval-card", apps_eval.EVAL_CARD_HTML)):
            with self.subTest(widget=name):
                self.assertEqual(html.count("function section("), 1)
                self.assertEqual(html.count("function steamChip("), 1)
        for module in ("apps.py", "apps_eval.py"):
            with self.subTest(module=module):
                self.assertNotIn("function section(", _WIDGET_SOURCES[module])
                self.assertNotIn("gridSteamChip", _WIDGET_SOURCES[module])
                self.assertNotIn(">= 7 ?", _WIDGET_SOURCES[module])     # ratingTier's thresholds


class LightboxChromeTests(unittest.TestCase):
    """A4: one focus trap, one ✕, one key router — in both widgets."""

    def test_both_widgets_carry_the_shared_trap_and_no_second_copy(self) -> None:
        for name, html in (("game-cards", apps.GAME_CARDS_HTML), ("eval-card", apps_eval.EVAL_CARD_HTML)):
            with self.subTest(widget=name):
                self.assertIn(apps_shared.LIGHTBOX_CHROME_JS, html)
                self.assertEqual(html.count("function keepFocusInside("), 1)
                self.assertEqual(html.count('el("button", "overlay-close"'), 1)
                self.assertIn("lightboxKeys(panel, function (delta) { show(index + delta); }", html)
                self.assertIn('document.addEventListener("keydown", ', html)
                # the focusin guard is installed with the dialog and removed with it
                self.assertEqual(html.count('document.addEventListener("focusin", '), 1)
                self.assertEqual(html.count('document.removeEventListener("focusin", '), 1)
                self.assertIn("= focusGuard(panel);", html)
        for module in ("apps.py", "apps_eval.py"):
            with self.subTest(module=module):
                self.assertNotIn("function keepFocusInside(", _WIDGET_SOURCES[module])
                self.assertNotIn('panel.setAttribute("role", "dialog")', _WIDGET_SOURCES[module])

    def test_the_chrome_is_a_labelled_modal_with_a_typed_close(self) -> None:
        js = apps_shared.LIGHTBOX_CHROME_JS
        for marker in (
            'panel.setAttribute("role", "dialog");',
            'panel.setAttribute("aria-modal", "true");',
            'closer.type = "button";',
            'closer.setAttribute("aria-label", "Close screenshots");',
            'if (ev.key === "Escape") { ev.preventDefault(); onClose(); }',
            'else if (ev.key === "Tab") keepFocusInside(ev, panel);',
            "var FOCUSABLE = 'button, [href], [tabindex]:not([tabindex=\"-1\"])';",
        ):
            self.assertIn(marker, js)


class PlainColorFallbackTests(unittest.TestCase):
    """B6: a theme for WebViews without light-dark()."""

    def test_every_color_token_is_redeclared_with_plain_values(self) -> None:
        tokens = apps_shared.TOKENS_CSS
        layer, fallback = tokens.split("@supports not (color: light-dark(red, blue)) {", 1)
        names = re.findall(r"^    (--gl-[a-z0-9-]+): var\([^,]+, light-dark\(", layer, re.MULTILINE)
        self.assertGreaterEqual(len(names), 18)
        light = fallback.split("@media (prefers-color-scheme: dark)", 1)[0]
        dark_media = fallback.split('[data-theme="light"])', 1)[1].split("}", 1)[0]
        dark_theme = fallback.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
        for name in names:
            with self.subTest(token=name):
                for part in (light, dark_media, dark_theme):
                    self.assertRegex(part, rf"{name}: var\(--[a-z-]+, [^;]+\);")
        self.assertNotIn("light-dark(", fallback)
        self.assertIn("--gl-text: var(--color-text-primary, #141413);", light)
        self.assertIn("--gl-text: var(--color-text-primary, #FAF9F5);", dark_media)


class SplicedVerbatimTests(unittest.TestCase):
    def test_every_shared_constant_appears_in_both_widgets(self) -> None:
        blocks = shared_blocks()
        self.assertGreater(len(blocks), 20)
        for name, block in blocks:
            with self.subTest(block=name):
                self.assertIn(block, apps.GAME_CARDS_HTML)
                self.assertIn(block, apps_eval.EVAL_CARD_HTML)


# ---- whole-widget runs ------------------------------------------------------
# A small DOM (elements, text nodes, a selector subset, capture/bubble event
# dispatch, focus with focusin) wide enough to run a WHOLE widget script under
# Node: the probe is spliced inside the widget's IIFE right after its
# startWidget() call, so it sees every widget function and the shared state.
MINI_DOM = r"""
var posted = [];
var timerSeq = 0, timers = {};
function setTimeout(fn) { timerSeq += 1; timers[timerSeq] = fn; return timerSeq; }
function clearTimeout(id) { delete timers[id]; }
function flushTimers() {
  Object.keys(timers).forEach(function (id) { var f = timers[id]; delete timers[id]; if (f) f(); });
}
function requestAnimationFrame(fn) { return setTimeout(fn); }
function tick() { return new Promise(function (r) { setImmediate(r); }); }

function makeStyle() {
  return { setProperty: function (k, v) { this[k] = v; }, removeProperty: function (k) { delete this[k]; } };
}
function TextNode(t) { this.nodeType = 3; this._text = String(t); this.parentNode = null; this.listeners = []; }
Object.defineProperty(TextNode.prototype, "textContent", { get: function () { return this._text; } });
function El(tag) {
  this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.parentNode = null;
  this.className = ""; this.attrs = {}; this.style = makeStyle(); this.dataset = {}; this.listeners = [];
  this._text = ""; this.hidden = false; this.disabled = false;
}
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this._text + this.childNodes.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) {
    this.childNodes.forEach(function (c) { c.parentNode = null; });
    this.childNodes = []; this._text = String(v);
  },
});
["id", "href", "type", "title"].forEach(function (name) {
  Object.defineProperty(El.prototype, name, {
    get: function () { return this.attrs[name] || ""; },
    set: function (v) { this.attrs[name] = String(v); },
  });
});
Object.defineProperty(El.prototype, "tabIndex", {
  get: function () { return this.attrs.tabindex === undefined ? -1 : Number(this.attrs.tabindex); },
  set: function (v) { this.attrs.tabindex = String(v); },
});
Object.defineProperty(El.prototype, "children", {
  get: function () { return this.childNodes.filter(function (c) { return c.nodeType === 1; }); },
});
Object.defineProperty(El.prototype, "firstElementChild", { get: function () { return this.children[0] || null; } });
Object.defineProperty(El.prototype, "nextElementSibling", {
  get: function () {
    if (!this.parentNode) return null;
    var kids = this.parentNode.children;
    return kids[kids.indexOf(this) + 1] || null;
  },
});
Object.defineProperty(El.prototype, "classList", {
  get: function () {
    var node = this;
    function names() { return node.className.split(/\s+/).filter(Boolean); }
    return {
      contains: function (c) { return names().indexOf(c) >= 0; },
      add: function (c) { if (names().indexOf(c) < 0) node.className = names().concat([c]).join(" "); },
      remove: function (c) { node.className = names().filter(function (n) { return n !== c; }).join(" "); },
      toggle: function (c, on) {
        var has = names().indexOf(c) >= 0;
        var want = on === undefined ? !has : !!on;
        if (want && !has) this.add(c);
        if (!want && has) this.remove(c);
        return want;
      },
    };
  },
});
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return this.attrs[k] === undefined ? null : this.attrs[k]; };
El.prototype.hasAttribute = function (k) { return this.attrs[k] !== undefined; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
function detach(c) {
  if (c.parentNode) {
    var kids = c.parentNode.childNodes;
    kids.splice(kids.indexOf(c), 1);
    c.parentNode = null;
  }
}
El.prototype.appendChild = function (c) { detach(c); c.parentNode = this; this.childNodes.push(c); return c; };
El.prototype.insertBefore = function (c, ref) {
  if (!ref) return this.appendChild(c);
  detach(c); c.parentNode = this; this.childNodes.splice(this.childNodes.indexOf(ref), 0, c); return c;
};
El.prototype.removeChild = function (c) { detach(c); return c; };
El.prototype.replaceChild = function (n, o) {
  detach(n); var i = this.childNodes.indexOf(o); this.childNodes[i] = n; n.parentNode = this; o.parentNode = null;
  return o;
};
El.prototype.remove = function () { detach(this); };
TextNode.prototype.remove = El.prototype.remove;
El.prototype.contains = function (n) {
  for (; n; n = n.parentNode) if (n === this) return true;
  return false;
};
/* The selector subset the widgets use: tag, .class, [attr], [attr="v"],
   :not([attr="v"]) — compound only, comma lists allowed. */
function matchesCompound(node, sel) {
  var m = /^([a-zA-Z][\w-]*)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)((?::not\(\[[^\]]+\]\))*)$/.exec(sel);
  if (!m) throw new Error("unsupported selector: " + sel);
  if (m[1] && node.tagName !== m[1].toUpperCase()) return false;
  var classes = m[2].split(".").filter(Boolean);
  for (var i = 0; i < classes.length; i++) if (!node.classList.contains(classes[i])) return false;
  function attrOk(tok) {
    var a = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(tok);
    if (!a) throw new Error("unsupported attribute selector: " + tok);
    var v = node.getAttribute(a[1]);
    return a[2] === undefined ? v !== null : v === a[2];
  }
  var attrs = m[3].match(/\[[^\]]+\]/g) || [];
  for (var j = 0; j < attrs.length; j++) if (!attrOk(attrs[j])) return false;
  var nots = m[4].match(/\[[^\]]+\]/g) || [];
  for (var k = 0; k < nots.length; k++) if (attrOk(nots[k])) return false;
  return true;
}
El.prototype.matches = function (sel) {
  var node = this;
  return sel.split(",").some(function (s) { return matchesCompound(node, s.trim()); });
};
El.prototype.querySelectorAll = function (sel) {
  var found = [];
  (function walk(n) {
    n.children.forEach(function (c) { if (c.matches(sel)) found.push(c); walk(c); });
  })(this);
  return found;
};
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
function addListener(type, fn, opts) {
  var capture = opts === true || !!(opts && opts.capture);
  this.listeners.push({ type: type, fn: fn, capture: capture });
}
function removeListener(type, fn, opts) {
  var capture = opts === true || !!(opts && opts.capture);
  this.listeners = this.listeners.filter(function (l) {
    return !(l.type === type && l.fn === fn && l.capture === capture);
  });
}
El.prototype.addEventListener = addListener;
El.prototype.removeEventListener = removeListener;
El.prototype.getBoundingClientRect = function () { return { top: 0, left: 0, width: 0, height: 0 }; };
El.prototype.offsetHeight = 0; El.prototype.scrollHeight = 0; El.prototype.clientHeight = 0;
El.prototype.focus = function () { document.activeElement = this; dispatch(this, "focusin"); };
El.prototype.click = function () { return dispatch(this, "click"); };

function dispatch(target, type, init) {
  var ev = {
    type: type, target: target, defaultPrevented: false, _stop: false, _stopNow: false,
    preventDefault: function () { this.defaultPrevented = true; },
    stopPropagation: function () { this._stop = true; },
    stopImmediatePropagation: function () { this._stop = true; this._stopNow = true; },
  };
  Object.keys(init || {}).forEach(function (k) { ev[k] = init[k]; });
  var path = [];
  for (var n = target; n; n = n.parentNode) path.push(n);
  if (path[path.length - 1] === document.documentElement) path.push(document);
  function run(node, phase) {
    node.listeners.slice().forEach(function (l) {
      if (ev._stopNow || l.type !== type) return;
      if (phase !== null && l.capture !== phase) return;
      l.fn.call(node, ev);
    });
  }
  for (var i = path.length - 1; i >= 1 && !ev._stop; i--) run(path[i], true);
  if (!ev._stop) run(target, null);
  for (var j = 1; j < path.length && !ev._stop; j++) run(path[j], false);
  return ev;
}

var htmlEl = new El("html"), headEl = new El("head"), bodyEl = new El("body"), rootEl = new El("div");
rootEl.id = "root";
htmlEl.appendChild(headEl); htmlEl.appendChild(bodyEl); bodyEl.appendChild(rootEl);
htmlEl.scrollWidth = 760; htmlEl.scrollHeight = 900;
var document = {
  documentElement: htmlEl, head: headEl, body: bodyEl, activeElement: bodyEl, listeners: [],
  fullscreenEnabled: false,
  createElement: function (t) { return new El(t); },
  createTextNode: function (t) { return new TextNode(t); },
  getElementById: function (id) { return htmlEl.querySelector("[id=\"" + id + "\"]"); },
  querySelector: function (s) { return htmlEl.querySelector(s); },
  querySelectorAll: function (s) { return htmlEl.querySelectorAll(s); },
  addEventListener: addListener, removeEventListener: removeListener,
};
var winListeners = {};
var hostFrame = { postMessage: function (m) { posted.push(m); } };
var window = {
  parent: hostFrame,
  addEventListener: function (k, f) { winListeners[k] = f; },
  matchMedia: function () { return { matches: false }; },
  scrollTo: function () {}, innerHeight: 800, scrollY: 0,
};
function host(msg) { winListeners.message({ source: hostFrame, data: msg }); }
function sent(method) { return posted.filter(function (m) { return m.method === method; }); }
function answer(method, result, error) {
  var req = sent(method).slice(-1)[0];
  host(error ? { jsonrpc: "2.0", id: req.id, error: error } : { jsonrpc: "2.0", id: req.id, result: result });
}
function findAll(node, pred, acc) {
  acc = acc || [];
  (node.children || []).forEach(function (c) { if (pred(c)) acc.push(c); findAll(c, pred, acc); });
  return acc;
}
"""

_WIDGET_TAILS = {
    "game-cards": ('  startWidget("gamelib-game-cards");\n})();', apps.GAME_CARDS_HTML),
    "eval-card": ('  startWidget("gamelib-eval-card");\n})();', apps_eval.EVAL_CARD_HTML),
}


def run_widget(widget: str, probe: str) -> dict:
    """Run one widget's whole script under MINI_DOM with ``probe`` spliced in
    after startWidget(); the probe prints one JSON object."""
    tail, html = _WIDGET_TAILS[widget]
    script = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
    assert script.count(tail) == 1, widget
    script = script.replace(tail, tail.split("\n")[0] + "\n" + probe + "\n})();")
    assert NODE is not None
    proc = subprocess.run([NODE, "-e", MINI_DOM + script], capture_output=True, text=True,
                          timeout=60, check=False)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


_STARTUP_PROBE = r"""
  (async function () {
    var out = {};
    var first = root.firstElementChild;
    out.before = first ? first.className : null;
    out.beforePanels = first ? findAll(first, function (n) { return n.classList.contains("sk-panel"); }).length : 0;
    out.beforeLines = first ? findAll(first, function (n) { return n.classList.contains("sk-line"); }).length : 0;
    out.beforeChips = first ? findAll(first, function (n) { return n.classList.contains("sk-chip"); }).length : 0;
    out.beforeCovers = first ? findAll(first, function (n) { return n.classList.contains("sk-thumb"); }).length : 0;
    if (MODE === "error") answer("ui/initialize", null, { code: -32603, message: "nope" });
    else if (MODE === "timeout") flushTimers();
    else answer("ui/initialize", { hostCapabilities: {}, hostContext: { theme: "dark" } });
    await tick();
    out.initialized = sent("ui/notifications/initialized").length;
    out.theme = document.documentElement.dataset.theme || null;
    if (INPUT) host({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: INPUT });
    out.afterInput = root.firstElementChild ? root.firstElementChild.className : null;
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
           params: { structuredContent: { nothing: true } } });
    out.afterResult = root.textContent;
    console.log(JSON.stringify(out));
  })();
"""


def _startup(widget: str, mode: str, tool_input: dict | None) -> dict:
    probe = ("  var MODE = " + json.dumps(mode) + ";\n  var INPUT = " + json.dumps(tool_input) + ";\n"
             + _STARTUP_PROBE)
    return run_widget(widget, probe)


@unittest.skipUnless(NODE, "node is not installed")
class StartupBehaviourTests(unittest.TestCase):
    """Item 6 + bridge: the neutral skeleton, its swap, and the handshake."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.detail = _startup("game-cards", "ok", {"arguments": {"name": "Hades II", "media": True}})
        cls.grid = _startup("game-cards", "error", {"arguments": {"vibes": ["roguelike"], "limit": 12}})
        cls.named = _startup("game-cards", "timeout",
                             {"arguments": {}, "_meta": {"toolName": "get_game_detail"}})
        cls.evaluation = _startup("eval-card", "ok", None)

    def test_the_neutral_skeleton_is_up_before_any_tool_input(self) -> None:
        out = self.detail
        self.assertEqual(out["before"], "skel skel-neutral")
        # one panel: a cover block, three lines and a chip row
        self.assertEqual((out["beforePanels"], out["beforeCovers"], out["beforeLines"], out["beforeChips"]),
                         (1, 1, 3, 3))

    def test_a_detail_tool_input_swaps_in_the_detail_skeleton(self) -> None:
        self.assertEqual(self.detail["afterInput"], "skel skel-detail")

    def test_a_list_tool_input_swaps_in_the_grid_skeleton(self) -> None:
        self.assertEqual(self.grid["before"], "skel skel-neutral")
        self.assertEqual(self.grid["afterInput"], "skel skel-grid")

    def test_the_tool_name_decides_the_shape_when_known(self) -> None:
        # empty arguments would read as the grid; the name says detail
        self.assertEqual(self.named["afterInput"], "skel skel-detail")

    def test_the_evaluation_card_skips_the_neutral_stage(self) -> None:
        self.assertEqual(self.evaluation["before"], "skel skel-eval")

    def test_initialized_follows_only_a_real_initialize_answer(self) -> None:
        self.assertEqual(self.detail["initialized"], 1)
        self.assertEqual(self.detail["theme"], "dark")
        self.assertEqual(self.evaluation["initialized"], 1)
        self.assertEqual(self.grid["initialized"], 0)          # an error answer
        self.assertEqual(self.named["initialized"], 0)         # no answer within the timeout

    def test_a_result_still_renders_after_a_failed_handshake(self) -> None:
        for out in (self.grid, self.named, self.detail):
            self.assertEqual(out["afterResult"], "Nothing to display.")
        self.assertEqual(self.evaluation["afterResult"], "Nothing to display.")


_FOCUS_PROBE = r"""
  (function () {
    var out = {};
    function name() { var a = document.activeElement; return a.getAttribute("aria-label") || a.textContent; }
    function key(k, shift) {
      return dispatch(document.activeElement, "keydown", { key: k, shiftKey: !!shift }).defaultPrevented;
    }
    var trigger = document.body.appendChild(el("button", "thumb", "trigger"));
    var outside = document.body.appendChild(el("button", "outside", "outside"));
    var shots = [{ full: "https://x/1.jpg" }, { full: "https://x/2.jpg" }];
    openCarousel(shots, 0, "Hades II", trigger);
    var panel = document.querySelector('[role="dialog"]');
    out.opened = name();
    out.shiftTabFromFirst = [key("Tab", true), name()];
    out.tabFromLast = [key("Tab"), name()];
    document.querySelector(".car-prev").focus();
    out.tabFromMiddle = [key("Tab"), name()];
    // Every focusable counts, not only buttons: a link and a tabindex=0 node.
    var link = panel.appendChild(el("a", "credit", "credit")); link.href = "https://x";
    var stop = panel.appendChild(el("span", "stop", "stop")); stop.tabIndex = 0;
    var skipped = panel.appendChild(el("span", "skipped", "skipped")); skipped.tabIndex = -1;
    document.querySelector(".overlay-close").focus();
    out.shiftTabToLastFocusable = [key("Tab", true), name()];
    out.tabFromNewLast = [key("Tab"), name()];
    panel.focus();
    out.tabFromPanel = [key("Tab"), name()];
    // focus that lands outside while the dialog is open is pulled back in
    outside.focus();
    out.afterOutsideFocus = name();
    // closed: no trap, focus back on the trigger, and outside focus stays put
    key("Escape");
    out.afterClose = name();
    outside.focus();
    out.outsideAfterClose = name();
    console.log(JSON.stringify(out));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class FocusTrapBehaviourTests(unittest.TestCase):
    """Bridge item 6, executed in both widgets: the lightbox keeps focus."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.outs = {widget: run_widget(widget, _FOCUS_PROBE) for widget in _WIDGET_TAILS}

    def test_tab_wraps_at_both_ends(self) -> None:
        for widget, out in self.outs.items():
            with self.subTest(widget=widget):
                self.assertEqual(out["opened"], "Close screenshots")
                self.assertEqual(out["shiftTabFromFirst"], [True, "Next screenshot"])
                self.assertEqual(out["tabFromLast"], [True, "Close screenshots"])
                self.assertEqual(out["tabFromMiddle"], [False, "Previous screenshot"])   # the browser's move
                self.assertEqual(out["tabFromPanel"], [True, "Close screenshots"])

    def test_links_and_tabindex_nodes_are_in_the_cycle(self) -> None:
        for widget, out in self.outs.items():
            with self.subTest(widget=widget):
                # the last focusable is the tabindex=0 span; tabindex=-1 is skipped
                self.assertEqual(out["shiftTabToLastFocusable"], [True, "stop"])
                self.assertEqual(out["tabFromNewLast"], [True, "Close screenshots"])

    def test_focus_outside_is_pulled_back_while_open(self) -> None:
        for widget, out in self.outs.items():
            with self.subTest(widget=widget):
                self.assertEqual(out["afterOutsideFocus"], "Close screenshots")
                self.assertEqual(out["afterClose"], "trigger")
                self.assertEqual(out["outsideAfterClose"], "outside")


_FULLSCREEN_PROBE = r"""
  (async function () {
    var out = {};
    function requests() { return sent("ui/request-display-mode").length; }
    function make(modes, onFullscreen) {
      applyHostContext({ availableDisplayModes: modes, displayMode: "inline" });
      var parent = el("div");
      var built = { n: 0 };
      var d = fullscreenOrDisclosure(parent, "Rows", function (body) {
        built.n += 1;
        body.appendChild(el("div", "row"));
      }, onFullscreen);
      return { d: d, built: built, parent: parent,
               state: function () {
                 return { expanded: d.button.getAttribute("aria-expanded"), hidden: d.body.hidden,
                          built: built.n, glyph: d.button.querySelector(".chev").textContent };
               } };
    }
    // (a) no fullscreen on offer: the disclosure opens in place, no request
    var a = make(["inline"]);
    var before = requests();
    out.noFsGlyph = a.state().glyph;
    a.d.button.click();
    out.noFs = [a.state(), requests() - before];
    // (b) offered, then refused: one request (a second click while asking
    // adds none), then in place — and later clicks stay in place
    var b = make(["inline", "fullscreen"]);
    before = requests();
    out.offeredGlyph = b.state().glyph;
    b.d.button.click(); b.d.button.click();
    out.asking = [b.state(), requests() - before];
    answer("ui/request-display-mode", null, { code: -1, message: "denied" });
    await tick(); await tick();
    out.refused = b.state();
    b.d.button.click();                                   // collapse
    b.d.button.click();                                   // reopen: no new request
    out.afterRefusal = [b.state(), requests() - before];
    // (c) granted, no handler: the block opens in place
    var c = make(["inline", "fullscreen"]);
    c.d.button.click();
    answer("ui/request-display-mode", { mode: "fullscreen" });
    await tick(); await tick();
    out.grantedInPlace = c.state();
    // (d) granted with a handler: the handler runs, nothing opens in place
    var handed = [];
    var d = make(["inline", "fullscreen"], function () { handed.push("fullscreen"); });
    d.d.button.click();
    answer("ui/request-display-mode", { mode: "fullscreen" });
    await tick(); await tick();
    out.grantedHandler = [d.state(), handed];
    console.log(JSON.stringify(out));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class FullscreenOrDisclosureTests(unittest.TestCase):
    """Item 13, executed: the one fullscreen-or-in-place control."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.out = run_widget("game-cards", _FULLSCREEN_PROBE)

    def test_without_fullscreen_it_is_a_plain_disclosure(self) -> None:
        self.assertEqual(self.out["noFsGlyph"], "▾")
        self.assertEqual(self.out["noFs"],
                         [{"expanded": "true", "hidden": False, "built": 1, "glyph": "▾"}, 0])

    def test_a_refusal_opens_in_place_and_stays_in_place(self) -> None:
        self.assertEqual(self.out["offeredGlyph"], "⤢")
        self.assertEqual(self.out["asking"],
                         [{"expanded": "false", "hidden": True, "built": 0, "glyph": "⤢"}, 1])
        self.assertEqual(self.out["refused"], {"expanded": "true", "hidden": False, "built": 1, "glyph": "▾"})
        # built once, and no second fullscreen request
        self.assertEqual(self.out["afterRefusal"],
                         [{"expanded": "true", "hidden": False, "built": 1, "glyph": "▾"}, 1])

    def test_a_grant_opens_in_place_or_hands_over(self) -> None:
        self.assertEqual(self.out["grantedInPlace"],
                         {"expanded": "true", "hidden": False, "built": 1, "glyph": "▾"})
        self.assertEqual(self.out["grantedHandler"],
                         [{"expanded": "false", "hidden": True, "built": 0, "glyph": "⤢"}, ["fullscreen"]])

    def test_both_widgets_use_it_and_keep_no_mechanism_of_their_own(self) -> None:
        self.assertIn("function fullscreenOrDisclosure(parent, text, build, onFullscreen) {",
                      apps_shared.DISCLOSURE_JS)
        for module in ("apps.py", "apps_eval.py"):
            with self.subTest(module=module):
                source = _WIDGET_SOURCES[module]
                self.assertIn("fullscreenOrDisclosure(", source)
                self.assertNotIn("disclosure(stack, text, build)", source)
                self.assertNotIn(".button.click()", source)
                self.assertNotIn("var inPlace = ", source)
                self.assertNotIn('requestDisplayMode("fullscreen").then', source)


_CARD_PROBE = r"""
  (function () {
    function chipText(chip) {
      return chip.childNodes.filter(function (c) {
        return c.nodeType === 1 && (c.classList.contains("lbl") || c.tagName === "B");
      }).map(function (c) { return c.textContent; }).join(" ");
    }
    function visible(card) {
      var bits = [];
      findAll(card, function (n) { return n.classList.contains("match"); }).forEach(function (m) {
        bits.push(m.children[0].textContent);
      });
      findAll(card, function (n) { return n.classList.contains("meta"); }).forEach(function (m) {
        bits = bits.concat(m.textContent.split(" · "));
      });
      findAll(card, function (n) { return n.classList.contains("chip"); }).forEach(function (c) {
        bits.push(chipText(c));
      });
      return bits;
    }
    var games = {
      full: { game_id: 1, name: "Hades II", match_percent: 100, hltb_main: 26.5, suggested_platform: "steam",
              playtime_hours: 2.3, metacritic_score: 93, opencritic_score: 91,
              steam_review_desc: "Overwhelmingly Positive" },
      sparse: { game_id: 2, name: "Noita", match_percent: 74.4, metacritic_score: -1 },
      bare: { game_id: 3, name: "Mystery" },
    };
    var out = {};
    Object.keys(games).forEach(function (k) {
      var card = gridCard(games[k]);
      out[k] = { label: card.getAttribute("aria-label"), visible: visible(card) };
    });
    console.log(JSON.stringify(out));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class CardLabelBehaviourTests(unittest.TestCase):
    """Item 14, executed: a grid card's accessible name IS what it shows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.out = run_widget("game-cards", _CARD_PROBE)

    def test_the_label_is_the_title_then_the_visible_bits(self) -> None:
        for key in ("full", "sparse", "bare"):
            with self.subTest(card=key):
                out = self.out[key]
                name = out["label"].split(": ", 1)[0]
                expected = name + (": " + ", ".join(out["visible"]) if out["visible"] else "")
                self.assertEqual(out["label"], expected)

    def test_the_label_reads_match_meta_and_scores_in_order(self) -> None:
        self.assertEqual(
            self.out["full"]["label"],
            "Hades II: 100% match, ~27h to beat, Steam, 2.3h played, Metacritic 93, OpenCritic 91, "
            "Steam Overwhelmingly positive",
        )
        self.assertEqual(self.out["sparse"]["label"], "Noita: 74% match")
        self.assertEqual(self.out["bare"]["label"], "Mystery")


_NUMBERS_PROBE = r"""
  (function () {
    console.log(JSON.stringify({
      counts: [999600, 9950, 1049, 999, 999.6, 1500, 114000, 2500000, 0].map(function (n) {
        return compactCount(n);
      }),
      one: compactCount(1, "review"),
      many: compactCount(9950, "review"),
    }));
  })();
"""

_CRAFT_PROBE = r"""
  (function () {
    function chips(craft) {
      var row = scoreChips({ craft: craft });
      return row ? row.children.map(function (c) {
        return [c.children[0].textContent, (c.querySelector("b") || {}).textContent, c.title];
      }) : null;
    }
    console.log(JSON.stringify({
      rawOne: chips({ positive_pct: 1, review_count: 120 }),
      rawHigh: chips({ positive_pct: 93 }),
      adjusted: chips({ adjusted: 0.88, positive_pct: 91, review_count: 114000 }),
      adjustedOne: chips({ adjusted: 1 }),
    }));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class NumberBehaviourTests(unittest.TestCase):
    """Items 7-8, executed: compactCount's units and the craft percentages."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.numbers = run_widget("game-cards", _NUMBERS_PROBE)
        cls.craft = run_widget("eval-card", _CRAFT_PROBE)

    def test_compact_count_picks_the_unit_from_the_rounded_value(self) -> None:
        self.assertEqual(self.numbers["counts"],
                         ["1M", "10k", "1k", "999", "1k", "1.5k", "114k", "2.5M", "0"])
        self.assertEqual(self.numbers["one"], "1 review")
        self.assertEqual(self.numbers["many"], "10k reviews")

    def test_positive_pct_is_already_a_percentage(self) -> None:
        # stored 0-100 (tools/assessment.py's _check_range): 1 is 1%, never 100%
        self.assertEqual(self.craft["rawOne"][0][:2], ["Reviews", "1% positive"])
        self.assertIn("(raw 1% positive)", self.craft["rawOne"][0][2])
        self.assertEqual(self.craft["rawHigh"][0][:2], ["Reviews", "93% positive"])

    def test_adjusted_is_a_fraction_rescaled(self) -> None:
        self.assertEqual(self.craft["adjusted"][0][:2], ["Reviews", "88% positive"])
        self.assertIn("(raw 91% positive), from 114k reviews", self.craft["adjusted"][0][2])
        self.assertEqual(self.craft["adjustedOne"][0][:2], ["Reviews", "100% positive"])


if __name__ == "__main__":
    unittest.main()
