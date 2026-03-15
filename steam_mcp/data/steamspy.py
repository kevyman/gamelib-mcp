"""Lazy SteamSpy user-curated tag fetch."""
import json
import logging
from datetime import datetime, timezone

import httpx

from .db import get_db

STEAMSPY_API = "https://steamspy.com/api.php"
CACHE_DAYS = 30
TOP_N = 15
logger = logging.getLogger(__name__)


async def enrich_steamspy(appid: int) -> list[str] | None:
    """Fetch SteamSpy tags and merge into games.tags. Returns merged tag list or None."""
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT steamspy_cached_at, tags FROM games WHERE appid = ?", (appid,)
        )
    if row and _is_fresh(row["steamspy_cached_at"], CACHE_DAYS):
        return json.loads(row["tags"]) if row["tags"] else None

    now = datetime.now(timezone.utc).isoformat()
    existing = json.loads(row["tags"]) if row and row["tags"] else []

    spy_tags = await _fetch_steamspy(appid)
    if spy_tags:
        # Top N by votes, SteamSpy tags first
        top = [t for t, _ in sorted(spy_tags.items(), key=lambda x: -x[1])[:TOP_N]]
        merged = _merge_tags(top, existing)
    else:
        merged = existing  # preserve on failure

    async with get_db() as db:
        await db.execute(
            "UPDATE games SET tags = ?, steamspy_cached_at = ? WHERE appid = ?",
            (json.dumps(merged) if merged else row["tags"], now, appid),
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


def _merge_tags(spy_tags: list[str], existing: list[str]) -> list[str]:
    seen = set()
    result = []
    for t in spy_tags + existing:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            result.append(t)
    return result


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at:
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() < days * 86400
    except ValueError:
        return False
