"""Wishlist pricing (get_wishlist with_prices=True) — merges ITAD (steam) and
DekuDeals (switch2) current
prices onto wishlist games and recommends a platform per game, honoring
hardware_preference.

The DB layer (``load_wishlist_with_prices``) fans out one row per (wishlist
entry x cached price row on ANY platform) — a game wishlisted on Steam may
also carry a cached switch2 price worth surfacing. ``_group_rows_by_game``
collapses that fan-out per game_id, keyed by ``price_platform`` (not the
wishlist ``platform`` column, which only means "where this was wishlisted").
For each game this module determines candidate platforms (wishlisted, plus
any hardware-preferred platform the game is available on and not already
owned — see Task 6's ``_candidate_platforms``/``_available_platforms``),
refreshes stale per-platform prices via the right provider (ITAD for steam;
DekuDeals's wishlist-page scrape for switch2 items actually on that shared
wishlist, else a capped set of per-title DekuDeals search lookups for
cross-platform candidates), then picks one recommended option per game via
``_pick_recommended`` (preferred platform wins unless another platform's
price is far enough below it to count as "too good to pass up"). A refresh
failure never raises and never touches the cache; it degrades to serving
whatever is already cached (possibly nothing).
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from ..data.db import (
    extract_best_fuzzy_key,
    get_meta,
    load_latest_assessments,
    load_wishlist_with_prices,
    upsert_game_prices,
)
from ..data.dekudeals import fetch_search_prices, fetch_wishlist_prices
from ..data.itad import fetch_steam_prices, is_itad_configured
from ..data.scrape_config import load_scrape_config
from .common import LIBRARY_PLATFORMS
from .common import validate_platform as _validate_platform

logger = logging.getLogger(__name__)

_PRICE_TTL_HOURS = 12

# Platforms this tool has a price source for: ITAD (steam) / DekuDeals (switch2).
_PRICEABLE_PLATFORMS = frozenset({"steam", "switch2"})
# Cap on per-title DekuDeals search lookups per call — each is a live page
# fetch with a politeness delay, so a cold cache prices at most this many
# cross-platform candidates per call and defers the rest (12h TTL staggers
# the remainder across subsequent calls).
_MAX_SWITCH2_SEARCH_LOOKUPS = 12
# Backoff for a CONFIRMED per-title search miss (DekuDeals loaded the search
# page and had no card for the title). Without it a permanent miss — a game
# with no Switch release under that name — is re-searched on every call and
# occupies a cap slot forever, throttling the drain rate for candidates that
# would actually resolve. Longer than the price TTL on purpose: "this game
# isn't sold there" changes far more slowly than its price does. Recorded as
# a NULL-price game_prices row (same (game, platform, shop) key a real price
# would take), so a later hit simply overwrites the marker.
_SWITCH2_MISS_RETRY_HOURS = 72
_DEFAULT_OVERRIDE_RATIO = 0.5


def _below_assessed_target(options: list[dict], assessment: dict | None) -> bool:
    """True when the best comparable price has reached the assessed target.

    "Wishlist at €20" is only answered by a price in the SAME currency —
    prices are never currency-converted here (the repo rule), so an option
    priced in another currency is not evidence either way and is skipped. An
    assessment that recorded no currency is compared against every option:
    that is the honest reading of "the number he wrote down", and it degrades
    to the old behavior of having no target at all when nothing is priced.
    """
    if assessment is None or assessment.get("target_price") is None:
        return False
    currency = assessment.get("price_currency")
    prices = [
        option["price"]
        for option in options
        if option.get("price") is not None
        and (currency is None or option.get("currency") in (None, currency))
    ]
    return bool(prices) and min(prices) <= assessment["target_price"]


def _fetched_at_is_stale(fetched_at: str | None, hours: int = _PRICE_TTL_HOURS) -> bool:
    """True if fetched_at is missing, unparseable, or older than `hours`
    (the price TTL by default; miss markers get their own longer window)."""
    if fetched_at is None:
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return datetime.now(UTC) - fetched > timedelta(hours=hours)


def _group_rows_by_game(rows: list[aiosqlite.Row]) -> dict[int, dict]:
    """Collapse loader rows (wishlist-row × price-row fan-out) per game.

    Returns {game_id: {"name", "wishlisted_on": {platform: wishlisted_at},
    "steam_appid", "igdb_platforms", "igdb_cached_at", "owned_platforms",
    "prices": {price_platform: {shop_key: row}}}}.
    """
    games: dict[int, dict] = {}
    for row in rows:
        state = games.setdefault(
            row["game_id"],
            {
                "name": row["name"],
                "wishlisted_on": {},
                "steam_appid": row["steam_appid"],
                "igdb_platforms": row["igdb_platforms"],
                "igdb_cached_at": row["igdb_cached_at"],
                "owned_platforms": set(json.loads(row["owned_platforms"] or "[]")),
                "prices": {},
            },
        )
        state["wishlisted_on"].setdefault(row["platform"], row["wishlisted_at"])
        if row["steam_appid"] is not None:
            state["steam_appid"] = row["steam_appid"]
        if row["price_platform"] is not None:
            state["prices"].setdefault(row["price_platform"], {})[row["shop"]] = row
    return games


def _platform_needs_refresh(price_rows: dict, refresh: bool) -> bool:
    if refresh:
        return True
    priced = [r for r in price_rows.values() if r["price"] is not None]
    if not priced:
        return True
    return _fetched_at_is_stale(max(r["fetched_at"] for r in priced))


def _has_cached_price(state: dict | None, platform: str) -> bool:
    """True if this game carries at least one usable (non-NULL) cached price on
    `platform` — i.e. its per-title lookup is resolved and need not be redone
    to make the game priceable."""
    if state is None:
        return False
    return any(r["price"] is not None for r in state["prices"].get(platform, {}).values())


def _miss_marker_is_fresh(price_rows: dict) -> bool:
    """True if a NULL-price row (a recorded search miss) is still inside the
    backoff window — the title was looked up, wasn't there, and should not
    burn another capped lookup slot yet."""
    return any(
        r["price"] is None
        and not _fetched_at_is_stale(r["fetched_at"], hours=_SWITCH2_MISS_RETRY_HOURS)
        for r in price_rows.values()
    )


def _switch2_lookup_pending(state: dict | None) -> bool:
    """True if this game's per-title switch2 lookup is still outstanding work:
    no usable price, and no in-window miss marker either."""
    rows = state["prices"].get("switch2", {}) if state is not None else {}
    if any(r["price"] is not None for r in rows.values()):
        return False
    return not _miss_marker_is_fresh(rows)


def _availability_is_known(igdb_platforms_json: str | None) -> bool:
    """True if games.igdb_platforms holds a usable release-platform list.

    Distinguishes "IGDB says this has no Switch release" (a non-empty list
    without 130/508 — a real answer) from "we have no platform list at all",
    which _available_platforms flattens into the same empty set and which
    would otherwise silently drop the game from the candidate count."""
    if not igdb_platforms_json:
        return False
    try:
        ids = json.loads(igdb_platforms_json)
    except ValueError:
        return False
    return isinstance(ids, list) and bool(ids)


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


def _available_platforms(igdb_platforms_json: str | None) -> set[str]:
    """Internal platforms a game is released on, per games.igdb_platforms."""
    from ..data.igdb import IGDB_TO_PLATFORM

    if not igdb_platforms_json:
        return set()
    try:
        ids = json.loads(igdb_platforms_json)
    except ValueError:
        return set()
    if not isinstance(ids, list):
        return set()
    return {IGDB_TO_PLATFORM[i] for i in ids if isinstance(i, int) and i in IGDB_TO_PLATFORM}


def _candidate_platforms(
    wishlisted_on: set[str], available: set[str], owned: set[str], hw_pref: list[str]
) -> set[str]:
    """Platforms worth pricing for one game: where it's wishlisted, plus any
    hardware-preference platform it's available on, priceable, and not
    already owned (no point recommending a purchase of an owned copy)."""
    candidates = set(wishlisted_on) & _PRICEABLE_PLATFORMS
    for platform in hw_pref:
        if (
            platform in _PRICEABLE_PLATFORMS
            and platform in available
            and platform not in owned
        ):
            candidates.add(platform)
    return candidates


def _option_passes_filters(
    option: dict, max_price: float | None, min_cut_pct: int | None
) -> bool:
    """True if a single priced option satisfies BOTH provided filters
    (whichever of max_price/min_cut_pct are not None)."""
    if max_price is not None and option["price"] > max_price:
        return False
    if min_cut_pct is None:
        return True
    return option["cut_pct"] is not None and option["cut_pct"] >= min_cut_pct


def _deal_has_qualifying_option(
    deal: dict, max_price: float | None, min_cut_pct: int | None
) -> bool:
    """True if the recommended option OR any alternative satisfies the
    filter(s) — a qualifying deal is never hidden just because the
    RECOMMENDED (e.g. hardware-preferred) platform happens to miss it."""
    options = [deal, *deal["alternatives"]]
    return any(_option_passes_filters(o, max_price, min_cut_pct) for o in options)


def _pick_recommended(
    options: list[dict], hw_pref: list[str], override_ratio: float
) -> tuple[dict, str]:
    """Choose which per-platform option to recommend.

    Preference order wins unless a non-preferred option's price drops
    strictly below override_ratio × the preferred price ("the deal is just
    too good"). options must be non-empty; prices are compared raw (no
    currency conversion — the caller flags mixed currencies)."""
    best = min(options, key=lambda o: o["price"])
    preferred = next(
        (
            min((o for o in options if o["platform"] == platform), key=lambda o: o["price"])
            for platform in hw_pref
            if any(o["platform"] == platform for o in options)
        ),
        None,
    )
    if preferred is None:
        return best, "cheapest available"
    if preferred["platform"] == best["platform"]:
        return preferred, f"cheapest available (also preferred platform {preferred['platform']})"
    if best["price"] < override_ratio * preferred["price"]:
        return best, (
            f"preference override: {best['platform']} at {best['price']} is below "
            f"{int(override_ratio * 100)}% of preferred {preferred['platform']} "
            f"price {preferred['price']}"
        )
    return preferred, (
        f"preferred platform {preferred['platform']} at {preferred['price']} "
        f"(cheapest elsewhere: {best['price']} on {best['platform']})"
    )


async def get_wishlist_deals(
    platform: str | None = None,
    max_price: float | None = None,
    min_cut_pct: int | None = None,
    refresh: bool = False,
    preference_override_ratio: float = _DEFAULT_OVERRIDE_RATIO,
) -> dict:
    """
    Current prices/deals for wishlist games, one entry per game, with a
    platform recommendation honoring hardware_preference.

    Prices come from IsThereAnyDeal (Steam wishlist items; covers Steam/GOG/
    Epic shops) and DekuDeals (switch2 items — both the shared-wishlist page
    and, for games wishlisted elsewhere that IGDB says also have a Switch
    release, per-title search lookups, capped per call). Cached 12h;
    refresh=True forces a live fetch. Each deal's flat fields describe the
    RECOMMENDED option (preferred platform unless another platform's price is
    below preference_override_ratio × the preferred price); other platforms'
    cheapest options appear in alternatives with the reasoning in
    recommendation_reason.

    Four counters describe the capped per-title switch2 lookup queue:
    switch2_lookups_performed (priced THIS call), switch2_lookups_deferred
    (the backlog LEFT AFTER it — still no price and no recorded miss, picked
    up by later calls; never-priced candidates take the capped slots before
    stale re-prices, so successive calls drain this instead of re-pricing the
    same newest-wishlisted titles), switch2_lookups_not_found (DekuDeals has no card for
    the title; negatively cached for _SWITCH2_MISS_RETRY_HOURS so a permanent
    miss stops consuming a lookup slot every call, which refresh=True does NOT
    override) and switch2_availability_unknown (wishlist games with no IGDB
    platform list at all, so their Switch availability is undecidable and they
    never become candidates — an enrichment gap, not a "no Switch release").
    Each is omitted when zero.

    A game with a recorded verdict (record_assessment) carries `assessment`
    (latest verdict, its date, the target price it named) and, when the best
    price in the SAME currency has reached that target,
    `below_assessed_target: true`. Annotation only — it never changes which
    option is recommended or which entries the filters keep.

    platform filters by where the game is WISHLISTED, not where the
    recommendation lands. max_price/min_cut_pct keep a game if
    ANY of its priced options — recommended or alternative — satisfies both
    given filters together; they never re-point the recommended fields, they
    only decide whether the entry is kept. Prices are never currency-converted.
    """
    resolved_platform = _validate_platform(platform, LIBRARY_PLATFORMS) if platform else None

    hw_pref_raw = await get_meta("hardware_preference")
    hw_pref: list[str] = json.loads(hw_pref_raw) if hw_pref_raw else []

    rows = await load_wishlist_with_prices(resolved_platform)
    games = _group_rows_by_game(rows)

    # Partition stale (game, platform) pricing needs by provider.
    steam_needs_refresh: dict[int, int] = {}      # appid -> game_id
    switch2_wishlist_needs: dict[int, str] = {}   # game_id -> name (on the deku wishlist page)
    switch2_search_pending: dict[int, str] = {}   # game_id -> name (never priced)
    switch2_search_stale: dict[int, str] = {}     # game_id -> name (stale re-price)
    availability_pending = 0
    switch2_availability_unknown = 0
    switch2_lookups_not_found = 0
    switch2_search_wanted = "switch2" in hw_pref

    for game_id, state in games.items():
        if state["igdb_cached_at"] is None:
            availability_pending += 1
        if (
            switch2_search_wanted
            and "switch2" not in state["wishlisted_on"]
            and "switch2" not in state["owned_platforms"]
            and not _availability_is_known(state["igdb_platforms"])
        ):
            # Not a candidate, but for want of data rather than because IGDB
            # said "no Switch release" — counted so the gap is attributable
            # instead of just missing from every number below.
            switch2_availability_unknown += 1
        candidates = _candidate_platforms(
            set(state["wishlisted_on"]),
            _available_platforms(state["igdb_platforms"]),
            state["owned_platforms"],
            hw_pref,
        )
        for cand in candidates:
            if not _platform_needs_refresh(state["prices"].get(cand, {}), refresh):
                continue
            if cand == "steam":
                if state["steam_appid"] is not None:
                    steam_needs_refresh[int(state["steam_appid"])] = game_id
            elif cand == "switch2":
                if "switch2" in state["wishlisted_on"]:
                    switch2_wishlist_needs[game_id] = state["name"]
                elif _miss_marker_is_fresh(state["prices"].get("switch2", {})):
                    # Confirmed miss inside the backoff window — deliberately
                    # NOT re-queued even under refresh=True, which is the whole
                    # point of the marker: a permanent miss must stop eating a
                    # capped lookup slot on every call.
                    switch2_lookups_not_found += 1
                elif _has_cached_price(state, "switch2"):
                    switch2_search_stale[game_id] = state["name"]
                else:
                    switch2_search_pending[game_id] = state["name"]

    # Never-priced candidates get first claim on the capped lookup slots;
    # stale re-prices queue behind them (each sub-queue keeps the loader's
    # newest-wishlisted-first order). As one wishlisted_at-DESC queue, the
    # newest candidates went stale and re-took every slot on each call spaced
    # past the price TTL, so the never-priced tail starved indefinitely — a
    # stale price still serves from cache, but an unpriced game serves nothing.
    switch2_search_needs = {**switch2_search_pending, **switch2_search_stale}

    # Whole per-title backlog for this call, kept before the cap truncates the
    # set actually looked up — the deferred counter is recomputed against it
    # AFTER the fetch, from what is still unpriced (see below).
    switch2_search_candidates = list(switch2_search_needs)
    if len(switch2_search_needs) > _MAX_SWITCH2_SEARCH_LOOKUPS:
        switch2_search_needs = dict(list(switch2_search_needs.items())[:_MAX_SWITCH2_SEARCH_LOOKUPS])

    price_refresh_errors: list[str] = []
    notes: dict[str, Any] = {}
    cache_updated = False
    switch2_lookups_performed = 0

    if steam_needs_refresh:
        if not is_itad_configured():
            notes["itad"] = "unconfigured"
        else:
            try:
                prices = await fetch_steam_prices(list(steam_needs_refresh.keys()))
            except Exception as exc:  # noqa: BLE001 - isolation boundary: any failure becomes an error record
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

    if switch2_wishlist_needs:
        try:
            prices_by_title = await fetch_wishlist_prices()
        except Exception as exc:  # noqa: BLE001 - isolation boundary: any failure becomes an error record
            logger.warning("DekuDeals price refresh failed: %s", exc)
            price_refresh_errors.append(f"dekudeals refresh failed: {exc}")
        else:
            config = await load_scrape_config("dekudeals")
            name_to_id = {name.lower(): gid for gid, name in switch2_wishlist_needs.items()}
            candidate_names = {name: name for name in name_to_id}
            upsert_rows = []
            for title, info in prices_by_title.items():
                matched_game_id = _match_wishlist_game_id(
                    title, candidate_names, name_to_id, cutoff=config.fuzzy_cutoff
                )
                if matched_game_id is None:
                    continue
                upsert_rows.append(_switch2_price_row(matched_game_id, info))
            if upsert_rows:
                await upsert_game_prices(upsert_rows)
                cache_updated = True

    if switch2_search_needs:
        try:
            by_title = await fetch_search_prices(list(switch2_search_needs.values()))
        except Exception as exc:  # noqa: BLE001 - fetch_search_prices is fail-soft; this is belt-and-braces
            logger.warning("DekuDeals search price refresh failed: %s", exc)
            price_refresh_errors.append(f"dekudeals search refresh failed: {exc}")
        else:
            name_to_id = {name: gid for gid, name in switch2_search_needs.items()}
            upsert_rows = []
            for title, search_info in by_title.items():
                if title not in name_to_id:
                    continue
                candidate_id = name_to_id[title]
                if search_info is not None:
                    upsert_rows.append(_switch2_price_row(candidate_id, search_info))
                    switch2_lookups_performed += 1
                elif not _has_cached_price(games.get(candidate_id), "switch2"):
                    # Confirmed miss (None, not "title absent" = fetch failed).
                    # Remembered as a NULL-price row so the next calls spend
                    # their capped lookups on candidates that can still
                    # resolve. Never written over a real cached price — that
                    # would blank a good price on a one-off search miss.
                    upsert_rows.append(_switch2_miss_row(candidate_id))
                    switch2_lookups_not_found += 1
            if upsert_rows:
                await upsert_game_prices(upsert_rows)
                cache_updated = True

    if cache_updated:
        rows = await load_wishlist_with_prices(resolved_platform)
        games = _group_rows_by_game(rows)

    # Post-call backlog, not a static "candidates minus cap" expression: a
    # candidate counts as deferred only while its lookup is still outstanding —
    # no usable price and no in-window miss marker. Anything the capped lookups
    # (this call or an earlier one) settled either way drops out, so the counter
    # drains to zero as the queue empties instead of reporting the same number
    # forever.
    switch2_lookups_deferred = sum(
        1 for gid in switch2_search_candidates if _switch2_lookup_pending(games.get(gid))
    )

    # Latest recorded verdict per wishlist game — one query for the whole
    # listing, never one per deal. Read-only annotation: an assessment never
    # changes which option is recommended, only what the reader knows about it.
    assessments = await load_latest_assessments(games)

    deals: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for game_id, state in games.items():
        options = []
        for price_platform, by_shop in state["prices"].items():
            if price_platform in state["owned_platforms"]:
                continue  # already owned there — never recommend buying it again
            priced = [r for r in by_shop.values() if r["price"] is not None]
            if not priced:
                continue
            cheapest = min(priced, key=lambda r: r["price"])
            options.append(
                {
                    "platform": price_platform,
                    "shop": cheapest["shop"],
                    "price": cheapest["price"],
                    "regular_price": cheapest["regular_price"],
                    "cut_pct": cheapest["cut_pct"],
                    "currency": cheapest["currency"],
                    "deal_url": cheapest["deal_url"],
                }
            )
        if not options:
            unpriced.append(state["name"])
            continue
        recommended, reason = _pick_recommended(options, hw_pref, preference_override_ratio)
        entry = {
            "game_id": game_id,
            "name": state["name"],
            **recommended,
            "wishlisted_at": min(state["wishlisted_on"].values()),
            "wishlisted_on": sorted(state["wishlisted_on"]),
            "recommendation_reason": reason,
            "alternatives": [o for o in options if o is not recommended],
        }
        assessment = assessments.get(game_id)
        if assessment is not None:
            entry["assessment"] = {
                "verdict": assessment["verdict"],
                "assessed_at": assessment["assessed_at"],
                "target_price": assessment["target_price"],
            }
            if _below_assessed_target(options, assessment):
                entry["below_assessed_target"] = True
        deals.append(entry)

    currencies = {
        c
        for d in deals
        for c in [d["currency"], *(a["currency"] for a in d["alternatives"])]
        if c is not None
    }

    if max_price is not None or min_cut_pct is not None:
        deals = [d for d in deals if _deal_has_qualifying_option(d, max_price, min_cut_pct)]

    deals.sort(key=lambda d: d["price"])

    response: dict[str, Any] = {
        "deals": deals,
        "unpriced": unpriced,
        "fetched_at": datetime.now(UTC).isoformat(),
        "count": len(deals),
    }
    if price_refresh_errors:
        response["price_refresh_errors"] = price_refresh_errors
    if switch2_lookups_performed:
        response["switch2_lookups_performed"] = switch2_lookups_performed
    if switch2_lookups_deferred:
        response["switch2_lookups_deferred"] = switch2_lookups_deferred
    if switch2_lookups_not_found:
        response["switch2_lookups_not_found"] = switch2_lookups_not_found
    if switch2_availability_unknown:
        response["switch2_availability_unknown"] = switch2_availability_unknown
    if availability_pending:
        response["availability_pending"] = availability_pending
    if len(currencies) > 1:
        response["currency_note"] = (
            f"deals span multiple currencies ({', '.join(sorted(currencies))}); "
            "max_price/min_cut_pct/preference_override_ratio are not currency-converted"
        )
    response.update(notes)
    return response


def _switch2_miss_row(game_id: int) -> dict:
    """A negative-cache row: same key a real DekuDeals price would occupy, with
    every price field NULL. Read back by `_miss_marker_is_fresh`; a later hit
    overwrites it in place (UNIQUE(game_id, platform, shop)), and every price
    reader already skips NULL-price rows."""
    return {
        "game_id": game_id,
        "platform": "switch2",
        "shop": "dekudeals",
        "price": None,
        "regular_price": None,
        "cut_pct": None,
        "currency": None,
        "deal_url": None,
    }


def _switch2_price_row(game_id: int, info: dict) -> dict:
    return {
        "game_id": game_id,
        "platform": "switch2",
        "shop": "dekudeals",
        "price": info.get("price"),
        "regular_price": info.get("regular_price"),
        "cut_pct": info.get("cut_pct"),
        "currency": info.get("currency"),
        "deal_url": info.get("deal_url"),
    }
