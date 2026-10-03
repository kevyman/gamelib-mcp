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
            'frame.src = "https://www.youtube-nocookie.com/embed/" + videoId;',
            apps_eval.EVAL_CARD_HTML,
        )
        # encoded once; the watch link carries the same encoded id (B4)
        self.assertIn("var videoId = encodeURIComponent(trailer.video_id);", apps_eval.EVAL_CARD_HTML)
        self.assertIn('var watchUrl = "https://www.youtube.com/watch?v=" + videoId;', apps_eval.EVAL_CARD_HTML)
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("encodeURIComponent(trailer.video_id)"), 1)
        self.assertNotIn("https://www.youtube.com/embed/", apps_eval.EVAL_CARD_HTML)


def widget_css(html: str) -> str:
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def css_rule(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector is exactly ``selector``."""
    start = css.index("  " + selector + " {")
    return css[start:css.index("}", start)]


class EvalCardHtmlSanityTests(unittest.TestCase):
    def test_widget_is_self_contained(self) -> None:
        # Dependency-free by design: no CDN script, no external stylesheet.
        lowered = apps_eval.EVAL_CARD_HTML.lower()
        self.assertNotIn("<script src", lowered)
        self.assertNotIn("<link", lowered)

    def test_payload_text_never_goes_through_innerhtml(self) -> None:
        # The widget's only escaping mechanism is el()'s use of textContent.
        self.assertNotIn("innerHTML", apps_eval.EVAL_CARD_HTML)

    def test_every_verdict_maps_to_its_frame_and_ribbon_tier(self) -> None:
        # Spec 2026-10-04 §1.3: buy_now / play_what_you_own good,
        # wishlist_for_sale / try_demo ok, skip bad. The words come from the
        # shared, registry-checked VERDICT_LABELS (the ribbon uppercases them
        # in CSS); this map only picks the tier.
        html = apps_eval.EVAL_CARD_HTML
        for verdict, tier in (
            ("buy_now", "good"),
            ("play_what_you_own", "good"),
            ("wishlist_for_sale", "ok"),
            ("try_demo", "ok"),
            ("skip", "bad"),
        ):
            self.assertIn(f'{verdict}: "{tier}",', html)
            self.assertIn(verdict, apps_shared.VERDICT_LABELS)
        self.assertIn('var frame = el("article", "frame tier-" + tier + " ev-card");', html)
        self.assertIn('var verdict = label("verdict", pkg.verdict);', html)
        # an unknown verdict is a common (tier-none) card, never a lookup miss
        self.assertIn(': "none";', html[html.index("function verdictTier("):])

    def test_the_verdict_is_a_straddling_ribbon(self) -> None:
        # The stamp is gone; the verdict is the shared ribbon, straddling the
        # inline card, its ink the theme-invariant ribbon token, its ends the
        # shared notched clip-path, nothing rotated.
        html = apps_eval.EVAL_CARD_HTML
        self.assertIn(
            'frame.appendChild(ribbonNode(verdict, tier, ribbonNote(pkg), "straddle"));', html
        )
        self.assertIn(apps_shared.RIBBON_CSS, html)
        ribbon = css_rule(apps_shared.RIBBON_CSS, ".ribbon")
        self.assertIn("clip-path: polygon(", ribbon)
        self.assertIn("color: var(--gl-ribbon-ink);", ribbon)
        self.assertIn("  .ribbon-straddle {", apps_shared.RIBBON_CSS)
        for gone in ("stampNode", "  .stamp {", "stamp-good", "VERDICT_STAMPS", '"stamp '):
            self.assertNotIn(gone, html)
        self.assertNotIn("rotate(-3deg)", html)
        # The straddle never clips: the card leaves 20px each side and the
        # wrap holds the 20px it hangs below the frame.
        css = widget_css(html)
        self.assertIn("width: min(300px, 100% - 40px);", css_rule(css, ".ev-card"))
        self.assertIn("padding: 4px 0 26px;", css_rule(css, ".ev-cardwrap"))

    def test_the_ribbon_note_is_the_price_to_wait_for(self) -> None:
        note = EvalCardLayoutTests._function("ribbonNote", "frameNode")
        self.assertIn('if (pkg.verdict === "wishlist_for_sale") {', note)
        self.assertIn('return target ? "wait for ~" + target.replace(/\\.00(?!\\d)/, "") : null;', note)
        self.assertIn('return pkg.verdict === "try_demo" ? "try first" : null;', note)

    def test_render_branches_cover_the_whole_response_contract(self) -> None:
        # package -> the card; a void (void_assessment's response shape) -> a
        # plain note card; a bare verdict -> a note card; else empty.
        html = apps_eval.EVAL_CARD_HTML
        render = html[html.index("function render(data)"):]
        render = render[:render.index("\n  }\n")]
        order = [
            "if (data && data.package) {",
            "place(evalCard(data.package, data));",
            "} else if (data && data.voided) {",
            "place(voidCard(data));",
            "} else if (data && data.verdict) {",
            "place(recordedCard(data));",
            '"Nothing to display."',
        ]
        positions = [render.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_the_note_cards_are_small_horizontal_frames(self) -> None:
        # Artboard G: a .frame-s with the 48x64 art, the title, a slim ribbon
        # (the verdict's tier; a void is tier-none and reads "Voided") and a
        # caption — the verdict always through label(), never the literal.
        html = apps_eval.EVAL_CARD_HTML
        note = EvalCardLayoutTests._function("noteCard", "recordedCard")
        self.assertIn('el("article", "frame frame-s tier-" + opts.tier + " ev-nc"', note)
        self.assertIn("frame.appendChild(grain);", note)
        self.assertIn('body.appendChild(ribbonNode(opts.ribbon, opts.tier, null, "s"));', note)
        self.assertIn('el("div", "ev-nc-cap", opts.caption)', note)
        recorded = EvalCardLayoutTests._function("recordedCard", "voidCard")
        self.assertIn('tier: verdictTier(data.verdict), ribbon: label("verdict", data.verdict),', recorded)
        self.assertIn('caption: "Recorded" + (when ? " " + when : ""),', recorded)
        voided = EvalCardLayoutTests._function("voidCard", "skeletonKind")
        self.assertIn('tier: "none", ribbon: "Voided", voided: true,', voided)
        self.assertIn('"Verdict" + (no ? " " + no : "") + (when ? " of " + when : "") + " voided"', voided)
        css = widget_css(html)
        self.assertIn("width: 48px;", css_rule(css, ".ev-nc > .art"))
        self.assertIn("height: 64px;", css_rule(css, ".ev-nc > .art"))
        self.assertIn("flex-direction: row;", css_rule(css, ".ev-nc"))
        self.assertNotIn('"Recorded — "', html)

    def test_trailer_falls_back_when_the_media_element_fails(self) -> None:
        # Valve's constructed mp4 URLs are undocumented legacy surface and a
        # host may strip media-src — both land as source errors, which have to
        # be caught in the capture phase because they don't bubble.
        self.assertIn('video.addEventListener("error", function () {', apps_eval.EVAL_CARD_HTML)
        self.assertIn("posterFallback(hero, trailer);", apps_eval.EVAL_CARD_HTML)
        self.assertIn('video.preload = "none";', apps_eval.EVAL_CARD_HTML)

    def test_the_studio_renders_from_the_package(self) -> None:
        studio = EvalCardLayoutTests._function("studioNode", "pastNode")
        for marker in (
            'var box = bdSection(parent, "From the studio");',
            "var headline = pedigreeHeadline(ped);",
            # the shared headline's parts as gap-separated spans, no middots
            'headline.split(" · ").forEach(function (part) { head.appendChild(el("span", null, part)); });',
            # under the big-studio damper only the headline renders
            "if (!items.length) return;",
            "ministrip(box, items, function (item) {",
            '"You\'ve played " + (num(record.played_count) || 0)',
            '" — avg " + avg + "/10."',
        ):
            self.assertIn(marker, studio)
        # his rating as pips outranks the critic score, which only stands in
        # for a game he doesn't own
        lines = EvalCardLayoutTests._function("studioLines", "studioNode")
        self.assertIn("var rating = item.owned ? num(item.my_rating) : null;", lines)
        self.assertIn('[realScore(critic) ? "critics " + Math.round(critic) : null]', lines)

    def test_why_care_renders_an_ability_per_kind(self) -> None:
        # The eval card is the only one that renders why_care (it is authored
        # content, not a neutral fact about the game): one run-in ability per
        # entry, its kind label mixed-case (CSS uppercases it, so screen
        # readers read "People", not P-E-O-P-L-E).
        html = apps_eval.EVAL_CARD_HTML
        for kind, word in (
            ("people", "People"),
            ("studio", "Studio"),
            ("anticipation", "Anticipation"),
            ("moment", "Moment"),
        ):
            self.assertIn(f'{kind}: "{word}",', html)
        abilities = EvalCardLayoutTests._function("abilitiesNode", "groundNode")
        self.assertIn("list(pres.why_care)", abilities)
        self.assertIn("entries.slice(0, 3)", abilities)
        self.assertIn("box.appendChild(abilityNode(", abilities)

    def test_hype_counts_are_never_rendered(self) -> None:
        # `hypes` rides in the pedigree payload for completeness; the card does
        # not argue from popularity, so no renderer may read it.
        self.assertNotIn("hypes", apps_eval.EVAL_CARD_HTML)

    def test_anchors_are_minis_with_his_rating_as_pips(self) -> None:
        # GROUNDED IN YOUR HISTORY: a mini per anchor, tiered by his rating
        # (none when unrated), ten pips, then hours and how it ended.
        anchors = EvalCardLayoutTests._function("anchorsNode", "comparisonLines")
        self.assertIn('bdSection(parent, "Grounded in your history")', anchors)
        self.assertIn("tier: ratedTier(a.rating),", anchors)
        self.assertIn(
            'lines: [[ratingPips(a.rating) || "unrated"], playedParts(a.playtime_hours, a.completion_status)]',
            anchors,
        )
        self.assertIn("return n == null ? null : pipsNode(n, 10, ratingTier(n));",
                      EvalCardLayoutTests._function("ratingPips", "playedParts"))
        for gone in ("an-state", "an-good", "an-bad", "an-warn", "COMPLETION", "anchor-cover"):
            self.assertNotIn(gone, apps_eval.EVAL_CARD_HTML)


class EvalCardLayoutTests(unittest.TestCase):
    """The Binder layout (spec 2026-10-04 §2 Phase 2B, artboards B / C / G).

    Source-presence style like the rest of this module: the render call-sites
    and the parts they build, in the order the card assembles them.
    """

    @staticmethod
    def _function(name: str, until: str) -> str:
        html = apps_eval.EVAL_CARD_HTML
        start = html.index(f"function {name}(")
        return html[start:html.index(f"function {until}(", start)]

    def test_the_card_assembles_in_the_reading_order(self) -> None:
        # frame → candidate → summary → weakness → pitch → abilities → flavor
        # → reel → library → actions → provenance → notice; the breakdown
        # bodies (in place, and fullscreen) after all of it.
        card = self._function("evalCard", "noteCard")
        order = [
            "var frame = frameNode(pkg, cardNo(",
            "groundNode(flow, pkg);",
            "mediaSlot(wrap, pkg.media || {}, game.name);",
            "libraryNode(wrap, pkg.similar);",
            "actionsNode(wrap, pkg, more, appid)",
            "var prov = provenanceNode(pkg, data);",
            "errorsNode(wrap, packageErrors(pkg));",
            "wrap.appendChild(inPlaceBody);",
            'el("div", "fs-breakdown")',
        ]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        ground = self._function("groundNode", "mediaSlot")
        order = [
            "var cand = candidateNode(pkg);",
            'el("p", "ev-sum", pkg.summary)',
            'traitsNode("Weakness", "bad", "minus", flags, "ev-weak")',
            'el("p", "ev-pitch", pres.elevator_pitch)',
            "var abilities = abilitiesNode(pres);",
            "flow.appendChild(flavorNode(pres.craft_note));",
        ]
        positions = [ground.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_the_frame_is_art_badge_plate_stats_ribbon(self) -> None:
        frame = self._function("frameNode", "candidateNode")
        order = [
            "var grain = grainNode();",
            "art.appendChild(coverNode(game));",
            "badgeNode({ value: critic.value, tag: critic.tag, tier: critic.tier })",
            'badge.classList.add("badge-low");',
            "frame.appendChild(plateNode(pkg, no));",
            "var stats = statsNode(pkg);",
            "var chips = scoreChips(pkg);",
            "frame.appendChild(ribbonNode(",
        ]
        positions = [frame.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        # the cover window keeps the top of the art (the wordmark)
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        self.assertIn("object-position: 50% 0;", css_rule(css, ".ev-card > .art .cover-wrap img"))

    def test_the_badge_is_opencritic_else_metacritic_else_none(self) -> None:
        badge = self._function("criticBadge", "plateNode")
        self.assertLess(badge.index("craft.opencritic_score"), badge.index("craft.metacritic_score"))
        self.assertIn('tag: "OpenCritic", tier: ocTier(oc)', badge)
        self.assertIn('tag: "Metacritic", tier: mcTier(mc)', badge)
        self.assertIn("if (realScore(oc))", badge)
        self.assertIn("return null;", badge)

    def test_the_plate_numbers_the_card_and_spans_its_sub_line(self) -> None:
        card = self._function("evalCard", "noteCard")
        self.assertIn("cardNo(data.assessment_id != null ? data.assessment_id : game.game_id)", card)
        self.assertIn('return "No. " + (s.length < 3 ? ("00" + s).slice(-3) : s);',
                      self._function("cardNo", "dateLabel"))
        plate = self._function("plateNode", "fitStat")
        for marker in (
            'el("h2", "plate-title", game.name || "Unknown game")',
            'el("span", "card-no", no)',
            "var dev = (pkg.pedigree || {}).developer || {};",
            'sub.appendChild(el("span", null, String(dev.name)));',
            "var platform = price.platform || list(own.platforms).filter(Boolean)[0];",
            'el("span", "loz", label("platform", platform))',
        ):
            self.assertIn(marker, plate)
        self.assertNotIn(" · ", plate)

    def test_the_stats_rows_and_fit_pips(self) -> None:
        stats = self._function("statsNode", "craftPercent")
        order = [
            'statRow({ label: "Pace", note: "last 30d", value: hoursLabel(weekly / 60, true) + "/wk" })',
            'label: "Seen", note: price.platform ? "on " + label("platform", price.platform) : null,',
            'statRow({ label: "Target", value: target, key: true })',
            'statRow({ label: "Length", note: "main story", value: main })',
            'statRow({ label: "Paid", note: how, value: paid })',
            "rows.push(fitStat(pkg.fit_call));",
        ]
        positions = [stats.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("rows.slice(0, STATS_CAP)", stats)
        self.assertIn("var STATS_CAP = 6;", apps_eval.EVAL_CARD_HTML)
        self.assertIn('"via " + label("purchase_source", own.purchase_source)', stats)
        # strong fit 3 good, probable fit 2 good, coin flip 1 ok, probable miss 1 bad
        html = apps_eval.EVAL_CARD_HTML
        for call, lit, tier in (("strong fit", 3, "good"), ("probable fit", 2, "good"),
                                ("coin flip", 1, "ok"), ("probable miss", 1, "bad")):
            self.assertIn(f'"{call}": [{lit}, "{tier}"],', html)
        fit = self._function("fitStat", "statsNode")
        self.assertIn("value.appendChild(pipsNode(pips[0], 3, pips[1]));", fit)
        self.assertIn('var fit = String(call).replace(/\\s+fit$/i, "");', fit)
        self.assertIn('row.classList.add("tier-" + pips[1]);', fit)

    def test_two_columns_from_560px(self) -> None:
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        wide = css[css.index("@media (min-width: 560px) {"):]
        wide = wide[:wide.index("\n  }\n")]
        self.assertIn("grid-template-columns: 340px minmax(0, 1fr);", wide)
        self.assertIn(".media-slot .thumb { flex-basis: calc((100% - 18px) / 4); }", wide)
        # the reel is a sibling of the two-column block, so it spans both
        card = self._function("evalCard", "noteCard")
        self.assertIn("wrap.appendChild(top);\n\n    mediaSlot(wrap,", card)

    def test_the_breakdown_stays_out_of_the_inline_tier(self) -> None:
        # The evidence sections are one click away: none of them may be called
        # from the inline builders, and the breakdown runs them in the spec's
        # order.
        inline = (
            self._function("evalCard", "noteCard")
            + self._function("frameNode", "candidateNode")
            + self._function("groundNode", "mediaSlot")
        )
        breakdown = self._function("breakdownNode", "storeAppid")
        sections = (
            "forYouNode(",
            "anchorsNode(",
            "lineageNode(",
            "if (withLibrary) libraryNode(",
            "studioNode(",
            "pastNode(",
            "errorDetailNode(",
        )
        for section_call in sections:
            self.assertNotIn(section_call, inline)
            self.assertIn(section_call, breakdown)
        positions = [breakdown.index(c) for c in sections]
        self.assertEqual(positions, sorted(positions))
        # fullscreen carries the library strip (it hides the inline one)
        self.assertIn("breakdownNode(fs.node, fs.pkg, true);", self._function("syncDisplayMode", "provenanceNode"))

    def test_breakdown_sections_are_trait_columns_minis_and_a_ledger(self) -> None:
        you = self._function("forYouNode", "anchorsNode")
        self.assertIn('traitsNode("For you if", "good", "plus", yes)', you)
        self.assertIn('traitsNode("Not for you if", "bad", "minus", no)', you)
        self.assertIn('if (pair.childNodes.length === 1) pair.classList.add("ev-one");', you)
        lineage = self._function("lineageNode", "studioLines")
        self.assertIn('bdSection(parent, "Lineage").appendChild(pair);', lineage)
        self.assertIn("miniCard({ name: c.name, tier: ratedTier(c.my_rating), lines: comparisonLines(c) })", lineage)
        self.assertIn('if (c.note) item.appendChild(el("p", "ev-note", String(c.note)));', lineage)
        html = apps_eval.EVAL_CARD_HTML
        for relation, head in (("ancestor", "Ancestors"), ("descendant", "Descendants"),
                               ("similar", "Similar")):
            self.assertIn(f'["{relation}", "{head}"],', html)
        self.assertIn('["", "Other comparisons"]', lineage)
        # strips never exceed eight minis
        self.assertIn("items.slice(0, STRIP_CAP)", self._function("ministrip", "libraryNode"))
        self.assertIn("var STRIP_CAP = 8;", html)

    def test_past_verdicts_are_a_small_ledger(self) -> None:
        past = self._function("pastNode", "hasBreakdown")
        for marker in (
            'el("table", "ev-past")',
            '["Date", "Verdict", "Price"]',
            'th.setAttribute("scope", "col");',
            'tr.appendChild(el("td", null, dateLabel(p.assessed_at) || "earlier"));',
            'ribbonNode(label("verdict", p.verdict), verdictTier(p.verdict), null, "s")',
            "var seen = money(p.price_seen, p.price_currency);",
            'bdSection(parent, "Past verdicts")',
            '"+" + (total - items.length) + " earlier"',
        ):
            self.assertIn(marker, past)
        self.assertLess(past.index("items.sort("), past.index("items.forEach("))

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
        self.assertEqual(actions.count("fullscreenOrDisclosure(row,"), 1)
        # Two storeButton calls, on mutually exclusive branches (store alone,
        # or store after the breakdown) — so the row never exceeds two.
        self.assertEqual(actions.count("storeButton(row, appid);"), 2)
        self.assertNotIn("row.appendChild(", actions)
        self.assertIn(
            'fullscreenOrDisclosure(row, "Full breakdown", function (body) { breakdownNode(body, pkg); },',
            actions,
        )
        # the breakdown button wears the primary pill
        self.assertIn('d.button.classList.add("primary");', actions)

    def test_store_button_follows_the_breakdown_only_with_an_appid(self) -> None:
        actions = self._function("actionsNode", "syncDisplayMode")
        # Second, after "Full breakdown", and only when an appid resolved.
        self.assertLess(
            actions.index('fullscreenOrDisclosure(row, "Full breakdown"'),
            actions.index("if (appid) storeButton(row, appid);"),
        )
        # No breakdown: the row is the store button alone (evalCard only
        # reaches this branch when an appid exists).
        alone = actions[actions.index("if (!more) {"):actions.index("var d = fullscreenOrDisclosure(")]
        self.assertIn("storeButton(row, appid);", alone)
        self.assertIn("return null;", alone)

    def test_store_button_is_a_secondary_btn_opening_the_steam_page(self) -> None:
        button = self._function("storeButton", "actionsNode")
        self.assertIn('el("button", "btn act-store")', button)
        self.assertIn('var icon = iconNode("0 0 20 20", EXT_ICON);', button)
        self.assertIn('btn.appendChild(el("span", null, "Store page"));', button)
        self.assertNotIn("primary", button)
        self.assertIn(
            'openLink("https://store.steampowered.com/app/" + appid + "/");', button
        )
        # Only a positive integer appid yields a button; anything else is null.
        appid = self._function("storeAppid", "storeButton")
        self.assertIn("var appid = (pkg.game || {}).steam_appid;", appid)
        for guard in ('typeof appid === "number"', "appid > 0", "Math.floor(appid) === appid"):
            self.assertIn(guard, appid)
        # The two pills share the row at every width, labels on one line.
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        rule = css_rule(css, ".actions > .btn, .actions > .disclosure")
        self.assertIn("flex: 1 1 auto;", rule)
        self.assertIn("white-space: nowrap;", rule)
        self.assertNotIn("max-width: 419px", css)

    def test_full_breakdown_requests_fullscreen_and_falls_back_in_place(self) -> None:
        # The shared control (apps_shared.DISCLOSURE_JS, executed in
        # tests/test_apps_shared.py::FullscreenOrDisclosureTests) asks for
        # fullscreen and falls back in place; on a grant the card's own
        # no-op hands over to syncDisplayMode.
        actions = self._function("actionsNode", "syncDisplayMode")
        self.assertIn(
            'fullscreenOrDisclosure(row, "Full breakdown", function (body) { breakdownNode(body, pkg); },\n'
            "      function () {});",
            actions,
        )
        for gone in ("requestDisplayMode", "inPlace", "d.button.click();", "}, true);"):
            self.assertNotIn(gone, actions)
        # Fullscreen: the card stays left, the breakdown builds once in the
        # right column after the pitch, CSS swaps the action row's button out,
        # and size reports go quiet.
        html = apps_eval.EVAL_CARD_HTML
        for marker in (
            'html[data-display-mode="fullscreen"] .fs-breakdown { display: flex; }',
            'html[data-display-mode="fullscreen"] .actions .act-breakdown,',
            'html[data-display-mode="fullscreen"] .ev-flow > .fs-breakdown { order: 3;',
            'html[data-display-mode="fullscreen"] .ev-flow > .ev-pitch { order: 2;',
            'html[data-display-mode="fullscreen"] .ev-flow > .ev-weak { order: 4; }',
            'html[data-display-mode="fullscreen"] .eval > .ev-lib,',
            'if (fs && !fs.built && currentDisplayMode() === "fullscreen") {',
            'attributeFilter: ["data-display-mode"]',
            'hooks.shouldReportSize = function () { return currentDisplayMode() !== "fullscreen"; };',
        ):
            self.assertIn(marker, html)
        self.assertIn("flow.appendChild(fs);", self._function("evalCard", "noteCard"))

    def test_secondary_scores_ride_under_the_stats(self) -> None:
        # The review share, a moving trend and the critic score the badge does
        # not show — the shared chips, inside the frame under the stat block.
        chips = self._function("scoreChips", "ribbonNote")
        for marker in (
            'el("div", "chips tags ev-scores")',
            'label: "Reviews", value: pct + "% positive", tier: craftTier(pct), meter: pct,',
            'var count = compactCount(craft.review_count, "review");',
            "aux: count,",
            'title: "Sample-adjusted share of positive reviews"',
            'label: "Trend", value: traj[0], tier: traj[1],',
            'if (realScore(mc) && !(badge && badge.source === "mc")) {',
            'scoreChip({ label: "Metacritic", value: Math.round(mc), tier: mcTier(mc) })',
        ):
            self.assertIn(marker, chips)
        self.assertNotIn("stable:", self._function("craftPercent", "scoreChips") + chips)
        for hex_color in ("#6c3", "#fc3", "#f00"):
            self.assertNotIn(hex_color, apps_eval.EVAL_CARD_HTML.lower())
        # Local tier helpers would drift from the shared ones.
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function mcTier("), 1)
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function hoursLabel("), 1)
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function money("), 1)

    def test_facts_flags_and_past_verdicts_read_as_words(self) -> None:
        # Platform ids and stored verdict literals go through label(); flags
        # are WEAKNESS traits; failures are one named notice.
        html = apps_eval.EVAL_CARD_HTML
        for marker in (
            '"on " + label("platform", price.platform)',
            'ribbonNode(label("verdict", p.verdict), verdictTier(p.verdict), null, "s")',
            'traitsNode("Weakness", "bad", "minus", flags, "ev-weak")',
            'items.forEach(function (text) { box.appendChild(traitNode(kind, capFirst(text))); });',
            '.map(function (p) { return label("platform", p); });',
            "var node = notice(parent, errors.map(errorItem));",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("some data unavailable", html)
        self.assertNotIn("parts.push(String(p.verdict))", html)

    def test_the_candidate_line_and_provenance_are_spans(self) -> None:
        cand = self._function("candidateNode", "traitsNode")
        for marker in (
            'parts = ["Candidate", own.wishlisted ? "On your wishlist" : "Not owned, not wishlisted"];',
            # zero is authoritative NOT-played; "0h played" must never render
            "own.playtime_hours > 0 ? hoursLabel(own.playtime_hours) : null",
            'parts.filter(Boolean).forEach(function (p) { line.appendChild(el("span", null, p)); });',
        ):
            self.assertIn(marker, cand)
        prov = self._function("provenanceNode", "evalCard")
        self.assertIn('"Published by " + ped.publisher_name', prov)
        self.assertIn('"assessed " + when', prov)
        self.assertIn('return Number(m[3]) + " " + MONTHS[Number(m[2]) - 1] + " " + m[1];',
                      self._function("dateLabel", "capFirst"))

    def test_the_craft_note_is_flavor_text_after_the_abilities(self) -> None:
        ground = self._function("groundNode", "mediaSlot")
        self.assertIn("if (pres.craft_note) flow.appendChild(flavorNode(pres.craft_note));", ground)
        self.assertLess(ground.index("abilitiesNode(pres)"), ground.index("flavorNode("))

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
        # three thumbs to a row on a phone
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        self.assertIn("flex: 0 0 calc((100% - 12px) / 3);", css_rule(css, ".media-slot .thumb"))

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
            ".overlay-panel.carousel {",
            "width: 100%;",
            'navButton("car-prev", "‹"',
            'navButton("car-next", "›"',
            'counter.textContent = (index + 1) + " / " + shots.length;',
            # the shared dialog chrome: ✕, focus trap, Escape and arrows
            'var chrome = lightboxPanel("overlay-panel carousel", gameName, closeOverlay);',
            "var keydown = lightboxKeys(panel, function (delta) { show(index + delta); }, closeOverlay);",
            'document.addEventListener("keydown", keydown, true);',
            'document.removeEventListener("keydown", s.keydown, true);',
            "chrome.closer.focus({ preventScroll: true });",
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

    def test_the_library_strip_is_minis_of_owned_neighbours(self) -> None:
        # IN YOUR LIBRARY is the tag-similarity row as mini cards (≤8): year
        # and his rating or hours, then "48% similar" and the shared tags.
        library = self._function("libraryNode", "ratingPips")
        self.assertIn('el("div", "section-title", "In your library")', library)
        self.assertIn("tier: ratedTier(item.my_rating),", library)
        lines = self._function("similarLines", "ministrip")
        self.assertIn('el("span", "v", Math.round(sim * 100) + "%")', lines)
        self.assertIn('el("span", null, " similar")', lines)
        self.assertIn('if (why.length) second.push(why.join(", "));', lines)
        # model-authored "similar" comparisons stay in the lineage, apart
        self.assertIn('["similar", "Similar"],', apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("foldSimilar", apps_eval.EVAL_CARD_HTML)

    def test_counts_are_pluralized(self) -> None:
        # "Rebel Wolves · est. 2022 · 1 games" shipped to the phone.
        self.assertIn(
            'return n + (truncated ? "+" : "") + " " + word '
            '+ (n === 1 && !truncated ? "" : "s");',
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn('plural(size, "game", ped.catalog_truncated)', apps_eval.EVAL_CARD_HTML)
        self.assertIn('"their " + plural(items.length, "previous game")',
                      self._function("studioNode", "pastNode"))

    def test_nothing_tilts(self) -> None:
        # The toybox tilt and the -3deg stamp are gone: the only rotations are
        # the shared deal-in, pip and chevron ones (tests/test_apps.py
        # DesignSystemTests pins the whole CSS).
        for gone in (
            ".tag:nth-child(2n)",
            ".flag:nth-child(2n)",
            ".tl-chip:nth-child(2n)",
            ".anchor:nth-child(2n)",
            ".wc-line:nth-child(2n)",
            "rotate(-3deg)",
        ):
            self.assertNotIn(gone, apps_eval.EVAL_CARD_HTML)

    def test_the_store_button_stays_in_fullscreen(self) -> None:
        # Fullscreen drops the breakdown button (fullscreen IS the breakdown)
        # but keeps a row holding the store link; a row without one goes.
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        self.assertIn(
            'html[data-display-mode="fullscreen"] .actions:not(.has-store),\n'
            '  html[data-display-mode="fullscreen"] .actions .act-breakdown,\n'
            '  html[data-display-mode="fullscreen"] .eval > .disclosure-body { display: none; }',
            css,
        )
        self.assertNotIn('html[data-display-mode="fullscreen"] .actions,', css)
        self.assertIn('row.classList.add("has-store");', self._function("storeButton", "actionsNode"))

    def test_the_card_is_dealt_in_once_out_of_the_skeleton(self) -> None:
        # M1 + M5: the frame (only the frame — the ground's text is in place
        # at t=0) deals in once per render; the first result resolves out of
        # the skeleton.
        place = self._function("place", "render")
        self.assertIn("resolveSkeleton(skel, function () { return built.node; });", place)
        self.assertIn('built.node.classList.remove("deal");', place)
        self.assertIn("dealIn(built.frame, 0);", place)
        self.assertEqual((_WIDGET_DIR / "apps_eval.py").read_text().count("dealIn("), 1)

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
function fullscreenOrDisclosure(parent, text) { return disclosure(parent, text); }
function openLink(url) { opened.push(url); }
function canFullscreen() { return false; }
function breakdownNode() {}
function iconNode() { return null; }
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
  return kids.map(function (c) {
    return c.text || c.children.map(function (k) { return k.text; }).join("");
  });
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
        self.assertEqual(self.out["both"], ["Full breakdown", "Store page"])

    def test_no_appid_means_no_store_button(self) -> None:
        self.assertEqual(self.out["breakdownOnly"], ["Full breakdown"])

    def test_store_button_alone_without_breakdown_content(self) -> None:
        self.assertEqual(self.out["storeOnly"], ["Store page"])

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


_ERROR_LABEL_SHIM = r"""
function El(tag, cls, text) { this.tag = tag; this.className = cls || ""; this.text = text || ""; this.kids = []; this.attrs = {}; }
El.prototype.appendChild = function (c) { this.kids.push(c); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this.text + this.kids.map(function (k) { return k.textContent; }).join(""); },
});
function el(tag, cls, text) { return new El(tag, cls, text); }
var document = { createElement: function (t) { return new El(t); } };
function list(v) { return Array.isArray(v) ? v : []; }
function section(parent, title) { var b = el("section", "panel"); parent.appendChild(b); return b; }
"""

_ERROR_LABEL_PROBE = r"""
function item(t) { return errorItem(t); }
var out = {
  similar: item("similar: lookup failed"),
  anchors: item("anchors: lookup failed"),
  pedigree: item("pedigree: unavailable"),
  studio: item("studio: unavailable"),
  igdb: item("igdb: unresolved — no igdb_id stored"),
  mediaSteam: item("media: steam: fetch failed"),
  mediaIgdb: item("media: igdb: name resolution failed"),
  mediaBare: item("media: fetch failed"),
  unknown: item("weird_block: boom"),
};
var p = el("div");
out.notice = notice(p, ["similar: lookup failed", "media: steam: fetch failed", "weird_block: boom"]
  .map(errorItem)).textContent;
var detail = el("div");
errorDetailNode(detail, ["media: steam: fetch failed", "igdb_resolver: " + new Array(40).join("slow ")]);
out.detail = detail.kids[0].kids[0].kids.map(function (li) { return li.textContent; });
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ErrorLabelBehaviourTests(unittest.TestCase):
    """F2/F4, executed: every failure names what and where, in words."""

    @classmethod
    def setUpClass(cls) -> None:
        html = apps_eval.EVAL_CARD_HTML
        start = html.index("  var ERROR_BLOCKS = {")
        mapping = html[start:html.index("  function errorHasData(", start)]
        detail = html[html.index("  function errorDetailNode("):html.index("  function named(v)")]
        detail = detail[:detail.index("\n  }\n") + 4]
        script = (_ERROR_LABEL_SHIM + apps_shared.LABELS_JS + apps_shared.NOTICE_JS
                  + mapping + detail + _ERROR_LABEL_PROBE)
        assert NODE is not None
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout)

    def test_library_blocks_say_library(self) -> None:
        self.assertEqual(self.out["similar"], {"what": "similar games", "source": "library",
                                               "why": "lookup failed"})
        self.assertEqual(self.out["anchors"]["what"], "your history")
        self.assertEqual(self.out["anchors"]["source"], "library")

    def test_studio_blocks_say_igdb(self) -> None:
        for key in ("pedigree", "studio", "igdb"):
            with self.subTest(key=key):
                self.assertEqual((self.out[key]["what"], self.out[key]["source"]), ("studio", "IGDB"))

    def test_media_takes_its_source_from_the_reason_prefix(self) -> None:
        self.assertEqual(self.out["mediaSteam"], {"what": "media", "source": "Steam", "why": "fetch failed"})
        self.assertEqual(self.out["mediaIgdb"]["source"], "IGDB")
        self.assertEqual(self.out["mediaBare"], {"what": "media", "source": "", "why": "fetch failed"})

    def test_an_unknown_block_is_humanized_without_a_source(self) -> None:
        self.assertEqual(self.out["unknown"], {"what": "weird block", "source": "", "why": "boom"})

    def test_the_notice_names_what_and_where(self) -> None:
        self.assertEqual(self.out["notice"],
                         "Couldn't load: similar games (library), media (Steam), weird block")

    def test_the_detail_humanizes_the_key_and_caps_the_reason(self) -> None:
        media, long_one = self.out["detail"]
        self.assertEqual(media, "Media (Steam) — fetch failed")
        self.assertTrue(long_one.startswith("Igdb resolver — slow slow"))
        self.assertNotIn("_", long_one)
        reason = long_one.split(" — ", 1)[1]
        self.assertLessEqual(len(reason), 120)
        self.assertTrue(reason.endswith("…"))


_RENDER_PACKAGE = {
    "game_id": 4235,
    "name": "Marvel's Wolverine",
    "assessment_id": 46,
    "assessed_at": "2026-10-03T13:04:42Z",
    "verdict": "wishlist_for_sale",
    "package": {
        "game": {"game_id": 4235, "name": "Marvel's Wolverine", "release_year": 2026,
                 "cover_url": None, "steam_appid": None},
        "verdict": "wishlist_for_sale",
        "summary": "Wait for ~€40.",
        "presentation": {
            "elevator_pitch": "A linear Logan story.",
            "craft_note": "OpenCritic 79.",
            "for_you_if": ["You finished Spider-Man"],
            "not_for_you_if": ["Slay the Spire 2 is active"],
            "why_care": [{"kind": "studio", "text": "Insomniac's first"},
                         {"kind": "moment", "text": "Critics and players split"}],
        },
        "comparisons": [{"name": "Marvel's Spider-Man", "relation": "ancestor", "note": "Where it started",
                         "game_id": 2354, "owned": True, "my_rating": 9, "playtime_hours": 50.0}],
        "craft": {"opencritic_score": 79, "metacritic_score": None, "trajectory": "stable"},
        "fit_call": "probable fit",
        "flags": ["repetitive combat per critics", "lowest-rated PS Studios PS5 game"],
        "anchors": [{"game_id": 2354, "name": "Marvel's Spider-Man", "rating": 9,
                     "playtime_hours": 50.0, "completion_status": "completed", "cover_url": None}],
        "ownership": {"owned": False, "wishlisted": False, "platforms": []},
        "time": {"hltb_main_hours": 12.0, "hltb_extra_hours": 15.0, "recent_weekly_minutes": 156},
        "price": {"seen": 69.99, "currency": "EUR", "platform": "ps5", "target": 40},
        "media": None,
        "similar": {"items": [{"game_id": 1991, "name": "MGS3", "release_year": 2023, "owned": True,
                               "unplayed": True, "playtime_hours": 0.0, "similarity": 0.48,
                               "shared_tags": ["stealth", "drama"]}]},
        "pedigree": {"developer": {"name": "Insomniac Games"}, "previous_games": [],
                     "publisher_name": "Sony Interactive Entertainment"},
        "past": {"items": [{"assessed_at": "2026-05-12T18:04:11Z", "verdict": "skip",
                            "price_seen": 79.99, "price_currency": "EUR"}]},
        "errors": [],
    },
}

_RENDER_PROBE = r"""
  (async function () {
    function q(n, s) { return n.querySelector(s); }
    function txt(n) { return n ? n.textContent : null; }
    function texts(n, s) { return n.querySelectorAll(s).map(function (x) { return x.textContent; }); }
    var out = {};
    answer("ui/initialize", { hostCapabilities: {}, hostContext: {} });
    await tick();
    out.before = root.firstElementChild.className;
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: PACKAGE } });
    var card = q(root, ".eval");
    var frame = q(card, ".frame");
    var ribbon = q(frame, ".ribbon");
    var badge = q(frame, ".badge");
    out.skeletonLeaving = root.children.length === 2 && root.children[1].classList.contains("leaving");
    out.wrapDealt = card.classList.contains("deal");
    out.frame = { tag: frame.tagName, cls: frame.className, label: frame.getAttribute("aria-label"),
                  first: frame.children[0].getAttribute("class") };
    out.badge = [txt(q(badge, ".badge-num")), txt(q(badge, ".badge-tag")), badge.className];
    out.plate = [txt(q(frame, ".plate-title")), txt(q(frame, ".card-no")),
                 q(q(frame, ".plate"), ".sub").children.map(txt)];
    out.stats = frame.querySelectorAll(".stat").map(function (r) {
      return [txt(q(r, ".stat-label")), txt(q(r, ".stat-val")), r.className];
    });
    out.fitLit = q(frame, ".stats").querySelectorAll(".on").length;
    out.chips = q(frame, ".ev-scores") ? texts(q(frame, ".ev-scores"), ".chip") : null;
    out.ribbon = [ribbon.className, texts(ribbon, "span")];
    out.ground = q(card, ".ev-flow").children.map(function (c) { return c.className; });
    var flow = q(card, ".ev-flow");
    out.cand = q(flow, ".ev-cand").children.filter(function (c) { return c.tagName === "SPAN"; }).map(txt);
    out.weak = q(flow, ".ev-weak").querySelectorAll(".trait").map(function (t) {
      return txt(t.children[t.children.length - 1]);
    });
    out.abilities = q(flow, ".ev-abil").querySelectorAll(".ability").map(function (a) { return txt(q(a, "b")); });
    out.wrap = card.children.map(function (c) { return c.className; });
    out.library = texts(q(card, ".ev-lib"), ".mini-name");
    out.prov = card.children.filter(function (c) { return c.classList.contains("ev-prov"); })[0].children.map(txt);
    out.actions = q(card, ".actions").children.filter(function (c) { return c.tagName === "BUTTON"; }).map(txt);
    var toggle = q(card, ".act-breakdown");
    toggle.click();
    var bd = q(card, ".disclosure-body");
    out.breakdown = bd.children.map(function (c) {
      var t = q(c, ".section-title");
      return t ? txt(t) : c.className;
    });
    out.traitHeads = texts(bd, ".traits-head");
    out.anchorPips = bd.querySelectorAll(".ev-bd-sec")[0].querySelectorAll(".on").length;
    out.ledger = q(bd, ".ev-past").querySelectorAll("tr").slice(1).map(function (tr) {
      return tr.children.map(function (td) { return td.textContent; });
    });
    out.ledgerRibbon = q(q(bd, ".ev-past"), ".ribbon").className;
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: {
      verdict: "play_what_you_own", name: "Slay the Spire II", assessed_at: "2026-10-03T08:40:00Z" } } });
    var note = q(root, ".frame");
    out.note = [note.className, txt(q(note, ".plate-title-s")), txt(q(note, ".ribbon")),
                q(note, ".ribbon").className, txt(q(root, ".ev-nc-cap")), note.classList.contains("deal")];
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: {
      voided: true, assessment_id: 46, name: "Marvel's Wolverine", verdict: "wishlist_for_sale",
      assessed_at: "2026-10-03T13:04:42Z" } } });
    var gone = q(root, ".frame");
    out.voided = [gone.className, txt(q(gone, ".ribbon")), q(gone, ".ribbon").className, txt(q(root, ".ev-nc-cap"))];
    console.log(JSON.stringify(out));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class EvalCardRenderTests(unittest.TestCase):
    """The Binder card, executed under test_apps_shared.MINI_DOM from the bridge."""

    @classmethod
    def setUpClass(cls) -> None:
        from test_apps_shared import run_widget

        probe = "  var PACKAGE = " + json.dumps(_RENDER_PACKAGE) + ";\n" + _RENDER_PROBE
        cls.out = run_widget("eval-card", probe)

    def test_the_first_result_resolves_out_of_the_skeleton(self) -> None:
        self.assertEqual(self.out["before"], "skel skel-eval")
        self.assertTrue(self.out["skeletonLeaving"])
        # only the frame is dealt; the ground's text is in place at t=0
        self.assertFalse(self.out["wrapDealt"])
        self.assertIn("deal", self.out["frame"]["cls"].split())

    def test_the_frame_is_tiered_by_the_verdict_and_grained(self) -> None:
        frame = self.out["frame"]
        self.assertEqual(frame["tag"], "ARTICLE")
        self.assertEqual(frame["cls"].split()[:3], ["frame", "tier-ok", "ev-card"])
        self.assertEqual(frame["label"], "Marvel's Wolverine: Wishlist for a sale")
        self.assertEqual(frame["first"], "grain")

    def test_badge_plate_and_stats(self) -> None:
        self.assertEqual(self.out["badge"][:2], ["79", "OpenCritic"])
        self.assertIn("tier-good", self.out["badge"][2])
        self.assertIn("badge-low", self.out["badge"][2])
        self.assertEqual(self.out["plate"], ["Marvel's Wolverine", "No. 046",
                                             ["Insomniac Games", "2026", "PS5"]])
        self.assertEqual([row[:2] for row in self.out["stats"]], [
            ["Pacelast 30d", "~2.6h/wk"],
            ["Seenon PS5", "€69.99"],
            ["Target", "€40.00"],
            ["Lengthmain story", "12h"],
            ["Fit", "Probable"],
        ])
        self.assertIn("is-key", self.out["stats"][2][2])
        self.assertIn("tier-good", self.out["stats"][4][2])
        self.assertEqual(self.out["fitLit"], 2)
        # a stable trend says nothing, and the OpenCritic score is the badge
        self.assertIsNone(self.out["chips"])

    def test_the_ribbon_straddles_with_its_note(self) -> None:
        cls, spans = self.out["ribbon"]
        self.assertEqual(cls.split(), ["ribbon", "ribbon-straddle", "tier-ok", "has-note"])
        self.assertEqual(spans, ["Wishlist for a sale", "wait for ~€40"])

    def test_the_ground_and_the_tail_in_order(self) -> None:
        self.assertEqual(self.out["ground"], [
            "ev-cand is-candidate", "ev-sum", "traits ev-weak", "ev-pitch", "ev-abil", "flavor",
            "fs-breakdown",
        ])
        self.assertEqual(self.out["cand"], ["Candidate", "Not owned, not wishlisted"])
        self.assertEqual(self.out["weak"], ["Repetitive combat per critics", "Lowest-rated PS Studios PS5 game"])
        self.assertEqual(self.out["abilities"], ["Studio", "Moment"])
        self.assertEqual(self.out["wrap"], ["ev-top", "ev-lib", "actions", "ev-prov", "disclosure-body ev-bd"])
        self.assertEqual(self.out["library"], ["MGS3"])
        self.assertEqual(self.out["prov"], ["Published by Sony Interactive Entertainment",
                                            "assessed 3 Oct 2026"])
        self.assertEqual(self.out["actions"], ["Full breakdown▾"])

    def test_the_breakdown_sections(self) -> None:
        self.assertEqual(self.out["breakdown"], [
            "ev-pair", "Grounded in your history", "Lineage", "From the studio", "Past verdicts",
        ])
        self.assertEqual(self.out["traitHeads"], ["For you if", "Not for you if"])
        self.assertEqual(self.out["anchorPips"], 9)
        self.assertEqual(self.out["ledger"], [["12 May 2026", "Skip", "seen€79.99"]])
        self.assertEqual(self.out["ledgerRibbon"].split(), ["ribbon", "ribbon-s", "tier-bad"])

    def test_note_cards(self) -> None:
        cls, name, ribbon, ribbon_cls, caption, dealt = self.out["note"]
        self.assertEqual(cls.split()[:4], ["frame", "frame-s", "tier-good", "ev-nc"])
        self.assertEqual((name, ribbon, caption), ("Slay the Spire II", "Play what you own",
                                                   "Recorded 3 Oct 2026"))
        self.assertEqual(ribbon_cls.split(), ["ribbon", "ribbon-s", "tier-good"])
        self.assertTrue(dealt)
        cls, ribbon, ribbon_cls, caption = self.out["voided"]
        self.assertEqual(cls.split()[:5], ["frame", "frame-s", "tier-none", "ev-nc", "ev-void"])
        self.assertEqual(ribbon, "Voided")
        self.assertIn("tier-none", ribbon_cls)
        self.assertEqual(caption, "Verdict No. 046 of 3 Oct 2026 voided")


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
