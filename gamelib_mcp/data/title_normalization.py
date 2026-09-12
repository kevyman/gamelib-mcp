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


# Storefronts and IGDB disagree about the ampersand: Steam ships "Rabbit and
# Steel" while IGDB holds "Rabbit & Steel", and a tokenizer that keeps only
# [a-z0-9]+ runs drops "&" entirely ("rabbit steel") while "and" survives
# ("rabbit and steel") — two spellings of one title that could never compare
# equal. Folding the ampersand into the word before tokenizing makes them meet.
_AMPERSAND_RE = re.compile(r"\s*&\s*")
_AND_WORD_RE = re.compile(r"\band\b", re.IGNORECASE)

# Apostrophes are REMOVED, not treated as separators: "Death's Door" and
# "Deaths Door" are one title, and splitting on the glyph ("death s door")
# leaves a stray token that matches neither spelling. Curly quotes and the
# backtick spelling included — NFKD leaves all of them alone.
_APOSTROPHE_TABLE = str.maketrans("", "", "'’‘`")

# A run of two or more single-letter-plus-dot groups is an acronym spelled out
# with periods ("F.E.A.R.", "Q.U.B.E. 2", "S.T.A.L.K.E.R.") — join the letters
# so it meets the undotted spelling stores use. The optional trailing letter
# covers a dropped final dot ("F.E.A.R"). The lookbehind is what keeps an
# ordinary abbreviation out of it: in "Mr. Driller" the only letter+dot group
# is preceded by a letter, so there is no run to join and it stays "mr driller".
_DOTTED_ACRONYM_RE = re.compile(r"(?<![a-z0-9])(?:[a-z]\.){2,}[a-z]?")

# Roman numerals folded to Arabic as WHOLE tokens, so "Hades II" and "Hades 2"
# are one title. Deliberately excludes "x" and "i": "Mega Man X" is a different
# game from "Mega Man 10", and a lone "I" is too ambiguous to read as a number.
_ROMAN_TOKEN_TO_ARABIC = {
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
}

# Whole-token spelling pairs that name one title. "vs." tokenizes to "vs"
# already, so the only spelling left to fold is the written-out word: Steam
# ships "Marvel vs. Capcom" while other catalogs hold "Marvel versus Capcom".
# Folded as a WHOLE token, never a substring, so "Reversus" is untouched.
_SPELLING_TOKEN_ALIASES = {
    "versus": "vs",
}

# Spelled-out numbers folded to digits ("Left 4 Dead" / "Left Four Dead").
_NUMBER_WORD_TO_DIGITS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}


def match_key(value: str) -> str:
    """The one canonical key for "are these two strings the same title?".

    Every comparison-only normalizer in this module ends here, and so does the
    IGDB resolver's name gate — one key instead of the six near-identical
    normalizations that let "&" (#184) and the Humble folding mismatch through.
    It folds, in order: trademark glyphs, diacritics and case; apostrophes
    (joined, never split); dotted acronyms; the ampersand/"and" spelling;
    Roman numerals ii-ix, the number words zero-ten and the "versus"/"vs"
    spelling, per whole token.

    What it deliberately does NOT fold: "x" and a lone "i" (see
    _ROMAN_TOKEN_TO_ARABIC), because "Mega Man X" is not "Mega Man 10".

    This is not ``normalize_search_text``: that one backs
    ``games.name_normalized`` and the FTS index (changing it would need a
    stored-column rebuild), and it is a SEARCH key — word order preserved,
    nothing reinterpreted. This one is an IDENTITY key and may reinterpret
    freely, because nothing is stored under it.
    """
    # NFKD would otherwise expand ™ to a literal "TM" glued onto the word.
    cleaned = value.replace("™", " ").replace("®", " ")
    folded = _ascii_fold(cleaned).casefold()
    folded = folded.translate(_APOSTROPHE_TABLE)
    folded = _DOTTED_ACRONYM_RE.sub(lambda m: m.group(0).replace(".", ""), folded)
    folded = _AMPERSAND_RE.sub(" and ", folded)

    tokens = []
    for token in re.findall(r"[a-z0-9]+", folded):
        tokens.append(
            _ROMAN_TOKEN_TO_ARABIC.get(token)
            or _NUMBER_WORD_TO_DIGITS.get(token)
            or _SPELLING_TOKEN_ALIASES.get(token)
            or token
        )
    return " ".join(tokens)


def normalize_catalog_title(name: str) -> str:
    cleaned = _ascii_fold(name)
    cleaned = cleaned.replace("™", "").replace("®", "")
    # The bare-word arms are guarded against a neighbouring period: "F.E.A.R."
    # and "S.T.A.L.K.E.R." carry a lone "R" between dots, and stripping it left
    # "F.E.A.." — a title that then compared equal to nothing, silently, in
    # every consumer of normalize_edition_comparison_title (the IGDB gate, the
    # PSN SKU fold, the acquisition edition probe). A real trademark marker is
    # never dot-adjacent; "(R)" and "(TM)" above still catch the common form.
    cleaned = re.sub(r"\(TM\)|\(R\)|(?<!\.)\bTM\b(?!\.)|(?<!\.)\bR\b(?!\.)", "", cleaned)
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
    # Prime Gaming / Luna giveaway markers on GOG and Epic order titles
    # ("Dishonored - Definitive Edition - Amazon Prime", "Fallout 2 - Amazon
    # Luna", "Batman: Arkham Knight (Amazon Prime)"). The importer minted a
    # phantom "X - Amazon Prime" row beside the real one for every such order.
    # Anchored on the two-word marker, never a bare "Prime": "Metroid Prime
    # Remastered" must keep its title word.
    re.compile(
        r"[\s:–—-]+\(?(?:Amazon\s+(?:Prime|Luna)(?:\s+Gaming)?|Prime\s+Gaming)\)?\s*$",
        re.IGNORECASE,
    ),
    # Trailing parenthesised platform tag on a purchase line ("Factorio
    # (Steam)", "Cuphead (Epic Games Store)") — which store delivered the key,
    # never part of the game's name.
    re.compile(
        r"\s*\((?:Steam|GOG|Epic(?:\s+Games(?:\s+Store)?)?|PC)\)\s*$",
        re.IGNORECASE,
    ),
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
    r"|digital|day one|launch)\+?"
)
# STRICT patterns: a KNOWN edition word (or a trailing year), nothing else.
# What separates these from the generic tail below is evidence — every phrase
# here is one a storefront actually uses to repackage the same game, so an
# equality reached through them is an edition relationship and not a guess.
_STRICT_COMPARISON_EDITION_PATTERNS = (
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
    # Trailing release-year marker ("Mass Effect (2007)").
    re.compile(r"\s*\(\s*\d{4}\s*\)\s*$"),
)

_COMPARISON_EDITION_PATTERNS = (
    *_STRICT_COMPARISON_EDITION_PATTERNS,
    # Generic tail for SKU words no list can enumerate ("STRAFE: Millennium
    # Edition", "DARK SOULS: Prepare To Die Edition"). Runs AFTER the
    # qualifier-anchored rule, which has already claimed the cases where a
    # known edition word starts the tail — otherwise leftmost matching here
    # would cut a title short ("… The Force | Unleashed Ultimate Sith
    # Edition"). It is a GUESS: the words it eats are arbitrary, so
    # "Minecraft: Education Edition" collapses onto "Minecraft" just as
    # readily as a real SKU does. Over-stripping is bounded by how this is
    # used — a suffix is only ever called an edition when BOTH names collapse
    # to the same title, so a genuinely different SKU ("Sacred 2 Gold" vs
    # "Sacred 2: Fallen Angel") still reads as a mismatch — and any caller
    # that LINKS rather than reports must tell the two strips apart
    # (igdb.py's gate ranks a generic-tail equality below a strict one and
    # demands year evidence for it).
    re.compile(r"[\s:–—-]+(?:the\s+)?(?:[\w'&.]+\s+){0,3}edition\s*$", re.IGNORECASE),
)


def _strip_edition_suffixes(name: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    """Loop ``normalize_catalog_title`` with ``patterns`` until stable."""
    cleaned = name
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = normalize_catalog_title(cleaned)
        for pattern in patterns:
            cleaned = pattern.sub("", cleaned)
    return cleaned


def normalize_strict_edition_title(name: str) -> str:
    """``normalize_edition_comparison_title`` without the generic SKU guess.

    Same loop, KNOWN edition phrases only (`_STRICT_COMPARISON_EDITION_PATTERNS`):
    "Watch Dogs: Day One Edition" and "Nioh 2 - The Complete Edition" still
    collapse onto their base games, while "Minecraft: Education Edition" keeps
    its tail — "Education" is not an edition word, it is a different product.
    Use this wherever an equality DECIDES something (igdb.py's resolver gate
    ranks a strict equality above a generic-tail one); the full form stays for
    report/dedup callers that only ever describe a relationship.
    """
    normalized = match_key(_strip_edition_suffixes(name, _STRICT_COMPARISON_EDITION_PATTERNS))
    return normalized or match_key(name)


def strip_comparison_edition_suffixes(name: str) -> str:
    """``normalize_edition_comparison_title``'s stripping, without the key.

    The raw title with every comparison-only edition/SKU tail peeled off, still
    in display form. Exposed for the one caller that must prefilter in SQL
    against ``games.name_normalized`` (acquisition.py's edition-sibling probe):
    that column is built from ``normalize_search_text``, so comparing it with
    the ``match_key`` this function's caller returns would silently miss every
    apostrophe/ampersand/numeral title. Normalize this with
    ``normalize_search_text`` for the prefilter, then confirm the hit with
    ``normalize_edition_comparison_title``.
    """
    return _strip_edition_suffixes(name, _COMPARISON_EDITION_PATTERNS)


def normalize_edition_comparison_title(name: str) -> str:
    """Normalize a title for "same game, different edition?" comparisons.

    Loops normalize_catalog_title together with the comparison-only edition
    patterns above until stable, then hands off to ``match_key``. Run BOTH
    sides through it: "Nioh 2 - The Complete Edition" and "Nioh 2" both
    collapse to "nioh 2", and "Sid Meier's Civilization III: Complete" meets
    "Sid Meier's Civilization III: Game of the Year Edition" in the middle.

    Deliberately over-eager compared with normalize_purchase_title — a title
    that IS just an edition phrase would strip to nothing, so the original's
    normalization is returned in that case rather than an empty string that
    would compare equal to every other fully-stripped name.

    Includes the GENERIC "<up to 3 words> Edition" tail, which is a guess:
    "Minecraft: Education Edition" collapses onto "Minecraft" just as readily
    as a real SKU does. Fine for describing a relationship; a caller that
    LINKS on the answer wants ``normalize_strict_edition_title`` (or must add
    its own evidence, as igdb.py's resolver gate does with the release year).
    """
    normalized = match_key(strip_comparison_edition_suffixes(name))
    return normalized or match_key(name)


def is_edition_variant_of(name: str, other: str) -> bool:
    """True when two DIFFERENT titles are the same game modulo an edition suffix.

    Identical names return False — the question is only interesting when the
    raw titles differ (otherwise every equal pair would report as an edition
    relationship).
    """
    if match_key(name) == match_key(other):
        return False
    return normalize_edition_comparison_title(name) == normalize_edition_comparison_title(
        other
    )


# SKU phrases that repackage the SAME store product — a "Complete"/"Deluxe"/
# "Gold"/GOTY edition is (near-universally) the base game plus its DLC under
# one storefront listing, not a separate app. Deliberately a strict SUBSET of
# _SERIES_GAP_EDITION_PATTERNS: remastered / director's cut / anniversary /
# legendary / definitive / enhanced / bare "edition" are EXCLUDED because
# those routinely ship as their own Steam appid and their own library row
# (BioShock vs BioShock Remastered, Death Stranding vs its Director's Cut,
# Mafia II vs its Definitive Edition). Used where a match DELETES a record —
# the Humble importer's key-vs-key fold — so a phrase belongs here only when
# collapsing on it can never lose a separately-owned product.
_SAME_PRODUCT_SKU_PATTERNS = (
    re.compile(r"[\s:–—-]+(?:the\s+)?game of the year(?: edition)?\s*$", re.IGNORECASE),
    re.compile(r"[\s:–—-]+(?:the\s+)?goty(?: edition)?\s*$", re.IGNORECASE),
    re.compile(r"[\s:–—-]+(?:the\s+)?complete edition\s*$", re.IGNORECASE),
    re.compile(r"[\s:–—-]+(?:the\s+)?deluxe edition\s*$", re.IGNORECASE),
    re.compile(r"[\s:–—-]+(?:the\s+)?gold edition\s*$", re.IGNORECASE),
)


def normalize_same_product_sku_title(name: str) -> str:
    """Normalize a title for "same store product, different SKU?" comparisons.

    The destructive-merge counterpart of normalize_series_gap_title: same
    loop, far narrower phrase list (see _SAME_PRODUCT_SKU_PATTERNS). Use this
    one when a match will DROP a record rather than merely report or redirect.
    """
    cleaned = name
    previous = None
    while cleaned != previous:
        previous = cleaned
        for pattern in _SAME_PRODUCT_SKU_PATTERNS:
            cleaned = pattern.sub("", cleaned)
    return match_key(cleaned)


def ampersand_alternate(name: str) -> str | None:
    """The other spelling of an ampersand/"and" title, or None.

    "Rabbit & Steel" -> "Rabbit and Steel" and "Rabbit and Steel" ->
    "Rabbit & Steel"; only the first applicable direction, never both, so the
    result always differs from the input in exactly one way. None when the
    title carries neither token (or the swap changes nothing) — a caller uses
    this as ONE extra exact-name query, not as a normalization.
    """
    if "&" in name:
        alternate = _AMPERSAND_RE.sub(" and ", name).strip()
    elif _AND_WORD_RE.search(name):
        alternate = _AND_WORD_RE.sub("&", name)
    else:
        return None
    alternate = re.sub(r"\s+", " ", alternate).strip()
    return alternate if alternate and alternate != name else None


def normalize_series_gap_title(name: str) -> str:
    """Normalize a title for discover_series_gaps have/gap exclusion matching.

    Loops off a trailing edition marker (so "Game of the Year Enhanced
    Edition" fully collapses), then hands off to ``match_key`` — which folds
    case, diacritics, punctuation, apostrophes, the ampersand/"and" spelling,
    dotted acronyms and numeral spellings, so every one of those variations is
    decided in one place rather than here.
    """
    cleaned = name
    previous = None
    while cleaned != previous:
        previous = cleaned
        for pattern in _SERIES_GAP_EDITION_PATTERNS:
            cleaned = pattern.sub("", cleaned)
    return match_key(cleaned)
