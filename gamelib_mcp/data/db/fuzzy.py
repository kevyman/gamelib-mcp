"""Fuzzy name matching against the games table."""

import aiosqlite

from ..title_normalization import normalize_search_text
from . import (
    extract_best_fuzzy_key,
    get_db,
)

_ROMAN_NUMERAL_SEQUEL_TOKENS = {
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
}


def _sequel_identity_tokens(value: str) -> set[str]:
    tokens = normalize_search_text(value).split()
    return {
        token
        for token in tokens
        if any(char.isdigit() for char in token) or token in _ROMAN_NUMERAL_SEQUEL_TOKENS
    }


def _has_conflicting_sequel_identity(query: str, candidate: str) -> bool:
    return _sequel_identity_tokens(query) != _sequel_identity_tokens(candidate)


def find_conflicting_fuzzy_key(
    name: str,
    candidates: dict[int, str],
    cutoff: int = 85,
) -> int | None:
    """Return a rejected fuzzy candidate when title identity tokens conflict."""
    best_id = extract_best_fuzzy_key(name, candidates, cutoff=cutoff)
    if best_id is None:
        return None
    if not _has_conflicting_sequel_identity(name, candidates[best_id]):
        return None
    return best_id


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

    if _has_conflicting_sequel_identity(name, candidates[best_id]):
        return None

    async with get_db() as db:
        return await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (best_id,))
