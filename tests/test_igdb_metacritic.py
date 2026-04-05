import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import igdb, metacritic


class _DummyDb:
    def __init__(self, row):
        self._row = row

    async def execute_fetchone(self, *_args, **_kwargs):
        return self._row


class _DummyContext:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _DummyDb(self._row)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class IGDBRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_treats_token_failures_as_no_result(self) -> None:
        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(side_effect=EnvironmentError("missing"))),
        ):
            results = await igdb.search_game("Portal")

        self.assertEqual(results, [])

    async def test_resolve_and_link_game_reuses_fuzzy_candidate_before_insert(self) -> None:
        igdb_game = igdb.IGDBGame(
            igdb_id=99,
            name="Portal",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2007-10-10",
        )

        with (
            patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=igdb_game)),
            patch("gamelib_mcp.data.db.get_game_by_igdb_id", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.db.find_game_by_name_fuzzy", AsyncMock(return_value={"id": 7})),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.db.get_db") as get_db,
        ):
            game_id, linked_game = await igdb.resolve_and_link_game(
                name="Portal",
                igdb_platform_id=igdb.IGDB_PLATFORM_PC,
                candidates={7: "Portal"},
            )

        self.assertEqual(game_id, 7)
        self.assertIs(linked_game, igdb_game)
        apply_metadata.assert_awaited_once_with(7, igdb_game)
        get_db.assert_not_called()


class MetacriticRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_metacritic_prefers_platform_specific_url(self) -> None:
        expected_url = "https://www.metacritic.com/game/metal-slug-tactics/?platform=playstation-5"

        with (
            patch("gamelib_mcp.data.db.get_db", return_value=_DummyContext(None)),
            patch(
                "gamelib_mcp.data.metacritic._fetch_score_from_url",
                AsyncMock(return_value=(72, expected_url)),
            ) as fetch_score,
            patch("gamelib_mcp.data.metacritic.upsert_game_platform_enrichment", AsyncMock()) as upsert,
        ):
            fields = await metacritic.enrich_metacritic(3, "Metal Slug Tactics", "ps5")

        fetch_score.assert_awaited_once_with(expected_url)
        upsert.assert_awaited_once()
        self.assertEqual(fields["metacritic_score"], 72)
        self.assertEqual(fields["metacritic_url"], expected_url)

    def test_candidate_urls_fall_back_to_generic_slug(self) -> None:
        slug = "metal-slug-tactics"

        self.assertEqual(
            metacritic._candidate_urls(slug, "ps5"),
            [
                "https://www.metacritic.com/game/metal-slug-tactics/?platform=playstation-5",
                "https://www.metacritic.com/game/playstation-5/metal-slug-tactics/",
                "https://www.metacritic.com/game/metal-slug-tactics/",
            ],
        )
