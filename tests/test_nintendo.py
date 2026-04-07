import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import igdb, nintendo


class SyncNintendoTests(unittest.TestCase):
    def _run_sync(self, entries, resolve_result=(42, None), candidates=None):
        mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_upsert_identifier = AsyncMock()
        mock_upsert_enrichment = AsyncMock()

        with (
            patch.dict("os.environ", {"NINTENDO_SESSION_TOKEN": ""}, clear=False),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=False),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value={"session": "cookie"}),
            patch("gamelib_mcp.data.nintendo.fetch_nintendo_library_vgcs", AsyncMock(return_value=entries)),
            patch("gamelib_mcp.data.nintendo.load_fuzzy_candidates", AsyncMock(return_value=candidates or {})),
            patch("gamelib_mcp.data.nintendo.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform_identifier", mock_upsert_identifier),
            patch("gamelib_mcp.data.nintendo.upsert_game_platform_enrichment", mock_upsert_enrichment),
        ):
            result = asyncio.run(nintendo.sync_nintendo())

        return result, mock_resolve, mock_upsert_platform, mock_upsert_identifier, mock_upsert_enrichment

    def test_vgcs_fallback_still_syncs_when_igdb_returns_no_result(self) -> None:
        entries = [
            {
                "name": "Fire Emblem Engage",
                "playtime_minutes": None,
                "title_id": "0100a5c00d9a2000",
            }
        ]

        result, mock_resolve, mock_upsert_platform, mock_upsert_identifier, mock_enrichment = self._run_sync(entries)

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Fire Emblem Engage", igdb.PLATFORM_TO_IGDB["switch2"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="switch2",
            playtime_minutes=None,
            owned=1,
        )
        mock_upsert_identifier.assert_awaited_once_with(99, nintendo.NINTENDO_TITLE_ID, "0100a5c00d9a2000")
        mock_enrichment.assert_not_called()

    def test_skips_when_no_credentials_are_available(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gamelib_mcp.data.nintendo._load_vgcs_cookies", return_value=None),
            patch("gamelib_mcp.data.nintendo._nxapi_available", return_value=False),
        ):
            result = asyncio.run(nintendo.sync_nintendo())

        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})


if __name__ == "__main__":
    unittest.main()
