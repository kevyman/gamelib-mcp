"""Game/platform/identifier/enrichment upserts, incl. bulk Steam library sync."""

import json
import logging
from datetime import UTC, datetime

from ..content import (
    CONTENT_BASE_GAME,
    CONTENT_EDITION,
    NESTED_CONTENT_TYPES,
    ContentClassification,
    derive_is_primary,
)
from ..title_normalization import normalize_search_text
from . import (
    NINTENDO_TITLE_ID_TYPE,
    STEAM_APP_ID,
    STEAM_PLATFORM,
    _backfill_name_normalized,
    _iter_chunks,
    get_db,
    normalize_identifier_value,
    retry_on_write_contention,
)

logger = logging.getLogger(__name__)

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
    "completion_status",
    "cover_image_id",
    "igdb_id",
    "igdb_platforms",
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


# game_platforms columns a user may pin by hand (set_playtime for the playtime
# pair, add_game_to_platform for delisted and owned). Unlike the
# ACQUISITION_FIELDS below, these ARE written by platform syncs, so pinning one
# records it in game_platforms.manual_overrides and the sync write paths
# (upsert_game_platform, bulk_upsert_steam_library, set_steam_delisted) skip a
# protected column. set_playtime(clear=[...]) hands any of them back to sync.
PLATFORM_EDITABLE_FIELDS = {
    "playtime_minutes",
    "last_played",
    "delisted",
    # Pinned by add_game_to_platform(unowned_at=…) — see schema.py's v34 note.
    # A source that keeps listing a title you no longer own (Xbox ownership is
    # title HISTORY; it never forgets a game you once launched) would otherwise
    # re-own the row on the next sync and quietly undo the correction.
    "owned",
}


async def get_platform_manual_overrides(db, game_platform_id: int) -> set[str]:
    """Return the set of game_platforms columns pinned by hand on one row.

    ``db`` is an open connection (the sync write paths consult this inline via
    json_each; this helper is for the Python-side read used by set_playtime).
    """
    row = await db.execute_fetchone(
        "SELECT manual_overrides FROM game_platforms WHERE id = ?",
        (game_platform_id,),
    )
    return _decode_overrides(row["manual_overrides"]) if row else set()


async def apply_manual_platform_fields(game_platform_id: int, fields: dict) -> set[str]:
    """Write user-supplied game_platforms columns and record them as overrides.

    Merges the written column names into game_platforms.manual_overrides so later
    platform syncs won't overwrite them. Returns the full override set after the
    write. Unknown columns (outside PLATFORM_EDITABLE_FIELDS) are rejected.
    """
    unknown = set(fields) - PLATFORM_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"not editable game_platforms columns: {sorted(unknown)}")
    if not fields:
        async with get_db() as db:
            return await get_platform_manual_overrides(db, game_platform_id)

    async with get_db() as db:
        current = await get_platform_manual_overrides(db, game_platform_id)
        merged = current | set(fields)
        updates = dict(fields)
        updates["manual_overrides"] = json.dumps(sorted(merged))
        cols_sql = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE game_platforms SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_platform_id),
        )
        await db.commit()
        return merged


async def remove_platform_manual_overrides(game_platform_id: int, columns) -> set[str]:
    """Stop protecting the given game_platforms columns so sync may update them.

    Removing protection does not change the current value — it just lets the next
    platform sync overwrite it. Returns the remaining override set.
    """
    async with get_db() as db:
        remaining = await get_platform_manual_overrides(db, game_platform_id) - set(columns)
        await db.execute(
            "UPDATE game_platforms SET manual_overrides = ? WHERE id = ?",
            (json.dumps(sorted(remaining)) if remaining else None, game_platform_id),
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
        # is_primary_library_item is ALWAYS derived from content_type (ADR
        # 0002). A caller passing content_type without the derived flag would
        # otherwise write the internally inconsistent "nested type + primary"
        # shape that hides rows from both the games and addons views.
        if "content_type" in updates and "is_primary_library_item" not in updates:
            updates["is_primary_library_item"] = int(
                derive_is_primary(updates["content_type"])
            )
        # Never persist a self-referencing parent: it would orphan the row from
        # both search (the is_primary filter) and its parent's editions list. Drop
        # the self-parent and keep the row a primary library item — forcing the
        # content_type back to base_game too, so is_primary stays derived from
        # content_type (a 'dlc' + primary row would be invisible to BOTH the
        # games and addons views).
        if updates.get("parent_game_id") == game_id:
            updates["parent_game_id"] = None
            if "is_primary_library_item" in updates:
                updates["is_primary_library_item"] = 1
                if updates.get("content_type") in NESTED_CONTENT_TYPES:
                    updates["content_type"] = CONTENT_BASE_GAME
        cols_sql = ", ".join(f"{column} = ?" for column in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()
        return game_id


async def resolve_parent_game(
    name: str | None,
    *,
    steam_appid: int | None = None,
    exclude_game_id: int | None = None,
    create: bool = False,
) -> int | None:
    """Find (or optionally mint) the games row a nested item belongs under.

    Used by non-IGDB classifiers (Steam store enrichment, purchase importers) to
    resolve a parent before writing content classification. Tries the Steam
    ``steam_appid`` identifier first, then an exact ``lower(name)`` match, then a
    normalized-name match (same fallback ladder upsert_game/adopt use). A
    candidate equal to ``exclude_game_id`` (the child itself) is never returned.
    With ``create=True`` and a non-empty name, an unmatched name mints a bare
    primary row via upsert_game; ``create=False`` returns None instead.
    """
    if steam_appid is not None:
        from .queries import get_game_by_identifier

        row = await get_game_by_identifier(STEAM_APP_ID, str(steam_appid))
        if row is not None and row["id"] != exclude_game_id:
            return row["id"]

    cleaned = name.strip() if name else ""
    if cleaned:
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
                (cleaned,),
            )
            if row is None:
                normalized = normalize_search_text(cleaned)
                if normalized:
                    row = await db.execute_fetchone(
                        "SELECT id FROM games WHERE COALESCE(name_normalized, '') = ? "
                        "ORDER BY id LIMIT 1",
                        (normalized,),
                    )
        if row is not None and row["id"] != exclude_game_id:
            return row["id"]

    if create and cleaned:
        return await upsert_game(None, cleaned)

    return None


async def apply_content_classification(
    game_id: int,
    classification: ContentClassification,
    *,
    source: str,
    parent_game_id: int | None = None,
) -> bool:
    """Write a ContentClassification onto a games row, honoring the shared guards.

    The reusable writer behind non-IGDB classifiers (Steam store enrichment,
    purchase importers). It never mints parent rows: callers that want minting
    resolve the parent themselves (e.g. resolve_parent_game(create=True)) and
    pass it as ``parent_game_id`` — supplying that kwarg skips resolution
    entirely. Otherwise the parent is resolved (non-minting) from the
    classification's parent_steam_appid, then parent_igdb_id, then parent_name.

    Guards, in order, kept faithful to igdb.py::_apply_igdb_metadata (the two
    MUST stay in sync — see the mirroring note there):
      1. manual-overrides skip — content_type/parent_game_id/is_primary_library_item
         writes are dropped for any column the user pinned via update_game.
      2. default-clobber guard — a bare base_game/primary/no-parent signal never
         overwrites a stored non-default classification (a later Steam/purchase
         pass must not flip a nested DLC back to a primary library item).
      3. self-parent guard — a parent equal to game_id is dropped (the row keeps
         its content_type, parent nulled).
    is_primary_library_item is always derived from content_type via
    derive_is_primary, never taken from the classification independently.
    Returns True iff a write happened; ``source`` labels the log line only.
    """
    resolved_parent = parent_game_id
    # Each parent id is tried in strength order, but a miss falls through to
    # the next: a Steam fullgame appid the library doesn't know must not block
    # resolving the same parent by name (e.g. base game owned on GOG).
    if resolved_parent is None and classification.parent_steam_appid is not None:
        resolved_parent = await resolve_parent_game(
            None,
            steam_appid=classification.parent_steam_appid,
            exclude_game_id=game_id,
        )
    if resolved_parent is None and classification.parent_igdb_id is not None:
        from .queries import get_game_by_igdb_id

        parent = await get_game_by_igdb_id(classification.parent_igdb_id)
        if parent is not None and parent["id"] != game_id:
            resolved_parent = parent["id"]
    if resolved_parent is None and classification.parent_name:
        resolved_parent = await resolve_parent_game(
            classification.parent_name, exclude_game_id=game_id, create=False
        )

    # Self-parent guard: a parent that is the row itself would orphan it (excluded
    # from search/rollups by the is_primary filter yet unreachable as any other
    # row's edition), so drop it and keep the content_type write.
    if resolved_parent == game_id:
        resolved_parent = None

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT content_type, parent_game_id, is_primary_library_item "
            "FROM games WHERE id = ?",
            (game_id,),
        )
        if row is None:
            return False

        # Parent guard: a row other rows nest under must stay primary. Nesting it
        # would hide the parent from the is_primary rollups and strand its
        # children under an unreachable row, so drop the whole classification
        # write rather than write half of it. Mirrored in _apply_igdb_metadata.
        if classification.content_type in NESTED_CONTENT_TYPES:
            from .queries import has_nested_children

            if await has_nested_children(db, game_id):
                logger.debug(
                    "content classification (%s) would nest game %s, which is a "
                    "parent of nested content; skipped",
                    source,
                    game_id,
                )
                return False

        # Substance guard — a row with a store identifier and real playtime is
        # never demoted under a parent that has neither (the Titanfall 2 shape:
        # the real, played game nested under an empty duplicate, leaving only
        # the hollow parent visible). Skip the whole classification write; the
        # row stays a primary item and detect_misclassified_dlc keeps
        # surfacing the pair for a human to merge or reparent.
        # NOTE: the same guard lives in igdb.py::_apply_igdb_metadata — keep
        # the two in sync (see the mirroring note in the docstring above).
        if (
            resolved_parent is not None
            and classification.content_type in NESTED_CONTENT_TYPES
        ):
            from .queries import nesting_substance_conflict

            if await nesting_substance_conflict(db, game_id, resolved_parent):
                logger.info(
                    "content classification (%s) for game %s skipped: nesting a "
                    "row with identifier+playtime under empty parent %s",
                    source,
                    game_id,
                    resolved_parent,
                )
                return False

        # Edition-ownership guard (mirrors igdb.py::_apply_igdb_metadata — keep
        # in sync): an OWNED row is never demoted to an edition of a parent
        # nobody owns; the edition IS the ownership record, and hiding it makes
        # the empty parent a false orphan. Ownership-keyed, unlike the
        # playtime-keyed substance guard above, so owned-but-unplayed editions
        # are protected too.
        if classification.content_type == CONTENT_EDITION:
            from .queries import edition_hides_owned_game

            if await edition_hides_owned_game(db, game_id, resolved_parent):
                logger.info(
                    "content classification (%s) for game %s skipped: demoting "
                    "an owned row to an edition of unowned parent %s",
                    source,
                    game_id,
                    resolved_parent,
                )
                return False

        overrides = await get_manual_overrides(db, game_id)

        # Default-clobber guard — mirrors igdb.py's new_is_default/stored_is_default.
        new_is_default = (
            classification.content_type == CONTENT_BASE_GAME
            and classification.is_primary_library_item
            and resolved_parent is None
        )
        stored_is_default = (
            row["content_type"] == CONTENT_BASE_GAME
            and bool(row["is_primary_library_item"])
            and row["parent_game_id"] is None
        )

        updates: dict = {}
        if not new_is_default or stored_is_default:
            if "content_type" not in overrides:
                updates["content_type"] = classification.content_type
            # Derive everything below from the content_type that will ACTUALLY
            # be stored: when the content_type write was skipped (pinned by a
            # manual override), deriving from the incoming classification
            # would desync the pair (e.g. a pinned 'dlc' row flipped primary
            # by a later remaster verdict).
            final_content_type = updates.get("content_type", row["content_type"])
            if "parent_game_id" not in overrides:
                if (
                    resolved_parent is not None
                    and final_content_type in NESTED_CONTENT_TYPES
                ):
                    updates["parent_game_id"] = resolved_parent
                elif (
                    derive_is_primary(final_content_type)
                    and row["parent_game_id"] is not None
                ):
                    # A primary library item must not keep a parent (the
                    # update_game promotion invariant): a non-default primary
                    # verdict clears the leftover link a wrong earlier nested
                    # classification wrote. Mirrored in _apply_igdb_metadata.
                    updates["parent_game_id"] = None
            if "is_primary_library_item" not in overrides:
                updates["is_primary_library_item"] = int(
                    derive_is_primary(final_content_type)
                )

        if not updates:
            return False

        # Optimistic concurrency: the read→guard→write above spans awaits, and
        # the Steam path has no claim serialization (a lazy detail-view enrich
        # and the background store worker can race the same stale appid). Guard
        # the UPDATE on the snapshot we judged against; losing the race means
        # the other writer's verdict is at least as fresh — discard ours.
        cols_sql = ", ".join(f"{col} = ?" for col in updates)
        cursor = await db.execute(
            f"""UPDATE games SET {cols_sql}
                WHERE id = ? AND content_type IS ? AND parent_game_id IS ?
                  AND is_primary_library_item = ?""",
            (
                *updates.values(),
                game_id,
                row["content_type"],
                row["parent_game_id"],
                row["is_primary_library_item"],
            ),
        )
        if cursor.rowcount == 0:
            logger.debug(
                "content classification (%s) for game %s lost a concurrent write; skipped",
                source,
                game_id,
            )
            return False
        await db.commit()

    logger.info("applied content classification (%s) to game %s: %s", source, game_id, updates)
    return True


@retry_on_write_contention
async def upsert_game_platform(
    game_id: int,
    platform: str,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    last_played: str | None = None,
    owned: int = 1,
    *,
    from_source: bool = False,
) -> int:
    """Insert or update a game_platforms row and return its id.

    ``last_played`` is an ISO date string (``YYYY-MM-DD``) for the platform's own
    last-played signal (Steam stores its own in steam_platform_data; Nintendo and
    PSN write it here). Like the playtime columns it only advances when a non-NULL
    value is supplied, so an ownership-only sync never clears it.

    ``from_source=True`` means the platform's own source returned this row on
    this run — every platform sync passes it, no manual tool does. It stamps
    last_seen_in_source (see schema.py's v34 note) and, when the row is
    genuinely re-owned, clears unowned_at: the source listing a title you had
    marked refunded/revoked is positive evidence you own it again. A row whose
    ``owned`` is pinned in manual_overrides keeps both its flag and its
    unowned_at, like every other protected column — but still gets the
    last_seen_in_source stamp, which records what the source SAID rather than
    what the row concluded.
    """
    now = datetime.now(UTC).isoformat()
    seen_at = now if from_source else None
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes,
                last_played, last_synced, last_seen_in_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned = CASE
                       WHEN game_platforms.manual_overrides IS NOT NULL
                            AND 'owned' IN (
                                SELECT value FROM json_each(game_platforms.manual_overrides))
                       THEN game_platforms.owned
                       ELSE excluded.owned
                   END,
                   unowned_at = CASE
                       WHEN game_platforms.manual_overrides IS NOT NULL
                            AND 'owned' IN (
                                SELECT value FROM json_each(game_platforms.manual_overrides))
                       THEN game_platforms.unowned_at
                       WHEN excluded.owned = 1 THEN NULL
                       ELSE game_platforms.unowned_at
                   END,
                   last_seen_in_source = COALESCE(
                       excluded.last_seen_in_source,
                       game_platforms.last_seen_in_source
                   ),
                   playtime_minutes = CASE
                       WHEN game_platforms.manual_overrides IS NOT NULL
                            AND 'playtime_minutes' IN (
                                SELECT value FROM json_each(game_platforms.manual_overrides))
                       THEN game_platforms.playtime_minutes
                       ELSE COALESCE(excluded.playtime_minutes, game_platforms.playtime_minutes)
                   END,
                   playtime_2weeks_minutes = COALESCE(
                       excluded.playtime_2weeks_minutes,
                       game_platforms.playtime_2weeks_minutes
                   ),
                   last_played = CASE
                       WHEN game_platforms.manual_overrides IS NOT NULL
                            AND 'last_played' IN (
                                SELECT value FROM json_each(game_platforms.manual_overrides))
                       THEN game_platforms.last_played
                       ELSE COALESCE(excluded.last_played, game_platforms.last_played)
                   END,
                   last_synced = excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes,
             last_played, now, seen_at),
        )
        row = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        await db.commit()
        return row["id"]


async def set_platform_ownership(
    game_platform_id: int,
    *,
    owned: bool,
    unowned_at: str | None = None,
) -> dict:
    """Flip one existing ownership row's ``owned`` flag and its unowned_at stamp.

    The write path for a refund, a revoked key, or a lapsed subscription title
    — states that were previously indistinguishable from permanent ownership
    (see docs/adr/0007-ownership-lifecycle.md). Deliberately NOT deletion: the
    row keeps its acquisition history, its identifiers, and its playtime, and
    every aggregate already filters ``owned = 1``, so it simply stops counting.

    ``owned=False`` stamps unowned_at and PINS ``owned`` in manual_overrides so
    no sync re-owns it; ``owned=True`` clears both the stamp and the pin,
    handing the column back to sync. Returns the row's resulting ownership
    state.
    """
    stamp = unowned_at or datetime.now(UTC).isoformat()
    if owned:
        async with get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET owned = 1, unowned_at = NULL WHERE id = ?",
                (game_platform_id,),
            )
            await db.commit()
        overrides = await remove_platform_manual_overrides(game_platform_id, ["owned"])
    else:
        # apply_manual_platform_fields writes the flag and records the pin in
        # one statement; unowned_at rides along as an ordinary column (no sync
        # writer sets it, so it needs no protection of its own).
        overrides = await apply_manual_platform_fields(game_platform_id, {"owned": 0})
        async with get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET unowned_at = ? WHERE id = ?",
                (stamp, game_platform_id),
            )
            await db.commit()

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT owned, unowned_at, last_seen_in_source FROM game_platforms WHERE id = ?",
            (game_platform_id,),
        )
    if row is None:
        raise ValueError(f"game_platforms row {game_platform_id} not found")
    return {
        "owned": bool(row["owned"]),
        "unowned_at": row["unowned_at"],
        "last_seen_in_source": row["last_seen_in_source"],
        "manual_overrides": sorted(overrides),
    }


# game_platforms columns holding user/importer-supplied acquisition data. No
# sync writer references them (upsert_game_platform / bulk_upsert_steam_library
# enumerate their columns explicitly), so they never need manual_overrides
# protection — set_platform_acquisition is their only write path.
ACQUISITION_FIELDS = (
    "acquired_at",
    "price_paid",
    "price_currency",
    "purchase_source",
    "bundle_name",
)


async def set_platform_acquisition(
    game_platform_id: int,
    fields: dict,
    *,
    only_if_null: bool = False,
) -> dict:
    """Write acquisition columns on one game_platforms row.

    Overwrite mode (default): ``col = ?`` — an explicit None clears the column.
    ``only_if_null`` (importer mode): ``col = COALESCE(col, ?)`` — never
    replaces an existing value, so re-imports can't clobber manual edits.

    Returns the row's resulting acquisition state (the 5 columns), letting
    callers distinguish "filled" from "already set" without a second query.
    """
    unknown = set(fields) - set(ACQUISITION_FIELDS)
    if unknown:
        raise ValueError(f"not acquisition columns: {sorted(unknown)}")

    async with get_db() as db:
        if fields:
            if only_if_null:
                cols_sql = ", ".join(f"{col} = COALESCE({col}, ?)" for col in fields)
            else:
                cols_sql = ", ".join(f"{col} = ?" for col in fields)
            await db.execute(
                f"UPDATE game_platforms SET {cols_sql} WHERE id = ?",
                (*fields.values(), game_platform_id),
            )
            await db.commit()
        row = await db.execute_fetchone(
            f"SELECT {', '.join(ACQUISITION_FIELDS)} FROM game_platforms WHERE id = ?",
            (game_platform_id,),
        )
    if row is None:
        raise ValueError(f"game_platforms row {game_platform_id} not found")
    return {col: row[col] for col in ACQUISITION_FIELDS}


async def upsert_wishlist_entry(
    game_id: int,
    platform: str,
    wishlisted_at: str | None = None,
    source: str | None = None,
    store_identifier: str | None = None,
    *,
    overwrite_source: bool = True,
) -> dict:
    """Insert or update a game_wishlist row; returns
    {"id": int, "created": bool, "source": str | None}.

    Lives in its own table rather than game_platforms — a wishlist item may not
    be owned anywhere yet, and game_platforms rows are meant to mean "a real
    platform relationship exists" (owned, or a manual stub). source records
    where the entry came from (e.g. "steam", "dekudeals", "manual",
    "assessment"). store_identifier captures the store's own ID (e.g. Steam
    appid) at sync time.

    overwrite_source=True (default, the sync behavior) stamps ``source`` even
    onto an existing row — that's how a manual/assessment row converges to
    source="steam" once the real Steam wishlist re-observes it. The manual
    tool path passes False: an existing row's provenance is never rewritten by
    a hand write, so promoting an already-wishlisted game can't relabel a
    hand-curated entry (making it bulk-removable as an assessment row) or pull
    a steam-sourced row out of the sync's source-scoped removal
    reconciliation. The returned "source" is what the row actually holds after
    the call — the preserved value when an update kept it.

    created reports whether this call minted the row (vs updating one in
    place) ATOMICALLY: the wishlist syncs count added/matched from it, and a
    separate exists-then-upsert pair could misreport under a concurrent sync
    or manual add (nothing serializes sync_wishlist). BEGIN IMMEDIATE makes
    the read+write a single writer transaction from its first statement, so
    it queues under busy_timeout instead of building a read snapshot a
    concurrent commit invalidates (see the write-contention pattern in
    CLAUDE.md).

    The default wishlisted_at uses second precision to match what the syncs
    write (Steam date_added is epoch seconds) — a manual add shouldn't be
    distinguishable from a synced row by timestamp format.
    """
    now = wishlisted_at or datetime.now(UTC).isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await db.execute_fetchone(
            "SELECT id FROM game_wishlist WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        # "source = source" (unqualified) keeps the EXISTING row's value on
        # conflict; a fresh insert still records the passed source either way.
        source_set = "excluded.source" if overwrite_source else "source"
        await db.execute(
            f"""INSERT INTO game_wishlist (game_id, platform, wishlisted_at, source, store_identifier)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   wishlisted_at = excluded.wishlisted_at,
                   source = {source_set},
                   store_identifier = COALESCE(excluded.store_identifier, store_identifier)""",
            (game_id, platform, now, source, store_identifier),
        )
        row = await db.execute_fetchone(
            "SELECT id, source FROM game_wishlist WHERE game_id = ? AND platform = ?",
            (game_id, platform),
        )
        await db.commit()
        return {"id": row["id"], "created": existing is None, "source": row["source"]}


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
    where = [
        (
            "EXISTS (SELECT 1 FROM game_platforms gp "
            "WHERE gp.game_id = game_wishlist.game_id "
            "AND gp.platform = game_wishlist.platform AND gp.owned = 1)"
        )
    ]
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


async def stamp_wishlist_alerts(keys_by_game: dict[int, str]) -> int:
    """Record that a deal alert went out, per game: {game_id: alert_key}.

    Written ONLY after the notification was actually delivered (see
    ``deal_alerts.py``) — stamping an alert that failed to send would silence
    the retry, and a missed price drop is exactly what the feature exists to
    prevent.

    Every wishlist row for the game is stamped, not just one platform's: the
    key describes an event about the GAME ("target reached at 19.99"), and
    leaving a sibling row unstamped would let the same event alert twice
    through the other platform. Returns the number of rows stamped.
    """
    if not keys_by_game:
        return 0
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        cursor = await db.executemany(
            """UPDATE game_wishlist
               SET last_alerted_at = ?, last_alert_key = ?
               WHERE game_id = ?""",
            [(now, key, game_id) for game_id, key in keys_by_game.items()],
        )
        rowcount = cursor.rowcount
        await db.commit()
    return rowcount if rowcount and rowcount > 0 else 0


async def repair_misclassified_platform_row(
    *,
    source_game_id: int,
    target_game_id: int,
    platform: str,
) -> bool:
    """Move a same-platform row from a bad fuzzy match to the corrected game."""
    if source_game_id == target_game_id:
        return False

    acquisition_cols = ", ".join(ACQUISITION_FIELDS)
    async with get_db() as db:
        source = await db.execute_fetchone(
            f"SELECT id, {acquisition_cols} FROM game_platforms WHERE game_id = ? AND platform = ?",
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
            # Acquisition data would be silently dropped with the source row's
            # DELETE below: fill each target column that is NULL from the
            # source (target wins on conflict — mirrors merge_games).
            acq_sql = ", ".join(
                f"{col} = COALESCE({col}, ?)" for col in ACQUISITION_FIELDS
            )
            await db.execute(
                f"UPDATE game_platforms SET {acq_sql} WHERE id = ?",
                (*(source[col] for col in ACQUISITION_FIELDS), target_platform_id),
            )
            await db.execute("DELETE FROM game_platforms WHERE id = ?", (source_platform_id,))

        await db.commit()
        return True


async def adopt_platform_identifier(
    *,
    name: str,
    platform: str,
    identifier_type: str,
    identifier_value: str | int,
    reference_release_date: str | None = None,
) -> int | None:
    """Attach a store identifier to an existing identifier-less platform row.

    Sync reconciliation calls this after the store-identifier lookup missed.
    If exactly one games row has the same normalized name AND already owns a
    ``platform`` row carrying NO identifier of ``identifier_type`` (typically a
    row ingested before that identifier type was recorded), the identifier is
    adopted onto that platform row and the game id returned — instead of the
    name/fuzzy fallback's ``exclude_platform`` guard pushing the sync into
    creating a stranded duplicate game row (observed in prod: 6 PS5 pairs like
    "Tiny Tina's Wonderlands" with one identifier-bearing and one frozen
    identifier-less twin).

    Returns None (caller falls back to normal resolution) when:
    * no candidate, or more than one same-name candidate (ambiguous);
    * the candidate's release year conflicts with ``reference_release_date``
      (same year guard as find_game_by_name_fuzzy — never adopt across a
      remake boundary);
    * every same-name platform row already has an identifier of this type —
      two identifier-bearing rows are distinct store entries and must stay
      separate (anti-collapse invariant).
    """
    normalized = normalize_search_text(name)
    if not normalized:
        return None

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.release_date, gp.id AS game_platform_id
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = ?
               WHERE COALESCE(g.name_normalized, '') = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM game_platform_identifiers gpi
                     WHERE gpi.game_platform_id = gp.id
                       AND gpi.identifier_type = ?
                 )""",
            (platform, normalized, identifier_type),
        )

    if len(rows) != 1:
        return None
    row = rows[0]

    if reference_release_date:
        from .fuzzy import _release_year

        ref_year = _release_year(reference_release_date)
        row_year = _release_year(row["release_date"])
        if ref_year is not None and row_year is not None and ref_year != row_year:
            return None

    await upsert_game_platform_identifier(
        row["game_platform_id"], identifier_type, identifier_value
    )
    return row["game_id"]


@retry_on_write_contention
async def upsert_game_platform_identifier(
    game_platform_id: int,
    identifier_type: str,
    identifier_value: str | int,
    *,
    is_primary: bool = True,
) -> None:
    # The write chokepoint: normalize once here (e.g. nintendo_title_id ->
    # uppercase) so every reader can compare with plain equality instead of
    # re-normalizing at read time. See normalize_identifier_value's docstring.
    identifier_value = normalize_identifier_value(identifier_type, str(identifier_value))
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platform_identifiers
               (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                   game_platform_id = excluded.game_platform_id,
                   is_primary = excluded.is_primary,
                   last_seen_at = excluded.last_seen_at""",
            (game_platform_id, identifier_type, identifier_value, int(is_primary), now),
        )
        if is_primary:
            row = await db.execute_fetchone(
                "SELECT id FROM game_platform_identifiers WHERE identifier_type = ? AND identifier_value = ?",
                (identifier_type, identifier_value),
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


@retry_on_write_contention
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


async def seed_platform_provider_alias(
    game_platform_id: int,
    alias: str,
    *,
    source: str,
    source_key: str | None = None,
) -> None:
    """Record a provider-known display name for the game owning a platform row.

    Enrichment providers routinely know a fuller title than the library row
    carries — a Metacritic/OpenCritic URL slug spells out
    "orwell-keeping-an-eye-on-you" while the row is named "Orwell" — and that
    fuller title is exactly what storefront purchase records and ownership
    screens later search for. Seeding it as an alias at enrich time lets the
    alias tiers (search_games, the acquisition matcher, the purchase-import
    near-duplicate guard) bridge the gap without any fuzzy heuristics that
    could collapse genuinely different games. A name that normalizes
    identically to the stored one adds no information and is skipped.
    """
    alias_normalized = normalize_search_text(alias)
    if not alias_normalized:
        return
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT g.id AS game_id, g.name AS name
               FROM game_platforms gp JOIN games g ON g.id = gp.game_id
               WHERE gp.id = ?""",
            (game_platform_id,),
        )
    if row is None or normalize_search_text(row["name"]) == alias_normalized:
        return
    await upsert_game_alias(
        row["game_id"],
        alias,
        alias_type="provider_name",
        source=source,
        source_key=source_key,
    )


@retry_on_write_contention
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


@retry_on_write_contention
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
            # Take the write lock BEFORE reading. Each chunk is a read-then-write
            # transaction (appid/name resolution against the live tables, then
            # platform writes), and a deferred transaction that reads first can
            # only fail when another writer commits in between: upgrading its
            # read snapshot raises SQLITE_BUSY_SNAPSHOT immediately, busy_timeout
            # deliberately not consulted. During a full library refresh the other
            # platform syncs commit continuously for the whole run, so the
            # retry_on_write_contention backstop (~1.5s of budget) lost that race
            # every time — Steam failed 100% of full refreshes while succeeding
            # alone. BEGIN IMMEDIATE makes this a writer from the start: it
            # QUEUES behind other writers under busy_timeout, and every read
            # below happens under a snapshot no one else can invalidate.
            await db.execute("BEGIN IMMEDIATE")
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

            # GetOwnedGames returning an appid IS the source seeing the row, so
            # every row written here is stamped last_seen_in_source (v34) — the
            # signal check_library's ownership.unseen_in_source reads. owned and
            # unowned_at follow upsert_game_platform's rules exactly: the pin
            # wins, otherwise a listed app is owned and its unowned_at clears.
            # last_played mirrors steam_platform_data.rtime_last_played (a unix
            # epoch) into the cross-platform ISO column, so Steam contributes to
            # the same last-played signal PSN and Nintendo write. 0 means "never
            # played" in GetOwnedGames, which is NULL here, not 1970-01-01.
            await db.execute(
                """INSERT INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes,
                    last_played, last_synced, last_seen_in_source)
                   SELECT t.resolved_game_id, ?, 1,
                          t.playtime_minutes,
                          t.playtime_2weeks_minutes,
                          CASE WHEN COALESCE(t.rtime_last_played, 0) > 0
                               THEN date(t.rtime_last_played, 'unixepoch') END,
                          ?, ?
                   FROM temp_steam_library_sync t
                   WHERE t.resolved_game_id IS NOT NULL
                   ON CONFLICT(game_id, platform) DO UPDATE SET
                       owned = CASE
                           WHEN game_platforms.manual_overrides IS NOT NULL
                                AND 'owned' IN (
                                    SELECT value FROM json_each(game_platforms.manual_overrides))
                           THEN game_platforms.owned
                           ELSE excluded.owned
                       END,
                       unowned_at = CASE
                           WHEN game_platforms.manual_overrides IS NOT NULL
                                AND 'owned' IN (
                                    SELECT value FROM json_each(game_platforms.manual_overrides))
                           THEN game_platforms.unowned_at
                           ELSE NULL
                       END,
                       last_seen_in_source = excluded.last_seen_in_source,
                       playtime_minutes = CASE
                           WHEN game_platforms.manual_overrides IS NOT NULL
                                AND 'playtime_minutes' IN (
                                    SELECT value FROM json_each(game_platforms.manual_overrides))
                           THEN game_platforms.playtime_minutes
                           ELSE COALESCE(
                               excluded.playtime_minutes,
                               game_platforms.playtime_minutes
                           )
                       END,
                       playtime_2weeks_minutes = COALESCE(
                           excluded.playtime_2weeks_minutes,
                           game_platforms.playtime_2weeks_minutes
                       ),
                       last_played = CASE
                           WHEN game_platforms.manual_overrides IS NOT NULL
                                AND 'last_played' IN (
                                    SELECT value FROM json_each(game_platforms.manual_overrides))
                           THEN game_platforms.last_played
                           ELSE COALESCE(excluded.last_played, game_platforms.last_played)
                       END,
                       last_synced = excluded.last_synced""",
                (STEAM_PLATFORM, synced_at, synced_at),
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
                        playtime_2weeks_minutes, last_synced, last_seen_in_source)
                       VALUES (?, ?, 1, ?, ?, ?, ?)""",
                    (new_game_id, STEAM_PLATFORM, new["playtime_minutes"],
                     new["playtime_2weeks_minutes"], synced_at, synced_at),
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


@retry_on_write_contention
async def set_steam_delisted(appids, delisted: bool) -> int:
    """Set game_platforms.delisted for the Steam rows holding these appids.

    delisted=1 marks ownership sourced from the account license audit for an
    app the public owned-games API no longer returns AND whose store page is
    gone (typically retired); delisted=0 clears the flag when the app reappears
    there — delistings get reversed (GTA IV Complete Edition superseded the
    retired standalone). Only flips rows whose flag differs, and returns that
    count. A row whose ``delisted`` is pinned in manual_overrides (see
    add_game_to_platform) is never touched, like every other protected column.
    """
    ids = [str(a) for a in appids]
    if not ids:
        return 0
    flag = int(bool(delisted))
    changed = 0
    async with get_db() as db:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            cursor = await db.execute(
                f"""UPDATE game_platforms SET delisted = ?
                    WHERE platform = ? AND delisted != ?
                      AND NOT (manual_overrides IS NOT NULL
                               AND 'delisted' IN (
                                   SELECT value FROM json_each(manual_overrides)))
                      AND id IN (SELECT game_platform_id
                                 FROM game_platform_identifiers
                                 WHERE identifier_type = ?
                                   AND identifier_value IN ({placeholders}))""",
                (flag, STEAM_PLATFORM, flag, STEAM_APP_ID, *chunk),
            )
            changed += cursor.rowcount
        await db.commit()
    return changed


@retry_on_write_contention
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


async def delete_nintendo_playtime_baseline(application_id: str) -> bool:
    """Remove the manual pre-tracking baseline row for one application_id.

    Returns True when a row was deleted. Only sentinel-device rows are
    touched; real Parental Controls daily summaries are never deleted.
    application_id is normalized (uppercase) the same way it's stored — see
    normalize_identifier_value — so this is a plain equality match.
    """
    from .queries import NINTENDO_BASELINE_DEVICE_ID, NINTENDO_BASELINE_PERIOD_KEY

    application_id = normalize_identifier_value(NINTENDO_TITLE_ID_TYPE, application_id)
    async with get_db() as db:
        cursor = await db.execute(
            """DELETE FROM nintendo_play_summary
               WHERE application_id = ? AND period_type = 'day'
                 AND device_id = ? AND period_key = ?""",
            (application_id, NINTENDO_BASELINE_DEVICE_ID, NINTENDO_BASELINE_PERIOD_KEY),
        )
        await db.commit()
    return cursor.rowcount > 0


@retry_on_write_contention
async def upsert_nintendo_play_summary(rows: list[dict]) -> int:
    """Idempotently upsert Parental Controls play-summary rows.

    Each row dict carries: device_id, application_id, period_type, period_key,
    playtime_minutes, and (optional) app_name. The natural primary key
    (device_id, application_id, period_type, period_key) makes re-syncing a day
    a no-op overwrite rather than a double-count. Returns the row count written.
    application_id is normalized (uppercased) here — the other write
    chokepoint alongside upsert_game_platform_identifier — so it always lines
    up with the nintendo_title_id identifier bridging it to a game; see
    normalize_identifier_value.
    """
    if not rows:
        return 0
    now = datetime.now(UTC).isoformat()
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
                    normalize_identifier_value(NINTENDO_TITLE_ID_TYPE, str(r["application_id"])),
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


@retry_on_write_contention
async def upsert_game_prices(rows: list[dict]) -> int:
    """Upsert current-price rows into game_prices, overwriting stale prices.

    Each row: {game_id, platform, shop, price, regular_price, cut_pct,
    currency, deal_url} plus the optional ITAD extras {history_low,
    history_low_currency, deal_ends_at}, which a provider without a
    history-of-record (DekuDeals) simply omits and which are then written
    NULL — a refresh always states the whole current answer, so a missing
    key means "this provider has none", never "keep the old value". This is a current-price cache, not a history table —
    fetched_at is stamped here (UTC now) on every call, even when the price
    is unchanged, so a later staleness check can trust it: a failed/partial
    fetch must never delete or blank a previously cached price, but a
    successful fetch that reconfirms the same price should still count as
    "just refreshed". Returns the number of rows written.

    Every caller in this system writes the *complete* current shop set for
    each (game_id, platform) key it's refreshing in one batch (ITAD's
    ``_best_deal`` returns exactly one winning shop per Steam appid;
    DekuDeals always writes shop="dekudeals" for switch2), so after the
    upsert, any pre-existing row for a key touched by this batch whose shop
    isn't among the shops just written is a stale loser from a previous
    refresh (e.g. last week's cheaper GOG price after Steam becomes the new
    cheapest) and is pruned. Without this, `UNIQUE(game_id, platform, shop)`
    lets old non-winning shop rows accumulate forever and can make a stale
    price look permanently "cheapest".
    """
    if not rows:
        return 0
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.executemany(
            """INSERT INTO game_prices
               (game_id, platform, shop, price, regular_price, cut_pct,
                currency, deal_url, history_low, history_low_currency,
                deal_ends_at, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform, shop) DO UPDATE SET
                   price = excluded.price,
                   regular_price = excluded.regular_price,
                   cut_pct = excluded.cut_pct,
                   currency = excluded.currency,
                   deal_url = excluded.deal_url,
                   history_low = excluded.history_low,
                   history_low_currency = excluded.history_low_currency,
                   deal_ends_at = excluded.deal_ends_at,
                   fetched_at = excluded.fetched_at""",
            [
                (
                    r["game_id"],
                    r["platform"],
                    r["shop"],
                    r.get("price"),
                    r.get("regular_price"),
                    r.get("cut_pct"),
                    r.get("currency"),
                    r.get("deal_url"),
                    r.get("history_low"),
                    r.get("history_low_currency"),
                    r.get("deal_ends_at"),
                    now,
                )
                for r in rows
            ],
        )
        shops_by_key: dict[tuple[int, str], set[str]] = {}
        for r in rows:
            shops_by_key.setdefault((r["game_id"], r["platform"]), set()).add(r["shop"])
        for (game_id, platform), shops in shops_by_key.items():
            placeholders = ",".join("?" for _ in shops)
            await db.execute(
                f"""DELETE FROM game_prices
                    WHERE game_id = ? AND platform = ?
                      AND shop NOT IN ({placeholders})""",
                (game_id, platform, *shops),
            )
        await db.commit()
    return len(rows)
