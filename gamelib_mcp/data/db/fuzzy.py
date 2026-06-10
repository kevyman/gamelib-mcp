"""Fuzzy name matching against the games table."""

import aiosqlite

from . import (
    extract_best_fuzzy_key,
    get_db,
)


async def load_fuzzy_candidates() -> dict[int, str]:
    """Load all game id->name pairs for use with find_game_by_name_fuzzy."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id, name FROM games")
    return {row["id"]: row["name"] for row in rows}


async def find_game_by_name_fuzzy(
    name: str,
    cutoff: int = 85,
    candidates: dict[int, str] | None = None,
) -> aiosqlite.Row | None:
    """Return the best-matching games row for a given title, or None if below cutoff."""
    if candidates is None:
        candidates = await load_fuzzy_candidates()

    best_id = extract_best_fuzzy_key(name, candidates, cutoff=cutoff)
    if best_id is None:
        return None

    async with get_db() as db:
        return await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (best_id,))
