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


class IGDBRetryTests(unittest.IsolatedAsyncioTestCase):
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
