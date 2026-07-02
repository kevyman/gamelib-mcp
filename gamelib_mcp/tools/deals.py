"""get_wishlist_deals — merges ITAD (steam) and DekuDeals (switch2) current
prices onto wishlist rows.

The DB layer (``load_wishlist_with_prices``) deliberately LEFT JOINs
game_prices on (game_id, platform) only — not shop — so a game with cached
prices from multiple shops yields multiple rows here. Picking "cheapest
across shops" is this module's job, along with deciding staleness and
driving the two provider refreshes (ITAD for steam, DekuDeals for switch2).
A refresh failure never raises and never touches the cache; it degrades to
serving whatever is already cached (possibly nothing).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from ..data.db import (
    extract_best_fuzzy_key,
    load_wishlist_with_prices,
    upsert_game_prices,
)
from ..data.dekudeals import fetch_wishlist_prices
from ..data.itad import fetch_steam_prices, is_itad_configured
from ..data.scrape_config import load_scrape_config
from .common import LIBRARY_PLATFORMS
from .common import validate_platform as _validate_platform

logger = logging.getLogger(__name__)

_PRICE_TTL_HOURS = 12


def _fetched_at_is_stale(fetched_at: str | None) -> bool:
    """True if fetched_at is missing, unparseable, or older than the TTL."""
    if fetched_at is None:
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched > timedelta(hours=_PRICE_TTL_HOURS)


def _group_by_key(
    rows: list[aiosqlite.Row],
) -> dict[tuple[int, str], list[aiosqlite.Row]]:
    grouped: dict[tuple[int, str], list[aiosqlite.Row]] = {}
    for row in rows:
        grouped.setdefault((row["game_id"], row["platform"]), []).append(row)
    return grouped


def _key_needs_refresh(key_rows: list[aiosqlite.Row], refresh: bool) -> bool:
    if refresh:
        return True
    priced = [r for r in key_rows if r["price"] is not None]
    if not priced:
        return True
    # Rows for the same (game_id, platform) are always written together (one
    # upsert_game_prices call per refresh), so their fetched_at values agree;
    # take the freshest defensively in case of a partial history.
    freshest = max(r["fetched_at"] for r in priced)
    return _fetched_at_is_stale(freshest)


def _match_wishlist_game_id(
    title: str, candidate_names: dict[str, str], name_to_id: dict[str, int], cutoff: int
) -> int | None:
    """Fuzzy-match a DekuDeals price title against this tool's own wishlist
    candidate set (NOT the whole games table — see dekudeals.py's
    _match_game_id, which this mirrors but scopes down)."""
    title_lower = title.lower()
    if title_lower in name_to_id:
        return name_to_id[title_lower]
    match = extract_best_fuzzy_key(title_lower, candidate_names, cutoff=cutoff)
    if match:
        return name_to_id[match]
    return None


async def get_wishlist_deals(
    platform: str | None = None,
    max_price: float | None = None,
    min_cut_pct: int | None = None,
    refresh: bool = False,
) -> dict:
    """
    Current prices/deals for wishlist games, cheapest first.

    Prices come from IsThereAnyDeal (Steam wishlist items; covers Steam/GOG/
    Epic shops) and DekuDeals (switch2 items). Cached in the DB; a fetch runs
    automatically when the cache is older than 12h, or immediately with
    refresh=True. Filters: platform, max_price, min_cut_pct (e.g. 50 for "at
    least half off"). max_price/comparisons are NOT currency-converted: they
    compare each deal's raw numeric price in whatever currency that deal is
    quoted in (Steam/GOG/Epic follow ITAD_COUNTRY, switch2 follows whatever
    currency DekuDeals renders for this account's region). Set ITAD_COUNTRY
    to match if comparing thresholds meaningfully across platforms. Items
    with no known price are listed separately in unpriced.
    """
    resolved_platform = _validate_platform(platform, LIBRARY_PLATFORMS) if platform else None

    rows = await load_wishlist_with_prices(resolved_platform)
    grouped = _group_by_key(rows)

    # Partition stale wishlist items by the provider that can refresh them.
    # Everything else (e.g. PSN manual entries) has no price source at all
    # and is simply left to fall into `unpriced` below.
    steam_needs_refresh: dict[int, int] = {}  # appid -> game_id
    switch2_needs_refresh: dict[int, str] = {}  # game_id -> name

    for (game_id, plat), key_rows in grouped.items():
        if not _key_needs_refresh(key_rows, refresh):
            continue
        if plat == "steam":
            appid = key_rows[0]["steam_appid"]
            if appid is not None:
                steam_needs_refresh[int(appid)] = game_id
        elif plat == "switch2":
            switch2_needs_refresh[game_id] = key_rows[0]["name"]

    price_refresh_errors: list[str] = []
    notes: dict[str, Any] = {}
    cache_updated = False

    if steam_needs_refresh:
        if not is_itad_configured():
            notes["itad"] = "unconfigured"
        else:
            try:
                prices = await fetch_steam_prices(list(steam_needs_refresh.keys()))
            except Exception as exc:
                logger.warning("ITAD price refresh failed: %s", exc)
                price_refresh_errors.append(f"itad refresh failed: {exc}")
            else:
                upsert_rows = [
                    {
                        "game_id": steam_needs_refresh[appid],
                        "platform": "steam",
                        "shop": info.shop,
                        "price": info.price,
                        "regular_price": info.regular_price,
                        "cut_pct": info.cut_pct,
                        "currency": info.currency,
                        "deal_url": info.deal_url,
                    }
                    for appid, info in prices.items()
                    if appid in steam_needs_refresh
                ]
                if upsert_rows:
                    await upsert_game_prices(upsert_rows)
                    cache_updated = True

    if switch2_needs_refresh:
        try:
            prices_by_title = await fetch_wishlist_prices()
        except Exception as exc:
            logger.warning("DekuDeals price refresh failed: %s", exc)
            price_refresh_errors.append(f"dekudeals refresh failed: {exc}")
        else:
            config = await load_scrape_config("dekudeals")
            name_to_id = {name.lower(): gid for gid, name in switch2_needs_refresh.items()}
            candidate_names = {name: name for name in name_to_id}
            upsert_rows = []
            for title, info in prices_by_title.items():
                matched_game_id = _match_wishlist_game_id(
                    title, candidate_names, name_to_id, cutoff=config.fuzzy_cutoff
                )
                if matched_game_id is None:
                    continue
                upsert_rows.append(
                    {
                        "game_id": matched_game_id,
                        "platform": "switch2",
                        "shop": "dekudeals",
                        "price": info.get("price"),
                        "regular_price": info.get("regular_price"),
                        "cut_pct": info.get("cut_pct"),
                        "currency": info.get("currency"),
                        "deal_url": info.get("deal_url"),
                    }
                )
            if upsert_rows:
                await upsert_game_prices(upsert_rows)
                cache_updated = True

    if cache_updated:
        rows = await load_wishlist_with_prices(resolved_platform)
        grouped = _group_by_key(rows)

    deals: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for (game_id, plat), key_rows in grouped.items():
        priced = [r for r in key_rows if r["price"] is not None]
        if not priced:
            unpriced.append(key_rows[0]["name"])
            continue
        cheapest = min(priced, key=lambda r: r["price"])
        deals.append(
            {
                "game_id": game_id,
                "name": cheapest["name"],
                "platform": plat,
                "shop": cheapest["shop"],
                "price": cheapest["price"],
                "regular_price": cheapest["regular_price"],
                "cut_pct": cheapest["cut_pct"],
                "currency": cheapest["currency"],
                "deal_url": cheapest["deal_url"],
                "wishlisted_at": cheapest["wishlisted_at"],
            }
        )

    # Computed on the unfiltered deal set, before max_price/min_cut_pct below
    # narrow it — those filters compare raw numeric prices with no currency
    # conversion, so flag it whenever more than one currency is in play.
    currencies = {d["currency"] for d in deals if d["currency"] is not None}

    # Filters apply AFTER cheapest-per-item selection; a filtered-out deal is
    # simply excluded, never moved to unpriced (it has a known price, it just
    # doesn't match the filter).
    if max_price is not None:
        deals = [d for d in deals if d["price"] <= max_price]
    if min_cut_pct is not None:
        deals = [d for d in deals if d["cut_pct"] is not None and d["cut_pct"] >= min_cut_pct]

    deals.sort(key=lambda d: d["price"])

    response: dict[str, Any] = {
        "deals": deals,
        "unpriced": unpriced,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(deals),
    }
    if price_refresh_errors:
        response["price_refresh_errors"] = price_refresh_errors
    if len(currencies) > 1:
        response["currency_note"] = (
            f"deals span multiple currencies ({', '.join(sorted(currencies))}); "
            "max_price/min_cut_pct are not currency-converted"
        )
    response.update(notes)
    return response
