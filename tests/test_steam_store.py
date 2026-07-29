import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from conftest import ToolDBTestCase, make_steam_game, virtual_clock

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_store


def _store(**overrides) -> dict:
    """Base appdetails payload merged with per-test type/fullgame/dlc fields."""
    data = {
        "genres": [{"description": "Action"}],
        "categories": [{"description": "Single-player"}],
        "short_description": "",
        "release_date": {"date": ""},
        "metacritic": {},
    }
    data.update(overrides)
    return data

_STORE_DATA = {
    "genres": [{"description": "Action"}],
    "categories": [{"description": "Single-player"}],
    "short_description": "",
    "release_date": {"date": ""},
    "metacritic": {},
}


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


class SteamRequestGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_enforces_target_interval_without_wall_clock_sleep(self) -> None:
        gate = steam_store._SteamRequestGate(
            target_interval=0.5,
            max_requests_per_second=2,
            max_in_flight=1,
        )
        with virtual_clock("gamelib_mcp.data.steam_store") as clock:
            await gate.acquire()
            gate.release()
            await gate.acquire()
            gate.release()

        self.assertEqual(clock.sleeps, [0.5])

    async def test_gate_waits_for_window_budget_to_roll(self) -> None:
        # Steam's quota is windowed, so the gate must stop at the budget even
        # when the per-second pacing would happily let the next request through.
        gate = steam_store._SteamRequestGate(
            target_interval=0.0,
            max_requests_per_second=1000,
            max_in_flight=1,
            budget_window_seconds=10.0,
            max_requests_per_window=2,
        )
        with virtual_clock("gamelib_mcp.data.steam_store") as clock:
            for _ in range(3):
                await gate.acquire()
                gate.release()

        # The third request waits for the first to age out of the window.
        self.assertEqual(clock.sleeps, [10.0])

    async def test_rate_limit_penalty_parks_every_caller(self) -> None:
        # A 429 means the quota is gone for everyone, not just the caller that
        # caught it — the next request must not walk straight into another one.
        gate = steam_store._SteamRequestGate(
            target_interval=0.0,
            max_requests_per_second=1000,
            max_in_flight=1,
        )
        with virtual_clock("gamelib_mcp.data.steam_store") as clock:
            await gate.acquire()
            gate.release()
            gate.penalize(2.0)
            await gate.acquire()
            gate.release()

        self.assertEqual(clock.sleeps, [steam_store._STEAM_RATE_LIMIT_COOLDOWN_SECONDS])

    async def test_get_json_penalizes_gate_on_rate_limit(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, {}, headers={"Retry-After": "0"}),
                _DummyResponse(200, {"ok": True}),
            ]
        )

        with (
            virtual_clock("gamelib_mcp.data.steam_store"),
            patch("gamelib_mcp.data.steam_store._sleep_before_retry", new=AsyncMock()),
            patch.object(steam_store._STEAM_REQUEST_GATE, "penalize") as penalize,
        ):
            await steam_store._steam_get_json_with_retry(
                client,
                steam_store.STORE_API,
                params={"appids": 10},
                timeout=15,
            )

        penalize.assert_called_once()

    async def test_get_json_penalizes_gate_on_terminal_rate_limit(self) -> None:
        # Every attempt 429s and retries exhaust: the gate must still be parked,
        # or the next queued call starts straight into the same quota outage.
        client = _DummyAsyncClient(
            [_DummyResponse(429, {}, headers={"Retry-After": "0"})]
            * (steam_store._STEAM_MAX_RETRIES + 1)
        )

        with (
            virtual_clock("gamelib_mcp.data.steam_store"),
            patch("gamelib_mcp.data.steam_store._sleep_before_retry", new=AsyncMock()),
            patch.object(steam_store._STEAM_REQUEST_GATE, "penalize") as penalize,
            self.assertRaises(steam_store.httpx.HTTPStatusError),
        ):
            await steam_store._steam_get_json_with_retry(
                client,
                steam_store.STORE_API,
                params={"appids": 10},
                timeout=15,
            )

        self.assertEqual(penalize.call_count, steam_store._STEAM_MAX_RETRIES + 1)


class SteamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_json_retries_rate_limit_response(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, {}, headers={"Retry-After": "0"}),
                _DummyResponse(200, {"ok": True}),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            virtual_clock("gamelib_mcp.data.steam_store") as clock,
            patch("gamelib_mcp.data.steam_store._sleep_before_retry", new=sleep_mock),
        ):
            payload = await steam_store._steam_get_json_with_retry(
                client,
                steam_store.STORE_API,
                params={"appids": 10},
                timeout=15,
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(client.calls, 2)
        sleep_mock.assert_awaited()
        # The 429 also parks the shared gate, so the retry waits out the
        # cooldown on top of _sleep_before_retry's own backoff.
        self.assertEqual(clock.sleeps, [steam_store._STEAM_RATE_LIMIT_COOLDOWN_SECONDS])

    async def test_get_json_uses_retry_after_before_backoff_jitter(self) -> None:
        client = _DummyAsyncClient(
            [
                _DummyResponse(429, {}, headers={"Retry-After": "7"}),
                _DummyResponse(200, {"ok": True}),
            ]
        )
        sleep_mock = AsyncMock()

        with (
            virtual_clock("gamelib_mcp.data.steam_store") as clock,
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
        # Retry-After 7 is shorter than the cooldown floor, so the gate holds
        # every caller for the full 10s rather than the header's 7.
        self.assertEqual(clock.sleeps, [steam_store._STEAM_RATE_LIMIT_COOLDOWN_SECONDS])


class EnrichGameTagSeedTests(ToolDBTestCase):
    async def _tags(self, game_id: int) -> list[str]:
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (game_id,))
        return json.loads(row["tags"]) if row["tags"] else []

    async def test_does_not_clobber_existing_tags(self) -> None:
        # SteamSpy already populated rich community tags; the 7-day store re-run
        # must NOT overwrite them with the genre-derived list (COALESCE seed).
        game_id = await make_steam_game("Sekiro", appid=814380, tags=["souls-like", "difficult"])

        with patch.object(steam_store, "_fetch_all", AsyncMock(return_value=(_STORE_DATA, {}))):
            await steam_store.enrich_game(814380)

        self.assertEqual(await self._tags(game_id), ["souls-like", "difficult"])

    async def test_seeds_tags_when_null(self) -> None:
        game_id = await make_steam_game("Blank", appid=999001)  # tags NULL

        with patch.object(steam_store, "_fetch_all", AsyncMock(return_value=(_STORE_DATA, {}))):
            await steam_store.enrich_game(999001)

        # Genre/category tags canonicalized (lowercased) on the seed path.
        self.assertEqual(await self._tags(game_id), ["action", "single-player"])


class EnrichGameContentClassificationTests(ToolDBTestCase):
    async def _content(self, game_id: int):
        async with db_module.get_db() as db:
            return await db.execute_fetchone(
                "SELECT content_type, parent_game_id, is_primary_library_item "
                "FROM games WHERE id = ?",
                (game_id,),
            )

    async def _set_columns(self, game_id: int, **cols) -> None:
        cols_sql = ", ".join(f"{c} = ?" for c in cols)
        async with db_module.get_db() as db:
            await db.execute(
                f"UPDATE games SET {cols_sql} WHERE id = ?", (*cols.values(), game_id)
            )
            await db.commit()

    async def _enrich(self, appid: int, store_data: dict) -> None:
        with patch.object(steam_store, "_fetch_all", AsyncMock(return_value=(store_data, {}))):
            await steam_store.enrich_game(appid)

    async def test_dlc_parent_resolved_by_steam_appid(self) -> None:
        parent_id = await make_steam_game("Base Game", appid=100)
        dlc_id = await make_steam_game("Base Game - Season Pass", appid=200)

        await self._enrich(
            200,
            _store(type="dlc", fullgame={"appid": 100, "name": "Base Game"}),
        )

        row = await self._content(dlc_id)
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["parent_game_id"], parent_id)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_dlc_parent_resolved_by_name_when_appid_missing(self) -> None:
        # fullgame appid unknown/absent, so the steam-appid branch misses and
        # apply_content_classification falls through to the parent_name path.
        parent_id = await make_steam_game("Parent Game", appid=300)
        dlc_id = await make_steam_game("Parent Game - Extra", appid=400)

        await self._enrich(400, _store(type="dlc", fullgame={"name": "Parent Game"}))

        row = await self._content(dlc_id)
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["parent_game_id"], parent_id)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_dlc_without_matching_parent_has_null_parent(self) -> None:
        dlc_id = await make_steam_game("Orphan DLC", appid=500)

        await self._enrich(
            500,
            _store(type="dlc", fullgame={"appid": 8888, "name": "Nonexistent Parent"}),
        )

        row = await self._content(dlc_id)
        self.assertEqual(row["content_type"], "dlc")
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_game_type_does_not_clobber_stored_dlc(self) -> None:
        game_id = await make_steam_game("Was A DLC", appid=600)
        await self._set_columns(game_id, content_type="dlc", is_primary_library_item=0)

        await self._enrich(600, _store(type="game"))

        row = await self._content(game_id)
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_base_game_dlc_list_written_to_meta_catalog(self) -> None:
        await make_steam_game("Base With DLC", appid=700)

        await self._enrich(700, _store(type="game", dlc=[701, 702, 703]))

        raw = await db_module.get_meta("steam_dlc_catalog:700")
        self.assertIsNotNone(raw)
        payload = json.loads(raw)
        self.assertEqual(payload["appids"], [701, 702, 703])
        self.assertIn("fetched_at", payload)

    async def test_dlc_type_does_not_write_catalog(self) -> None:
        await make_steam_game("A DLC", appid=800)

        await self._enrich(
            800,
            _store(type="dlc", fullgame={"name": "X"}, dlc=[999]),
        )

        self.assertIsNone(await db_module.get_meta("steam_dlc_catalog:800"))

    async def test_music_type_classified_unknown_addon(self) -> None:
        game_id = await make_steam_game("Original Soundtrack", appid=900)

        await self._enrich(900, _store(type="music"))

        row = await self._content(game_id)
        self.assertEqual(row["content_type"], "unknown_addon")
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_unknown_type_writes_no_classification(self) -> None:
        game_id = await make_steam_game("A Trailer", appid=901)

        before = await self._content(game_id)
        await self._enrich(901, _store(type="video"))

        row = await self._content(game_id)
        # Unmapped type -> classify returns None -> row untouched at its default.
        self.assertEqual(row["content_type"], before["content_type"])
        self.assertEqual(row["content_type"], "base_game")

    async def test_manual_override_on_content_type_is_respected(self) -> None:
        game_id = await make_steam_game("Pinned", appid=1000)
        # User pinned content_type=dlc; a later music signal must not change it.
        await self._set_columns(
            game_id,
            content_type="dlc",
            is_primary_library_item=0,
            manual_overrides=json.dumps(["content_type"]),
        )

        await self._enrich(1000, _store(type="music"))

        row = await self._content(game_id)
        self.assertEqual(row["content_type"], "dlc")

    async def test_malformed_payloads_do_not_crash(self) -> None:
        dlc_id = await make_steam_game("Malformed DLC", appid=1100)
        base_id = await make_steam_game("Malformed Base", appid=1101)

        # fullgame missing appid -> resolves by name (no match) -> NULL parent.
        await self._enrich(1100, _store(type="dlc", fullgame={"name": "Unseen"}))
        # dlc field is not a list -> no catalog, no crash.
        await self._enrich(1101, _store(type="game", dlc="not-a-list"))

        dlc_row = await self._content(dlc_id)
        self.assertEqual(dlc_row["content_type"], "dlc")
        self.assertIsNone(dlc_row["parent_game_id"])
        self.assertIsNone(await db_module.get_meta("steam_dlc_catalog:1101"))
        base_row = await self._content(base_id)
        self.assertEqual(base_row["content_type"], "base_game")


if __name__ == "__main__":
    unittest.main()
