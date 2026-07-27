import asyncio
import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from conftest import virtual_clock
from gamelib_mcp.data import igdb


class _DummyResponse:
    def __init__(self, status_code: int, json_data, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.request = httpx.Request("POST", igdb._IGDB_GAMES_URL)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=self.request,
                response=httpx.Response(
                    self.status_code,
                    headers=self.headers,
                    json=self._json_data,
                    request=self.request,
                ),
            )

    def json(self):
        return self._json_data


class _DummyAsyncClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class IGDBRequestGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_releases_permit_when_acquire_is_cancelled(self) -> None:
        gate = igdb._IGDBRequestGate(
            target_interval=1.0,
            max_requests_per_second=4,
            max_in_flight=1,
        )

        await gate.acquire()
        gate.release()

        async def cancel_sleep(_delay: float) -> None:
            raise asyncio.CancelledError()

        with patch("gamelib_mcp.data.igdb.asyncio.sleep", new=cancel_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await gate.acquire()

        state = gate._get_loop_state()
        self.assertEqual(state.semaphore._value, 1)

    async def test_gate_enforces_target_interval_without_wall_clock_sleep(self) -> None:
        gate = igdb._IGDBRequestGate(
            target_interval=0.5,
            max_requests_per_second=4,
            max_in_flight=1,
        )
        clock = _FakeClock()

        with (
            patch("gamelib_mcp.data.igdb.time.monotonic", side_effect=clock.monotonic),
            patch("gamelib_mcp.data.igdb.asyncio.sleep", new=clock.sleep),
        ):
            await gate.acquire()
            gate.release()
            await gate.acquire()
            gate.release()

        self.assertEqual(clock.sleeps, [0.5])


class IGDBRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_builds_platform_filter_without_placeholder_clause(self) -> None:
        post_mock = AsyncMock(return_value=[])

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=post_mock),
        ):
            await igdb.search_game("Age of Wonders", igdb.IGDB_PLATFORM_PC)

        query = post_mock.await_args.args[0]
        self.assertIn("search \"Age of Wonders\";", query)
        self.assertIn("platforms = 6", query)
        self.assertNotIn("category !=", query)
        self.assertNotIn("where 1 = 1", query)

    async def test_search_game_escapes_quotes_in_search_string(self) -> None:
        post_mock = AsyncMock(return_value=[])

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=post_mock),
        ):
            await igdb.search_game('3 out of 10, EP 5: "The Rig Is Up!"')

        query = post_mock.await_args.args[0]
        self.assertIn('search "3 out of 10, EP 5: \\"The Rig Is Up!\\"";', query)

    async def test_search_game_does_not_filter_out_results_with_missing_category(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            if "category !=" in query and "category = null" not in query:
                return []
            return [{"id": 141533, "name": "Loop Hero", "release_dates": [{"platform": 6, "date": 1615334400}]}]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Loop Hero", igdb.PLATFORM_TO_IGDB["epic"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].igdb_id, 141533)
        self.assertEqual(results[0].name, "Loop Hero")

    async def test_search_game_falls_back_to_game_type_when_category_missing(self) -> None:
        # Same family as test_search_game_does_not_filter_out_results_with_missing_category,
        # but also asserts the parsed IGDBGame reflects the game_type-derived
        # classification (game_type=13 "pack" -> non-primary dlc) instead of
        # defaulting to a mislabeled base_game/primary result.
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [
                {
                    "id": 266009,
                    "name": "Persona 3 Reload: Persona 5 Royal Persona Set 1",
                    "category": None,
                    "game_type": 13,
                }
            ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Persona 3 Reload")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].game_type, 13)
        self.assertEqual(results[0].content_type, "dlc")
        self.assertFalse(results[0].is_primary_library_item)

    async def test_search_game_requests_content_relationship_fields_without_excluding_addons(self) -> None:
        post_mock = AsyncMock(return_value=[])

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=post_mock),
        ):
            await igdb.search_game("Portal 2")

        query = post_mock.await_args.args[0]
        self.assertIn("fields id, name, category, game_type, first_release_date", query)
        self.assertIn("parent_game.id, parent_game.name", query)
        self.assertIn("version_parent.id, version_parent.name", query)
        self.assertNotIn("category !=", query)
        self.assertIn("limit 20;", query)

    async def test_search_game_requests_series_fields(self) -> None:
        post_mock = AsyncMock(return_value=[])

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=post_mock),
        ):
            await igdb.search_game("Bloodborne")

        query = post_mock.await_args.args[0]
        self.assertIn("collections.id, collections.name", query)
        self.assertIn("franchises.id, franchises.name", query)

    async def test_search_game_parses_collections_and_franchises(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [
                {
                    "id": 7346,
                    "name": "Breath of the Wild",
                    "category": igdb.CATEGORY_MAIN_GAME,
                    "collections": [{"id": 1, "name": "The Legend of Zelda"}],
                    "franchises": [{"id": 2, "name": "The Legend of Zelda"}],
                }
            ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Breath of the Wild")

        self.assertEqual(
            results[0].series,
            [
                ("collection", 1, "The Legend of Zelda"),
                ("franchise", 2, "The Legend of Zelda"),
            ],
        )

    async def test_search_game_parses_expansion_parent_relationship(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [
                {
                    "id": 222,
                    "name": "Sid Meier's Civilization IV: Warlords",
                    "category": igdb.CATEGORY_EXPANSION,
                    "parent_game": {"id": 111, "name": "Sid Meier's Civilization IV"},
                }
            ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Sid Meier's Civilization IV: Warlords")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content_type, "expansion")
        self.assertEqual(results[0].parent_igdb_id, 111)
        self.assertEqual(results[0].parent_name, "Sid Meier's Civilization IV")
        self.assertFalse(results[0].is_primary_library_item)

    async def test_search_game_captures_platform_ids(self) -> None:
        item = {
            "id": 1,
            "name": "Hades",
            "category": 0,
            "platforms": [6, 130, 508],
            "release_dates": [{"platform": 167, "date": 1600000000}],
        }

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [item]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Hades", None)

        self.assertEqual(results[0].platforms, [6, 130, 167, 508])

    async def test_search_game_retries_rate_limit_response(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, [], headers={"Retry-After": "0"}),
                _DummyResponse(
                    200,
                    [
                        {
                            "id": 620,
                            "name": "Portal 2",
                            "category": igdb.CATEGORY_MAIN_GAME,
                            "first_release_date": 1302566400,
                        }
                    ],
                ),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=sleep_mock),
        ):
            results = await igdb.search_game("Portal 2")

        self.assertEqual([game.name for game in results], ["Portal 2"])
        self.assertEqual(client.calls, 2)
        sleep_mock.assert_awaited()

    async def test_retry_after_blocks_other_requests_at_shared_gate(self) -> None:
        gate = igdb._IGDBRequestGate(
            target_interval=0.0,
            max_requests_per_second=100,
            max_in_flight=1,
        )
        post_started = asyncio.Event()
        allow_retry = asyncio.Event()
        post_calls = 0

        class _SharedClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *_args, **_kwargs):
                nonlocal post_calls
                post_calls += 1
                if post_calls == 1:
                    return _DummyResponse(429, [], headers={"Retry-After": "1"})
                post_started.set()
                return _DummyResponse(200, [{"id": post_calls}])

        async def block_retry(delay_seconds: float) -> None:
            self.assertEqual(delay_seconds, 1.0)
            await allow_retry.wait()

        with (
            patch("gamelib_mcp.data.igdb._IGDB_REQUEST_GATE", gate),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=_SharedClient()),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=block_retry),
        ):
            first = asyncio.create_task(igdb._post_igdb_games("fields id;", headers={}))
            await asyncio.sleep(0)
            second = asyncio.create_task(igdb._post_igdb_games("fields id;", headers={}))
            await asyncio.sleep(0)

            self.assertFalse(post_started.is_set())
            self.assertEqual(post_calls, 1)

            allow_retry.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(sorted(result[0]["id"] for result in (first_result, second_result)), [2, 3])

    async def test_search_game_uses_retry_after_before_backoff_jitter(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, [], headers={"Retry-After": "7"}),
                _DummyResponse(200, []),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            virtual_clock("gamelib_mcp.data.igdb") as clock,
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=sleep_mock),
            patch("gamelib_mcp.data.igdb.random.uniform", side_effect=AssertionError("unexpected jitter")),
        ):
            results = await igdb.search_game("Portal 2")

        self.assertEqual(results, [])
        sleep_mock.assert_awaited_once_with(7.0)
        # Retry-After is also pushed onto the shared gate, so the retry waits
        # the full 7s there rather than walking into the same rate limit.
        self.assertEqual(clock.sleeps, [7.0])

    async def test_search_game_does_not_retry_bad_request(self) -> None:
        client = _DummyAsyncClient([_DummyResponse(400, {"message": "bad request"})])
        sleep_mock = AsyncMock()

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=sleep_mock),
        ):
            results = await igdb.search_game("Portal 2")

        self.assertEqual(results, [])
        self.assertEqual(client.calls, 1)
        sleep_mock.assert_not_awaited()

    async def test_search_game_returns_empty_after_retry_exhaustion(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, [], headers={"Retry-After": "0"}),
                _DummyResponse(503, {"message": "unavailable"}),
                httpx.ReadTimeout("timeout"),
                httpx.ConnectError("boom"),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            virtual_clock("gamelib_mcp.data.igdb"),
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=sleep_mock),
        ):
            results = await igdb.search_game("Portal 2")

        self.assertEqual(results, [])
        self.assertEqual(client.calls, 4)
        self.assertEqual(sleep_mock.await_count, 3)

    async def test_search_game_logs_retry_exhaustion(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, [], headers={"Retry-After": "0"}),
                _DummyResponse(503, {"message": "unavailable"}),
                httpx.ReadTimeout("timeout"),
                httpx.ConnectError("boom"),
            ]
        )

        with (
            virtual_clock("gamelib_mcp.data.igdb"),
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=AsyncMock()),
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING") as logs,
        ):
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.search_game("Portal 2", suppress_errors=False)

        self.assertTrue(any("IGDB search exhausted retries" in line for line in logs.output))


class IGDBLinkingConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_and_link_game_reuses_existing_row_under_concurrent_calls(self) -> None:
        igdb_game = igdb.IGDBGame(
            igdb_id=99,
            name="Portal",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2007-10-10",
        )
        state = {
            "linked_game_id": None,
            "next_game_id": 100,
            "inserted_ids": [],
        }

        async def get_game_by_igdb_id(_igdb_id: int):
            await asyncio.sleep(0.01)
            if state["linked_game_id"] is None:
                return None
            return {"id": state["linked_game_id"]}

        async def find_game_by_name_fuzzy(*_args, **_kwargs):
            return None

        async def apply_metadata(game_id: int, _igdb_game: igdb.IGDBGame) -> None:
            if state["linked_game_id"] is None:
                state["linked_game_id"] = game_id
                return
            if state["linked_game_id"] != game_id:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: games.igdb_id")

        class _InsertResult:
            def __init__(self, lastrowid: int) -> None:
                self.lastrowid = lastrowid

        class _FakeDb:
            async def execute(self, sql: str, _params):
                self_sql = " ".join(sql.split())
                if self_sql != "INSERT INTO games (name, name_normalized) VALUES (?, ?)":
                    raise AssertionError(f"unexpected SQL: {sql}")
                state["next_game_id"] += 1
                game_id = state["next_game_id"]
                state["inserted_ids"].append(game_id)
                await asyncio.sleep(0)
                return _InsertResult(game_id)

            async def commit(self) -> None:
                return None

        class _FakeDbContext:
            async def __aenter__(self):
                return _FakeDb()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=igdb_game)),
            patch("gamelib_mcp.data.db.get_game_by_igdb_id", AsyncMock(side_effect=get_game_by_igdb_id)),
            patch("gamelib_mcp.data.db.find_game_by_name_fuzzy", AsyncMock(side_effect=find_game_by_name_fuzzy)),
            patch("gamelib_mcp.data.db.get_db", return_value=_FakeDbContext()),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock(side_effect=apply_metadata)),
        ):
            results = await asyncio.gather(
                igdb.resolve_and_link_game("Portal", igdb.IGDB_PLATFORM_PC, {}),
                igdb.resolve_and_link_game("Portal", igdb.IGDB_PLATFORM_PC, {}),
            )

        self.assertEqual(results, [(101, igdb_game), (101, igdb_game)])
        self.assertEqual(state["inserted_ids"], [101])

    async def test_resolve_and_link_game_serializes_no_igdb_fallback_inserts(self) -> None:
        state = {
            "game_id": None,
            "insert_calls": 0,
        }

        async def find_game_by_name_fuzzy(*_args, **_kwargs):
            await asyncio.sleep(0.01)
            if state["game_id"] is None:
                return None
            return {"id": state["game_id"]}

        async def upsert_game(*, appid: int | None, name: str, match_existing_by_name: bool = True):
            self.assertIsNone(appid)
            self.assertEqual(name, "Portal")
            # The non-IGDB create-new terminal must opt out of the name fallback so a
            # deliberately-rejected fuzzy match isn't silently re-collapsed.
            self.assertFalse(match_existing_by_name)
            state["insert_calls"] += 1
            await asyncio.sleep(0.01)

            if state["game_id"] is None:
                state["game_id"] = 200
                return 200

            return 201

        with (
            patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.db.find_game_by_name_fuzzy", AsyncMock(side_effect=find_game_by_name_fuzzy)),
            patch("gamelib_mcp.data.db.upsert_game", AsyncMock(side_effect=upsert_game)),
        ):
            results = await asyncio.gather(
                igdb.resolve_and_link_game("Portal", igdb.IGDB_PLATFORM_PC, {}),
                igdb.resolve_and_link_game("Portal", igdb.IGDB_PLATFORM_PC, {}),
            )

        self.assertEqual(results, [(200, None), (200, None)])
        self.assertEqual(state["insert_calls"], 1)


class IGDBBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_missing_games_uses_existing_request_gate(self) -> None:
        igdb_game = igdb.IGDBGame(
            igdb_id=620,
            name="Portal 2",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2011-04-19",
        )
        game_row = {"id": 7, "name": "Portal 2", "igdb_id": None}

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[game_row])),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=igdb.IGDB_PLATFORM_PC)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(return_value=igdb._ResolveOutcome(game=igdb_game, saw_candidates=True)),
            ),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        apply_metadata.assert_awaited_once_with(7, igdb_game)

    async def test_backfill_missing_games_skips_duplicate_igdb_id_and_continues(self) -> None:
        first = {"id": 7, "name": "Portal", "igdb_id": None}
        second = {"id": 8, "name": "Portal 2", "igdb_id": None}
        duplicate_game = igdb.IGDBGame(
            igdb_id=620,
            name="Portal",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2007-10-10",
        )
        unique_game = igdb.IGDBGame(
            igdb_id=621,
            name="Portal 2",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2011-04-19",
        )

        async def apply_metadata(game_id: int, igdb_game: igdb.IGDBGame) -> None:
            if game_id == 7:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: games.igdb_id")
            self.assertEqual((game_id, igdb_game.igdb_id), (8, 621))

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7, 8])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[first, second])),
            patch(
                "gamelib_mcp.data.igdb.choose_igdb_platform_hint",
                AsyncMock(side_effect=[igdb.IGDB_PLATFORM_PC, igdb.IGDB_PLATFORM_PC]),
            ),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(
                    side_effect=[
                        igdb._ResolveOutcome(game=duplicate_game, saw_candidates=True),
                        igdb._ResolveOutcome(game=unique_game, saw_candidates=True),
                    ]
                ),
            ),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock(side_effect=apply_metadata)),
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
        ):
            count = await igdb.backfill_missing_games(limit=2)

        self.assertEqual(count, 2)
        mark_checked.assert_awaited_once_with(7)
        self.assertEqual(release_claim.await_count, 2)

    async def test_backfill_missing_games_does_not_mark_checked_on_operational_failure(self) -> None:
        game_row = {"id": 7, "name": "Portal 2", "igdb_id": None}

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[game_row])),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=igdb.IGDB_PLATFORM_PC)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(side_effect=igdb.IGDBRequestFailure("retry exhausted")),
            ),
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 0)
        mark_checked.assert_not_awaited()
        release_claim.assert_awaited_once_with(7, "igdb_claimed_at")
        self.assertTrue(any("IGDB backfill leaving game retryable" in line for line in logs.output))

    async def test_backfill_missing_games_fetches_by_id_when_igdb_id_already_set(self) -> None:
        # NieR:Automata case: the row already has a matched igdb_id from an
        # earlier pass, so the backfill must fetch it directly instead of
        # re-resolving by name (which can drift onto a different candidate).
        by_id_game = igdb.IGDBGame(
            igdb_id=391942,
            name="NieR:Automata",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2017-02-23",
            platforms=[6, 48],
        )
        game_row = {"id": 9, "name": "NieR:Automata", "igdb_id": 391942}

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[9])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[game_row])),
            patch("gamelib_mcp.data.igdb.fetch_game_by_id", AsyncMock(return_value=by_id_game)) as fetch_by_id,
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock()) as platform_hint,
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()) as resolve_status,
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        fetch_by_id.assert_awaited_once_with(391942, suppress_errors=False)
        apply_metadata.assert_awaited_once_with(9, by_id_game)
        platform_hint.assert_not_awaited()
        resolve_status.assert_not_awaited()

    async def test_backfill_missing_games_falls_back_to_name_search_on_empty_platforms(self) -> None:
        # A by-id fetch that returns a game with no platform data isn't useful
        # (the whole point is populating igdb_platforms) — fall through to the
        # existing name-based resolution path rather than applying it as-is.
        empty_platforms_game = igdb.IGDBGame(
            igdb_id=391942,
            name="NieR:Automata",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2017-02-23",
            platforms=[],
        )
        resolved_game = igdb.IGDBGame(
            igdb_id=391942,
            name="NieR:Automata",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2017-02-23",
            platforms=[6, 48],
        )
        game_row = {"id": 9, "name": "NieR:Automata", "igdb_id": 391942}

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[9])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[game_row])),
            patch(
                "gamelib_mcp.data.igdb.fetch_game_by_id", AsyncMock(return_value=empty_platforms_game)
            ) as fetch_by_id,
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=igdb.IGDB_PLATFORM_PC)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(return_value=igdb._ResolveOutcome(game=resolved_game, saw_candidates=True)),
            ) as resolve_status,
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        fetch_by_id.assert_awaited_once_with(391942, suppress_errors=False)
        resolve_status.assert_awaited_once()
        apply_metadata.assert_awaited_once_with(9, resolved_game)


class ResolveGameIdentityTests(unittest.IsolatedAsyncioTestCase):
    """resolve_game must never collapse a title onto a different series entry."""

    def _game(self, igdb_id: int, name: str) -> "igdb.IGDBGame":
        return igdb.IGDBGame(
            igdb_id=igdb_id,
            name=name,
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2020-05-29",
        )

    async def _resolve(self, query: str, candidate_names: list[str]):
        candidates = [self._game(i + 1, n) for i, n in enumerate(candidate_names)]
        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch(
                "gamelib_mcp.data.igdb.search_game",
                AsyncMock(return_value=candidates),
            ),
        ):
            return await igdb.resolve_game(query, igdb.PLATFORM_TO_IGDB["switch2"])

    async def test_rejects_when_every_candidate_is_a_different_sequel(self) -> None:
        # The Definitive Edition isn't in IGDB's results; do not fall back to XC2.
        result = await self._resolve(
            "Xenoblade Chronicles",
            ["Xenoblade Chronicles 2", "Xenoblade Chronicles 3"],
        )
        self.assertIsNone(result)

    async def test_picks_identity_compatible_candidate_over_conflicting_top_hit(self) -> None:
        # IGDB ranks XC2 first by relevance; the matcher must skip it.
        result = await self._resolve(
            "Xenoblade Chronicles",
            ["Xenoblade Chronicles 2", "Xenoblade Chronicles: Definitive Edition"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Xenoblade Chronicles: Definitive Edition")

    async def test_switch2_edition_query_does_not_match_numbered_sequel(self) -> None:
        # The "2" in "Switch 2 Edition" must not pull in "Xenoblade Chronicles 2".
        result = await self._resolve(
            "Xenoblade Chronicles: Definitive Edition - Nintendo Switch 2 Edition",
            ["Xenoblade Chronicles 2"],
        )
        self.assertIsNone(result)

    async def test_same_sequel_number_still_matches(self) -> None:
        result = await self._resolve("Hitman 2", ["Hitman 2", "Hitman"])
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Hitman 2")

    async def test_name_gate_rejects_unrelated_fuzzy_or_fallback_candidate(self) -> None:
        # Prod disaster: "Borderlands GOTY" got enriched as igdb 258897 "The
        # Tower on the Borderland" — a completely unrelated game accepted via
        # the inconclusive-fuzzy relevance fallback. The strict name gate must
        # reject any candidate whose edition-stripped normalized title is not
        # EQUAL to the query's, leaving the row unmatched.
        result = await self._resolve("Borderlands GOTY", ["The Tower on the Borderland"])
        self.assertIsNone(result)

    async def test_name_gate_rejects_near_name_variant_candidate(self) -> None:
        # Prod disaster: "PAYDAY 2" got enriched as "Payday 2 VR" (high fuzzy
        # similarity, no identity conflict). "payday 2" != "payday 2 vr" after
        # edition stripping -> no match stored.
        result = await self._resolve("PAYDAY 2", ["Payday 2 VR"])
        self.assertIsNone(result)

    async def test_exact_name_candidate_wins_over_near_variant(self) -> None:
        # When the true title is present alongside the near-variant, the exact
        # match must win regardless of candidate order.
        result = await self._resolve("PAYDAY 2", ["Payday 2 VR", "PAYDAY 2"])
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "PAYDAY 2")

    async def test_name_gate_allows_edition_stripped_equal_titles(self) -> None:
        # Edition variants still match: both sides strip to "the witcher".
        result = await self._resolve("The Witcher: Enhanced Edition", ["The Witcher"])
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "The Witcher")

    async def test_picks_exact_title_base_game_over_dlc_shaped_higher_relevance_hits(self) -> None:
        # Persona 3 Reload case from the handover doc: IGDB's own relevance
        # ranking put 5 DLC/cosmetic "Persona Set"/"BGM Set" packs (game_type=13)
        # ahead of the actual base game (game_type=8, "remake") in the result
        # list. The base game's title is an exact match for the query; every
        # pack's title has a longer suffix — the exact-match short-circuit must
        # pick the base game regardless of list position.
        packs = [
            igdb.IGDBGame(
                igdb_id=266009 + i,
                name=f"Persona 3 Reload: Persona 5 Royal Persona Set {i + 1}",
                category=None,
                game_type=13,
                content_type="dlc",
                is_primary_library_item=False,
                first_release_date="2024-02-02",
            )
            for i in range(5)
        ]
        base_game = igdb.IGDBGame(
            igdb_id=252647,
            name="Persona 3 Reload",
            category=None,
            game_type=8,
            content_type="remake",
            is_primary_library_item=True,
            first_release_date="2024-02-02",
        )
        # Base game listed last, as IGDB's relevance ranking put it at position
        # 11 of 20 behind DLC packs — list order must not matter.
        candidates = [*packs, base_game]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=candidates)),
        ):
            result = await igdb.resolve_game("Persona 3 Reload", igdb.IGDB_PLATFORM_SWITCH2)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 252647)
        self.assertEqual(result.name, "Persona 3 Reload")
        self.assertTrue(result.is_primary_library_item)


class ResolveGameZeroResultLadderTests(unittest.IsolatedAsyncioTestCase):
    """resolve_game's zero-result fallback ladder (Fix 4)."""

    async def test_ladder_variant_finds_accented_title_search_missed(self) -> None:
        # The Seance of Blake Manor case: IGDB's search returns zero results
        # for the stored (accent-less) title, and even a "Seance of Blake
        # Manor"/"Seance Blake Manor" variant returns nothing — only the
        # last-resort two-token query "Blake Manor" finds the real (accented)
        # match. It must still be accepted, since it fuzzy/exact-matches the
        # ORIGINAL query under ascii-folded normalization.
        seance_game = igdb.IGDBGame(
            igdb_id=335833,
            name="The Séance of Blake Manor",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2024-10-29",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            if name == "Blake Manor":
                return [seance_game]
            return []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("The Seance of Blake Manor", None)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 335833)

    async def test_ladder_does_not_accept_unrelated_results_from_a_narrow_variant(self) -> None:
        # A narrower fallback-ladder query (e.g. "Seance" alone) can return
        # totally unrelated games ("Silly Seance", etc.) that don't conflict on
        # sequel identity but also don't fuzzy-match the original query. These
        # must not be accepted just because the variant returned something.
        unrelated = [
            igdb.IGDBGame(
                igdb_id=1,
                name="Silly Seance",
                category=igdb.CATEGORY_MAIN_GAME,
                first_release_date="2019-01-01",
            ),
            igdb.IGDBGame(
                igdb_id=2,
                name="Ghost Hunters Simulator",
                category=igdb.CATEGORY_MAIN_GAME,
                first_release_date="2020-01-01",
            ),
        ]

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            # Original query returns nothing; every ladder variant returns the
            # same unrelated candidates.
            if name == "The Seance of Blake Manor":
                return []
            return unrelated

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("The Seance of Blake Manor", None)

        self.assertIsNone(result)

    async def test_article_stripped_variant_must_gate_against_the_original(self) -> None:
        # "The Forest"/"The Surge"/"The Hex"/"The Gunk" all have unrelated
        # IGDB entries under the article-less name. The leading-article rung
        # stays in the ladder as a query WIDENER, but its hits are gated
        # against the original title, so the bare entry is refused.
        wrong_entity = igdb.IGDBGame(
            igdb_id=346813,
            name="Forest",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2015-01-01",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            return [wrong_entity] if name == "Forest" else []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("The Forest", None)

        self.assertIsNone(result)

    async def test_exact_name_lookup_rescues_a_title_search_cannot_find(self) -> None:
        # Prod: IGDB's search returns zero for "The Forest" while IGDB holds
        # that exact name as 7504. The equality lookup finds it; the ladder's
        # article rung would have offered the unrelated "Forest" (346813).
        real = igdb.IGDBGame(
            igdb_id=7504,
            name="The Forest",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2018-04-30",
        )
        stranger = igdb.IGDBGame(
            igdb_id=346813,
            name="Forest",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2025-01-01",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            return [stranger] if name == "Forest" else []

        async def fake_exact(name, igdb_platform_id=None, *, suppress_errors=True):
            return [real] if name == "The Forest" else []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
            patch(
                "gamelib_mcp.data.igdb.fetch_games_by_exact_name",
                AsyncMock(side_effect=fake_exact),
            ),
        ):
            result = await igdb.resolve_game("The Forest", None)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 7504)

    async def test_ambiguous_exact_name_is_refused_not_ranked(self) -> None:
        # Two real games share the exact name "The Bridge" (2013 and 2024);
        # picking one is the same class of guess as accepting a stranger.
        candidates = [
            igdb.IGDBGame(
                igdb_id=8440,
                name="The Bridge",
                category=igdb.CATEGORY_MAIN_GAME,
                first_release_date="2013-02-22",
            ),
            igdb.IGDBGame(
                igdb_id=352753,
                name="The Bridge",
                category=igdb.CATEGORY_MAIN_GAME,
                first_release_date="2024-01-01",
            ),
        ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=[])),
            patch(
                "gamelib_mcp.data.igdb.fetch_games_by_exact_name",
                AsyncMock(return_value=candidates),
            ),
        ):
            result = await igdb.resolve_game("The Bridge", None)

        self.assertIsNone(result)

    async def test_exact_name_duplicate_rows_of_one_game_still_resolve(self) -> None:
        # Same game returned twice (platform-filtered and not) is not ambiguity.
        game = igdb.IGDBGame(
            igdb_id=136000,
            name="The Gunk",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2021-12-16",
        )
        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=[])),
            patch(
                "gamelib_mcp.data.igdb.fetch_games_by_exact_name",
                AsyncMock(return_value=[game, game]),
            ),
        ):
            result = await igdb.resolve_game("The Gunk", igdb.IGDB_PLATFORM_SWITCH2)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 136000)

    async def test_exact_name_query_is_an_equality_filter(self) -> None:
        query = igdb._build_exact_name_query("The Forest", 6)
        self.assertIn('where name = "The Forest"', query)
        self.assertIn("platforms = 6", query)
        self.assertNotIn("search ", query)

    async def test_article_strip_gate_is_case_insensitive(self) -> None:
        # "The Masterplan" vs IGDB "MasterPlan": the casing differs too, but
        # the gate normalizes case on both sides, so what is left is the
        # article — still an identity difference, still refused.
        wrong_entity = igdb.IGDBGame(
            igdb_id=150888,
            name="MasterPlan",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2012-01-01",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            return [wrong_entity] if name.casefold() == "masterplan" else []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("The Masterplan", None)

        self.assertIsNone(result)

    async def test_article_stripped_variant_still_finds_the_real_game(self) -> None:
        # The rung keeps its purpose: searching "Forest" can surface the row's
        # actual game, which passes the gate against "The Forest".
        real = igdb.IGDBGame(
            igdb_id=7830,
            name="The Forest",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2018-04-30",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            return [real] if name == "Forest" else []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("The Forest", None)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 7830)

    async def test_ladder_numbered_edition_matches_base_game(self) -> None:
        # "Sea of Thieves: 2026 Edition" case: the edition-strip variant "Sea
        # of Thieves" finds the base game, but gating it against the ORIGINAL
        # title would reject it — "2026" reads as a sequel number in
        # titles_conflict_on_identity. Identity-preserving variants must gate
        # against the variant itself.
        base_game = igdb.IGDBGame(
            igdb_id=27159,
            name="Sea of Thieves",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2018-03-20",
            platforms=[6, 169],
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            if name == "Sea of Thieves":
                return [base_game]
            return []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("Sea of Thieves: 2026 Edition", None)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 27159)

    async def test_gate_rejected_nonempty_results_fall_through_to_ladder(self) -> None:
        # P2 regression: the initial search for "Sea of Thieves: 2026
        # Edition" RETURNS the base game (non-empty results), but the best
        # candidate is rejected against the original title (the "2026" token
        # survives normalization). resolve_game must not stop there — it
        # falls through to the ladder, whose identity-preserving
        # edition-strip rung re-queries "Sea of Thieves" and gates against
        # the rung's own (stripped) query string, which passes.
        base_game = igdb.IGDBGame(
            igdb_id=27159,
            name="Sea of Thieves",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2018-03-20",
            platforms=[6, 169],
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            if name in ("Sea of Thieves: 2026 Edition", "Sea of Thieves"):
                return [base_game]
            return []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("Sea of Thieves: 2026 Edition", None)

        self.assertIsNotNone(result)
        self.assertEqual(result.igdb_id, 27159)

    async def test_gate_failing_ladder_rungs_still_store_nothing(self) -> None:
        # Fall-through must not weaken the terminal: when the original query
        # and every ladder rung return only a differently-named candidate,
        # each rung's gate rejects it and resolve_game returns None.
        garbage = igdb.IGDBGame(
            igdb_id=42,
            name="Tower of Nonsense",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2019-01-01",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            return [garbage]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("Sea of Thieves: 2026 Edition", None)

        self.assertIsNone(result)

    async def test_ladder_token_variant_still_gates_against_original_identity(self) -> None:
        # Token-dropping variants change what the query means, so their
        # results must still be validated against the ORIGINAL title: a
        # two-token query returning a numbered sequel ("Blake Manor 2") must
        # not be accepted for a query with no sequel number.
        sequel = igdb.IGDBGame(
            igdb_id=99,
            name="Blake Manor 2",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2026-01-01",
        )

        async def fake_search_game(name, igdb_platform_id=None, *, suppress_errors=True):
            if name == "Blake Manor":
                return [sequel]
            return []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "x"}),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search_game)),
        ):
            result = await igdb.resolve_game("The Seance of Blake Manor", None)

        self.assertIsNone(result)


class PlatformFilterTests(unittest.TestCase):
    def test_single_platform_id_filter_unchanged(self):
        query = igdb._build_search_game_query("Hades", 6)
        self.assertIn("where platforms = 6;", query)

    def test_tuple_platform_ids_render_contains_any(self):
        query = igdb._build_search_game_query(
            "Mario Kart World", igdb.PLATFORM_TO_IGDB_ANY["switch2"]
        )
        self.assertIn("where platforms = (508,130);", query)

    def test_platforms_field_requested(self):
        query = igdb._build_search_game_query("Hades")
        self.assertIn(" platforms,", query)

    def test_platform_maps_cover_switch_generations(self):
        self.assertEqual(igdb.IGDB_PLATFORM_SWITCH2, 508)
        self.assertEqual(igdb.PLATFORM_TO_IGDB_ANY["switch2"], (508, 130))
        self.assertEqual(igdb.IGDB_TO_PLATFORM[130], "switch2")
        self.assertEqual(igdb.IGDB_TO_PLATFORM[508], "switch2")
        self.assertEqual(igdb.IGDB_TO_PLATFORM[6], "steam")
        self.assertEqual(igdb.IGDB_TO_PLATFORM[167], "ps5")


class FetchSeriesMembersTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_collection_query_and_parses(self) -> None:
        captured = {}

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            captured["query"] = query
            return [
                {
                    "id": 1,
                    "name": "Pikmin",
                    "first_release_date": 1009843200,
                    "game_type": 0,
                    "platforms": [{"id": 130}],
                },
                {"id": 2, "name": "Pikmin 4 Bundle", "game_type": 3},
            ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            members = await igdb.fetch_series_members("collection", 555)

        self.assertIn("where collections = (555)", captured["query"])
        self.assertEqual([m.igdb_id for m in members], [1])  # bundle filtered out
        self.assertEqual(members[0].first_release_date, "2002-01-01")
        self.assertEqual(members[0].platforms, [130])

    async def test_builds_franchise_query(self) -> None:
        captured = {}

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            captured["query"] = query
            return []

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            await igdb.fetch_series_members("franchise", 42)

        self.assertIn("where franchises = (42)", captured["query"])

    async def test_defaults_missing_game_type_to_main_game(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [{"id": 3, "name": "Undated Entry"}]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            members = await igdb.fetch_series_members("collection", 1)

        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].game_type, 0)
        self.assertIsNone(members[0].first_release_date)

    async def test_paginates_when_a_full_page_is_returned(self) -> None:
        call_count = 0

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    {"id": i, "name": f"Game {i}", "game_type": 0}
                    for i in range(igdb._SERIES_MEMBERS_PAGE_SIZE)
                ]
            return [{"id": 9999, "name": "Last Page Game", "game_type": 0}]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            members = await igdb.fetch_series_members("collection", 1)

        self.assertEqual(call_count, 2)
        self.assertEqual(len(members), igdb._SERIES_MEMBERS_PAGE_SIZE + 1)
        self.assertEqual(members[-1].igdb_id, 9999)

    async def test_rejects_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            await igdb.fetch_series_members("saga", 1)

    async def test_raises_igdb_request_failure_without_credentials(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.fetch_series_members("collection", 1)

    async def test_wraps_post_failure_as_igdb_request_failure(self) -> None:
        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch(
                "gamelib_mcp.data.igdb._post_igdb_games",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.fetch_series_members("collection", 1)


class FetchVersionParentAliasesTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_version_parent_query_and_maps_editions(self) -> None:
        captured = {}

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            captured["query"] = query
            return [
                {"id": 283715, "version_parent": 80, "name": "The Witcher: Enhanced Edition"},
                {"id": 20740, "version_parent": 478, "name": "The Witcher 2 Enhanced Edition"},
            ]

        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            aliases = await igdb.fetch_version_parent_aliases([80, 478])

        self.assertIn(
            "where version_parent = (80, 478) | parent_game = (80, 478)", captured["query"]
        )
        self.assertIn("parent_game, category, game_type", captured["query"])
        self.assertEqual(aliases, {283715: 80, 20740: 478})

    async def test_parent_game_children_alias_unless_dlc_like(self) -> None:
        # parent_game children are re-releases/remasters/standalone GOTY
        # entries (alias) or DLC-like content (must NOT alias — owning a DLC
        # or a cosmetic pack is not owning the base game). Eligibility rides
        # on content_type_from_igdb_category, so pack-style add-ons (13) are
        # excluded alongside DLC (1) and expansion (2). category=None falls
        # back to game_type, mirroring _parse_igdb_item. version_parent
        # children always alias, and version_parent wins when a child
        # carries both links.
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [
                # 2021 "Tales from the Borderlands" re-release: parent_game
                # child, remaster-ish category -> alias.
                {"id": 214139, "parent_game": 6707, "category": 9},
                # DLC child -> no alias.
                {"id": 111, "parent_game": 6707, "category": 1},
                # Expansion via game_type fallback (category absent) -> no alias.
                {"id": 222, "parent_game": 6707, "game_type": 2},
                # Pack-style add-on (13: cosmetic/BGM/persona-set packs) ->
                # classifier deems it DLC content -> no alias.
                {"id": 666, "parent_game": 6707, "category": 13},
                # Pack via game_type fallback -> no alias.
                {"id": 777, "parent_game": 6707, "game_type": 13},
                # No category/game_type at all -> not provably DLC -> alias.
                {"id": 333, "parent_game": 6707},
                # version_parent child that is ALSO a DLC by category: the
                # version link wins and it still aliases.
                {"id": 444, "version_parent": 6707, "category": 1},
                # Both links present: version_parent wins.
                {"id": 555, "version_parent": 6707, "parent_game": 9999, "category": 9},
            ]

        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            aliases = await igdb.fetch_version_parent_aliases([6707])

        self.assertEqual(
            aliases, {214139: 6707, 333: 6707, 444: 6707, 555: 6707}
        )

    async def test_paginates_when_a_full_page_of_editions_is_returned(self) -> None:
        # IGDB caps a page at 500; >500 edition children across a member-id
        # chunk must paginate (offset loop) instead of silently dropping the
        # overflow, mirroring fetch_series_members.
        queries: list[str] = []

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            queries.append(query)
            if len(queries) == 1:
                return [
                    {"id": 10_000 + i, "version_parent": 80, "name": f"Edition {i}"}
                    for i in range(igdb._SERIES_MEMBERS_PAGE_SIZE)
                ]
            return [{"id": 99_999, "version_parent": 80, "name": "Last Page Edition"}]

        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            aliases = await igdb.fetch_version_parent_aliases([80])

        self.assertEqual(len(queries), 2)
        self.assertIn("offset 0;", queries[0])
        self.assertIn(f"offset {igdb._SERIES_MEMBERS_PAGE_SIZE};", queries[1])
        self.assertEqual(len(aliases), igdb._SERIES_MEMBERS_PAGE_SIZE + 1)
        self.assertEqual(aliases[99_999], 80)

    async def test_empty_input_short_circuits_without_request(self) -> None:
        with patch.dict(
            "os.environ",
            {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
            clear=True,
        ):
            aliases = await igdb.fetch_version_parent_aliases([])

        self.assertEqual(aliases, {})

    async def test_unconfigured_returns_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            aliases = await igdb.fetch_version_parent_aliases([80])

        self.assertEqual(aliases, {})

    async def test_wraps_post_failure_as_igdb_request_failure(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch(
                "gamelib_mcp.data.igdb._post_igdb_games",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.fetch_version_parent_aliases([80])


class IGDBCredentialHygieneTests(unittest.IsolatedAsyncioTestCase):
    """Missing credentials are operational failures, never 'no match'.

    Prod 2026-07-05: a deploy briefly ran without TWITCH_CLIENT_ID and the
    backfill cached 800+ owned games as permanent no-matches because the
    creds-missing early returns were indistinguishable from 'not found'.
    """

    def _no_creds_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TWITCH_")}
        return patch.dict("os.environ", env, clear=True)

    async def test_search_game_raises_without_credentials_when_unsuppressed(self) -> None:
        with self._no_creds_env():
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.search_game("Portal 2", suppress_errors=False)

    async def test_search_game_returns_empty_without_credentials_when_suppressed(self) -> None:
        with self._no_creds_env():
            self.assertEqual(await igdb.search_game("Portal 2"), [])

    async def test_fetch_game_by_id_raises_without_credentials_when_unsuppressed(self) -> None:
        with self._no_creds_env():
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.fetch_game_by_id(620, suppress_errors=False)

    async def test_fetch_game_by_id_returns_none_without_credentials_when_suppressed(self) -> None:
        with self._no_creds_env():
            self.assertIsNone(await igdb.fetch_game_by_id(620))

    async def test_resolve_game_raises_without_credentials_when_unsuppressed(self) -> None:
        with self._no_creds_env():
            with self.assertRaises(igdb.IGDBRequestFailure):
                await igdb.resolve_game("Portal 2", None, suppress_errors=False)

    async def test_resolve_game_returns_none_without_credentials_when_suppressed(self) -> None:
        with self._no_creds_env():
            self.assertIsNone(await igdb.resolve_game("Portal 2", None))

    async def test_backfill_leaves_rows_retryable_without_credentials(self) -> None:
        game_row = {"id": 7, "name": "Rocket League", "igdb_id": None}

        with (
            self._no_creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[game_row])),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 0)
        mark_checked.assert_not_awaited()
        release_claim.assert_awaited_once_with(7, "igdb_claimed_at")
        self.assertTrue(any("leaving game retryable" in line for line in logs.output))


class IGDBBackfillCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    """Consecutive name-search no-matches must read as an outage, not misses."""

    def setUp(self) -> None:
        igdb._consecutive_backfill_misses = 0

    def tearDown(self) -> None:
        igdb._consecutive_backfill_misses = 0

    def _rows(self, ids: list[int]) -> list[dict]:
        return [{"id": i, "name": f"Game {i}", "igdb_id": None} for i in ids]

    @staticmethod
    def _miss() -> "igdb._ResolveOutcome":
        """Zero-candidate search — the only outcome that may count as outage."""
        return igdb._ResolveOutcome(game=None, saw_candidates=False)

    @staticmethod
    def _gate_rejection() -> "igdb._ResolveOutcome":
        """Candidates returned but all gate-rejected — proof the API is alive."""
        return igdb._ResolveOutcome(game=None, saw_candidates=True)

    async def test_breaker_aborts_pass_when_canary_confirms_outage(self) -> None:
        ids = list(range(1, 13))  # 12 claimed; breaker trips at the 10th miss

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=ids)),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=self._rows(ids))),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(return_value=self._miss()),
            ) as resolve_status,
            patch("gamelib_mcp.data.igdb._igdb_canary_alive", AsyncMock(return_value=False)) as canary,
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
            self.assertLogs("gamelib_mcp.data.igdb", level="ERROR") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=12)

        self.assertEqual(count, 0)
        mark_checked.assert_not_awaited()
        canary.assert_awaited_once()
        # Ten rows were attempted before the trip; the remaining two are
        # released without ever being attempted.
        self.assertEqual(resolve_status.await_count, 10)
        self.assertEqual(release_claim.await_count, 12)
        self.assertTrue(any("circuit breaker tripped" in line for line in logs.output))

    async def test_breaker_trip_with_alive_canary_commits_pending_and_continues(self) -> None:
        # The livelock killer: 10+ consecutive genuinely-IGDB-absent titles at
        # the head of the deterministic claim order must not deadlock the heal.
        ids = list(range(1, 13))

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=ids)),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=self._rows(ids))),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(return_value=self._miss()),
            ) as resolve_status,
            patch("gamelib_mcp.data.igdb._igdb_canary_alive", AsyncMock(return_value=True)) as canary,
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=12)

        # All 12 rows reach a terminal state: 10 committed when the canary
        # proved the API alive at the would-be trip, 2 more at pass end.
        self.assertEqual(count, 12)
        self.assertEqual(resolve_status.await_count, 12)
        self.assertEqual(mark_checked.await_count, 12)
        self.assertEqual(release_claim.await_count, 12)
        canary.assert_awaited_once()
        self.assertEqual(igdb._consecutive_backfill_misses, 2)
        self.assertTrue(any("canary" in line for line in logs.output))

    async def test_gate_rejection_cluster_commits_rows_without_tripping(self) -> None:
        # The prod livelock shape: the first 10 claimable rows are old titles
        # whose IGDB candidates all fail the name-match gate ('Cogs' ->
        # 'Dr. Cog', 'Counter-Strike' -> 'Counter-Strike Nexon', ...). The
        # search API answered every time — these are genuine refuse-to-guess
        # no-matches and must commit + never count toward the breaker.
        ids = list(range(1, 13))

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=ids)),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=self._rows(ids))),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(return_value=self._gate_rejection()),
            ),
            patch("gamelib_mcp.data.igdb._igdb_canary_alive", AsyncMock()) as canary,
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
        ):
            count = await igdb.backfill_missing_games(limit=12)

        self.assertEqual(count, 12)
        self.assertEqual(mark_checked.await_count, 12)
        self.assertEqual(release_claim.await_count, 12)
        canary.assert_not_awaited()
        self.assertEqual(igdb._consecutive_backfill_misses, 0)

    async def test_gate_rejection_flushes_pending_and_resets_inherited_counter(self) -> None:
        # Recovery path after a confirmed-outage abort: the counter survives
        # the abort (fast re-trip is deliberate), so the next healthy pass
        # must be able to reset it — here via a gate rejection (API alive).
        igdb._consecutive_backfill_misses = 15
        ids = [1, 2]
        outcomes = [self._miss(), self._gate_rejection()]

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=ids)),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=self._rows(ids))),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(side_effect=outcomes),
            ),
            # Row 1's miss re-trips the inherited counter; the canary answers
            # alive, so the pass continues into row 2's gate rejection.
            patch("gamelib_mcp.data.igdb._igdb_canary_alive", AsyncMock(return_value=True)) as canary,
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING"),
        ):
            count = await igdb.backfill_missing_games(limit=2)

        self.assertEqual(count, 2)
        canary.assert_awaited_once()
        self.assertEqual(
            sorted(call.args[0] for call in mark_checked.await_args_list), [1, 2]
        )
        self.assertEqual(igdb._consecutive_backfill_misses, 0)

    async def test_breaker_counter_spans_passes(self) -> None:
        first_ids = list(range(1, 6))
        second_ids = list(range(6, 11))

        with (
            patch(
                "gamelib_mcp.data.igdb.claim_game_ids_for_igdb",
                AsyncMock(side_effect=[first_ids, second_ids]),
            ),
            patch(
                "gamelib_mcp.data.igdb.load_games_for_igdb_backfill",
                AsyncMock(side_effect=[self._rows(first_ids), self._rows(second_ids)]),
            ),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(return_value=self._miss()),
            ),
            patch("gamelib_mcp.data.igdb._igdb_canary_alive", AsyncMock(return_value=False)),
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            first_count = await igdb.backfill_missing_games(limit=5)
            with self.assertLogs("gamelib_mcp.data.igdb", level="ERROR"):
                second_count = await igdb.backfill_missing_games(limit=5)

        # Pass one stays under the threshold, so its misses are committed as
        # genuine no-matches; the counter carries over and pass two trips
        # (canary confirms the outage).
        self.assertEqual(first_count, 5)
        self.assertEqual(mark_checked.await_count, 5)
        self.assertEqual(second_count, 0)

    async def test_search_success_flushes_pending_misses_and_resets_counter(self) -> None:
        ids = [1, 2, 3]
        rows = self._rows(ids)
        hit = igdb.IGDBGame(
            igdb_id=620,
            name="Game 2",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2011-04-19",
        )
        # Inherited near-trip counter: row 1's miss brings it to 9 — one more
        # miss would trip, but row 2's search success resets it first.
        igdb._consecutive_backfill_misses = 8

        with (
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=ids)),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=rows)),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock(return_value=None)),
            patch(
                "gamelib_mcp.data.igdb._resolve_game_with_status",
                AsyncMock(
                    side_effect=[
                        self._miss(),
                        igdb._ResolveOutcome(game=hit, saw_candidates=True),
                        self._miss(),
                    ]
                ),
            ),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()),
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            count = await igdb.backfill_missing_games(limit=3)

        # Row 1's miss was buffered until row 2's search success proved the
        # API works; the success also reset the inherited counter, so row 3's
        # miss did not trip and was committed at pass end.
        self.assertEqual(count, 3)
        self.assertEqual(
            [call.args[0] for call in mark_checked.await_args_list], [1, 3]
        )
        self.assertEqual(igdb._consecutive_backfill_misses, 1)


class IGDBBackfillExternalGamesTests(unittest.IsolatedAsyncioTestCase):
    """external_games (Steam appid -> IGDB id) is authoritative and comes first."""

    def setUp(self) -> None:
        igdb._consecutive_backfill_misses = 0

    def tearDown(self) -> None:
        igdb._consecutive_backfill_misses = 0

    def _creds_env(self):
        return patch.dict(
            "os.environ",
            {"TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "secret"},
            clear=False,
        )

    def _game(self, igdb_id: int, name: str) -> "igdb.IGDBGame":
        return igdb.IGDBGame(
            igdb_id=igdb_id,
            name=name,
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2016-02-16",
            platforms=[6],
        )

    async def test_external_mapping_resolves_before_name_search(self) -> None:
        row = {
            "id": 7,
            "name": "Layers of Fear",
            "igdb_id": None,
            "manual_overrides": None,
            "steam_appid": "391720",
        }
        fetched = self._game(111, "Layers of Fear")

        with (
            self._creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[row])),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"391720": 111}),
            ) as external,
            patch("gamelib_mcp.data.igdb.fetch_game_by_id", AsyncMock(return_value=fetched)) as fetch_by_id,
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()) as resolve_game,
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock()),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        external.assert_awaited_once_with(["391720"])
        fetch_by_id.assert_awaited_once_with(111, suppress_errors=False)
        resolve_game.assert_not_awaited()
        apply_metadata.assert_awaited_once_with(7, fetched)

    async def test_external_disagreement_relinks_stored_igdb_id(self) -> None:
        # Layers of Fear case: steam appid 391720 (the 2016 game) stored with
        # igdb_id 254177 (the 2023 remake). external_games wins.
        row = {
            "id": 7,
            "name": "Layers of Fear",
            "igdb_id": 254177,
            "manual_overrides": None,
            "steam_appid": "391720",
        }
        fetched = self._game(111, "Layers of Fear")

        with (
            self._creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[row])),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"391720": 111}),
            ),
            patch("gamelib_mcp.data.igdb.fetch_game_by_id", AsyncMock(return_value=fetched)) as fetch_by_id,
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()) as resolve_game,
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock()),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
            self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        # The stored (wrong) id is never fetched — only the authoritative one.
        fetch_by_id.assert_awaited_once_with(111, suppress_errors=False)
        resolve_game.assert_not_awaited()
        apply_metadata.assert_awaited_once_with(7, fetched)
        self.assertTrue(any("re-linking" in line for line in logs.output))

    async def test_external_disagreement_keeps_a_name_matching_stored_link(self) -> None:
        # FTL case: steam appid 212680 maps to 178437 ("Faster than light?"),
        # a junk duplicate, while the row's stored 3075 ("FTL: Faster Than
        # Light") is correct. The mapping must not override a link the name
        # vouches for with one it doesn't.
        row = {
            "id": 7,
            "name": "FTL: Faster Than Light",
            "igdb_id": 3075,
            "manual_overrides": None,
            "steam_appid": "212680",
        }
        junk = self._game(178437, "Faster than light?")
        stored = self._game(3075, "FTL: Faster Than Light")

        async def fake_fetch_by_id(igdb_id, *, suppress_errors=True):
            return {178437: junk, 3075: stored}[igdb_id]

        with (
            self._creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[row])),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"212680": 178437}),
            ),
            patch(
                "gamelib_mcp.data.igdb.fetch_game_by_id",
                AsyncMock(side_effect=fake_fetch_by_id),
            ),
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()) as resolve_game,
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock()),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
            self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        resolve_game.assert_not_awaited()
        apply_metadata.assert_awaited_once_with(7, stored)
        self.assertTrue(any("keeping stored igdb_id" in line for line in logs.output))

    async def test_external_disagreement_wins_when_stored_name_matches_no_better(self) -> None:
        # Neither name matches the row: the authoritative mapping still wins,
        # exactly as before — the guard only protects a VOUCHED-FOR link.
        row = {
            "id": 7,
            "name": "Some Renamed Row",
            "igdb_id": 555,
            "manual_overrides": None,
            "steam_appid": "391720",
        }
        external = self._game(111, "Layers of Fear")
        stored = self._game(555, "Something Else Entirely")

        async def fake_fetch_by_id(igdb_id, *, suppress_errors=True):
            return {111: external, 555: stored}[igdb_id]

        with (
            self._creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[row])),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"391720": 111}),
            ),
            patch(
                "gamelib_mcp.data.igdb.fetch_game_by_id",
                AsyncMock(side_effect=fake_fetch_by_id),
            ),
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()),
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock()),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            await igdb.backfill_missing_games(limit=1)

        apply_metadata.assert_awaited_once_with(7, external)

    async def test_manual_igdb_override_pins_stored_link(self) -> None:
        row = {
            "id": 7,
            "name": "Layers of Fear",
            "igdb_id": 254177,
            "manual_overrides": '["igdb_id"]',
            "steam_appid": "391720",
        }
        pinned = self._game(254177, "Layers of Fear")

        with (
            self._creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[row])),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"391720": 111}),
            ),
            patch("gamelib_mcp.data.igdb.fetch_game_by_id", AsyncMock(return_value=pinned)) as fetch_by_id,
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()) as resolve_game,
            patch("gamelib_mcp.data.igdb.choose_igdb_platform_hint", AsyncMock()),
            patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
            patch("gamelib_mcp.data.igdb.upsert_backfill_platform_release_dates", AsyncMock()),
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()),
        ):
            count = await igdb.backfill_missing_games(limit=1)

        self.assertEqual(count, 1)
        fetch_by_id.assert_awaited_once_with(254177, suppress_errors=False)
        resolve_game.assert_not_awaited()
        apply_metadata.assert_awaited_once_with(7, pinned)

    async def test_external_lookup_failure_leaves_whole_pass_retryable(self) -> None:
        rows = [
            {"id": 7, "name": "A", "igdb_id": None, "manual_overrides": None, "steam_appid": "1"},
            {"id": 8, "name": "B", "igdb_id": None, "manual_overrides": None, "steam_appid": "2"},
        ]

        with (
            self._creds_env(),
            patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7, 8])),
            patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=rows)),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(side_effect=RuntimeError("IGDB down")),
            ),
            patch("gamelib_mcp.data.igdb.fetch_game_by_id", AsyncMock()) as fetch_by_id,
            patch("gamelib_mcp.data.igdb._resolve_game_with_status", AsyncMock()) as resolve_game,
            patch("gamelib_mcp.data.igdb.mark_igdb_checked", AsyncMock()) as mark_checked,
            patch("gamelib_mcp.data.igdb.release_game_claim", AsyncMock()) as release_claim,
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING") as logs,
        ):
            count = await igdb.backfill_missing_games(limit=2)

        self.assertEqual(count, 0)
        fetch_by_id.assert_not_awaited()
        resolve_game.assert_not_awaited()
        mark_checked.assert_not_awaited()
        self.assertEqual(release_claim.await_count, 2)
        self.assertTrue(any("external_games lookup failed" in line for line in logs.output))


class IGDBCanaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_alive_when_candidates_returned(self) -> None:
        witcher = igdb.IGDBGame(
            igdb_id=1942,
            name="The Witcher 3: Wild Hunt",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2015-05-19",
        )
        with patch(
            "gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=[witcher])
        ) as search:
            self.assertTrue(await igdb._igdb_canary_alive())
        search.assert_awaited_once_with(igdb._CANARY_TITLE, suppress_errors=False)

    async def test_dead_when_blockbuster_returns_zero_candidates(self) -> None:
        with patch("gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=[])):
            self.assertFalse(await igdb._igdb_canary_alive())

    async def test_dead_when_search_fails_operationally(self) -> None:
        with (
            patch(
                "gamelib_mcp.data.igdb.search_game",
                AsyncMock(side_effect=igdb.IGDBRequestFailure("down")),
            ),
            self.assertLogs("gamelib_mcp.data.igdb", level="WARNING") as logs,
        ):
            self.assertFalse(await igdb._igdb_canary_alive())
        self.assertTrue(any("canary" in line for line in logs.output))


class NameGateCandidateWalkTests(unittest.IsolatedAsyncioTestCase):
    """The strict name gate walks ALL identity-compatible candidates.

    Previously only the single ranked pick was gated: a decorated sibling at
    the top ('Counter-Strike' -> 'Counter-Strike Nexon', 'Cogs' -> 'Dr. Cog')
    rejected the whole game even when a gate-passing candidate sat further
    down the result list.
    """

    def _game(self, igdb_id: int, name: str, *, primary: bool = True) -> "igdb.IGDBGame":
        return igdb.IGDBGame(
            igdb_id=igdb_id,
            name=name,
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date=None,
            is_primary_library_item=primary,
        )

    async def test_walk_accepts_gate_passing_candidate_behind_decorated_top_hit(self) -> None:
        # Fuzzy is inconclusive for both, so the relevance fallback selects
        # the (wrong) first candidate; the gate must walk on to the
        # edition-variant whose stripped title equals the query.
        results = [
            self._game(1, "Dr. Cog"),
            self._game(2, "Cogs Definitive Edition"),
        ]
        match = igdb._select_best_match("Cogs", results, allow_inconclusive_fallback=True)
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 2)

    async def test_walk_applies_on_ladder_rungs_without_relevance_fallback(self) -> None:
        results = [
            self._game(1, "Dr. Cog"),
            self._game(2, "Cogs Definitive Edition"),
        ]
        match = igdb._select_best_match("Cogs", results, allow_inconclusive_fallback=False)
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 2)

    async def test_walk_prefers_primary_library_items(self) -> None:
        # Both edition variants strip to "cogs" and pass the gate; the walk
        # must prefer the primary library item over the non-primary one even
        # though the non-primary sits earlier in relevance order.
        results = [
            self._game(1, "Dr. Cog"),
            self._game(2, "Cogs Definitive Edition", primary=False),
            self._game(3, "Cogs Anniversary Edition"),
        ]
        match = igdb._select_best_match("Cogs", results, allow_inconclusive_fallback=True)
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 3)

    async def test_walk_still_rejects_when_no_candidate_passes(self) -> None:
        results = [
            self._game(1, "Counter-Strike Nexon"),
            self._game(2, "Counter-Strike Condition Zero"),
        ]
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = igdb._select_best_match(
                "Counter-Strike", results, allow_inconclusive_fallback=True
            )
        self.assertIsNone(match)
        self.assertTrue(any("gate rejected" in line for line in logs.output))

    async def test_resolve_reports_gate_rejection_as_candidates_seen(self) -> None:
        # The status the backfill's breaker consumes: gate-rejected != outage.
        rejected = [self._game(1, "Counter-Strike Nexon")]
        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "cid"}, clear=False),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=rejected)),
        ):
            outcome = await igdb._resolve_game_with_status("Counter-Strike", None)
        self.assertIsNone(outcome.game)
        self.assertTrue(outcome.saw_candidates)

    async def test_resolve_reports_zero_candidates(self) -> None:
        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "cid"}, clear=False),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(return_value=[])),
        ):
            outcome = await igdb._resolve_game_with_status("Totally Absent Game", None)
        self.assertIsNone(outcome.game)
        self.assertFalse(outcome.saw_candidates)


class FetchIgdbChildrenTests(unittest.IsolatedAsyncioTestCase):
    """fetch_igdb_children: the one-shot dlcs+expansions lookup for a single
    IGDB game id (fallback dlc_ownership source for non-Steam games)."""

    async def test_parses_dlcs_and_expansions_into_combined_shape(self) -> None:
        captured = {}

        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            captured["query"] = query
            return [
                {
                    "dlcs": [
                        {"id": 1, "name": "DLC One"},
                        {"id": 2, "name": "DLC Two"},
                    ],
                    "expansions": [{"id": 3, "name": "Expansion One"}],
                }
            ]

        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            children = await igdb.fetch_igdb_children(42)

        self.assertIn("where id = 42;", captured["query"])
        self.assertIn(
            "dlcs.id, dlcs.name, expansions.id, expansions.name", captured["query"]
        )
        self.assertEqual(
            children,
            [
                {"igdb_id": 1, "name": "DLC One", "kind": "dlc"},
                {"igdb_id": 2, "name": "DLC Two", "kind": "dlc"},
                {"igdb_id": 3, "name": "Expansion One", "kind": "expansion"},
            ],
        )

    async def test_no_results_returns_empty_list(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return []

        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            children = await igdb.fetch_igdb_children(42)

        self.assertEqual(children, [])

    async def test_game_with_no_children_returns_empty_list(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            return [{"id": 42}]

        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post),
        ):
            children = await igdb.fetch_igdb_children(42)

        self.assertEqual(children, [])

    async def test_returns_none_without_credentials(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(await igdb.fetch_igdb_children(42))

    async def test_returns_none_on_request_failure(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"},
                clear=True,
            ),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch(
                "gamelib_mcp.data.igdb._post_igdb_games",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            self.assertIsNone(await igdb.fetch_igdb_children(42))


class GetIgdbChildrenCachedTests(unittest.IsolatedAsyncioTestCase):
    """get_igdb_children_cached: meta-KV cache (7-day TTL, stale-on-failure)
    around fetch_igdb_children, mirroring data/series_gaps.py's semantics."""

    async def test_fresh_fetch_stores_meta_and_returns_children(self) -> None:
        children = [{"igdb_id": 1, "name": "DLC One", "kind": "dlc"}]
        stored: dict[str, str] = {}

        async def fake_set_meta(key: str, value: str) -> None:
            stored[key] = value

        with (
            patch("gamelib_mcp.data.igdb.get_meta", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.igdb.set_meta", AsyncMock(side_effect=fake_set_meta)),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_children",
                AsyncMock(return_value=children),
            ),
        ):
            result = await igdb.get_igdb_children_cached(42)

        self.assertEqual(result, children)
        self.assertIn("igdb_children:42", stored)
        payload = json.loads(stored["igdb_children:42"])
        self.assertEqual(payload["children"], children)
        self.assertIn("fetched_at", payload)

    async def test_cache_hit_within_ttl_skips_network(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cached_children = [{"igdb_id": 5, "name": "Old DLC", "kind": "dlc"}]
        raw = json.dumps({"fetched_at": now, "children": cached_children})
        fetch_mock = AsyncMock(side_effect=AssertionError("should not fetch"))

        with (
            patch("gamelib_mcp.data.igdb.get_meta", AsyncMock(return_value=raw)),
            patch("gamelib_mcp.data.igdb.fetch_igdb_children", fetch_mock),
        ):
            result = await igdb.get_igdb_children_cached(42)

        self.assertEqual(result, cached_children)
        fetch_mock.assert_not_called()

    async def test_expired_cache_with_fetch_failure_serves_stale(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        stale_children = [{"igdb_id": 9, "name": "Stale DLC", "kind": "dlc"}]
        raw = json.dumps({"fetched_at": old, "children": stale_children})

        with (
            patch("gamelib_mcp.data.igdb.get_meta", AsyncMock(return_value=raw)),
            patch("gamelib_mcp.data.igdb.fetch_igdb_children", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.igdb.set_meta", AsyncMock()) as set_meta_mock,
        ):
            result = await igdb.get_igdb_children_cached(42)

        self.assertEqual(result, stale_children)
        set_meta_mock.assert_not_called()

    async def test_no_cache_and_fetch_failure_writes_negative_entry(self) -> None:
        # A failure with no prior cache writes a short-TTL negative marker so an
        # IGDB outage doesn't re-run the retry ladder on every detail view.
        stored: dict[str, str] = {}

        async def fake_set_meta(key: str, value: str) -> None:
            stored[key] = value

        with (
            patch("gamelib_mcp.data.igdb.get_meta", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.igdb.set_meta", AsyncMock(side_effect=fake_set_meta)),
            patch("gamelib_mcp.data.igdb.fetch_igdb_children", AsyncMock(return_value=None)),
        ):
            result = await igdb.get_igdb_children_cached(42)

        self.assertIsNone(result)
        payload = json.loads(stored["igdb_children:42"])
        self.assertTrue(payload["failed"])
        self.assertEqual(payload["children"], [])

        # Within the failure TTL the marker suppresses refetching entirely.
        fetch_mock = AsyncMock(side_effect=AssertionError("should not refetch"))
        with (
            patch(
                "gamelib_mcp.data.igdb.get_meta",
                AsyncMock(return_value=stored["igdb_children:42"]),
            ),
            patch("gamelib_mcp.data.igdb.fetch_igdb_children", fetch_mock),
        ):
            result = await igdb.get_igdb_children_cached(42)
        self.assertIsNone(result)
        fetch_mock.assert_not_called()

    async def test_expired_failure_marker_retries_and_success_overwrites(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        marker = json.dumps({"fetched_at": old, "children": [], "failed": True})
        children = [{"igdb_id": 3, "name": "Fresh DLC", "kind": "dlc"}]
        stored: dict[str, str] = {}

        async def fake_set_meta(key: str, value: str) -> None:
            stored[key] = value

        with (
            patch("gamelib_mcp.data.igdb.get_meta", AsyncMock(return_value=marker)),
            patch("gamelib_mcp.data.igdb.set_meta", AsyncMock(side_effect=fake_set_meta)),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_children",
                AsyncMock(return_value=children),
            ),
        ):
            result = await igdb.get_igdb_children_cached(42)

        self.assertEqual(result, children)
        payload = json.loads(stored["igdb_children:42"])
        self.assertEqual(payload["children"], children)
        self.assertNotIn("failed", payload)

    async def test_empty_children_list_is_cached_and_served(self) -> None:
        stored: dict[str, str] = {}

        async def fake_set_meta(key: str, value: str) -> None:
            stored[key] = value

        with (
            patch("gamelib_mcp.data.igdb.get_meta", AsyncMock(return_value=None)),
            patch("gamelib_mcp.data.igdb.set_meta", AsyncMock(side_effect=fake_set_meta)),
            patch("gamelib_mcp.data.igdb.fetch_igdb_children", AsyncMock(return_value=[])),
        ):
            result = await igdb.get_igdb_children_cached(42)

        self.assertEqual(result, [])
        payload = json.loads(stored["igdb_children:42"])
        self.assertEqual(payload["children"], [])

        # Second call within TTL must serve the cached empty list without
        # re-fetching — an empty catalog is itself a valid, cache-worthy
        # answer, not a miss.
        with (
            patch(
                "gamelib_mcp.data.igdb.get_meta",
                AsyncMock(return_value=stored["igdb_children:42"]),
            ),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_children",
                AsyncMock(side_effect=AssertionError("should not fetch")),
            ),
        ):
            result2 = await igdb.get_igdb_children_cached(42)

        self.assertEqual(result2, [])
