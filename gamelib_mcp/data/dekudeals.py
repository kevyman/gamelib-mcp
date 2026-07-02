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
name matching is the only available bridge to owned switch2 games, not a
shortcut taken for convenience. "added_at" is each item's real wishlist-add
time and is used as-is for wishlisted_at (it's already ISO 8601 UTC).

Removal reconciliation: a title removed from the DekuDeals wishlist is deleted
from game_wishlist too, via delete_stale_wishlist_entries — but only after a
successful fetch. _fetch_wishlist_items raises on failure rather than
swallowing it to an empty list, specifically so a transient fetch error can't
be mistaken for "the wishlist is now empty" and wipe every switch2 entry.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .db import (
    delete_stale_wishlist_entries,
    extract_best_fuzzy_key,
    get_db,
    upsert_wishlist_entry,
)
from .scrape_config import DekuDealsScrapeConfig, load_scrape_config

DEKUDEALS_WISHLIST_URL = os.getenv("DEKUDEALS_WISHLIST_URL", "")
logger = logging.getLogger(__name__)


def is_dekudeals_configured() -> bool:
    return bool(os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL))


async def sync_dekudeals_wishlist() -> dict:
    """
    Fetch the configured DekuDeals shared wishlist and fuzzy-match titles to DB
    games, upserting a game_wishlist row for each on the switch2 platform, and
    removing any prior dekudeals-sourced entry no longer in the fetched list.
    Returns stats. Raises if the fetch itself fails (see _fetch_wishlist_items)
    rather than treating a failed fetch as an empty wishlist.
    """
    wishlist_url = os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL)
    if not wishlist_url:
        return {
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

    matched = skipped = 0
    fallback_now = datetime.now(timezone.utc).isoformat()
    resolved_game_ids: set[int] = set()

    for item in items:
        game_id = _match_game_id(
            item["title"], candidate_names, name_to_id, cutoff=config.fuzzy_cutoff
        )
        if game_id is None:
            logger.debug("No match for DekuDeals wishlist title: %s", item["title"])
            skipped += 1
            continue
        await upsert_wishlist_entry(
            game_id, "switch2", wishlisted_at=item["added_at"] or fallback_now, source="dekudeals"
        )
        matched += 1
        resolved_game_ids.add(game_id)

    # Only reached once _fetch_wishlist_items has succeeded, so an empty/partial
    # items list here genuinely reflects the current upstream wishlist.
    removed = await delete_stale_wishlist_entries("switch2", "dekudeals", resolved_game_ids)

    return {"matched": matched, "skipped": skipped, "removed": removed, "total_scraped": len(items)}


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
        resp = await client.get(url, follow_redirects=True)
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
