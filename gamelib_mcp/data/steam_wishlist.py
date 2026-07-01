"""Fetch Steam wishlist via IWishlistService/GetWishlist.

Auth reuses STEAM_API_KEY/STEAM_ID (same as steam_xml.py's owned-games fetch).
Unlike GetOwnedGames, this endpoint returns only appid/priority/date_added —
no title — so a wishlist item with no existing game_platforms row needs a
follow-up Steam Store lookup (steam_store.fetch_app_name) to name it.

A wishlist item that isn't owned anywhere yet gets a games row but no
game_platforms row (see game_wishlist's schema note) — so unlike an owned sync,
there's no steam_appid identifier to attach it to. Re-syncs before purchase
fall back to upsert_game's exact-name matching to avoid duplicating the game
row, the same fallback GOG already relies on for lacking a stable store id.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

from .db import (
    STEAM_APP_ID,
    get_game_by_identifier,
    upsert_game,
    upsert_wishlist_entry,
)
from .steam_store import fetch_app_name
from .steam_xml import STEAM_API_KEY, STEAM_ID
from .title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"


async def fetch_wishlist() -> dict:
    """Fetch the Steam wishlist and upsert entries into game_wishlist.

    Returns {"added": int, "matched": int, "skipped": int}.
    """
    steam_api_key = os.getenv("STEAM_API_KEY", STEAM_API_KEY)
    steam_id = os.getenv("STEAM_ID", STEAM_ID)
    if not steam_api_key or not steam_id:
        raise ValueError("STEAM_API_KEY and STEAM_ID environment variables must be set")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            WISHLIST_URL,
            params={"key": steam_api_key, "steamid": steam_id},
        )
        resp.raise_for_status()
        items = resp.json().get("response", {}).get("items", [])

        added = matched = skipped = 0
        now = datetime.now(timezone.utc).isoformat()

        for item in items:
            appid = item.get("appid")
            if appid is None:
                skipped += 1
                continue

            existing = await get_game_by_identifier(STEAM_APP_ID, str(appid))
            if existing is not None:
                await upsert_wishlist_entry(existing["id"], "steam", wishlisted_at=now, source="steam")
                matched += 1
                continue

            name = await fetch_app_name(appid, client=client)
            prepared_title = prepare_catalog_title(name) if name else None
            if prepared_title is None:
                skipped += 1
                continue

            game_id = await upsert_game(appid, prepared_title)
            await upsert_wishlist_entry(game_id, "steam", wishlisted_at=now, source="steam")
            added += 1

    logger.info("Steam wishlist sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
