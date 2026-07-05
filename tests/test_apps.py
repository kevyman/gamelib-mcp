"""Tests for the MCP Apps game-cards widget and cover-art plumbing."""

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
            "ui/notifications/initialized",
            "ui/notifications/tool-result",
            "ui/notifications/size-changed",
            "__PREVIEW_DATA__",
        ):
            self.assertIn(marker, html)

    def test_csp_allows_exactly_the_cover_hosts(self) -> None:
        domains = apps._GAME_CARDS_CSP.resource_domains
        self.assertEqual(
            domains,
            ["https://images.igdb.com", "https://cdn.cloudflare.steamstatic.com"],
        )


if __name__ == "__main__":
    unittest.main()
