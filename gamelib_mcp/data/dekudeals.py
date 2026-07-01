"""Sync a DekuDeals shared wishlist into game_wishlist (switch2).

Nintendo has no official wishlist API. DekuDeals exposes a public, unauthenticated
JSON export of a shared wishlist page (append ".json" to the share URL), so this
reuses the same httpx-GET-and-fuzzy-match shape as backloggd.py rather than
reverse-engineering Nintendo's own eShop wishlist.

Configure DEKUDEALS_WISHLIST_URL to your share link, e.g.
https://www.dekudeals.com/wishlist/<share-id> (the ".json" suffix is added here).
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from .db import extract_best_fuzzy_key, get_db, upsert_wishlist_entry

DEKUDEALS_WISHLIST_URL = os.getenv("DEKUDEALS_WISHLIST_URL", "")
logger = logging.getLogger(__name__)


def is_dekudeals_configured() -> bool:
    return bool(os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL))


async def sync_dekudeals_wishlist() -> dict:
    """
    Fetch the configured DekuDeals shared wishlist and fuzzy-match titles to DB
    games, upserting a game_wishlist row for each on the switch2 platform.
    Returns stats.
    """
    wishlist_url = os.getenv("DEKUDEALS_WISHLIST_URL", DEKUDEALS_WISHLIST_URL)
    if not wishlist_url:
        return {
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": "DEKUDEALS_WISHLIST_URL is not set",
            "error_classification": "missing_configuration",
        }

    titles = await _fetch_wishlist_titles(wishlist_url)

    async with get_db() as db:
        game_rows = await db.execute_fetchall("SELECT id, name FROM games")
    name_to_id = {r["name"].lower(): r["id"] for r in game_rows}
    candidate_names = {name: name for name in name_to_id}

    matched = skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    for title in titles:
        game_id = _match_game_id(title, candidate_names, name_to_id)
        if game_id is None:
            logger.debug("No match for DekuDeals wishlist title: %s", title)
            skipped += 1
            continue
        await upsert_wishlist_entry(game_id, "switch2", wishlisted_at=now, source="dekudeals")
        matched += 1

    return {"matched": matched, "skipped": skipped, "total_scraped": len(titles)}


async def _fetch_wishlist_titles(wishlist_url: str) -> list[str]:
    """Fetch the DekuDeals wishlist JSON export and return a list of game titles."""
    url = wishlist_url.rstrip("/")
    if not url.endswith(".json"):
        url += ".json"

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "gamelib-mcp/1.0"}) as client:
        try:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("DekuDeals wishlist fetch failed: %s", e)
            return []
        payload = resp.json()

    items = payload if isinstance(payload, list) else payload.get("items", payload.get("games", []))
    titles = []
    for item in items:
        if isinstance(item, str):
            titles.append(item)
        elif isinstance(item, dict):
            title = item.get("title") or item.get("name")
            if title:
                titles.append(title)
    return titles


def _match_game_id(title: str, candidate_names: dict[str, str], name_to_id: dict) -> int | None:
    """Fuzzy-match a DekuDeals title to a game in the DB, returns games.id."""
    title_lower = title.lower()

    if title_lower in name_to_id:
        return name_to_id[title_lower]

    match = extract_best_fuzzy_key(title_lower, candidate_names, cutoff=85)
    if match:
        return name_to_id[match]

    return None
