import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import protondb


class ProtonDBQualityGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "protondb.sqlite"
        db_module._DB_READY_PATH = None
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            await db_module.init_db()

    async def asyncTearDown(self) -> None:
        db_module._DB_READY_PATH = None
        self.tmpdir.cleanup()

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
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
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
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
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

    async def test_success_writes_tier(self) -> None:
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
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
