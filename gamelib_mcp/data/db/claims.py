"""Enrichment row-claiming and batch loaders for background workers."""

from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import aiosqlite

from . import (
    STEAM_APP_ID,
    get_db,
)


def _claim_cutoff_iso(minutes: int = 15) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


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

        now = datetime.now(timezone.utc).isoformat()
        claimed: list[int] = []
        for row_id in ids:
            cursor = await db.execute(update_sql, update_params_for_id(now, row_id))
            if cursor.rowcount:
                claimed.append(row_id)
        await db.commit()
        return claimed


async def claim_game_ids_for_igdb(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT id
           FROM games
           WHERE igdb_cached_at IS NULL
             AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)
           ORDER BY is_farmed ASC, id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE games
           SET igdb_claimed_at = ?
           WHERE id = ?
             AND igdb_cached_at IS NULL
             AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)""",
        lambda now, game_id: (now, game_id, stale_before),
    )


async def claim_game_ids_for_hltb(limit: int, stale_before: str) -> list[int]:
    return await _claim_ids(
        """SELECT id
           FROM games
           WHERE (hltb_cached_at IS NULL OR hltb_cached_at = 'FAILED')
             AND (hltb_claimed_at IS NULL OR hltb_claimed_at < ?)
           ORDER BY is_farmed ASC, id
           LIMIT ?""",
        (stale_before, limit),
        """UPDATE games
           SET hltb_claimed_at = ?
           WHERE id = ?
             AND (hltb_cached_at IS NULL OR hltb_cached_at = 'FAILED')
             AND (hltb_claimed_at IS NULL OR hltb_claimed_at < ?)""",
        lambda now, game_id: (now, game_id, stale_before),
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
    ids = list(dict.fromkeys(game_ids))
    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    async with get_db() as db:
        return await db.execute_fetchall(
            f"""SELECT id, name, igdb_id
                FROM games
                WHERE id IN ({placeholders})
                ORDER BY id""",
            ids,
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
            f"""SELECT id AS game_id, name
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
