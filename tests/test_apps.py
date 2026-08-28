"""Tests for the MCP Apps game-cards widget and cover-art plumbing."""

import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client, FastMCP

from gamelib_mcp import apps
from gamelib_mcp.data import igdb
from gamelib_mcp.tools.common import cover_url


class CoverUrlTests(unittest.TestCase):
    def test_igdb_slug_preferred(self) -> None:
        self.assertEqual(
            cover_url("co1wyy", 1145360),
            "https://images.igdb.com/igdb/image/upload/t_cover_big/co1wyy.jpg",
        )

    def test_steam_capsule_fallback(self) -> None:
        self.assertEqual(
            cover_url(None, 1145360),
            "https://cdn.cloudflare.steamstatic.com/steam/apps/1145360/library_600x900.jpg",
        )

    def test_no_sources(self) -> None:
        self.assertIsNone(cover_url(None, None))


class IGDBCoverParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_requests_and_parses_cover(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            self.assertIn("cover.image_id", query)
            return [
                {
                    "id": 113112,
                    "name": "Hades",
                    "category": igdb.CATEGORY_MAIN_GAME,
                    "cover": {"id": 89530, "image_id": "co39vc"},
                }
            ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Hades")

        self.assertEqual(results[0].cover_image_id, "co39vc")

    async def test_missing_or_malformed_cover_is_none(self) -> None:
        for cover in (None, 89530):  # absent, or a bare id with no expansion
            item = {"id": 1, "name": "X", "category": igdb.CATEGORY_MAIN_GAME}
            if cover is not None:
                item["cover"] = cover
            self.assertIsNone(igdb._parse_igdb_item(item).cover_image_id)


class GameCardsResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_registered_and_serves_widget(self) -> None:
        mcp = FastMCP("test")
        apps.register_apps(mcp)
        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = [str(r.uri) for r in resources]
            self.assertIn(apps.GAME_CARDS_URI, uris)
            content = await client.read_resource(apps.GAME_CARDS_URI)
            html = content[0].text

        # The hand-rolled bridge must speak the MCP Apps handshake and the
        # result notification, and handle preview injection for local review.
        for marker in (
            "ui/initialize",
            "appInfo",              # required by the ext-apps SDK schema
            "ui/notifications/initialized",
            "ui/notifications/tool-result",
            "ui/notifications/size-changed",
            "tools/call",           # click-to-expand fetches get_game_detail
            "get_game_detail",
            "ui/open-link",         # rating pills link out via the host
            "__PREVIEW_DATA__",
        ):
            self.assertIn(marker, html)

    def test_rating_chips_use_source_brand_colors(self) -> None:
        # Verified against the sites' own stylesheets: OpenCritic tier
        # variables, Steam's .game_review_summary rules, Metacritic's classic
        # metascore box colors. Locks the research so a restyle can't silently
        # drift off-brand.
        for hex_color in (
            "#fc430a", "#9e00b4", "#4aa1ce", "#80b06a",  # OpenCritic tiers
            "#66c0f4", "#b9a074", "#c85e2d",             # Steam summary text
            "#6c3", "#fc3", "#f00",                      # Metacritic metascore
        ):
            self.assertIn(hex_color, apps.GAME_CARDS_HTML)

    def test_csp_allows_exactly_the_cover_and_media_hosts(self) -> None:
        # Covers + the media hosts a get_game_detail(media=True) card needs:
        # Steam serves screenshots and movie posters from shared.*, the mp4
        # renditions from cdn.*, and IGDB trailers are YouTube thumbnails.
        self.assertEqual(
            apps._GAME_CARDS_CSP.resource_domains,
            [
                "https://images.igdb.com",
                "https://cdn.cloudflare.steamstatic.com",
                "https://cdn.akamai.steamstatic.com",
                "https://shared.akamai.steamstatic.com",
                "https://shared.cloudflare.steamstatic.com",
                "https://i.ytimg.com",
            ],
        )

    def test_csp_frames_only_the_privacy_mode_youtube_host(self) -> None:
        # frame_domains feeds frame-src; the trailer embed is the only nested
        # frame the widget ever creates, and only after a click.
        self.assertEqual(
            apps._GAME_CARDS_CSP.frame_domains,
            ["https://www.youtube-nocookie.com"],
        )


class ContentTypeBadgeTests(unittest.TestCase):
    """The DLC/expansion/edition badge + "part of <base game>" subtitle.

    apps.py has no headless-DOM test harness (see GameCardsResourceTests
    above), so these follow the same source-presence style: they assert the
    label map and render call-sites exist with the right shape rather than
    executing the JS.
    """

    def test_nested_content_types_have_human_labels(self) -> None:
        # Nested types (data/content.py::NESTED_CONTENT_TYPES) each map to a
        # short human label used for both the grid chip and the detail badge.
        for key, label in (
            ("dlc", "DLC"),
            ("expansion", "Expansion"),
            ("bundle", "Bundle"),
            ("edition", "Edition"),
            ("unknown_addon", "Add-on"),
        ):
            self.assertIn(f'{key}: "{label}"', apps.GAME_CARDS_HTML)

    def test_primary_content_types_are_not_in_the_label_map(self) -> None:
        # Primary types (data/content.py::PRIMARY_CONTENT_TYPES) must render
        # no badge at all — contentTypeLabel() falls back to null for any key
        # not in CONTENT_TYPE_LABELS, so the map must never mention them.
        start = apps.GAME_CARDS_HTML.index("var CONTENT_TYPE_LABELS")
        end = apps.GAME_CARDS_HTML.index("};", start)
        label_map_src = apps.GAME_CARDS_HTML[start:end]
        for primary_type in (
            "base_game",
            "standalone_expansion",
            "remake",
            "remaster",
            "expanded_game",
            "port",
        ):
            self.assertNotIn(primary_type, label_map_src)

    def test_grid_card_renders_type_chip_and_parent_subtitle(self) -> None:
        self.assertIn('el("span", "type-chip", typeLabel)', apps.GAME_CARDS_HTML)
        self.assertIn('el("div", "parent-sub", "⤷ " + pName)', apps.GAME_CARDS_HTML)

    def test_detail_card_renders_content_badge_and_parent_subtitle(self) -> None:
        self.assertIn(
            'el("span", "badge content-badge", typeLabel)', apps.GAME_CARDS_HTML
        )
        self.assertIn('el("div", "sub parent-sub", "part of " + pName)', apps.GAME_CARDS_HTML)

    def test_parent_name_supports_both_grid_and_detail_shapes(self) -> None:
        # Grid/search rows carry a flat parent_name; get_game_detail carries
        # parent: {game_id, name} on nested rows. parentName() must read both.
        self.assertIn("game.parent && game.parent.name", apps.GAME_CARDS_HTML)
        self.assertIn("game.parent_name", apps.GAME_CARDS_HTML)

    def test_badge_and_subtitle_text_use_textcontent_not_innerhtml(self) -> None:
        # The widget's only escaping mechanism is el()'s use of textContent
        # (never innerHTML with payload data) — verify the new badge/subtitle
        # strings go through that same helper rather than string concatenation
        # into markup.
        self.assertNotIn("innerHTML", apps.GAME_CARDS_HTML)

    def test_media_sections_render_from_the_detail_payload(self) -> None:
        # The detail card grows a hero, a screenshot strip and a similar-games
        # row when get_game_detail(media=True) supplies them. Source-presence
        # style, like the badge tests above — there is no headless-DOM harness.
        for marker in (
            'var media = game.media || {};',      # detailCard reads the block
            'heroNode(media)',
            'shotsNode(stack, media, game.name)',
            'if (game.similar) similarNode(stack, game.similar)',
            'el("div", "sim-name", item.name)',
            '"You own " + owned + " of the " + shown + " most similar"',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)

    def test_hero_is_trailer_only_and_never_autoplays(self) -> None:
        # Screenshots alone make no hero here (the card leads with the cover),
        # and the mp4 hero fetches zero bytes until the viewer hits play.
        self.assertIn(
            "if (!trailer || !(trailer.url || trailer.video_id)) return null;",
            apps.GAME_CARDS_HTML,
        )
        self.assertIn('video.preload = "none";', apps.GAME_CARDS_HTML)
        # …and the YouTube branch loads nothing until the click either.
        self.assertIn(
            'frame.src = "https://www.youtube-nocookie.com/embed/"',
            apps.GAME_CARDS_HTML,
        )

    def test_screenshots_open_in_a_lightbox_over_the_detail_card(self) -> None:
        # The overlay machinery is a stack, so enlarging a screenshot from
        # inside a detail overlay must not close the card underneath it.
        self.assertIn("openShot(shot, label, btn)", apps.GAME_CARDS_HTML)
        self.assertIn('el("div", "overlay-panel shot-panel")', apps.GAME_CARDS_HTML)
        self.assertIn(
            'if (ev.key === "Escape" && overlays[overlays.length - 1] === entry)',
            apps.GAME_CARDS_HTML,
        )

    def test_grid_overlay_upgrade_call_requests_media(self) -> None:
        self.assertIn(
            'callTool("get_game_detail", { game_id: game.game_id, media: true })',
            apps.GAME_CARDS_HTML,
        )

    def test_widget_stays_dependency_free(self) -> None:
        # No CDN, no external script, and still no innerHTML anywhere — the
        # media sections build every node through el()/createElement.
        self.assertNotIn("<script src", apps.GAME_CARDS_HTML)
        self.assertNotIn("innerHTML", apps.GAME_CARDS_HTML)

    def test_uri_is_content_hashed_and_reflects_current_html(self) -> None:
        expected = (
            "ui://gamelib/game-cards-"
            + hashlib.sha1(apps.GAME_CARDS_HTML.encode()).hexdigest()[:8]
            + ".html"
        )
        self.assertEqual(apps.GAME_CARDS_URI, expected)


if __name__ == "__main__":
    unittest.main()
