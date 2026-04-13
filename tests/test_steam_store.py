import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from gamelib_mcp.data import steam_store


class _DummyResponse:
    def __init__(self, status_code: int, json_data, headers: dict[str, str] | None = None, url: str | None = None):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.request = httpx.Request("GET", url or steam_store.STORE_API)

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

    async def get(self, *args, **kwargs):
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


class SteamRequestGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_enforces_target_interval_without_wall_clock_sleep(self) -> None:
        gate = steam_store._SteamRequestGate(
            target_interval=0.5,
            max_requests_per_second=2,
            max_in_flight=1,
        )
        clock = _FakeClock()

        with (
            patch("gamelib_mcp.data.steam_store.time.monotonic", side_effect=clock.monotonic),
            patch("gamelib_mcp.data.steam_store.asyncio.sleep", new=clock.sleep),
        ):
            await gate.acquire()
            gate.release()
            await gate.acquire()
            gate.release()

        self.assertEqual(clock.sleeps, [0.5])


class SteamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_json_retries_rate_limit_response(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, {}, headers={"Retry-After": "0"}),
                _DummyResponse(200, {"ok": True}),
            ]
        )
        sleep_mock = AsyncMock()

        with patch("gamelib_mcp.data.steam_store._sleep_before_retry", new=sleep_mock):
            payload = await steam_store._steam_get_json_with_retry(
                client,
                steam_store.STORE_API,
                params={"appids": 10},
                timeout=15,
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(client.calls, 2)
        sleep_mock.assert_awaited()

    async def test_get_json_uses_retry_after_before_backoff_jitter(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, {}, headers={"Retry-After": "7"}),
                _DummyResponse(200, {"ok": True}),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            patch("gamelib_mcp.data.steam_store._sleep_before_retry", new=sleep_mock),
            patch("gamelib_mcp.data.steam_store.random.uniform", side_effect=AssertionError("unexpected jitter")),
        ):
            payload = await steam_store._steam_get_json_with_retry(
                client,
                steam_store.STORE_API,
                params={"appids": 10},
                timeout=15,
            )

        self.assertEqual(payload, {"ok": True})
        sleep_mock.assert_awaited_once_with(7.0)


if __name__ == "__main__":
    unittest.main()
