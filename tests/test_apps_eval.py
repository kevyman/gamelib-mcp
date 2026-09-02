"""Tests for the MCP Apps evaluation-card widget.

Same approach as tests/test_apps.py: there is no headless-DOM harness for the
widget JS, so these assert the module's registration/CSP contract and the
presence of the render call-sites and host-quirk workarounds that the layout
depends on, rather than executing the script.
"""

import difflib
import hashlib
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
        for verdict, label, cls in (
            ("buy_now", "BUY NOW", "stamp-good"),
            ("wishlist_for_sale", "WISHLIST FOR SALE", "stamp-ok"),
            ("try_demo", "TRY THE DEMO", "stamp-p1"),
            ("play_what_you_own", "PLAY WHAT YOU OWN", "stamp-p4"),
            ("skip", "SKIP", "stamp-bad"),
        ):
            self.assertIn(f'{verdict}: ["{label}", "{cls}"]', apps_eval.EVAL_CARD_HTML)

    def test_render_branches_cover_the_whole_response_contract(self) -> None:
        # package -> full card, voided -> note, verdict -> note, else empty.
        for marker in (
            "if (data && data.package)",
            "else if (data && data.voided)",
            "else if (data && data.verdict)",
            '"Assessment voided"',
            '"Recorded: "',
            '"Nothing to display."',
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)

    def test_trailer_falls_back_when_the_media_element_fails(self) -> None:
        # Valve's constructed mp4 URLs are undocumented legacy surface and a
        # host may strip media-src — both land as source errors, which have to
        # be caught in the capture phase because they don't bubble.
        self.assertIn('video.addEventListener("error", function () {', apps_eval.EVAL_CARD_HTML)
        self.assertIn("posterFallback(hero, trailer);", apps_eval.EVAL_CARD_HTML)
        self.assertIn('video.preload = "none";', apps_eval.EVAL_CARD_HTML)

    def test_pedigree_strip_renders_from_the_package(self) -> None:
        for marker in (
            "pedigreeNode(wrap, pkg.pedigree)",
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
        self.assertIn('el("span", "tag rated", rating + "/10")', badges)
        self.assertIn("} else if (critic != null && critic >= 0) {", badges)
        self.assertIn("if (item.owned && rating == null) tags.appendChild", badges)

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
        for kind, label, cls in (
            ("people", "PEOPLE", "wc-people"),
            ("studio", "STUDIO", "wc-studio"),
            ("anticipation", "HYPE", "wc-hype"),
            ("moment", "MOMENT", "wc-moment"),
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

    def test_anchor_pills_are_neutral_and_only_the_status_is_coloured(self) -> None:
        # The live card lit up "Cyberpunk 2077 6.6h" — a game he bounced off —
        # in endorsement green. The pill is card-coloured now; the completion
        # glyph carries good/bad, and an unstatused anchor carries neither.
        for verdictish, cls in (
            ("completed", "an-good"),
            ("evergreen", "an-good"),
            ("abandoned", "an-bad"),
        ):
            self.assertIn(f'"{cls}"', apps_eval.EVAL_CARD_HTML)
            self.assertIn(f"{verdictish}: [", apps_eval.EVAL_CARD_HTML)
        self.assertIn('playing: ["▶", "playing", ""]', apps_eval.EVAL_CARD_HTML)
        self.assertIn(
            'el("span", "an-state" + (state[2] ? " " + state[2] : ""), state[0])',
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn(".an-state.an-good { background: var(--good)", apps_eval.EVAL_CARD_HTML)
        self.assertIn(".an-state.an-bad { background: var(--bad)", apps_eval.EVAL_CARD_HTML)
        # …and the pill itself no longer paints an opinion.
        self.assertNotIn("an-warn", apps_eval.EVAL_CARD_HTML)


class EvalCardLayoutTests(unittest.TestCase):
    """The v2 layout (field feedback: "too busy, not ordered naturally").

    Source-presence style like the rest of this module: the render call-sites
    and the panels they build, in the order the card assembles them.
    """

    def test_panels_assemble_in_the_reading_order(self) -> None:
        start = apps_eval.EVAL_CARD_HTML.index("function evalCard(pkg)")
        end = apps_eval.EVAL_CARD_HTML.index("function noteCard(", start)
        body = apps_eval.EVAL_CARD_HTML[start:end]
        order = [
            "headerNode(pkg)",
            "pitchNode(wrap, pkg)",
            "mediaNode(wrap, pkg.media || {}, game.name)",
            "forYouNode(wrap, pkg.presentation || {})",
            "anchorsNode(wrap,",
            "lineageNode(wrap, pkg, comps)",
            "similarNode(wrap, pkg.similar || {})",
            "pedigreeNode(wrap, pkg.pedigree)",
            "closingNode(wrap, pkg)",
            "errorsNode(wrap,",
        ]
        positions = []
        for marker in order:
            self.assertIn(marker, body)
            positions.append(body.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_the_score_chips_live_in_the_header_panel(self) -> None:
        # The standalone "CRAFT & FIT" panel is gone — it held two chips.
        self.assertIn("var chips = scoreChips(pkg);", apps_eval.EVAL_CARD_HTML)
        self.assertIn('el("div", "chips head-chips")', apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("Craft & fit", apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("scoresNode", apps_eval.EVAL_CARD_HTML)

    def test_metacritic_renders_as_its_own_branded_square(self) -> None:
        # Same source-brand mapping as the game-cards widget: square box,
        # green/yellow/red at the games thresholds.
        self.assertIn(
            'function mcTier(n) { return n >= 75 ? "mc-hi" : n >= 50 ? "mc-mid" : "mc-lo"; }',
            apps_eval.EVAL_CARD_HTML,
        )
        self.assertIn('el("span", "chip mc " + mcTier(mc))', apps_eval.EVAL_CARD_HTML)
        self.assertIn("num(craft.metacritic_score)", apps_eval.EVAL_CARD_HTML)
        for hex_color in ("#6c3", "#fc3", "#f00"):
            self.assertIn(hex_color, apps_eval.EVAL_CARD_HTML)

    def test_the_craft_note_renders_under_the_chips(self) -> None:
        self.assertIn(
            'if (pres.craft_note) box.appendChild(el("div", "craft-note", pres.craft_note));',
            apps_eval.EVAL_CARD_HTML,
        )

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
                         .split('section(parent, "Similar games")')[0])

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
        # Model-authored "similar" note-cards no longer fold into IGDB's strip:
        # mixing the two is what made the live Similar section confusing.
        self.assertIn("function lineageNode(parent, pkg, comps)", apps_eval.EVAL_CARD_HTML)
        self.assertIn('onlySimilar ? "Also similar" : "Other comparisons"',
                      apps_eval.EVAL_CARD_HTML)
        self.assertNotIn("foldSimilar", apps_eval.EVAL_CARD_HTML)
        self.assertIn("function similarNode(parent, similar)", apps_eval.EVAL_CARD_HTML)

    def test_the_closing_panel_merges_time_price_flags_and_past(self) -> None:
        self.assertIn('section(parent, "The call")', apps_eval.EVAL_CARD_HTML)
        for gone in ("Time & price", '"Flags"', "Past verdicts"):
            self.assertNotIn(gone, apps_eval.EVAL_CARD_HTML)

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

    def test_sticker_like_pills_tilt_like_the_game_cards_widget(self) -> None:
        # Toybox language: stickers tilt, data chips (score meter, pace/price)
        # stay straight so the numbers stay legible.
        for marker in (
            ".tag:nth-child(2n) { transform: rotate(",
            ".flag:nth-child(2n) { transform: rotate(",
            ".tl-chip:nth-child(2n) { transform: rotate(",
            ".anchor:nth-child(2n) { transform: rotate(",
            ".wc-line:nth-child(2n) .wc-eyebrow { transform: rotate(",
        ):
            self.assertIn(marker, apps_eval.EVAL_CARD_HTML)


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
    both. The longest identical run today is 15 non-blank lines (the tail of
    the initialize handshake, the document's closing tags, and the
    content-hash comment under them), so the cap leaves room for a small block
    to be ported deliberately before this fires.
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
