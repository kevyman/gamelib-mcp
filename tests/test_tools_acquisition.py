"""Tests for the acquisition tools (set/batch/spending stats)."""

import contextlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError

from conftest import (
    ToolDBTestCase,
    add_identifier,
    add_platform,
    make_steam_game,
    seed_game,
)
from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.purchases import PurchaseRecord
from gamelib_mcp.tools import acquisition

# Mirrors tests/test_purchase_importers.py's helper: patch every registered
# fetcher on tools.acquisition so no real fetch (HTTP/filesystem) ever runs.
_FETCHER_ATTRS = (
    "fetch_eshop_purchases",
    "fetch_gog_purchases",
    "fetch_humble_purchases",
    "fetch_steam_purchases",
)


@contextlib.contextmanager
def _patch_fetchers(**overrides):
    with contextlib.ExitStack() as stack:
        mocks = {}
        for attr in _FETCHER_ATTRS:
            mock = overrides.get(attr) or AsyncMock(return_value=([], []))
            stack.enter_context(patch.object(acquisition, attr, mock))
            mocks[attr] = mock
        yield mocks


async def _game_row(game_id: int) -> dict:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            "SELECT id, name, content_type, parent_game_id, is_primary_library_item "
            "FROM games WHERE id = ?",
            (game_id,),
        )
    return dict(row) if row is not None else {}


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

    async def test_edition_suffix_stripped_matches_base_game(self):
        # An eShop purchase title carries a platform/edition suffix the library
        # row never does; token-AND fails on the raw title until it is peeled.
        gid = await seed_game("DAVE THE DIVER")
        await add_platform(gid, "switch2")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "DAVE THE DIVER Nintendo Switch™ 2 Edition",
                "platform": "switch2",
                "price_paid": 19.99,
            }]
        )
        entry = result["results"][0]
        self.assertEqual(entry["status"], "filled")
        self.assertEqual(entry["game_id"], gid)
        self.assertEqual(entry["match_type"], "name")

    async def test_switch2_upgrade_pack_suffix_matches_base_game(self):
        gid = await seed_game("Hollow Knight")
        await add_platform(gid, "switch2")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Hollow Knight – Nintendo Switch 2 Edition-upgradepack",
                "platform": "switch2",
                "price_paid": 0.0,
            }]
        )
        self.assertEqual(result["results"][0]["game_id"], gid)

    async def test_distinct_edition_row_wins_before_stripping(self):
        # When the exact edition row exists, the raw title must match it rather
        # than collapsing onto a base-game row via the stripped fallback.
        base = await seed_game("LUMINES")
        await add_platform(base, "switch2")
        remaster = await seed_game("LUMINES REMASTERED")
        await add_platform(remaster, "switch2")

        result = await acquisition.set_acquisitions_batch(
            [{"name": "LUMINES REMASTERED", "platform": "switch2", "price_paid": 9.99}]
        )
        self.assertEqual(result["results"][0]["game_id"], remaster)

    async def test_bundle_name_does_not_false_match_single_game(self):
        # "Blasphemous" exists; a multi-game bundle title must NOT silently
        # attach its whole price to that one row — it stays unmatched.
        gid = await seed_game("Blasphemous")
        await add_platform(gid, "switch2")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Blasphemous + Blasphemous 2 Bundle",
                "platform": "switch2",
                "price_paid": 11.24,
            }]
        )
        self.assertEqual(result["results"][0]["status"], "unmatched")

    async def test_no_platform_row_details_carry_names(self):
        gid = await seed_game("Detailed")
        await add_platform(gid, "steam")

        item = {"game_id": gid, "platform": "gog", "price_paid": 3.0}
        result = await acquisition.set_acquisitions_batch([item])
        self.assertEqual(result["no_platform_row"], 1)
        details = result["no_platform_row_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["game_id"], gid)
        self.assertEqual(details[0]["matched_name"], "Detailed")
        self.assertEqual(details[0]["platform"], "gog")
        self.assertEqual(details[0]["platforms"], ["steam"])

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


class SplitBundleAcquisitionTests(ToolDBTestCase):
    async def test_even_split_across_existing_games_sums_to_total(self):
        p1 = await seed_game("Portal")
        await add_platform(p1, "switch2")
        p2 = await seed_game("Portal 2")
        await add_platform(p2, "switch2")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Portal: Companion Collection",
            platform="switch2",
            games=[{"game_id": p1}, {"game_id": p2}],
            total_price=4.75,
            price_currency="eur",
            acquired_at="2026-07-06",
            purchase_source="eshop",
        )
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["recorded"], 2)
        self.assertEqual(result["allocated_price"], 4.75)
        # 475 cents / 2 = 238 + 237 (leftover cent to the first game).
        prices = sorted(r["price_paid"] for r in result["games"])
        self.assertEqual(prices, [2.37, 2.38])
        row = await _acquisition_row(p1, "switch2")
        self.assertEqual(row["bundle_name"], "Portal: Companion Collection")
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["acquired_at"], "2026-07-06")

    async def test_explicit_prices_excluded_from_split(self):
        a = await seed_game("Alpha")
        await add_platform(a, "steam")
        b = await seed_game("Beta")
        await add_platform(b, "steam")
        c = await seed_game("Gamma")
        await add_platform(c, "steam")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Trio Pack",
            platform="steam",
            games=[
                {"game_id": a, "price_paid": 5.0},
                {"game_id": b},
                {"game_id": c},
            ],
            total_price=11.0,
        )
        by_id = {r["game_id"]: r["price_paid"] for r in result["games"]}
        self.assertEqual(by_id[a], 5.0)
        # Remaining 6.00 split evenly across Beta and Gamma.
        self.assertEqual(by_id[b], 3.0)
        self.assertEqual(by_id[c], 3.0)
        self.assertTrue(result["reconciled"])

    async def test_explicit_prices_exceeding_total_raise(self):
        a = await seed_game("Spendy")
        await add_platform(a, "steam")
        with self.assertRaisesRegex(ToolError, "exceed total_price"):
            await acquisition.split_bundle_acquisition(
                bundle_name="Overbudget",
                platform="steam",
                games=[{"game_id": a, "price_paid": 99.0}],
                total_price=10.0,
            )

    async def test_create_missing_makes_new_owned_game(self):
        existing = await seed_game("BioShock")
        await add_platform(existing, "switch2")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="BioShock: The Collection",
            platform="switch2",
            games=[
                {"name": "BioShock"},
                {"name": "BioShock 2"},  # not yet in library
            ],
            total_price=10.0,
            create_missing=True,
        )
        statuses = {r["status"] for r in result["games"]}
        self.assertEqual(result["created"], 1)
        self.assertIn("created", statuses)
        # The new game exists and is owned on switch2 with its bundle share.
        row = await _acquisition_row(
            next(r["game_id"] for r in result["games"] if r["status"] == "created"),
            "switch2",
        )
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["price_paid"], 5.0)
        self.assertEqual(row["bundle_name"], "BioShock: The Collection")

    async def test_unmatched_share_surfaces_as_unallocated(self):
        a = await seed_game("Known Game")
        await add_platform(a, "switch2")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Half Missing",
            platform="switch2",
            games=[{"name": "Known Game"}, {"name": "Nonexistent Zzz Title"}],
            total_price=10.0,
            create_missing=False,
        )
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["allocated_price"], 5.0)
        self.assertEqual(result["unallocated_price"], 5.0)
        # Still "reconciled": allocated + unallocated == total.
        self.assertTrue(result["reconciled"])

    async def test_creates_platform_row_for_matched_game(self):
        # Game exists but only on steam; bundle bought on switch2.
        gid = await seed_game("Cross Platform")
        await add_platform(gid, "steam")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Switch Bundle",
            platform="switch2",
            games=[{"game_id": gid}],
            total_price=8.0,
        )
        self.assertEqual(result["recorded"], 1)
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["owned"], 1)
        self.assertEqual(row["price_paid"], 8.0)

    async def test_fill_only_default_preserves_manual_price(self):
        gid = await seed_game("Preserved")
        await add_platform(gid, "switch2")
        await acquisition.set_acquisition(
            game_id=gid, platform="switch2", price_paid=1.0
        )

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Nope Bundle",
            platform="switch2",
            games=[{"game_id": gid}],
            total_price=8.0,
        )
        # bundle_name is newly filled, but the pre-existing price is untouched.
        self.assertEqual(result["games"][0]["status"], "filled")
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["price_paid"], 1.0)  # manual value preserved
        # Allocation reflects what PERSISTED (1.0), not the proposed 8.0 share,
        # so reconciled is false — the bundle total wasn't actually recorded.
        self.assertEqual(result["games"][0]["recorded_price"], 1.0)
        self.assertEqual(result["allocated_price"], 1.0)
        self.assertFalse(result["reconciled"])

        # overwrite=True re-attributes it and reconciles.
        result = await acquisition.split_bundle_acquisition(
            bundle_name="Nope Bundle",
            platform="switch2",
            games=[{"game_id": gid}],
            total_price=8.0,
            overwrite=True,
        )
        self.assertEqual(result["games"][0]["status"], "applied")
        self.assertEqual(result["allocated_price"], 8.0)
        self.assertTrue(result["reconciled"])
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["price_paid"], 8.0)

    async def test_dry_run_predicts_preserved_price_reconciliation(self):
        # dry_run must foresee that fill-only keeps the existing price, so its
        # allocated_price/reconciled match the real run (no surprise on apply).
        gid = await seed_game("Preserved")
        await add_platform(gid, "switch2")
        await acquisition.set_acquisition(
            game_id=gid, platform="switch2", price_paid=1.0
        )

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Nope Bundle",
            platform="switch2",
            games=[{"game_id": gid}],
            total_price=8.0,
            dry_run=True,
        )
        self.assertEqual(result["games"][0]["recorded_price"], 1.0)
        self.assertEqual(result["allocated_price"], 1.0)
        self.assertFalse(result["reconciled"])
        # Truly a preview — the price is still the manual 1.0.
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["price_paid"], 1.0)
        self.assertIsNone(row["bundle_name"])

    async def test_membership_without_prices(self):
        gid = await seed_game("Priceless")
        await add_platform(gid, "switch2")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Freebie Bundle",
            platform="switch2",
            games=[{"game_id": gid}],
        )
        self.assertTrue(result["reconciled"])
        self.assertIsNone(result["games"][0]["price_paid"])
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["bundle_name"], "Freebie Bundle")
        self.assertIsNone(row["price_paid"])

    async def test_rejects_unknown_game_key(self):
        with self.assertRaisesRegex(ToolError, "unknown key"):
            await acquisition.split_bundle_acquisition(
                bundle_name="Bad",
                platform="steam",
                games=[{"game_id": 1, "playtime_minutes": 5}],
            )

    async def test_dry_run_previews_without_writing(self):
        existing = await seed_game("Portal")
        await add_platform(existing, "switch2")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Portal: Companion Collection",
            platform="switch2",
            games=[{"name": "Portal"}, {"name": "Portal 2"}],
            total_price=4.75,
            price_currency="EUR",
            create_missing=True,
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        # Statuses predict the real run: Portal filled, Portal 2 created.
        statuses = [r["status"] for r in result["games"]]
        self.assertEqual(sorted(statuses), ["created", "filled"])
        self.assertEqual(result["recorded"], 2)
        self.assertEqual(result["allocated_price"], 4.75)

        # Nothing was written: no new game, no acquisition on Portal.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 1)
        row = await _acquisition_row(existing, "switch2")
        self.assertIsNone(row["bundle_name"])
        self.assertIsNone(row["price_paid"])

    async def test_dry_run_predicts_no_change_for_set_fields(self):
        gid = await seed_game("Already Priced")
        await add_platform(gid, "switch2")
        await acquisition.set_acquisition(
            game_id=gid,
            platform="switch2",
            price_paid=1.0,
            bundle_name="Old Bundle",
        )

        result = await acquisition.split_bundle_acquisition(
            bundle_name="New Bundle",
            platform="switch2",
            games=[{"game_id": gid}],
            total_price=8.0,
            dry_run=True,
        )
        self.assertEqual(result["games"][0]["status"], "no_change")
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["no_change"], 1)
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["bundle_name"], "Old Bundle")  # untouched

    async def test_explicit_prices_without_total_report_currency(self):
        gid = await seed_game("Solo Priced")
        await add_platform(gid, "switch2")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="Pinned Only",
            platform="switch2",
            games=[{"game_id": gid, "price_paid": 3.5}],
            price_currency="EUR",
        )
        # No total_price, but a price was written — the currency must be echoed.
        self.assertEqual(result["price_currency"], "EUR")
        row = await _acquisition_row(gid, "switch2")
        self.assertEqual(row["price_currency"], "EUR")


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
        self.assertEqual(stats["by_family"], [])


class NestedContentGuardTests(ToolDBTestCase):
    async def test_dlc_title_matches_base_without_content_type(self):
        # Baseline (pre-fix behavior the guard prevents): with no content_type
        # hint the DLC-ish title fuzzy-collapses onto the base game row.
        base = await seed_game("Hollow Knight")
        await add_platform(base, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{"name": "Hollow Knight: Pack", "platform": "steam", "price_paid": 5.0}]
        )
        entry = result["results"][0]
        self.assertEqual(entry["match_type"], "fuzzy")
        self.assertEqual(entry["game_id"], base)

    async def test_nested_dlc_guard_blocks_base_and_mints_with_parent(self):
        base = await seed_game("Hollow Knight")
        await add_platform(base, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Hollow Knight: Pack",
                "platform": "steam",
                "price_paid": 5.0,
                "content_type": "dlc",
            }],
            create_missing=True,
        )
        entry = result["results"][0]
        self.assertEqual(entry["status"], "created")
        self.assertNotEqual(entry["game_id"], base)
        self.assertEqual(entry["content_type"], "dlc")
        self.assertEqual(entry["parent_game_id"], base)
        self.assertEqual(entry["parent_name"], "Hollow Knight")

        detail = result["created_details"][0]
        self.assertEqual(detail["content_type"], "dlc")
        self.assertEqual(detail["parent_game_id"], base)
        self.assertEqual(detail["parent_name"], "Hollow Knight")

        row = await _game_row(entry["game_id"])
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["parent_game_id"], base)
        self.assertEqual(row["is_primary_library_item"], 0)

        # The base game row is untouched — no DLC spend attached to it.
        base_row = await _acquisition_row(base, "steam")
        self.assertIsNone(base_row["price_paid"])

    async def test_nested_dlc_unmatched_without_create_missing(self):
        base = await seed_game("Hollow Knight")
        await add_platform(base, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Hollow Knight: Pack",
                "platform": "steam",
                "price_paid": 5.0,
                "content_type": "dlc",
            }]
        )
        # Guard blocks the base match; create_missing off → unmatched, no mint.
        self.assertEqual(result["results"][0]["status"], "unmatched")
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 1)

    async def test_parent_resolution_skips_nested_candidates(self):
        # "Game: Expansion: Soundtrack" splits longest-first; the "Game:
        # Expansion" candidate is itself nested, so resolution must fall
        # through to the primary "Game" — update_game rejects nested parents
        # and nothing walks parent chains.
        base = await seed_game("Chained Game")
        await seed_game(
            "Chained Game: Expansion",
            content_type="expansion",
            parent_game_id=base,
            is_primary_library_item=0,
        )

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Chained Game: Expansion: Soundtrack",
                "platform": "steam",
                "price_paid": 3.0,
                "content_type": "unknown_addon",
            }],
            create_missing=True,
        )
        entry = result["results"][0]
        self.assertEqual(entry["status"], "created")
        self.assertEqual(entry["parent_game_id"], base)
        self.assertEqual(entry["parent_name"], "Chained Game")

    async def test_matched_default_row_is_reclassified_by_nested_hint(self):
        # A DLC purchase exact-matching a row still at the default
        # classification (a phantom minted before classification existed) must
        # reclassify it — otherwise the spend records but the row keeps
        # inflating game counts forever.
        base = await seed_game("Cyberpunk 2077")
        await add_platform(base, "steam")
        phantom = await seed_game("Cyberpunk 2077: Phantom Liberty")
        await add_platform(phantom, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Cyberpunk 2077: Phantom Liberty",
                "platform": "steam",
                "price_paid": 29.99,
                "content_type": "dlc",
            }]
        )
        entry = result["results"][0]
        self.assertEqual(entry["game_id"], phantom)
        self.assertEqual(entry["status"], "filled")
        self.assertTrue(entry["reclassified"])
        self.assertEqual(entry["content_type"], "dlc")
        self.assertEqual(entry["parent_game_id"], base)

        row = await _game_row(phantom)
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["parent_game_id"], base)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_matched_pinned_row_is_not_reclassified(self):
        from gamelib_mcp.data.db import apply_manual_game_fields

        pinned = await seed_game("Deliberate Game: DLC Sounding Name")
        await add_platform(pinned, "steam")
        # The user already decided this row is a real game — pin it.
        await apply_manual_game_fields(
            pinned, {"content_type": "base_game", "is_primary_library_item": 1}
        )

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Deliberate Game: DLC Sounding Name",
                "platform": "steam",
                "price_paid": 9.99,
                "content_type": "dlc",
            }]
        )
        entry = result["results"][0]
        self.assertEqual(entry["game_id"], pinned)
        self.assertNotIn("reclassified", entry)
        row = await _game_row(pinned)
        self.assertEqual(row["content_type"], "base_game")
        self.assertEqual(row["is_primary_library_item"], 1)

    async def test_matched_nested_row_keeps_curated_parent(self):
        # An already-nested match is left entirely alone: a split-title guess
        # must never clobber a curated parent link.
        real_parent = await seed_game("Real Parent")
        decoy = await seed_game("Decoy Prefix")
        child = await seed_game(
            "Decoy Prefix: The DLC",
            content_type="dlc",
            parent_game_id=real_parent,
            is_primary_library_item=0,
        )
        await add_platform(child, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Decoy Prefix: The DLC",
                "platform": "steam",
                "price_paid": 4.99,
                "content_type": "dlc",
            }]
        )
        entry = result["results"][0]
        self.assertEqual(entry["game_id"], child)
        self.assertNotIn("reclassified", entry)
        row = await _game_row(child)
        # The curated parent survives; the "Decoy Prefix" guess never applied.
        self.assertEqual(row["parent_game_id"], real_parent)
        self.assertNotEqual(row["parent_game_id"], decoy)

    async def test_edition_mint_never_adopts_existing_base_game(self):
        # "Hades: Deluxe Edition" strips to exactly "Hades" — upsert_game's
        # lower(name) adoption would seize the real base game and demote it
        # with the mint fields. Nested mints must create a NEW row under the
        # RAW storefront title and leave the base untouched.
        base = await seed_game("Hades")
        await add_platform(base, "steam")
        await acquisition.set_acquisition(
            game_id=base, platform="steam", price_paid=24.99
        )

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Hades: Deluxe Edition",
                "platform": "steam",
                "price_paid": 9.99,
                "content_type": "edition",
            }],
            create_missing=True,
        )
        entry = result["results"][0]
        self.assertEqual(entry["status"], "created")
        self.assertNotEqual(entry["game_id"], base)
        self.assertEqual(entry["matched_name"], "Hades: Deluxe Edition")
        self.assertEqual(entry["parent_game_id"], base)

        base_row = await _game_row(base)
        self.assertEqual(base_row["content_type"], "base_game")
        self.assertEqual(base_row["is_primary_library_item"], 1)
        self.assertIsNone(base_row["parent_game_id"])
        base_acq = await _acquisition_row(base, "steam")
        self.assertEqual(base_acq["price_paid"], 24.99)

        minted = await _game_row(entry["game_id"])
        self.assertEqual(minted["content_type"], "edition")
        self.assertEqual(minted["parent_game_id"], base)
        self.assertEqual(minted["is_primary_library_item"], 0)

    async def test_stripped_title_tier_disabled_for_nested_items(self):
        # The edition-stripped query ("X - Game of the Year Edition" -> "X")
        # rank-0 exact-matches the base game; for nested items that tier must
        # be skipped entirely or the edition's spend lands on the base row.
        base = await seed_game("The Witcher 3: Wild Hunt")
        await add_platform(base, "gog")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "The Witcher 3: Wild Hunt - Game of the Year Edition",
                "platform": "gog",
                "price_paid": 49.99,
                "content_type": "edition",
            }]
        )
        self.assertEqual(result["results"][0]["status"], "unmatched")
        base_acq = await _acquisition_row(base, "gog")
        self.assertIsNone(base_acq["price_paid"])

        # Without a content_type the stripped tier still reconciles base-game
        # purchases exactly as before (regression).
        result2 = await acquisition.set_acquisitions_batch(
            [{
                "name": "The Witcher 3: Wild Hunt - Game of the Year Edition",
                "platform": "gog",
                "price_paid": 49.99,
            }]
        )
        entry2 = result2["results"][0]
        self.assertEqual(entry2["game_id"], base)
        self.assertEqual(entry2["match_type"], "name")

    async def test_exact_name_still_matches_nested_row(self):
        cp = await seed_game("Cyberpunk 2077")
        await add_platform(cp, "steam")
        pl = await seed_game(
            "Phantom Liberty",
            content_type="dlc",
            parent_game_id=cp,
            is_primary_library_item=0,
        )
        await add_platform(pl, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Phantom Liberty",
                "platform": "steam",
                "price_paid": 29.99,
                "content_type": "dlc",
            }]
        )
        entry = result["results"][0]
        self.assertEqual(entry["status"], "filled")
        self.assertEqual(entry["match_type"], "name")
        self.assertEqual(entry["game_id"], pl)
        # No mint — exact tier resolved to the existing nested row.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 2)
        row = await _acquisition_row(pl, "steam")
        self.assertEqual(row["price_paid"], 29.99)

    async def test_primary_content_type_mints_as_base_game(self):
        # A primary/None content_type keeps today's behavior: base_game default.
        result = await acquisition.set_acquisitions_batch(
            [{
                "name": "Brand New Remake",
                "platform": "steam",
                "price_paid": 39.99,
                "content_type": "remake",
            }],
            create_missing=True,
        )
        entry = result["results"][0]
        self.assertEqual(entry["status"], "created")
        # No nested minting surfaced (mints as default base_game).
        self.assertNotIn("content_type", entry)
        row = await _game_row(entry["game_id"])
        self.assertEqual(row["content_type"], "base_game")
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)

    async def test_invalid_content_type_is_per_item_error(self):
        gid = await seed_game("Some Game")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisitions_batch(
            [{
                "game_id": gid,
                "platform": "steam",
                "price_paid": 1.0,
                "content_type": "nonsense",
            }]
        )
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("content_type", result["results"][0]["error"])


class SplitBundleNestedTests(ToolDBTestCase):
    async def test_nested_constituent_mints_nested_with_parent(self):
        base = await seed_game("The Witcher 3")
        await add_platform(base, "gog")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="The Witcher 3 GOTY",
            platform="gog",
            games=[
                {"name": "The Witcher 3"},
                {"name": "The Witcher 3: Blood and Wine", "content_type": "expansion"},
            ],
            total_price=10.0,
            create_missing=True,
        )
        created = [r for r in result["games"] if r["status"] == "created"]
        self.assertEqual(len(created), 1)
        entry = created[0]
        self.assertEqual(entry["content_type"], "expansion")
        self.assertEqual(entry["parent_game_id"], base)
        self.assertEqual(entry["parent_name"], "The Witcher 3")
        # Price split is unchanged by the guard: 10.0 across two → 5.0 each.
        self.assertEqual(entry["price_paid"], 5.0)

        row = await _game_row(entry["game_id"])
        self.assertEqual(row["content_type"], "expansion")
        self.assertEqual(row["parent_game_id"], base)
        self.assertEqual(row["is_primary_library_item"], 0)

    async def test_dry_run_created_surfaces_content_type_and_parent(self):
        base = await seed_game("The Witcher 3")
        await add_platform(base, "gog")

        result = await acquisition.split_bundle_acquisition(
            bundle_name="The Witcher 3 GOTY",
            platform="gog",
            games=[
                {"name": "The Witcher 3: Blood and Wine", "content_type": "expansion"},
            ],
            total_price=5.0,
            create_missing=True,
            dry_run=True,
        )
        entry = result["games"][0]
        self.assertEqual(entry["status"], "created")
        self.assertEqual(entry["content_type"], "expansion")
        self.assertEqual(entry["parent_game_id"], base)
        self.assertEqual(entry["parent_name"], "The Witcher 3")
        # Preview only — no new row written.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 1)


class ImportPurchasesDlcTests(ToolDBTestCase):
    def _dlc_record(self) -> PurchaseRecord:
        return PurchaseRecord(
            title="Cyberpunk 2077: Phantom Liberty",
            platform="switch2",
            purchase_source="eshop",
            acquired_at="2024-09-01",
            price_paid=29.99,
            price_currency="USD",
            content_type="dlc",
        )

    async def test_dlc_purchase_mints_nested_linked_to_parent(self):
        parent = await seed_game("Cyberpunk 2077")
        await add_platform(parent, "switch2")

        eshop = AsyncMock(return_value=([self._dlc_record()], []))
        with _patch_fetchers(fetch_eshop_purchases=eshop):
            result = await acquisition.import_purchases(sources=["eshop"])

        src = result["sources"]["eshop"]
        self.assertEqual(src["created"], 1)
        detail = src["created_details"][0]
        self.assertEqual(detail["content_type"], "dlc")
        self.assertEqual(detail["parent_game_id"], parent)
        self.assertEqual(detail["parent_name"], "Cyberpunk 2077")

        new_id = detail["game_id"]
        row = await _game_row(new_id)
        self.assertEqual(row["content_type"], "dlc")
        self.assertEqual(row["parent_game_id"], parent)
        # Absent from primary rollups — it is nested, not a phantom base game.
        self.assertEqual(row["is_primary_library_item"], 0)

        # The DLC spend still lands in totals and by_family.
        stats = await acquisition.get_spending_stats()
        totals = {t["currency"]: t["total_spent"] for t in stats["totals"]}
        self.assertEqual(totals["USD"], 29.99)
        fam = [f for f in stats["by_family"] if f["family_game_id"] == parent]
        self.assertEqual(len(fam), 1)
        self.assertEqual(fam[0]["family_name"], "Cyberpunk 2077")
        self.assertEqual(fam[0]["addon_spent"], 29.99)
        self.assertEqual(fam[0]["addon_count"], 1)

    async def test_dry_run_would_create_shows_content_type_and_parent(self):
        parent = await seed_game("Cyberpunk 2077")
        await add_platform(parent, "switch2")

        eshop = AsyncMock(return_value=([self._dlc_record()], []))
        with _patch_fetchers(fetch_eshop_purchases=eshop):
            result = await acquisition.import_purchases(
                sources=["eshop"], dry_run=True
            )

        would_create = result["sources"]["eshop"]["would_create"]
        self.assertEqual(len(would_create), 1)
        self.assertEqual(would_create[0]["content_type"], "dlc")
        self.assertEqual(would_create[0]["parent_game_id"], parent)
        self.assertEqual(would_create[0]["parent_name"], "Cyberpunk 2077")
        # Nothing written.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM games")
        self.assertEqual(count["c"], 1)


class SpendingByFamilyTests(ToolDBTestCase):
    async def _seed_families(self) -> dict[str, int]:
        ids: dict[str, int] = {}
        # Elden Ring family: base (USD, 10h playtime) + USD DLC + EUR DLC.
        ids["elden"] = await seed_game("Elden Ring")
        await add_platform(ids["elden"], "steam", playtime_minutes=600)
        await acquisition.set_acquisition(
            game_id=ids["elden"], platform="steam", price_paid=40.0
        )
        ids["erdtree"] = await seed_game(
            "Shadow of the Erdtree",
            content_type="dlc",
            parent_game_id=ids["elden"],
            is_primary_library_item=0,
        )
        await add_platform(ids["erdtree"], "steam")
        await acquisition.set_acquisition(
            game_id=ids["erdtree"], platform="steam", price_paid=30.0
        )
        ids["eur_dlc"] = await seed_game(
            "Elden Ring EUR Pack",
            content_type="dlc",
            parent_game_id=ids["elden"],
            is_primary_library_item=0,
        )
        await add_platform(ids["eur_dlc"], "gog")
        await acquisition.set_acquisition(
            game_id=ids["eur_dlc"],
            platform="gog",
            price_paid=10.0,
            price_currency="EUR",
        )

        # Witcher family (USD, lower total) — checks per-currency ordering.
        ids["witcher"] = await seed_game("The Witcher 3")
        await add_platform(ids["witcher"], "gog")
        await acquisition.set_acquisition(
            game_id=ids["witcher"], platform="gog", price_paid=20.0
        )
        ids["blood"] = await seed_game(
            "Blood and Wine",
            content_type="expansion",
            parent_game_id=ids["witcher"],
            is_primary_library_item=0,
        )
        await add_platform(ids["blood"], "gog")
        await acquisition.set_acquisition(
            game_id=ids["blood"], platform="gog", price_paid=5.0
        )

        # Unrelated standalone (no nested contributor) — excluded from by_family.
        ids["solo"] = await seed_game("Random Solo")
        await add_platform(ids["solo"], "steam")
        await acquisition.set_acquisition(
            game_id=ids["solo"], platform="steam", price_paid=15.0
        )

        # Orphan nested row (parent_game_id NULL, is_primary=0), priced —
        # excluded from by_family (a lone addon with no base is noise).
        ids["orphan"] = await seed_game(
            "Orphan Addon", content_type="dlc", is_primary_library_item=0
        )
        await add_platform(ids["orphan"], "steam")
        await acquisition.set_acquisition(
            game_id=ids["orphan"], platform="steam", price_paid=3.0
        )
        return ids

    async def test_by_family_grouping_and_edge_cases(self):
        ids = await self._seed_families()
        stats = await acquisition.get_spending_stats()
        families = stats["by_family"]

        by_key = {(f["family_game_id"], f["currency"]): f for f in families}

        # Elden USD: base 40 + addon 30 = 70, one addon, 10h playtime.
        elden_usd = by_key[(ids["elden"], "USD")]
        self.assertEqual(elden_usd["family_name"], "Elden Ring")
        self.assertEqual(elden_usd["base_spent"], 40.0)
        self.assertEqual(elden_usd["addon_spent"], 30.0)
        self.assertEqual(elden_usd["total_spent"], 70.0)
        self.assertEqual(elden_usd["addon_count"], 1)
        self.assertEqual(elden_usd["family_playtime_hours"], 10.0)
        self.assertEqual(elden_usd["family_cost_per_hour"], 7.0)

        # Elden EUR: the root has no EUR spend, only the EUR DLC (currency
        # isolation — never summed with USD).
        elden_eur = by_key[(ids["elden"], "EUR")]
        self.assertEqual(elden_eur["base_spent"], 0.0)
        self.assertEqual(elden_eur["addon_spent"], 10.0)
        self.assertEqual(elden_eur["total_spent"], 10.0)
        self.assertEqual(elden_eur["addon_count"], 1)
        # Playtime is the root game's, currency-independent → cost/hour 1.0.
        self.assertEqual(elden_eur["family_playtime_hours"], 10.0)
        self.assertEqual(elden_eur["family_cost_per_hour"], 1.0)

        # Witcher USD family present with no playtime → null cost/hour.
        witcher = by_key[(ids["witcher"], "USD")]
        self.assertEqual(witcher["total_spent"], 25.0)
        self.assertIsNone(witcher["family_playtime_hours"])
        self.assertIsNone(witcher["family_cost_per_hour"])

        # Standalone and orphan excluded.
        family_ids = {f["family_game_id"] for f in families}
        self.assertNotIn(ids["solo"], family_ids)
        self.assertNotIn(ids["orphan"], family_ids)

        # Per-currency ordering by total_spent DESC: Elden (70) before Witcher (25).
        usd_order = [
            f["family_game_id"] for f in families if f["currency"] == "USD"
        ]
        self.assertEqual(usd_order, [ids["elden"], ids["witcher"]])

    async def test_by_family_base_currency_row_survives_cross_currency_family(self):
        # Base bought in USD, its ONLY DLC bought in EUR: qualification is
        # decided across the whole family, so the USD row (base_spent=60,
        # addon_spent=0) must surface alongside the EUR row — a per-currency
        # HAVING would hide the base spend entirely.
        base = await seed_game("Cross Currency Base")
        await add_platform(base, "steam", playtime_minutes=120)
        await acquisition.set_acquisition(
            game_id=base, platform="steam", price_paid=60.0
        )
        dlc = await seed_game(
            "Cross Currency Base: DLC",
            content_type="dlc",
            parent_game_id=base,
            is_primary_library_item=0,
        )
        await add_platform(dlc, "gog")
        await acquisition.set_acquisition(
            game_id=dlc, platform="gog", price_paid=30.0, price_currency="EUR"
        )

        stats = await acquisition.get_spending_stats()
        by_key = {(f["family_game_id"], f["currency"]): f for f in stats["by_family"]}

        usd = by_key[(base, "USD")]
        self.assertEqual(usd["base_spent"], 60.0)
        self.assertEqual(usd["addon_spent"], 0.0)
        self.assertEqual(usd["total_spent"], 60.0)

        eur = by_key[(base, "EUR")]
        self.assertEqual(eur["base_spent"], 0.0)
        self.assertEqual(eur["addon_spent"], 30.0)

    async def test_by_family_respects_filters(self):
        ids = await self._seed_families()
        # Platform filter narrows spend rows (playtime stays root-wide).
        stats = await acquisition.get_spending_stats(platform="gog")
        by_key = {(f["family_game_id"], f["currency"]): f for f in stats["by_family"]}
        # Elden USD base is on steam (filtered out); only the EUR gog DLC remains.
        self.assertNotIn((ids["elden"], "USD"), by_key)
        self.assertIn((ids["elden"], "EUR"), by_key)
        self.assertEqual(by_key[(ids["elden"], "EUR")]["addon_spent"], 10.0)


class KeyResellerSourceTests(ToolDBTestCase):
    async def test_key_reseller_vocabulary_and_aliases(self):
        gid = await seed_game("The Case of the Golden Idol")
        await add_platform(gid, "steam")

        result = await acquisition.set_acquisition(
            game_id=gid, platform="steam", purchase_source="GAMIVO",
            price_paid=9.38, price_currency="EUR",
        )
        self.assertEqual(result["acquisition"]["purchase_source"], "key_reseller")

        for alias in ("kinguin", "g2a", "Green Man Gaming", "indiegala", "key_reseller"):
            result = await acquisition.set_acquisition(
                game_id=gid, platform="steam", purchase_source=alias
            )
            self.assertEqual(result["acquisition"]["purchase_source"], "key_reseller")


class AddonMintParentResolutionTests(ToolDBTestCase):
    async def test_season_pass_parents_under_most_specific_title(self):
        # Both franchise entries exist; the suffix-stripped, longest candidate
        # must win — first-match order parented "Deus Ex: Mankind Divided
        # Season Pass" under the 2000 original in prod.
        original = await seed_game("Deus Ex")
        await add_platform(original, "steam")
        mankind_divided = await seed_game("Deus Ex: Mankind Divided")
        await add_platform(mankind_divided, "steam")

        batch = await acquisition.set_acquisitions_batch(
            [{
                "name": "Deus Ex: Mankind Divided Season Pass",
                "platform": "steam",
                "content_type": "dlc",
                "price_paid": 14.99,
                "purchase_source": "steam",
            }],
            create_missing=True,
        )

        created = batch["created_details"][0]
        self.assertEqual(created["parent_game_id"], mankind_divided)
        self.assertEqual(created["parent_name"], "Deus Ex: Mankind Divided")
        self.assertNotEqual(created["parent_game_id"], original)


class BatchDryRunTests(ToolDBTestCase):
    async def test_dry_run_statuses_without_writes(self):
        gid = await seed_game("Hades")
        await add_platform(gid, "steam")

        batch = await acquisition.set_acquisitions_batch(
            [
                {"name": "Hades", "platform": "steam", "price_paid": 19.99},
                {"name": "Missing Game", "platform": "steam", "price_paid": 5.0},
            ],
            create_missing=False,
            dry_run=True,
        )

        self.assertTrue(batch["dry_run"])
        self.assertEqual(batch["filled"], 1)
        self.assertEqual([i["name"] for i in batch["unmatched"]], ["Missing Game"])

        row = await _acquisition_row(gid, "steam")
        self.assertIsNone(row["price_paid"])

    async def test_dry_run_no_change_matches_wet_semantics(self):
        gid = await seed_game("Celeste")
        await add_platform(gid, "steam")
        await acquisition.set_acquisition(game_id=gid, platform="steam", price_paid=9.99)

        batch = await acquisition.set_acquisitions_batch(
            [{"name": "Celeste", "platform": "steam", "price_paid": 4.99}],
            dry_run=True,
        )
        self.assertEqual(batch["no_change"], 1)

        row = await _acquisition_row(gid, "steam")
        self.assertEqual(row["price_paid"], 9.99)


class FamilyConflictGuardTests(ToolDBTestCase):
    async def _seed_family(self) -> tuple[int, int]:
        # Base game owns steam (the real appid); the edition child owns epic.
        base = await seed_game("Fallout: New Vegas")
        base_gpid = await add_platform(base, "steam", playtime_minutes=2694)
        await add_identifier(base_gpid, "steam_appid", 22380)
        child = await seed_game(
            "Fallout New Vegas Ultimate Edition",
            content_type="edition",
            parent_game_id=base,
            is_primary_library_item=0,
        )
        await add_platform(child, "epic")
        return base, child

    async def test_fuzzy_match_refused_when_family_owns_platform(self):
        base, child = await self._seed_family()

        # A typo'd SKU that only the fuzzy tier can resolve — onto the CHILD.
        batch = await acquisition.set_acquisitions_batch(
            [{
                "name": "Fallout New Vegas Ultimat Edtion",
                "platform": "steam",
                "price_paid": 9.99,
                "purchase_source": "steam",
            }],
            create_platform_rows=True,
        )

        self.assertEqual(batch["family_conflict"], 1)
        detail = batch["family_conflict_details"][0]
        self.assertEqual(detail["game_id"], child)
        self.assertEqual(detail["conflicting_game_id"], base)
        # No second steam row was forked inside the family.
        async with db_module.get_db() as db:
            count = await db.execute_fetchone(
                "SELECT COUNT(*) AS c FROM game_platforms "
                "WHERE platform = 'steam' AND game_id IN (?, ?)",
                (base, child),
            )
        self.assertEqual(count["c"], 1)

    async def test_fuzzy_match_allowed_when_family_does_not_own_platform(self):
        base, child = await self._seed_family()

        batch = await acquisition.set_acquisitions_batch(
            [{
                "name": "Fallout New Vegas Ultimat Edtion",
                "platform": "gog",
                "price_paid": 9.99,
            }],
            create_platform_rows=True,
        )

        self.assertEqual(batch["family_conflict"], 0)
        self.assertEqual(batch["filled"], 1)

    async def test_unowned_family_stub_does_not_trigger_conflict(self):
        # An owned=0 manual stub is not real ownership — "already owns that
        # platform" must mean owned=1, or a valid purchase write gets refused.
        base, child = await self._seed_family()
        await add_platform(base, "gog", owned=0)

        batch = await acquisition.set_acquisitions_batch(
            [{
                "name": "Fallout New Vegas Ultimat Edtion",
                "platform": "gog",
                "price_paid": 9.99,
            }],
            create_platform_rows=True,
        )

        self.assertEqual(batch["family_conflict"], 0)
        self.assertEqual(batch["filled"], 1)


class BundleBreakdownCapTests(ToolDBTestCase):
    """by_bundle grows with purchase history, unlike the other breakdowns.

    by_year/by_source/by_platform are bounded by a fixed vocabulary;
    bundle_name gains a row for every bundle ever bought (139 distinct on the
    real library, 47% of the whole spending response before the cap).
    """

    async def _buy(self, name, bundle, price):
        gid = await seed_game(name)
        await add_platform(gid, "steam")
        await acquisition.set_acquisition(
            game_id=gid, platform="steam", price_paid=price,
            price_currency="USD", bundle_name=bundle,
        )

    async def test_by_bundle_is_capped_but_count_is_the_true_total(self):
        cap = acquisition.BUNDLE_BREAKDOWN_CAP
        for i in range(cap + 7):
            await self._buy(f"Game {i}", f"Bundle {i}", float(i + 1))

        stats = await acquisition.get_spending_stats()
        self.assertEqual(len(stats["by_bundle"]), cap)
        self.assertEqual(stats["by_bundle_count"], cap + 7)
        self.assertTrue(stats["by_bundle_truncated"])

    async def test_capped_page_is_the_biggest_spends(self):
        cap = acquisition.BUNDLE_BREAKDOWN_CAP
        for i in range(cap + 5):
            await self._buy(f"Game {i}", f"Bundle {i}", float(i + 1))

        stats = await acquisition.get_spending_stats()
        spends = [row["spent"] for row in stats["by_bundle"]]
        self.assertEqual(spends, sorted(spends, reverse=True))
        # the cheapest bundles are the ones dropped
        self.assertGreater(min(spends), 5.0)

    async def test_untruncated_breakdown_reports_not_truncated(self):
        await self._buy("Solo", "Only Bundle", 10.0)
        stats = await acquisition.get_spending_stats()
        self.assertEqual(stats["by_bundle_count"], 1)
        self.assertFalse(stats["by_bundle_truncated"])
