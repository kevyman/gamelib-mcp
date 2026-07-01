"""Content relationship classification for DLC, expansions, and editions."""

from dataclasses import dataclass

from .title_normalization import normalize_search_text


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
    alias_for_parent: bool = False


def _primary(content_type: str) -> ContentClassification:
    return ContentClassification(content_type=content_type, is_primary_library_item=True)


def _nested(
    content_type: str,
    parent_name: str | None = None,
    *,
    parent_igdb_id: int | None = None,
    alias_for_parent: bool = False,
) -> ContentClassification:
    return ContentClassification(
        content_type=content_type,
        parent_name=parent_name,
        parent_igdb_id=parent_igdb_id,
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
        8: CONTENT_REMAKE,
        9: CONTENT_REMASTER,
        10: CONTENT_EXPANDED_GAME,
        11: CONTENT_PORT,
    }.get(category, CONTENT_BASE_GAME)


def classify_igdb_game(
    *,
    title: str,
    category: int | None,
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

    if version_parent_name or version_parent_igdb_id:
        return _nested(
            CONTENT_EDITION,
            version_parent_name,
            parent_igdb_id=version_parent_igdb_id,
            alias_for_parent=True,
        )

    content_type = content_type_from_igdb_category(category)
    if content_type in PRIMARY_CONTENT_TYPES:
        return _primary(content_type)

    return _nested(content_type, parent_name, parent_igdb_id=parent_igdb_id)
