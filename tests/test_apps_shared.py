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

from gamelib_mcp import apps, apps_eval, apps_shared


def shared_css_blocks() -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in sorted(vars(apps_shared).items())
        if name.endswith("_CSS") and isinstance(value, str)
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
    "a.chip", ".btn", ".disclosure", ".thumb", ".fs-btn", ".car-nav",
    ".overlay-close", '.card[role="button"]', ".hero-pill",
    '.stamp[role="button"]', "button.stamp",
)


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

    def test_stale_host_variables_are_removed(self) -> None:
        js = apps_shared.BRIDGE_JS
        self.assertIn('var HOST_TOKEN_PREFIXES = ["--color-", "--font-", "--border-", "--shadow-"];', js)
        self.assertIn("nextVars[name] = true;", js)
        self.assertIn("if (isHostToken(name)) docEl.style.removeProperty(name);", js)
        self.assertIn("appliedHostVars = nextVars;", js)

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
        self.assertIn('docEl.classList.toggle("touch", !!device.touch);', js)
        self.assertIn("} else if (!hostContext.deviceCapabilities) {", js)
        # and the fallback runs before any host context arrives
        self.assertIn("\n  applyInputFallback();\n", js)

    def test_safe_area_insets_drive_the_strip_scroll_padding(self) -> None:
        self.assertIn('docEl.style.setProperty("--gl-safe-" + side[0], extra + "px");', apps_shared.BRIDGE_JS)
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

    def test_chip_rows_keep_extensions_within_half_the_gap(self) -> None:
        a11y, chips = apps_shared.A11Y_CSS, apps_shared.CHIP_CSS
        # pointer: 8px rows / 6px columns against -4px / -3px
        self.assertIn(".chips { display: flex; gap: 8px 6px;", chips)
        self.assertIn(".chips a.chip::after { inset: -4px -3px; }", a11y)
        # touch: 32px chips + 2 x 6px = 44px, rows 12px apart, columns 8px
        self.assertIn("html.touch .chips { gap: 12px 8px; }", chips)
        self.assertIn("html.touch .chips a.chip::after { inset: -6px -4px; }", a11y)
        self.assertIn("html.touch .chip, html.touch .hero-pill { min-height: 32px; }", a11y)

    def test_extension_hosts_are_containing_blocks(self) -> None:
        self.assertIn("position: relative;", css_rule(apps_shared.CONTROLS_CSS, ".btn, .disclosure", exact=True))
        self.assertIn("position: relative;", css_rule(apps_shared.CHIP_CSS, ".chip", exact=True))
        # a clickable card holds link chips: its extension sits behind them
        self.assertIn('.card[role="button"] { position: relative; z-index: 0; }', apps_shared.A11Y_CSS)
        self.assertIn('.card[role="button"]::after { z-index: -1; }', apps_shared.A11Y_CSS)

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
            "scroll-padding-inline: calc(2px + var(--gl-safe-left)) calc(2px + var(--gl-safe-right));",
            "overscroll-behavior-x: contain;",
        ):
            self.assertIn(decl, strip)
        self.assertIn(".strip > * { scroll-snap-align: start; }", apps_shared.STRIP_CSS)
        self.assertIn('.strip::after { content: ""; flex: 0 0 max(2px, var(--gl-safe-right)); }',
                      apps_shared.STRIP_CSS)


class LifecycleTests(unittest.TestCase):
    """§1.4: teardown, unreadable results, cancellation, notices."""

    def test_teardown_flag_guards_report_size_and_host_context(self) -> None:
        sizing = apps_shared.SIZING_JS
        self.assertIn("tornDown = true;", sizing)
        self.assertIn("if (tornDown || window.__PREVIEW_DATA__) return;", sizing)
        self.assertIn("sizeTimer = null;", sizing)
        self.assertIn('if (tornDown || !ctx || typeof ctx !== "object") return;', apps_shared.BRIDGE_JS)

    def test_unparseable_result_replaces_the_skeleton_with_a_notice(self) -> None:
        js = apps_shared.TOOL_RESULT_JS
        self.assertIn('notice(root, "Couldn\'t read the result");', js)
        body = js.split("function handleToolResult(result) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("if (!rootShowsContent()) root.textContent = \"\";", body)

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

  applyHostContext({ styles: { variables: { "--color-text-primary": "#111", "--font-sans": "X" },
                               css: { fonts: "@font-face{a}" } } });
  applyHostContext({ styles: { variables: { "--color-text-primary": "#222" },
                               css: { fonts: "@font-face{a}" } } });
  out.fontNodes = fontNodes.length; out.fontText = fontNodes[0].textContent;
  out.props = props; out.removed = removed;
  applyHostContext({ styles: { css: { fonts: "@font-face{b}" } } });
  out.fontText2 = fontNodes[0].textContent; out.fontNodes2 = fontNodes.length;
  out.fontWrites = fontNodes[0].writes;
  applyHostContext({ deviceCapabilities: { touch: false, hover: true } });
  out.hostTouch = classes.touch;
  applyHostContext({ theme: "dark" });
  out.touchAfterPartial = classes.touch;
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

    def test_stale_variables_removed_and_fresh_ones_kept(self) -> None:
        self.assertEqual(self.out["props"].get("--color-text-primary"), "#222")
        self.assertNotIn("--font-sans", self.out["props"])
        self.assertEqual(self.out["removed"], ["--font-sans"])

    def test_host_device_capabilities_win_and_survive_partial_updates(self) -> None:
        self.assertFalse(self.out["hostTouch"])
        self.assertFalse(self.out["touchAfterPartial"])

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


class SplicedVerbatimTests(unittest.TestCase):
    def test_every_shared_constant_appears_in_both_widgets(self) -> None:
        blocks = shared_blocks()
        self.assertGreater(len(blocks), 20)
        for name, block in blocks:
            with self.subTest(block=name):
                self.assertIn(block, apps.GAME_CARDS_HTML)
                self.assertIn(block, apps_eval.EVAL_CARD_HTML)


if __name__ == "__main__":
    unittest.main()
