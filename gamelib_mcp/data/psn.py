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
    adopt_platform_identifier,
    find_conflicting_fuzzy_key,
    get_game_by_identifier,
    load_fuzzy_candidates,
    repair_misclassified_platform_row,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB, resolve_and_link_game
from gamelib_mcp.data.title_normalization import (
    normalize_edition_comparison_title,
    normalize_search_text,
    prepare_catalog_title,
)

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


class _SkuAggregate:
    """The PSN SKUs of one game, folded into a single ``game_platforms`` row.

    PSN's gamelist is per-SKU, not per-game: a cross-generation title comes back
    as two or three entries (``CUSA…`` for PS4, ``PPSA…`` for PS5, sometimes one
    PPSA per region) each carrying its OWN play_duration. They all resolve to the
    same games row, and ``game_platforms`` is UNIQUE(game_id, platform), so they
    contend for a single row — and ``upsert_game_platform`` resolves playtime with
    COALESCE(excluded, existing), i.e. last writer wins. Since PSN sorts the
    gamelist by last-played descending, the STALEST SKU is written last and
    silently overwrote the newer one (observed in prod: Assassin's Creed Valhalla
    and Ghost of Tsushima kept their PS4 minutes and lost their PS5 ones, and
    last_played regressed to the PS4 date along with it).

    So the SKUs are accumulated here and written once: playtime is the SUM across
    SKUs (the hours are real and were played on the same console), last_played is
    the MAX (a newer session must never be walked backwards).
    """

    __slots__ = ("igdb_game", "last_played", "playtime_minutes", "skus", "title")

    def __init__(self, title: str) -> None:
        self.title = title
        self.playtime_minutes: int | None = None
        self.last_played: str | None = None
        # (title_id, playtime_minutes) per SKU, in the order PSN returned them.
        self.skus: list[tuple[str, int]] = []
        self.igdb_game = None

    def add(self, entry: dict, igdb_game=None) -> None:
        minutes = entry.get("playtime_minutes")
        if minutes is not None:
            self.playtime_minutes = (self.playtime_minutes or 0) + minutes
        last_played = entry.get("last_played")
        # ISO YYYY-MM-DD sorts lexicographically, so plain > is a date compare.
        if last_played and (self.last_played is None or last_played > self.last_played):
            self.last_played = last_played
        title_id = entry.get("title_id")
        if title_id and all(title_id != known for known, _ in self.skus):
            self.skus.append((title_id, minutes or 0))
        if self.igdb_game is None and igdb_game is not None:
            self.igdb_game = igdb_game

    def accepts(self, title: str) -> bool:
        """True when ``title`` is the same game as this aggregate's, edition aside.

        The gate on folding a second SKU into an existing row. Two SKUs of one
        game carry the same name modulo an edition suffix ("Ghost of Tsushima" /
        "Ghost of Tsushima DIRECTOR'S CUT"), which
        ``normalize_edition_comparison_title`` collapses. A genuinely different
        game does not ("Ratchet & Clank" is not an edition of "Ratchet & Clank:
        Rift Apart") — and without this check its playtime would be summed onto
        the wrong game. Compares SKU name against SKU name, never against the
        stored library name, so a hand-renamed library row cannot fork a sync.
        """
        return normalize_edition_comparison_title(
            self.title
        ) == normalize_edition_comparison_title(title)

    def primary_sku(self) -> str | None:
        """The title id to mark is_primary — the SKU with the most playtime.

        Ties keep the first PSN returned. The alternative (whichever happened to
        be written last) made the least-recently-played SKU primary.
        """
        if not self.skus:
            return None
        return max(self.skus, key=lambda sku: sku[1])[0]


def is_psn_configured() -> bool:
    return bool(os.getenv("PSN_NPSSO"))


def _get_psnawp():
    """Return an authenticated PSNAWP instance, or raise if not configured."""
    from psnawp_api import PSNAWP  # lazy import — optional dependency
    npsso = os.environ.get("PSN_NPSSO")
    if not npsso:
        raise OSError("PSN_NPSSO not set")
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

    # Pass 1 — resolve every SKU to its games row, accumulating per game rather
    # than writing per SKU (see _SkuAggregate: one row, N SKUs, last writer won).
    aggregates: dict[int, _SkuAggregate] = {}
    igdb_platform_id = PLATFORM_TO_IGDB.get("ps5")

    for entry, name in prepared_entries:
        title_id = entry.get("title_id")

        # Prefer the stable PSN title id: if we've ingested this title before,
        # match the existing game directly so a localized name can't fork a
        # duplicate row. Fall back to fuzzy name resolution for first ingest.
        existing = (
            await get_game_by_identifier(PSN_TITLE_ID, title_id) if title_id else None
        )
        # Identifier miss but a same-name ps5 row exists without any psn_title_id
        # (ingested before title ids were recorded): adopt the identifier onto it
        # instead of letting the exclude_platform guard fork a stranded duplicate.
        adopted_game_id = (
            await adopt_platform_identifier(
                name=name,
                platform="ps5",
                identifier_type=PSN_TITLE_ID,
                identifier_value=title_id,
            )
            if existing is None and title_id
            else None
        )
        # added/matched count GAMES, not SKUs — the tally is applied below, once
        # per distinct games row, so a cross-gen title's second entry does not
        # report as a second game.
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            is_new_game = False
        elif adopted_game_id is not None:
            game_id = adopted_game_id
            igdb_game = None
            is_new_game = False
        else:
            conflicting_game_id = find_conflicting_fuzzy_key(name, candidates)
            game_id, igdb_game = await resolve_and_link_game(
                name, igdb_platform_id, candidates, platform="ps5"
            )
            is_new_game = game_id not in candidates
            if is_new_game:
                candidates[game_id] = name

            if conflicting_game_id is not None and conflicting_game_id != game_id:
                conflicting_title = candidates.get(conflicting_game_id)
                if conflicting_title and normalize_search_text(conflicting_title) not in current_titles:
                    await repair_misclassified_platform_row(
                        source_game_id=conflicting_game_id,
                        target_game_id=game_id,
                        platform="ps5",
                    )

        # A second SKU may only join an existing aggregate when it names the same
        # game. When it does not, resolution collapsed two distinct games onto one
        # row (prod: the 2016 "Ratchet & Clank" sitting on "Ratchet & Clank: Rift
        # Apart"), and summing their playtime would compound the error — so the
        # SKU gets its own games row instead. Safe to do automatically, unlike a
        # general over-merge split: PSN reports playtime per SKU, so there is
        # nothing to re-attribute.
        aggregate = aggregates.get(game_id)
        if aggregate is not None and not aggregate.accepts(name):
            logger.info(
                "PSN: %r (%s) does not name the same game as %r — keeping it separate",
                name, title_id, aggregate.title,
            )
            game_id = await upsert_game(appid=None, name=name, match_existing_by_name=False)
            candidates[game_id] = name
            igdb_game = None
            is_new_game = True
            aggregate = aggregates.get(game_id)

        if aggregate is None:
            aggregate = aggregates[game_id] = _SkuAggregate(name)
            if is_new_game:
                added += 1
            else:
                matched += 1
        aggregate.add(entry, igdb_game)

    # Pass 2 — one platform write per game, carrying every SKU's playtime.
    for game_id, aggregate in aggregates.items():
        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="ps5",
            playtime_minutes=aggregate.playtime_minutes,
            last_played=aggregate.last_played,
            owned=1,
            from_source=True,
        )

        # Record the stable ids so future syncs match by them (idempotent). The
        # most-played SKU is written last so it ends up is_primary (the write
        # demotes this row's other ids of the same type).
        primary_sku = aggregate.primary_sku()
        for sku, _minutes in aggregate.skus:
            if sku != primary_sku:
                await upsert_game_platform_identifier(
                    platform_id, PSN_TITLE_ID, sku, is_primary=False
                )
        if primary_sku is not None:
            await upsert_game_platform_identifier(platform_id, PSN_TITLE_ID, primary_sku)

        igdb_game = aggregate.igdb_game
        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    merged_skus = len(prepared_entries) - len(aggregates)
    logger.info(
        "PSN sync: added=%d matched=%d skipped=%d merged_skus=%d",
        added, matched, skipped, merged_skus,
    )
    return {
        "added": added,
        "matched": matched,
        "skipped": skipped,
        # Extra SKUs folded into a game already counted above — the PS4/PS5
        # entries of a cross-gen title. Not games; not double-counted in matched.
        "merged_skus": merged_skus,
    }
