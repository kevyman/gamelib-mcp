import json
import unittest
from unittest.mock import ANY, AsyncMock, patch

from gamelib_mcp.data import igdb, metacritic
from gamelib_mcp.data.db import fuzzy as db_fuzzy


class _FakeResponse:
    def __init__(self, html: str, url: str, status_code: int = 200):
        self.text = html
        self.url = url
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.is_redirect = False

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return self._response


def _ld_json(rating_value, best_rating) -> str:
    block = {"@type": "Product", "aggregateRating": {
        "@type": "AggregateRating", "ratingValue": rating_value, "bestRating": best_rating}}
    return f'<html><head><script type="application/ld+json">{json.dumps(block)}</script></head></html>'


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

    async def test_resolve_and_link_game_records_edition_alias_on_parent_game(self) -> None:
        igdb_game = igdb.IGDBGame(
            igdb_id=200,
            name="Fallout New Vegas Ultimate Edition",
            category=igdb.CATEGORY_BUNDLE,
            first_release_date=None,
            content_type="edition",
            parent_name="Fallout: New Vegas",
            alias_for_parent=True,
            is_primary_library_item=False,
        )

        with (
            patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=igdb_game)),
            patch("gamelib_mcp.data.db.find_game_by_name_fuzzy", AsyncMock(return_value={"id": 7})),
            patch("gamelib_mcp.data.db.upsert_game_alias", AsyncMock()) as upsert_alias,
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
        ):
            game_id, linked_game = await igdb.resolve_and_link_game(
                name="Fallout New Vegas Ultimate Edition",
                igdb_platform_id=igdb.IGDB_PLATFORM_PC,
                candidates={7: "Fallout: New Vegas"},
            )

        self.assertEqual(game_id, 7)
        self.assertIs(linked_game, igdb_game)
        upsert_alias.assert_awaited_once_with(
            7,
            "Fallout New Vegas Ultimate Edition",
            alias_type="edition",
            source="igdb",
            source_key="200",
        )
        apply_metadata.assert_not_awaited()

    async def test_resolve_and_link_game_uses_local_edition_override_without_igdb(self) -> None:
        with (
            patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.db.find_game_by_name_fuzzy", AsyncMock(return_value={"id": 7})),
            patch("gamelib_mcp.data.db.upsert_game_alias", AsyncMock()) as upsert_alias,
            patch("gamelib_mcp.data.db.upsert_game", AsyncMock()) as upsert_game,
        ):
            game_id, linked_game = await igdb.resolve_and_link_game(
                name="Fallout New Vegas Ultimate Edition",
                igdb_platform_id=igdb.IGDB_PLATFORM_PC,
                candidates={7: "Fallout: New Vegas"},
            )

        self.assertEqual(game_id, 7)
        self.assertIsNone(linked_game)
        upsert_alias.assert_awaited_once_with(
            7,
            "Fallout New Vegas Ultimate Edition",
            alias_type="edition",
            source="local_override",
            source_key=None,
        )
        upsert_game.assert_not_awaited()

    async def test_resolve_and_link_game_uses_local_dlc_override_without_igdb(self) -> None:
        with (
            patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.db.find_game_by_name_fuzzy", AsyncMock(return_value={"id": 7})),
            patch("gamelib_mcp.data.db.upsert_game", AsyncMock(return_value=8)) as upsert_game,
        ):
            game_id, linked_game = await igdb.resolve_and_link_game(
                name="Fallout New Vegas: Dead Money",
                igdb_platform_id=igdb.IGDB_PLATFORM_PC,
                candidates={7: "Fallout: New Vegas"},
            )

        self.assertEqual(game_id, 8)
        self.assertIsNone(linked_game)
        upsert_game.assert_awaited_once_with(
            appid=None,
            name="Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=7,
            is_primary_library_item=0,
        )


class FuzzyIdentityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fuzzy_identity_rejects_conflicting_sequel_numbers(self) -> None:
        cases = [
            ("Borderlands 4", "Borderlands 2"),
            ("PowerWash Simulator 2", "PowerWash Simulator"),
            ("Red Dead Redemption", "Red Dead Redemption 2"),
        ]

        for query, candidate in cases:
            with self.subTest(query=query, candidate=candidate):
                with (
                    patch("gamelib_mcp.data.db.fuzzy.extract_best_fuzzy_key", return_value=7),
                    patch("gamelib_mcp.data.db.fuzzy.get_db", return_value=_DummyContext({"id": 7})),
                ):
                    row = await db_fuzzy.find_game_by_name_fuzzy(query, candidates={7: candidate})

                self.assertIsNone(row)

    async def test_fuzzy_identity_allows_same_sequel_number_variants(self) -> None:
        with (
            patch("gamelib_mcp.data.db.fuzzy.extract_best_fuzzy_key", return_value=7),
            patch("gamelib_mcp.data.db.fuzzy.get_db", return_value=_DummyContext({"id": 7})),
        ):
            row = await db_fuzzy.find_game_by_name_fuzzy(
                "Borderlands 4 Ultimate Edition",
                candidates={7: "Borderlands 4"},
            )

        self.assertEqual(row, {"id": 7})
        self.assertIsNone(
            db_fuzzy.find_conflicting_fuzzy_key(
                "Borderlands 4 Ultimate Edition",
                candidates={7: "Borderlands 4"},
            )
        )

    async def test_fuzzy_identity_allows_non_numbered_title_variants(self) -> None:
        with (
            patch("gamelib_mcp.data.db.fuzzy.extract_best_fuzzy_key", return_value=7),
            patch("gamelib_mcp.data.db.fuzzy.get_db", return_value=_DummyContext({"id": 7})),
        ):
            row = await db_fuzzy.find_game_by_name_fuzzy(
                "Sekiro Shadows Die Twice",
                candidates={7: "Sekiro: Shadows Die Twice"},
            )

        self.assertEqual(row, {"id": 7})
        self.assertIsNone(
            db_fuzzy.find_conflicting_fuzzy_key(
                "Sekiro Shadows Die Twice",
                candidates={7: "Sekiro: Shadows Die Twice"},
            )
        )

    def test_titles_conflict_on_identity_cases(self) -> None:
        conflict = db_fuzzy.titles_conflict_on_identity
        # Base title vs numbered sequel — the original Xenoblade bug.
        self.assertTrue(conflict("Xenoblade Chronicles", "Xenoblade Chronicles 2"))
        # Roman numeral and its Arabic form are the same entry.
        self.assertFalse(conflict("Final Fantasy VII", "Final Fantasy 7"))
        # "Switch 2 Edition" (with and without "Nintendo") is not a sequel number.
        self.assertFalse(
            conflict(
                "Xenoblade Chronicles: Definitive Edition - Nintendo Switch 2 Edition",
                "Xenoblade Chronicles",
            )
        )
        self.assertFalse(conflict("Zelda: Echoes of Wisdom - Switch 2 Edition", "Zelda: Echoes of Wisdom"))
        self.assertTrue(
            conflict(
                "Xenoblade Chronicles: Definitive Edition - Nintendo Switch 2 Edition",
                "Xenoblade Chronicles 2",
            )
        )
        # Platform tags carry numbers but are not series identity.
        self.assertFalse(conflict("God of War Ragnarok PS5", "God of War Ragnarok PS4"))
        # Annualized titles whose version is fused into a token stay distinct.
        self.assertTrue(conflict("NBA 2K24", "NBA 2K25"))
        self.assertFalse(conflict("NBA 2K24", "NBA 2K24"))


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

        fetch_score.assert_awaited_once_with(expected_url, ANY)
        upsert.assert_awaited_once()
        self.assertEqual(fields["metacritic_score"], 72)
        self.assertEqual(fields["metacritic_url"], expected_url)

    async def test_fetch_score_accepts_critic_metascore(self) -> None:
        url = "https://www.metacritic.com/game/portal/"
        response = _FakeResponse(_ld_json("90", "100"), url)
        with patch("gamelib_mcp.data.metacritic.httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            score, _ = await metacritic._fetch_score_from_url(url)
        self.assertEqual(score, 90)

    async def test_fetch_score_rejects_user_score_aggregate(self) -> None:
        # bestRating=10 means this aggregateRating is the user score, not the Metascore.
        url = "https://www.metacritic.com/game/garrys-mod/"
        response = _FakeResponse(_ld_json("8.4", "10"), url)
        with patch("gamelib_mcp.data.metacritic.httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            score, _ = await metacritic._fetch_score_from_url(url)
        self.assertIsNone(score)

    async def test_fetch_score_picks_critic_block_when_both_present(self) -> None:
        url = "https://www.metacritic.com/game/hades/"
        critic = {"@type": "Product", "aggregateRating": {
            "@type": "AggregateRating", "ratingValue": "93", "bestRating": "100"}}
        user = {"@type": "Product", "aggregateRating": {
            "@type": "AggregateRating", "ratingValue": "9", "bestRating": "10"}}
        html = (
            f'<html><head>'
            f'<script type="application/ld+json">{json.dumps(user)}</script>'
            f'<script type="application/ld+json">{json.dumps(critic)}</script>'
            f'</head></html>'
        )
        response = _FakeResponse(html, url)
        with patch("gamelib_mcp.data.metacritic.httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            score, _ = await metacritic._fetch_score_from_url(url)
        self.assertEqual(score, 93)

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
