"""Characterization tests for gamelib_mcp.tools.deals.get_wishlist_deals.

Patches the fetcher/config functions as imported INTO tools.deals (this
repo's established patching convention — see tests/test_tools_admin.py),
not their origin modules (data.itad / data.dekudeals).
"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, make_steam_game, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.itad import PriceInfo
from gamelib_mcp.tools import deals


async def _seed_wishlist(
    game_id: int,
    platform: str,
    *,
    store_identifier: str | None = None,
    source: str = "steam",
) -> None:
    await db_module.upsert_wishlist_entry(
        game_id, platform, source=source, store_identifier=store_identifier
    )


async def _seed_price(
    game_id: int,
    platform: str,
    shop: str,
    price: float | None,
    *,
    regular_price: float | None = None,
    cut_pct: int | None = None,
    currency: str = "USD",
    deal_url: str = "https://example.com/deal",
    fetched_at: str | None = None,
    history_low: float | None = None,
    history_low_currency: str | None = None,
    deal_ends_at: str | None = None,
) -> None:
    await db_module.upsert_game_prices(
        [
            {
                "game_id": game_id,
                "platform": platform,
                "shop": shop,
                "price": price,
                "regular_price": regular_price if regular_price is not None else price,
                "cut_pct": cut_pct,
                "currency": currency,
                "deal_url": deal_url,
                "history_low": history_low,
                "history_low_currency": history_low_currency,
                "deal_ends_at": deal_ends_at,
            }
        ]
    )
    if fetched_at is not None:
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_prices SET fetched_at = ? "
                "WHERE game_id = ? AND platform = ? AND shop = ?",
                (fetched_at, game_id, platform, shop),
            )
            await db.commit()


class GetWishlistDealsTests(ToolDBTestCase):
    async def test_cached_fresh_path_does_not_call_fetchers(self):
        game_id = await seed_game("Hollow Knight")
        await _seed_wishlist(game_id, "steam", store_identifier="123")
        await _seed_price(game_id, "steam", "steam", 9.99, cut_pct=50)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ) as mock_itad, patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ) as mock_deku, patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        mock_itad.assert_not_awaited()
        mock_deku.assert_not_awaited()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["deals"][0]["price"], 9.99)
        self.assertEqual(result["deals"][0]["game_id"], game_id)
        self.assertEqual(result["unpriced"], [])

    async def test_refresh_true_calls_fetchers_even_if_cache_fresh(self):
        game_id = await seed_game("Hollow Knight")
        await _seed_wishlist(game_id, "steam", store_identifier="123")
        await _seed_price(game_id, "steam", "steam", 9.99, cut_pct=50)

        switch_id = await seed_game("Pikmin 4")
        await _seed_wishlist(switch_id, "switch2", source="dekudeals")
        await _seed_price(switch_id, "switch2", "dekudeals", 39.99)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices",
            AsyncMock(return_value={123: PriceInfo("steam", 4.99, 9.99, 50, "USD", "url")}),
        ) as mock_itad, patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices",
            AsyncMock(
                return_value={
                    "Pikmin 4": {
                        "price": 19.99,
                        "regular_price": 39.99,
                        "cut_pct": 50,
                        "currency": "USD",
                        "deal_url": "https://dekudeals.com/x",
                    }
                }
            ),
        ) as mock_deku, patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(refresh=True)

        mock_itad.assert_awaited_once()
        mock_deku.assert_awaited_once()
        by_game = {d["game_id"]: d for d in result["deals"]}
        self.assertEqual(by_game[game_id]["price"], 4.99)
        self.assertEqual(by_game[switch_id]["price"], 19.99)

    async def test_max_price_and_min_cut_pct_filter_without_moving_to_unpriced(self):
        cheap_id = await seed_game("Cheap Game")
        await _seed_wishlist(cheap_id, "steam", store_identifier="1")
        await _seed_price(cheap_id, "steam", "steam", 5.00, cut_pct=10)

        pricey_id = await seed_game("Pricey Game")
        await _seed_wishlist(pricey_id, "steam", store_identifier="2")
        await _seed_price(pricey_id, "steam", "steam", 59.99, cut_pct=90)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(max_price=10.0)

        names = {d["name"] for d in result["deals"]}
        self.assertEqual(names, {"Cheap Game"})
        # Filtered-out game must NOT appear in unpriced (it has a price, just
        # doesn't pass the filter).
        self.assertEqual(result["unpriced"], [])
        self.assertEqual(result["count"], 1)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(min_cut_pct=50)

        names = {d["name"] for d in result["deals"]}
        self.assertEqual(names, {"Pricey Game"})
        self.assertEqual(result["unpriced"], [])

    async def test_max_price_keeps_game_when_only_alternative_qualifies(self):
        # Reviewer-found bug: filtering only checked the RECOMMENDED option's
        # flat price/cut_pct. Here the recommended (hardware-preferred)
        # option is switch2 @ 50 (steam's 30 isn't below the 50% override
        # ratio of 25, so no override fires) — but the steam alternative @ 30
        # clearly satisfies max_price=40 and must not be hidden.
        game_id = await seed_game("Cross Platform Game")
        await _seed_wishlist(game_id, "steam", store_identifier="1")
        await _seed_wishlist(game_id, "switch2", source="dekudeals")
        await db_module.set_meta("hardware_preference", json.dumps(["switch2", "steam"]))
        await _seed_price(game_id, "switch2", "dekudeals", 50.0, currency="EUR")
        await _seed_price(game_id, "steam", "steam", 30.0, currency="EUR")

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(max_price=40.0)

        entry = next((d for d in result["deals"] if d["game_id"] == game_id), None)
        self.assertIsNotNone(entry, "game with a qualifying alternative must not be dropped")
        # Recommended fields must stay pointed at switch2/50 — the filter
        # decides keep-or-drop only, it never re-points recommended.
        self.assertEqual(entry["platform"], "switch2")
        self.assertEqual(entry["price"], 50.0)
        self.assertEqual(
            [a["platform"] for a in entry["alternatives"]], ["steam"]
        )
        self.assertEqual(result["count"], 1)

    async def test_max_price_drops_game_when_no_option_qualifies(self):
        # Inverse of the above: neither the recommended option nor any
        # alternative satisfies max_price, so the game is correctly excluded.
        game_id = await seed_game("Too Expensive Everywhere")
        await _seed_wishlist(game_id, "steam", store_identifier="2")
        await _seed_wishlist(game_id, "switch2", source="dekudeals")
        await db_module.set_meta("hardware_preference", json.dumps(["switch2", "steam"]))
        await _seed_price(game_id, "switch2", "dekudeals", 50.0, currency="EUR")
        await _seed_price(game_id, "steam", "steam", 30.0, currency="EUR")

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(max_price=20.0)

        self.assertNotIn(game_id, [d["game_id"] for d in result["deals"]])
        # Still has a price — must not be misfiled as unpriced.
        self.assertEqual(result["unpriced"], [])
        self.assertEqual(result["count"], 0)

    async def test_currency_note_added_when_deals_span_multiple_currencies(self):
        usd_id = await seed_game("USD Game")
        await _seed_wishlist(usd_id, "steam", store_identifier="1")
        await _seed_price(usd_id, "steam", "steam", 9.99, currency="USD")

        eur_id = await seed_game("EUR Game")
        await _seed_wishlist(eur_id, "switch2")
        await _seed_price(eur_id, "switch2", "dekudeals", 19.99, currency="EUR")

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        self.assertIn("currency_note", result)
        self.assertIn("EUR", result["currency_note"])
        self.assertIn("USD", result["currency_note"])

    async def test_currency_note_absent_when_single_currency(self):
        game_id = await seed_game("Single Currency Game")
        await _seed_wishlist(game_id, "steam", store_identifier="1")
        await _seed_price(game_id, "steam", "steam", 9.99, currency="USD")

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        self.assertNotIn("currency_note", result)

    async def test_fetcher_exception_returns_cached_data_with_error(self):
        game_id = await seed_game("Stale Game")
        await _seed_wishlist(game_id, "steam", store_identifier="42")
        await _seed_price(
            game_id,
            "steam",
            "steam",
            14.99,
            cut_pct=25,
            fetched_at="2020-01-01T00:00:00+00:00",  # far past the 12h TTL
        )

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        self.assertIn("price_refresh_errors", result)
        self.assertTrue(result["price_refresh_errors"])
        # Cached (stale) price must still be served.
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["deals"][0]["price"], 14.99)

    async def test_refresh_prunes_stale_cheaper_shop_from_previous_winner(self):
        # End-to-end regression for the game_prices staleness bug: a GOG sale
        # two weeks ago cached (steam, GOG, 5.00) as the winner. Today's ITAD
        # refresh says Steam is now cheapest at 20.00. Without pruning, the
        # stale GOG row would still be in the join and `min()` would report
        # the dead 5.00 deal forever, even with refresh=True.
        game_id = await seed_game("Sale Then Not")
        await _seed_wishlist(game_id, "steam", store_identifier="555")
        await _seed_price(
            game_id,
            "steam",
            "GOG",
            5.00,
            currency="USD",
            deal_url="https://gog.com/deal",
            fetched_at="2020-01-01T00:00:00+00:00",  # far past the 12h TTL
        )

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices",
            AsyncMock(
                return_value={555: PriceInfo("Steam", 20.00, 20.00, 0, "USD", "https://store.steampowered.com/app/555")}
            ),
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals(refresh=True)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["deals"][0]["shop"], "Steam")
        self.assertEqual(result["deals"][0]["price"], 20.00)

        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT shop FROM game_prices WHERE game_id = ?", (game_id,)
            )
        self.assertEqual([r["shop"] for r in rows], ["Steam"])

    async def test_unconfigured_itad_leaves_steam_rows_unpriced(self):
        game_id = await seed_game("No ITAD Game")
        await _seed_wishlist(game_id, "steam", store_identifier="7")
        # No cached price at all -> needs refresh, but ITAD is unconfigured.

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ) as mock_itad, patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=False
        ):
            result = await deals.get_wishlist_deals()

        mock_itad.assert_not_awaited()
        self.assertEqual(result["unpriced"], ["No ITAD Game"])
        self.assertEqual(result["itad"], "unconfigured")
        self.assertEqual(result["deals"], [])

    async def test_wishlist_nested_dlc_row_flows_through_without_crash(self):
        # Regression: nested (dlc) rows in a wishlist must pass through
        # without crashing and appear in the output with pricing applied
        # by the same rules as any entry.
        parent_id = await make_steam_game("Bloodborne", 100, tags=["Horror"])
        dlc_id = await seed_game(
            "The Old Hunters",
            tags=["Horror"],
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await _seed_wishlist(dlc_id, "steam", store_identifier="101")
        await _seed_price(dlc_id, "steam", "steam", 19.99, cut_pct=10)

        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        self.assertEqual(result["count"], 1)
        deal = result["deals"][0]
        self.assertEqual(deal["game_id"], dlc_id)
        self.assertEqual(deal["name"], "The Old Hunters")
        self.assertEqual(deal["price"], 19.99)


async def _set_igdb_platforms(game_id: int, ids: list[int]) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            "UPDATE games SET igdb_platforms = ?, igdb_cached_at = 'x' WHERE id = ?",
            (json.dumps(ids), game_id),
        )
        await db.commit()


async def _set_hw_pref(platforms: list[str]) -> None:
    await db_module.set_meta("hardware_preference", json.dumps(platforms))


async def _miss_every_requested_title(titles: list[str]) -> dict[str, None]:
    """fetch_search_prices stub reporting a CONFIRMED miss for every requested
    title (searched cleanly, no card) — the negatively-cacheable outcome, as
    opposed to an empty dict, which means the searches never loaded."""
    return dict.fromkeys(titles)


async def _price_every_requested_title(titles: list[str]) -> dict[str, dict]:
    """fetch_search_prices stub that hits on every title it is asked about —
    keyed off the request, since which candidates survive the per-call cap
    depends on the loader's row order, not on seed order."""
    return {
        title: {
            "price": 5.0,
            "regular_price": 10.0,
            "cut_pct": 50,
            "currency": "USD",
            "deal_url": "https://dekudeals.com/x",
        }
        for title in titles
    }


class PreferenceAwareDealsTests(ToolDBTestCase):
    async def test_steam_item_with_switch_release_gets_search_priced_and_preferred(self):
        game_id = await seed_game("Crossplay Game")
        await _seed_wishlist(game_id, "steam", store_identifier="42")
        await _set_igdb_platforms(game_id, [6, 508])
        await _set_hw_pref(["switch2", "steam"])

        itad = AsyncMock(return_value={42: PriceInfo("Steam", 10.0, 10.0, 0, "EUR", "u1")})
        search = AsyncMock(return_value={"Crossplay Game": {
            "price": 12.0, "regular_price": 12.0, "cut_pct": 0,
            "currency": "EUR", "deal_url": "u2"}})
        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", itad), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", search), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_awaited_once_with(["Crossplay Game"])
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "switch2")   # preferred wins at 12.0 vs 10.0
        self.assertEqual(entry["price"], 12.0)
        self.assertIn("preferred", entry["recommendation_reason"])
        self.assertEqual(entry["alternatives"][0]["platform"], "steam")
        self.assertEqual(entry["wishlisted_on"], ["steam"])

    async def test_override_when_steam_deal_too_good(self):
        game_id = await seed_game("Bargain Game")
        await _seed_wishlist(game_id, "steam", store_identifier="43")
        await _set_igdb_platforms(game_id, [6, 130])
        await _set_hw_pref(["switch2", "steam"])
        await _seed_price(game_id, "steam", "Steam", 4.99, currency="EUR")
        await _seed_price(game_id, "switch2", "dekudeals", 14.0, currency="EUR")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_not_awaited()  # both platforms freshly cached
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "steam")
        self.assertIn("override", entry["recommendation_reason"])

    async def test_owned_on_switch2_suppresses_candidate(self):
        game_id = await seed_game("Already On Switch")
        await add_platform(game_id, "switch2", owned=1)
        await _seed_wishlist(game_id, "steam", store_identifier="44")
        await _set_igdb_platforms(game_id, [6, 508])
        await _set_hw_pref(["switch2", "steam"])
        await _seed_price(game_id, "steam", "Steam", 9.0, currency="EUR")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_not_awaited()
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "steam")

    async def test_stale_owned_platform_price_excluded_from_assembly(self):
        # Reproduces a reviewer-found bug: game_prices can carry a cached
        # price for a platform the game is NOW owned on (e.g. cached before
        # the purchase, or a leftover cross-platform search hit). The
        # deal-assembly loop must exclude it the same way _candidate_platforms
        # excludes it from refresh — not just skip refreshing it while still
        # surfacing the stale cached row.
        game_id = await seed_game("Bought It On Switch")
        await add_platform(game_id, "switch2", owned=1)
        await _seed_wishlist(game_id, "steam", store_identifier="45")
        await _set_igdb_platforms(game_id, [6, 508])
        await _set_hw_pref(["switch2", "steam"])
        # Stale switch2 price predating ownership, cheaper than steam.
        await _seed_price(game_id, "switch2", "dekudeals", 5.0, currency="EUR")
        await _seed_price(game_id, "steam", "Steam", 9.0, currency="EUR")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_not_awaited()  # already owned there — no refresh, but also no recommend
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "steam")
        self.assertNotIn(
            "switch2", [a["platform"] for a in entry["alternatives"]]
        )

    async def test_all_priced_platforms_owned_lands_in_unpriced(self):
        # Edge case: the ONLY cached price is for a platform now owned —
        # must degrade to unpriced instead of crashing on an empty options list.
        game_id = await seed_game("Only Owned Priced")
        await add_platform(game_id, "switch2", owned=1)
        await _seed_wishlist(game_id, "switch2", source="dekudeals")
        await _seed_price(game_id, "switch2", "dekudeals", 5.0, currency="EUR")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        self.assertNotIn(game_id, [d["game_id"] for d in result["deals"]])
        self.assertIn("Only Owned Priced", result["unpriced"])

    async def _seed_search_candidates(self, count: int) -> list[int]:
        """`count` steam-wishlisted games with a Switch release and no price —
        i.e. per-title DekuDeals search candidates."""
        await _set_hw_pref(["switch2"])
        ids = []
        for i in range(count):
            gid = await seed_game(f"Cap Game {i}")
            await _seed_wishlist(gid, "steam", store_identifier=str(100 + i))
            await _set_igdb_platforms(gid, [6, 508])
            ids.append(gid)
        return ids

    async def test_search_lookup_cap_defers_overflow(self):
        await self._seed_search_candidates(deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock(return_value={})) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        self.assertEqual(len(search.await_args.args[0]), deals._MAX_SWITCH2_SEARCH_LOOKUPS)
        # An empty result dict means every attempt was inconclusive (see
        # fetch_search_prices' contract), so nothing resolved and nothing was
        # negatively cached — the whole backlog is deferred, not just the
        # over-cap remainder.
        self.assertEqual(
            result["switch2_lookups_deferred"], deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3
        )
        self.assertNotIn("switch2_lookups_performed", result)
        self.assertNotIn("switch2_lookups_not_found", result)

    async def test_deferred_counter_excludes_lookups_resolved_this_call(self):
        ids = await self._seed_search_candidates(deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_price_every_requested_title)), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        self.assertEqual(
            result["switch2_lookups_performed"], deals._MAX_SWITCH2_SEARCH_LOOKUPS
        )
        self.assertEqual(result["switch2_lookups_deferred"], 3)
        self.assertEqual(
            len([d for d in result["deals"] if d["game_id"] in ids]),
            deals._MAX_SWITCH2_SEARCH_LOOKUPS,
        )

    async def test_deferred_counter_excludes_candidates_priced_on_an_earlier_call(self):
        # The reported bug: the counter was `len(candidates) - cap`, a static
        # expression blind to the price cache, so it reported the same number on
        # every call while the queue was in fact draining. refresh=True re-queues
        # every candidate, but the three already priced are NOT still backlog.
        ids = await self._seed_search_candidates(deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3)
        for gid in ids[:3]:
            await _seed_price(gid, "switch2", "dekudeals", 12.0)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals(refresh=True)

        self.assertEqual(result["switch2_lookups_deferred"], deals._MAX_SWITCH2_SEARCH_LOOKUPS)

    async def test_never_priced_candidates_get_cap_slots_before_stale_reprices(self):
        # Root cause of a prod backlog pinned at ~47 for weeks: the lookup
        # queue was one wishlisted_at-DESC list, so once the newest-wishlisted
        # candidates were priced and their 12h TTL lapsed they re-took every
        # capped slot on each call, starving the never-priced tail whenever
        # calls arrive further apart than the TTL. Never-priced candidates
        # must claim the slots first — a stale price still serves from cache.
        cap = deals._MAX_SWITCH2_SEARCH_LOOKUPS
        ids = await self._seed_search_candidates(cap + 3)
        pending_ids, stale_ids = ids[:3], ids[3:]
        now = datetime.now(UTC)
        stale_fetch = (now - timedelta(hours=48)).isoformat()
        async with db_module.get_db() as db:
            for rank, gid in enumerate([*stale_ids, *pending_ids]):
                # Stale-priced games newest-wishlisted, pending ones oldest —
                # the exact order that starved the tail before the fix.
                await db.execute(
                    "UPDATE game_wishlist SET wishlisted_at = ? WHERE game_id = ?",
                    ((now - timedelta(days=rank)).isoformat(), gid),
                )
            await db.commit()
        for gid in stale_ids:
            await _seed_price(gid, "switch2", "dekudeals", 12.0, fetched_at=stale_fetch)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_price_every_requested_title)) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        requested = search.await_args.args[0]
        self.assertEqual(len(requested), cap)
        for i in range(3):
            self.assertIn(f"Cap Game {i}", requested)  # the never-priced three
        # Everything never-priced was looked up this call, so no backlog left.
        self.assertNotIn("switch2_lookups_deferred", result)

    async def test_deferred_counter_omitted_once_backlog_drains(self):
        await self._seed_search_candidates(2)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_price_every_requested_title)), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        self.assertEqual(result["switch2_lookups_performed"], 2)
        self.assertNotIn("switch2_lookups_deferred", result)

    async def test_availability_pending_counts_unfetched_games(self):
        gid = await seed_game("IGDB Pending")
        await _seed_wishlist(gid, "steam", store_identifier="77")
        # igdb_cached_at stays NULL from seed_game
        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()
        search.assert_not_awaited()
        self.assertEqual(result["availability_pending"], 1)

    async def test_confirmed_miss_is_cached_and_stops_reconsuming_the_cap(self):
        await self._seed_search_candidates(deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_miss_every_requested_title)) as first_search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            first = await deals.get_wishlist_deals()

        first_titles = set(first_search.await_args.args[0])
        self.assertEqual(
            first["switch2_lookups_not_found"], deals._MAX_SWITCH2_SEARCH_LOOKUPS
        )
        # The misses are settled, so only the untried over-cap remainder is backlog.
        self.assertEqual(first["switch2_lookups_deferred"], 3)

        # Second call: the recorded misses must not take a lookup slot again —
        # the whole point of the negative cache. refresh=True does not override it.
        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_miss_every_requested_title)) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            second = await deals.get_wishlist_deals(refresh=True)

        second_titles = set(search.await_args.args[0])
        self.assertEqual(len(second_titles), 3)  # only the untried three
        self.assertEqual(second_titles & first_titles, set())
        self.assertEqual(
            second["switch2_lookups_not_found"], deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3
        )
        self.assertNotIn("switch2_lookups_deferred", second)  # queue fully settled

    async def test_inconclusive_fetch_is_not_negatively_cached(self):
        # An empty result dict = the searches never loaded. Caching that as a
        # miss would suppress retries for a game DekuDeals may well sell, so
        # the candidate must stay queued and no marker row may be written.
        ids = await self._seed_search_candidates(2)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            first = await deals.get_wishlist_deals()

        self.assertEqual(first["switch2_lookups_deferred"], 2)
        self.assertNotIn("switch2_lookups_not_found", first)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) AS n FROM game_prices WHERE platform = 'switch2'"
            )
        self.assertEqual(row["n"], 0)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_price_every_requested_title)) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            second = await deals.get_wishlist_deals()

        self.assertEqual(len(search.await_args.args[0]), len(ids))  # retried, not skipped
        self.assertEqual(second["switch2_lookups_performed"], 2)

    async def test_miss_never_blanks_an_existing_cached_price(self):
        # upsert_game_prices' invariant: a failed/partial fetch must never
        # blank a previously cached price. A refresh whose search misses a
        # game we already have a price for must leave that price intact.
        gid = (await self._seed_search_candidates(1))[0]
        await _seed_price(gid, "switch2", "dekudeals", 21.0)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_miss_every_requested_title)), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals(refresh=True)

        entry = next(d for d in result["deals"] if d["game_id"] == gid)
        self.assertEqual(entry["price"], 21.0)
        self.assertNotIn("switch2_lookups_not_found", result)

    async def test_stale_miss_marker_is_retried(self):
        gid = (await self._seed_search_candidates(1))[0]
        stale = (
            datetime.now(UTC)
            - timedelta(hours=deals._SWITCH2_MISS_RETRY_HOURS + 1)
        ).isoformat()
        await _seed_price(gid, "switch2", "dekudeals", None, fetched_at=stale)

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices",
                   AsyncMock(side_effect=_price_every_requested_title)) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        self.assertEqual(len(search.await_args.args[0]), 1)  # backoff expired
        self.assertEqual(result["switch2_lookups_performed"], 1)

    async def test_availability_unknown_counts_games_with_no_igdb_platform_list(self):
        await _set_hw_pref(["switch2"])
        unknown = await seed_game("No Platform Data")
        await _seed_wishlist(unknown, "steam", store_identifier="200")
        # igdb_platforms stays NULL — Switch availability is undecidable.
        confirmed_no = await seed_game("PC Only")
        await _seed_wishlist(confirmed_no, "steam", store_identifier="201")
        await _set_igdb_platforms(confirmed_no, [6])  # a real "no Switch release"

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_not_awaited()
        self.assertEqual(result["switch2_availability_unknown"], 1)

    async def test_availability_unknown_silent_when_switch2_not_preferred(self):
        await _set_hw_pref(["steam"])
        gid = await seed_game("No Platform Data")
        await _seed_wishlist(gid, "steam", store_identifier="200")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        # No switch2 preference means no per-title search path at all — the
        # counter would be noise, not a gap.
        self.assertNotIn("switch2_availability_unknown", result)


class DealsPureHelperTests(unittest.TestCase):
    def _opt(self, platform, price):
        return {"platform": platform, "price": price}

    def test_available_platforms_maps_igdb_ids(self):
        self.assertEqual(deals._available_platforms("[6, 130, 508, 167, 999]"),
                         {"steam", "switch2", "ps5"})
        self.assertEqual(deals._available_platforms(None), set())
        self.assertEqual(deals._available_platforms("not json"), set())

    def test_candidates_add_preferred_available_unowned_priceable(self):
        got = deals._candidate_platforms(
            wishlisted_on={"steam"}, available={"steam", "switch2", "ps5"},
            owned=set(), hw_pref=["switch2", "steam"],
        )
        self.assertEqual(got, {"steam", "switch2"})  # ps5 has no price source

    def test_candidates_exclude_owned_and_respect_empty_pref(self):
        self.assertEqual(
            deals._candidate_platforms({"steam"}, {"steam", "switch2"}, {"switch2"}, ["switch2"]),
            {"steam"},
        )
        self.assertEqual(
            deals._candidate_platforms({"steam"}, {"steam", "switch2"}, set(), []),
            {"steam"},
        )

    def test_pick_recommended_prefers_preferred_platform(self):
        options = [self._opt("steam", 10.0), self._opt("switch2", 14.0)]
        chosen, reason = deals._pick_recommended(options, ["switch2", "steam"], 0.5)
        self.assertEqual(chosen["platform"], "switch2")
        self.assertIn("preferred", reason)

    def test_pick_recommended_overrides_when_deal_too_good(self):
        options = [self._opt("steam", 4.99), self._opt("switch2", 14.0)]
        chosen, reason = deals._pick_recommended(options, ["switch2", "steam"], 0.5)
        self.assertEqual(chosen["platform"], "steam")
        self.assertIn("override", reason)

    def test_pick_recommended_boundary_is_strict(self):
        options = [self._opt("steam", 7.0), self._opt("switch2", 14.0)]
        chosen, _ = deals._pick_recommended(options, ["switch2"], 0.5)
        self.assertEqual(chosen["platform"], "switch2")  # 7.0 is NOT < 0.5*14.0

    def test_pick_recommended_no_pref_returns_cheapest(self):
        options = [self._opt("switch2", 14.0), self._opt("steam", 10.0)]
        chosen, reason = deals._pick_recommended(options, [], 0.5)
        self.assertEqual(chosen["platform"], "steam")
        self.assertEqual(reason, "cheapest available")

    def test_pick_recommended_preferred_is_cheapest(self):
        options = [self._opt("steam", 10.0), self._opt("switch2", 8.0)]
        chosen, reason = deals._pick_recommended(options, ["switch2"], 0.5)
        self.assertEqual(chosen["platform"], "switch2")
        self.assertIn("also preferred platform", reason)
        self.assertIn("switch2", reason)


class HistoryLowAndExpiryTests(ToolDBTestCase):
    """ITAD's all-time low and the deal's expiry, cached and surfaced."""

    @staticmethod
    def _no_fetch():
        return (
            patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()),
            patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()),
            patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()),
            patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True),
        )

    async def _deal_for(self, game_id: int) -> dict:
        itad, deku, search, configured = self._no_fetch()
        with itad, deku, search, configured:
            result = await deals.get_wishlist_deals()
        return next(d for d in result["deals"] if d["game_id"] == game_id)

    async def test_price_at_the_all_time_low_is_flagged(self):
        game_id = await seed_game("Hollow Knight")
        await _seed_wishlist(game_id, "steam", store_identifier="367520")
        await _seed_price(
            game_id, "steam", "Steam", 7.49, cut_pct=50, currency="EUR",
            history_low=7.49, history_low_currency="EUR",
            deal_ends_at="2026-09-15T17:00:00+00:00",
        )

        entry = await self._deal_for(game_id)
        self.assertEqual(entry["history_low"], 7.49)
        self.assertEqual(entry["history_low_currency"], "EUR")
        self.assertEqual(entry["deal_ends_at"], "2026-09-15T17:00:00+00:00")
        self.assertTrue(entry["at_history_low"])

    async def test_price_above_the_low_is_not_flagged(self):
        game_id = await seed_game("Above The Low")
        await _seed_wishlist(game_id, "steam", store_identifier="1")
        await _seed_price(
            game_id, "steam", "Steam", 12.99, currency="EUR",
            history_low=7.49, history_low_currency="EUR",
        )

        entry = await self._deal_for(game_id)
        self.assertEqual(entry["history_low"], 7.49)
        self.assertFalse(entry["at_history_low"])

    async def test_row_without_history_reports_nulls_and_false(self):
        # The DekuDeals (switch2) shape: a real price, no history behind it.
        # "No known low" must never read as "lowest ever".
        game_id = await seed_game("Pikmin 4")
        await _seed_wishlist(game_id, "switch2", source="dekudeals")
        await _seed_price(game_id, "switch2", "dekudeals", 39.99, currency="EUR")

        entry = await self._deal_for(game_id)
        self.assertIsNone(entry["history_low"])
        self.assertIsNone(entry["history_low_currency"])
        self.assertIsNone(entry["deal_ends_at"])
        self.assertFalse(entry["at_history_low"])

    async def test_mismatched_currency_never_claims_the_low(self):
        # Prices are never converted here, so a USD low proves nothing about a
        # EUR price even when the number is lower.
        game_id = await seed_game("Cross Currency")
        await _seed_wishlist(game_id, "steam", store_identifier="2")
        await _seed_price(
            game_id, "steam", "Steam", 5.00, currency="EUR",
            history_low=9.99, history_low_currency="USD",
        )

        entry = await self._deal_for(game_id)
        self.assertFalse(entry["at_history_low"])

    async def test_alternatives_carry_the_same_fields(self):
        game_id = await seed_game("Two Platforms")
        await _seed_wishlist(game_id, "steam", store_identifier="3")
        await _seed_wishlist(game_id, "switch2", source="dekudeals")
        await _seed_price(
            game_id, "steam", "Steam", 5.00, currency="EUR",
            history_low=5.00, history_low_currency="EUR",
            deal_ends_at="2026-09-20T00:00:00+00:00",
        )
        await _seed_price(game_id, "switch2", "dekudeals", 29.99, currency="EUR")

        entry = await self._deal_for(game_id)
        self.assertEqual(entry["platform"], "steam")
        self.assertTrue(entry["at_history_low"])
        alternative = entry["alternatives"][0]
        self.assertEqual(alternative["platform"], "switch2")
        self.assertIsNone(alternative["history_low"])
        self.assertIsNone(alternative["deal_ends_at"])

    async def test_itad_refresh_persists_history_and_expiry(self):
        game_id = await seed_game("Fresh From ITAD")
        await _seed_wishlist(game_id, "steam", store_identifier="99")

        info = PriceInfo(
            "Steam", 4.99, 19.99, 75, "USD", "https://store/99",
            history_low=4.99,
            history_low_currency="USD",
            deal_ends_at="2026-09-30T17:00:00+00:00",
        )
        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices",
            AsyncMock(return_value={99: info}),
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            result = await deals.get_wishlist_deals()

        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertTrue(entry["at_history_low"])
        self.assertEqual(entry["deal_ends_at"], "2026-09-30T17:00:00+00:00")

        # ...and the cache carries them, so the next (cached) call agrees.
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT history_low, history_low_currency, deal_ends_at"
                " FROM game_prices WHERE game_id = ?",
                (game_id,),
            )
        self.assertEqual(row["history_low"], 4.99)
        self.assertEqual(row["history_low_currency"], "USD")
        self.assertEqual(row["deal_ends_at"], "2026-09-30T17:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
