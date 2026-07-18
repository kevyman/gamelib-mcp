"""Content relationship classification for DLC, expansions, and editions."""

import re
from dataclasses import dataclass

from .title_normalization import normalize_search_text


# "A + B" compilation titles (e.g. Nintendo's "Super Mario 3D World + Bowser's
# Fury") ship as a single owned SKU with their own store identifier and
# playtime — they are primary library items, not IGDB bundles. IGDB tags such
# titles category 3 (bundle) or hangs a version_parent on them, either of which
# would demote them out of the library rollups (get_library_stats, series,
# discover). Requires non-space text on both sides of a spaced "+" so we don't
# match a trailing "+" or a lone operator.
_COMPILATION_TITLE_RE = re.compile(r"\S\s+\+\s+\S")


def is_compilation_title(title: str) -> bool:
    """True for "A + B" compilation titles that are primary library items."""
    return bool(_COMPILATION_TITLE_RE.search(title or ""))


CONTENT_BASE_GAME = "base_game"
CONTENT_DLC = "dlc"
CONTENT_EXPANSION = "expansion"
CONTENT_BUNDLE = "bundle"
CONTENT_EDITION = "edition"
CONTENT_STANDALONE_EXPANSION = "standalone_expansion"
CONTENT_REMAKE = "remake"
CONTENT_REMASTER = "remaster"
CONTENT_EXPANDED_GAME = "expanded_game"
CONTENT_PORT = "port"
CONTENT_UNKNOWN_ADDON = "unknown_addon"

PRIMARY_CONTENT_TYPES = {
    CONTENT_BASE_GAME,
    CONTENT_STANDALONE_EXPANSION,
    CONTENT_REMAKE,
    CONTENT_REMASTER,
    CONTENT_EXPANDED_GAME,
    CONTENT_PORT,
}

NESTED_CONTENT_TYPES = {
    CONTENT_DLC,
    CONTENT_EXPANSION,
    CONTENT_BUNDLE,
    CONTENT_EDITION,
    CONTENT_UNKNOWN_ADDON,
}


@dataclass(frozen=True)
class ContentClassification:
    content_type: str
    is_primary_library_item: bool
    parent_name: str | None = None
    parent_igdb_id: int | None = None
    # Steam's own parent pointer (fullgame appid on a DLC's store record). IGDB
    # has no equivalent here, so this stays None on the IGDB path.
    parent_steam_appid: int | None = None
    alias_for_parent: bool = False


def _primary(content_type: str) -> ContentClassification:
    return ContentClassification(content_type=content_type, is_primary_library_item=True)


def _nested(
    content_type: str,
    parent_name: str | None = None,
    *,
    parent_igdb_id: int | None = None,
    parent_steam_appid: int | None = None,
    alias_for_parent: bool = False,
) -> ContentClassification:
    return ContentClassification(
        content_type=content_type,
        parent_name=parent_name,
        parent_igdb_id=parent_igdb_id,
        parent_steam_appid=parent_steam_appid,
        is_primary_library_item=False,
        alias_for_parent=alias_for_parent,
    )


_TITLE_OVERRIDES = {
    normalize_search_text("Sid Meier's Civilization IV: Warlords"): _nested(
        CONTENT_EXPANSION, "Sid Meier's Civilization IV"
    ),
    normalize_search_text("Sid Meier's Civilization IV: Beyond the Sword"): _nested(
        CONTENT_EXPANSION, "Sid Meier's Civilization IV"
    ),
    normalize_search_text("Sid Meier's Civilization IV: Colonization"): _primary(
        CONTENT_STANDALONE_EXPANSION
    ),
    normalize_search_text("Sid Meiers Civilization IV The Complete Edition"): _nested(
        CONTENT_EDITION, "Sid Meier's Civilization IV", alias_for_parent=True
    ),
    normalize_search_text("Sid Meier's Civilization III: Complete"): _nested(
        CONTENT_EDITION, "Sid Meier's Civilization III", alias_for_parent=True
    ),
    normalize_search_text("Fallout New Vegas Ultimate Edition"): _nested(
        CONTENT_EDITION, "Fallout: New Vegas", alias_for_parent=True
    ),
}

for _dlc in (
    "Fallout New Vegas: Courier's Stash",
    "Fallout New Vegas: Dead Money",
    "Fallout New Vegas: Gun Runners' Arsenal",
    "Fallout New Vegas: Gun Runners’ Arsenal",
    "Fallout New Vegas: Honest Hearts",
    "Fallout New Vegas: Lonesome Road",
    "Fallout New Vegas: Old World Blues",
):
    _TITLE_OVERRIDES[normalize_search_text(_dlc)] = _nested(CONTENT_DLC, "Fallout: New Vegas")

# Well-known DLC whose names carry no addon-ish word for the pattern table.
_TITLE_OVERRIDES[normalize_search_text("Outlast: Whistleblower")] = _nested(
    CONTENT_DLC, "Outlast"
)


def classify_title_override(title: str) -> ContentClassification | None:
    return _TITLE_OVERRIDES.get(normalize_search_text(title))


def content_type_from_igdb_category(category: int | None) -> str:
    if category is None:
        return CONTENT_BASE_GAME
    return {
        1: CONTENT_DLC,
        2: CONTENT_EXPANSION,
        3: CONTENT_BUNDLE,
        4: CONTENT_STANDALONE_EXPANSION,
        # 5 ("mod") is independently playable — hiding an owned mod would lose a
        # real library item, so it stays a primary base game.
        5: CONTENT_BASE_GAME,
        # 6 ("episode") is a sub-purchase of a parent; IGDB carries a parent_game
        # for it, so it nests like DLC.
        6: CONTENT_DLC,
        # 7 ("season") is the sellable, playable unit (Telltale-style), so it is a
        # primary base game, not nested content.
        7: CONTENT_BASE_GAME,
        8: CONTENT_REMAKE,
        9: CONTENT_REMASTER,
        10: CONTENT_EXPANDED_GAME,
        11: CONTENT_PORT,
        # 13 ("pack") is IGDB's bucket for cosmetic/BGM/persona-set style addon
        # content (e.g. the Persona 3 Reload "Persona Set"/"BGM Set" packs) — it
        # is nested addon content, not a primary library item, so it maps to
        # CONTENT_DLC rather than falling through to the base-game default.
        13: CONTENT_DLC,
    }.get(category, CONTENT_BASE_GAME)


def classify_igdb_game(
    *,
    title: str,
    category: int | None,
    game_type: int | None = None,
    parent_name: str | None = None,
    parent_igdb_id: int | None = None,
    version_parent_name: str | None = None,
    version_parent_igdb_id: int | None = None,
) -> ContentClassification:
    override = classify_title_override(title)
    if override is not None:
        return ContentClassification(
            content_type=override.content_type,
            is_primary_library_item=override.is_primary_library_item,
            parent_name=override.parent_name or parent_name,
            parent_igdb_id=override.parent_igdb_id or parent_igdb_id,
            alias_for_parent=override.alias_for_parent,
        )

    # A compilation SKU is a primary library item regardless of the version_parent
    # IGDB hangs on it (which would otherwise nest it as an edition) or a
    # category=bundle tag below.
    compilation = is_compilation_title(title)

    if version_parent_name or version_parent_igdb_id:
        if compilation:
            return _primary(CONTENT_BASE_GAME)
        return _nested(
            CONTENT_EDITION,
            version_parent_name,
            parent_igdb_id=version_parent_igdb_id,
            alias_for_parent=True,
        )

    # IGDB has effectively migrated `category` -> `game_type` (same numeric
    # enum values); some titles come back with category=None but game_type
    # populated. Fall back to game_type only when category itself is absent —
    # category, when present, is authoritative.
    effective = category if category is not None else game_type
    content_type = content_type_from_igdb_category(effective)
    if content_type == CONTENT_BUNDLE and compilation:
        return _primary(CONTENT_BASE_GAME)
    if content_type in PRIMARY_CONTENT_TYPES:
        return _primary(content_type)

    return _nested(content_type, parent_name, parent_igdb_id=parent_igdb_id)


def classify_steam_app_type(
    app_type: str | None,
    *,
    title: str | None = None,
    fullgame_name: str | None = None,
    fullgame_appid: int | None = None,
) -> ContentClassification | None:
    """Classify a Steam store ``type`` string, or None for "no signal".

    A title override wins first (consistency with classify_igdb_game). Steam's
    own type is authoritative for its own store, so the "A + B" compilation
    escape — which exists only to undo IGDB's bundle mislabeling — is NOT
    applied here. Unknown/unmapped types ("video", "hardware", "mod", "series",
    None, …) return None so the caller writes nothing rather than forcing a
    row to base_game off a type we don't understand.
    """
    if title is not None:
        override = classify_title_override(title)
        if override is not None:
            return override

    normalized = (app_type or "").strip().lower()
    if normalized == "game":
        return _primary(CONTENT_BASE_GAME)
    if normalized == "dlc":
        return _nested(CONTENT_DLC, fullgame_name, parent_steam_appid=fullgame_appid)
    if normalized in ("music", "demo"):
        # Soundtracks/demos are noise in game counts, not primary items.
        return _nested(CONTENT_UNKNOWN_ADDON, fullgame_name)
    return None


def derive_is_primary(content_type: str) -> bool:
    """Whether a content_type is a primary (library-visible) item."""
    return content_type in PRIMARY_CONTENT_TYPES


def split_addon_title(title: str) -> list[str]:
    """Ordered candidate parent names from separator splitting.

    For each separator (colon first, then the three dash variants) return the
    left side of every split point, longest-first when a separator occurs more
    than once (e.g. "A: B: C" -> ["A: B", "A"]). Deduplicated; empties,
    whitespace-only candidates, and the whole title are excluded; [] when no
    separator is present. Deliberately dumb and predictable — it feeds an
    exact-match parent lookup only.
    """
    candidates: list[str] = []
    for sep in (": ", " - ", " – ", " — "):
        starts: list[int] = []
        idx = title.find(sep)
        while idx != -1:
            starts.append(idx)
            idx = title.find(sep, idx + 1)
        # Later occurrences yield longer left sides, so walk them back-to-front.
        for start in reversed(starts):
            candidates.append(title[:start])

    result: list[str] = []
    for candidate in candidates:
        if not candidate.strip() or candidate == title:
            continue
        if candidate not in result:
            result.append(candidate)
    return result


# Trailing addon-kind phrases whose removal yields the base game's own name
# ("Deus Ex: Mankind Divided Season Pass" → "Deus Ex: Mankind Divided").
# Separator splitting alone can't reach that candidate — the suffix isn't
# separator-delimited — and its shortest split ("Deus Ex") resolves to the
# wrong (earliest) franchise entry. Iterated so stacked suffixes peel off.
_ADDON_SUFFIX_RE = re.compile(
    r"[\s:–—-]+(?:season pass|expansion pass|original soundtrack|soundtrack"
    r"|OST|artbook|art book|digital artbook|upgrade pack|costume pack"
    r"|character pack|map pack|DLC(?:\s+(?:pack|bundle))?|add-?on)\s*$",
    re.IGNORECASE,
)


def parent_name_candidates(title: str) -> list[str]:
    """Ordered candidate parent names for a nested item, longest first.

    Combines two derivations and orders ALL candidates by descending length so
    the most specific name wins ("Deus Ex: Mankind Divided" before "Deus Ex"):

    * addon-suffix stripping — the title minus a trailing addon-kind phrase
      (Season Pass, Soundtrack, DLC, …), iterated until stable;
    * separator splitting (split_addon_title) of both the raw and the
      suffix-stripped title.

    Longest-first matters: candidates feed an exact-match lookup where the
    FIRST hit wins, and a franchise's earliest entry is usually the shortest
    name — first-match order was exactly how "Saints Row: The Third Season
    Pass" ended up parented under "Saints Row". Deduplicated; excludes empties
    and the raw title itself.
    """
    forms = [title]
    stripped = title
    previous = None
    while stripped != previous:
        previous = stripped
        stripped = _ADDON_SUFFIX_RE.sub("", stripped).strip()
    if stripped and stripped != title:
        forms.append(stripped)

    candidates: list[str] = []
    for form in forms:
        if form != title and form not in candidates:
            candidates.append(form)
        for candidate in split_addon_title(form):
            if candidate not in candidates:
                candidates.append(candidate)
    return sorted(candidates, key=len, reverse=True)


# Name patterns that betray addon content misfiled as a primary base_game.
# Case-insensitive; "dlc" matches only as a whole word so "Half-Life" and
# friends never trip it. soundtrack/artbook are noise in game counts with no
# real parent gameplay, so they map to unknown_addon; the rest map to dlc.
# Shared by detect_misclassified_dlc (detection buckets) and the Humble
# purchase importer (a content_type hint for stores with no item typing).
ADDON_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"season pass", re.IGNORECASE), CONTENT_DLC, "season pass"),
    (re.compile(r"expansion pass", re.IGNORECASE), CONTENT_DLC, "expansion pass"),
    (re.compile(r"soundtrack", re.IGNORECASE), CONTENT_UNKNOWN_ADDON, "soundtrack"),
    (re.compile(r"upgrade pack", re.IGNORECASE), CONTENT_DLC, "upgrade pack"),
    (re.compile(r"\bdlc\b", re.IGNORECASE), CONTENT_DLC, "dlc"),
    (re.compile(r"bonus content", re.IGNORECASE), CONTENT_DLC, "bonus content"),
    (re.compile(r"character pass", re.IGNORECASE), CONTENT_DLC, "character pass"),
    (re.compile(r"cosmetic", re.IGNORECASE), CONTENT_DLC, "cosmetic"),
    (re.compile(r"costume pack", re.IGNORECASE), CONTENT_DLC, "costume pack"),
    # Paradox-style micro-DLC naming ("Crusader Kings II - Norse Unit Pack").
    (
        re.compile(r"\b(?:unit|portrait|music|sprite)\s+pack\b", re.IGNORECASE),
        CONTENT_DLC,
        "content pack",
    ),
    (re.compile(r"art\s?book", re.IGNORECASE), CONTENT_UNKNOWN_ADDON, "artbook"),
)


def match_addon_name(name: str | None) -> tuple[str, str] | None:
    """First addon-ish name pattern hit → (suggested content_type, label)."""
    for pattern, content_type, label in ADDON_NAME_PATTERNS:
        if pattern.search(name or ""):
            return content_type, label
    return None
