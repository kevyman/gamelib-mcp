"""Meta-KV-cached IGDB series-member lookups (7-day TTL, no schema migration).

Member lists for an IGDB collection/franchise are cached under
``meta`` keys ``series_members:{kind}:{igdb_id}`` so `discover_series_gaps`
doesn't re-fetch a series' full member list on every call.
"""

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .db import get_meta, set_meta
from .igdb import IGDBRequestFailure, SeriesMember, fetch_series_members

logger = logging.getLogger(__name__)

SERIES_CACHE_TTL_DAYS = 7


def _cache_key(kind: str, series_igdb_id: int) -> str:
    return f"series_members:{kind}:{series_igdb_id}"


def _parse_cache(raw: str | None) -> tuple[datetime, list[SeriesMember]] | None:
    """Parse a cached meta value, treating any malformed entry as absent."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        members = [SeriesMember(**m) for m in data["members"]]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return fetched_at, members


async def get_series_members_cached(
    kind: str, series_igdb_id: int, refresh: bool = False
) -> list[SeriesMember]:
    """fetch_series_members with a meta-KV cache (7-day TTL).

    Cache hit within TTL returns without a network call. On fetch failure with
    a stale cache present, serves the stale copy (logged); with no cache, the
    failure propagates.
    """
    key = _cache_key(kind, series_igdb_id)
    cached = _parse_cache(await get_meta(key))

    if not refresh and cached is not None:
        fetched_at, members = cached
        age = datetime.now(timezone.utc) - fetched_at
        if age < timedelta(days=SERIES_CACHE_TTL_DAYS):
            return members

    try:
        members = await fetch_series_members(kind, series_igdb_id)
    except IGDBRequestFailure:
        if cached is not None:
            logger.warning(
                "IGDB series-member fetch failed for %s %s; serving stale cache",
                kind,
                series_igdb_id,
                exc_info=True,
            )
            return cached[1]
        raise

    now = datetime.now(timezone.utc).isoformat()
    await set_meta(
        key,
        json.dumps({"fetched_at": now, "members": [asdict(m) for m in members]}),
    )
    return members
