"""Tests for gamelib_mcp.tools.checks — the check_library registry.

Mirrors the fixture patterns from tests/test_tools_admin.py (ToolDBTestCase +
temp SQLite) and tests/test_split_games.py /
tests/test_tools_admin.py::RevalidateIgdbMatchesTests for the network checks
(IGDB env vars + patched fetch functions; Steam session mocks for the license
audit). The underlying detector functions (detect_farmed_games,
detect_collapsed_games, etc.) already have their own characterization tests —
these tests exercise the checks.py adapter layer: envelope shape, selection
semantics, apply gating, suppressions, and error isolation.
"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_identifier,
    add_platform,
    make_steam_game,
    seed_game,
)
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_licenses, steam_session
from gamelib_mcp.tools import admin, checks

_IGDB_ENV = {"TWITCH_CLIENT_ID": "test-client", "TWITCH_CLIENT_SECRET": "test-secret"}


def _assert_envelope(test, finding):
    test.assertEqual(
        set(finding),
        {"check", "severity", "game_id", "name", "message", "evidence", "suggested_action"},
    )
    test.assertIn(finding["check"], checks.CHECKS)
    test.assertIn(finding["severity"], {"notice", "warning", "error"})
    test.assertIsInstance(finding["message"], str)
    test.assertIsInstance(finding["evidence"], dict)
    if finding["suggested_action"] is not None:
        test.assertIsInstance(finding["suggested_action"], dict)
        test.assertIn("tool", finding["suggested_action"])
        test.assertIn("args", finding["suggested_action"])


class CheckRegistryTests(ToolDBTestCase):
    async def test_ids_match_pattern_and_category(self):
        for check_id, spec in checks.CHECKS.items():
            with self.subTest(check_id=check_id):
                self.assertRegex(check_id, r"^[a-z_]+\.[a-z_]+$")
                self.assertEqual(spec.category, check_id.split(".", 1)[0])

    async def test_list_checks_returns_catalog_only(self):
        result = await checks.run_library_checks(list_checks=True)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["checks_run"], [])
        self.assertEqual(result["checks_skipped"], [])
        catalog_ids = {c["id"] for c in result["catalog"]}
        self.assertEqual(catalog_ids, set(checks.CHECKS))
        for entry in result["catalog"]:
            self.assertEqual(
                set(entry),
                {
                    "id",
                    "category",
                    "description",
                    "network",
                    "writes_on_apply",
                    "default_severity",
                    "options",
                },
            )


class SelectionSemanticsTests(ToolDBTestCase):
    async def test_default_run_is_offline_only(self):
        result = await checks.run_library_checks()
        offline_ids = {cid for cid, spec in checks.CHECKS.items() if spec.network is None}
        network_ids = {cid for cid, spec in checks.CHECKS.items() if spec.network is not None}
        self.assertEqual(set(result["checks_run"]), offline_ids)
        skipped_ids = {s["check"] for s in result["checks_skipped"]}
        self.assertEqual(skipped_ids, network_ids)
        for skip in result["checks_skipped"]:
            self.assertEqual(skip["reason"], "not_selected_network")

    async def test_default_run_makes_zero_db_writes(self):
        # Seed shapes that WOULD trigger writes if any check applied itself.
        epoch = 1700000000
        await make_steam_game("Card Farm A", 1, playtime_minutes=30, rtime_last_played=epoch)
        await make_steam_game("Card Farm B", 2, playtime_minutes=60, rtime_last_played=epoch)

        async def snapshot():
            async with db_module.get_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT id, is_farmed, content_type, parent_game_id, igdb_id "
                    "FROM games ORDER BY id"
                )
            return [tuple(r) for r in rows]

        before = await snapshot()
        await checks.run_library_checks(options={"playtime.farming": {"min_games_per_day": 2}})
        after = await snapshot()
        self.assertEqual(before, after)

    async def test_unknown_check_id_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(checks=["nonexistent.check"])

    async def test_category_prefix_selects_all_in_category(self):
        result = await checks.run_library_checks(checks=["identity"])
        identity_ids = {cid for cid in checks.CHECKS if checks.CHECKS[cid].category == "identity"}
        # Every id run belongs to the selected category...
        self.assertTrue(set(result["checks_run"]) <= identity_ids)
        # ...and identity.cross_store_collapse (network, named via its
        # category) is at least attempted — unconfigured (no IGDB creds) here,
        # so it shows up skipped rather than run.
        skipped_ids = {s["check"] for s in result["checks_skipped"]}
        self.assertTrue(identity_ids <= set(result["checks_run"]) | skipped_ids)
        self.assertIn(
            {"check": "identity.cross_store_collapse", "reason": "unconfigured:igdb"},
            result["checks_skipped"],
        )

    async def test_include_network_adds_network_checks_to_default_run(self):
        result = await checks.run_library_checks(include_network=True)
        self.assertEqual(set(result["checks_run"]) | {s["check"] for s in result["checks_skipped"]}, set(checks.CHECKS))
        # No credentials configured in the test environment, so every network
        # check is skipped (not run), and NEVER raises.
        for check_id, spec in checks.CHECKS.items():
            if spec.network is not None:
                self.assertNotIn(check_id, result["checks_run"])

    async def test_unconfigured_network_check_lands_in_checks_skipped(self):
        result = await checks.run_library_checks(checks=["extid.igdb_drift"])
        self.assertEqual(result["checks_run"], [])
        self.assertEqual(result["errors"], [])
        self.assertIn(
            {"check": "extid.igdb_drift", "reason": "unconfigured:igdb"}, result["checks_skipped"]
        )

    async def test_named_network_check_runs_without_include_network(self):
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch("gamelib_mcp.data.igdb.fetch_igdb_game_records", AsyncMock(return_value={})),
        ):
            result = await checks.run_library_checks(checks=["extid.igdb_drift"])
        self.assertEqual(result["checks_run"], ["extid.igdb_drift"])

    async def test_unknown_option_check_id_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(options={"nonexistent.check": {}})

    async def test_unknown_option_key_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(
                checks=["playtime.farming"], options={"playtime.farming": {"bogus_key": 1}}
            )

    async def test_apply_on_unknown_id_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(apply=["nonexistent.check"])

    async def test_apply_on_non_writer_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(
                checks=["identity.same_store_collapse"], apply=["identity.same_store_collapse"]
            )

    async def test_apply_without_selection_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(checks=["identity"], apply=["playtime.farming"])


class ErrorIsolationTests(ToolDBTestCase):
    async def test_one_check_raising_does_not_fail_others(self):
        game_id = await seed_game("Dead Space")
        async with db_module.get_db() as db:
            gpid_row = await db.execute(
                "INSERT INTO game_platforms (game_id, platform, owned) VALUES (?, 'steam', 1)",
                (game_id,),
            )
            gpid = gpid_row.lastrowid
            await db.commit()
        await add_identifier(gpid, db_module.STEAM_APP_ID, "17470")
        await add_identifier(gpid, db_module.STEAM_APP_ID, "1693980", is_primary=False)

        async def _boom(*, apply, options):
            raise RuntimeError("kaboom")

        broken = replace(checks.CHECKS["playtime.farming"], runner=_boom)
        with patch.dict(checks.CHECKS, {"playtime.farming": broken}):
            result = await checks.run_library_checks()

        self.assertEqual(result["errors"], [{"check": "playtime.farming", "error": "kaboom"}])
        self.assertNotIn("playtime.farming", result["checks_run"])
        self.assertIn("identity.same_store_collapse", result["checks_run"])
        self.assertEqual(result["summary"]["identity.same_store_collapse"]["findings"], 1)


class PlaytimeFarmingCheckTests(ToolDBTestCase):
    async def _seed_farming_day(self):
        epoch = 1700000000
        await make_steam_game("Card Farm A", 1, playtime_minutes=30, rtime_last_played=epoch)
        await make_steam_game("Card Farm B", 2, playtime_minutes=60, rtime_last_played=epoch)

    async def test_report_mode_finding_envelope_and_no_write(self):
        await self._seed_farming_day()
        result = await checks.run_library_checks(
            checks=["playtime.farming"], options={"playtime.farming": {"min_games_per_day": 2}}
        )
        self.assertEqual(result["checks_run"], ["playtime.farming"])
        self.assertGreater(len(result["findings"]), 0)
        for finding in result["findings"]:
            _assert_envelope(self, finding)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games WHERE is_farmed = 1")
        self.assertEqual(row["c"], 0)
        self.assertEqual(result["applied"], {})

    async def test_apply_marks_is_farmed(self):
        await self._seed_farming_day()
        result = await checks.run_library_checks(
            checks=["playtime.farming"],
            apply=["playtime.farming"],
            options={"playtime.farming": {"min_games_per_day": 2}},
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games WHERE is_farmed = 1")
        self.assertEqual(row["c"], 2)
        self.assertIn("playtime.farming", result["applied"])

    async def test_clean_library_reports_nothing(self):
        await make_steam_game("Solo Game", 1, playtime_minutes=600)
        result = await checks.run_library_checks(checks=["playtime.farming"])
        self.assertEqual(result["findings"], [])


class IdentitySameStoreCollapseTests(ToolDBTestCase):
    async def test_reports_and_suggests_split(self):
        game_id = await make_steam_game("Dead Space", 17470)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM game_platforms WHERE game_id = ? AND platform = 'steam'",
                (game_id,),
            )
        await add_identifier(row["id"], db_module.STEAM_APP_ID, "1693980", is_primary=False)

        result = await checks.run_library_checks(checks=["identity.same_store_collapse"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["game_id"], game_id)
        self.assertEqual(finding["suggested_action"]["tool"], "split_game")
        self.assertEqual(finding["suggested_action"]["args"]["source_game_id"], game_id)

    async def test_clean_library_reports_nothing(self):
        await make_steam_game("Dead Space", 17470)
        await make_steam_game("Portal", 400)
        result = await checks.run_library_checks(checks=["identity.same_store_collapse"])
        self.assertEqual(result["findings"], [])


async def _insert_duplicate_game(name: str) -> int:
    """Raw insert (seed_game would name-match onto the existing row)."""
    from gamelib_mcp.data.title_normalization import normalize_search_text

    async with db_module.get_db() as db:
        cursor = await db.execute(
            "INSERT INTO games (name, name_normalized) VALUES (?, ?)",
            (name, normalize_search_text(name)),
        )
        await db.commit()
        return cursor.lastrowid


class IdentityStrandedDuplicateTests(ToolDBTestCase):
    async def test_reports_and_suggests_merge(self):
        identified = await make_steam_game("Real English", 100)
        duplicate = await _insert_duplicate_game("Real English")
        await add_platform(duplicate, "steam", playtime_minutes=10)

        result = await checks.run_library_checks(checks=["identity.stranded_duplicate"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], duplicate)
        self.assertEqual(
            finding["suggested_action"],
            {
                "tool": "merge_games",
                "args": {"source_game_id": duplicate, "target_game_id": identified},
                "note": "the identifier-less duplicate merges into the identified row",
            },
        )


class OwnershipOrphanAndPhantomParentTests(ToolDBTestCase):
    async def test_true_orphan_reported_under_ownership_orphan_only(self):
        orphan = await seed_game("Dangling Game")
        result = await checks.run_library_checks(checks=["ownership.orphan", "nesting.phantom_parent"])
        orphan_findings = [f for f in result["findings"] if f["check"] == "ownership.orphan"]
        phantom_findings = [f for f in result["findings"] if f["check"] == "nesting.phantom_parent"]
        self.assertEqual([f["game_id"] for f in orphan_findings], [orphan])
        self.assertEqual(phantom_findings, [])
        _assert_envelope(self, orphan_findings[0])
        self.assertEqual(orphan_findings[0]["suggested_action"]["tool"], "delete_game")

    async def test_phantom_parent_with_owned_child_moves_to_superseded_base(self):
        # Phase B completion: a phantom parent WITH an owned child now reports
        # ONLY under nesting.superseded_base (with a concrete merge-to-heir
        # suggestion) — never under nesting.phantom_parent too.
        shell = await seed_game("Pathfinder: Wrath of the Righteous")
        edition = await seed_game(
            "Pathfinder: Wrath of the Righteous - Enhanced Edition",
            content_type="edition",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        await add_platform(edition, "steam", playtime_minutes=500)

        result = await checks.run_library_checks(
            checks=["nesting.phantom_parent", "nesting.superseded_base"]
        )
        phantom_findings = [f for f in result["findings"] if f["check"] == "nesting.phantom_parent"]
        superseded_findings = [
            f for f in result["findings"] if f["check"] == "nesting.superseded_base"
        ]
        self.assertEqual(phantom_findings, [])
        self.assertEqual(len(superseded_findings), 1)
        finding = superseded_findings[0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], shell)
        self.assertEqual(finding["evidence"]["heir_game_id"], edition)
        self.assertEqual(
            finding["suggested_action"],
            {
                "tool": "merge_games",
                "args": {"source_game_id": shell, "target_game_id": edition},
                "note": (
                    "owned edition becomes canonical primary; merge transfers "
                    "ratings/series/spend/wishlist and re-points siblings"
                ),
            },
        )

    async def test_phantom_parent_without_owned_child_stays_phantom_parent(self):
        # The vice-versa split: a phantom parent whose only child is UNOWNED
        # stays reported as nesting.phantom_parent and never appears in
        # nesting.superseded_base.
        shell = await seed_game("Shell Game")
        await seed_game(
            "Shell Game DLC",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        result = await checks.run_library_checks(
            checks=["nesting.phantom_parent", "nesting.superseded_base"]
        )
        phantom_findings = [f for f in result["findings"] if f["check"] == "nesting.phantom_parent"]
        superseded_findings = [
            f for f in result["findings"] if f["check"] == "nesting.superseded_base"
        ]
        self.assertEqual([f["game_id"] for f in phantom_findings], [shell])
        self.assertEqual(superseded_findings, [])

    async def test_selecting_only_one_id_still_runs_but_filters(self):
        await seed_game("Dangling Game")
        result = await checks.run_library_checks(checks=["ownership.orphan"])
        self.assertEqual(result["checks_run"], ["ownership.orphan"])
        self.assertTrue(all(f["check"] == "ownership.orphan" for f in result["findings"]))


class NestingMisclassifiedTests(ToolDBTestCase):
    async def test_offline_default_makes_no_network_call(self):
        await seed_game("Base Game")
        await seed_game(
            "Base Game: Story DLC", content_type="dlc", is_primary_library_item=0
        )
        with patch.object(admin, "_fetch_steam_appdetails", AsyncMock()) as fetch_mock:
            result = await checks.run_library_checks(checks=["nesting.misclassified"])
        fetch_mock.assert_not_awaited()
        self.assertEqual(result["summary"]["nesting.misclassified"]["probed"], 0)
        self.assertGreaterEqual(len(result["findings"]), 1)
        for finding in result["findings"]:
            _assert_envelope(self, finding)

    async def test_probe_steam_option_opts_into_network(self):
        base = await seed_game("Base Game")
        game = await make_steam_game("Mysterious Content", 555)
        payload = {"type": "dlc", "fullgame": {"appid": 999, "name": "Base Game"}}
        with patch.object(admin, "_fetch_steam_appdetails", AsyncMock(return_value=payload)):
            result = await checks.run_library_checks(
                checks=["nesting.misclassified"],
                options={"nesting.misclassified": {"probe_steam": True, "limit": 5}},
            )
        self.assertGreaterEqual(result["summary"]["nesting.misclassified"]["probed"], 1)
        mismatch = next(f for f in result["findings"] if f["game_id"] == game)
        self.assertEqual(mismatch["evidence"]["reason"], "steam_type_mismatch")
        self.assertEqual(
            mismatch["suggested_action"],
            {"tool": "update_game", "args": {"game_id": game, "content_type": "dlc", "parent_game_id": base}},
        )


class IdentityCrossStoreCollapseTests(ToolDBTestCase):
    async def _multi_platform_steam_game(self, name, appid, igdb_id) -> int:
        game_id = await make_steam_game(name, appid)
        await add_platform(game_id, "ps5")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = ? WHERE id = ?", (igdb_id, game_id))
            await db.commit()
        return game_id

    async def test_flags_mismatch_and_suggests_split(self):
        game_id = await self._multi_platform_steam_game("Dead Space", 17470, 999)
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value={"17470": 222}),
            ),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_names",
                AsyncMock(return_value={999: "Dead Space (2023)", 222: "Dead Space"}),
            ),
        ):
            result = await checks.run_library_checks(checks=["identity.cross_store_collapse"])
        self.assertEqual(result["checks_run"], ["identity.cross_store_collapse"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], game_id)
        self.assertEqual(finding["suggested_action"]["tool"], "split_game")


class ExtidIgdbDriftTests(ToolDBTestCase):
    @staticmethod
    def _record(name: str, **overrides) -> dict:
        record = {
            "name": name,
            "category": None,
            "game_type": None,
            "parent_igdb_id": None,
            "parent_name": None,
            "version_parent_igdb_id": None,
            "version_parent_name": None,
        }
        record.update(overrides)
        return record

    async def test_report_mode_finds_mismatch_without_writing(self):
        bad = await seed_game("PAYDAY 2")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 150511 WHERE id = ?", (bad,))
            await db.commit()

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_records",
                AsyncMock(return_value={150511: self._record("Payday 2 VR")}),
            ),
        ):
            result = await checks.run_library_checks(checks=["extid.igdb_drift"])

        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], bad)
        self.assertIsNotNone(finding["suggested_action"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT igdb_id FROM games WHERE id = ?", (bad,))
        self.assertEqual(row["igdb_id"], 150511)  # untouched

    async def test_apply_resets_igdb_link(self):
        bad = await seed_game("PAYDAY 2")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 150511 WHERE id = ?", (bad,))
            await db.commit()

        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_records",
                AsyncMock(return_value={150511: self._record("Payday 2 VR")}),
            ),
        ):
            result = await checks.run_library_checks(
                checks=["extid.igdb_drift"], apply=["extid.igdb_drift"]
            )

        self.assertIn("extid.igdb_drift", result["applied"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT igdb_id FROM games WHERE id = ?", (bad,))
        self.assertIsNone(row["igdb_id"])
        for finding in result["findings"]:
            self.assertIsNone(finding["suggested_action"])


class _LicenseGapRunnerMixin:
    """Shared license-audit patching for the license_gap check tests."""

    async def _run(self, owned, appdetails=None, steamspy=None, **kwargs):
        appdetails = appdetails or {}
        steamspy = steamspy or {}
        with (
            patch.object(steam_session, "is_steam_session_configured", return_value=True),
            patch.object(
                steam_licenses, "fetch_owned_steam_appids", AsyncMock(return_value=set(owned))
            ),
            patch.object(
                steam_licenses,
                "fetch_store_appdetails",
                AsyncMock(side_effect=lambda appid: appdetails.get(appid)),
            ),
            patch.object(
                steam_licenses,
                "fetch_steamspy_name",
                AsyncMock(side_effect=lambda appid: steamspy.get(appid)),
            ),
        ):
            return await checks.run_library_checks(**kwargs)


class OwnershipLicenseGapTests(_LicenseGapRunnerMixin, ToolDBTestCase):
    async def test_unconfigured_lands_in_checks_skipped(self):
        result = await checks.run_library_checks(checks=["ownership.license_gap"])
        self.assertIn(
            {"check": "ownership.license_gap", "reason": "unconfigured:steam_session"},
            result["checks_skipped"],
        )
        self.assertEqual(result["errors"], [])

    async def test_report_mode_lists_finding_and_writes_nothing(self):
        result = await self._run(
            owned={4000},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
            checks=["ownership.license_gap"],
        )
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["evidence"]["would_mint"], True)
        self.assertIsNotNone(finding["suggested_action"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(row["c"], 0)
        self.assertEqual(result["applied"], {})

    async def test_apply_mints_owned_row(self):
        result = await self._run(
            owned={4000},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
            checks=["ownership.license_gap"],
            apply=["ownership.license_gap"],
        )
        self.assertIn("ownership.license_gap", result["applied"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT g.name FROM game_platform_identifiers gpi "
                "JOIN game_platforms gp ON gp.id = gpi.game_platform_id "
                "JOIN games g ON g.id = gp.game_id "
                "WHERE gpi.identifier_type = 'steam_appid' AND gpi.identifier_value = '4000'"
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Garry's Mod")


class SuppressionTests(ToolDBTestCase):
    async def _seed_two_collapse_candidates(self):
        for name, appid, extra_appid in (("Dead Space", 17470, "1693980"), ("Portal", 400, "401")):
            game_id = await make_steam_game(name, appid)
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ? AND platform = 'steam'",
                    (game_id,),
                )
            await add_identifier(row["id"], db_module.STEAM_APP_ID, extra_appid, is_primary=False)

    async def test_suppress_filters_and_persists(self):
        await self._seed_two_collapse_candidates()
        first = await checks.run_library_checks(checks=["identity.same_store_collapse"])
        self.assertEqual(len(first["findings"]), 2)
        target = first["findings"][0]["game_id"]

        suppressed = await checks.run_library_checks(
            checks=["identity.same_store_collapse"],
            suppress=[{"check": "identity.same_store_collapse", "game_id": target}],
        )
        self.assertEqual(suppressed["suppressions_changed"], 1)
        self.assertEqual(suppressed["suppressed_count"], 1)
        self.assertEqual(len(suppressed["findings"]), 1)
        self.assertNotEqual(suppressed["findings"][0]["game_id"], target)

        # Persisted: a later call with no suppress/unsuppress args still filters.
        again = await checks.run_library_checks(checks=["identity.same_store_collapse"])
        self.assertEqual(len(again["findings"]), 1)
        self.assertEqual(again["suppressed_count"], 1)

    async def test_unsuppress_restores_and_is_idempotent(self):
        await self._seed_two_collapse_candidates()
        first = await checks.run_library_checks(checks=["identity.same_store_collapse"])
        target = first["findings"][0]["game_id"]
        await checks.run_library_checks(
            suppress=[{"check": "identity.same_store_collapse", "game_id": target}]
        )

        restored = await checks.run_library_checks(
            checks=["identity.same_store_collapse"],
            unsuppress=[{"check": "identity.same_store_collapse", "game_id": target}],
        )
        self.assertEqual(restored["suppressions_changed"], 1)
        self.assertEqual(len(restored["findings"]), 2)

        # Unsuppressing something not suppressed is a no-op.
        noop = await checks.run_library_checks(
            unsuppress=[{"check": "identity.same_store_collapse", "game_id": target}]
        )
        self.assertEqual(noop["suppressions_changed"], 0)

    async def test_suppress_unknown_check_id_raises(self):
        with self.assertRaises(ToolError):
            await checks.run_library_checks(suppress=[{"check": "nonexistent.check", "game_id": 1}])


class LimitPerCheckTests(ToolDBTestCase):
    async def _seed_two_orphans(self):
        await seed_game("Orphan One")
        await seed_game("Orphan Two")

    async def test_truncates_and_flags_summary(self):
        await self._seed_two_orphans()
        result = await checks.run_library_checks(checks=["ownership.orphan"], limit_per_check=1)
        self.assertEqual(len(result["findings"]), 1)
        self.assertTrue(result["summary"]["ownership.orphan"]["truncated"])

    async def test_zero_is_uncapped(self):
        await self._seed_two_orphans()
        result = await checks.run_library_checks(checks=["ownership.orphan"], limit_per_check=0)
        self.assertEqual(len(result["findings"]), 2)
        self.assertFalse(result["summary"]["ownership.orphan"]["truncated"])


# --- Phase B: checks 9-18 -----------------------------------------------------


def _pctl_day(app_id, day, minutes, name="Game", device="device-1"):
    return {
        "device_id": device,
        "application_id": app_id,
        "period_type": "day",
        "period_key": day,
        "playtime_minutes": minutes,
        "app_name": name,
    }


class NestingSupersededBaseTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        await make_steam_game("Ordinary Game", 8001)
        result = await checks.run_library_checks(checks=["nesting.superseded_base"])
        self.assertEqual(result["findings"], [])

    async def test_owned_edition_under_unowned_shell_suggests_merge(self):
        shell = await seed_game("Burnout Paradise")
        edition = await seed_game(
            "Burnout Paradise: The Ultimate Box",
            content_type="edition",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        await add_platform(edition, "steam", playtime_minutes=500)

        result = await checks.run_library_checks(checks=["nesting.superseded_base"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], shell)
        self.assertEqual(finding["evidence"]["heir_game_id"], edition)
        self.assertEqual(len(finding["evidence"]["children"]), 1)
        self.assertEqual(finding["evidence"]["children"][0]["playtime_minutes"], 500)
        self.assertEqual(finding["suggested_action"]["tool"], "merge_games")
        self.assertEqual(
            finding["suggested_action"]["args"],
            {"source_game_id": shell, "target_game_id": edition},
        )

    async def test_heir_picked_by_playtime_then_identifiers_then_id(self):
        shell = await seed_game("Multi Edition Shell")
        weak = await seed_game(
            "Multi Edition Shell: Weak Edition",
            content_type="edition",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        strong = await seed_game(
            "Multi Edition Shell: Strong Edition",
            content_type="edition",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        await add_platform(weak, "steam", playtime_minutes=10)
        await add_platform(strong, "steam", playtime_minutes=999)

        result = await checks.run_library_checks(checks=["nesting.superseded_base"])
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["evidence"]["heir_game_id"], strong)


class IdentityUnlinkedEditionTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        await make_steam_game("Dead Island", 91310)
        await make_steam_game("Portal", 400)
        result = await checks.run_library_checks(checks=["identity.unlinked_edition"])
        self.assertEqual(result["findings"], [])

    async def test_reports_unlinked_edition_sibling(self):
        base = await make_steam_game("Dead Island", 91310, playtime_minutes=120)
        edition = await make_steam_game(
            "Dead Island Definitive Edition", 91311, playtime_minutes=30
        )

        result = await checks.run_library_checks(checks=["identity.unlinked_edition"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], edition)
        self.assertEqual(finding["evidence"]["base_game_id"], base)
        self.assertIsNone(finding["suggested_action"])

    async def test_excludes_pairs_already_linked_as_parent_child(self):
        base = await make_steam_game("Dead Island", 91310)
        edition = await make_steam_game("Dead Island Definitive Edition", 91311)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET parent_game_id = ? WHERE id = ?", (base, edition)
            )
            await db.commit()

        result = await checks.run_library_checks(checks=["identity.unlinked_edition"])
        self.assertEqual(result["findings"], [])


class NestingDanglingParentTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        parent = await seed_game("Good Parent")
        await seed_game(
            "Good Parent DLC",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=parent,
        )
        result = await checks.run_library_checks(checks=["nesting.dangling_parent"])
        self.assertEqual(result["findings"], [])

    async def test_self_parent_reported(self):
        gid = await seed_game("Self Parent Game")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET parent_game_id = ? WHERE id = ?", (gid, gid))
            await db.commit()

        result = await checks.run_library_checks(checks=["nesting.dangling_parent"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["evidence"]["reason"], "self_parent")
        self.assertEqual(
            finding["suggested_action"]["args"], {"game_id": gid, "parent_game_id": 0}
        )

    async def test_parent_not_primary_reported(self):
        nested_parent = await seed_game(
            "Nested Parent", content_type="dlc", is_primary_library_item=0
        )
        child = await seed_game(
            "Child Of Nested",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=nested_parent,
        )

        result = await checks.run_library_checks(checks=["nesting.dangling_parent"])
        findings = {f["game_id"]: f for f in result["findings"]}
        self.assertIn(child, findings)
        self.assertEqual(findings[child]["evidence"]["reason"], "parent_not_primary")


class WishlistAlreadyOwnedTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        gid = await seed_game("Not Owned Yet")
        await db_module.upsert_wishlist_entry(gid, "steam", source="steam")
        result = await checks.run_library_checks(checks=["wishlist.already_owned"])
        self.assertEqual(result["findings"], [])

    async def test_reports_wishlist_row_already_owned(self):
        gid = await make_steam_game("Wishlisted But Owned", 5001)
        await db_module.upsert_wishlist_entry(gid, "steam", source="steam")

        result = await checks.run_library_checks(checks=["wishlist.already_owned"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], gid)
        self.assertEqual(finding["evidence"]["platform"], "steam")
        self.assertIsNone(finding["suggested_action"])


class PlaytimeSnapshotRegressionTests(ToolDBTestCase):
    async def _insert_snapshot(self, game_id, platform, date, minutes):
        async with db_module.get_db() as db:
            await db.execute(
                "INSERT INTO play_history (game_id, platform, snapshot_date, playtime_minutes) "
                "VALUES (?, ?, ?, ?)",
                (game_id, platform, date, minutes),
            )
            await db.commit()

    async def test_monotonic_history_reports_nothing(self):
        gid = await make_steam_game("Growing Game", 6002)
        await self._insert_snapshot(gid, "steam", "2026-01-01", 60)
        await self._insert_snapshot(gid, "steam", "2026-01-02", 100)
        result = await checks.run_library_checks(checks=["playtime.snapshot_regression"])
        self.assertEqual(result["findings"], [])

    async def test_reports_a_regression(self):
        gid = await make_steam_game("Regressed Game", 6001)
        await self._insert_snapshot(gid, "steam", "2026-01-01", 100)
        await self._insert_snapshot(gid, "steam", "2026-01-02", 60)

        result = await checks.run_library_checks(checks=["playtime.snapshot_regression"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["game_id"], gid)
        self.assertEqual(finding["evidence"]["prev_minutes"], 100)
        self.assertEqual(finding["evidence"]["next_minutes"], 60)
        self.assertIsNone(finding["suggested_action"])


class PlaytimeOrphanSwitchSummaryTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        result = await checks.run_library_checks(checks=["playtime.orphan_switch_summary"])
        self.assertEqual(result["findings"], [])

    async def test_matched_identifier_reports_nothing(self):
        gid = await seed_game("Mario Kart World")
        pid = await add_platform(gid, "switch2")
        await add_identifier(pid, "nintendo_title_id", "010067300059A000")
        await db_module.upsert_nintendo_play_summary(
            [_pctl_day("010067300059A000", "2026-07-01", 120)]
        )
        result = await checks.run_library_checks(checks=["playtime.orphan_switch_summary"])
        self.assertEqual(result["findings"], [])

    async def test_reports_unmatched_application_id(self):
        await db_module.upsert_nintendo_play_summary(
            [_pctl_day("010067300059A000", "2026-07-01", 120)]
        )
        result = await checks.run_library_checks(checks=["playtime.orphan_switch_summary"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["evidence"]["application_id"], "010067300059A000")
        self.assertEqual(finding["evidence"]["total_minutes"], 120)
        self.assertIsNone(finding["suggested_action"])

    async def test_manual_baseline_sentinel_excluded_even_when_unmatched(self):
        # set_switch2_playtime_baseline writes device_id='manual-baseline' rows
        # whose application_id already has an identifier by the time they're
        # written — but the exclusion is on device_id, not on match state, so
        # verify it holds even for an unmatched application_id.
        await db_module.upsert_nintendo_play_summary([
            {
                "device_id": db_module.NINTENDO_BASELINE_DEVICE_ID,
                "application_id": "0100000000000000",
                "period_type": "day",
                "period_key": db_module.NINTENDO_BASELINE_PERIOD_KEY,
                "playtime_minutes": 500,
                "app_name": "Unlinked Baseline",
            }
        ])
        result = await checks.run_library_checks(checks=["playtime.orphan_switch_summary"])
        self.assertEqual(result["findings"], [])


class SpendDuplicatePurchaseTests(ToolDBTestCase):
    async def _acquire(self, game_id, platform, **fields):
        pid = await add_platform(game_id, platform, playtime_minutes=0)
        await db_module.set_platform_acquisition(pid, fields)
        return pid

    async def test_different_family_and_name_not_flagged(self):
        a = await seed_game("Hades")
        b = await seed_game("Portal")
        await self._acquire(
            a, "steam", acquired_at="2026-01-01", price_paid=19.99,
            price_currency="USD", purchase_source="steam", bundle_name=None,
        )
        await self._acquire(
            b, "epic", acquired_at="2026-01-01", price_paid=19.99,
            price_currency="USD", purchase_source="steam", bundle_name=None,
        )
        result = await checks.run_library_checks(checks=["spend.duplicate_purchase"])
        self.assertEqual(result["findings"], [])

    async def test_reports_same_name_duplicate(self):
        a = await seed_game("Hades")
        b = await _insert_duplicate_game("Hades")
        await self._acquire(
            a, "steam", acquired_at="2026-01-01", price_paid=19.99,
            price_currency="USD", purchase_source="steam", bundle_name=None,
        )
        await self._acquire(
            b, "epic", acquired_at="2026-01-01", price_paid=19.99,
            price_currency="USD", purchase_source="steam", bundle_name=None,
        )

        result = await checks.run_library_checks(checks=["spend.duplicate_purchase"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertIsNone(finding["suggested_action"])


class SpendPriceAnomalyTests(ToolDBTestCase):
    async def test_clean_library_reports_nothing(self):
        a = await seed_game("Normal Purchase")
        pid_a = await add_platform(a, "steam")
        await db_module.set_platform_acquisition(
            pid_a,
            {"purchase_source": "steam", "price_paid": 19.99, "price_currency": "USD",
             "acquired_at": "2026-01-01"},
        )
        b = await seed_game("Second Normal Purchase")
        pid_b = await add_platform(b, "steam")
        await db_module.set_platform_acquisition(
            pid_b,
            {"purchase_source": "steam", "price_paid": 29.99, "price_currency": "USD",
             "acquired_at": "2026-01-02"},
        )
        result = await checks.run_library_checks(checks=["spend.price_anomaly"])
        self.assertEqual(result["findings"], [])

    async def test_reports_free_with_price_and_singleton_currency(self):
        a = await seed_game("Free Game With Price")
        pid_a = await add_platform(a, "steam")
        await db_module.set_platform_acquisition(
            pid_a,
            {"purchase_source": "free", "price_paid": 9.99, "price_currency": "USD",
             "acquired_at": "2026-01-01"},
        )
        b = await seed_game("Odd Currency Game")
        pid_b = await add_platform(b, "steam")
        await db_module.set_platform_acquisition(
            pid_b,
            {"purchase_source": "steam", "price_paid": 5.0, "price_currency": "XYZ",
             "acquired_at": "2026-01-01"},
        )

        result = await checks.run_library_checks(checks=["spend.price_anomaly"])
        kinds = {f["evidence"]["kind"] for f in result["findings"]}
        self.assertEqual(kinds, {"free_with_price", "singleton_currency"})
        for finding in result["findings"]:
            _assert_envelope(self, finding)
        free_finding = next(
            f for f in result["findings"] if f["evidence"]["kind"] == "free_with_price"
        )
        self.assertEqual(free_finding["suggested_action"]["tool"], "set_acquisition")
        currency_finding = next(
            f for f in result["findings"] if f["evidence"]["kind"] == "singleton_currency"
        )
        self.assertIsNone(currency_finding["suggested_action"])


class EnrichCoverageTests(ToolDBTestCase):
    async def test_fully_enriched_game_reports_nothing(self):
        gid = await make_steam_game("Fully Enriched", 7001, tags=["rpg"], hltb_main=10.0)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_id = ?, cover_image_id = ? WHERE id = ?",
                (912345, "co1abc", gid),
            )
            await db.commit()
        result = await checks.run_library_checks(checks=["enrich.coverage"])
        self.assertEqual(result["findings"], [])

    async def test_reports_missing_fields(self):
        gid = await seed_game("Barely Enriched")
        await add_platform(gid, "gog", playtime_minutes=120)

        result = await checks.run_library_checks(checks=["enrich.coverage"])
        fields = {f["evidence"]["field"] for f in result["findings"]}
        self.assertEqual(fields, {"tags", "igdb_id", "cover", "hltb_main"})
        for finding in result["findings"]:
            _assert_envelope(self, finding)
            self.assertEqual(finding["evidence"]["missing"], 1)
            self.assertEqual(finding["evidence"]["total"], 1)
            self.assertIsNone(finding["suggested_action"])
            self.assertEqual(finding["evidence"]["worst_offenders"][0]["game_id"], gid)


class SyncStalenessTests(ToolDBTestCase):
    async def test_recently_synced_no_playtime_reports_nothing(self):
        gid = await seed_game("Fresh Steam Game")
        await add_platform(gid, "steam", playtime_minutes=0)
        result = await checks.run_library_checks(checks=["sync.staleness"])
        self.assertEqual(result["findings"], [])

    async def test_reports_stale_platform(self):
        gid = await seed_game("Stale Steam Game")
        pid = await add_platform(gid, "steam", playtime_minutes=60)
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET last_synced = ? WHERE id = ?", (old, pid)
            )
            await db.commit()

        result = await checks.run_library_checks(checks=["sync.staleness"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["evidence"]["platform"], "steam")
        self.assertEqual(finding["suggested_action"]["tool"], "sync")

    async def test_custom_stale_days_option(self):
        gid = await seed_game("Barely Stale Steam Game")
        pid = await add_platform(gid, "steam", playtime_minutes=0)
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET last_synced = ? WHERE id = ?", (old, pid)
            )
            await db.commit()

        result = await checks.run_library_checks(
            checks=["sync.staleness"], options={"sync.staleness": {"stale_days": 1}}
        )
        self.assertEqual(len(result["findings"]), 1)

    async def _insert_snapshot(self, game_id, platform, date, minutes):
        async with db_module.get_db() as db:
            await db.execute(
                "INSERT INTO play_history (game_id, platform, snapshot_date, playtime_minutes) "
                "VALUES (?, ?, ?, ?)",
                (game_id, platform, date, minutes),
            )
            await db.commit()

    async def test_recently_synced_with_no_snapshots_reports_gap(self):
        # Playtime with NO snapshot at all: the post-sync writer owed a first
        # snapshot and never wrote it — flagged.
        gid = await seed_game("Snapshot Gap Game")
        await add_platform(gid, "epic", playtime_minutes=120)

        result = await checks.run_library_checks(checks=["sync.staleness"])
        gap = [f for f in result["findings"] if f["evidence"].get("platform") == "epic"]
        self.assertEqual(len(gap), 1)
        self.assertIsNone(gap[0]["suggested_action"])
        self.assertEqual(gap[0]["evidence"]["divergent_games"], 1)
        example = gap[0]["evidence"]["examples"][0]
        self.assertEqual(example["current_minutes"], 120)
        self.assertIsNone(example["last_snapshot_minutes"])

    async def test_idle_library_with_old_equal_snapshot_is_healthy(self):
        # Snapshots write only on CHANGE — an old snapshot matching current
        # playtime is a healthy idle library, never a snapshot-writer failure.
        gid = await seed_game("Idle But Healthy Game")
        await add_platform(gid, "epic", playtime_minutes=120)
        await self._insert_snapshot(gid, "epic", "2026-01-01", 120)

        result = await checks.run_library_checks(checks=["sync.staleness"])
        self.assertEqual(result["findings"], [])

    async def test_playtime_ahead_of_latest_snapshot_reports_gap(self):
        gid = await seed_game("Diverged Snapshot Game")
        await add_platform(gid, "epic", playtime_minutes=120)
        await self._insert_snapshot(gid, "epic", "2026-01-01", 100)

        result = await checks.run_library_checks(checks=["sync.staleness"])
        gap = [f for f in result["findings"] if f["evidence"].get("platform") == "epic"]
        self.assertEqual(len(gap), 1)
        example = gap[0]["evidence"]["examples"][0]
        self.assertEqual(example["current_minutes"], 120)
        self.assertEqual(example["last_snapshot_minutes"], 100)

    async def test_switch2_exempt_from_snapshot_gap(self):
        gid = await seed_game("Switch2 No Snapshots Game")
        await add_platform(gid, "switch2", playtime_minutes=120)

        result = await checks.run_library_checks(checks=["sync.staleness"])
        self.assertEqual(result["findings"], [])


class BugfixRegressionTests(ToolDBTestCase):
    """Regressions from the 2026-07-25 check_library sweep report."""

    async def test_explicit_checks_are_not_widened_by_include_network(self):
        # BUG-7: naming one network check plus include_network used to run
        # every network check (and return 25 unwanted igdb_drift findings).
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch("gamelib_mcp.data.igdb.fetch_igdb_game_records", AsyncMock(return_value={})),
        ):
            result = await checks.run_library_checks(
                checks=["extid.igdb_drift"], include_network=True
            )
        self.assertEqual(result["checks_run"], ["extid.igdb_drift"])

    async def test_include_network_still_widens_the_default_run(self):
        result = await checks.run_library_checks(include_network=True)
        selected = set(result["checks_run"]) | {s["check"] for s in result["checks_skipped"]}
        self.assertEqual(selected, set(checks.CHECKS))

    async def test_dlc_child_does_not_make_the_parent_a_supersession(self):
        # BUG-5: owning a route DLC without Train Sim World is an ownership
        # state, not an edition supersession — merging would rename the base
        # row to the DLC's title and flatten its siblings.
        shell = await seed_game("Train Sim World 3")
        route = await seed_game(
            "Sand Patch Grade",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        await add_platform(route, "steam", playtime_minutes=300)
        await seed_game(
            "Bakerloo Line",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=shell,
        )

        result = await checks.run_library_checks(
            checks=["nesting.superseded_base", "ownership.dlc_without_base"]
        )
        superseded = [f for f in result["findings"] if f["check"] == "nesting.superseded_base"]
        dlc_only = [f for f in result["findings"] if f["check"] == "ownership.dlc_without_base"]
        self.assertEqual(superseded, [])
        self.assertEqual([f["game_id"] for f in dlc_only], [shell])
        _assert_envelope(self, dlc_only[0])
        self.assertIsNone(dlc_only[0]["suggested_action"])
        self.assertEqual(
            [c["game_id"] for c in dlc_only[0]["evidence"]["owned_children"]], [route]
        )

    async def test_edition_named_child_still_supersedes(self):
        # The heir test is content_type OR an edition-suffixed name: "Pinball
        # FX Classic" under "Pinball FX" is the same game, typed base_game.
        shell = await seed_game("Pinball FX")
        classic = await seed_game(
            "Pinball FX Classic",
            content_type="base_game",
            is_primary_library_item=0,
            parent_game_id=shell,
        )
        await add_platform(classic, "steam", playtime_minutes=45)

        result = await checks.run_library_checks(
            checks=["nesting.superseded_base", "ownership.dlc_without_base"]
        )
        superseded = [f for f in result["findings"] if f["check"] == "nesting.superseded_base"]
        dlc_only = [f for f in result["findings"] if f["check"] == "ownership.dlc_without_base"]
        self.assertEqual([f["evidence"]["heir_game_id"] for f in superseded], [classic])
        self.assertEqual(dlc_only, [])

    async def test_bundle_split_across_a_family_is_not_a_duplicate_purchase(self):
        # BUG-6: split_bundle_acquisition writes exactly this shape — a base
        # game and its DLC sharing one bundle line's per-item price.
        base = await seed_game("Killing Floor")
        dlc = await seed_game(
            "Killing Floor - Community Weapon Pack",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=base,
        )
        for game_id in (base, dlc):
            pid = await add_platform(game_id, "steam")
            await db_module.set_platform_acquisition(
                pid,
                {
                    "acquired_at": "2013-12-30",
                    "price_paid": 0.12,
                    "price_currency": "USD",
                    "purchase_source": "humble",
                    "bundle_name": "Humble Unreal Engine Bundle",
                },
            )

        result = await checks.run_library_checks(checks=["spend.duplicate_purchase"])
        self.assertEqual(result["findings"], [])

    async def test_family_rows_without_a_bundle_name_still_report(self):
        base = await seed_game("Magicka")
        dlc = await seed_game(
            "Magicka - Item Pack",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=base,
        )
        for game_id in (base, dlc):
            pid = await add_platform(game_id, "steam")
            await db_module.set_platform_acquisition(
                pid,
                {
                    "acquired_at": "2013-12-30",
                    "price_paid": 0.30,
                    "price_currency": "USD",
                    "purchase_source": "humble",
                },
            )

        result = await checks.run_library_checks(checks=["spend.duplicate_purchase"])
        self.assertEqual(len(result["findings"]), 1)

    async def test_edition_suffix_link_is_not_reported_as_drift(self):
        # BUG-4: "Nioh 2 - The Complete Edition" → IGDB "Nioh 2" is correct.
        good = await seed_game("Nioh 2 - The Complete Edition")
        bad = await seed_game("A Hat in Time")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 111 WHERE id = ?", (good,))
            await db.execute("UPDATE games SET igdb_id = 222 WHERE id = ?", (bad,))
            await db.commit()

        records = {
            111: ExtidIgdbDriftTests._record("Nioh 2"),
            222: ExtidIgdbDriftTests._record("Among Us 3D: VR"),
        }
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_records",
                AsyncMock(return_value=records),
            ),
        ):
            result = await checks.run_library_checks(
                checks=["extid.igdb_drift"], apply=["extid.igdb_drift"]
            )

        self.assertEqual([f["game_id"] for f in result["findings"]], [bad])
        self.assertEqual(
            result["findings"][0]["evidence"]["drift_kind"], "wrong_entity"
        )
        summary = result["summary"]["extid.igdb_drift"]
        self.assertEqual(summary["edition_suffix_count"], 1)
        self.assertEqual(summary["edition_suffix_examples"][0]["game_id"], good)
        async with db_module.get_db() as db:
            kept = await db.execute_fetchone(
                "SELECT igdb_id FROM games WHERE id = ?", (good,)
            )
            reset = await db.execute_fetchone(
                "SELECT igdb_id FROM games WHERE id = ?", (bad,)
            )
        self.assertEqual(kept["igdb_id"], 111)  # good enrichment kept
        self.assertIsNone(reset["igdb_id"])

    async def test_include_edition_suffix_option_folds_them_back_in(self):
        good = await seed_game("Cities XL Platinum")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 333 WHERE id = ?", (good,))
            await db.commit()
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_records",
                AsyncMock(return_value={333: ExtidIgdbDriftTests._record("Cities XL")}),
            ),
        ):
            result = await checks.run_library_checks(
                checks=["extid.igdb_drift"],
                options={"extid.igdb_drift": {"include_edition_suffix": True}},
            )
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(
            result["findings"][0]["evidence"]["drift_kind"], "edition_suffix"
        )


class LicenseGapAppliedFindingTests(_LicenseGapRunnerMixin, ToolDBTestCase):
    async def test_applied_findings_report_the_minted_game_id(self):
        # BUG-8: after an apply the finding used to still read "is absent from
        # the library" with would_mint=false — indistinguishable from a refusal.
        result = await self._run(
            owned={4000},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
            checks=["ownership.license_gap"],
            apply=["ownership.license_gap"],
        )
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["severity"], "notice")
        self.assertTrue(finding["evidence"]["minted"])
        self.assertFalse(finding["evidence"]["would_mint"])
        self.assertFalse(finding["evidence"]["delisted"])
        self.assertIsNotNone(finding["game_id"])
        self.assertIn("has been minted", finding["message"])
        self.assertEqual(
            result["summary"]["ownership.license_gap"]["minted_game_ids"],
            [finding["game_id"]],
        )

    async def test_live_store_page_is_not_flagged_delisted(self):
        # BUG-1: absence from GetOwnedGames is not evidence of a delisting.
        await self._run(
            owned={4000},
            appdetails={4000: {"type": "game", "name": "Garry's Mod"}},
            checks=["ownership.license_gap"],
            apply=["ownership.license_gap"],
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT gp.delisted FROM game_platform_identifiers gpi "
                "JOIN game_platforms gp ON gp.id = gpi.game_platform_id "
                "WHERE gpi.identifier_type = 'steam_appid' "
                "AND gpi.identifier_value = '4000'"
            )
        self.assertEqual(row["delisted"], 0)

    async def test_retired_app_is_still_flagged_delisted(self):
        result = await self._run(
            owned={24740},
            appdetails={},
            steamspy={24740: "Burnout Paradise: The Ultimate Box"},
            checks=["ownership.license_gap"],
            apply=["ownership.license_gap"],
        )
        self.assertTrue(result["findings"][0]["evidence"]["delisted"])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT gp.delisted FROM game_platform_identifiers gpi "
                "JOIN game_platforms gp ON gp.id = gpi.game_platform_id "
                "WHERE gpi.identifier_type = 'steam_appid' "
                "AND gpi.identifier_value = '24740'"
            )
        self.assertEqual(row["delisted"], 1)


class StoreAuthoritativeDriftTests(ToolDBTestCase):
    """A link IGDB's own appid mapping produces is not drift (FTL loop)."""

    @staticmethod
    def _record(name: str) -> dict:
        return ExtidIgdbDriftTests._record(name)

    async def _seed(self, name: str, appid: int, igdb_id: int) -> int:
        game_id = await make_steam_game(name, appid)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = ? WHERE id = ?", (igdb_id, game_id))
            await db.commit()
        return game_id

    async def _run(self, external: dict[str, int], records: dict[int, dict], **kwargs):
        with (
            patch.dict(os.environ, _IGDB_ENV),
            patch(
                "gamelib_mcp.data.igdb.fetch_igdb_game_records",
                AsyncMock(return_value=records),
            ),
            patch(
                "gamelib_mcp.data.igdb.resolve_steam_appids_to_igdb",
                AsyncMock(return_value=external),
            ),
        ):
            return await checks.run_library_checks(checks=["extid.igdb_drift"], **kwargs)

    async def test_appid_backed_link_is_reported_as_notice_never_reset(self):
        # IGDB's mapping can point at a junk duplicate (prod: FTL), so the
        # finding stays visible — it just must never be reset, because the
        # next backfill would re-apply the identical link.
        game_id = await self._seed("FTL: Faster Than Light", 212680, 178437)
        result = await self._run(
            {"212680": 178437},
            {178437: self._record("Faster than light?")},
            apply=["extid.igdb_drift"],
        )
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["severity"], "notice")
        self.assertEqual(finding["evidence"]["drift_kind"], "store_authoritative")
        self.assertFalse(finding["evidence"]["reset"])
        self.assertEqual(finding["suggested_action"]["tool"], "update_game")
        summary = result["summary"]["extid.igdb_drift"]
        self.assertEqual(summary["store_authoritative_count"], 1)
        self.assertEqual(summary["reset_count"], 0)
        self.assertEqual(
            summary["store_authoritative_examples"][0]["drift_kind"],
            "store_authoritative",
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id FROM games WHERE id = ?", (game_id,)
            )
        self.assertEqual(row["igdb_id"], 178437)

    async def test_disagreeing_mapping_still_reports_and_resets(self):
        game_id = await self._seed("The Forest", 242760, 346813)
        result = await self._run(
            {"242760": 7830},
            {346813: self._record("Forest")},
            apply=["extid.igdb_drift"],
        )
        self.assertEqual([f["game_id"] for f in result["findings"]], [game_id])
        self.assertEqual(
            result["findings"][0]["evidence"]["drift_kind"], "wrong_entity"
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id, cover_image_id FROM games WHERE id = ?", (game_id,)
            )
        self.assertIsNone(row["igdb_id"])

    async def test_unmapped_appid_still_reports_as_wrong_entity(self):
        await self._seed("The Hex", 510420, 227064)
        result = await self._run({}, {227064: self._record("Hex")})
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "warning")
        self.assertEqual(
            result["findings"][0]["evidence"]["drift_kind"], "wrong_entity"
        )
        self.assertEqual(
            result["summary"]["extid.igdb_drift"]["store_authoritative_count"], 0
        )

    async def test_apply_clears_the_wrong_entitys_cover_art(self):
        game_id = await self._seed("The Gunk", 1087760, 404388)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET cover_image_id = 'cowrong' WHERE id = ?", (game_id,)
            )
            await db.commit()
        await self._run(
            {}, {404388: self._record("Gunk")}, apply=["extid.igdb_drift"]
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_id, cover_image_id FROM games WHERE id = ?", (game_id,)
            )
        self.assertIsNone(row["igdb_id"])
        self.assertIsNone(row["cover_image_id"])

    async def test_applied_findings_say_the_link_was_reset(self):
        await self._seed("The Operator", 226706001, 226706)
        result = await self._run(
            {}, {226706: self._record("Operator")}, apply=["extid.igdb_drift"]
        )
        finding = result["findings"][0]
        self.assertEqual(finding["severity"], "notice")
        self.assertTrue(finding["evidence"]["reset"])
        self.assertIn("link reset", finding["message"])
        self.assertIsNone(finding["suggested_action"])


class CompletionUnclassifiedTests(ToolDBTestCase):
    """completion.unclassified — the check that replaced suggest_completion_status.

    The heuristic itself is covered by tests/test_tools_completion.py; these
    guard the ADAPTER, which is the second place that has to stay in sync with
    the heuristic's return shape (the ADR 0003 risk that ADR 0004 inherits).
    """

    async def test_classified_library_reports_nothing(self):
        gid = await seed_game("Already Judged", hltb_main=10.0)
        await add_platform(gid, "steam", playtime_minutes=1200)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET completion_status = 'completed' WHERE id = ?", (gid,)
            )
            await db.commit()

        result = await checks.run_library_checks(checks=["completion.unclassified"])
        self.assertEqual(result["findings"], [])

    async def test_reports_completed_candidate_with_update_game_action(self):
        gid = await seed_game("Beaten Not Marked", hltb_main=10.0)
        await add_platform(gid, "steam", playtime_minutes=12 * 60)

        result = await checks.run_library_checks(checks=["completion.unclassified"])
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        _assert_envelope(self, finding)
        self.assertEqual(finding["check"], "completion.unclassified")
        self.assertEqual(finding["game_id"], gid)
        self.assertEqual(finding["severity"], "notice")
        self.assertEqual(finding["evidence"]["suggested_status"], "completed")
        self.assertEqual(finding["suggested_action"]["tool"], "update_game")
        self.assertEqual(
            finding["suggested_action"]["args"],
            {"game_id": gid, "completion_status": "completed"},
        )

    async def test_evergreen_candidate_keeps_its_status_in_the_action(self):
        gid = await seed_game("Endless Sandbox", hltb_main=10.0)
        await add_platform(gid, "steam", playtime_minutes=60 * 60)

        result = await checks.run_library_checks(checks=["completion.unclassified"])
        finding = result["findings"][0]
        self.assertEqual(finding["evidence"]["suggested_status"], "evergreen")
        self.assertEqual(finding["suggested_action"]["args"]["completion_status"], "evergreen")

    async def test_limit_option_caps_the_findings(self):
        for i in range(3):
            gid = await seed_game(f"Beaten {i}", hltb_main=10.0)
            await add_platform(gid, "steam", playtime_minutes=12 * 60)

        result = await checks.run_library_checks(
            checks=["completion.unclassified"], options={"completion.unclassified": {"limit": 2}}
        )
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["summary"]["completion.unclassified"]["limit"], 2)
        self.assertEqual(result["summary"]["completion.unclassified"]["suggested"], 2)

    async def test_check_never_writes_even_when_applied(self):
        gid = await seed_game("Beaten Not Marked", hltb_main=10.0)
        await add_platform(gid, "steam", playtime_minutes=12 * 60)

        # completion_status is user-set only: the check is permanently
        # report-only, so naming it in `apply` must be rejected outright.
        with self.assertRaises(ToolError):
            await checks.run_library_checks(
                checks=["completion.unclassified"], apply=["completion.unclassified"]
            )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT completion_status FROM games WHERE id = ?", (gid,)
            )
        self.assertIsNone(row["completion_status"])
