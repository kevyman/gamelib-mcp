"""Fuzzy name matching against the games table."""

import aiosqlite

from ..title_normalization import normalize_catalog_title, normalize_search_text
from . import (
    extract_best_fuzzy_key,
    get_db,
)

# Roman numerals used as sequel markers, normalized to their Arabic value so
# "Final Fantasy VII" and "Final Fantasy 7" resolve to the same identity. Single
# "I" is intentionally omitted — it is too ambiguous to treat as a sequel marker.
_ROMAN_TO_ARABIC = {
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}


def _sequel_identity_tokens(value: str) -> set[str]:
    """Return the numbers that distinguish entries within a series.

    Edition/platform decorations (e.g. "Definitive Edition", "Nintendo Switch 2
    Edition") are stripped first via ``normalize_catalog_title`` so a marketing
    number — like the "2" in "Switch 2 Edition" — never reads as a sequel number.
    Only pure-digit and Roman-numeral tokens count as identity (Roman normalized
    to Arabic); platform tags such as "PS5" are deliberately ignored.
    """
    tokens = normalize_search_text(normalize_catalog_title(value)).split()
    identity: set[str] = set()
    for token in tokens:
        if token in _ROMAN_TO_ARABIC:
            identity.add(_ROMAN_TO_ARABIC[token])
        elif token.isdigit():
            identity.add(token)
    return identity


def _has_conflicting_sequel_identity(query: str, candidate: str) -> bool:
    return _sequel_identity_tokens(query) != _sequel_identity_tokens(candidate)


def titles_conflict_on_identity(a: str, b: str) -> bool:
    """True when two titles disagree on series-distinguishing numbers.

    Public guard for fuzzy resolvers: a confident name match should be rejected
    when, for example, a base title would collapse onto a numbered sequel
    ("Xenoblade Chronicles" vs "Xenoblade Chronicles 2").
    """
    return _has_conflicting_sequel_identity(a, b)


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
