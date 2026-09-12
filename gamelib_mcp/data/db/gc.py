"""Garbage collection for ``games`` rows nothing points at any more.

A wishlist sync that drops an upstream removal (``delete_stale_wishlist_entries``)
deletes the ``game_wishlist`` row, and until now left the ``games`` row behind
with nothing referencing it — the residue ``check_library``'s ``ownership.orphan``
reports. This module is the narrow counterpart: given the ids a caller just
un-referenced, delete only those that are genuinely bare.

Deliberately id-scoped rather than a library-wide sweep. A blanket "delete every
unreferenced game" would be a destructive background job over rows a human may
have minted on purpose; scoping to the ids one operation just orphaned means the
GC can only ever undo what that operation did.
"""

from collections.abc import Iterable

from . import get_db

# Every reference that makes a games row worth keeping. Ownership and wishlist
# are the obvious two; the rest are rows that would FK-cascade away with it —
# a recorded verdict the calibration report reads, a rating feeding affinity,
# playtime history, a nested child that would be orphaned in turn. A non-empty
# manual_overrides means a human edited this row by hand, which is a statement
# in its own right.
_REFERENCE_CLAUSES = (
    "EXISTS (SELECT 1 FROM game_platforms WHERE game_id = g.id)",
    "EXISTS (SELECT 1 FROM game_wishlist WHERE game_id = g.id)",
    "EXISTS (SELECT 1 FROM game_assessments WHERE game_id = g.id)",
    "EXISTS (SELECT 1 FROM game_assessments WHERE instead_game_id = g.id)",
    "EXISTS (SELECT 1 FROM ratings WHERE game_id = g.id)",
    "EXISTS (SELECT 1 FROM play_history WHERE game_id = g.id)",
    "EXISTS (SELECT 1 FROM games child WHERE child.parent_game_id = g.id)",
    "COALESCE(TRIM(g.manual_overrides), '') NOT IN ('', '[]')",
)


async def delete_unreferenced_games(game_ids: Iterable[int]) -> list[int]:
    """Delete the given games rows that nothing references; return the deleted ids.

    Only ids in ``game_ids`` are ever considered, and one is deleted only when
    it has no ``game_platforms`` row (owned or retired), no ``game_wishlist``
    row, no ``game_assessments`` row (as ``game_id`` or ``instead_game_id``),
    no ``ratings``, no ``play_history``, no child nesting under it, and no
    manual overrides. Anything else is left exactly where it is.
    """
    ids = sorted({int(game_id) for game_id in game_ids})
    if not ids:
        return []

    placeholders = ",".join("?" * len(ids))
    keep_sql = " OR ".join(_REFERENCE_CLAUSES)
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"SELECT g.id FROM games g WHERE g.id IN ({placeholders}) AND NOT ({keep_sql})",
            ids,
        )
        deletable = [row["id"] for row in rows]
        if not deletable:
            return []
        delete_placeholders = ",".join("?" * len(deletable))
        await db.execute(
            f"DELETE FROM games WHERE id IN ({delete_placeholders})", deletable
        )
        await db.commit()
    return deletable
