"""Tests for gamelib_mcp.tools.query (query_library/get_db_schema) and the
backing gamelib_mcp.data.db.readonly connection + schema.py views/query_log.
"""

import math
import re
import sqlite3

from conftest import ToolDBTestCase, add_identifier, add_platform, seed_game

from gamelib_mcp import main
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import readonly
from gamelib_mcp.tools import query as query_tool


def _nps_row(app_id: str, day: str, minutes: int) -> dict:
    return {
        "device_id": "device-1",
        "application_id": app_id,
        "period_type": "day",
        "period_key": day,
        "playtime_minutes": minutes,
        "app_name": None,
    }


class QueryToolTestCase(ToolDBTestCase):
    """Base case that also drops the per-loop read-only connection on teardown.

    Each test gets a fresh temp DB (ToolDBTestCase) and a fresh event loop
    (IsolatedAsyncioTestCase); without this the read-only singleton connection
    for a prior test's loop would only ever be garbage-collected, which trips
    aiosqlite's "deleted before being closed" ResourceWarning.
    """

    async def asyncTearDown(self) -> None:
        await readonly.close_readonly_connection()
        await super().asyncTearDown()


class QueryLibrarySelectTests(QueryToolTestCase):
    async def test_select_returns_columns_rows_row_count(self):
        await seed_game("Hades")
        await seed_game("Celeste")

        result = await query_tool.query_library("SELECT id, name FROM games ORDER BY id")

        self.assertEqual(result["columns"], ["id", "name"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0][1], "Hades")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["truncated_cells"], [])
        self.assertIn("elapsed_ms", result)
        self.assertIsInstance(result["elapsed_ms"], int)

    async def test_with_and_explain_allowed(self):
        await seed_game("Hades")

        with_result = await query_tool.query_library(
            "WITH g AS (SELECT * FROM games) SELECT COUNT(*) AS n FROM g"
        )
        self.assertNotIn("error", with_result)
        self.assertEqual(with_result["rows"][0][0], 1)

        explain_result = await query_tool.query_library("EXPLAIN SELECT * FROM games")
        self.assertNotIn("error", explain_result)

    async def test_values_allowed(self):
        result = await query_tool.query_library("VALUES (1, 'a'), (2, 'b')")
        self.assertNotIn("error", result)
        self.assertEqual(result["row_count"], 2)

    async def test_gl_ln_callable(self):
        result = await query_tool.query_library("SELECT gl_ln(10) AS x")
        self.assertNotIn("error", result)
        self.assertAlmostEqual(result["rows"][0][0], math.log(10))

    async def test_empty_sql_rejected(self):
        result = await query_tool.query_library("")
        self.assertIn("error", result)
        self.assertEqual(result["sql"], "")

        result = await query_tool.query_library("   ")
        self.assertIn("error", result)


class QueryLibraryWriteRefusalTests(QueryToolTestCase):
    """Every mutation/DDL/PRAGMA/ATTACH statement must come back as an error
    response (never raise), and the database must be provably unchanged."""

    async def _assert_refused_and_unchanged(self, sql: str) -> None:
        await seed_game("Untouched Game")
        async with db_module.get_db() as db:
            before = await db.execute_fetchall("SELECT id, name FROM games ORDER BY id")

        result = await query_tool.query_library(sql)

        self.assertIn("error", result, f"expected an error response for: {sql!r}")
        self.assertEqual(result["sql"], sql)

        async with db_module.get_db() as db:
            after = await db.execute_fetchall("SELECT id, name FROM games ORDER BY id")
        self.assertEqual(
            [tuple(r) for r in before],
            [tuple(r) for r in after],
            f"database changed after refused statement: {sql!r}",
        )

    async def test_insert_refused(self):
        await self._assert_refused_and_unchanged("INSERT INTO games (name) VALUES ('Nope')")

    async def test_update_refused(self):
        await self._assert_refused_and_unchanged("UPDATE games SET name = 'Nope'")

    async def test_delete_refused(self):
        await self._assert_refused_and_unchanged("DELETE FROM games")

    async def test_drop_table_refused(self):
        await self._assert_refused_and_unchanged("DROP TABLE games")

    async def test_create_table_refused(self):
        await self._assert_refused_and_unchanged("CREATE TABLE evil (id INTEGER)")

    async def test_pragma_refused(self):
        await self._assert_refused_and_unchanged("PRAGMA table_info(games)")

    async def test_attach_refused(self):
        await self._assert_refused_and_unchanged("ATTACH DATABASE ':memory:' AS aux")

    async def test_multi_statement_refused(self):
        await self._assert_refused_and_unchanged("SELECT 1; SELECT 2")


class ReadonlyAuthorizerUnitTests(QueryToolTestCase):
    """Direct unit coverage of the authorizer allow/deny table itself (not
    just the belt-check), since PRAGMA/ATTACH are also caught earlier by the
    first-keyword belt in the full query_library path above."""

    def test_authorizer_allows_read_actions(self):
        for action in (
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    readonly._authorizer(action, None, None, None, None), sqlite3.SQLITE_OK
                )

    def test_authorizer_denies_everything_else(self):
        for action in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_TRANSACTION,
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    readonly._authorizer(action, None, None, None, None), sqlite3.SQLITE_DENY
                )


class QueryLibraryTruncationTests(QueryToolTestCase):
    async def test_row_limit_truncation(self):
        for i in range(5):
            await seed_game(f"Game {i}")

        result = await query_tool.query_library("SELECT id FROM games ORDER BY id", row_limit=3)

        self.assertEqual(result["row_count"], 3)
        self.assertTrue(result["truncated"])

    async def test_row_limit_not_truncated_when_exact(self):
        for i in range(3):
            await seed_game(f"Game {i}")

        result = await query_tool.query_library("SELECT id FROM games ORDER BY id", row_limit=3)

        self.assertEqual(result["row_count"], 3)
        self.assertFalse(result["truncated"])

    async def test_row_limit_clamped_to_max(self):
        result = await query_tool.query_library("SELECT 1", row_limit=99999)
        self.assertNotIn("error", result)
        result = await query_tool.query_library("SELECT 1", row_limit=0)
        self.assertNotIn("error", result)

    async def test_cell_truncation(self):
        long_desc = "y" * 500
        await seed_game("Long Game", short_description=long_desc)

        result = await query_tool.query_library("SELECT short_description FROM games")

        cell = result["rows"][0][0]
        self.assertEqual(len(cell), query_tool.CELL_TRUNCATE_LENGTH + 1)  # + ellipsis char
        self.assertTrue(cell.endswith("…"))
        self.assertEqual(result["truncated_cells"], ["short_description"])

    async def test_short_cell_not_truncated(self):
        await seed_game("Short Game", short_description="short")
        result = await query_tool.query_library("SELECT short_description FROM games")
        self.assertEqual(result["rows"][0][0], "short")
        self.assertEqual(result["truncated_cells"], [])


class QueryLibraryResourceLimitTests(QueryToolTestCase):
    async def test_oversized_blob_refused_fast(self):
        # The progress handler can't interrupt a single VM opcode, so the
        # engine-level SQLITE_LIMIT_LENGTH cap must refuse the allocation
        # outright instead of building a 2 GB blob.
        result = await query_tool.query_library("SELECT length(randomblob(2000000000))")
        self.assertIn("error", result)
        self.assertIn("too big", result["error"])
        self.assertIn("1 MiB", result.get("hint", ""))

    async def test_small_blob_still_works(self):
        result = await query_tool.query_library("SELECT length(randomblob(1000)) AS n")
        self.assertNotIn("error", result)
        self.assertEqual(result["rows"][0][0], 1000)


class QueryLibraryErrorHintTests(QueryToolTestCase):
    async def test_no_such_table_hint(self):
        result = await query_tool.query_library("SELECT * FROM not_a_real_table")
        self.assertIn("error", result)
        self.assertIn("get_db_schema", result.get("hint", ""))

    async def test_denied_statement_hint(self):
        result = await query_tool.query_library("DELETE FROM games")
        self.assertIn("error", result)
        self.assertIn("read-only", result.get("hint", "").lower())


class ViewTests(QueryToolTestCase):
    async def test_views_exist_after_init_db(self):
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name"
            )
        self.assertEqual({r["name"] for r in rows}, {"v_game_playtime", "v_owned_games"})

    async def test_v_game_playtime_switch2_sums_nintendo_play_summary(self):
        game_id = await seed_game("Mario Kart World")
        # Stored game_platforms.playtime_minutes is deliberately stale/wrong
        # here to prove the view does NOT trust it for switch2.
        pid = await add_platform(game_id, "switch2", playtime_minutes=5, owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100AAA")
        await db_module.upsert_nintendo_play_summary(
            [
                _nps_row("0100AAA", "2026-07-01", 30),
                _nps_row("0100AAA", "2026-07-02", 15),
            ]
        )

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM v_game_playtime WHERE game_id = ? AND platform = 'switch2'",
                (game_id,),
            )

        self.assertEqual(row["playtime_minutes"], 45)

    async def test_v_game_playtime_switch2_matches_title_id_via_ingest_normalization(self):
        # VGCS can hand back a title id in a different case than Parental
        # Controls reports for the same title. Both write chokepoints
        # (upsert_game_platform_identifier, upsert_nintendo_play_summary)
        # normalize nintendo_title_id values to uppercase at ingest (see
        # data/db/__init__.py::normalize_identifier_value), so a lowercase
        # identifier and an uppercase summary row both land as the same
        # uppercase string — the view's plain-equality join (no UPPER() at
        # read time) still bridges them.
        game_id = await seed_game("Case Test Game")
        pid = await add_platform(game_id, "switch2", playtime_minutes=5, owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100abcdef")
        await db_module.upsert_nintendo_play_summary([_nps_row("0100ABCDEF", "2026-07-01", 42)])

        async with db_module.get_db() as db:
            stored_identifier = await db.execute_fetchone(
                "SELECT identifier_value FROM game_platform_identifiers WHERE game_platform_id = ?",
                (pid,),
            )
            stored_summary = await db.execute_fetchone(
                "SELECT application_id FROM nintendo_play_summary LIMIT 1"
            )
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM v_game_playtime WHERE game_id = ? AND platform = 'switch2'",
                (game_id,),
            )

        self.assertEqual(stored_identifier["identifier_value"], "0100ABCDEF")
        self.assertEqual(stored_summary["application_id"], "0100ABCDEF")
        self.assertEqual(row["playtime_minutes"], 42)

    async def test_v_game_playtime_switch2_pinned_keeps_gp_value(self):
        # A set_playtime pin on switch2 outranks the summary SUM everywhere
        # else in the codebase (upsert_game_platform's json_each guard), so
        # the view must honor it too.
        game_id = await seed_game("Pinned Switch Game")
        pid = await add_platform(game_id, "switch2", playtime_minutes=999, owned=1)
        await add_identifier(pid, "nintendo_title_id", "0100BBB")
        await db_module.upsert_nintendo_play_summary([_nps_row("0100BBB", "2026-07-01", 30)])
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET manual_overrides = ? WHERE id = ?",
                ('["playtime_minutes"]', pid),
            )
            await db.commit()

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM v_game_playtime WHERE game_id = ? AND platform = 'switch2'",
                (game_id,),
            )

        self.assertEqual(row["playtime_minutes"], 999)

    async def test_v_game_playtime_switch2_without_summaries_falls_back(self):
        # A switch2 row never seen by a PCTL sync (e.g. added manually with a
        # known playtime) must report the stored value, not NULL.
        game_id = await seed_game("Manual Switch Game")
        await add_platform(game_id, "switch2", playtime_minutes=77, owned=1)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM v_game_playtime WHERE game_id = ? AND platform = 'switch2'",
                (game_id,),
            )

        self.assertEqual(row["playtime_minutes"], 77)

    async def test_v_game_playtime_steam_uses_stored_column(self):
        game_id = await seed_game("Hades")
        await add_platform(game_id, "steam", playtime_minutes=120, owned=1)

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM v_game_playtime WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )

        self.assertEqual(row["playtime_minutes"], 120)

    async def test_v_owned_games_only_owned_rows(self):
        owned_id = await seed_game("Owned Game")
        await add_platform(owned_id, "steam", playtime_minutes=10, owned=1)
        stub_id = await seed_game("Wishlist Stub")
        await add_platform(stub_id, "steam", playtime_minutes=None, owned=0)

        async with db_module.get_db() as db:
            rows = await db.execute_fetchall("SELECT game_id FROM v_owned_games")

        game_ids = {r["game_id"] for r in rows}
        self.assertIn(owned_id, game_ids)
        self.assertNotIn(stub_id, game_ids)


class QueryLogTests(QueryToolTestCase):
    async def _log_rows(self) -> list:
        async with db_module.get_db() as db:
            return list(
                await db.execute_fetchall(
                    "SELECT sql, row_count, truncated, error FROM query_log ORDER BY id"
                )
            )

    async def test_success_writes_query_log_row(self):
        await seed_game("Hades")
        await query_tool.query_library("SELECT * FROM games")

        rows = await self._log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sql"], "SELECT * FROM games")
        self.assertEqual(rows[0]["row_count"], 1)
        self.assertIsNone(rows[0]["error"])

    async def test_error_writes_query_log_row(self):
        await query_tool.query_library("DROP TABLE games")

        rows = await self._log_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["error"])
        self.assertIsNone(rows[0]["row_count"])


class AnnotationSyncTests(QueryToolTestCase):
    """Strict two-way drift guard between TABLE_ANNOTATIONS and the live schema."""

    async def _live_table_view_names(self) -> set:
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                f"""SELECT name FROM sqlite_master
                    WHERE type IN ('table', 'view')
                      AND {query_tool._INTROSPECTION_EXCLUDE_SQL}"""
            )
        return {r["name"] for r in rows}

    async def test_every_live_table_is_annotated_and_vice_versa(self):
        live_names = await self._live_table_view_names()
        annotated_names = set(query_tool.TABLE_ANNOTATIONS)
        self.assertEqual(
            live_names,
            annotated_names,
            f"drift: live-only={live_names - annotated_names}, "
            f"annotation-only={annotated_names - live_names}",
        )

    async def test_every_annotated_column_exists(self):
        async with db_module.get_db() as db:
            for table, annotation in query_tool.TABLE_ANNOTATIONS.items():
                col_rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
                live_cols = {r["name"] for r in col_rows}
                for column in annotation.get("columns", {}):
                    with self.subTest(table=table, column=column):
                        self.assertIn(column, live_cols)


class ExampleQueryTests(QueryToolTestCase):
    async def test_example_queries_execute_without_error(self):
        # Seed enough data that joins/json_each/aggregates all have something
        # to operate on (an empty DB would trivially "pass" every example).
        hades_id = await seed_game("Hades", tags=["soulslike", "roguelike"])
        hades_pid = await add_platform(hades_id, "steam", playtime_minutes=120, owned=1)
        await db_module.set_platform_acquisition(
            hades_pid,
            {"price_paid": 19.99, "price_currency": "USD", "purchase_source": "steam"},
        )

        switch_id = await seed_game("Mario Kart World")
        switch_pid = await add_platform(switch_id, "switch2", playtime_minutes=0, owned=1)
        await add_identifier(switch_pid, "nintendo_title_id", "0100AAA")
        await db_module.upsert_nintendo_play_summary([_nps_row("0100AAA", "2026-07-01", 60)])

        schema = await query_tool.get_db_schema()
        self.assertGreater(len(schema["example_queries"]), 0)
        for example in schema["example_queries"]:
            with self.subTest(question=example["question"]):
                result = await query_tool.query_library(example["sql"])
                self.assertNotIn(
                    "error", result, f"{example['question']!r} failed: {result.get('error')}"
                )


class GetDbSchemaTests(QueryToolTestCase):
    async def test_enums_include_fixture_values(self):
        game_id = await seed_game("Hades", content_type="base_game")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET completion_status = 'completed' WHERE id = ?", (game_id,))
            await db.commit()
        pid = await add_platform(game_id, "steam", playtime_minutes=10, owned=1)
        await db_module.set_platform_acquisition(pid, {"purchase_source": "steam"})

        schema = await query_tool.get_db_schema()

        self.assertIn("steam", schema["enums"]["game_platforms.platform"])
        self.assertIn("base_game", schema["enums"]["games.content_type"])
        self.assertIn("completed", schema["enums"]["games.completion_status"])
        self.assertIn("steam", schema["enums"]["game_platforms.purchase_source"])

    async def test_tables_carry_annotations_and_columns(self):
        schema = await query_tool.get_db_schema()
        by_name = {t["name"]: t for t in schema["tables"]}

        self.assertIn("games", by_name)
        games_table = by_name["games"]
        self.assertIsNotNone(games_table["description"])
        col_names = {c["name"] for c in games_table["columns"]}
        self.assertIn("is_primary_library_item", col_names)
        is_primary_col = next(
            c for c in games_table["columns"] if c["name"] == "is_primary_library_item"
        )
        self.assertIsNotNone(is_primary_col["notes"])

        self.assertIn("v_owned_games", by_name)
        self.assertEqual(by_name["v_owned_games"]["type"], "view")

    async def test_fts_shadow_tables_excluded(self):
        schema = await query_tool.get_db_schema()
        names = {t["name"] for t in schema["tables"]}
        for excluded in ("games_fts", "games_fts_data", "games_fts_idx", "games_fts_docsize", "sqlite_sequence"):
            self.assertNotIn(excluded, names)

    async def test_foreign_keys_reported(self):
        schema = await query_tool.get_db_schema()
        by_name = {t["name"]: t for t in schema["tables"]}
        game_platforms_fks = by_name["game_platforms"]["foreign_keys"]
        self.assertTrue(any(fk["references"].startswith("games.") for fk in game_platforms_fks))

    async def test_guidance_present(self):
        schema = await query_tool.get_db_schema()
        self.assertGreater(len(schema["guidance"]), 0)


class DocstringInventorySyncTests(QueryToolTestCase):
    """query_library's main.py docstring hand-writes a table/view inventory
    for the model to skim — this must not silently drift from the live
    schema (renaming/adding/removing a table without updating it)."""

    async def test_docstring_tables_and_views_match_live_schema(self):
        tools = await main.mcp.list_tools()
        by_name = {t.name: t for t in tools}
        docstring = by_name["query_library"].description
        self.assertIsNotNone(docstring)

        tables_match = re.search(r"Tables:\s*(.+?)\.\s*\n", docstring, re.DOTALL)
        views_match = re.search(r"Views:\s*(.+?)\.\s*$", docstring, re.DOTALL)
        self.assertIsNotNone(tables_match, "docstring missing a 'Tables: ...' inventory line")
        self.assertIsNotNone(views_match, "docstring missing a 'Views: ...' inventory line")

        docstring_tables = {name.strip() for name in tables_match.group(1).replace("\n", " ").split(",")}
        docstring_views = {name.strip() for name in views_match.group(1).replace("\n", " ").split(",")}

        async with db_module.get_db() as db:
            live_tables = await db.execute_fetchall(
                f"""SELECT name FROM sqlite_master
                    WHERE type = 'table' AND {query_tool._INTROSPECTION_EXCLUDE_SQL}"""
            )
            live_views = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            )

        self.assertEqual(docstring_tables, {r["name"] for r in live_tables})
        self.assertEqual(docstring_views, {r["name"] for r in live_views})
