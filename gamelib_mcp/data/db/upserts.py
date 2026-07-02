"""Game/platform/identifier/enrichment upserts, incl. bulk Steam library sync."""

import json
from datetime import datetime, timezone

from . import (
    STEAM_APP_ID,
    STEAM_PLATFORM,
    _backfill_name_normalized,
    _iter_chunks,
    get_db,
)
from ..title_normalization import normalize_search_text

# games columns the update_game tool may set manually. A subset of these is
# recorded per-row in games.manual_overrides so background sync/enrichment knows
# not to clobber a value the user set by hand. name_normalized is derived from
# name and never protected on its own.
GAME_EDITABLE_FIELDS = {
    "name",
    "sort_name",
    "release_date",
    "genres",
    "tags",
    "features",
    "short_description",
    "hltb_main",
    "hltb_extra",
    "hltb_complete",
    "is_farmed",
    "content_type",
    "parent_game_id",
    "is_primary_library_item",
}


def _decode_overrides(raw) -> set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    return set(data) if isinstance(data, list) else set()


async def get_manual_overrides(db, game_id: int) -> set[str]:
    """Return the set of games columns marked as manual overrides for a game.

    ``db`` is an open connection (enrichment writers already hold one). Used by
    the sync/enrichment paths to skip columns the user set via update_game.
    """
    row = await db.execute_fetchone(
        "SELECT manual_overrides FROM games WHERE id = ?", (game_id,)
    )
    return _decode_overrides(row["manual_overrides"]) if row else set()


async def apply_manual_game_fields(game_id: int, fields: dict) -> set[str]:
    """Write user-supplied games columns and record them as manual overrides.

    Recomputes name_normalized when name changes. Merges the written column
    names into games.manual_overrides so later sync/enrichment won't overwrite
    them. Returns the full override set after the write.
    """
    if not fields:
        async with get_db() as db:
            return await get_manual_overrides(db, game_id)

    updates = dict(fields)
    if "name" in updates:
        updates["name_normalized"] = normalize_search_text(updates["name"])

    async with get_db() as db:
        current = await get_manual_overrides(db, game_id)
        merged = current | (set(fields) & GAME_EDITABLE_FIELDS)
        updates["manual_overrides"] = json.dumps(sorted(merged))
        cols_sql = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()
        return merged


async def remove_manual_overrides(game_id: int, columns) -> set[str]:
    """Stop protecting the given columns so sync/enrichment may update them again.

    Removing protection does not change the current value — it just lets the next
    sync/enrichment pass overwrite it. Returns the remaining override set.
    """
    async with get_db() as db:
        remaining = await get_manual_overrides(db, game_id) - set(columns)
        await db.execute(
            "UPDATE games SET manual_overrides = ? WHERE id = ?",
            (json.dumps(sorted(remaining)) if remaining else None, game_id),
        )
        await db.commit()
        return remaining


async def upsert_game(
    appid: int | None,
    name: str,
    *,
    match_existing_by_name: bool = True,
    **fields,
) -> int:
    """Insert or update a canonical game row. Returns games.id.

    With ``match_existing_by_name`` (the default) an unmatched appid falls back to
    attaching onto any existing row with the same name. Pass ``False`` from callers
    that have already decided no existing row should be reused (e.g. a fuzzy match
    that was deliberately rejected on platform/year identity) so the name fallback
    does not silently re-collapse two distinct games.
    """
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

        if row is None and match_existing_by_name:
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
        # Never persist a self-referencing parent: it would orphan the row from
        # both search (the is_primary filter) and its parent's editions list. Drop
        # the self-parent and keep the row a primary library item.
        if updates.get("parent_game_id") == game_id:
            updates["parent_game_id"] = None
            if "is_primary_library_item" in updates:
                updates["is_primary_library_item"] = 1
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
    last_played: str | None = None,
    owned: int = 1,
) -> int:
    """Insert or update a game_platforms row and return its id.

    ``last_played`` is an ISO date string (``YYYY-MM-DD``) for the platform's own
    last-played signal (Steam stores its own in steam_platform_data; Nintendo and
    PSN write it here). Like the playtime columns it only advances when a non-NULL
    value is supplied, so an ownership-only sync never clears it.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes,
                last_played, last_synced)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned = excluded.owned,
                   playtime_minutes = COALESCE(excluded.playtime_minutes, game_platforms.playtime_minutes),
                   playtime_2weeks_minutes = COALESCE(
                       excluded.playtime_2weeks_minutes,
                       game_platforms.playtime_2weeks_minutes
                   ),
                   last_played = COALESCE(excluded.last_played, game_platforms.last_played),
                   last_synced = excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes,
             last_played, now),
        )
        row = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        await db.commit()
        return row["id"]


async def upsert_wishlist_entry(
    game_id: int,
    platform: str,
    wishlisted_at: str | None = None,
    source: str | None = None,
    store_identifier: str | None = None,
) -> int:
    """Insert or update a game_wishlist row and return its id.

    Lives in its own table rather than game_platforms — a wishlist item may not
    be owned anywhere yet, and game_platforms rows are meant to mean "a real
    platform relationship exists" (owned, or a manual stub). source records
    where the entry came from (e.g. "steam", "dekudeals", "manual").
    store_identifier captures the store's own ID (e.g. Steam appid) at sync time.
    """
    now = wishlisted_at or datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_wishlist (game_id, platform, wishlisted_at, source, store_identifier)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   wishlisted_at = excluded.wishlisted_at,
                   source = excluded.source,
                   store_identifier = COALESCE(excluded.store_identifier, store_identifier)""",
            (game_id, platform, now, source, store_identifier),
        )
        row = await db.execute_fetchone(
            "SELECT id FROM game_wishlist WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        await db.commit()
        return row["id"]


async def clear_fulfilled_wishlist_entries(
    game_id: int | None = None,
    platform: str | None = None,
) -> int:
    """Delete wishlist entries whose game is now owned on that platform.

    Mirrors how storefronts like Steam clear a wishlist item once you buy it.
    Call this after any sync/write that may have established ownership
    (library refresh, wishlist sync, add_game_to_platform) — game_platforms
    rows are the source of truth for ownership, so a wishlist row lingers only
    until the next such call notices it's fulfilled. Optionally scoped to a
    single game_id/platform for an immediate, targeted check. Returns the
    number of rows deleted.
    """
    where = ["EXISTS (SELECT 1 FROM game_platforms gp "
             "WHERE gp.game_id = game_wishlist.game_id "
             "AND gp.platform = game_wishlist.platform AND gp.owned = 1)"]
    params: list = []
    if game_id is not None:
        where.append("game_wishlist.game_id = ?")
        params.append(game_id)
    if platform is not None:
        where.append("game_wishlist.platform = ?")
        params.append(platform)

    async with get_db() as db:
        cursor = await db.execute(
            f"DELETE FROM game_wishlist WHERE {' AND '.join(where)}", params
        )
        await db.commit()
        return cursor.rowcount


async def delete_stale_wishlist_entries(
    platform: str,
    source: str,
    keep_game_ids,
) -> int:
    """Delete (platform, source) game_wishlist rows not in keep_game_ids.

    Reconciles removals — a game taken off the upstream wishlist (without
    being bought) shouldn't linger locally forever. Scoped to (platform,
    source) so it never touches manual entries or another source's rows.

    Callers MUST only invoke this after confirming the source wishlist was
    fetched and resolved in full this round: keep_game_ids should be every
    game_id the sync could account for. An empty or partial keep_game_ids from
    a failed, partial, or per-item-unresolved fetch would otherwise wipe real
    entries — this function has no way to tell "genuinely removed upstream"
    apart from "we just couldn't confirm it this time".
    """
    keep_ids = list(keep_game_ids)
    async with get_db() as db:
        if not keep_ids:
            cursor = await db.execute(
                "DELETE FROM game_wishlist WHERE platform = ? AND source = ?",
                (platform, source),
            )
        else:
            placeholders = ",".join("?" * len(keep_ids))
            cursor = await db.execute(
                f"DELETE FROM game_wishlist WHERE platform = ? AND source = ? "
                f"AND game_id NOT IN ({placeholders})",
                (platform, source, *keep_ids),
            )
        await db.commit()
        return cursor.rowcount


async def repair_misclassified_platform_row(
    *,
    source_game_id: int,
    target_game_id: int,
    platform: str,
) -> bool:
    """Move a same-platform row from a bad fuzzy match to the corrected game."""
    if source_game_id == target_game_id:
        return False

    async with get_db() as db:
        source = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (source_game_id, platform),
        )
        if source is None:
            return False

        target = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (target_game_id, platform),
        )

        source_platform_id = source["id"]
        if target is None:
            await db.execute(
                "UPDATE game_platforms SET game_id = ? WHERE id = ?",
                (target_game_id, source_platform_id),
            )
        else:
            target_platform_id = target["id"]
            await db.execute(
                "UPDATE game_platform_identifiers SET game_platform_id = ? WHERE game_platform_id = ?",
                (target_platform_id, source_platform_id),
            )
            await db.execute("DELETE FROM game_platforms WHERE id = ?", (source_platform_id,))

        await db.commit()
        return True


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


async def upsert_game_alias(
    game_id: int,
    alias: str,
    *,
    alias_type: str = "edition",
    source: str | None = None,
    source_key: str | None = None,
) -> None:
    alias_normalized = normalize_search_text(alias)
    if not alias_normalized:
        return

    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT id FROM game_aliases
               WHERE game_id = ?
                 AND alias_normalized = ?
                 AND alias_type = ?
                 AND COALESCE(source, '') = COALESCE(?, '')
                 AND COALESCE(source_key, '') = COALESCE(?, '')
               LIMIT 1""",
            (game_id, alias_normalized, alias_type, source, source_key),
        )
        if row is None:
            await db.execute(
                """INSERT INTO game_aliases
                   (game_id, alias, alias_normalized, alias_type, source, source_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (game_id, alias, alias_normalized, alias_type, source, source_key),
            )
        else:
            await db.execute(
                """UPDATE game_aliases
                   SET alias = ?, source = ?, source_key = ?
                   WHERE id = ?""",
                (alias, source, source_key, row["id"]),
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
                   row_order INTEGER NOT NULL,
                   resolved_game_id INTEGER
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

            # Resolve each appid ONCE, up front, into temp.resolved_game_id, in two
            # passes so resolution is stable across the later platform inserts (which
            # add Steam rows the steam-row guard would otherwise start excluding):
            #
            #   Pass 1 — its own steam_appid identifier (a re-sync).
            #   Pass 2 — for appids still unresolved, an existing same-name game that
            #            does NOT already own a Steam row (a cross-platform attach onto
            #            an Epic/GOG/etc. entry). Crucially only ONE appid per name may
            #            claim that single Steam-less row (the lowest row_order); other
            #            same-name appids stay NULL and get their own row below.
            # The steam-row guard is the anti-collapse fix: a second appid whose name is
            # already taken by a Steam game is a distinct edition (Dead Space 2008 vs
            # 2023), not the same game, so it stays NULL.
            await db.execute(
                """UPDATE temp_steam_library_sync AS t
                   SET resolved_game_id = (
                       SELECT gp.game_id
                       FROM game_platform_identifiers gpi
                       JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                       WHERE gpi.identifier_type = ?
                         AND gpi.identifier_value = CAST(t.appid AS TEXT)
                       LIMIT 1
                   )""",
                (STEAM_APP_ID,),
            )

            await db.execute(
                """UPDATE temp_steam_library_sync AS t
                   SET resolved_game_id = (
                       SELECT g.id
                       FROM games g
                       WHERE lower(g.name) = lower(t.name)
                         AND NOT EXISTS (
                             SELECT 1 FROM game_platforms gp_excl
                             WHERE gp_excl.game_id = g.id
                               AND gp_excl.platform = 'steam'
                         )
                       ORDER BY g.id
                       LIMIT 1
                   )
                   WHERE t.resolved_game_id IS NULL
                     AND t.row_order = (
                         SELECT MIN(t2.row_order)
                         FROM temp_steam_library_sync t2
                         WHERE t2.resolved_game_id IS NULL
                           AND lower(t2.name) = lower(t.name)
                     )"""
            )

            await db.execute(
                """UPDATE games
                   SET name = (
                       SELECT t.name
                       FROM temp_steam_library_sync t
                       WHERE t.resolved_game_id = games.id
                       ORDER BY t.row_order DESC
                       LIMIT 1
                   ),
                   name_normalized = NULL
                   WHERE id IN (
                       SELECT resolved_game_id
                       FROM temp_steam_library_sync
                       WHERE resolved_game_id IS NOT NULL
                   )
                   AND (manual_overrides IS NULL
                        OR 'name' NOT IN (SELECT value FROM json_each(manual_overrides)))"""
            )

            await db.execute(
                """INSERT INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   SELECT t.resolved_game_id, ?, 1,
                          t.playtime_minutes,
                          t.playtime_2weeks_minutes,
                          ?
                   FROM temp_steam_library_sync t
                   WHERE t.resolved_game_id IS NOT NULL
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
                (STEAM_PLATFORM, synced_at),
            )

            await db.execute(
                """INSERT INTO game_platform_identifiers
                   (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
                   SELECT gp.id, ?, CAST(t.appid AS TEXT), 1, ?
                   FROM temp_steam_library_sync t
                   JOIN game_platforms gp
                     ON gp.game_id = t.resolved_game_id AND gp.platform = ?
                   WHERE t.resolved_game_id IS NOT NULL
                   ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                       game_platform_id = excluded.game_platform_id,
                       is_primary = excluded.is_primary,
                       last_seen_at = excluded.last_seen_at""",
                (STEAM_APP_ID, synced_at, STEAM_PLATFORM),
            )

            await db.execute(
                """INSERT INTO steam_platform_data
                   (game_platform_id, rtime_last_played, library_updated_at)
                   SELECT gp.id, t.rtime_last_played, ?
                   FROM temp_steam_library_sync t
                   JOIN game_platforms gp
                     ON gp.game_id = t.resolved_game_id AND gp.platform = ?
                   WHERE t.resolved_game_id IS NOT NULL
                   ON CONFLICT(game_platform_id) DO UPDATE SET
                       rtime_last_played = excluded.rtime_last_played,
                       library_updated_at = excluded.library_updated_at""",
                (synced_at, STEAM_PLATFORM),
            )

            # Appids that resolved to NULL (no appid match, no Steam-less same-name
            # row) are genuinely new games. Create one row *per appid* on this same
            # connection so two distinct appids that happen to share a name (a fresh
            # sync owning both Dead Spaces) never collapse the way a name GROUP BY would.
            new_rows = await db.execute_fetchall(
                """SELECT appid, name, playtime_minutes,
                          playtime_2weeks_minutes, rtime_last_played
                   FROM temp_steam_library_sync
                   WHERE resolved_game_id IS NULL
                   ORDER BY row_order"""
            )
            for new in new_rows:
                cursor = await db.execute(
                    "INSERT INTO games (name, name_normalized) VALUES (?, ?)",
                    (new["name"], normalize_search_text(new["name"])),
                )
                new_game_id = cursor.lastrowid
                cursor = await db.execute(
                    """INSERT INTO game_platforms
                       (game_id, platform, owned, playtime_minutes,
                        playtime_2weeks_minutes, last_synced)
                       VALUES (?, ?, 1, ?, ?, ?)""",
                    (new_game_id, STEAM_PLATFORM, new["playtime_minutes"],
                     new["playtime_2weeks_minutes"], synced_at),
                )
                new_platform_id = cursor.lastrowid
                await db.execute(
                    """INSERT INTO game_platform_identifiers
                       (game_platform_id, identifier_type, identifier_value,
                        is_primary, last_seen_at)
                       VALUES (?, ?, ?, 1, ?)""",
                    (new_platform_id, STEAM_APP_ID, str(new["appid"]), synced_at),
                )
                await db.execute(
                    """INSERT INTO steam_platform_data
                       (game_platform_id, rtime_last_played, library_updated_at)
                       VALUES (?, ?, ?)""",
                    (new_platform_id, new["rtime_last_played"], synced_at),
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


async def upsert_game_series_links(game_id: int, series_entries) -> None:
    """Link a game to its series (IGDB collections/franchises).

    ``series_entries`` is an iterable of (kind, igdb_id, name) tuples, where
    ``kind`` is "collection" or "franchise". Series rows are deduped on
    (kind, igdb_id); memberships are add-only and idempotent, so re-running
    IGDB backfill never creates duplicates.
    """
    entries = [
        (kind, igdb_id, name)
        for kind, igdb_id, name in series_entries
        if igdb_id is not None and name
    ]
    if not entries:
        return

    async with get_db() as db:
        for kind, igdb_id, name in entries:
            await db.execute(
                """INSERT INTO game_series (kind, igdb_id, name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(kind, igdb_id) DO UPDATE SET name = excluded.name""",
                (kind, igdb_id, name),
            )
            row = await db.execute_fetchone(
                "SELECT id FROM game_series WHERE kind = ? AND igdb_id = ?",
                (kind, igdb_id),
            )
            await db.execute(
                """INSERT OR IGNORE INTO game_series_membership (game_id, series_id)
                   VALUES (?, ?)""",
                (game_id, row["id"]),
            )
        await db.commit()


async def upsert_nintendo_play_summary(rows: list[dict]) -> int:
    """Idempotently upsert Parental Controls play-summary rows.

    Each row dict carries: device_id, application_id, period_type, period_key,
    playtime_minutes, and (optional) app_name. The natural primary key
    (device_id, application_id, period_type, period_key) makes re-syncing a day
    a no-op overwrite rather than a double-count. Returns the row count written.
    """
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.executemany(
            """INSERT INTO nintendo_play_summary
               (device_id, application_id, period_type, period_key,
                playtime_minutes, app_name, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, application_id, period_type, period_key)
               DO UPDATE SET
                   playtime_minutes = excluded.playtime_minutes,
                   app_name = COALESCE(excluded.app_name, nintendo_play_summary.app_name),
                   updated_at = excluded.updated_at""",
            [
                (
                    str(r["device_id"]),
                    str(r["application_id"]),
                    r["period_type"],
                    r["period_key"],
                    int(r["playtime_minutes"]),
                    r.get("app_name"),
                    now,
                )
                for r in rows
            ],
        )
        await db.commit()
    return len(rows)
