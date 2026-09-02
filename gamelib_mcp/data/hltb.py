"""Lazy HowLongToBeat fetch + semaphore-limited background pre-warm."""

import asyncio
import logging
import re
from datetime import UTC, datetime

from howlongtobeatpy import HowLongToBeat

from .db import HLTB_NOT_FOUND_RETRY_DAYS, get_db, get_manual_overrides
from .title_normalization import normalize_catalog_title

HLTB_CACHE_DAYS = 30
# Prefix of the retryable not-found marker ("NOT_FOUND:<iso timestamp>").
# A marker older than HLTB_NOT_FOUND_RETRY_DAYS (see db/claims.py) is
# re-attempted, so matcher improvements and HLTB catalog additions are picked
# up automatically. A bare legacy "NOT_FOUND" (no timestamp) always reads as
# expired.
HLTB_NOT_FOUND = "NOT_FOUND"
_semaphore = asyncio.Semaphore(3)
logger = logging.getLogger(__name__)

# Trailing edition decorations that normalize_catalog_title deliberately does
# NOT strip (they can denote a distinct catalog row for library identity), but
# that HLTB's catalog does not carry — only ever used as *fallback* search
# variants after the literal and normalized names returned nothing.
_HLTB_FALLBACK_SUFFIX_PATTERNS = (
    re.compile(r"\s+Legacy\s*$", re.IGNORECASE),
    re.compile(r"\s*[:\-]?\s*Game of the Year\s*$", re.IGNORECASE),
    re.compile(r"\s+GOTY\s*$", re.IGNORECASE),
    re.compile(r"\s*[:\-]?\s*\S+\s+Edition\s*$", re.IGNORECASE),
)


def _search_name_variants(name: str) -> list[str]:
    """Ordered, deduplicated search queries for a library title.

    The literal name goes first; fallbacks only widen the search when earlier
    queries return zero results. Covers the observed prod failure shapes:
    ALL-CAPS names ("HITMAN 2"), trademark glyphs, and edition suffixes
    ("Grand Theft Auto V Legacy", "Borderlands: Game of the Year (Classic)").
    Reuses normalize_catalog_title for the shared ™/®/edition stripping rather
    than duplicating those patterns.
    """
    variants: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        candidate = (candidate or "").strip()
        # Deduplicate on the exact string, NOT case-insensitively: the whole
        # point of the title-case variant is that HLTB treats "HITMAN 2" and
        # "Hitman 2" differently.
        if candidate and candidate not in seen:
            seen.add(candidate)
            variants.append(candidate)

    _add(name)
    _add(normalize_catalog_title(name))

    # HLTB's search matcher chokes on ALL-CAPS queries; retry in title case.
    for base in variants:
        if base.isupper():
            _add(base.title())

    # Edition-suffix fallbacks, applied to every variant gathered so far.
    for base in variants:
        stripped = base
        previous = None
        while stripped != previous:
            previous = stripped
            for pattern in _HLTB_FALLBACK_SUFFIX_PATTERNS:
                stripped = pattern.sub("", stripped)
        _add(stripped.strip(" :-"))

    return variants


async def get_hltb(game_id: int, name: str) -> dict | None:
    """
    Lazy-fetch HLTB data for a game. Caches in DB.
    Returns dict with hltb_main, hltb_extra, hltb_complete or None.
    """
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT hltb_main, hltb_extra, hltb_complete, hltb_cached_at FROM games WHERE id = ?",
            (game_id,),
        )

    if row:
        cached_at = row["hltb_cached_at"]
        if _is_not_found_marker(cached_at):
            if _not_found_is_fresh(cached_at):
                return None
            # Expired not-found — fall through and retry the search.
        elif _is_fresh(cached_at, HLTB_CACHE_DAYS):
            return {
                "hltb_main": row["hltb_main"],
                "hltb_extra": row["hltb_extra"],
                "hltb_complete": row["hltb_complete"],
            }

    return await _fetch_and_cache(game_id, name)


async def _fetch_and_cache(game_id: int, name: str) -> dict | None:
    async with _semaphore:
        try:
            results = None
            for query in _search_name_variants(name):
                results = await HowLongToBeat().async_search(query)
                if results is None:
                    # API failure — preserve existing data, leave row retryable.
                    # An operational failure must never be recorded as NOT_FOUND.
                    return None
                if results:
                    break

            now = datetime.now(UTC).isoformat()

            if not results:
                # Authoritative not-found across every variant — mark it with a
                # timestamp (retried after HLTB_NOT_FOUND_RETRY_DAYS) but keep
                # any prior data intact.
                await _set_cached_at(game_id, f"{HLTB_NOT_FOUND}:{now}")
                return None

            # Pick closest match by similarity score
            best = max(results, key=lambda e: e.similarity)
            # HLTB returns 0 for durations it has no data for; store NULL so
            # callers can distinguish "unknown length" from "instant" and so
            # max_hltb_hours filters / shortest-game sorts don't pick them up.
            main = _nonzero(best.main_story)
            extra = _nonzero(best.main_extra)
            comp = _nonzero(best.completionist)

            await _cache_result(game_id, main, extra, comp, now)
            return {"hltb_main": main, "hltb_extra": extra, "hltb_complete": comp}
        except Exception as e:
            logger.warning("HLTB fetch failed for %s (%d): %s", name, game_id, e)
            # Preserve existing data, leave row retryable
            return None


def _nonzero(value: float | None) -> float | None:
    """Treat HLTB's 0 (no data) as NULL."""
    if value is None or value == 0:
        return None
    return value


def _is_not_found_marker(cached_at: str | None) -> bool:
    return bool(cached_at) and str(cached_at).startswith(HLTB_NOT_FOUND)


def _not_found_is_fresh(marker: str) -> bool:
    timestamp = marker[len(HLTB_NOT_FOUND) + 1 :]
    return _is_fresh(timestamp, HLTB_NOT_FOUND_RETRY_DAYS)


async def _set_cached_at(game_id: int, marker: str | None) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
            (marker, game_id),
        )
        await db.commit()


async def _cache_result(
    game_id: int,
    main: float | None,
    extra: float | None,
    comp: float | None,
    cached_at: str | None,
) -> None:
    async with get_db() as db:
        # Preserve manually-edited HLTB times: still stamp hltb_cached_at (so the
        # background worker doesn't hot-loop re-claiming the row) but leave the
        # user's durations alone.
        overrides = await get_manual_overrides(db, game_id)
        if overrides & {"hltb_main", "hltb_extra", "hltb_complete"}:
            await db.execute(
                "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                (cached_at, game_id),
            )
        else:
            await db.execute(
                """UPDATE games SET hltb_main = ?, hltb_extra = ?, hltb_complete = ?, hltb_cached_at = ?
                   WHERE id = ?""",
                (main, extra, comp, cached_at, game_id),
            )
        await db.commit()


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at or cached_at == "FAILED":
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        age = datetime.now(UTC) - dt
        return age.total_seconds() < days * 86400
    except ValueError:
        return False
