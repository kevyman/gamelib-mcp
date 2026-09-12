"""Fuzzy name matching against the games table."""

import aiosqlite

from ..title_normalization import match_key, strip_comparison_edition_suffixes
from . import (
    extract_best_fuzzy_key,
    get_db,
)

# Roman numerals used as sequel markers, normalized to their Arabic value so
# "Final Fantasy VII" and "Final Fantasy 7" resolve to the same identity.
# ``match_key`` already folds these, so this map is a belt-and-braces backstop
# for a token it leaves alone.
#
# "I" is intentionally omitted — too ambiguous to read as a sequel marker — and
# so is "X": "Mega Man X" is a distinct game from "Mega Man 10", and folding
# the letter to 10 made the two read as one identity.
_ROMAN_TO_ARABIC = {
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
}

# Digit-bearing tokens that denote *hardware*, not a series entry. Ignored so
# "God of War PS5" and "God of War PS4" are recognized as the same game while
# genuine version tokens (e.g. "2k24") are still kept.
_PLATFORM_IDENTITY_TAGS = {
    "ps1",
    "ps2",
    "ps3",
    "ps4",
    "ps5",
    "x360",
    "xbox360",
    "n64",
    "3ds",
    "switch2",
}


def _sequel_identity_tokens(value: str) -> set[str]:
    """Return the numbers that distinguish entries within a series.

    Edition/SKU/platform decorations are stripped first — via
    ``strip_comparison_edition_suffixes``, the WIDEST of the edition lists — so
    a marketing number never reads as a sequel number: the "2" in "Switch 2
    Edition", the "2026" in "Sea of Thieves: 2026 Edition", the year in "Dead
    Space (2023)", and the "One" in "Watch Dogs: Day One Edition", which only
    became a number once these tokens started coming from ``match_key``
    (spelled-out numbers fold to digits there, so the narrower catalog strip
    left a phantom "1" that dropped the base game before the name gate saw it).

    Tokens then come from ``match_key``, the one identity normalization, so a
    Roman numeral or a spelled-out number ("Hades II", "Left Four Dead") is
    already Arabic here. A token counts as identity when it contains a digit
    (so embedded version markers like "2k24" are preserved); known platform
    tags such as "PS5" are deliberately ignored.

    Over-stripping is safe in this direction and only in this direction: the
    question asked here is "do these two titles DISAGREE about a series
    number", and a decoration neither side keeps cannot manufacture agreement
    between genuinely different entries — "Portal" and "Portal 2" still carry
    {} and {"2"}.
    """
    tokens = match_key(strip_comparison_edition_suffixes(value)).split()
    identity: set[str] = set()
    for token in tokens:
        if token in _PLATFORM_IDENTITY_TAGS:
            continue
        if token in _ROMAN_TO_ARABIC:
            identity.add(_ROMAN_TO_ARABIC[token])
        elif any(char.isdigit() for char in token):
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


def _release_year(release_date: str | None) -> int | None:
    """Extract a 4-digit year from a stored release_date (e.g. '2008-10-13')."""
    if not release_date:
        return None
    head = release_date.strip()[:4]
    return int(head) if head.isdigit() else None


async def _game_ids_with_platform(platform: str) -> set[int]:
    """Return game ids that already own a game_platforms row for ``platform``.

    Queried live so it reflects rows created earlier in the same sync pass — two
    distinct same-platform store entries with the same name therefore never
    collapse onto each other.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT DISTINCT game_id FROM game_platforms WHERE platform = ?",
            (platform,),
        )
    return {row["game_id"] for row in rows}


async def find_game_by_name_fuzzy(
    name: str,
    cutoff: int = 85,
    candidates: dict[int, str] | None = None,
    *,
    exclude_platform: str | None = None,
    reference_release_date: str | None = None,
) -> aiosqlite.Row | None:
    """Return the best-matching games row for a given title, or None if below cutoff.

    ``exclude_platform`` drops candidates that already own a row for that platform,
    so a distinct same-platform store entry (e.g. a second "Dead Space" on Steam)
    starts a new game rather than collapsing onto an existing one. Cross-platform
    matches are unaffected.

    ``reference_release_date`` drops candidates whose release year disagrees with it
    *before* ranking — the signal that separates same-named remakes (Dead Space 2008
    vs 2023) that share no sequel number. Filtering before ranking (rather than
    rejecting only the single best match) lets an equally-named candidate with the
    matching year still win instead of forking a duplicate.
    """
    if candidates is None:
        candidates = await load_fuzzy_candidates()

    if exclude_platform is not None:
        excluded = await _game_ids_with_platform(exclude_platform)
        if excluded:
            candidates = {gid: n for gid, n in candidates.items() if gid not in excluded}

    if reference_release_date is not None and candidates:
        ref_year = _release_year(reference_release_date)
        if ref_year is not None:
            async with get_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT id FROM games "
                    "WHERE release_date IS NOT NULL AND substr(release_date, 1, 4) <> ?",
                    (f"{ref_year:04d}",),
                )
            conflicting = {row["id"] for row in rows}
            if conflicting:
                candidates = {gid: n for gid, n in candidates.items() if gid not in conflicting}

    best_id = extract_best_fuzzy_key(name, candidates, cutoff=cutoff)
    if best_id is None:
        return None

    if _has_conflicting_sequel_identity(name, candidates[best_id]):
        return None

    async with get_db() as db:
        return await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (best_id,))
