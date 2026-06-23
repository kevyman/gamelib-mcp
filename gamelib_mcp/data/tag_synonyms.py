"""Canonical tag normalization shared across tag writers and query inputs.

Steam (SteamSpy community tags), IGDB (themes/keywords), and user filter input all
describe the same concepts with slightly different surface forms — ``souls-like`` vs
``soulslike`` vs ``souls like``, ``co-op`` vs ``coop``. Without a shared canonical
form these become distinct ``tag_affinity`` keys and tag filters miss games.

``canonical_tag`` is applied at write time (so ``games.tags`` stores canonical
forms) and to query inputs (so filters resolve to the same keys). It is idempotent:
``canonical_tag(canonical_tag(x)) == canonical_tag(x)``.

On a miss it returns the plain lowercased tag — matching SQLite ``lower(value)`` used
by the discover/library tag-match joins, so affinity keys and stored tags stay in
sync even for rows not yet rewritten to canonical form. Separator-collapsing applies
only to the synonym *lookup key*, so ``souls-like``/``soulslike``/``souls like`` all
resolve to the same curated surface form.
"""

import re

_SEPARATORS = re.compile(r"[\s_-]+")


def _norm_key(tag: str) -> str:
    """Lowercase, trim, and collapse separators to a single space (the lookup key)."""
    return _SEPARATORS.sub(" ", tag.strip().lower()).strip()


# Maps a normalized lookup key (see ``_norm_key``) to its canonical surface form. Keep
# this small and high-confidence; tags with no entry fall through as plain lowercase.
# Each canonical surface form's own normalized key must resolve back to itself so the
# function stays idempotent (e.g. _norm_key("souls-like") == "souls like", a key here).
_SYNONYMS: dict[str, str] = {
    "souls like": "souls-like",
    "soulslike": "souls-like",
    "soulsborne": "souls-like",
    "souls likes": "souls-like",
    "co op": "co-op",
    "coop": "co-op",
    "online co op": "online co-op",
    "local co op": "local co-op",
    "action rpg": "action rpg",
    "arpg": "action rpg",
    "metroidvania": "metroidvania",
    "metroid vania": "metroidvania",
    "rogue like": "roguelike",
    "roguelike": "roguelike",
    "rogue lite": "roguelite",
    "roguelite": "roguelite",
    "open world": "open world",
    "openworld": "open world",
    "first person": "first-person",
    "firstperson": "first-person",
    "third person": "third-person",
    "thirdperson": "third-person",
    "hack and slash": "hack and slash",
    "hack n slash": "hack and slash",
    "side scroller": "side-scrolling",
    "side scrolling": "side-scrolling",
    "sidescroller": "side-scrolling",
}


def canonical_tag(tag: str) -> str:
    """Return the canonical surface form of a tag.

    Synonyms map to a curated surface form; everything else returns plain lowercase
    (``tag.strip().lower()``) to stay consistent with SQLite ``lower()`` matching.
    """
    low = tag.strip().lower()
    mapped = _SYNONYMS.get(_norm_key(low))
    return mapped if mapped is not None else low
