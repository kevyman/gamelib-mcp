"""Enrichment row-claiming and batch loaders for background workers."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

import aiosqlite

from . import (
    STEAM_APP_ID,
    get_db,
)


def _claim_cutoff_iso(minutes: int = 15) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


# How long an HLTB "NOT_FOUND:<iso>" marker suppresses re-fetching. NOT_FOUND
# is deliberately retryable (unlike the old permanent sentinel): matcher
# improvements and HLTB catalog additions get picked up automatically. Lives
# here (not hltb.py) because the claim predicate below and hltb.py's freshness
# check must share it, and hltb.py already imports from this package.
HLTB_NOT_FOUND_RETRY_DAYS = 30


def _hltb_not_found_cutoff_iso() -> str:
    return (
        datetime.now(UTC) - timedelta(days=HLTB_NOT_FOUND_RETRY_DAYS)
    ).isoformat()


async def clear_claim(table: str, claim_column: str, row_id: int, id_column: str = "id") -> None:
    async with get_db() as db:
        await db.execute(
            f"UPDATE {table} SET {claim_column} = NULL WHERE {id_column} = ?",
            (row_id,),
        )
        await db.commit()


async def clear_all_enrichment_claims() -> None:
    async with get_db() as db:
        await db.execute("UPDATE games SET igdb_claimed_at = NULL, hltb_claimed_at = NULL")
        await db.execute(
            "UPDATE steam_platform_data SET store_claimed_at = NULL, protondb_claimed_at = NULL, steamspy_claimed_at = NULL"
        )
        await db.execute(
            "UPDATE game_platform_enrichment SET opencritic_claimed_at = NULL, metacritic_claimed_at = NULL"
        )
        await db.commit()


async def release_game_claim(game_id: int, column: str) -> None:
    await clear_claim("games", column, game_id)


# Providers whose enrichment is keyed on the game's *name*. When a game is
# renamed the cached values describe the old title, so they must be re-fetched.
# (Steam store/SteamSpy/ProtonDB are keyed on steam_appid, not name, so they are
# deliberately left alone.)
_HLTB_OVERRIDE_COLUMNS = frozenset({"hltb_main", "hltb_extra", "hltb_complete"})


async def invalidate_name_derived_enrichment(
    game_id: int, overrides: Iterable[str] = (),
) -> list[str]:
    """Clear name-matched enrichment caches so background workers re-fetch under
    a game's new title. Call this after renaming a game.

    IGDB (series + shared genres/tags/release_date) and the OpenCritic/Metacritic
    critic scores are always re-claimed; field-level ``manual_overrides`` still
    protect any user-pinned columns at write time. HLTB is skipped when all of its
    durations are manually overridden, since a re-fetch could not change them.

    Returns the list of providers invalidated.
    """
    overrides = set(overrides)
    invalidated: list[str] = ["igdb"]
    async with get_db() as db:
        # IGDB — name-matched; drives series and shared cross-platform metadata.
        await db.execute(
            "UPDATE games SET igdb_cached_at = NULL, igdb_claimed_at = NULL, "
            "igdb_resolver_version = NULL WHERE id = ?",
            (game_id,),
        )
        # Drop existing IGDB series memberships too: upsert_game_series_links is
        # add-only, so without this a title renamed into a different collection/
        # franchise would keep its old series alongside the new one. The backfill
        # worker repopulates the correct memberships when it re-fetches.
        await db.execute(
            "DELETE FROM game_series_membership WHERE game_id = ?",
            (game_id,),
        )
        # HLTB — name-matched; pointless to re-fetch if the user pinned every duration.
        if not _HLTB_OVERRIDE_COLUMNS <= overrides:
            await db.execute(
                "UPDATE games SET hltb_cached_at = NULL, hltb_claimed_at = NULL WHERE id = ?",
                (game_id,),
            )
            invalidated.append("hltb")
        # OpenCritic + Metacritic — name-matched critic scores on every platform
        # enrichment row for this game.
        await db.execute(
            """UPDATE game_platform_enrichment
                  SET opencritic_cached_at = NULL, opencritic_claimed_at = NULL,
                      metacritic_cached_at = NULL, metacritic_claimed_at = NULL
                WHERE game_platform_id IN (
                    SELECT id FROM game_platforms WHERE game_id = ?
                )""",
            (game_id,),
        )
        invalidated.extend(["opencritic", "metacritic"])
        await db.commit()
    return invalidated


async def invalidate_igdb_match_enrichment(game_id: int) -> None:
    """Clear IGDB enrichment so the backfill re-fetches under a corrected igdb_id.

    Call this after update_game repins ``igdb_id`` to a different match. Unlike
    invalidate_name_derived_enrichment (a rename), this touches ONLY the IGDB
    caches — HLTB and the critic scores are name-matched, not igdb-matched, so an
    id correction must not re-claim them.

    Nulling igdb_cached_at/igdb_claimed_at re-qualifies the row for
    claim_game_ids_for_igdb, and the backfill worker fetches the pinned igdb_id
    directly (it skips name re-resolution when a stored igdb_id exists), so the
    corrected match's series/platform/cover metadata replaces the stale one. The
    game_platforms/igdb_platforms/cover_image_id manual overrides are still
    honored at write time. Series memberships are add-only (upsert_game_series_links),
    so drop the stale ones here — the worker repopulates the correct set.
    """
    async with get_db() as db:
        await db.execute(
            "UPDATE games SET igdb_cached_at = NULL, igdb_claimed_at = NULL, "
            "igdb_resolver_version = NULL WHERE id = ?",
            (game_id,),
        )
        await db.execute(
            "DELETE FROM game_series_membership WHERE game_id = ?",
            (game_id,),
        )
        await db.commit()


async def _claim_ids(
    select_sql: str,
    select_params: tuple,
    update_sql: str,
    update_params_for_id: Callable[[str, int], tuple],
) -> list[int]:
    async with get_db() as db:
        rows = await db.execute_fetchall(select_sql, select_params)
        ids = [row["id"] for row in rows]
        if not ids:
            return []

        now = datetime.now(UTC).isoformat()
        claimed: list[int] = []
        for row_id in ids:
            cursor = await db.execute(update_sql, update_params_for_id(now, row_id))
            if cursor.rowcount:
                claimed.append(row_id)
        await db.commit()
        return claimed


async def claim_game_ids_for_igdb(
    limit: int,
    stale_before: str,
    game_ids: Iterable[int] | None = None,
    *,
    resolver_version: int,
) -> list[int]:
    """Claim up to ``limit`` unlinked games for the IGDB backfill.

    ``game_ids`` narrows the claim to those rows (still subject to every other
    condition) — the scoped path ``get_game_detail`` uses to link the one row
    it was asked about instead of waiting for the background drain.

    Claimable is "never checked" OR "checked, no match, by an OLDER resolver
    generation" (``resolver_version`` is the caller's current
    ``igdb.IGDB_RESOLVER_VERSION``; it is a required parameter rather than an
    import because this package sits underneath igdb.py, and it carries no
    default because a forgotten argument would silently re-claim every stale
    no-match on every pass). That second arm is what
    makes a no-match stamp temporary: a matcher improvement re-queues every row
    it could newly resolve without a bespoke migration — the shape v10, v28 and
    v41 each had to hand-write. A LINKED row is never re-claimed by a bump.

    Never-checked rows sort FIRST (``igdb_cached_at IS NOT NULL`` ascending) so
    a large backlog of stale no-matches drains behind new rows rather than
    starving them; ``is_farmed``/``id`` order the rest exactly as before.
    """
    scope = list(dict.fromkeys(game_ids)) if game_ids is not None else None
    if scope is not None and not scope:
        return []
    scope_sql = f" AND id IN ({','.join('?' for _ in scope)})" if scope else ""
    scope_params = tuple(scope or ())
    claimable = (
        "(igdb_cached_at IS NULL"
        " OR (igdb_id IS NULL AND COALESCE(igdb_resolver_version, 0) < ?))"
    )
    return await _claim_ids(
        f"""SELECT id
           FROM games
           WHERE {claimable}
             AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)
             {scope_sql}
           ORDER BY (igdb_cached_at IS NOT NULL), is_farmed ASC, id
           LIMIT ?""",
        (resolver_version, stale_before, *scope_params, limit),
        f"""UPDATE games
           SET igdb_claimed_at = ?
           WHERE id = ?
             AND {claimable}
             AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)""",
        lambda now, game_id: (now, game_id, resolver_version, stale_before),
    )


async def claim_game_ids_for_hltb(limit: int, stale_before: str) -> list[int]:
    # Claimable rows: never fetched, legacy FAILED sentinel, or an expired
    # NOT_FOUND marker. NOT_FOUND markers carry their write time as
    # "NOT_FOUND:<iso>"; substr(x, 11) is the ISO part ("NOT_FOUND:" is 10
    # chars) and compares lexicographically. A bare legacy "NOT_FOUND" yields
    # '' which always reads as expired.
    not_found_cutoff = _hltb_not_found_cutoff_iso()
    claimable = (
        "(hltb_cached_at IS NULL OR hltb_cached_at = 'FAILED'"
        " OR (hltb_cached_at LIKE 'NOT_FOUND%' AND substr(hltb_cached_at, 11) < ?))"
    )
    return await _claim_ids(
        f"""SELECT id
           FROM games
           WHERE {claimable}
             AND (hltb_claimed_at IS NULL OR hltb_claimed_at < ?)
           ORDER BY is_farmed ASC, id
           LIMIT ?""",
        (not_found_cutoff, stale_before, limit),
        f"""UPDATE games
           SET hltb_claimed_at = ?
           WHERE id = ?
             AND {claimable}
             AND (hltb_claimed_at IS NULL OR hltb_claimed_at < ?)""",
        lambda now, game_id: (now, game_id, not_found_cutoff, stale_before),
    )


async def claim_steam_platform_ids_for_store(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT spd.game_platform_id AS id
           FROM steam_platform_data spd
           JOIN game_platforms gp ON gp.id = spd.game_platform_id
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_identifiers gpi
             ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
           WHERE spd.store_cached_at IS NULL
             AND (spd.store_claimed_at IS NULL OR spd.store_claimed_at < ?)
           ORDER BY g.is_farmed ASC, COALESCE(gp.playtime_minutes, 0) DESC, spd.game_platform_id
           LIMIT ?""",
        (STEAM_APP_ID, stale_before, limit),
        """UPDATE steam_platform_data
           SET store_claimed_at = ?
           WHERE game_platform_id = ?
             AND store_cached_at IS NULL
             AND (store_claimed_at IS NULL OR store_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_steam_platform_ids_for_protondb(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT spd.game_platform_id AS id
           FROM steam_platform_data spd
           JOIN game_platforms gp ON gp.id = spd.game_platform_id
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_identifiers gpi
             ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
           WHERE spd.protondb_cached_at IS NULL
             AND (spd.protondb_claimed_at IS NULL OR spd.protondb_claimed_at < ?)
           ORDER BY g.is_farmed ASC, COALESCE(gp.playtime_minutes, 0) DESC, spd.game_platform_id
           LIMIT ?""",
        (STEAM_APP_ID, stale_before, limit),
        """UPDATE steam_platform_data
           SET protondb_claimed_at = ?
           WHERE game_platform_id = ?
             AND protondb_cached_at IS NULL
             AND (protondb_claimed_at IS NULL OR protondb_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_steam_platform_ids_for_steamspy(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT spd.game_platform_id AS id
           FROM steam_platform_data spd
           JOIN game_platforms gp ON gp.id = spd.game_platform_id
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_identifiers gpi
             ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
           WHERE spd.steamspy_cached_at IS NULL
             AND (spd.steamspy_claimed_at IS NULL OR spd.steamspy_claimed_at < ?)
           ORDER BY g.is_farmed ASC, COALESCE(gp.playtime_minutes, 0) DESC, spd.game_platform_id
           LIMIT ?""",
        (STEAM_APP_ID, stale_before, limit),
        """UPDATE steam_platform_data
           SET steamspy_claimed_at = ?
           WHERE game_platform_id = ?
             AND steamspy_cached_at IS NULL
             AND (steamspy_claimed_at IS NULL OR steamspy_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_game_platform_ids_for_opencritic(limit: int, stale_before: str) -> list[int]:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO game_platform_enrichment (game_platform_id)
               SELECT gp.id
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id"""
        )
        await db.commit()

    return await _claim_ids(
        """SELECT gp.id AS id
           FROM game_platforms gp
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
           WHERE gpe.opencritic_cached_at IS NULL
             AND (gpe.opencritic_claimed_at IS NULL OR gpe.opencritic_claimed_at < ?)
           ORDER BY g.is_farmed ASC, COALESCE(gp.playtime_minutes, 0) DESC, gp.id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE game_platform_enrichment
           SET opencritic_claimed_at = ?
           WHERE game_platform_id = ?
             AND opencritic_cached_at IS NULL
             AND (opencritic_claimed_at IS NULL OR opencritic_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def claim_game_platform_ids_for_metacritic(limit: int, stale_before: str) -> list[int]:
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO game_platform_enrichment (game_platform_id)
               SELECT gp.id
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id"""
        )
        await db.commit()

    return await _claim_ids(
        """SELECT gp.id AS id
           FROM game_platforms gp
           JOIN games g ON g.id = gp.game_id
           JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
           WHERE gpe.metacritic_cached_at IS NULL
             AND (gpe.metacritic_claimed_at IS NULL OR gpe.metacritic_claimed_at < ?)
           ORDER BY g.is_farmed ASC, COALESCE(gp.playtime_minutes, 0) DESC, gp.id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE game_platform_enrichment
           SET metacritic_claimed_at = ?
           WHERE game_platform_id = ?
             AND metacritic_cached_at IS NULL
             AND (metacritic_claimed_at IS NULL OR metacritic_claimed_at < ?)""",
        lambda now, platform_id: (now, platform_id, stale_before),
    )


async def load_games_for_igdb_backfill(game_ids: Iterable[int]) -> list[aiosqlite.Row]:
    """Rows for the IGDB backfill: identity + steam_appid + manual_overrides.

    steam_appid feeds the external_games-first resolution (the authoritative
    appid -> IGDB mapping). The effective-appid chain: a platform identifier
    first, then the Steam wishlist's store_identifier, then the newest
    assessment's steam_appid — the last two for rows with no Steam identifier
    row at all, since identifiers hang off game_platforms and an unowned
    wishlist-/assessment-only row has no ownership to hang one on. IGDB links
    by appid through external_games, so the fallbacks are exactly what lets
    such a row get linked at all. manual_overrides lets the backfill honor a
    pinned igdb_id without a per-row lookup, and release_date is the reference
    year the resolver uses to tell two same-named IGDB records apart.
    """
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT g.id, g.name, g.igdb_id, g.release_date, g.manual_overrides,
                       COALESCE((SELECT gpi.identifier_value
                        FROM game_platforms gp
                        JOIN game_platform_identifiers gpi
                          ON gpi.game_platform_id = gp.id
                         AND gpi.identifier_type = ?
                        WHERE gp.game_id = g.id
                        ORDER BY gpi.is_primary DESC, gpi.id ASC
                        LIMIT 1),
                       (SELECT w.store_identifier
                        FROM game_wishlist w
                        WHERE w.game_id = g.id AND w.platform = 'steam'),
                       (SELECT CAST(a.steam_appid AS TEXT)
                        FROM game_assessments a
                        WHERE a.game_id = g.id AND a.steam_appid IS NOT NULL
                        ORDER BY a.assessed_at DESC, a.id DESC
                        LIMIT 1)) AS steam_appid
                FROM games g
                WHERE g.id IN ({placeholders})
                ORDER BY g.id""",
            [STEAM_APP_ID, *ids],
        )


async def load_store_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id,
                       CAST(gpi.identifier_value AS INTEGER) AS appid,
                       g.name
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                JOIN game_platform_identifiers gpi
                  ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            [STEAM_APP_ID, *ids],
        )


async def load_hltb_batch_rows(game_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT id AS game_id, name, hltb_cached_at
                FROM games
                WHERE id IN ({placeholders})
                ORDER BY id""",
            ids,
        )


async def load_steam_platform_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id,
                       CAST(gpi.identifier_value AS INTEGER) AS appid,
                       g.name,
                       gp.platform
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                JOIN game_platform_identifiers gpi
                  ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            [STEAM_APP_ID, *ids],
        )


async def load_opencritic_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id, g.name
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            ids,
        )


async def load_metacritic_batch_rows(platform_ids: Iterable[int]) -> list[aiosqlite.Row]:
    ids = list(dict.fromkeys(platform_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT gp.id AS game_platform_id, gp.platform, g.name
                FROM game_platforms gp
                JOIN games g ON g.id = gp.game_id
                WHERE gp.id IN ({placeholders})
                ORDER BY COALESCE(gp.playtime_minutes, 0) DESC, gp.id""",
            ids,
        )
