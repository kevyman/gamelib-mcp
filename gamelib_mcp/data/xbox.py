"""Xbox library sync via OpenXBL (https://xbl.io).

Requires OPENXBL_API_KEY (personal key from xbl.io/console; sent as the
X-Authorization header). Xbox has no purchase-library API, so ownership is
derived from the account's title history ("played on this account") — the
same approximation nintendo_pctl makes for Parental Controls playtime.
Playtime is fetched best-effort from the stats endpoint; when unavailable,
titles sync with playtime_minutes=None (unknown, like GOG).

OPENXBL_XUID optionally pins the account to inspect; it defaults to the API
key owner's own xuid, resolved via GET /account.
"""

import logging
import os
from typing import Any

import httpx

from gamelib_mcp.data.db import (
    XBOX_TITLE_ID,
    adopt_platform_identifier,
    get_game_by_identifier,
    load_fuzzy_candidates,
    upsert_game_alias,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB, resolve_and_link_game
from gamelib_mcp.data.title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

_OPENXBL_BASE = "https://xbl.io/api/v2"
_OPENXBL_TIMEOUT = 30.0
_MINUTES_PLAYED_STAT = "MinutesPlayed"


def is_xbox_configured() -> bool:
    return bool(os.getenv("OPENXBL_API_KEY"))


def _headers() -> dict[str, str]:
    return {
        "X-Authorization": os.getenv("OPENXBL_API_KEY", ""),
        "Accept": "application/json",
    }


def _extract_title(entry: Any) -> tuple[str | None, str | None]:
    if not isinstance(entry, dict):
        return None, None
    title_id = entry.get("titleId")
    name = entry.get("name")
    return (str(title_id) if title_id else None, str(name) if name else None)


async def fetch_xbox_titles(xuid: str | None = None) -> list[dict]:
    """Return raw title-history entries. Raises on HTTP failure.

    When ``xuid`` is given, the XUID-qualified endpoint is used so the
    ownership fetch targets that account; otherwise the unqualified endpoint
    returns the API key owner's own title history.
    """
    url = f"{_OPENXBL_BASE}/player/titleHistory"
    if xuid:
        url = f"{url}/{xuid}"
    async with httpx.AsyncClient(timeout=_OPENXBL_TIMEOUT, headers=_headers()) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    titles = payload.get("titles") if isinstance(payload, dict) else None
    if not isinstance(titles, list):
        raise RuntimeError("unexpected OpenXBL titleHistory payload")
    return [t for t in titles if isinstance(t, dict)]


async def _resolve_xuid(client: httpx.AsyncClient) -> str | None:
    """Return the API key owner's own xuid via GET /account."""
    resp = await client.get(f"{_OPENXBL_BASE}/account")
    resp.raise_for_status()
    payload = resp.json()
    profile_users = payload.get("profileUsers") if isinstance(payload, dict) else None
    if not isinstance(profile_users, list) or not profile_users:
        return None
    first = profile_users[0]
    xuid = first.get("id") if isinstance(first, dict) else None
    return str(xuid) if xuid else None


def _parse_minutes_played(payload: Any) -> dict[str, int]:
    """Best-effort extraction of {title_id: minutes} from a batch stats payload.

    The documented shape (Xbox Live's userstats /batch endpoint, which OpenXBL
    proxies) groups stats per title:

        {"groups": [{"titleId": "...", "statlistscollection": [
            {"stats": [{"name": "MinutesPlayed", "value": "123"}]}
        ]}]}

    Parsing is defensive — any unexpected nesting is skipped rather than
    raising, since playtime is best-effort and must never block ownership.
    """
    result: dict[str, int] = {}
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return result

    for group in groups:
        if not isinstance(group, dict):
            continue
        title_id = group.get("titleId")
        if not title_id:
            continue
        collections = group.get("statlistscollection")
        if not isinstance(collections, list):
            continue
        for collection in collections:
            if not isinstance(collection, dict):
                continue
            stats = collection.get("stats")
            if not isinstance(stats, list):
                continue
            for stat in stats:
                if not isinstance(stat, dict) or stat.get("name") != _MINUTES_PLAYED_STAT:
                    continue
                value = stat.get("value")
                if not isinstance(value, (str, int, float)):
                    logger.debug("Skipping Xbox stat row with invalid value: %r", stat)
                    continue
                try:
                    result[str(title_id)] = int(float(value))
                except (TypeError, ValueError):
                    logger.debug("Skipping Xbox stat row with invalid value: %r", stat)

    return result


async def fetch_xbox_playtime(title_ids: list[str], xuid: str | None = None) -> dict[str, int]:
    """title_id -> total minutes played; best-effort ({} on any failure).

    ``xuid`` should be the same account the title history was fetched for
    (see ``sync_xbox``); when None, the API key owner's own xuid is resolved
    via GET /account — consistent with the unqualified title-history fetch.
    """
    if not title_ids:
        return {}

    try:
        async with httpx.AsyncClient(timeout=_OPENXBL_TIMEOUT, headers=_headers()) as client:
            if xuid is None:
                xuid = await _resolve_xuid(client)
            if not xuid:
                logger.warning("Xbox playtime unavailable: could not resolve an xuid")
                return {}

            body = {
                "xuids": [xuid],
                "groups": [{"name": "Hero", "titleId": tid} for tid in title_ids],
                "stats": [{"name": _MINUTES_PLAYED_STAT, "titleId": tid} for tid in title_ids],
            }
            resp = await client.post(f"{_OPENXBL_BASE}/player/stats", json=body)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning("Xbox playtime unavailable: %s", exc)
        return {}

    return _parse_minutes_played(payload)


async def sync_xbox() -> dict:
    """
    Sync Xbox library into game_platforms via OpenXBL title history.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not is_xbox_configured():
        logger.info("OPENXBL_API_KEY not set — skipping Xbox sync")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": "OPENXBL_API_KEY is not set",
            "error_classification": "missing_configuration",
        }

    # Resolve the pinned account once and target BOTH fetches at it, so a
    # configured OPENXBL_XUID can never import one account's library with
    # another account's stats. When unset, both fetches consistently fall
    # back to the API key owner's own account.
    xuid = os.getenv("OPENXBL_XUID") or None

    try:
        titles = await fetch_xbox_titles(xuid)
    except Exception as exc:
        logger.warning("Xbox sync failed: %s", exc)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "failed",
            "error_summary": f"Xbox sync failed: {exc}",
        }

    if not titles:
        logger.info("OpenXBL title history is empty — skipping Xbox sync")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": "OpenXBL title history is empty",
            "error_classification": "missing_configuration",
        }

    all_title_ids = [tid for tid, _ in (_extract_title(t) for t in titles) if tid]
    try:
        playtime_by_title = await fetch_xbox_playtime(all_title_ids, xuid)
    except Exception as exc:
        # fetch_xbox_playtime is documented best-effort ({} on failure), but
        # playtime must never block an ownership sync even if that contract
        # is violated by a future change.
        logger.warning("Xbox playtime unavailable (non-fatal): %s", exc)
        playtime_by_title = {}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()
    igdb_platform_id = PLATFORM_TO_IGDB.get("xbox")

    for entry in titles:
        title_id, name = _extract_title(entry)
        if not name:
            skipped += 1
            continue
        prepared_title = prepare_catalog_title(name)
        if prepared_title is None:
            skipped += 1
            continue

        # Prefer the stable Xbox title id: a re-sync matches the existing
        # game directly so name/fuzzy resolution (which now refuses to
        # attach onto an existing Xbox-owning row) never re-creates it.
        existing = (
            await get_game_by_identifier(XBOX_TITLE_ID, title_id) if title_id else None
        )
        # Identifier miss but a same-name xbox row exists without any
        # xbox_title_id: adopt the identifier onto it instead of letting the
        # exclude_platform guard fork a stranded duplicate.
        adopted_game_id = (
            await adopt_platform_identifier(
                name=prepared_title,
                platform="xbox",
                identifier_type=XBOX_TITLE_ID,
                identifier_value=title_id,
            )
            if existing is None and title_id
            else None
        )
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            matched += 1
        elif adopted_game_id is not None:
            game_id = adopted_game_id
            igdb_game = None
            matched += 1
        else:
            game_id, igdb_game = await resolve_and_link_game(
                prepared_title, igdb_platform_id, candidates, platform="xbox"
            )
            if game_id in candidates:
                matched += 1
            else:
                candidates[game_id] = prepared_title
                added += 1

        if name != prepared_title:
            await upsert_game_alias(
                game_id,
                name,
                alias_type="edition",
                source="xbox",
                source_key=title_id,
            )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="xbox",
            playtime_minutes=playtime_by_title.get(title_id) if title_id else None,
            owned=1,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

        if title_id:
            await upsert_game_platform_identifier(platform_id, XBOX_TITLE_ID, title_id)

    logger.info(
        "Xbox sync: added=%d matched=%d skipped=%d playtime_rows=%d",
        added,
        matched,
        skipped,
        len(playtime_by_title),
    )
    return {"added": added, "matched": matched, "skipped": skipped}
