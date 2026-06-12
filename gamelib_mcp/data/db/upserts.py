"""Game/platform/identifier/enrichment upserts, incl. bulk Steam library sync."""

from datetime import datetime, timezone

from . import (
    STEAM_APP_ID,
    STEAM_PLATFORM,
    _backfill_name_normalized,
    _iter_chunks,
    get_db,
)
from ..title_normalization import normalize_search_text


async def upsert_game(
    appid: int | None,
    name: str,
    **fields,
) -> int:
    """Insert or update a canonical game row. Returns games.id."""
    async with get_db() as db:
        row = None
        if appid is not None:
            row = await db.execute_fetchone(
                """SELECT g.id
                   FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id
                   JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                   WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
                   LIMIT 1""",
                (STEAM_APP_ID, str(appid)),
            )

        if row is None:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
                (name,),
            )

        if row is None:
            cursor = await db.execute("INSERT INTO games (name) VALUES (?)", (name,))
            game_id = cursor.lastrowid
        else:
            game_id = row["id"]

        updates = {"name": name, "name_normalized": normalize_search_text(name), **fields}
        cols_sql = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()
        return game_id


async def upsert_game_platform(
    game_id: int,
    platform: str,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    owned: int = 1,
) -> int:
    """Insert or update a game_platforms row and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned = excluded.owned,
                   playtime_minutes = COALESCE(excluded.playtime_minutes, game_platforms.playtime_minutes),
                   playtime_2weeks_minutes = COALESCE(
                       excluded.playtime_2weeks_minutes,
                       game_platforms.playtime_2weeks_minutes
                   ),
                   last_synced = excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, now),
        )
        row = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        await db.commit()
        return row["id"]


async def upsert_game_platform_identifier(
    game_platform_id: int,
    identifier_type: str,
    identifier_value: str | int,
    *,
    is_primary: bool = True,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platform_identifiers
               (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                   game_platform_id = excluded.game_platform_id,
                   is_primary = excluded.is_primary,
                   last_seen_at = excluded.last_seen_at""",
            (game_platform_id, identifier_type, str(identifier_value), int(is_primary), now),
        )
        if is_primary:
            row = await db.execute_fetchone(
                "SELECT id FROM game_platform_identifiers WHERE identifier_type = ? AND identifier_value = ?",
                (identifier_type, str(identifier_value)),
            )
            row_id = row["id"]
            await db.execute(
                """
                UPDATE game_platform_identifiers
                SET is_primary = 0
                WHERE game_platform_id = ? AND identifier_type = ? AND id != ?
                """,
                (game_platform_id, identifier_type, row_id),
            )
        await db.commit()


async def upsert_steam_platform_data(game_platform_id: int, **fields) -> None:
    if not fields:
        return

    columns = ", ".join(["game_platform_id", *fields.keys()])
    placeholders = ", ".join("?" for _ in range(len(fields) + 1))
    updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
    async with get_db() as db:
        await db.execute(
            f"""INSERT INTO steam_platform_data ({columns})
                VALUES ({placeholders})
                ON CONFLICT(game_platform_id) DO UPDATE SET {updates}""",
            (game_platform_id, *fields.values()),
        )
        await db.commit()


async def bulk_upsert_steam_library(
    rows: list[dict],
    synced_at: str,
    chunk_size: int = 250,
) -> int:
    if not rows:
        return 0

    async with get_db() as db:
        await db.execute(
            """CREATE TEMP TABLE IF NOT EXISTS temp_steam_library_sync (
                   appid INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   playtime_minutes INTEGER,
                   playtime_2weeks_minutes INTEGER,
                   rtime_last_played INTEGER,
                   row_order INTEGER NOT NULL
               )"""
        )

        row_offset = 0
        for chunk in _iter_chunks(rows, chunk_size):
            await db.execute("DELETE FROM temp_steam_library_sync")
            await db.executemany(
                """INSERT INTO temp_steam_library_sync
                   (appid, name, playtime_minutes, playtime_2weeks_minutes, rtime_last_played, row_order)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       name = excluded.name,
                       playtime_minutes = excluded.playtime_minutes,
                       playtime_2weeks_minutes = excluded.playtime_2weeks_minutes,
                       rtime_last_played = excluded.rtime_last_played,
                       row_order = excluded.row_order""",
                [
                    (
                        row["appid"],
                        row["name"],
                        row.get("playtime_minutes"),
                        row.get("playtime_2weeks_minutes"),
                        row.get("rtime_last_played"),
                        row_offset + index,
                    )
                    for index, row in enumerate(chunk)
                ],
            )
            row_offset += len(chunk)

            await db.execute(
                """INSERT INTO games (name)
                   SELECT MIN(t.name)
                   FROM temp_steam_library_sync t
                   WHERE NOT EXISTS (
                       SELECT 1
                       FROM game_platform_identifiers gpi
                       JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                       WHERE gpi.identifier_type = ?
                         AND gpi.identifier_value = CAST(t.appid AS TEXT)
                   )
                     AND NOT EXISTS (
                       SELECT 1
                       FROM games g
                       WHERE lower(g.name) = lower(t.name)
                   )
                   GROUP BY lower(t.name)""",
                (STEAM_APP_ID,),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              t.row_order,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   UPDATE games
                   SET name = (
                       SELECT resolved.name
                       FROM resolved
                       WHERE resolved.game_id = games.id
                       ORDER BY resolved.row_order DESC
                       LIMIT 1
                   ),
                   name_normalized = NULL
                   WHERE id IN (
                       SELECT game_id
                       FROM resolved
                       WHERE game_id IS NOT NULL
                   )""",
                (STEAM_APP_ID,),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              t.playtime_minutes,
                              t.playtime_2weeks_minutes,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   INSERT INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   SELECT resolved.game_id, ?, 1,
                          resolved.playtime_minutes,
                          resolved.playtime_2weeks_minutes,
                          ?
                   FROM resolved
                   WHERE resolved.game_id IS NOT NULL
                   ON CONFLICT(game_id, platform) DO UPDATE SET
                       owned = excluded.owned,
                       playtime_minutes = COALESCE(
                           excluded.playtime_minutes,
                           game_platforms.playtime_minutes
                       ),
                       playtime_2weeks_minutes = COALESCE(
                           excluded.playtime_2weeks_minutes,
                           game_platforms.playtime_2weeks_minutes
                       ),
                       last_synced = excluded.last_synced""",
                (STEAM_APP_ID, STEAM_PLATFORM, synced_at),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   INSERT INTO game_platform_identifiers
                   (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
                   SELECT gp.id, ?, CAST(resolved.appid AS TEXT), 1, ?
                   FROM resolved
                   JOIN game_platforms gp
                     ON gp.game_id = resolved.game_id AND gp.platform = ?
                   WHERE resolved.game_id IS NOT NULL
                   ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                       game_platform_id = excluded.game_platform_id,
                       is_primary = excluded.is_primary,
                       last_seen_at = excluded.last_seen_at""",
                (STEAM_APP_ID, STEAM_APP_ID, synced_at, STEAM_PLATFORM),
            )

            await db.execute(
                """WITH resolved AS (
                       SELECT t.appid,
                              t.name,
                              t.rtime_last_played,
                              COALESCE(
                                  (
                                      SELECT gp.game_id
                                      FROM game_platform_identifiers gpi
                                      JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                                      WHERE gpi.identifier_type = ?
                                        AND gpi.identifier_value = CAST(t.appid AS TEXT)
                                      LIMIT 1
                                  ),
                                  (
                                      SELECT g.id
                                      FROM games g
                                      WHERE lower(g.name) = lower(t.name)
                                      ORDER BY g.id
                                      LIMIT 1
                                  )
                              ) AS game_id
                       FROM temp_steam_library_sync t
                   )
                   INSERT INTO steam_platform_data
                   (game_platform_id, rtime_last_played, library_updated_at)
                   SELECT gp.id, resolved.rtime_last_played, ?
                   FROM resolved
                   JOIN game_platforms gp
                     ON gp.game_id = resolved.game_id AND gp.platform = ?
                   WHERE resolved.game_id IS NOT NULL
                   ON CONFLICT(game_platform_id) DO UPDATE SET
                       rtime_last_played = excluded.rtime_last_played,
                       library_updated_at = excluded.library_updated_at""",
                (STEAM_APP_ID, synced_at, STEAM_PLATFORM),
            )

            await db.commit()

        # Pure-SQL inserts/renames above leave name_normalized NULL; fill it in
        # one pass so search matching never sees a stale value.
        await _backfill_name_normalized(db)
        await db.execute("DROP TABLE IF EXISTS temp_steam_library_sync")
        await db.commit()

    return len(rows)


async def upsert_game_platform_enrichment(game_platform_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(["game_platform_id", *fields.keys()])
    placeholders = ", ".join("?" for _ in range(len(fields) + 1))
    updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
    async with get_db() as db:
        await db.execute(
            f"""INSERT INTO game_platform_enrichment ({columns})
                VALUES ({placeholders})
                ON CONFLICT(game_platform_id) DO UPDATE SET {updates}""",
            (game_platform_id, *fields.values()),
        )
        await db.commit()
