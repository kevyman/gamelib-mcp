import sys
import types
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import aiosqlite  # type: ignore
except ModuleNotFoundError:
    aiosqlite = types.ModuleType("aiosqlite")

    class Connection:  # minimal stub for db.py import-time polyfill
        pass

    class Row(dict):
        pass

    async def connect(*_args, **_kwargs):
        raise ModuleNotFoundError("aiosqlite is not installed")

    aiosqlite.Connection = Connection
    aiosqlite.Row = Row
    aiosqlite.connect = connect
    sys.modules["aiosqlite"] = aiosqlite

from gamelib_mcp.data import gog, igdb


class ParseOutputTests(unittest.TestCase):
    """Tests for _parse_lgogdownloader_output() — pure function, no I/O."""

    def test_parses_plain_slug(self) -> None:
        result = gog._parse_lgogdownloader_output("cyberpunk_2077\n")
        self.assertEqual(result, ["Cyberpunk 2077"])

    def test_strips_ansi_codes(self) -> None:
        result = gog._parse_lgogdownloader_output("\x1b[01;34mcyberpunk_2077\x1b[0m\n")
        self.assertEqual(result, ["Cyberpunk 2077"])

    def test_strips_update_indicator(self) -> None:
        result = gog._parse_lgogdownloader_output("cyberpunk_2077 [1]\n")
        self.assertEqual(result, ["Cyberpunk 2077"])

    def test_strips_ansi_and_update_indicator(self) -> None:
        result = gog._parse_lgogdownloader_output("\x1b[01;34mcyberpunk_2077 [1]\x1b[0m\n")
        self.assertEqual(result, ["Cyberpunk 2077"])

    def test_skips_blank_lines(self) -> None:
        result = gog._parse_lgogdownloader_output("game_one\n\ngame_two\n")
        self.assertEqual(result, ["Game One", "Game Two"])

    def test_multiple_games(self) -> None:
        output = "a_plague_tale_innocence\nthe_witcher_3_wild_hunt\n"
        result = gog._parse_lgogdownloader_output(output)
        self.assertEqual(result, ["A Plague Tale Innocence", "The Witcher 3 Wild Hunt"])

    def test_empty_output_returns_empty_list(self) -> None:
        self.assertEqual(gog._parse_lgogdownloader_output(""), [])


class ConfigDirTests(unittest.TestCase):
    def test_default_config_dir(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
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
        with (
            patch("gamelib_mcp.data.gog.shutil") as mock_shutil,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
        ):
            mock_shutil.which = MagicMock(return_value=None)
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_skips_when_config_dir_missing(self) -> None:
        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("gamelib_mcp.data.gog._config_dir", return_value=Path("/nonexistent/path/that/cannot/exist")),
        ):
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_skips_on_nonzero_returncode(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch("gamelib_mcp.data.gog.shutil") as mock_shutil,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
        ):
            mock_shutil.which = MagicMock(return_value="/usr/bin/lgogdownloader")
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})


class SyncGogSyncTests(unittest.TestCase):
    def _make_proc(self, stdout: bytes, returncode: int = 0) -> MagicMock:
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.communicate = AsyncMock(return_value=(stdout, b""))
        return mock_proc

    def _run_sync(self, stdout: bytes, resolve_result, candidates=None):
        proc = self._make_proc(stdout)
        mock_resolve = AsyncMock(return_value=resolve_result)
        mock_upsert_platform = AsyncMock(return_value=99)
        mock_load_candidates = AsyncMock(return_value=candidates or {})

        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.gog.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.gog.load_fuzzy_candidates", mock_load_candidates),
        ):
            result = asyncio.run(gog.sync_gog())

        return result, mock_resolve, mock_upsert_platform

    def test_matched_game_increments_matched(self) -> None:
        result, mock_resolve, mock_upsert_platform = self._run_sync(
            b"cyberpunk_2077\n",
            resolve_result=(7, None),
            candidates={7: "Cyberpunk 2077"},
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Cyberpunk 2077", igdb.PLATFORM_TO_IGDB["gog"]),
        )
        mock_upsert_platform.assert_awaited_once_with(
            game_id=7,
            platform="gog",
            playtime_minutes=None,
            owned=1,
        )

    def test_unmatched_game_increments_added(self) -> None:
        result, mock_resolve, mock_upsert_platform = self._run_sync(
            b"some_indie_game\n",
            resolve_result=(42, None),
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], 0)
        mock_resolve.assert_awaited_once()
        mock_upsert_platform.assert_awaited_once_with(
            game_id=42,
            platform="gog",
            playtime_minutes=None,
            owned=1,
        )

    def test_upsert_game_platform_called_with_none_playtime(self) -> None:
        _, _, mock_upsert_platform = self._run_sync(
            b"some_indie_game\n",
            resolve_result=(42, None),
        )
        call_kwargs = mock_upsert_platform.call_args
        self.assertIsNone(call_kwargs.kwargs.get("playtime_minutes"))

    def test_ansi_stripped_before_fuzzy_match(self) -> None:
        """Verify ANSI codes don't pollute the title passed to the IGDB resolver."""
        mock_resolve = AsyncMock(return_value=(5, None))

        proc = self._make_proc(b"\x1b[01;34mcyberpunk_2077 [1]\x1b[0m\n")

        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
            patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
        ):
            asyncio.run(gog.sync_gog())

        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Cyberpunk 2077", igdb.PLATFORM_TO_IGDB["gog"]),
        )

    def test_non_game_rows_are_skipped_before_resolving(self) -> None:
        proc = self._make_proc(b"quake_ii_quad_damage_game\nq_u_b_e_2_soundtrack\n")
        mock_resolve = AsyncMock(return_value=(5, None))

        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
            patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
        ):
            result = asyncio.run(gog.sync_gog())

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 1})
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Quake Ii Quad Damage", igdb.PLATFORM_TO_IGDB["gog"]),
        )


if __name__ == "__main__":
    unittest.main()
