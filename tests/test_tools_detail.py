"""Characterization tests for gamelib_mcp.tools.detail.

Enrichment calls (Steam Store / ProtonDB / HLTB) are patched to no-ops so the
test characterizes lookup + formatting only, without network.
"""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_rating
from gamelib_mcp.tools import detail


class GetGameDetailTests(ToolDBTestCase):
    def setUp(self):
        super().setUp()
        self._patchers = [
            patch.object(detail, "enrich_game", AsyncMock(return_value=None)),
            patch.object(detail, "get_protondb", AsyncMock(return_value=None)),
            patch.object(detail, "get_hltb", AsyncMock(return_value=None)),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        super().tearDown()

    async def test_requires_an_identifier(self):
        self.assertEqual(
            await detail.get_game_detail(),
            {"error": "Provide game_id, name, or appid"},
        )

    async def test_not_found_returns_error(self):
        self.assertEqual(
            await detail.get_game_detail(name="does-not-exist"),
            {"error": "Game not found in library"},
        )

    async def test_lookup_by_game_id_shape(self):
        gid = await make_steam_game(
            "Celeste",
            504230,
            playtime_minutes=300,
            tags=["platformer"],
            genres=["Indie"],
            metacritic_score=92,
        )
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(
            set(result),
            {
                "game_id",
                "appid",
                "steam_appid",
                "name",
                "release_date",
                "platforms",
                "playtime_hours",
                "playtime_2weeks_hours",
                "last_played_date",
                "is_farmed",
                "genres",
                "tags",
                "short_description",
                "steam_review_score",
                "steam_review_desc",
                "hltb_main",
                "hltb_extra",
                "hltb_complete",
                "protondb_tier",
            },
        )
        self.assertEqual(result["name"], "Celeste")
        self.assertEqual(result["appid"], 504230)
        self.assertEqual(result["playtime_hours"], 5.0)
        self.assertEqual(result["tags"], ["platformer"])
        self.assertEqual(result["genres"], ["Indie"])
        self.assertNotIn("my_rating", result)

    async def test_lookup_by_appid(self):
        await make_steam_game("Hollow Knight", 367520, playtime_minutes=120)
        result = await detail.get_game_detail(appid=367520)
        self.assertEqual(result["name"], "Hollow Knight")

    async def test_lookup_by_name_partial(self):
        await make_steam_game("Hollow Knight", 367520, playtime_minutes=120)
        result = await detail.get_game_detail(name="hollow")
        self.assertEqual(result["name"], "Hollow Knight")

    async def test_includes_rating_when_present(self):
        gid = await seed_game("Disco Elysium")
        await add_rating(gid, "backloggd", raw_score=5.0, normalized_score=10.0, review_text="GOAT")
        result = await detail.get_game_detail(game_id=gid)
        self.assertEqual(
            result["my_rating"],
            {
                "source": "backloggd",
                "raw_score": 5.0,
                "normalized_score": 10.0,
                "review_text": "GOAT",
            },
        )
