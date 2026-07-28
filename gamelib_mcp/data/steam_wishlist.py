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

date_added is read defensively (_parse_steam_added_at accepts epoch or ISO,
else falls back to sync time) — unlike the DekuDeals export, this endpoint's
exact response shape hasn't been confirmed against a live account yet.

Removal reconciliation: a game taken off your Steam wishlist (without being
bought) is deleted from game_wishlist too, via delete_stale_wishlist_entries —
but only when every fetched item resolved to a game_id this round. If any
item couldn't be resolved (a malformed entry, or fetch_app_name failing for an
unowned item — the Steam Store lookup shares a rate-limited gate, so this is a
real possibility, not just a theoretical one), the removal pass is skipped
entirely rather than risk deleting a wishlist entry that's still there and we
simply failed to re-confirm.
"""

import logging
import os
from datetime import UTC, datetime

import httpx

from .db import (
    STEAM_APP_ID,
    delete_stale_wishlist_entries,
    get_game_by_identifier,
    upsert_game,
    upsert_wishlist_entry,
)
from .steam_store import fetch_app_name
from .steam_xml import STEAM_API_KEY, STEAM_ID
from .title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"


def _parse_steam_added_at(value) -> str | None:
    """Best-effort parse of a wishlist item's date_added into an ISO string.

    Steam Web API timestamps are conventionally Unix epoch seconds (int, or a
    numeric string); accept a plain ISO string too in case the field is ever
    returned pre-formatted. Returns None if absent/unparseable so the caller
    can fall back to sync time — never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        if value.isdigit():
            return _parse_steam_added_at(int(value))
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return None
        return value
    return None


async def fetch_wishlist() -> dict:
    """Fetch the Steam wishlist and upsert entries into game_wishlist.

    Returns {"added": int, "matched": int, "skipped": int, "removed": int}, or
    an "unconfigured" status dict (matching sync_dekudeals_wishlist's shape)
    if STEAM_API_KEY/STEAM_ID aren't set. removed is 0 whenever the removal
    reconciliation didn't run (see module docstring).
    """
    steam_api_key = os.getenv("STEAM_API_KEY", STEAM_API_KEY)
    steam_id = os.getenv("STEAM_ID", STEAM_ID)
    if not steam_api_key or not steam_id:
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "removed": 0,
            "sync_status": "unconfigured",
            "error_summary": "STEAM_API_KEY and STEAM_ID environment variables must be set",
            "error_classification": "missing_configuration",
        }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            WISHLIST_URL,
            params={"key": steam_api_key, "steamid": steam_id},
        )
        resp.raise_for_status()
        items = resp.json().get("response", {}).get("items", [])

        added = matched = skipped = 0
        fallback_now = datetime.now(UTC).isoformat()
        resolved_game_ids: set[int] = set()
        all_resolved = True

        for item in items:
            appid = item.get("appid")
            if appid is None:
                skipped += 1
                all_resolved = False
                continue
            item_added_at = _parse_steam_added_at(item.get("date_added")) or fallback_now

            existing = await get_game_by_identifier(STEAM_APP_ID, str(appid))
            if existing is not None:
                await upsert_wishlist_entry(
                    existing["id"], "steam", wishlisted_at=item_added_at, source="steam", store_identifier=str(appid)
                )
                matched += 1
                resolved_game_ids.add(existing["id"])
                continue

            name = await fetch_app_name(appid, client=client)
            prepared_title = prepare_catalog_title(name) if name else None
            if prepared_title is None:
                skipped += 1
                all_resolved = False
                continue

            game_id = await upsert_game(appid, prepared_title)
            await upsert_wishlist_entry(game_id, "steam", wishlisted_at=item_added_at, source="steam", store_identifier=str(appid))
            added += 1
            resolved_game_ids.add(game_id)

    removed = 0
    if all_resolved:
        removed = await delete_stale_wishlist_entries("steam", "steam", resolved_game_ids)
    elif items:
        logger.info(
            "Skipping Steam wishlist removal-reconciliation: %d item(s) unresolved this sync", skipped
        )

    logger.info(
        "Steam wishlist sync: added=%d matched=%d skipped=%d removed=%d", added, matched, skipped, removed
    )
    return {"added": added, "matched": matched, "skipped": skipped, "removed": removed}
