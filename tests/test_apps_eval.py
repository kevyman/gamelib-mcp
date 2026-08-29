"""Tests for the MCP Apps evaluation-card widget.

Same approach as tests/test_apps.py: there is no headless-DOM harness for the
widget JS, so these assert the module's registration/CSP contract and the
presence of the render call-sites and host-quirk workarounds that the layout
depends on, rather than executing the script.
"""

import hashlib
import unittest

from fastmcp import Client, FastMCP

from gamelib_mcp import apps_eval


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
            '"You\'ve played " + (num(record.played_count) || 0) + " of their "',
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
        # under the elevator pitch.
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

    def test_abandoned_anchor_is_styled_as_a_warning(self) -> None:
        # An abandoned anchor is negative evidence about fit.
        self.assertIn('abandoned: ["⚠", "abandoned", "an-warn"]', apps_eval.EVAL_CARD_HTML)
        self.assertIn(".anchor.an-warn", apps_eval.EVAL_CARD_HTML)


if __name__ == "__main__":
    unittest.main()
