"""Lazy SteamSpy user-curated tag fetch."""
import json
import logging
from datetime import UTC, datetime

import httpx

from .db import (
    get_db,
    get_manual_overrides,
    get_steam_platform_row_by_appid,
    upsert_steam_platform_data,
)
from .tag_synonyms import canonical_tag
from .tags import is_feature_flag

STEAMSPY_API = "https://steamspy.com/api.php"
CACHE_DAYS = 30
TOP_N = 20
logger = logging.getLogger(__name__)


async def enrich_steamspy(appid: int) -> list[str] | None:
    """Fetch SteamSpy tags and merge into games.tags. Returns merged tag list or None."""
    row = await get_steam_platform_row_by_appid(appid)
    if row is None:
        return None

    if row and _is_fresh(row["steamspy_cached_at"], CACHE_DAYS):
        return json.loads(row["tags"]) if row["tags"] else None

    now = datetime.now(UTC).isoformat()
    existing = json.loads(row["tags"]) if row and row["tags"] else []

    spy_tags = await _fetch_steamspy(appid)
    if spy_tags:
        # Top N by votes, SteamSpy tags first
        top = [t for t, _ in sorted(spy_tags.items(), key=lambda x: -x[1])[:TOP_N]]
        merged = _merge_tags(top, existing)
    else:
        merged = existing  # preserve on failure

    # Always write cached_at — on failure this acts as a backoff marker so the
    # background worker doesn't immediately re-claim the row and hot-loop.
    await upsert_steam_platform_data(row["game_platform_id"], steamspy_cached_at=now)

    async with get_db() as db:
        # Don't overwrite tags the user set via update_game.
        if "tags" not in await get_manual_overrides(db, row["game_id"]):
            await db.execute(
                "UPDATE games SET tags = ? WHERE id = ?",
                (json.dumps(merged) if merged else row["tags"], row["game_id"]),
            )
            await db.commit()

    return merged or None


async def _fetch_steamspy(appid: int) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                STEAMSPY_API, params={"request": "appdetails", "appid": appid}
            )
            resp.raise_for_status()
            return resp.json().get("tags") or None
    except Exception as e:
        logger.warning("SteamSpy fetch failed for appid %d: %s", appid, e)
        return None


async def fetch_steamspy_name(appid: int) -> str | None:
    """The app's name per SteamSpy, or None when unknown/unreachable.

    SteamSpy retains data for retired/delisted GAMES (it tracked them while
    live) but generally has nothing for DLC and tools — which makes a non-null
    name here double as a "this was a real game" signal for apps whose store
    appdetails no longer exist. Used by the Steam license audit to name owned
    apps the store API has forgotten.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                STEAMSPY_API, params={"request": "appdetails", "appid": appid}
            )
            resp.raise_for_status()
            name = resp.json().get("name")
    except Exception as e:
        logger.warning("SteamSpy name fetch failed for appid %d: %s", appid, e)
        return None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _merge_tags(spy_tags: list[str], existing: list[str]) -> list[str]:
    seen = set()
    result = []
    for t in spy_tags + existing:
        # Check feature-flag membership on the original surface form (the flag set
        # uses specific punctuation), then store the canonical form so SteamSpy,
        # IGDB, and steam_store share one tag vocabulary.
        if is_feature_flag(t):
            continue
        c = canonical_tag(t)
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at:
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(UTC) - dt).total_seconds() < days * 86400
    except ValueError:
        return False
