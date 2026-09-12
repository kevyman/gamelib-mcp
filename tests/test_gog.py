import asyncio
import contextlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

try:
    import aiosqlite  # type: ignore
except ModuleNotFoundError:
    aiosqlite = types.ModuleType("aiosqlite")

    class Connection:  # minimal stub for db package import-time polyfill
        pass

    class Row(dict):
        pass

    async def connect(*_args, **_kwargs):
        raise ModuleNotFoundError("aiosqlite is not installed")

    aiosqlite.Connection = Connection
    aiosqlite.Row = Row
    aiosqlite.connect = connect
    sys.modules["aiosqlite"] = aiosqlite

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import gog, igdb


@contextlib.contextmanager
def _gog_session_dir(token: str = "tok123"):
    """A temp lgogdownloader config dir holding a galaxy_tokens.json session."""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "galaxy_tokens.json"), "w", encoding="utf-8") as f:
            json.dump({"access_token": token}, f)
        with patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": tmp}, clear=False):
            yield Path(tmp)


def _product(product_id, title, slug, **extra) -> dict:
    return {"id": product_id, "title": title, "slug": slug, "isGame": True, **extra}


def _listing_transport(pages: list[dict]):
    """MockTransport serving the account listing page by page, plus the log of
    (query params, Authorization header) each request carried."""
    seen: list[tuple[dict, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append((params, request.headers.get("Authorization")))
        return httpx.Response(
            200,
            json=pages[int(params["page"]) - 1],
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler), seen


async def _identifiers_for_game(game_id: int) -> list[tuple[str, str]]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT gpi.identifier_type, gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gp.game_id = ? AND gp.platform = 'gog'""",
            (game_id,),
        )
    return [(row["identifier_type"], row["identifier_value"]) for row in rows]


async def _game_name(game_id: int) -> str:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone("SELECT name FROM games WHERE id = ?", (game_id,))
    return row["name"]


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

    def test_auth_files_detects_session_like_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "cookies.txt").write_text("session", encoding="utf-8")

            self.assertTrue(gog._has_auth_files(config_dir))

    def test_auth_files_ignores_empty_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(gog._has_auth_files(Path(tmp)))


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
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "degraded")
        self.assertEqual(result["error_classification"], "missing_runtime_dependency")

    def test_skips_when_config_dir_missing(self) -> None:
        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("gamelib_mcp.data.gog._config_dir", return_value=Path("/nonexistent/path/that/cannot/exist")),
        ):
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_skips_when_session_files_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            with (
                patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
                patch("gamelib_mcp.data.gog._config_dir", return_value=config_dir),
            ):
                result = asyncio.run(gog.sync_gog())

        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "unconfigured")
        self.assertEqual(result["error_classification"], "missing_configuration")

    def test_skips_on_nonzero_returncode(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch("gamelib_mcp.data.gog.shutil") as mock_shutil,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.gog._has_auth_files", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
        ):
            mock_shutil.which = MagicMock(return_value="/usr/bin/lgogdownloader")
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["sync_status"], "failed")
        self.assertIn("lgogdownloader --list failed", result["error_summary"])


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
            patch("gamelib_mcp.data.gog._has_auth_files", return_value=True),
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
            from_source=True,
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
            from_source=True,
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
            patch("gamelib_mcp.data.gog._has_auth_files", return_value=True),
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

    def test_resync_reuses_existing_gog_row_without_reresolving(self) -> None:
        """A same-normalized-name row that already owns gog IS this catalog item.

        GOG has no per-item store id, so the title is the stable key. Prod dupe
        root cause: re-running the title through IGDB landed on a *different*
        same-named IGDB candidate whose conflicting release year made the fuzzy
        fallback refuse the existing row and fork a duplicate ("Agony" pair).
        The pre-match must short-circuit before any IGDB resolution.
        """
        proc = self._make_proc(b"agony\n")
        mock_resolve = AsyncMock(
            side_effect=AssertionError("re-sync must not re-resolve through IGDB")
        )
        mock_upsert_platform = AsyncMock(return_value=99)
        existing_row = {"id": 7, "name": "Agony"}

        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.gog._has_auth_files", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch(
                "gamelib_mcp.data.gog.get_platform_game_by_normalized_name",
                AsyncMock(return_value=existing_row),
            ) as prematch,
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.gog.upsert_game_platform", mock_upsert_platform),
            patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
        ):
            result = asyncio.run(gog.sync_gog())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        prematch.assert_awaited_once_with("Agony", "gog")
        mock_upsert_platform.assert_awaited_once_with(
            game_id=7,
            platform="gog",
            playtime_minutes=None,
            owned=1,
            from_source=True,
        )

    def test_non_game_rows_are_skipped_before_resolving(self) -> None:
        proc = self._make_proc(b"quake_ii_quad_damage_game\nq_u_b_e_2_soundtrack\n")
        mock_resolve = AsyncMock(return_value=(5, None))

        with (
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": "/config/lgogdownloader"}, clear=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("gamelib_mcp.data.gog._has_auth_files", return_value=True),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
            patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
        ):
            result = asyncio.run(gog.sync_gog())

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 1)
        # No stored session in this temp config dir, so the account listing
        # never gets off the ground and the CLI backend carries the sync.
        self.assertEqual(result["listing_backend"], "lgogdownloader")
        self.assertIn("lgogdownloader --login", result["listing_error"])
        mock_resolve.assert_awaited_once()
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Quake II Quad Damage", igdb.PLATFORM_TO_IGDB["gog"]),
        )


class FetchLibraryProductsTests(unittest.IsolatedAsyncioTestCase):
    """The account listing (the endpoint lgogdownloader itself reads)."""

    async def test_paginates_and_filters_non_games(self) -> None:
        pages = [
            {
                "page": 1,
                "totalPages": 2,
                "products": [
                    _product(
                        1207658930,
                        "Legacy of Kain: Defiance",
                        "legacy_of_kain_defiance",
                        dlcCount=0,
                    ),
                    # A movie in the same account library must never become a game.
                    _product(
                        2001,
                        "Double Fine Adventure",
                        "double_fine_adventure",
                        isGame=False,
                        isMovie=True,
                    ),
                ],
            },
            {
                "page": 2,
                "totalPages": 2,
                "products": [
                    _product("1449802253", "Spells & Secrets", "spells_secrets"),
                ],
            },
        ]
        transport, seen = _listing_transport(pages)

        with _gog_session_dir():
            products = await gog.fetch_gog_library_products(transport=transport)

        self.assertEqual(
            [(p.product_id, p.title, p.slug) for p in products],
            [
                ("1207658930", "Legacy of Kain: Defiance", "legacy_of_kain_defiance"),
                ("1449802253", "Spells & Secrets", "spells_secrets"),
            ],
        )
        self.assertEqual([params["page"] for params, _ in seen], ["1", "2"])
        self.assertEqual([auth for _, auth in seen], ["Bearer tok123"] * 2)
        self.assertEqual(
            {k: v for k, v in seen[0][0].items() if k != "page"},
            {
                "hiddenFlag": "0",
                "isUpdated": "0",
                "mediaType": "1",
                "sortBy": "title",
                "system": "",
            },
        )

    async def test_missing_session_raises_relogin_advice(self) -> None:
        transport, _ = _listing_transport([{"products": [], "totalPages": 1}])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"LGOGDOWNLOADER_CONFIG_PATH": tmp}, clear=False),
            self.assertRaisesRegex(RuntimeError, "lgogdownloader --login"),
        ):
            await gog.fetch_gog_library_products(transport=transport)

    async def test_rejected_session_raises_relogin_advice(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(401, json={}))
        with (
            _gog_session_dir(),
            self.assertRaisesRegex(RuntimeError, "lgogdownloader --login"),
        ):
            await gog.fetch_gog_library_products(transport=transport)


class AccountListingSyncTests(ToolDBTestCase):
    """sync_gog over the account listing: identifier-first, rename-aware."""

    async def _run_sync(self, products: list[dict], *, resolve=None):
        transport, _ = _listing_transport(
            [{"page": 1, "totalPages": 1, "products": products}]
        )

        real_fetch = gog.fetch_gog_library_products

        async def _fetch():
            return await real_fetch(transport=transport)

        mock_resolve = resolve or AsyncMock(
            side_effect=AssertionError("a resolved row must not be re-resolved via IGDB")
        )
        with (
            _gog_session_dir(),
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("gamelib_mcp.data.gog.fetch_gog_library_products", _fetch),
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
        ):
            result = await gog.sync_gog()
        return result, mock_resolve

    @staticmethod
    def _minting_resolver() -> AsyncMock:
        async def _mint(name, igdb_platform_id, candidates, **kwargs):
            return await seed_game(name), None

        return AsyncMock(side_effect=_mint)

    async def test_new_product_records_gog_product_id(self) -> None:
        resolver = self._minting_resolver()
        result, mock_resolve = await self._run_sync(
            [_product(1207658930, "Legacy of Kain: Defiance", "legacy_of_kain_defiance")],
            resolve=resolver,
        )

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["listing_backend"], "account_api")
        self.assertNotIn("listing_error", result)
        # The fuzzy fallback must refuse a row already owning gog (anti-collapse).
        self.assertEqual(mock_resolve.await_args.kwargs.get("platform"), "gog")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ?", ("Legacy of Kain: Defiance",)
            )
        game_id = row["id"]
        self.assertEqual(
            await _identifiers_for_game(game_id),
            [(db_module.GOG_PRODUCT_ID, "1207658930")],
        )

    async def test_legacy_slug_row_is_adopted_and_renamed(self) -> None:
        # Exactly what the lgogdownloader backend used to store: the slug
        # title-cased, with no product id anywhere.
        game_id = await seed_game("Legacy Of Kain Defiance")
        await add_platform(game_id, "gog", from_source=True)

        result, mock_resolve = await self._run_sync(
            [_product(1207658930, "Legacy of Kain: Defiance", "legacy_of_kain_defiance")]
        )

        mock_resolve.assert_not_awaited()
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(await _game_name(game_id), "Legacy of Kain: Defiance")
        self.assertEqual(
            await _identifiers_for_game(game_id),
            [(db_module.GOG_PRODUCT_ID, "1207658930")],
        )

    async def test_slug_title_adoption_when_catalog_title_normalizes_differently(self) -> None:
        # "beyond_good_and_evil" title-cases to "Beyond Good And Evil", which
        # normalizes differently from the catalog's "Beyond Good & Evil" — only
        # the legacy-slug adoption attempt can find this row.
        game_id = await seed_game("Beyond Good And Evil")
        await add_platform(game_id, "gog", from_source=True)

        result, mock_resolve = await self._run_sync(
            [_product(1207658930, "Beyond Good & Evil", "beyond_good_and_evil")]
        )

        mock_resolve.assert_not_awaited()
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(await _game_name(game_id), "Beyond Good & Evil")
        self.assertEqual(
            await _identifiers_for_game(game_id),
            [(db_module.GOG_PRODUCT_ID, "1207658930")],
        )

    async def test_gog_only_row_takes_the_catalog_title(self) -> None:
        game_id = await seed_game("Legacy of Kain - Defiance")
        await add_platform(game_id, "gog", from_source=True)

        result, _ = await self._run_sync(
            [_product(1207658930, "Legacy of Kain: Defiance", "legacy_of_kain_defiance")]
        )

        self.assertEqual(result["renamed"], 1)
        self.assertEqual(await _game_name(game_id), "Legacy of Kain: Defiance")

    async def test_row_owning_another_platform_is_not_renamed(self) -> None:
        # The name of a multi-platform row answers to more than GOG's catalog,
        # and this row's stored name is not the legacy slug title either.
        game_id = await seed_game("Legacy of Kain - Defiance")
        await add_platform(game_id, "gog", from_source=True)
        await add_platform(game_id, "steam", from_source=True)

        result, _ = await self._run_sync(
            [_product(1207658930, "Legacy of Kain: Defiance", "legacy_of_kain_defiance")]
        )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(await _game_name(game_id), "Legacy of Kain - Defiance")
        self.assertEqual(
            await _identifiers_for_game(game_id),
            [(db_module.GOG_PRODUCT_ID, "1207658930")],
        )

    async def test_manual_name_override_is_never_renamed(self) -> None:
        game_id = await seed_game("Legacy Of Kain Defiance")
        await add_platform(game_id, "gog", from_source=True)
        # A hand-set spelling that still normalizes onto the catalog title, so
        # the row is found and only the override stops the rename.
        await db_module.apply_manual_game_fields(game_id, {"name": "Legacy of Kain - Defiance"})

        result, _ = await self._run_sync(
            [_product(1207658930, "Legacy of Kain: Defiance", "legacy_of_kain_defiance")]
        )

        self.assertEqual(result["renamed"], 0)
        self.assertEqual(await _game_name(game_id), "Legacy of Kain - Defiance")


class ListingFallbackTests(unittest.TestCase):
    def test_401_listing_falls_back_to_lgogdownloader(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"cyberpunk_2077\n", b""))
        transport = httpx.MockTransport(lambda request: httpx.Response(401, json={}))

        real_fetch = gog.fetch_gog_library_products

        async def _fetch():
            return await real_fetch(transport=transport)

        mock_resolve = AsyncMock(return_value=(42, None))
        with (
            _gog_session_dir(),
            patch("gamelib_mcp.data.gog.shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("gamelib_mcp.data.gog.fetch_gog_library_products", _fetch),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
            patch("gamelib_mcp.data.gog.resolve_and_link_game", mock_resolve),
            patch("gamelib_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
            patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
        ):
            result = asyncio.run(gog.sync_gog())

        self.assertEqual(result["listing_backend"], "lgogdownloader")
        self.assertIn("not authenticated", result["listing_error"])
        self.assertEqual(result["added"], 1)
        self.assertNotIn("renamed", result)
        mock_resolve.assert_awaited_once()
        # The CLI backend keeps its old call shape (no platform kwarg).
        self.assertEqual(
            mock_resolve.await_args.args[:2],
            ("Cyberpunk 2077", igdb.PLATFORM_TO_IGDB["gog"]),
        )
        self.assertEqual(mock_resolve.await_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()
