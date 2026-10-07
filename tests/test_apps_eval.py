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
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from conftest import ProtocolEraMixin
from fastmcp import FastMCP

from gamelib_mcp import apps_eval, apps_shared

_WIDGET_DIR = Path(apps_eval.__file__).parent


def shared_blocks() -> list[tuple[str, str]]:
    """The (name, text) pairs both widgets splice into their HTML."""
    return [
        (name, value)
        for name, value in sorted(vars(apps_shared).items())
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    ]


class EvalCardWireTests(ProtocolEraMixin, unittest.IsolatedAsyncioTestCase):
    """The eval-card resource as a real Client reads it, in both eras."""

    async def test_resource_registered_and_serves_widget(self) -> None:
        mcp = FastMCP("test")
        apps_eval.register_eval_app(mcp)
        async with self.open_client(mcp) as client:
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


class LegacyEvalCardWireTests(EvalCardWireTests):
    PROTOCOL_MODE = "legacy"


class EvalCardResourceTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertIn('var frame = frameNode("article", "ev-card", tier);', html)
        self.assertIn('var verdict = label("verdict", pkg.verdict);', html)
        # an unknown verdict is a common (tier-none) card, never a lookup miss
        self.assertIn(': "none";', html[html.index("function verdictTier("):])

    def test_the_verdict_is_a_straddling_ribbon(self) -> None:
        # The old rotated badge is gone (guarded below); the verdict is the shared ribbon, straddling the
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
        note = EvalCardLayoutTests._function("ribbonNote", "cardNode")
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
            'place(evalCard(data.package, data), renderKey(data, "package"));',
            "} else if (data && data.voided) {",
            'place(voidCard(data), renderKey(data, "voided"));',
            "} else if (data && data.verdict) {",
            'place(recordedCard(data), renderKey(data, "recorded"));',
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
        # the shared frame builder (grain first) — no local frame/grain code
        self.assertIn('frameNode("article", "frame-s ev-nc" + (opts.voided ? " ev-void" : ""), opts.tier);', note)
        self.assertNotIn("grainNode()", note)
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
        # FROM THE STUDIO is the shared builder (apps_shared.PEDIGREE_JS),
        # given the candidate's year for the timeline hairline; the local
        # studioNode and its track-record sentence are gone (each mini states
        # its own ownership).
        breakdown = EvalCardLayoutTests._function("breakdownNode", "storeAppid")
        self.assertIn("studioStrip(parent, pkg.pedigree, (pkg.game || {}).release_year);", breakdown)
        source = Path(apps_eval.__file__).read_text()
        for gone in ("function studioNode(", "function studioStrip(", "library_track_record",
                     "You've played", "previous game\")"):
            self.assertNotIn(gone, source)
        # the shared mini format (B3): his rating as pips and his hours only
        # for a game he owns, "not owned" otherwise, then the year; with no
        # rating, the critic score as a "Critics 86" line (executed in
        # tests/test_apps.py::ReviewFixBehaviourTests for both widgets)
        studio = apps_shared.PEDIGREE_JS.split("function studioMini(item) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("var rating = item.owned ? num(item.my_rating) : null;", studio)
        self.assertIn("lines: miniLines({ rating: rating, critic: item.critic_score,", studio)
        self.assertIn("hours: item.owned ? item.playtime_hours : null,", studio)
        self.assertIn("owned: !!item.owned, year: item.release_year, platform: item.platform }),", studio)
        self.assertEqual(apps_eval.EVAL_CARD_HTML.count("function studioMini(item) {"), 1)

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
        # (none when unrated), the shared mini format: ten small pips, then
        # hours and how it ended ("50h" "completed").
        anchors = EvalCardLayoutTests._function("anchorsNode", "lineageNode")
        self.assertIn('eyebrowSection(parent, "Grounded in your history")', anchors)
        self.assertIn("tier: ratedTier(a.rating),", anchors)
        self.assertIn("lines: miniLines({ rating: a.rating, hours: a.playtime_hours, status: a.completion_status,",
                      anchors)
        self.assertIn("if (rating != null) lines.push([pipsNode(rating, 10, ratingTier(rating))]);",
                      apps_shared.MINI_JS)
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
        # bodies (in place, and fullscreen) after all of it. Built once each:
        # the candidate line and the provenance under the card in .ev-left
        # (where fullscreen shows them); inline, CSS order places them
        # (executed in EvalCardRenderTests.test_each_part_is_built_once_in_both_modes).
        card = self._function("evalCard", "noteCard")
        order = [
            "var frame = cardNode(pkg, cardNo(",
            "var cand = candidateNode(pkg);",
            "var prov = provenanceNode(pkg, data);",
            "groundNode(flow, pkg);",
            "mediaSlot(wrap, pkg.media || {}, game.name);",
            "libraryNode(wrap, pkg.similar);",
            "actionsNode(wrap, pkg, more, appid)",
            "errorsNode(wrap, packageErrors(pkg));",
            "wrap.appendChild(inPlaceBody);",
            'el("div", "fs-breakdown")',
        ]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        inline = 'html:not([data-display-mode="fullscreen"])'
        self.assertIn(f"{inline} .ev-pkg .ev-top,\n  {inline} .ev-pkg .ev-left {{ display: contents; }}", css)
        self.assertIn(".ev-pkg .ev-prov, .ev-pkg > .notice, .ev-pkg > .disclosure-body { order: 1; }", css)
        ground = self._function("groundNode", "mediaSlot")
        self.assertNotIn("candidateNode(", ground)
        order = [
            'el("p", "ev-sum", pkg.summary)',
            'traitsNode("Weakness", "bad", "minus", flags, "ev-weak")',
            'el("p", "ev-pitch", pres.elevator_pitch)',
            "var abilities = abilitiesNode(pres);",
            "flow.appendChild(flavorNode(pres.craft_note));",
        ]
        positions = [ground.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_the_frame_is_art_badge_plate_stats_ribbon(self) -> None:
        frame = self._function("cardNode", "candidateNode")
        order = [
            'var frame = frameNode("article", "ev-card", tier);',
            "art.appendChild(coverNode(game));",
            "badgeNode({ value: critic.value, tag: critic.source, tier: critic.tier })",
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
        # The shared lead critic (apps_shared.SCORE_CHIP_JS), the same one the
        # game cards read; no local precedence (executed in
        # tests/test_apps.py::LeadCriticBehaviourTests).
        self.assertNotIn("function criticBadge(", apps_eval.EVAL_CARD_HTML)
        frame = self._function("cardNode", "candidateNode")
        self.assertIn("var critic = leadCritic(pkg.craft);", frame)
        lead = apps_shared.SCORE_CHIP_JS
        lead = lead[lead.index("function leadCritic(scores)"):lead.index("var STEAM_STEPS")]
        self.assertLess(lead.index("s.opencritic_score"), lead.index("s.metacritic_score"))
        self.assertIn('source: "OpenCritic", tier: ocTier(oc, s.opencritic_tier)', lead)
        self.assertIn('source: "Metacritic", tier: mcTier(mc)', lead)
        self.assertIn("return null;", lead)

    def test_the_plate_numbers_the_card_and_spans_its_sub_line(self) -> None:
        card = self._function("evalCard", "noteCard")
        self.assertIn("cardNo(data.assessment_id != null ? data.assessment_id : game.game_id)", card)
        self.assertIn('return "No. " + (s.length < 3 ? ("00" + s).slice(-3) : s);',
                      self._function("cardNo", "capFirst"))
        plate = self._function("plateNode", "fitStat")
        for marker in (
            'el("h2", "plate-title", game.name || "Unknown game")',
            'el("span", "card-no", no)',
            "var dev = (pkg.pedigree || {}).developer || {};",
            'sub.appendChild(el("span", null, String(dev.name)));',
            "var platform = price.platform || list(own.platforms).filter(Boolean)[0];",
            'el("span", "loz", label("platform_short", platform))',
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
            + self._function("cardNode", "candidateNode")
            + self._function("groundNode", "mediaSlot")
        )
        breakdown = self._function("breakdownNode", "storeAppid")
        sections = (
            "forYouNode(",
            "anchorsNode(",
            "lineageNode(",
            "studioStrip(",
            "pastNode(",
            "errorDetailNode(",
        )
        for section_call in sections:
            self.assertNotIn(section_call, inline)
            self.assertIn(section_call, breakdown)
        positions = [breakdown.index(c) for c in sections]
        self.assertEqual(positions, sorted(positions))
        # the library strip is never part of the breakdown: one copy, after
        # the reel inline and before it in fullscreen
        self.assertNotIn("libraryNode(", breakdown)
        self.assertIn("breakdownNode(fs.node, fs.pkg);", self._function("syncDisplayMode", "provenanceNode"))

    def test_breakdown_sections_are_trait_columns_minis_and_a_ledger(self) -> None:
        you = self._function("forYouNode", "anchorsNode")
        self.assertIn('traitsNode("For you if", "good", "plus", yes)', you)
        self.assertIn('traitsNode("Not for you if", "bad", "minus", no)', you)
        self.assertIn('if (pair.childNodes.length === 1) pair.classList.add("ev-one");', you)
        lineage = self._function("lineageNode", "pastNode")
        self.assertIn('eyebrowSection(parent, "Lineage").appendChild(pair);', lineage)
        # F1: a comparison he has carries its cover (no plate) and year
        self.assertIn("item.appendChild(miniCard({ name: c.name, cover_url: c.cover_url, tier: ratedTier(c.my_rating),",
                      lineage)
        self.assertIn("year: c.release_year, platform: c.platform }) }));", lineage)
        # the lineage minis read the same line format as every strip (B3)
        self.assertIn("lines: miniLines({ rating: c.my_rating, hours: c.playtime_hours, owned: c.owned,", lineage)
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
            'tr.appendChild(el("td", null, dayMonthYear(p.assessed_at) || "earlier"));',
            'ribbonNode(label("verdict", p.verdict), verdictTier(p.verdict), null, "s")',
            "var seen = money(p.price_seen, p.price_currency);",
            'eyebrowSection(parent, "Past verdicts")',
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
        # the shared store pill (apps_shared.NOTICE_JS), as the game cards' is
        self.assertIn('var pill = storePill("https://store.steampowered.com/app/" + appid + "/", "Store page");',
                      button)
        self.assertIn('pill.classList.add("act-store");', button)
        self.assertNotIn("primary", button)
        self.assertNotIn("EXT_ICON", apps_eval.EVAL_CARD_HTML)
        # Only a positive integer appid yields a button; anything else is null.
        appid = self._function("storeAppid", "storeButton")
        self.assertIn("var appid = (pkg.game || {}).steam_appid;", appid)
        for guard in ('typeof appid === "number"', "appid > 0", "Math.floor(appid) === appid"):
            self.assertIn(guard, appid)
        # The two pills share the row at every width, labels on one line.
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        rule = css_rule(css, ".actions > .btn, .actions > .disclosure")
        self.assertIn("flex: 1 1 auto;", rule)
        self.assertIn(".actions > .btn, .actions > .disclosure, .grid-head > .btn, .topbar > .btn "
                      "{ white-space: nowrap; }", apps_shared.CONTROLS_CSS)
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
            # the library strip reads before the reel in fullscreen (one
            # copy, moved by order — never a hidden second strip)
            'html[data-display-mode="fullscreen"] .ev-pkg > .ev-lib { order: -1; }',
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
            'if (realScore(mc) && !(badge && badge.source === "Metacritic")) {',
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
            'var node = notice(parent, errorSentences(errors).join(" "));',
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
        # the shared month-year date (NUMBERS_JS), no local date helper
        self.assertIn("var when = dayMonthYear(data.assessed_at);", prov)
        self.assertNotIn("function dateLabel(", apps_eval.EVAL_CARD_HTML)

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
        # IN YOUR LIBRARY is the tag-similarity row as mini cards (≤8) in the
        # shared mini format (pips, hours + status, year); how alike it is
        # and why ("48% similar; shares stealth, drama") is the hover text.
        library = self._function("libraryNode", "errorItem")
        self.assertIn('el("div", "section-title", "In your library")', library)
        self.assertIn("tier: ratedTier(item.my_rating),", library)
        self.assertIn("title: similarTitle(item),", library)
        title = self._function("similarTitle", "ministrip")
        self.assertIn('if (sim != null) parts.push(Math.round(sim * 100) + "% similar");', title)
        self.assertIn('if (why.length) parts.push("shares " + why.join(", "));', title)
        self.assertIn('return parts.length ? parts.join("; ") : null;', title)
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
        # the big-studio headline's catalogue size (the shared PEDIGREE_JS)
        self.assertIn('plural(size, "game", ped.catalog_truncated)', apps_eval.EVAL_CARD_HTML)

    def test_nothing_tilts(self) -> None:
        # The toybox tilt and the old -3deg verdict badge are gone: the only rotations are
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
            attrs: {}, setAttribute: function (k, v) { n.attrs[k] = String(v); },
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
var clicked = { defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
var pill = wrap.children[0].children[2];
pill.handlers.click(clicked);
out.opened = opened;
out.pill = [pill.tag, pill.className, pill.href, pill.attrs["data-link"], clicked.defaultPrevented];
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
        # the real shared store pill (apps_shared.NOTICE_JS), not a stub
        notice = apps_shared.NOTICE_JS
        pill = notice[notice.index("  var EXT_LINK_ICON"):notice.index("  var NOTICE_ICON")]
        assert NODE is not None
        proc = subprocess.run(
            [NODE, "-e", _ACTION_ROW_SHIM + pill + builders + _ACTION_ROW_PROBE],
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
        # the shared store pill: a real link that never navigates the sandbox
        self.assertEqual(self.out["pill"], ["a", "btn act-store", "https://store.steampowered.com/app/1145350/",
                                            "", True])


_ERRORS_PROBE = r"""
var pkg = {
  time: { hltb_main_hours: 18 },
  media: { trailer: { kind: "youtube", video_id: "x" }, screenshots: [] },
  pedigree: { developer: { name: "Retro" }, timeline: { before: [], after: [] } },
  errors: ["hltb: completionist time unavailable", "media: steam: no trailer",
           "igdb: unresolved", "pace: unavailable", "", null],
};
var bare = { errors: ["hltb: down", "media: fetch failed", "igdb: unresolved"] };
var tags = "similar: skipped \u2014 fewer than 3 tags on this row (not enriched yet)";
var year = new Date().getFullYear();
var gap = function (game) { return packageErrors({ game: game, errors: [tags, "similar: lookup failed"] }); };
console.log(JSON.stringify({ full: packageErrors(pkg), bare: packageErrors(bare),
  released: gap({ release_year: 2017 }), undated: gap({ release_year: null }), noGame: gap(undefined),
  future: gap({ release_year: year + 1 }), thisYear: gap({ release_year: year }) }));
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

    def test_too_few_tags_is_silent_before_release(self) -> None:
        # Round-2 F8: a future game has no community tags yet by definition,
        # so the similar line is held back; a failed lookup is still
        # reported. A released game keeps the tags line, and so does an
        # undated one: no year is an unknown, not "not out yet".
        tags = "similar: skipped \u2014 fewer than 3 tags on this row (not enriched yet)"
        for key in ("released", "thisYear", "undated", "noGame"):
            with self.subTest(case=key):
                self.assertEqual(self.out[key], [tags, "similar: lookup failed"])
        self.assertEqual(self.out["future"], ["similar: lookup failed"])


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
function item(t) { var i = errorItem(t); return { what: i.what, source: i.source, why: i.why }; }
function say(t) { return errorItem(t).text; }
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
  sentences: {
    similarTags: say("similar: skipped — fewer than 3 tags on this row (not enriched yet)"),
    similar: say("similar: lookup failed"),
    anchors: say("anchors: lookup failed"),
    pace: say("pace: unavailable"),
    pedigree: say("pedigree: unavailable"),
    studio: say("studio: unavailable"),
    igdbUnresolved: say("igdb: unresolved — no igdb_id stored and no unique exact-name match"),
    igdb: say("igdb: rate limited"),
    steam: say("steam: appdetails failed"),
    hltb: say("hltb: down"),
    mediaSteam: say("media: steam: fetch failed"),
    mediaBare: say("media: fetch failed"),
    package: say("package: assembly failed"),
    unknown: say("weird_block: boom"),
  },
};
var p = el("div");
out.notice = notice(p, errorSentences(["similar: lookup failed", "media: steam: fetch failed", "weird_block: boom",
                                        "pedigree: x", "studio: y"]).join(" ")).textContent;
var detail = el("div");
errorDetailNode(detail, ["media: steam: fetch failed", "igdb_resolver: " + new Array(40).join("slow ")]);
out.detail = detail.kids[0].kids[0].kids.map(function (li) { return [li.textContent, li.attrs.title]; });
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ErrorLabelBehaviourTests(unittest.TestCase):
    """F2/F4, then round-2 F8, executed: every failure is ONE plain sentence
    naming what and where; the server's string is hover text only."""

    @classmethod
    def setUpClass(cls) -> None:
        html = apps_eval.EVAL_CARD_HTML
        start = html.index("  var ERROR_BLOCKS = {")
        mapping = html[start:html.index("  function errorHasData(", start)]
        sentences = html[html.index("  function errorSentences("):html.index("  function errorsNode(")]
        detail = html[html.index("  function errorDetailNode("):html.index("  function named(v)")]
        detail = detail[:detail.index("\n  }\n") + 4]
        script = (_ERROR_LABEL_SHIM + apps_shared.LABELS_JS + apps_shared.NOTICE_JS
                  + mapping + sentences + detail + _ERROR_LABEL_PROBE)
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

    def test_every_reason_is_one_plain_sentence(self) -> None:
        self.assertEqual(self.out["sentences"], {
            "similarTags": "Not enough tags yet to find similar games in your library.",
            "similar": "Couldn't compare it with the games in your library.",
            "anchors": "Couldn't load your history behind this verdict.",
            "pace": "Couldn't work out your recent play pace.",
            "pedigree": "Couldn't load the studio's games from IGDB.",
            "studio": "Couldn't load the studio's games from IGDB.",
            "igdbUnresolved": "Not linked to IGDB yet, so the studio's games are missing.",
            "igdb": "Couldn't load the studio from IGDB.",
            "steam": "Couldn't load store data from Steam.",
            "hltb": "Couldn't load time to beat from HowLongToBeat.",
            "mediaSteam": "Couldn't load screenshots or the trailer from Steam.",
            "mediaBare": "Couldn't load screenshots or the trailer.",
            "package": "Some evaluation details couldn't load.",
            "unknown": "Couldn't load weird block.",
        })
        for text in self.out["sentences"].values():
            with self.subTest(text=text):
                self.assertNotIn("—", text)                 # no em dash
                self.assertEqual(text.count("."), 1)              # one sentence
                self.assertTrue(text.endswith("."))

    def test_the_notice_is_the_sentences_once_each(self) -> None:
        self.assertEqual(self.out["notice"],
                         "Couldn't compare it with the games in your library. "
                         "Couldn't load screenshots or the trailer from Steam. "
                         "Couldn't load weird block. Couldn't load the studio's games from IGDB.")

    def test_the_detail_shows_the_sentence_and_hides_the_capped_reason(self) -> None:
        (media, media_title), (long_one, long_title) = self.out["detail"]
        self.assertEqual(media, "Couldn't load screenshots or the trailer from Steam.")
        self.assertEqual(media_title, "fetch failed")
        self.assertEqual(long_one, "Couldn't load igdb resolver.")
        self.assertNotIn("—", long_one)
        self.assertLessEqual(len(long_title), 120)
        self.assertTrue(long_title.endswith("…"))


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
        "pedigree": {"developer": {"name": "Insomniac Games"}, "timeline": {"before": [], "after": []},
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
    out.cand = q(card, ".ev-cand").children.filter(function (c) { return c.tagName === "SPAN"; }).map(txt);
    out.left = q(card, ".ev-left").children.map(function (c) { return c.className; });
    out.weak = q(flow, ".ev-weak").querySelectorAll(".trait").map(function (t) {
      return txt(t.children[t.children.length - 1]);
    });
    out.abilities = q(flow, ".ev-abil").querySelectorAll(".ability").map(function (a) { return txt(q(a, "b")); });
    out.wrap = card.children.map(function (c) { return c.className; });
    out.library = texts(q(card, ".ev-lib"), ".mini-name");
    out.prov = q(card, ".ev-prov").children.map(txt);
    // #11: every part once, inline and (after a re-render) in fullscreen
    function counts() {
      var c = q(root, ".eval");
      return [c.querySelectorAll(".ev-cand").length, c.querySelectorAll(".ev-prov").length,
              c.querySelectorAll(".ev-lib").length,
              c.querySelectorAll(".section-title").filter(function (t) { return t.textContent === "In your library"; }).length];
    }
    out.onceInline = counts();
    out.actions = q(card, ".actions").children.filter(function (c) { return c.tagName === "BUTTON"; }).map(txt);
    var toggle = q(card, ".act-breakdown");
    toggle.click();
    var bd = q(card, ".disclosure-inner");
    out.breakdown = bd.children.map(function (c) {
      var t = q(c, ".section-title");
      return t ? txt(t) : c.className;
    });
    out.traitHeads = texts(bd, ".traits-head");
    out.anchorPips = bd.querySelectorAll(".eyebrow-sec")[0].querySelectorAll(".on").length;
    out.ledger = q(bd, ".ev-past").querySelectorAll("tr").slice(1).map(function (tr) {
      return tr.children.map(function (td) { return td.textContent; });
    });
    out.ledgerRibbon = q(q(bd, ".ev-past"), ".ribbon").className;
    out.firstDeals = root.querySelectorAll(".deal").length;
    // A1: the host re-delivering the same result (a new object) redraws the
    // card still — one deal per payload, keyed on what the card is
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
           params: { structuredContent: JSON.parse(JSON.stringify(PACKAGE)) } });
    out.redeliveredDeals = root.querySelectorAll(".deal").length;
    out.redeliveredFrame = q(root, ".frame") ? q(root, ".frame").className : null;
    hostContext.displayMode = "fullscreen";
    document.documentElement.setAttribute("data-display-mode", "fullscreen");
    render(JSON.parse(JSON.stringify(PACKAGE)));
    out.onceFullscreen = counts();
    out.fsBuilt = q(root, ".fs-breakdown").children.length > 0;
    hostContext.displayMode = "inline";
    document.documentElement.setAttribute("data-display-mode", "inline");
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: {
      verdict: "play_what_you_own", name: "Slay the Spire II", assessed_at: "2026-10-03T08:40:00Z" } } });
    var note = q(root, ".frame");
    out.noteSub = q(note, ".sub") ? true : false;
    out.note = [note.className, txt(q(note, ".plate-title-s")), txt(q(note, ".ribbon")),
                q(note, ".ribbon").className, txt(q(root, ".ev-nc-cap")), note.classList.contains("deal")];
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: {
      voided: true, assessment_id: 46, name: "Marvel's Wolverine", verdict: "wishlist_for_sale",
      assessed_at: "2026-10-03T13:04:42Z" } } });
    var gone = q(root, ".frame");
    out.voided = [gone.className, txt(q(gone, ".ribbon")), q(gone, ".ribbon").className, txt(q(root, ".ev-nc-cap"))];
    render({ verdict: "buy_now", name: "Hades II", assessed_at: "2026-10-03T08:40:00Z",
             playtime_hours: 12.5, platform: "epic" });
    var rich = q(root, ".sub");
    out.richSub = rich ? rich.children.map(function (s) { return [s.className, txt(s)]; }) : null;
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

    def test_note_cards_show_only_the_sub_spans_the_response_carries(self) -> None:
        # B9: hours played and a short platform lozenge under the title when
        # present; today's record / void responses carry neither, so no line
        self.assertFalse(self.out["noteSub"])
        self.assertEqual(self.out["richSub"], [["v", "13h"], ["", "played"], ["loz", "Epic"]])

    def test_the_straddling_ribbon_sits_on_the_frame_edge_clear_of_content(self) -> None:
        # B5: the ribbon (40px) is centred on the frame's outer bottom edge —
        # bottom: -(20px + frame) from the padding box — so it overlaps that
        # edge the same way on every sample, and the card's 28px bottom
        # padding keeps the last stat / chip row 14px clear of its top.
        # Measured in headless Chromium for samples 0 and 5 at 360 and 760:
        # ribbon mid == frame bottom, content ends 14px above the ribbon.
        straddle = css_rule(apps_shared.RIBBON_CSS, ".ribbon-straddle")
        self.assertIn("bottom: calc(-20px - var(--gl-frame));", straddle)
        self.assertIn("height: 40px;", css_rule(apps_shared.RIBBON_CSS, ".ribbon"))
        card = css_rule(widget_css(apps_eval.EVAL_CARD_HTML), ".ev-card")
        self.assertIn("padding-bottom: 28px;", card)

    def test_a_redelivered_result_does_not_replay_the_deal(self) -> None:
        # A1: same game + assessment + verdict + kind → the redraw is still;
        # the next, different response (the note card) deals again
        self.assertEqual(self.out["firstDeals"], 1)
        self.assertEqual(self.out["redeliveredDeals"], 0)
        self.assertNotIn("deal", self.out["redeliveredFrame"].split())
        self.assertTrue(self.out["note"][5])

    def test_the_frame_is_tiered_by_the_verdict_and_grained(self) -> None:
        frame = self.out["frame"]
        self.assertEqual(frame["tag"], "ARTICLE")
        self.assertEqual(frame["cls"].split()[:3], ["frame", "ev-card", "tier-ok"])
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
            "ev-sum", "traits ev-weak", "ev-pitch", "ev-abil", "flavor", "fs-breakdown",
        ])
        # the candidate line and the provenance sit under the card in
        # .ev-left; inline, CSS dissolves it and orders them into the flow
        self.assertEqual(self.out["left"], ["ev-cardwrap", "ev-cand is-candidate", "ev-prov"])
        self.assertEqual(self.out["cand"], ["Candidate", "Not owned, not wishlisted"])
        self.assertEqual(self.out["weak"], ["Repetitive combat per critics", "Lowest-rated PS Studios PS5 game"])
        self.assertEqual(self.out["abilities"], ["Studio", "Moment"])
        self.assertEqual(self.out["wrap"], ["ev-top", "ev-lib", "actions", "disclosure-body ev-bd"])
        self.assertEqual(self.out["library"], ["MGS3"])
        self.assertEqual(self.out["prov"], ["Published by Sony Interactive Entertainment",
                                            "assessed 3 Oct 2026"])
        self.assertEqual(self.out["actions"], ["Full breakdown▾"])

    def test_each_part_is_built_once_in_both_modes(self) -> None:
        # #11: one candidate line, one provenance line, one IN YOUR LIBRARY
        # strip per render — inline and fullscreen alike (no hidden twin)
        self.assertEqual(self.out["onceInline"], [1, 1, 1, 1])
        self.assertEqual(self.out["onceFullscreen"], [1, 1, 1, 1])
        self.assertTrue(self.out["fsBuilt"])
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        self.assertNotIn(".ev-side", css + apps_eval.EVAL_CARD_HTML)
        for selector in (".ev-cand", ".ev-lib", ".ev-prov"):
            self.assertNotRegex(css, re.escape(selector) + r"[^{]*\{[^}]*display: none")

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
        self.assertEqual(cls.split()[:4], ["frame", "frame-s", "ev-nc", "tier-good"])
        self.assertEqual((name, ribbon, caption), ("Slay the Spire II", "Play what you own",
                                                   "Recorded 3 Oct 2026"))
        self.assertEqual(ribbon_cls.split(), ["ribbon", "ribbon-s", "tier-good"])
        self.assertTrue(dealt)
        cls, ribbon, ribbon_cls, caption = self.out["voided"]
        self.assertEqual(cls.split()[:5], ["frame", "frame-s", "ev-nc", "ev-void", "tier-none"])
        self.assertEqual(ribbon, "Voided")
        self.assertIn("tier-none", ribbon_cls)
        self.assertEqual(caption, "Verdict No. 046 of 3 Oct 2026 voided")


# THE STORY, synthetic (never a claim about a real person): two sentences,
# two sources, the second sentence citing both.
_STORY = {
    "sentences": [
        {"text": "Studio Example was founded in 2019.", "sources": [1]},
        {"text": "Its writer also wrote Example Quest.", "sources": [1, 2]},
    ],
    "sources": [
        {"url": "https://www.example.test/interview", "kind": "press", "title": "An interview"},
        {"url": "https://studio.example.test/blog", "kind": "studio"},
    ],
}

_STORY_PROBE = r"""
  (async function () {
    function q(n, s) { return n.querySelector(s); }
    function txt(n) { return n ? n.textContent : null; }
    function withStory(story) {
      var pkg = JSON.parse(JSON.stringify(PACKAGE));
      if (story === undefined) delete pkg.package.presentation.story;
      else pkg.package.presentation.story = story;
      return pkg;
    }
    function storyOf() {
      var box = q(root, ".ev-story");
      if (!box) return null;
      return {
        tag: box.tagName,
        eyebrow: txt(q(box, ".section-title")),
        paragraphs: box.querySelectorAll("p").map(function (p) { return p.className; }),
        text: txt(q(box, ".story-text")),
        refs: box.querySelectorAll(".story-ref").map(txt),
        sup: box.querySelectorAll("sup").length,
        row: q(box, ".story-sources") ? q(box, ".story-sources").className : null,
        chips: box.querySelectorAll(".story-src").map(function (c) {
          return { tag: c.tagName, cls: c.className, href: c.href, link: c.hasAttribute("data-link"),
                   title: c.title || null,
                   spans: c.children.map(function (k) { return [k.className, k.textContent]; }) };
        }),
      };
    }
    var out = {};
    answer("ui/initialize", { hostCapabilities: {}, hostContext: {} });
    await tick();
    render(withStory(STORY));
    out.ground = q(root, ".ev-flow").children.map(function (c) { return c.className; });
    out.story = storyOf();
    var chip = q(root, ".story-src");
    var ev = chip.click();
    await tick();
    out.prevented = ev.defaultPrevented;
    var opened = sent("ui/open-link");
    out.opened = opened.length ? opened[opened.length - 1].params.url : null;

    render(withStory({ sentences: [{ text: "Cited.", sources: [1] }, { text: "Dangling.", sources: [9] }],
                       sources: [{ url: "not a url at all, just a rather long run of text", kind: "wiki" },
                                 { url: "https://uncited.example.test/", kind: "social" }] }));
    out.dangling = storyOf();

    out.empty = [undefined, null, {}, { sentences: [], sources: STORY.sources },
                 { sentences: STORY.sentences, sources: [] },
                 { sentences: [{ text: "", sources: [1] }], sources: STORY.sources }].map(function (story) {
      render(withStory(story));
      return q(root, ".ev-story") ? "rendered" : null;
    });
    console.log(JSON.stringify(out));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class StoryBlockRenderTests(unittest.TestCase):
    """THE STORY: one serif paragraph, mono refs, one source chip per citation."""

    @classmethod
    def setUpClass(cls) -> None:
        from test_apps_shared import run_widget

        probe = ("  var PACKAGE = " + json.dumps(_RENDER_PACKAGE) + ";\n  var STORY = "
                 + json.dumps(_STORY) + ";\n" + _STORY_PROBE)
        cls.out = run_widget("eval-card", probe)

    def test_the_story_sits_after_the_abilities_and_before_the_craft_note(self) -> None:
        self.assertEqual(self.out["ground"], [
            "ev-sum", "traits ev-weak", "ev-pitch", "ev-abil", "ev-story", "flavor", "fs-breakdown",
        ])

    def test_one_paragraph_with_a_ref_after_each_sentence(self) -> None:
        story = self.out["story"]
        self.assertEqual(story["tag"], "SECTION")
        self.assertEqual(story["eyebrow"], "The story")
        self.assertEqual(story["paragraphs"], ["story-text"])
        self.assertEqual(story["refs"], ["[1]", "[1,2]"])
        self.assertEqual(story["sup"], 0)
        # a thin NO-BREAK space between a sentence and its ref (a ref never
        # wraps to the start of the next line); a space between sentences
        self.assertEqual(story["text"], "Studio Example was founded in 2019.\u202f[1] "
                                        "Its writer also wrote Example Quest.\u202f[1,2]")

    def test_one_link_chip_per_cited_source_as_three_spans(self) -> None:
        story = self.out["story"]
        self.assertIn("story-sources", story["row"].split())
        self.assertEqual([c["spans"] for c in story["chips"]], [
            [["story-n", "[1]"], ["story-dom", "example.test"], ["story-kind", "Press"]],
            [["story-n", "[2]"], ["story-dom", "studio.example.test"], ["story-kind", "Studio"]],
        ])
        first, second = story["chips"]
        self.assertEqual((first["tag"], first["href"], first["link"]),
                         ("A", "https://www.example.test/interview", True))
        self.assertIn("chip", first["cls"].split())
        self.assertEqual(first["title"], "An interview")
        self.assertEqual(second["href"], "https://studio.example.test/blog")

    def test_a_chip_opens_through_the_host_like_the_store_pill(self) -> None:
        self.assertTrue(self.out["prevented"])
        self.assertEqual(self.out["opened"], "https://www.example.test/interview")

    def test_a_missing_index_renders_without_a_ref_and_without_throwing(self) -> None:
        dangling = self.out["dangling"]
        self.assertEqual(dangling["refs"], ["[1]"])
        self.assertEqual(dangling["text"], "Cited.\u202f[1] Dangling.")
        # only the cited source gets a chip; an unparseable url shows its
        # first 40 characters as the "domain"
        self.assertEqual([c["spans"] for c in dangling["chips"]], [
            [["story-n", "[1]"], ["story-dom", "not a url at all, just a rather long run"],
             ["story-kind", "Wiki"]],
        ])

    def test_an_absent_or_empty_story_renders_nothing(self) -> None:
        self.assertEqual(self.out["empty"], [None] * 6)


class StoryBlockCssTests(unittest.TestCase):
    def test_the_paragraph_is_upright_serif_body_text(self) -> None:
        rule = css_rule(widget_css(apps_eval.EVAL_CARD_HTML), ".story-text")
        for decl in ("font-family: var(--gl-serif);", "font-style: normal;",
                     "font-size: var(--gl-body);", "line-height: 1.55;", "color: var(--gl-text);"):
            self.assertIn(decl, rule)

    def test_refs_hold_the_12px_floor_in_mono(self) -> None:
        rule = css_rule(widget_css(apps_eval.EVAL_CARD_HTML), ".story-ref")
        for decl in ("font-family: var(--gl-mono);", "font-size: var(--gl-cap);",
                     "color: var(--gl-muted);"):
            self.assertIn(decl, rule)
        self.assertNotIn("<sup", apps_eval.EVAL_CARD_HTML)
        self.assertNotIn('"sup"', apps_eval.EVAL_CARD_HTML)

    def test_fullscreen_places_the_story_between_abilities_and_flavor(self) -> None:
        css = widget_css(apps_eval.EVAL_CARD_HTML)
        fs = 'html[data-display-mode="fullscreen"] .ev-flow > '
        self.assertIn(fs + ".ev-abil { order: 5; }", css)
        self.assertIn(fs + ".ev-story { order: 6; max-width: 560px; }", css)
        self.assertIn(fs + ".flavor { order: 7; }", css)

    def test_the_story_stays_local_to_the_eval_card(self) -> None:
        for name, block in shared_blocks():
            with self.subTest(block=name):
                self.assertIsNone(re.search(r"\bstory", block))


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
