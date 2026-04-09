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


def normalize_catalog_title(name: str) -> str:
    cleaned = _ascii_fold(name)
    cleaned = cleaned.replace("™", "").replace("®", "")
    cleaned = re.sub(r"\(TM\)|\(R\)|\bTM\b|\bR\b", "", cleaned)
    cleaned = re.sub(r"(?<=[A-Za-z])TM(?=[:\s]|$)", "", cleaned)
    cleaned = re.sub(r"(?<=[A-Za-z])R(?=[:\s]|$)", "", cleaned)
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"\(\s*(\d{4})\s*\)$", "", cleaned)

    previous = None
    while cleaned != previous:
        previous = cleaned
        for pattern in _TRAILING_VARIANT_PATTERNS:
            cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s*-\s*$", "", cleaned)
    return cleaned.strip()


def prepare_catalog_title(name: str | None) -> str | None:
    if not name:
        return None

    normalized = normalize_catalog_title(name)
    if not normalized or is_non_game_title(normalized):
        return None
    return normalized
