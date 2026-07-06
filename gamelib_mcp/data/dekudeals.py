"""Sync a DekuDeals shared wishlist into game_wishlist (switch2).

Nintendo has no official wishlist API. DekuDeals exposes a public, unauthenticated
JSON export of a shared wishlist page (append ".json" to the share URL), so this
reuses the same httpx-GET-and-fuzzy-match shape as backloggd.py rather than
reverse-engineering Nintendo's own eShop wishlist.

Configure DEKUDEALS_WISHLIST_URL to your share link, e.g.
https://www.dekudeals.com/wishlist/<share-id> (the ".json" suffix is added here).

Confirmed export shape (2026-07-01): {"items": [{"name", "link", "added_at"},
...], "default_desired_price": ...}. There is no numeric/NSUID identifier
anywhere in it — "link" is DekuDeals' own slug (e.g. /items/pikmin-4), unrelated
to Nintendo's applicationId (nintendo_title_id) used for VGCS ownership — so
name matching is the only available bridge to games in the DB, not a shortcut
taken for convenience. A title with no existing fuzzy match gets a fresh
wishlist-only games row (see sync_dekudeals_wishlist) rather than being
skipped forever — most switch2-wishlisted titles are Nintendo exclusives never
synced from any other platform, so they have no games row yet at all.
"added_at" is each item's real wishlist-add time and is used as-is for
wishlisted_at (it's already ISO 8601 UTC).

Removal reconciliation: a title removed from the DekuDeals wishlist is deleted
from game_wishlist too, via delete_stale_wishlist_entries — but only after a
successful fetch. _fetch_wishlist_items raises on failure rather than
swallowing it to an empty list, specifically so a transient fetch error can't
be mistaken for "the wishlist is now empty" and wipe every switch2 entry.

This module also prices arbitrary titles NOT on the shared wishlist via
DekuDeals' public search page (fetch_search_prices) — used for a game
wishlisted on another platform that also has a Switch release, so it can get
a switch2 price quote too. DekuDeals is a multi-platform tracker (Switch,
PlayStation, Xbox, PC), so search results are always scoped with
`filter[platform]=switch_2` first, falling back to `filter[platform]=switch`
on a miss (see `_SEARCH_PLATFORM_FILTERS`) — an unfiltered search can surface
a card whose price/sale-status belongs to a different platform entirely for
a multi-platform title. Unlike the wishlist scrape, per-title fetch/parse
failures and non-matches are skipped rather than raised, since there is no
removal reconciliation downstream for this path.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from .db import (
    delete_stale_wishlist_entries,
    extract_best_fuzzy_key,
    get_db,
    upsert_game,
    upsert_wishlist_entry,
)
from .scrape_config import (
    DekuDealsScrapeConfig,
    DisallowedHostError,
    fetch_allowlisted,
    load_scrape_config,
)
from .title_normalization import prepare_catalog_title

# Leading currency symbol -> ISO 4217 code. Extend if another symbol shows up
# in a live fetch; these three cover what's been confirmed against real pages.
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}

# deal_url is built from a scraped href; an absolute (non "/"-relative) href
# is only trusted if it actually points at DekuDeals — mirrors the host-check
# scrape_config.py's _validate_field applies to url_template overrides (see
# ALLOWED_HOSTS there). This constant covers deal_url specifically (a scraped
# value, not a config field), so it stays separate from ALLOWED_HOSTS even
# though the two host sets are the same.
_DEKUDEALS_HOSTS = frozenset({"dekudeals.com", "www.dekudeals.com"})

DEKUDEALS_WISHLIST_URL = os.getenv("DEKUDEALS_WISHLIST_URL", "")
logger = logging.getLogger(__name__)

# Politeness delay between per-title search requests (fetch_search_prices).
_SEARCH_REQUEST_DELAY_SECONDS = 0.5

# DekuDeals is a multi-platform deal tracker (Switch, PlayStation, Xbox, PC), so an
# UNFILTERED search for a multi-platform title can surface a card whose displayed
# price/sale-status belongs to a different platform, not Switch. DekuDeals supports
# a `filter[platform]=<value>` search query param to scope results; live checks
# (2026-07-03) confirm `switch_2` and `switch` are DISJOINT facets, not a
# superset/subset relationship — a Switch-2-exclusive title (e.g. "Mario Kart
# World") only appears under `switch_2`, while an original-Switch title (e.g.
# "Hades") only appears under `switch`. Since this codebase's "switch2" platform
# value deliberately covers both the native Switch 2 catalog and the
# backward-compatible original-Switch catalog (mirrors igdb.py's
# PLATFORM_TO_IGDB_ANY["switch2"] = (IGDB_PLATFORM_SWITCH2, IGDB_PLATFORM_SWITCH),
# i.e. try id 508 first, then 130), fetch_search_prices tries `switch_2` first and
# falls back to `switch` only if the first attempt has no match.
_SEARCH_PLATFORM_FILTERS = ("switch_2", "switch")


def is_dekudeals_configured() -> bool:
    return bool(os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL))


async def sync_dekudeals_wishlist() -> dict:
    """
    Fetch the configured DekuDeals shared wishlist and fuzzy-match titles to DB
    games, upserting a game_wishlist row for each on the switch2 platform, and
    removing any prior dekudeals-sourced entry no longer in the fetched list.
    A title with no existing fuzzy match gets a fresh wishlist-only games row
    (mirrors steam_wishlist.fetch_wishlist's handling of a new appid) — most
    switch2-wishlisted titles are Nintendo exclusives never synced from any
    other platform, so they have no games row yet at all. Returns stats.
    Raises if the fetch itself fails (see _fetch_wishlist_items) rather than
    treating a failed fetch as an empty wishlist.
    """
    wishlist_url = os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL)
    if not wishlist_url:
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "removed": 0,
            "sync_status": "unconfigured",
            "error_summary": "DEKUDEALS_WISHLIST_URL is not set",
            "error_classification": "missing_configuration",
        }

    config = await load_scrape_config("dekudeals")
    items = await _fetch_wishlist_items(wishlist_url, config)

    async with get_db() as db:
        game_rows = await db.execute_fetchall("SELECT id, name FROM games")
    name_to_id = {r["name"].lower(): r["id"] for r in game_rows}
    candidate_names = {name: name for name in name_to_id}

    added = matched = skipped = 0
    fallback_now = datetime.now(timezone.utc).isoformat()
    resolved_game_ids: set[int] = set()

    for item in items:
        game_id = _match_game_id(
            item["title"], candidate_names, name_to_id, cutoff=config.fuzzy_cutoff
        )
        if game_id is not None:
            matched += 1
        else:
            prepared_title = prepare_catalog_title(item["title"])
            if prepared_title is None:
                logger.debug("Unresolvable DekuDeals wishlist title: %s", item["title"])
                skipped += 1
                continue
            game_id = await upsert_game(None, prepared_title)
            added += 1
        await upsert_wishlist_entry(
            game_id, "switch2", wishlisted_at=item["added_at"] or fallback_now, source="dekudeals"
        )
        resolved_game_ids.add(game_id)

    # Only reached once _fetch_wishlist_items has succeeded, so an empty/partial
    # items list here genuinely reflects the current upstream wishlist.
    removed = await delete_stale_wishlist_entries("switch2", "dekudeals", resolved_game_ids)

    return {
        "added": added,
        "matched": matched,
        "skipped": skipped,
        "removed": removed,
        "total_scraped": len(items),
    }


async def _fetch_wishlist_items(
    wishlist_url: str, config: DekuDealsScrapeConfig | None = None
) -> list[dict]:
    """Fetch the DekuDeals wishlist JSON export.

    Returns a list of {"title": str, "added_at": str | None} — added_at is the
    item's own wishlist-add timestamp when the export provides one (confirmed
    present in the wishlist export; None is just a defensive fallback for
    other DekuDeals export shapes, e.g. a plain title list). Propagates fetch
    failures (raises) rather than swallowing them to an empty list — the
    caller's removal reconciliation must not mistake a network hiccup for a
    genuinely empty wishlist.
    """
    url = wishlist_url.rstrip("/")
    if not url.endswith(".json"):
        url += ".json"

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "gamelib-mcp/1.0"}) as client:
        resp = await fetch_allowlisted(client, url, provider="dekudeals")
        resp.raise_for_status()
        payload = resp.json()

    return _parse_wishlist_payload(payload, config)


def _parse_wishlist_payload(
    payload: Any, config: DekuDealsScrapeConfig | None = None
) -> list[dict]:
    """Extract {title, added_at} rows from a wishlist export payload. Pure."""
    if config is None:
        config = DekuDealsScrapeConfig()

    if isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []
        for key in config.items_keys:
            candidate = payload.get(key)
            if candidate:
                raw_items = candidate
                break

    items = []
    for item in raw_items:
        if isinstance(item, str):
            items.append({"title": item, "added_at": None})
        elif isinstance(item, dict):
            title = None
            for key in config.title_keys:
                title = item.get(key)
                if title:
                    break
            if title:
                items.append({"title": title, "added_at": item.get(config.added_at_key)})
    return items


def _parse_price_text(text: str) -> tuple[float, str] | None:
    """Parse a price string like '€59,99' into (59.99, "EUR"). None if unparseable.

    Detects currency from the leading symbol, then normalizes the decimal
    separator: this account's locale renders prices with a comma decimal
    (e.g. "€59,99" == 59.99 EUR, not 5,999) rather than the plan's originally
    assumed "$29.99"-style dot decimal, so both must be handled.
    """
    stripped = text.strip()
    if not stripped:
        return None
    currency = _CURRENCY_SYMBOLS.get(stripped[0])
    if currency is None:
        return None
    numeric = stripped[1:].strip()
    # Comma-as-decimal locales never group thousands at this price scale, so a
    # plain comma->dot swap is sufficient (no "1.234,56" grouping to handle).
    if "," in numeric and "." not in numeric:
        numeric = numeric.replace(",", ".")
    try:
        return float(numeric), currency
    except ValueError:
        return None


def _parse_cut_pct(text: str) -> int | None:
    """Parse a discount badge like '-50%' into 50. None if unparseable."""
    stripped = text.strip().lstrip("-").rstrip("%").strip()
    try:
        return int(stripped)
    except ValueError:
        return None


def _parse_wishlist_prices(html: str, config: DekuDealsScrapeConfig | None = None) -> dict[str, dict]:
    """Pure HTML->prices parse, selector-driven so the heal tools can fix drift.

    Returns {title: {"price", "regular_price", "cut_pct", "currency", "deal_url"}}.
    Cards without a resolvable title or price are skipped defensively rather
    than raising, per this module's existing convention.
    """
    if config is None:
        config = DekuDealsScrapeConfig()

    soup = BeautifulSoup(html, "lxml")
    results: dict[str, dict] = {}

    for card in soup.select(config.wishlist_item_selector):
        title_el = card.select_one(config.item_title_selector)
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            logger.debug("DekuDeals wishlist card has no resolvable title; skipping")
            continue

        price_el = card.select_one(config.price_selector)
        parsed_price = _parse_price_text(price_el.get_text()) if price_el else None
        if parsed_price is None:
            logger.debug("DekuDeals wishlist card %r has no parsable price; skipping", title)
            continue
        price, currency = parsed_price

        regular_price = price
        regular_price_el = card.select_one(config.regular_price_selector)
        if regular_price_el is not None:
            parsed_regular = _parse_price_text(regular_price_el.get_text())
            if parsed_regular is not None:
                regular_price = parsed_regular[0]

        cut_pct: int | None = None
        cut_pct_el = card.select_one(config.cut_pct_selector)
        if cut_pct_el is not None:
            cut_pct = _parse_cut_pct(cut_pct_el.get_text())

        deal_url = ""
        link_el = card.select_one(config.item_link_selector)
        if isinstance(link_el, Tag):
            href = link_el.get("href")
            if isinstance(href, str) and href:
                if href.startswith("/"):
                    deal_url = "https://www.dekudeals.com" + href
                elif urlsplit(href).hostname in _DEKUDEALS_HOSTS:
                    deal_url = href
                # else: absolute URL pointing somewhere else — untrusted,
                # leave deal_url as "" rather than passing it through.

        results[title] = {
            "price": price,
            "regular_price": regular_price,
            "cut_pct": cut_pct,
            "currency": currency,
            "deal_url": deal_url,
        }

    return results


async def fetch_wishlist_prices() -> dict[str, dict]:
    """Fetch the shared wishlist HTML page and parse current prices per title.

    Raises on fetch failure (same rationale as _fetch_wishlist_items: a
    transient error must not be mistaken for "nothing is on sale").
    """
    wishlist_url = os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL)
    url = wishlist_url.rstrip("/")
    if url.endswith(".json"):
        url = url[: -len(".json")]

    config = await load_scrape_config("dekudeals")

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "gamelib-mcp/1.0"}) as client:
        resp = await fetch_allowlisted(client, url, provider="dekudeals")
        resp.raise_for_status()

    return _parse_wishlist_prices(resp.text, config)


def _match_game_id(
    title: str, candidate_names: dict[str, str], name_to_id: dict, cutoff: int = 85
) -> int | None:
    """Fuzzy-match a DekuDeals title to a game in the DB, returns games.id."""
    title_lower = title.lower()

    if title_lower in name_to_id:
        return name_to_id[title_lower]

    match = extract_best_fuzzy_key(title_lower, candidate_names, cutoff=cutoff)
    if match:
        return name_to_id[match]

    return None


def _match_search_title(title: str, prices: dict[str, dict], cutoff: int) -> str | None:
    """Best search-result title for a requested title, or None. Exact
    (case-insensitive) first, then the same fuzzy matcher the wishlist
    sync uses."""
    by_lower = {t.lower(): t for t in prices}
    title_lower = title.lower()
    if title_lower in by_lower:
        return by_lower[title_lower]
    match = extract_best_fuzzy_key(title_lower, {k: k for k in by_lower}, cutoff=cutoff)
    return by_lower[match] if match else None


async def fetch_search_prices(titles: list[str]) -> dict[str, dict]:
    """Current switch2 prices for arbitrary titles via the public DekuDeals
    search page — used for games NOT on the shared wishlist (e.g. a
    Steam-wishlisted game that also has a Switch release).

    DekuDeals is a multi-platform tracker, so results are always scoped with
    `filter[platform]=<value>` to avoid caching a price/sale-status that
    actually belongs to a different platform's card for the same title. Per
    title, tries each filter in `_SEARCH_PLATFORM_FILTERS` (switch_2, then
    switch) in order, stopping at the first one that yields a matched card —
    so most titles cost one GET, and only a switch_2-miss costs a second. One
    GET per (title, filter) attempt; results parse with the same selector
    config as the wishlist page (identical card markup). Returns
    {requested_title: price_dict}. Per-attempt failures and non-matches are
    skipped rather than raised: unlike the wishlist scrape there is no
    removal reconciliation downstream, so a miss just leaves that item
    unpriced.
    """
    if not titles:
        return {}
    config = await load_scrape_config("dekudeals")
    results: dict[str, dict] = {}
    request_count = 0
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "gamelib-mcp/1.0"}) as client:
        for title in titles:
            base_url = config.search_url_template.format(query=quote_plus(title))
            for platform_filter in _SEARCH_PLATFORM_FILTERS:
                if request_count:
                    await asyncio.sleep(_SEARCH_REQUEST_DELAY_SECONDS)
                request_count += 1
                url = f"{base_url}&filter%5Bplatform%5D={platform_filter}"
                try:
                    resp = await fetch_allowlisted(client, url, provider="dekudeals")
                    resp.raise_for_status()
                except (httpx.HTTPError, DisallowedHostError) as exc:
                    logger.warning(
                        "DekuDeals search failed for %r (filter=%s): %s", title, platform_filter, exc
                    )
                    continue
                prices = _parse_wishlist_prices(resp.text, config)
                matched = _match_search_title(title, prices, cutoff=config.fuzzy_cutoff)
                if matched is not None:
                    results[title] = prices[matched]
                    break
    return results
