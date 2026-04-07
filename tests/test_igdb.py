import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import httpx

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
            if "category !=" in query:
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

    async def test_search_game_uses_retry_after_before_backoff_jitter(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, [], headers={"Retry-After": "7"}),
                _DummyResponse(200, []),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=sleep_mock),
            patch("gamelib_mcp.data.igdb.random.uniform", side_effect=AssertionError("unexpected jitter")),
        ):
            results = await igdb.search_game("Portal 2")

        self.assertEqual(results, [])
        sleep_mock.assert_awaited_once_with(7.0)

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
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb._sleep_before_retry", new=sleep_mock),
        ):
            results = await igdb.search_game("Portal 2")

        self.assertEqual(results, [])
        self.assertEqual(client.calls, 4)
        self.assertEqual(sleep_mock.await_count, 3)


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
                if self_sql != "INSERT INTO games (name) VALUES (?)":
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

        async def upsert_game(*, appid: int | None, name: str):
            self.assertIsNone(appid)
            self.assertEqual(name, "Portal")
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
