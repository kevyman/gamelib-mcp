"""Tests for the MCP Apps evaluation-card widget.

Same approach as tests/test_apps.py: there is no headless-DOM harness for the
widget JS, so these assert the module's registration/CSP contract and the
presence of the render call-sites and host-quirk workarounds that the layout
depends on, rather than executing the script — except the action row, whose
three small builders also run under Node (``ActionRowBehaviourTests``).
"""

import difflib
import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from fastmcp import Client, FastMCP

from gamelib_mcp import apps_eval, apps_shared

_WIDGET_DIR = Path(apps_eval.__file__).parent


def shared_blocks() -> list[tuple[str, str]]:
    """The (name, text) pairs both widgets splice into their HTML."""
    return [
        (name, value)
        for name, value in sorted(vars(apps_shared).items())
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    ]


class EvalCardResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_registered_and_serves_widget(self) -> None:
        mcp = FastMCP("test")
        apps_eval.register_eval_app(mcp)
        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = [str(r.uri) for r in resources]
            self.assertIn(apps_eval.EVAL_CARD_URI, uris)
            content = await client.read_resource(apps_eval.EVAL_CARD_URI)
            html = content[0].text

        self.assertEqual(html, apps_eval.EVAL_CARD_HTML)
        # The hand-rolled bridge must speak the MCP Apps handshake and the
        # result notification, link out through the host, and handle preview
        # injection for local review.
        for marker in (
            "ui/initialize",
            "appInfo",              # required by the ext-apps SDK schema
            "ui/notifications/initialized",
            "ui/notifications/tool-result",
            "ui/notifications/size-changed",
            "ui/open-link",         # trailer link-out goes through the host
            "__PREVIEW_DATA__",
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)

    async def test_app_config_points_at_the_widget_uri(self) -> None:
        self.assertEqual(apps_eval.EVAL_CARD_APP.resource_uri, apps_eval.EVAL_CARD_URI)


class EvalCardUriTests(unittest.TestCase):
    def test_uri_is_content_hashed_and_reflects_current_html(self) -> None:
        expected = (
            "ui://gamelib/eval-card-"
            + hashlib.sha1(apps_eval.EVAL_CARD_HTML.encode()).hexdigest()[:8]
            + ".html"
        )
        self.assertEqual(apps_eval.EVAL_CARD_URI, expected)

    def test_uri_changes_when_the_html_changes(self) -> None:
        # Hosts cache ui:// resources by URI, so a widget edit must mint a URI
        # the host has never seen — that only holds if the hash covers the HTML.
        edited = apps_eval.EVAL_CARD_HTML + "<!-- tweak -->"
        other = (
            "ui://gamelib/eval-card-"
            + hashlib.sha1(edited.encode()).hexdigest()[:8]
            + ".html"
        )
        self.assertNotEqual(other, apps_eval.EVAL_CARD_URI)

    def test_uri_is_distinct_from_the_game_cards_widget(self) -> None:
        from gamelib_mcp import apps

        self.assertNotEqual(apps_eval.EVAL_CARD_URI, apps.GAME_CARDS_URI)


class EvalCardCSPTests(unittest.TestCase):
    def test_resource_domains_are_exactly_the_media_hosts(self) -> None:
        # resource_domains feeds img-src/media-src: IGDB art, Steam capsules,
        # Steam screenshot + movie hosts (shared.*), YouTube thumbnails.
        self.assertEqual(
            apps_eval._EVAL_CARD_CSP.resource_domains,
            [
                "https://images.igdb.com",
                "https://cdn.cloudflare.steamstatic.com",
                "https://cdn.akamai.steamstatic.com",
                "https://shared.akamai.steamstatic.com",
                "https://shared.cloudflare.steamstatic.com",
                "https://i.ytimg.com",
                "https://assets.claude.ai",   # the host's own font files
            ],
        )

    def test_frame_domains_are_only_the_nocookie_youtube_embed(self) -> None:
        self.assertEqual(
            apps_eval._EVAL_CARD_CSP.frame_domains,
            ["https://www.youtube-nocookie.com"],
        )

    def test_no_other_csp_directives_are_opened_up(self) -> None:
        self.assertIsNone(apps_eval._EVAL_CARD_CSP.connect_domains)
        self.assertIsNone(apps_eval._EVAL_CARD_CSP.base_uri_domains)

    def test_embed_url_uses_the_allowlisted_nocookie_host(self) -> None:
        # A src the CSP doesn't cover renders as a silently blank frame.
        self.assertIn(
            'https://www.youtube-nocookie.com/embed/" + encodeURIComponent(trailer.video_id)',
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertNotIn("https://www.youtube.com/embed/", apps_eval.EVAL_CARD_HTML)


class EvalCardHtmlSanityTests(unittest.TestCase):
    def test_widget_is_self_contained(self) -> None:
        # Dependency-free by design: no CDN script, no external stylesheet.
        lowered = apps_eval.EVAL_CARD_HTML.lower()
        self.assertNotIn("<script src", lowered)
        self.assertNotIn("<link", lowered)

    def test_payload_text_never_goes_through_innerhtml(self) -> None:
        # The widget's only escaping mechanism is el()'s use of textContent.
        self.assertNotIn("innerHTML", apps_eval.EVAL_CARD_HTML)

    def test_every_verdict_has_a_stamp_label_and_color(self) -> None:
        # The words come from the shared, registry-checked VERDICT_LABELS (the
        # stamp uppercases them in CSS); this map only picks the tier fill.
        for verdict, cls in (
            ("buy_now", "stamp-good"),
            ("wishlist_for_sale", "stamp-ok"),
            ("try_demo", "stamp-plain"),
            ("play_what_you_own", "stamp-plain"),
            ("skip", "stamp-bad"),
        ):
            self.assertIn(f'{verdict}: "{cls}"', apps_eval.EVAL_CARD_HTML)
            self.assertIn(verdict, apps_shared.VERDICT_LABELS)
        self.assertIn('var text = label("verdict", verdict);', apps_eval.EVAL_CARD_HTML)
        for tier in ("good", "ok", "bad"):
            self.assertIn(
                f".stamp-{tier} {{ background: var(--gl-{tier}); color: var(--gl-surface); }}",
                apps_eval.EVAL_CARD_HTML,
            )

    def test_the_stamp_keeps_its_brand_shape(self) -> None:
        # One of the two deliberate brand elements left (spec §1.5): strong
        # 2px border, hard 3px shadow, -3deg tilt.
        css = apps_eval.EVAL_CARD_HTML.split("<style>")[1].split("</style>")[0]
        start = css.index("  .stamp {")
        rule = css[start:css.index("}", start)]
        for decl in (
            "border: 2px solid var(--gl-border-strong);",
            "box-shadow: 3px 3px 0 var(--gl-border-strong);",
            "transform: rotate(-3deg);",
            "text-transform: uppercase;",
        ):
            self.assertIn(decl, rule)

    def test_render_branches_cover_the_whole_response_contract(self) -> None:
        # package -> full card, verdict -> note, else empty. There is no
        # "voided" branch: void_assessment is its own tool and is not bound to
        # the card, so record_assessment responses never carry that key.
        for marker in (
            "if (data && data.package)",
            "else if (data && data.verdict)",
            '"Recorded — " + label("verdict", data.verdict) + name',
            '"Nothing to display."',
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("data.voided", apps_eval.EVAL_CARD_HTML)

    def test_the_note_card_reads_as_words(self) -> None:
        # "Recorded — Play what you own · Slay the Spire II", stamp beside it:
        # the verdict goes through label(), never the raw literal.
        self.assertIn('var name = data && data.name ? " · " + data.name : "";',
                      apps_eval.EVAL_CARD_HTML)
        self.assertNotIn('"Recorded: "', apps_eval.EVAL_CARD_HTML)
        start = apps_eval.EVAL_CARD_HTML.index("function noteCard(text, verdict)")
        body = apps_eval.EVAL_CARD_HTML[start:apps_eval.EVAL_CARD_HTML.index("function render(", start)]
        self.assertIn("var stamp = verdict ? stampNode(verdict) : null;", body)

    def test_trailer_falls_back_when_the_media_element_fails(self) -> None:
        # Valve's constructed mp4 URLs are undocumented legacy surface and a
        # host may strip media-src — both land as source errors, which have to
        # be caught in the capture phase because they don't bubble.
        self.assertIn('video.addEventListener("error", function () {', apps_eval.EVAL_CARD_HTML)
        self.assertIn("posterFallback(hero, trailer);", apps_eval.EVAL_CARD_HTML)
        self.assertIn('video.preload = "none";', apps_eval.EVAL_CARD_HTML)

    def test_pedigree_strip_renders_from_the_package(self) -> None:
        for marker in (
            "pedigreeNode(parent, pkg.pedigree)",
            'section(parent, "From the studio")',
            'el("div", "ped-head", headline)',
            'el("div", "ped-pub", "published by " + ped.publisher_name)',
            '"You\'ve played " + (num(record.played_count) || 0) + " of "',
            # Zero is authoritative NOT-played; "0h played" must never render.
            'own.playtime_hours > 0 ? hoursLabel(own.playtime_hours) : null',
            '" — avg " + avg + "/10."',
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)

    def test_pedigree_badge_prefers_his_rating_over_the_critic_score(self) -> None:
        start = apps_eval.EVAL_CARD_HTML.index("function pedigreeBadges(item)")
        end = apps_eval.EVAL_CARD_HTML.index("function pedigreeNode(", start)
        badges = apps_eval.EVAL_CARD_HTML[start:end]
        self.assertIn("if (item.owned && rating != null) {", badges)
        self.assertIn("chips.push(youChip(rating));", badges)
        self.assertIn("} else if (critic != null && critic >= 0) {", badges)
        self.assertIn("chips.push(criticsChip(critic));", badges)

    def test_the_damper_branch_renders_the_header_line_alone(self) -> None:
        start = apps_eval.EVAL_CARD_HTML.index("function pedigreeNode(parent, ped)")
        end = apps_eval.EVAL_CARD_HTML.index(
            'var strip = el("div", "strip ped-strip")', start
        )
        self.assertIn("if (!items.length) return;", apps_eval.EVAL_CARD_HTML[start:end])

    def test_why_care_renders_a_chip_per_kind_under_the_pitch(self) -> None:
        # The eval card is the only one that renders why_care (it is authored
        # content, not a neutral fact about the game), and it sits directly
        # under the elevator pitch — both inside the one pitch panel.
        # Mixed-case words, uppercased by the eyebrow style (screen readers
        # then read "People", not P-E-O-P-L-E).
        for kind, label, cls in (
            ("people", "People", "wc-people"),
            ("studio", "Studio", "wc-studio"),
            ("anticipation", "Hype", "wc-hype"),
            ("moment", "Moment", "wc-moment"),
        ):
            self.assertIn(f'{kind}: ["{label}", "{cls}"]', apps_eval.EVAL_CARD_HTML)
        self.assertIn(
            'if (pres.elevator_pitch) box.appendChild(el("p", "pitch", pres.elevator_pitch));\n'
            "    whyCareNode(box, pres);",
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn("list(pres.why_care)", apps_eval.EVAL_CARD_HTML)
        self.assertIn('el("div", "wc-line")', apps_eval.EVAL_CARD_HTML)

    def test_hype_counts_are_never_rendered(self) -> None:
        # `hypes` rides in the pedigree payload for completeness; the card does
        # not argue from popularity, so no renderer may read it.
        self.assertNotIn("hypes", apps_eval.EVAL_CARD_HTML)

    def test_anchor_cards_are_neutral_and_carry_the_shared_chips(self) -> None:
        # The live card lit up "Cyberpunk 2077 6.6h" — a game he bounced off —
        # in endorsement green. The card stays neutral; its chips are the
        # shared library chips ("You 6/10", "Played 6.6h", "Status
        # Abandoned"), each tiered by what it says.
        html = apps_eval.EVAL_CARD_HTML
        self.assertIn(
            "var chips = chipRow([youChip(a.rating), playedChip(hours, hours === 0),\n"
            "        statusChip(a.completion_status)]);",
            html,
        )
        self.assertIn("  .anchor {\n", html)
        self.assertIn("    background: var(--gl-inset);\n    font-variant-numeric", html)
        # the glyph pills and their local colors are gone
        for gone in ("an-state", "an-good", "an-bad", "an-warn", "COMPLETION"):
            self.assertNotIn(gone, html)


class EvalCardLayoutTests(unittest.TestCase):
    """The v2 layout (field feedback: "too busy, not ordered naturally").

    Source-presence style like the rest of this module: the render call-sites
    and the panels they build, in the order the card assembles them.
    """

    @staticmethod
    def _function(name: str, until: str) -> str:
        html = apps_eval.EVAL_CARD_HTML
        start = html.index(f"function {name}(")
        return html[start:html.index(f"function {until}(", start)]

    def test_panels_assemble_in_the_reading_order(self) -> None:
        # Spec §2.2: inline header (identity, scores, the call) → the verdict
        # in words → media → the action row → the failure notice; the
        # breakdown bodies (in place, and fullscreen) come after all of it.
        body = self._function("evalCard", "noteCard")
        order = [
            "inlineCard(wrap, pkg)",
            "actionsNode(wrap, pkg, more, appid)",
            "errorsNode(wrap,",
            "wrap.appendChild(inPlaceBody)",
            'el("div", "fs-breakdown")',
        ]
        positions = [body.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        inline = self._function("inlineCard", "evalCard")
        order = [
            "headerNode(pkg)",
            "pitchNode(wrap, pkg)",
            "mediaSlot(wrap, pkg.media || {}, (pkg.game || {}).name)",
        ]
        positions = [inline.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_the_call_sits_under_the_scores_before_the_pitch(self) -> None:
        # The facts block used to close the card ~2,400px down; it is now
        # inside the header panel, after the score row and craft note and
        # before the summary/pitch panel is even built.
        header = self._function("headerNode", "craftPercent")
        order = [
            "head.appendChild(coverNode(game))",
            "var stamp = stampNode(pkg.verdict);",
            "var chips = scoreChips(pkg);",
            'el("div", "craft-note", pres.craft_note)',
            "var call = callNode(pkg);",
        ]
        positions = [header.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        call = self._function("callNode", "pitchNode")
        self.assertIn("factChips(pkg, row);", call)
        self.assertIn('scoreChip({ label: String(f), tier: "bad", cls: "flag" })', call)
        self.assertIn('el("span", "call-label", "The call")', call)

    def test_the_breakdown_stays_out_of_the_inline_tier(self) -> None:
        # The height budget (≤900px at 760 with media, ≤1,300px at 360) holds
        # only because the evidence sections are one click away: none of them
        # may be called from the inline builders.
        inline = (
            self._function("inlineCard", "evalCard")
            + self._function("headerNode", "craftPercent")
            + self._function("pitchNode", "whyCareNode")
        )
        breakdown = self._function("breakdownNode", "actionsNode")
        sections = (
            "forYouNode(",
            "anchorsNode(",
            "lineageNode(",
            "similarNode(",
            "pedigreeNode(",
            "pastNode(",
            "errorDetailNode(",
        )
        for section_call in sections:
            self.assertNotIn(section_call, inline)
            self.assertIn(section_call, breakdown)
        # …and the order inside the breakdown is the spec's.
        positions = [breakdown.index(c) for c in sections]
        self.assertEqual(positions, sorted(positions))

    def test_at_most_two_actions_and_none_without_either(self) -> None:
        # One action row, built only when there is something to disclose or a
        # store page to open; it holds the disclosure button and, at most, the
        # store button — never a third action.
        body = self._function("evalCard", "noteCard")
        self.assertIn("var more = hasBreakdown(pkg);", body)
        self.assertIn("var appid = storeAppid(pkg);", body)
        self.assertIn(
            "var inPlaceBody = more || appid ? actionsNode(wrap, pkg, more, appid) : null;",
            body,
        )
        actions = self._function("actionsNode", "syncDisplayMode")
        self.assertEqual(actions.count('el("div", "actions")'), 1)
        self.assertEqual(actions.count("disclosure(row,"), 1)
        # Two storeButton calls, on mutually exclusive branches (store alone,
        # or store after the breakdown) — so the row never exceeds two.
        self.assertEqual(actions.count("storeButton(row, appid);"), 2)
        self.assertNotIn("row.appendChild(", actions)
        self.assertIn(
            'disclosure(row, "Full breakdown", function (body) { breakdownNode(body, pkg); });',
            actions,
        )

    def test_store_button_follows_the_breakdown_only_with_an_appid(self) -> None:
        actions = self._function("actionsNode", "syncDisplayMode")
        # Second, after "Full breakdown", and only when an appid resolved.
        self.assertLess(
            actions.index('disclosure(row, "Full breakdown"'),
            actions.index("if (appid) storeButton(row, appid);"),
        )
        # No breakdown: the row is the store button alone (evalCard only
        # reaches this branch when an appid exists).
        alone = actions[actions.index("if (!more) {"):actions.index("var d = disclosure(")]
        self.assertIn("storeButton(row, appid);", alone)
        self.assertIn("return null;", alone)

    def test_store_button_is_a_secondary_btn_opening_the_steam_page(self) -> None:
        button = self._function("storeButton", "actionsNode")
        self.assertIn('el("button", "btn act-store", "Store page ↗")', button)
        self.assertNotIn("primary", button)
        self.assertIn(
            'openLink("https://store.steampowered.com/app/" + appid + "/");', button
        )
        # Only a positive integer appid yields a button; anything else is null.
        appid = self._function("storeAppid", "storeButton")
        self.assertIn("var appid = (pkg.game || {}).steam_appid;", appid)
        for guard in ('typeof appid === "number"', "appid > 0", "Math.floor(appid) === appid"):
            self.assertIn(guard, appid)
        # Below 420px the two buttons stack full-width.
        html = apps_eval.EVAL_CARD_HTML
        phone = html[html.index("@media (max-width: 419px) {"):]
        phone = phone[:phone.index("\n  }\n")]
        self.assertIn(".actions { flex-direction: column; }", phone)
        self.assertIn(".actions .act-store { width: 100%; }", phone)

    def test_full_breakdown_requests_fullscreen_and_falls_back_in_place(self) -> None:
        actions = self._function("actionsNode", "syncDisplayMode")
        for marker in (
            'if (inPlace || !canFullscreen() || d.button.getAttribute("aria-expanded") === "true") return;',
            "ev.stopPropagation();",
            'requestDisplayMode("fullscreen").then(function (mode) {',
            'if (mode === "fullscreen") return;',
            "inPlace = true;",
            "d.button.click();",
            "}, true);",                    # capture: runs before the disclosure's own handler
        ):
            self.assertIn(marker, actions)
        # Fullscreen: the inline card stays, the breakdown builds once below
        # it, CSS swaps the action row out, and size reports go quiet.
        html = apps_eval.EVAL_CARD_HTML
        for marker in (
            'html[data-display-mode="fullscreen"] .fs-breakdown { display: flex; }',
            'html[data-display-mode="fullscreen"] .actions .act-breakdown,',
            'if (fs && !fs.built && currentDisplayMode() === "fullscreen") {',
            'attributeFilter: ["data-display-mode"]',
            'if (currentDisplayMode() !== "fullscreen") reportInlineSize();',
            "resizeObserver = new ResizeObserver(function () { reportSize(); });",
        ):
            self.assertIn(marker, html)

    def test_the_score_chips_live_in_the_header_panel(self) -> None:
        # The standalone "CRAFT & FIT" panel is gone — it held two chips.
        self.assertIn("var chips = scoreChips(pkg);", apps_eval.EVAL_CARD_HTML)
        self.assertIn('el("div", "chips head-chips")', apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("Craft & fit", apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("scoresNode", apps_eval.EVAL_CARD_HTML)

    def test_every_score_renders_through_the_shared_chip(self) -> None:
        # Spec §1.3: one chip, tier color, the source as label text — the
        # Metacritic brand square (and its #6c3/#fc3/#f00) is gone.
        for marker in (
            'scoreChip({ label: "Metacritic", value: Math.round(mc), tier: mcTier(mc) })',
            'scoreChip({ label: "OpenCritic", value: Math.round(oc), tier: ocTier(oc) })',
            # "Reviews ▬ 93% positive 114k" — the sample-adjusted wording
            # lives in the tooltip
            'label: "Reviews", value: pct + "% positive", tier: craftTier(pct), meter: pct,',
            "var count = compactCount(craft.review_count);",
            "aux: count,",
            'title: "Sample-adjusted share of positive reviews"',
            'label: "Trend", value: traj[0], tier: traj[1],',
            # "Fit Strong", not "Fit Strong fit"
            'var fit = String(pkg.fit_call).replace(/\\s+fit$/i, "");',
            'label: "Fit", value: fit.charAt(0).toUpperCase() + fit.slice(1),',
            "num(craft.metacritic_score)",
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)
        for hex_color in ("#6c3", "#fc3", "#f00"):
            self.assertNotIn(hex_color, apps_eval.EVAL_CARD_HTML.lower())
        self.assertNotIn('" · n="', apps_eval.EVAL_CARD_HTML)
        # Local tier helpers would drift from the shared ones.
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function mcTier("), 1)
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function hoursLabel("), 1)
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function money("), 1)

    def test_facts_flags_and_past_verdicts_read_as_words(self) -> None:
        # Platform ids and stored verdict literals go through label(); flags
        # are danger-tier chips; failures are one named notice.
        for marker in (
            '" on " + label("platform", price.platform)',
            'if (p.verdict) parts.push(label("verdict", p.verdict));',
            'scoreChip({ label: String(f), tier: "bad", cls: "flag" })',
            '.map(function (p) { return label("platform", p); });',
            "var node = notice(parent, errors.map(errorItem));",
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("some data unavailable", apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("parts.push(String(p.verdict))", apps_eval.EVAL_CARD_HTML)

    def test_the_craft_note_renders_under_the_chips(self) -> None:
        self.assertIn(
            'if (pres.craft_note) head.appendChild(el("div", "craft-note", pres.craft_note));',
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn('"cover info stamp" "cover scores scores" "cover note note"',
                      apps_eval.EVAL_CARD_HTML)

    def test_media_is_one_viewer_and_one_thumb_strip(self) -> None:
        for marker in (
            'section(parent, "Media")',
            'var viewer = el("div", "hero viewer")',
            'el("div", "strip thumbs")',
            "showEntry(viewer, entries[i], shots, gameName)",
            'btn.classList.toggle("sel", i === j)',
            "select(0);",                       # trailer first when there is one
            'el("span", "thumb-play", "▶")',
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)
        # The separate screenshot panel is gone with it.
        self.assertNotIn("Screenshots", apps_eval.EVAL_CARD_HTML)

    def test_the_dead_more_chip_is_gone_from_the_media_block(self) -> None:
        # "+N more" was unclickable: the extra images are not in the payload,
        # so neither the flag nor the count is READ any more (the comment
        # explaining that is the only mention left in the source).
        self.assertNotIn("media.screenshots_truncated", apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("media.screenshot_count", apps_eval.EVAL_CARD_HTML)
        self.assertNotIn('" more"', apps_eval.EVAL_CARD_HTML.split('section(parent, "Media")')[1]
                         .split('section(parent, "Similar in your library")')[0])

    def test_screenshots_open_an_edge_to_edge_carousel(self) -> None:
        for marker in (
            "openCarousel(shots, entry.index, gameName, btn)",
            'el("div", "overlay-panel carousel")',
            ".overlay-panel.carousel {",
            "width: 100%;",
            'navButton("car-prev", "‹"',
            'navButton("car-next", "›"',
            'counter.textContent = (index + 1) + " / " + shots.length;',
            'if (ev.key === "Escape") closeOverlay();',
            'else if (ev.key === "ArrowLeft") show(index - 1);',
            'else if (ev.key === "ArrowRight") show(index + 1);',
            'stage.addEventListener("pointerup"',
            "if (Math.abs(dx) > 40) show(index + (dx < 0 ? 1 : -1));",
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)

    def test_fullscreen_is_attempted_but_never_faked(self) -> None:
        # A sandboxed host iframe without allow="fullscreen" reports
        # fullscreenEnabled false (no button), and a denied request removes the
        # button rather than leaving an inert control on the viewer.
        self.assertIn(
            "if (!document.fullscreenEnabled && !document.webkitFullscreenEnabled) return null;",
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn("var req = target.requestFullscreen || target.webkitRequestFullscreen;",
                      apps_eval.EVAL_CARD_HTML)
        self.assertIn("pending.catch(function () { btn.remove(); });", apps_eval.EVAL_CARD_HTML)
        self.assertIn("} catch (e) {\n        btn.remove();", apps_eval.EVAL_CARD_HTML)

    def test_similar_comparisons_moved_into_the_lineage_panel(self) -> None:
        # Model-authored "similar" note-cards no longer fold into the library
        # strip: mixing the two is what made the live Similar section confusing.
        self.assertIn("function lineageNode(parent, pkg, comps)", apps_eval.EVAL_CARD_HTML)
        self.assertIn('onlySimilar ? "Also similar" : "Other comparisons"',
                      apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("foldSimilar", apps_eval.EVAL_CARD_HTML)
        self.assertIn("function similarNode(parent, similar)", apps_eval.EVAL_CARD_HTML)

    def test_the_call_is_one_row_and_past_verdicts_moved_to_the_breakdown(self) -> None:
        # The closing "The call" panel is gone: its facts and flags are one
        # chip row in the header, and past verdicts are a breakdown section.
        self.assertNotIn('section(parent, "The call")', apps_eval.EVAL_CARD_HTML)
        self.assertIn('section(parent, "Past verdicts")', apps_eval.EVAL_CARD_HTML)
        for gone in ("Time & price", '"Flags"', "closingNode"):
            self.assertNotIn(gone, apps_eval.EVAL_CARD_HTML)

    def test_the_stamp_stacks_under_the_title_on_a_phone(self) -> None:
        css = apps_eval.EVAL_CARD_HTML.split("<style>")[1].split("</style>")[0]
        narrow = css[css.index("@media (max-width: 419px)"):]
        self.assertIn('"cover info" "cover stamp" "scores scores" "note note"', narrow)

    def test_counts_are_pluralized(self) -> None:
        # "Rebel Wolves · est. 2022 · 1 games" shipped to the phone.
        self.assertIn(
            'return n + (truncated ? "+" : "") + " " + word '
            '+ (n === 1 && !truncated ? "" : "s");',
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn('plural(size, "game", ped.catalog_truncated)', apps_eval.EVAL_CARD_HTML)
        self.assertIn('"their " + plural(items.length, "previous game")',
                      apps_eval.EVAL_CARD_HTML)

    def test_stickers_no_longer_tilt(self) -> None:
        # The toybox tilt is gone with the host-token restyle (spec §1.5):
        # nothing rotates but the verdict stamp (and the shared disclosure
        # chevron). tests/test_apps.py::DesignSystemTests pins the whole CSS.
        for gone in (
            ".tag:nth-child(2n)",
            ".flag:nth-child(2n)",
            ".tl-chip:nth-child(2n)",
            ".anchor:nth-child(2n)",
            ".wc-line:nth-child(2n)",
        ):
            self.assertNotIn(gone, apps_eval.EVAL_CARD_HTML)

    def test_the_store_button_stays_in_fullscreen(self) -> None:
        # Fullscreen drops the breakdown button (fullscreen IS the breakdown)
        # but keeps a row holding the store link; a row without one goes.
        css = apps_eval.EVAL_CARD_HTML.split("<style>")[1].split("</style>")[0]
        self.assertIn(
            'html[data-display-mode="fullscreen"] .actions:not(.has-store),\n'
            '  html[data-display-mode="fullscreen"] .actions .act-breakdown,\n'
            '  html[data-display-mode="fullscreen"] .eval > .disclosure-body { display: none; }',
            css,
        )
        self.assertNotIn('html[data-display-mode="fullscreen"] .actions,', css)
        self.assertIn('row.classList.add("has-store");', self._function("storeButton", "actionsNode"))

    def test_the_call_reads_in_words(self) -> None:
        facts = self._function("factChips", "callNode")
        for marker in (
            'factChip(row, "Time to beat", main, extra ? extra + " full" : null, hltbTitle);',
            'else if (extra) factChip(row, "Time to beat", "~" + extra, "full", hltbTitle);',
            'factChip(row, "Your pace", hoursLabel(weekly / 60, true) + "/wk", "last 30 days",',
            '" via " + label("purchase_source", own.purchase_source)',
        ):
            self.assertIn(marker, facts)
        self.assertNotIn('"HLTB"', apps_eval.EVAL_CARD_HTML)
        self.assertNotIn('"Pace"', apps_eval.EVAL_CARD_HTML)
        # Below 420px "THE CALL" is a label above its chips, not an eyebrow
        # sharing a line with one chip.
        css = apps_eval.EVAL_CARD_HTML.split("<style>")[1].split("</style>")[0]
        phone = css[css.index("@media (max-width: 419px) {"):]
        phone = phone[:phone.index("\n  }\n")]
        self.assertIn(".call .call-label { flex-basis: 100%; margin-right: 0; }", phone)

    def test_an_error_whose_data_is_present_is_suppressed(self) -> None:
        body = self._function("evalCard", "noteCard")
        self.assertIn("errorsNode(wrap, packageErrors(pkg));", body)
        self.assertIn("errorDetailNode(parent, packageErrors(pkg));",
                      self._function("breakdownNode", "storeAppid"))


NODE = shutil.which("node")

# Just enough DOM and bridge for the action-row builders: el/disclosure build
# plain records, openLink records the URL it was asked to open.
_ACTION_ROW_SHIM = r"""
var opened = [];
function el(tag, cls, text) {
  var n = { tag: tag, className: cls || "", text: text || "", children: [], handlers: {},
            classList: { add: function (c) { n.className += " " + c; } },
            addEventListener: function (k, f) { n.handlers[k] = f; },
            appendChild: function (c) { n.children.push(c); return c; } };
  return n;
}
function disclosure(parent, text) {
  var btn = el("button", "disclosure", text);
  btn.contains = function (t) { return t === btn; };
  var body = el("div", "disclosure-body");
  parent.appendChild(btn);
  parent.appendChild(body);
  return { button: btn, body: body };
}
function openLink(url) { opened.push(url); }
function canFullscreen() { return false; }
function breakdownNode() {}
"""

_ACTION_ROW_PROBE = r"""
function labels(game, more) {
  var wrap = el("div", "eval");
  var pkg = { game: game };
  var appid = storeAppid(pkg);
  if (!(more || appid)) return null;
  var body = actionsNode(wrap, pkg, more, appid);
  var row = wrap.children[0];
  // evalCard moves the in-place body out of the row; mirror that.
  var kids = row.children.filter(function (c) { return c !== body; });
  return kids.map(function (c) { return c.text; });
}
var out = {
  both: labels({ steam_appid: 1145350 }, true),
  breakdownOnly: labels({}, true),
  storeOnly: labels({ steam_appid: 1145350 }, false),
  neither: labels({ steam_appid: null }, false),
  rejected: [0, -5, 1.5, "1145350", true, NaN].map(function (v) { return storeAppid({ game: { steam_appid: v } }); }),
};
var wrap = el("div", "eval");
actionsNode(wrap, { game: { steam_appid: 1145350 } }, true, 1145350);
wrap.children[0].children[2].handlers.click();
out.opened = opened;
out.storeRowClass = wrap.children[0].className;
var bare = el("div", "eval");
actionsNode(bare, { game: {} }, true, null);
out.bareRowClass = bare.children[0].className;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ActionRowBehaviourTests(unittest.TestCase):
    """The action row's builders, executed: ≤2 actions, store link by appid."""

    @classmethod
    def setUpClass(cls) -> None:
        html = apps_eval.EVAL_CARD_HTML
        start = html.index("function storeAppid(")
        builders = html[start:html.index("function syncDisplayMode(", start)]
        assert NODE is not None
        proc = subprocess.run(
            [NODE, "-e", _ACTION_ROW_SHIM + builders + _ACTION_ROW_PROBE],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout)

    def test_store_button_is_second_after_the_breakdown(self) -> None:
        self.assertEqual(self.out["both"], ["Full breakdown", "Store page ↗"])

    def test_no_appid_means_no_store_button(self) -> None:
        self.assertEqual(self.out["breakdownOnly"], ["Full breakdown"])

    def test_store_button_alone_without_breakdown_content(self) -> None:
        self.assertEqual(self.out["storeOnly"], ["Store page ↗"])

    def test_no_row_without_either(self) -> None:
        self.assertIsNone(self.out["neither"])

    def test_only_a_positive_integer_appid_counts(self) -> None:
        self.assertEqual(self.out["rejected"], [None] * 6)

    def test_a_row_with_the_store_button_is_marked_for_fullscreen(self) -> None:
        self.assertIn("has-store", self.out["storeRowClass"].split())
        self.assertNotIn("has-store", self.out["bareRowClass"].split())

    def test_store_button_opens_the_steam_store_page(self) -> None:
        self.assertEqual(
            self.out["opened"], ["https://store.steampowered.com/app/1145350/"]
        )


_ERRORS_PROBE = r"""
var pkg = {
  time: { hltb_main_hours: 18 },
  media: { trailer: { kind: "youtube", video_id: "x" }, screenshots: [] },
  pedigree: { developer: { name: "Retro" }, previous_games: [] },
  errors: ["hltb: completionist time unavailable", "media: steam: no trailer",
           "igdb: unresolved", "pace: unavailable", "", null],
};
var bare = { errors: ["hltb: down", "media: fetch failed", "igdb: unresolved"] };
console.log(JSON.stringify({ full: packageErrors(pkg), bare: packageErrors(bare) }));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ErrorSuppressionBehaviourTests(unittest.TestCase):
    """K4: an error entry whose data is on the card anyway is dropped."""

    @classmethod
    def setUpClass(cls) -> None:
        html = apps_eval.EVAL_CARD_HTML
        start = html.index("function errorHasData(")
        errors = html[start:html.index("function errorsNode(", start)]
        named = html[html.index("function named(v)"):]
        named = named[:named.index("\n") + 1]
        script = (
            "function num(v) { if (v === null || v === undefined || v === '') return null;"
            " var n = Number(v); return isFinite(n) ? n : null; }\n"
            "function list(v) { return Array.isArray(v) ? v : []; }\n"
            "function plural(n, w) { return n + ' ' + w + 's'; }\n"
            + apps_shared.MEDIA_PANEL_JS + apps_shared.PEDIGREE_JS + named + errors + _ERRORS_PROBE
        )
        assert NODE is not None
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout)

    def test_present_data_silences_its_error(self) -> None:
        self.assertEqual(self.out["full"], ["pace: unavailable"])

    def test_missing_data_keeps_its_error(self) -> None:
        self.assertEqual(self.out["bare"], ["hltb: down", "media: fetch failed", "igdb: unresolved"])


class SharedBlockTests(unittest.TestCase):
    """apps_shared.py is spliced in, never paraphrased (see tests/test_apps.py)."""

    def test_every_shared_constant_is_spliced_in_verbatim(self) -> None:
        for name, block in shared_blocks():
            with self.subTest(block=name):
                self.assertIn(block, apps_eval.EVAL_CARD_HTML)


class WidgetDriftTests(unittest.TestCase):
    """The two widget modules must not grow a second copy of the same block.

    Before apps_shared.py existed the two files shared 899 identical lines in
    33 blocks — the trailer stage, the carousel, the bridge, the link-out
    fallback — and a fix applied to one was easy to forget in the other. What
    is left in apps.py and apps_eval.py is each widget's own layout; anything
    substantial they agree on belongs in apps_shared.py, where one edit reaches
    both. The longest identical run today is 13 non-blank lines (the document
    head and the shared token/reset/component splices under it), so the cap
    leaves room for a small block to be ported deliberately before this fires.
    """

    MAX_IDENTICAL_LINES = 20

    @staticmethod
    def _significant_lines(name: str) -> list[str]:
        # Blank lines carry no logic, and the one apps_shared import is the
        # whole point of the refactor — neither counts as a duplicated block.
        source = (_WIDGET_DIR / name).read_text().splitlines()
        return [
            line
            for line in source
            if line.strip() and line.strip() != "from . import apps_shared"
        ]

    def test_no_large_block_is_duplicated_outside_apps_shared(self) -> None:
        cards = self._significant_lines("apps.py")
        evaluation = self._significant_lines("apps_eval.py")
        matcher = difflib.SequenceMatcher(None, cards, evaluation, autojunk=False)
        offenders = [
            (size, cards[start])
            for start, _, size in matcher.get_matching_blocks()
            if size >= self.MAX_IDENTICAL_LINES
        ]
        self.assertEqual(
            offenders,
            [],
            "apps.py and apps_eval.py share a block that belongs in apps_shared.py: "
            + "; ".join(f"{n} lines from {first.strip()!r}" for n, first in offenders),
        )


if __name__ == "__main__":
    unittest.main()
