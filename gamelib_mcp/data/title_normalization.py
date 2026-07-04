"""Shared title cleanup for library ingest and IGDB resolution."""

import re
import unicodedata


_NON_GAME_PATTERNS = (
    re.compile(r"\b(soundtrack|wallpaper|art book|artbook)\b$", re.IGNORECASE),
    re.compile(
        r"\b(test server|public test(?:ing)?|public beta(?: client)?|playtest|staging branch|experimental branch|test branch)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(friend'?s pass|pre-?game editor|resource archiver)\b", re.IGNORECASE),
    re.compile(
        r"\b(bonus content|digital content|content pack|goodies collection|scenario pack|unit pack|editor|vfx)\b$",
        re.IGNORECASE,
    ),
    re.compile(r"\b(dlc|expansion pack)(?:\s+no\.?\s*\d+)?\b$", re.IGNORECASE),
    re.compile(r"\bcontent\b$", re.IGNORECASE),
    re.compile(r"\bbeta(?:\s+demo)?\b\W*$", re.IGNORECASE),
)
_TRAILING_VARIANT_PATTERNS = (
    re.compile(r"\s*\((?:PlayStation ?5|PS5)\)\s*$", re.IGNORECASE),
    re.compile(r"\s*-\s*Nintendo Switch 2 Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+Nintendo Switch 2 Edition\s*$", re.IGNORECASE),
    # Bare "Switch 2 Edition" (no "Nintendo") — strip so its "2" is not mistaken
    # for a sequel number during identity matching.
    re.compile(r"\s*-\s*Switch 2 Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+Switch 2 Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+for Nintendo Switch\s*$", re.IGNORECASE),
    re.compile(r"\s+GOTY Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+Game of the Year Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+Definitive Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+Anniversary Edition\s*$", re.IGNORECASE),
    re.compile(r"\s+Final Cut\s*$", re.IGNORECASE),
    re.compile(r"\s+Director'?s Cut\s*$", re.IGNORECASE),
    re.compile(r"\s+-\s+Remastered\s*$", re.IGNORECASE),
    re.compile(r"\s+Remastered\s*$", re.IGNORECASE),
    re.compile(r"\s+Enhanced\s*$", re.IGNORECASE),
    re.compile(r"\s+\(Classic\)\s*$", re.IGNORECASE),
    re.compile(r"\s+Steam Edition\s*$", re.IGNORECASE),
)


def _ascii_fold(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)
    )


def is_non_game_title(name: str) -> bool:
    folded = _ascii_fold(name)
    if any(pattern.search(folded) for pattern in _NON_GAME_PATTERNS):
        return True

    words = re.findall(r"[a-z0-9]+", folded.casefold())
    if words and words[-1] == "demo" and len(words) <= 3:
        return True

    return False


def normalize_search_text(value: str) -> str:
    """Normalize a title or query for search matching.

    Strips trademark glyphs, ascii-folds, casefolds, and collapses every
    non-alphanumeric run into a single space, so "Sekiro™: Shadows Die Twice"
    and "sekiro shadows die twice" normalize identically. Word order is
    preserved (unlike the token-sorting fuzzy preprocessor) so
    prefix/substring ranking stays meaningful.
    """
    # NFKD would otherwise expand ™ to a literal "TM" glued onto the word.
    cleaned = value.replace("™", " ").replace("®", " ")
    folded = _ascii_fold(cleaned).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def normalize_catalog_title(name: str) -> str:
    cleaned = _ascii_fold(name)
    cleaned = cleaned.replace("™", "").replace("®", "")
    cleaned = re.sub(r"\(TM\)|\(R\)|\bTM\b|\bR\b", "", cleaned)
    cleaned = re.sub(r"(?<=[A-Za-z])TM(?=[:\s]|$)", "", cleaned)
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"\(\s*(\d{4})\s*\)$", "", cleaned)

    previous = None
    while cleaned != previous:
        previous = cleaned
        for pattern in _TRAILING_VARIANT_PATTERNS:
            cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned)
    # Strip any separator left dangling after a trailing variant/subtitle was
    # removed, so "Deus Ex: Game of the Year Edition" -> "Deus Ex" (not
    # "Deus Ex:") and "...Templars: Director's Cut" -> "...Templars".
    cleaned = re.sub(r"[\s:-]+$", "", cleaned)
    return cleaned.strip()


def prepare_catalog_title(name: str | None) -> str | None:
    if not name:
        return None

    normalized = normalize_catalog_title(name)
    if not normalized or is_non_game_title(normalized):
        return None
    return normalized


# Trailing edition-marker phrases stripped for series-gap have/gap exclusion
# matching only (normalize_series_gap_title below) — deliberately NOT folded
# into _TRAILING_VARIANT_PATTERNS above, which also drive within-platform
# game identity matching where a Remastered/Definitive/etc. release can
# legitimately be its own catalog row (see get_series_breakdown's Fallout
# fixture: "Fallout 4 Remastered" is a distinct primary library item). Here
# the goal is narrower: "does this look like the same underlying game as an
# IGDB series member" so an owned edition doesn't false-positive as a gap.
_SERIES_GAP_EDITION_PATTERNS = (
    re.compile(r"\s+enhanced edition\s*$", re.IGNORECASE),
    re.compile(r"\s+game of the year(?: edition)?\s*$", re.IGNORECASE),
    re.compile(r"\s+goty\s*$", re.IGNORECASE),
    re.compile(r"\s+definitive edition\s*$", re.IGNORECASE),
    re.compile(r"\s+remastered\s*$", re.IGNORECASE),
    re.compile(r"\s+complete edition\s*$", re.IGNORECASE),
    re.compile(r"\s+deluxe edition\s*$", re.IGNORECASE),
    re.compile(r"\s+ultimate edition\s*$", re.IGNORECASE),
    re.compile(r"\s+gold edition\s*$", re.IGNORECASE),
    re.compile(r"\s+collector'?s edition\s*$", re.IGNORECASE),
    re.compile(r"\s+director'?s cut\s*$", re.IGNORECASE),
    re.compile(r"\s+legendary edition\s*$", re.IGNORECASE),
    re.compile(r"\s+anniversary edition\s*$", re.IGNORECASE),
    # Bare "edition" leftover, once a more specific pattern above has already
    # peeled off its qualifier (or for a qualifier not otherwise listed).
    re.compile(r"\s+edition\s*$", re.IGNORECASE),
)


def normalize_series_gap_title(name: str) -> str:
    """Normalize a title for discover_series_gaps have/gap exclusion matching.

    Loops off a trailing edition marker (so "Game of the Year Enhanced
    Edition" fully collapses), then hands off to normalize_search_text for
    case-folding and punctuation-insensitive comparison — which, since it
    extracts only [a-z0-9]+ runs, already treats any apostrophe variant
    (straight or curly) as a separator, so "Marvel's" and "Marvel’s"
    normalize identically with no separate unicode-apostrophe step needed.
    """
    cleaned = name
    previous = None
    while cleaned != previous:
        previous = cleaned
        for pattern in _SERIES_GAP_EDITION_PATTERNS:
            cleaned = pattern.sub("", cleaned)
    return normalize_search_text(cleaned)
