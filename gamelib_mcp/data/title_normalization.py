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
    # Trial builds listed as their own catalog entries ("Infinity Wealth
    # Special Trial Version") — a demo by another name; never a real gap or a
    # library row. Anchored on the pairing so the Trials series ("Trials
    # Fusion") can never trip it.
    re.compile(r"\btrial (?:version|edition)\b\W*$", re.IGNORECASE),
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
    return bool(words and words[-1] == "demo" and len(words) <= 3)


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


# A Switch-2 paid upgrade purchase arrives as "…Edition-upgradepack" /
# "…- upgrade pack" — a suffix no library row carries. Peeled before
# normalize_catalog_title so the "Nintendo Switch 2 Edition" pattern beneath it
# can also apply ("Hollow Knight – Nintendo Switch 2 Edition-upgradepack" →
# "Hollow Knight").
_UPGRADE_PACK_RE = re.compile(r"[\s:–—-]*upgrade\s*pack\s*$", re.IGNORECASE)

# Purchase-history SKU decorations seen in the wild that no library row (or
# store catalog page) ever carries: region markers ("Fallout New Vegas Ultimate
# ROW", "Sekiro: Shadows Die Twice (Rest of World)"), package-kind markers
# ("Nidhogg Store", "Teleglitch: Base Game"), and bare edition words without
# the "Edition" tail ("Oblivion Game of the Year Deluxe", "Saints Row IV Game
# of the Century Edition"). Applied ONLY by normalize_purchase_title — these
# are far too aggressive for identity matching (a real row can be named
# "…Complete"), but purchase matching tries the raw title first, so stripping
# only ever widens the net after the exact form has already missed.
_PURCHASE_SKU_PATTERNS = (
    # Parenthesized region/SKU markers anywhere at the tail.
    re.compile(
        r"\s*\((?:ROW|NA|EU|WW|RU(?:/CIS)?|LATAM|Rest of (?:the )?World"
        r"|North America|Worldwide|Global|Asia|Latin America)\)\s*$",
        re.IGNORECASE,
    ),
    # Bare region tails ("… Ultimate ROW", "… Rest of World").
    re.compile(r"\s+(?:ROW|Rest of (?:the )?World|Worldwide|Global)\s*$"),
    # Store-state markers on old bundle keys ("GRAV (Early Access)",
    # "Streamline Early Access") — with or without the parentheses.
    re.compile(r"\s*\(?Early Access\)?\s*$", re.IGNORECASE),
    # Package-kind tails from the licenses/history pages.
    re.compile(
        r"[\s:–—-]+(?:Base Game|Store|Steam Store and Retail Key|Retail(?: Key)?|Standard)\s*$",
        re.IGNORECASE,
    ),
    # Capcom store titles carry the Japanese-market alternate name as a
    # "GAME / BIOHAZARD …" tail ("RESIDENT EVIL 2 / BIOHAZARD RE:2", "RESIDENT
    # EVIL VILLAGE / BIOHAZARD VILLAGE") that no library row uses. Strip from the
    # "/ BIOHAZARD" onward, so any edition tail after it ("… Standard Edition")
    # goes with it.
    re.compile(r"\s*/\s*BIOHAZARD\b.*$", re.IGNORECASE),
    # Edition phrases, with or without the "Edition"/"Deluxe" tail that
    # _TRAILING_VARIANT_PATTERNS alone would leave behind. "Complete" is
    # deliberately absent: unlike Ultimate/GOTY/Deluxe (same game, richer SKU),
    # "X Complete" routinely names a multi-game compilation ("Hexcells
    # Complete" = three games) — stripping it would exact-match the base game
    # and book the whole compilation's price onto it.
    re.compile(
        r"\s+(?:Game of the (?:Year|Century)|GOTY|Ultimate|Ultra|Deluxe"
        r"|Digital Deluxe|Premium|Master|Gold|Platinum|Definitive|Standard)"
        r"(?:\s+(?:Edition|Deluxe))?\s*$",
        re.IGNORECASE,
    ),
)


def normalize_purchase_title(name: str) -> str:
    """Strip storefront SKU/edition/platform/upgrade-pack suffixes for match retry.

    A purchase title ("DAVE THE DIVER Nintendo Switch™ 2 Edition", "Fallout New
    Vegas Ultimate ROW", "Nidhogg Store") carries marketing/SKU suffixes the
    canonical library row never does; token-AND name matching needs every query
    token present in the candidate, so those extra tokens sink the match until
    peeled off. Reuses normalize_catalog_title for the shared edition/™/en-dash
    handling and iterates the purchase-only SKU patterns above until stable.
    Used solely as a fallback query — never to alter game identity or what gets
    written — so over-stripping only ever widens the net after an exact match
    has already failed.
    """
    cleaned = _UPGRADE_PACK_RE.sub("", name)
    previous = None
    while cleaned != previous:
        previous = cleaned
        for pattern in _PURCHASE_SKU_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        cleaned = normalize_catalog_title(cleaned)
    return cleaned


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
    # Storefront dual-listing marker for a superseded SKU kept alongside its
    # re-release ("Yakuza Kiwami (Legacy)") — the owned legacy row is the same
    # underlying game as the member, so it must suppress the gap.
    re.compile(r"\s*\(legacy\)\s*$", re.IGNORECASE),
)


# Comparison-only edition/SKU tails. Broader than every list above: the
# generic "<up to 3 words> Edition" tail catches SKU names no curated list can
# enumerate ("Millennium Edition", "Ultimate Sith Edition"), and the bare
# qualifiers include "Complete" — which normalize_purchase_title deliberately
# keeps (there, stripping it would book a compilation's price onto the base
# game). Safe here because these patterns NEVER alter identity, matching, or
# anything written: they only answer "are these two names the same game, one
# of them wearing an edition suffix?" for report/dedup decisions
# (check_library's extid.igdb_drift and nesting.superseded_base, the
# purchase-import create guard).
_COMPARISON_QUALIFIER = (
    # No "master": "Halo: The Master Chief Collection" is not an edition of
    # "Halo". A real "Master Edition" still lands via the generic rule below.
    # The optional trailing "+" covers SKU decorations ("Digital+ Edition",
    # "Deluxe+") — without it the qualifier never matched the decorated word,
    # and "Marvel's Midnight Suns Digital+ Edition" minted a duplicate beside
    # "Marvel's Midnight Suns".
    r"(?:game of the (?:year|century)|goty|complete|ultimate|deluxe|premium"
    r"|gold|platinum|definitive|standard|enhanced|legendary|anniversary"
    r"|collector'?s|remastered|redux|classic|uncut|uncensored|unrated"
    r"|digital)\+?"
)
_COMPARISON_EDITION_PATTERNS = (
    # Qualifier-anchored tail: a known edition word, up to two words riding
    # along, and an optional "Edition" ("… Ultimate Sith Edition", "…: The
    # Complete Edition", "… Ultimate Box", "Cities XL Platinum"). Anchoring on
    # the qualifier is what stops the generic rule below from eating real
    # subtitle words ("The Force Unleashed" is not an edition of "Star Wars").
    re.compile(
        rf"[\s:–—-]+(?:the\s+)?{_COMPARISON_QUALIFIER}(?:\s+[\w'&.]+){{0,2}}"
        r"(?:\s+edition)?\s*$",
        re.IGNORECASE,
    ),
    # Generic tail for SKU words no list can enumerate ("STRAFE: Millennium
    # Edition", "DARK SOULS: Prepare To Die Edition"). Runs AFTER the
    # qualifier-anchored rule, which has already claimed the cases where a
    # known edition word starts the tail — otherwise leftmost matching here
    # would cut a title short ("… The Force | Unleashed Ultimate Sith
    # Edition"). Over-stripping is bounded by how this is used: a suffix is
    # only ever called an edition when BOTH names collapse to the same title,
    # so a genuinely different SKU ("Sacred 2 Gold" vs "Sacred 2: Fallen
    # Angel") still reads as a mismatch.
    re.compile(r"[\s:–—-]+(?:the\s+)?(?:[\w'&.]+\s+){0,3}edition\s*$", re.IGNORECASE),
    # Trailing release-year marker ("Mass Effect (2007)").
    re.compile(r"\s*\(\s*\d{4}\s*\)\s*$"),
)


def normalize_edition_comparison_title(name: str) -> str:
    """Normalize a title for "same game, different edition?" comparisons.

    Loops normalize_catalog_title together with the comparison-only edition
    patterns above until stable, then hands off to normalize_search_text. Run
    BOTH sides through it: "Nioh 2 - The Complete Edition" and "Nioh 2" both
    collapse to "nioh 2", and "Sid Meier's Civilization III: Complete" meets
    "Sid Meier's Civilization III: Game of the Year Edition" in the middle.

    Deliberately over-eager compared with normalize_purchase_title — a title
    that IS just an edition phrase would strip to nothing, so the original's
    normalization is returned in that case rather than an empty string that
    would compare equal to every other fully-stripped name.
    """
    cleaned = name
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = normalize_catalog_title(cleaned)
        for pattern in _COMPARISON_EDITION_PATTERNS:
            cleaned = pattern.sub("", cleaned)
    normalized = normalize_search_text(cleaned)
    return normalized or normalize_search_text(name)


def is_edition_variant_of(name: str, other: str) -> bool:
    """True when two DIFFERENT titles are the same game modulo an edition suffix.

    Identical names return False — the question is only interesting when the
    raw titles differ (otherwise every equal pair would report as an edition
    relationship).
    """
    if normalize_search_text(name) == normalize_search_text(other):
        return False
    return normalize_edition_comparison_title(name) == normalize_edition_comparison_title(
        other
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
