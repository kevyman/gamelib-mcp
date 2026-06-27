"""PlayStation Network library sync via PSNAWP.

Auth: set PSN_NPSSO in .env.
Obtain the NPSSO cookie by visiting https://ca.account.sony.com/api/v1/ssocookie
while logged in to your PSN account in a browser. The page renders an error message,
but the `npsso` cookie is set — open DevTools (F12) → Application → Cookies →
find `npsso` under the Sony domain and copy the 64-character value.

Library source: client.title_stats() — returns all titles the user has played,
with name, play_count, and play_duration (datetime.timedelta). Only played titles
appear; unplayed purchases will not show up (PSN platform limitation).
"""

import asyncio
import logging
import os
import re

from psnawp_api.models.title_stats import PlatformCategory

from gamelib_mcp.data.db import (
    find_conflicting_fuzzy_key,
    get_game_by_identifier,
    load_fuzzy_candidates,
    repair_misclassified_platform_row,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB
from gamelib_mcp.data.title_normalization import normalize_search_text, prepare_catalog_title

logger = logging.getLogger(__name__)

# Identifier type for the stable PSN title id (e.g. "PPSA01234_00"). Matching on
# it lets a re-sync find an already-ingested title regardless of the (locale-
# dependent) display name PSN returns.
PSN_TITLE_ID = "psn_title_id"

# Non-Latin script ranges — the PSN gamelist endpoint returns titles in the
# account's system language, so some come back localized (e.g. Chinese). Those
# can't fuzzy-match the English library, which spawns duplicate rows. Covers CJK,
# Kana, Hangul, and fullwidth forms plus Cyrillic/Arabic/Hebrew/Thai so non-CJK
# locales are resolved too (the English lookup degrades gracefully on failure).
_NON_LATIN_RE = re.compile(
    "["
    "　-ヿ㐀-鿿가-힯＀-￯"  # CJK / Kana / Hangul / fullwidth
    "Ѐ-ӿ"  # Cyrillic
    "؀-ۿ"  # Arabic
    "֐-׿"  # Hebrew
    "฀-๿"  # Thai
    "]"
)


def _is_probably_non_latin(name: str) -> bool:
    return bool(_NON_LATIN_RE.search(name))


def _resolve_english_title(psnawp, title_id: str, fallback: str) -> str:
    """Best-effort: look up a title's canonical English name via the PSN catalog.

    The gamelist endpoint honours the account language, not Accept-Language, so a
    localized name is resolved to English through the title-concept API. Any
    failure degrades gracefully to ``fallback`` (the original name).
    """
    try:
        from psnawp_api.models.trophies.trophy_constants import PlatformType

        game_title = psnawp.game_title(title_id=title_id, platform=PlatformType.PS5)
        details = game_title.get_details(country="US", language="en-US")
        english = (details[0].get("name") if details else None) or ""
        if english.strip():
            return english.strip()
    except Exception as exc:  # defensive: never let a lookup break the sync
        logger.debug("PSN English title lookup failed for %s: %s", title_id, exc)
    return fallback


# Media/streaming apps to exclude from library sync.
# The primary filter catches PPSA-prefixed titles with UNKNOWN category (PS5-era apps).
# This blocklist catches PS4-era apps (CUSA IDs) that share the same UNKNOWN category
# but wouldn't be caught by the prefix check alone.
_MEDIA_APP_NAMES = {
    "Disney+", "Spotify", "Netflix", "YouTube", "Prime Video",
    "Plex", "Crunchyroll", "Apple TV", "Twitch", "SONY PICTURES CORE",
}


def is_psn_configured() -> bool:
    return bool(os.getenv("PSN_NPSSO"))


def _get_psnawp():
    """Return an authenticated PSNAWP instance, or raise if not configured."""
    from psnawp_api import PSNAWP  # lazy import — optional dependency
    npsso = os.environ.get("PSN_NPSSO")
    if not npsso:
        raise EnvironmentError("PSN_NPSSO not set")
    return PSNAWP(npsso)


async def fetch_psn_library() -> list[dict]:
    """
    Return a list of dicts with 'name', 'playtime_minutes', and 'last_played'
    (ISO ``YYYY-MM-DD`` or None) for each played PS5 title.

    Uses client.title_stats() which returns name, play_count, play_duration
    (a datetime.timedelta), and last_played_date_time. PSN exposes no rolling
    2-week playtime, so only lifetime playtime and last-played are captured.
    Runs PSNAWP synchronously in an executor.
    """
    def _fetch():
        psnawp = _get_psnawp()
        client = psnawp.me()
        results = []
        for entry in client.title_stats():
            name = entry.name
            if not name:
                continue
            if entry.category is PlatformCategory.UNKNOWN and (entry.title_id or "").startswith("PPSA"):
                continue
            if name in _MEDIA_APP_NAMES:
                continue
            title_id = entry.title_id
            # PSN may return a localized (non-Latin) name; resolve it to English
            # so it matches the existing library instead of spawning a duplicate.
            if title_id and _is_probably_non_latin(name):
                name = _resolve_english_title(psnawp, title_id, name)
            minutes = int(entry.play_duration.total_seconds() // 60)
            last_played_dt = getattr(entry, "last_played_date_time", None)
            last_played = last_played_dt.date().isoformat() if last_played_dt else None
            results.append(
                {
                    "name": name,
                    "title_id": title_id,
                    "playtime_minutes": minutes,
                    "last_played": last_played,
                }
            )
        return results

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


async def sync_psn() -> dict:
    """
    Sync PSN library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("PSN_NPSSO"):
        logger.info("PSN_NPSSO not set — skipping PSN sync")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": "PSN_NPSSO is not set",
            "error_classification": "missing_configuration",
        }

    added = matched = skipped = 0

    try:
        entries = await fetch_psn_library()
    except Exception as exc:
        logger.warning("PSN sync failed: %s", exc)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "failed",
            "error_summary": f"PSN sync failed: {exc}",
        }

    prepared_entries = []
    for entry in entries:
        name = prepare_catalog_title(entry["name"])
        if not name:
            skipped += 1
            continue
        prepared_entries.append((entry, name))

    current_titles = {normalize_search_text(name) for _entry, name in prepared_entries}
    candidates = await load_fuzzy_candidates()

    for entry, name in prepared_entries:
        igdb_platform_id = PLATFORM_TO_IGDB.get("ps5")
        title_id = entry.get("title_id")

        # Prefer the stable PSN title id: if we've ingested this title before,
        # match the existing game directly so a localized name can't fork a
        # duplicate row. Fall back to fuzzy name resolution for first ingest.
        existing = (
            await get_game_by_identifier(PSN_TITLE_ID, title_id) if title_id else None
        )
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            matched += 1
        else:
            conflicting_game_id = find_conflicting_fuzzy_key(name, candidates)
            game_id, igdb_game = await resolve_and_link_game(
                name, igdb_platform_id, candidates, platform="ps5"
            )
            if game_id in candidates:
                matched += 1
            else:
                candidates[game_id] = name
                added += 1

            if conflicting_game_id is not None and conflicting_game_id != game_id:
                conflicting_title = candidates.get(conflicting_game_id)
                if conflicting_title and normalize_search_text(conflicting_title) not in current_titles:
                    await repair_misclassified_platform_row(
                        source_game_id=conflicting_game_id,
                        target_game_id=game_id,
                        platform="ps5",
                    )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="ps5",
            playtime_minutes=entry["playtime_minutes"],
            last_played=entry.get("last_played"),
            owned=1,
        )

        # Record the stable id so future syncs match by it (idempotent).
        if title_id:
            await upsert_game_platform_identifier(platform_id, PSN_TITLE_ID, title_id)

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    logger.info("PSN sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
