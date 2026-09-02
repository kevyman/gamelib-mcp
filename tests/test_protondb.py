import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import ToolDBTestCase

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import protondb, provider_health


class ProtonDBQualityGateTests(ToolDBTestCase):
    async def _seed_game_with_tier(self, tier: str) -> tuple[int, int]:
        """Insert a game + steam platform row with a known tier. Returns (game_id, game_platform_id)."""
        game_id = await db_module.upsert_game(appid=123, name="Half-Life 2")
        game_platform_id = await db_module.upsert_game_platform(game_id, "steam")
        await db_module.upsert_steam_platform_data(game_platform_id, protondb_tier=tier)
        return game_id, game_platform_id

    async def _read_row(self, game_platform_id: int) -> dict | None:
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT protondb_tier, protondb_cached_at FROM steam_platform_data WHERE game_platform_id = ?",
                (game_platform_id,),
            )
        return dict(row) if row else None

    async def test_fetch_exception_does_not_overwrite_tier(self) -> None:
        _, game_platform_id = await self._seed_game_with_tier("gold")

        mock_client = AsyncMock()
        mock_client.get.side_effect = RuntimeError("connection refused")
        with patch("gamelib_mcp.data.protondb.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await protondb._fetch_and_cache(123, game_platform_id)

        self.assertIsNone(result)
        row = await self._read_row(game_platform_id)

        # Tier must survive the failure; cached_at should be stamped (backoff marker).
        self.assertEqual(row["protondb_tier"], "gold")
        self.assertIsNotNone(row["protondb_cached_at"])

    async def test_non_200_does_not_overwrite_tier(self) -> None:
        _, game_platform_id = await self._seed_game_with_tier("platinum")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        with patch("gamelib_mcp.data.protondb.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await protondb._fetch_and_cache(123, game_platform_id)

        self.assertIsNone(result)
        row = await self._read_row(game_platform_id)

        # Tier must survive the failure; cached_at should be stamped (backoff marker).
        self.assertEqual(row["protondb_tier"], "platinum")
        self.assertIsNotNone(row["protondb_cached_at"])

    async def _fetch_with_status(self, status_code: int) -> None:
        _, game_platform_id = await self._seed_game_with_tier("platinum")
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        with patch("gamelib_mcp.data.protondb.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await protondb._fetch_and_cache(123, game_platform_id)

    async def test_404_is_a_miss_not_a_provider_failure(self) -> None:
        # ProtonDB answers 404 for an app nobody has reported on. That is an
        # answer, so the enrichment health counter must not read a library of
        # obscure games as a dead provider.
        provider_health.reset()
        await self._fetch_with_status(404)
        stats = provider_health.snapshot()["protondb"]
        self.assertEqual(stats["failures"], 0)
        self.assertEqual(stats["successes"], 1)

    async def test_server_error_is_a_provider_failure(self) -> None:
        provider_health.reset()
        await self._fetch_with_status(503)
        stats = provider_health.snapshot()["protondb"]
        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["successes"], 0)
        self.assertIn("503", stats["last_error"])

    async def test_success_writes_tier(self) -> None:
        game_id = await db_module.upsert_game(appid=456, name="Portal 2")
        game_platform_id = await db_module.upsert_game_platform(game_id, "steam")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"tier": "silver"}
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        with patch("gamelib_mcp.data.protondb.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await protondb._fetch_and_cache(456, game_platform_id)

        self.assertEqual(result, "silver")
        row = await self._read_row(game_platform_id)

        self.assertEqual(row["protondb_tier"], "silver")


if __name__ == "__main__":
    asyncio.run(unittest.main())
