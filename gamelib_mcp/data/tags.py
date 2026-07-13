"""Tag hygiene: separate storefront/feature/capability labels from real tags.

Steam's appdetails ``categories`` mix gameplay modes (co-op, PvP — useful taste
signals consumed by the vibe mappings) with storefront/platform features
(Steam Trading Cards, Family Sharing) that carry no taste information and used
to pollute tag_affinity; Steam's accessibility categories and a few IGDB
distribution keywords are the same kind of noise. Feature flags are
quarantined into ``games.features``; gameplay-mode categories stay in
``games.tags`` on purpose.
"""

from typing import Iterable

STEAM_FEATURE_FLAGS: frozenset[str] = frozenset(
    {
        "captions available",
        "commentary available",
        "cross-platform multiplayer",
        "downloadable content",
        "family sharing",
        "full controller support",
        "game demo",
        "hdr available",
        "in-app purchases",
        "includes level editor",
        "includes source sdk",
        "mods",
        "mods (require hl2)",
        "native steam controller support",
        "partial controller support",
        "remote play on phone",
        "remote play on tablet",
        "remote play on tv",
        "remote play together",
        "stats",
        "steam achievements",
        "steam cloud",
        "steam leaderboards",
        "steam timeline",
        "steam trading cards",
        "steam turn notifications",
        "steam workshop",
        "steamvr collectibles",
        "tracked controller support",
        "valve anti-cheat enabled",
        "vr only",
        "vr support",
        "vr supported",
        # Steam accessibility categories (2025+): capability metadata, not
        # taste. Left in tags they dominate matched_tags explanations ("save
        # anytime" is nobody's reason to love a game).
        "adjustable difficulty",
        "adjustable text colors",
        "adjustable text size",
        "camera comfort",
        "chat speech to text",
        "chat text to speech",
        "color alternatives",
        "custom volume controls",
        "keyboard only option",
        "mono sound",
        "mouse only option",
        "narrated game menus",
        "playable without timed input",
        "save anytime",
        "stereo sound",
        "subtitle options",
        "surround sound",
        "touch-friendly",
        # IGDB keywords that describe distribution/platform, not the game.
        "achievements",
        "controller",
        "crowdfunding - kickstarter",
        "digital distribution",
        "steam",
        "vr",
        # IGDB keywords about how a game is sold/funded/certified — they say
        # nothing about what playing it is like, but their rarity gives them
        # huge IDF weight in match scoring ("previously on - prime gaming"
        # once outranked every real tag in the taste profile).
        "abandonware",
        "controller recommendation",
        "controller support",
        "crowdfunded",
        "crowdfunding",
        "demo",
        "free demo",
        "game pass",
        "games with gold",
        "gog preservation program",
        "humble bundle",
        "kickstarter",
        "mouse only",
        "playstation plus",
        "single-player only",
        "steam greenlight",
        "unofficial",
        "wii classic controller support",
        "wii u pro controller support",
        "xbox controller support for pc",
    }
)

# IGDB mints one keyword per storefront/subscription/expo/award instance —
# open-ended families that can't be enumerated one tag at a time.
FEATURE_FLAG_PREFIXES: tuple[str, ...] = (
    "available on - ",
    "previously on - ",
    "crowdfunding - ",
    "the game awards",
    "game critics awards",
    "game developers choice awards",
    "pax aus",
    "pax east ",
    "pax prime ",
    "pax south ",
    "pax west ",
    "gamescom ",
    "e3 ",
    "gdc ",
    "igf ",
)


def is_feature_flag(tag: str) -> bool:
    lowered = tag.lower()
    return lowered in STEAM_FEATURE_FLAGS or lowered.startswith(FEATURE_FLAG_PREFIXES)


def split_features(tags: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split a tag list into (real_tags, feature_flags), preserving order."""
    real_tags: list[str] = []
    features: list[str] = []
    for tag in tags:
        (features if is_feature_flag(tag) else real_tags).append(tag)
    return real_tags, features
