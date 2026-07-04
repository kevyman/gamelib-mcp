"""Meta-KV-cached IGDB series-member lookups (7-day TTL, no schema migration).

Member lists for an IGDB collection/franchise are cached under
``meta`` keys ``series_members_v2:{kind}:{igdb_id}`` so `discover_series_gaps`
doesn't re-fetch a series' full member list on every call. The payload also
carries a version-parent alias map (edition igdb_id -> canonical member
igdb_id) so an owned edition-specific IGDB entry (e.g. "The Witcher: Enhanced
Edition") can be recognized as owning its canonical series member even though
it isn't itself in the member list and typically has no series membership row
of its own (see fetch_version_parent_aliases). The cache key was bumped from
``series_members:`` to ``series_members_v2:`` when aliases were added so any
pre-existing cache entry (necessarily alias-less) is a clean cache miss rather
than being silently served without alias data.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .db import get_meta, set_meta
from .igdb import (
    IGDBRequestFailure,
    SeriesMember,
    fetch_series_members,
    fetch_version_parent_aliases,
)

logger = logging.getLogger(__name__)

SERIES_CACHE_TTL_DAYS = 7


@dataclass(frozen=True)
class SeriesMembersResult:
    members: list[SeriesMember] = field(default_factory=list)
    # edition igdb_id -> canonical member igdb_id (see fetch_version_parent_aliases)
    aliases: dict[int, int] = field(default_factory=dict)


def _cache_key(kind: str, series_igdb_id: int) -> str:
    return f"series_members_v2:{kind}:{series_igdb_id}"


def _parse_cache(raw: str | None) -> tuple[datetime, SeriesMembersResult] | None:
    """Parse a cached meta value, treating any malformed/pre-alias entry as absent."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        members = [SeriesMember(**m) for m in data["members"]]
        # Required (not .get): an entry from before aliases existed must be
        # treated as absent so it gets refetched, not silently served without
        # alias data.
        raw_aliases = data["aliases"]
        aliases = {int(k): v for k, v in raw_aliases.items()}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return None
    return fetched_at, SeriesMembersResult(members=members, aliases=aliases)


async def _fetch_aliases(members: list[SeriesMember]) -> dict[int, int]:
    """Best-effort version-parent alias fetch: failures degrade to no aliases.

    Aliases are a defense-in-depth layer on top of the id-based and
    normalized-name have/gap checks (tools/series.py), so a transient IGDB
    failure here shouldn't block the whole series-member fetch that already
    succeeded.
    """
    member_ids = [m.igdb_id for m in members]
    if not member_ids:
        return {}
    try:
        return await fetch_version_parent_aliases(member_ids)
    except IGDBRequestFailure:
        logger.warning(
            "IGDB version-parent alias fetch failed for %d member ids; continuing without aliases",
            len(member_ids),
            exc_info=True,
        )
        return {}


async def get_series_members_cached(
    kind: str, series_igdb_id: int, refresh: bool = False
) -> SeriesMembersResult:
    """fetch_series_members (+ version-parent aliases) with a meta-KV cache (7-day TTL).

    Cache hit within TTL returns without a network call. On member-fetch
    failure with a stale cache present, serves the stale copy (logged); with
    no cache, the failure propagates. ``refresh=True`` bypasses the cache and
    refreshes both members and aliases.
    """
    key = _cache_key(kind, series_igdb_id)
    cached = _parse_cache(await get_meta(key))

    if not refresh and cached is not None:
        fetched_at, result = cached
        age = datetime.now(timezone.utc) - fetched_at
        if age < timedelta(days=SERIES_CACHE_TTL_DAYS):
            return result

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

    aliases = await _fetch_aliases(members)
    result = SeriesMembersResult(members=members, aliases=aliases)

    now = datetime.now(timezone.utc).isoformat()
    await set_meta(
        key,
        json.dumps(
            {
                "fetched_at": now,
                "members": [asdict(m) for m in members],
                "aliases": {str(k): v for k, v in aliases.items()},
            }
        ),
    )
    return result
