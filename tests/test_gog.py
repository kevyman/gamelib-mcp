import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from steam_mcp.data import gog


class ParseJsonTests(unittest.TestCase):
    def test_parses_title_and_product_id(self) -> None:
        stdout = '[{"gamename": "cyberpunk_2077", "title": "Cyberpunk 2077", "product_id": 1423049431}]'
        result = gog._parse_lgogdownloader_json(stdout)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Cyberpunk 2077")
        self.assertEqual(result[0]["product_id"], 1423049431)

    def test_skips_empty_list(self) -> None:
        result = gog._parse_lgogdownloader_json("[]")
        self.assertEqual(result, [])

    def test_handles_missing_product_id(self) -> None:
        stdout = '[{"gamename": "some_game", "title": "Some Game"}]'
        result = gog._parse_lgogdownloader_json(stdout)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Some Game")
        self.assertIsNone(result[0]["product_id"])

    def test_returns_title_not_gamename(self) -> None:
        stdout = '[{"gamename": "witcher_3_wild_hunt", "title": "The Witcher 3: Wild Hunt", "product_id": 1207664663}]'
        result = gog._parse_lgogdownloader_json(stdout)
        self.assertEqual(result[0]["title"], "The Witcher 3: Wild Hunt")
        self.assertNotEqual(result[0]["title"], "witcher_3_wild_hunt")


class ConfigDirTests(unittest.TestCase):
    def test_default_config_dir(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            # Remove override if present
            import os
            os.environ.pop("LGOGDOWNLOADER_CONFIG_PATH", None)
            result = gog._config_dir()
        self.assertIsInstance(result, Path)
        self.assertTrue(str(result).endswith("lgogdownloader"))

    def test_env_override(self) -> None:
        with patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/custom/lgogdownloader"}, clear=False):
            result = gog._config_dir()
        self.assertEqual(result, Path("/custom/lgogdownloader"))


class SubprocessEnvTests(unittest.TestCase):
    def test_xdg_config_home_set_to_parent(self) -> None:
        with patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False):
            env = gog._subprocess_env()
        self.assertEqual(env["XDG_CONFIG_HOME"], "/config")

    def test_existing_env_preserved(self) -> None:
        with patch.dict(
            "os.environ",
            {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader", "HOME": "/home/user"},
            clear=False,
        ):
            env = gog._subprocess_env()
        self.assertEqual(env["HOME"], "/home/user")


class SyncGogSkipTests(unittest.TestCase):
    def test_skips_when_lgogdownloader_not_in_path(self) -> None:
        mock_which = MagicMock(return_value=None)
        with (
            patch("steam_mcp.data.gog.shutil") as mock_shutil,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
        ):
            mock_shutil.which = mock_which
            result = asyncio.run(gog.sync_gog())
        mock_which.assert_called_once_with("lgogdownloader")
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_skips_when_config_dir_missing(self) -> None:
        with (
            patch("steam_mcp.data.gog.shutil") as mock_shutil,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/nonexistent/lgogdownloader"}, clear=False),
        ):
            mock_shutil.which = MagicMock(return_value="/usr/bin/lgogdownloader")
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_skips_on_nonzero_returncode(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch("steam_mcp.data.gog.shutil") as mock_shutil,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
        ):
            mock_shutil.which = MagicMock(return_value="/usr/bin/lgogdownloader")
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})


class SyncGogSyncTests(unittest.TestCase):
    def _make_proc(self, stdout: str, returncode: int = 0) -> MagicMock:
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
        return mock_proc

    def _run_sync(self, stdout: str, find_result, upsert_game_return=42, platform_id=99):
        import json

        game_list = json.loads(stdout)
        proc = self._make_proc(stdout)

        mock_find = AsyncMock(return_value=find_result)
        mock_upsert_game = AsyncMock(return_value=upsert_game_return)
        mock_upsert_platform = AsyncMock(return_value=platform_id)
        mock_upsert_identifier = AsyncMock(return_value=None)
        mock_load_candidates = AsyncMock(return_value={})

        with (
            patch("shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("steam_mcp.data.gog.find_game_by_name_fuzzy", mock_find),
            patch("steam_mcp.data.gog.upsert_game", mock_upsert_game),
            patch("steam_mcp.data.gog.upsert_game_platform", mock_upsert_platform),
            patch("steam_mcp.data.gog.upsert_game_platform_identifier", mock_upsert_identifier),
            patch("steam_mcp.data.gog.load_fuzzy_candidates", mock_load_candidates),
        ):
            result = asyncio.run(gog.sync_gog())

        return result, mock_upsert_game, mock_upsert_platform, mock_upsert_identifier

    def test_matched_game_increments_matched(self) -> None:
        stdout = '[{"gamename": "cyberpunk_2077", "title": "Cyberpunk 2077", "product_id": 1423049431}]'
        existing_game = {"id": 7, "name": "Cyberpunk 2077"}

        result, mock_upsert_game, _, _ = self._run_sync(stdout, find_result=existing_game)

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        mock_upsert_game.assert_not_called()

    def test_unmatched_game_increments_added(self) -> None:
        stdout = '[{"gamename": "some_indie", "title": "Some Indie Game", "product_id": 9999999}]'

        result, mock_upsert_game, _, _ = self._run_sync(stdout, find_result=None)

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], 0)
        mock_upsert_game.assert_called_once()

    def test_upsert_game_platform_called_with_none_playtime(self) -> None:
        stdout = '[{"gamename": "some_indie", "title": "Some Indie Game", "product_id": 9999999}]'

        _, _, mock_upsert_platform, _ = self._run_sync(stdout, find_result=None)

        call_kwargs = mock_upsert_platform.call_args
        # playtime_minutes must be None — GOG playtime not available via lgogdownloader
        self.assertIsNone(call_kwargs.kwargs.get("playtime_minutes"))

    def test_upsert_game_platform_identifier_called_with_product_id(self) -> None:
        from steam_mcp.data.db import GOG_PRODUCT_ID

        stdout = '[{"gamename": "cyberpunk_2077", "title": "Cyberpunk 2077", "product_id": 1423049431}]'

        _, _, _, mock_upsert_identifier = self._run_sync(stdout, find_result=None, platform_id=99)

        mock_upsert_identifier.assert_called_once()
        call_args = mock_upsert_identifier.call_args
        # First positional arg is platform_id, second is identifier_type, third is identifier_value
        self.assertEqual(call_args.args[0], 99)
        self.assertEqual(call_args.args[1], GOG_PRODUCT_ID)
        self.assertEqual(call_args.args[2], 1423049431)


if __name__ == "__main__":
    unittest.main()
