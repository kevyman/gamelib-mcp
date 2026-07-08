"""Tests for the acquisition tools (set/batch/spending stats)."""

from datetime import datetime, timezone

from fastmcp.exceptions import ToolError

from conftest import (
    ToolDBTestCase,
    add_identifier,
    add_platform,
    make_steam_game,
    seed_game,
)
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import acquisition


async def _acquisition_row(game_id: int, platform: str) -> dict:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            f"""SELECT {', '.join(db_module.ACQUISITION_FIELDS)}, playtime_minutes, owned
                FROM game_platforms WHERE game_id = ? AND platform = ?""",
            (game_id, platform),
        )
    return dict(row) if row is not None else {}


class SetAcquisitionTests(ToolDBTestCase):
    async def test_set_by_game_id_echo_and_db_state(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisition(
            game_id=gid,
            platform="steam",
            acquired_at="2024-06-15",
            price_paid=19.99,
            price_currency="eur",
            purchase_source="steam",
            bundle_name="Summer Sale Haul",
        )

        self.assertEqual(result["game_id"], gid)
        self.assertEqual(result["name"], "Hades")
        self.assertEqual(result["platform"], "steam")
        self.assertFalse(result["platform_row_created"])
        self.assertEqual(result["cleared"], [])
        self.assertEqual(
            result["acquisition"],
            {
                "acquired_at": "2024-06-15",
                "price_paid": 19.99,
                "price_currency": "EUR",  # uppercased on write
                "purchase_source": "steam",
                "bundle_name": "Summer Sale Haul",
            },
        )

        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["acquired_at"], "2024-06-15")
        self.assertEqual(row["price_paid"], 19.99)
        self.assertEqual(row["price_currency"], "EUR")

    async def test_fuzzy_name_resolution(self):
        gid = await seed_game("Hollow Knight")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisition(
            name="Hollow Knigt", platform="steam", price_paid=14.99
        )
        self.assertEqual(result["game_id"], gid)
        self.assertEqual(result["name"], "Hollow Knight")

    async def test_price_defaults_to_usd(self):
        gid = await seed_game("Celeste")
        await add_platform(gid, "steam")
        result = await acquisition.set_acquisition(
            game_id=gid, platform="steam", price_paid=9.99
        )
        self.assertEqual(result["acquisition"]["price_currency"], "USD")

    async def test_source_alias_normalization(self):
        gid = await seed_game("Aliased")
        await add_platform(gid, "steam")
        result = await acquisition.set_acquisition(
            game_id=gid, platform="steam", purchase_source="Humble Bundle"
        )
        self.assertEqual(result["acquisition"]["purchase_source"], "humble")

    async def test_partial_dates_accepted(self):
        gid = await seed_game("Dated")
        await add_platform(gid, "steam")
        for value in ("2023", "2023-07"):
            result = await acquisition.set_acquisition(
                game_id=gid, platform="steam", acquired_at=value
            )
            self.assertEqual(result["acquisition"]["acquired_at"], value)

    async def test_clear_resets_columns(self):
        gid = await seed_game("Cleared")
        await add_platform(gid, "steam")
        await acquisition.set_acquisition(
            game_id=gid, platform="steam", price_paid=5.0, purchase_source="gog"
        )

        result = await acquisition.set_acquisition(
            game_id=gid, platform="steam", clear=["price_paid", "price_currency"]
        )
        self.assertEqual(result["cleared"], ["price_paid", "price_currency"])
        self.assertIsNone(result["acquisition"]["price_paid"])
        self.assertIsNone(result["acquisition"]["price_currency"])
        # Untouched column survives the clear.
        self.assertEqual(result["acquisition"]["purchase_source"], "gog")

    async def test_set_and_clear_conflict(self):
        gid = await seed_game("Conflicted")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "set and clear"):
            await acquisition.set_acquisition(
                game_id=gid, platform="steam", price_paid=5.0, clear=["price_paid"]
            )

    async def test_unknown_clear_column(self):
        gid = await seed_game("BadClear")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "unknown column"):
            await acquisition.set_acquisition(
                game_id=gid, platform="steam", clear=["playtime_minutes"]
            )

    async def test_invalid_source(self):
        gid = await seed_game("BadSource")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "Unknown purchase_source"):
            await acquisition.set_acquisition(
                game_id=gid, platform="steam", purchase_source="the void"
            )

    async def test_invalid_dates(self):
        gid = await seed_game("BadDate")
        await add_platform(gid, "steam")
        for bad in ("junk", "24-06-01", "2024-13", "2024-02-30"):
            with self.assertRaises(ToolError):
                await acquisition.set_acquisition(
                    game_id=gid, platform="steam", acquired_at=bad
                )

    async def test_invalid_currency(self):
        gid = await seed_game("BadCurrency")
        await add_platform(gid, "steam")
        for bad in ("EU", "EUROS", "12$"):
            with self.assertRaisesRegex(ToolError, "3-letter"):
                await acquisition.set_acquisition(
                    game_id=gid, platform="steam", price_paid=1.0, price_currency=bad
                )

    async def test_negative_price(self):
        gid = await seed_game("Negative")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "negative"):
            await acquisition.set_acquisition(
                game_id=gid, platform="steam", price_paid=-1.0
            )

    async def test_currency_without_price(self):
        gid = await seed_game("CurrencyOnly")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "requires price_paid"):
            await acquisition.set_acquisition(
                game_id=gid, platform="steam", price_currency="USD"
            )

    async def test_auto_creates_platform_row(self):
        gid = await seed_game("Rowless")
        result = await acquisition.set_acquisition(
            game_id=gid, platform="gog", price_paid=7.5
        )
        self.assertTrue(result["platform_row_created"])
        row = await _acquisition_row(gid, "gog")
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["price_paid"], 7.5)

    async def test_create_platform_row_false_errors(self):
        gid = await seed_game("StrictRow")
        with self.assertRaisesRegex(ToolError, "add_game_to_platform"):
            await acquisition.set_acquisition(
                game_id=gid, platform="gog", price_paid=7.5, create_platform_row=False
            )

    async def test_unknown_game(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            await acquisition.set_acquisition(
                name="Definitely Not In The Library", platform="steam", price_paid=1.0
            )

    async def test_platform_required(self):
        gid = await seed_game("NoPlatform")
        with self.assertRaisesRegex(ToolError, "platform is required"):
            await acquisition.set_acquisition(game_id=gid, price_paid=1.0)

    async def test_requires_field_or_clear(self):
        gid = await seed_game("Empty")
        await add_platform(gid, "steam")
        with self.assertRaisesRegex(ToolError, "at least one"):
            await acquisition.set_acquisition(game_id=gid, platform="steam")


class SetAcquisitionsBatchTests(ToolDBTestCase):
    async def test_filled_vs_no_change_preserves_existing(self):
        gid = await seed_game("Importer")
        await add_platform(gid, "steam")
        await acquisition.set_acquisition(
            game_id=gid, platform="steam", price_paid=10.0
        )

        result = await acquisition.set_acquisitions_batch(
            [
                # price already set → COALESCE mode must not replace it.
                {"game_id": gid, "platform": "steam", "price_paid": 99.0},
                {"game_id": gid, "platform": "steam", "acquired_at": "2022"},
            ]
        )
        self.assertEqual(result["results"][0]["status"], "no_change")
        self.assertEqual(result["results"][1]["status"], "filled")
        self.assertEqual(result["filled"], 1)
        self.assertEqual(result["no_change"], 1)

        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["price_paid"], 10.0)  # preserved
        self.assertEqual(row["acquired_at"], "2022")

    async def test_overwrite_applies(self):
        gid = await seed_game("Overwriter")
        await add_platform(gid, "steam")
        await acquisition.set_acquisition(
            game_id=gid, platform="steam", price_paid=10.0
        )

        result = await acquisition.set_acquisitions_batch(
            [{"game_id": gid, "platform": "steam", "price_paid": 4.0}],
            overwrite=True,
        )
        self.assertEqual(result["results"][0]["status"], "applied")
        self.assertEqual(result["applied"], 1)
        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["price_paid"], 4.0)

    async def test_unmatched_echoes_item(self):
        item = {"name": "Zyxxlon Quest 9000", "platform": "steam", "price_paid": 1.0}
        result = await acquisition.set_acquisitions_batch([item])
        self.assertEqual(result["results"][0]["status"], "unmatched")
        self.assertEqual(result["unmatched"], [item])
        # Never creates games rows.
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(row["c"], 0)

    async def test_match_types(self):
        gid = await seed_game("Stardew Valley")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisitions_batch(
            [
                {"game_id": gid, "platform": "steam", "price_paid": 1.0},
                {"name": "Stardew Valley", "platform": "steam", "acquired_at": "2020"},
                {"name": "Stardew Vally", "platform": "steam", "purchase_source": "gog"},
            ]
        )
        types = [r["match_type"] for r in result["results"]]
        self.assertEqual(types, ["id", "name", "fuzzy"])
        for r in result["results"]:
            self.assertEqual(r["matched_name"], "Stardew Valley")
            self.assertEqual(r["game_id"], gid)

    async def test_no_platform_row_reports_actual_platforms(self):
        gid = await seed_game("Elsewhere")
        await add_platform(gid, "steam")

        item = {"game_id": gid, "platform": "gog", "price_paid": 3.0}
        result = await acquisition.set_acquisitions_batch([item])
        entry = result["results"][0]
        self.assertEqual(entry["status"], "no_platform_row")
        self.assertEqual(entry["platforms"], ["steam"])
        self.assertEqual(entry["item"], item)
        self.assertEqual(result["no_platform_row"], 1)

    async def test_create_platform_rows_true_creates(self):
        gid = await seed_game("Expandable")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{"game_id": gid, "platform": "gog", "price_paid": 3.0}],
            create_platform_rows=True,
        )
        self.assertEqual(result["results"][0]["status"], "filled")
        row = await _acquisition_row(gid, "gog")
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["price_paid"], 3.0)

    async def test_per_item_error_isolation(self):
        gid = await seed_game("Survivor")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisitions_batch(
            [
                {"game_id": gid, "platform": "steam", "purchase_source": "not a store"},
                {"game_id": gid, "platform": "atari2600", "price_paid": 1.0},
                {"game_id": gid, "platform": "steam"},  # no acquisition fields
                {"game_id": gid, "platform": "steam", "price_paid": 2.0},
            ]
        )
        statuses = [r["status"] for r in result["results"]]
        self.assertEqual(statuses, ["error", "error", "error", "filled"])
        self.assertEqual(result["errors"], 3)
        self.assertEqual(result["filled"], 1)
        for r in result["results"][:3]:
            self.assertIn("error", r)
            self.assertIn("item", r)
        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["price_paid"], 2.0)

    async def test_item_cap(self):
        items = [{"game_id": 1, "platform": "steam", "price_paid": 1.0}] * 201
        with self.assertRaisesRegex(ToolError, "capped at 200"):
            await acquisition.set_acquisitions_batch(items)

    async def test_identifier_match_beats_unmatchable_name(self):
        # "Localized title" scenario: the purchase export's name matches
        # nothing in the library, but the store identifier hits exactly.
        gid = await seed_game("Dragon Quest III HD-2D Remake")
        gpid = await add_platform(gid, "switch2")
        await add_identifier(gpid, "nintendo_title_id", "70010000012345")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Totally Different Localized Name",
                "platform": "switch2",
                "price_paid": 59.99,
                "identifier_type": "nintendo_title_id",
                "identifier_value": "70010000012345",
            }]
        )

        entry = result["results"][0]
        self.assertEqual(entry["status"], "filled")
        self.assertEqual(entry["match_type"], "identifier")
        self.assertEqual(entry["game_id"], gid)
        self.assertEqual(entry["matched_name"], "Dragon Quest III HD-2D Remake")
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["price_paid"], 59.99)

    async def test_identifier_keys_are_both_or_neither(self):
        gid = await seed_game("Paired")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisitions_batch(
            [
                {
                    "game_id": gid,
                    "platform": "steam",
                    "price_paid": 1.0,
                    "identifier_type": "steam_appid",
                },
                {
                    "game_id": gid,
                    "platform": "steam",
                    "price_paid": 1.0,
                    "identifier_value": "440",
                },
            ]
        )

        self.assertEqual(result["errors"], 2)
        for entry in result["results"]:
            self.assertEqual(entry["status"], "error")
            self.assertIn("together", entry["error"])
        row = await _acquisition_row(gid, "steam")
        self.assertIsNone(row["price_paid"])

    async def test_identifier_miss_falls_through_to_name(self):
        # A first-time import may predate the sync that attaches the id — the
        # unknown identifier must not be terminal when the name still matches.
        gid = await seed_game("Celeste")
        await add_platform(gid, "switch2")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Celeste",
                "platform": "switch2",
                "price_paid": 4.99,
                "identifier_type": "nintendo_title_id",
                "identifier_value": "700100000never",
            }]
        )

        entry = result["results"][0]
        self.assertEqual(entry["status"], "filled")
        self.assertEqual(entry["match_type"], "name")
        self.assertEqual(entry["game_id"], gid)

    async def test_create_platform_rows_attaches_carried_identifier(self):
        gid = await seed_game("Expandable Deluxe")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Expandable Deluxe",
                "platform": "gog",
                "price_paid": 3.0,
                "identifier_type": "gog_product_id",
                "identifier_value": "1207658924",
            }],
            create_platform_rows=True,
        )

        self.assertEqual(result["results"][0]["status"], "filled")
        self.assertEqual(result["results"][0]["match_type"], "name")
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                """SELECT gpi.identifier_value
                   FROM game_platform_identifiers gpi
                   JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                   WHERE gp.game_id = ? AND gp.platform = 'gog'
                     AND gpi.identifier_type = 'gog_product_id'""",
                (gid,),
            )
        self.assertIsNotNone(row)
        self.assertEqual(row["identifier_value"], "1207658924")


class SyncNoClobberTests(ToolDBTestCase):
    async def test_platform_syncs_never_touch_acquisition_columns(self):
        gid = await make_steam_game("Clobber Bait", 4242, playtime_minutes=30)
        await acquisition.set_acquisition(
            game_id=gid,
            platform="steam",
            acquired_at="2021-11-05",
            price_paid=29.99,
            price_currency="EUR",
            purchase_source="steam",
            bundle_name="Autumn Sale",
        )

        # Generic per-platform sync path.
        await db_module.upsert_game_platform(gid, "steam", playtime_minutes=60)
        # Bulk Steam library sync path for the same appid.
        await db_module.bulk_upsert_steam_library(
            [{"appid": 4242, "name": "Clobber Bait", "playtime_minutes": 120}],
            synced_at=datetime.now(timezone.utc).isoformat(),
        )

        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["acquired_at"], "2021-11-05")
        self.assertEqual(row["price_paid"], 29.99)
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["purchase_source"], "steam")
        self.assertEqual(row["bundle_name"], "Autumn Sale")
        self.assertEqual(row["playtime_minutes"], 120)


class SpendingStatsTests(ToolDBTestCase):
    async def _seed_library(self) -> dict[str, int]:
        """Two currencies, a 3-game split-price bundle, a 0.0 gift, a
        priced-unplayed, a priced-NULL-playtime, and an unpriced-played row."""
        ids = {}

        ids["bundle_played"] = await seed_game("Bundle Played")
        await add_platform(ids["bundle_played"], "steam", playtime_minutes=600)
        ids["bundle_unplayed"] = await seed_game("Bundle Unplayed")
        await add_platform(ids["bundle_unplayed"], "steam", playtime_minutes=0)
        ids["bundle_nullplay"] = await seed_game("Bundle Nullplay")
        await add_platform(ids["bundle_nullplay"], "steam")

        for name, price in (
            ("Bundle Played", 3.0),
            ("Bundle Unplayed", 4.0),
            ("Bundle Nullplay", 5.0),
        ):
            await acquisition.set_acquisition(
                name=name,
                platform="steam",
                acquired_at="2023-05-10",
                price_paid=price,
                purchase_source="fanatical",
                bundle_name="Fanatical Trio",
            )

        ids["gift"] = await seed_game("Gifted Gem")
        await add_platform(ids["gift"], "steam", playtime_minutes=300)
        await acquisition.set_acquisition(
            game_id=ids["gift"],
            platform="steam",
            acquired_at="2023-12-25",
            price_paid=0.0,
            purchase_source="gift",
        )

        ids["eur"] = await seed_game("Euro Buy")
        await add_platform(ids["eur"], "gog", playtime_minutes=60)
        await acquisition.set_acquisition(
            game_id=ids["eur"],
            platform="gog",
            acquired_at="2024-03-01",
            price_paid=50.0,
            price_currency="EUR",
            purchase_source="gog",
        )

        ids["unpriced"] = await seed_game("Mystery Cost")
        await add_platform(ids["unpriced"], "steam", playtime_minutes=100)
        return ids

    async def test_summary_totals_and_breakdowns(self):
        await self._seed_library()
        stats = await acquisition.get_spending_stats()

        self.assertEqual(stats["owned_rows"], 6)
        self.assertEqual(stats["priced_rows"], 5)
        self.assertEqual(stats["coverage_pct"], 83.3)
        self.assertEqual(stats["zero_cost_rows"], 1)

        totals = {t["currency"]: t for t in stats["totals"]}
        self.assertEqual(totals["USD"]["total_spent"], 12.0)
        self.assertEqual(totals["USD"]["priced_rows"], 4)
        self.assertEqual(totals["EUR"]["total_spent"], 50.0)
        self.assertEqual(totals["EUR"]["priced_rows"], 1)

        by_year = {(r["year"], r["currency"]): r for r in stats["by_year"]}
        self.assertEqual(by_year[("2023", "USD")]["spent"], 12.0)
        self.assertEqual(by_year[("2023", "USD")]["count"], 4)
        self.assertEqual(by_year[("2024", "EUR")]["spent"], 50.0)

        by_source = {(r["purchase_source"], r["currency"]): r for r in stats["by_source"]}
        self.assertEqual(by_source[("fanatical", "USD")]["spent"], 12.0)
        self.assertEqual(by_source[("fanatical", "USD")]["count"], 3)
        self.assertEqual(by_source[("gift", "USD")]["spent"], 0.0)
        self.assertEqual(by_source[("gog", "EUR")]["spent"], 50.0)

        by_platform = {(r["platform"], r["currency"]): r for r in stats["by_platform"]}
        self.assertEqual(by_platform[("steam", "USD")]["spent"], 12.0)
        self.assertEqual(by_platform[("steam", "USD")]["count"], 4)
        self.assertEqual(by_platform[("gog", "EUR")]["spent"], 50.0)

        self.assertEqual(len(stats["by_bundle"]), 1)
        bundle = stats["by_bundle"][0]
        self.assertEqual(bundle["bundle_name"], "Fanatical Trio")
        self.assertEqual(bundle["spent"], 12.0)
        self.assertEqual(bundle["count"], 3)

        expensive = [(r["name"], r["price_paid"]) for r in stats["top_expensive"]]
        self.assertEqual(
            expensive,
            [
                ("Euro Buy", 50.0),
                ("Bundle Nullplay", 5.0),
                ("Bundle Unplayed", 4.0),
                ("Bundle Played", 3.0),
                ("Gifted Gem", 0.0),
            ],
        )

    async def test_cost_per_hour(self):
        await self._seed_library()
        stats = await acquisition.get_spending_stats()
        cph = stats["cost_per_hour"]

        overall = {r["currency"]: r for r in cph["overall"]}
        # USD played+priced: Bundle Played (3.0, 10h) + Gifted Gem (0.0, 5h).
        self.assertEqual(overall["USD"]["total_spent"], 3.0)
        self.assertEqual(overall["USD"]["total_hours"], 15.0)
        self.assertEqual(overall["USD"]["cost_per_hour"], 0.2)
        self.assertEqual(overall["EUR"]["cost_per_hour"], 50.0)

        best = [(r["name"], r["cost_per_hour"]) for r in cph["best_value"]]
        self.assertEqual(
            best,
            [("Gifted Gem", 0.0), ("Bundle Played", 0.3), ("Euro Buy", 50.0)],
        )
        # Zero playtime and NULL playtime never appear in value rankings.
        names = {r["name"] for r in cph["best_value"]}
        self.assertNotIn("Bundle Unplayed", names)
        self.assertNotIn("Bundle Nullplay", names)

        worst = [(r["name"], r["cost_per_hour"]) for r in cph["worst_value"]]
        self.assertEqual(worst, [("Euro Buy", 50.0), ("Bundle Played", 0.3)])

        self.assertEqual(cph["unpriced_playtime_rows"], 1)

        unplayed = cph["unplayed_spend"]
        self.assertEqual(len(unplayed["totals"]), 1)
        self.assertEqual(unplayed["totals"][0]["currency"], "USD")
        self.assertEqual(unplayed["totals"][0]["spent"], 9.0)
        self.assertEqual(unplayed["totals"][0]["count"], 2)
        self.assertEqual(
            [(r["name"], r["price_paid"]) for r in unplayed["top"]],
            [("Bundle Nullplay", 5.0), ("Bundle Unplayed", 4.0)],
        )

    async def test_filters(self):
        await self._seed_library()

        by_year = await acquisition.get_spending_stats(year=2024)
        self.assertEqual(by_year["owned_rows"], 1)
        self.assertEqual(by_year["totals"][0]["currency"], "EUR")
        self.assertEqual(by_year["totals"][0]["total_spent"], 50.0)

        by_platform = await acquisition.get_spending_stats(platform="gog")
        self.assertEqual(by_platform["owned_rows"], 1)
        self.assertEqual(by_platform["totals"][0]["currency"], "EUR")

        by_source = await acquisition.get_spending_stats(purchase_source="Gifted")
        self.assertEqual(by_source["owned_rows"], 1)
        self.assertEqual(by_source["zero_cost_rows"], 1)
        self.assertEqual(by_source["totals"][0]["total_spent"], 0.0)

    async def test_empty_library(self):
        stats = await acquisition.get_spending_stats()
        self.assertEqual(stats["owned_rows"], 0)
        self.assertEqual(stats["coverage_pct"], 0.0)
        self.assertEqual(stats["totals"], [])
        self.assertEqual(stats["cost_per_hour"]["best_value"], [])
