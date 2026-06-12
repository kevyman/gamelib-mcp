"""Tag hygiene: separate Steam storefront/feature categories from real tags.

Steam's appdetails ``categories`` mix gameplay modes (co-op, PvP — useful taste
signals consumed by the vibe mappings) with storefront/platform features
(Steam Trading Cards, Family Sharing) that carry no taste information and used
to pollute tag_affinity. Feature flags are quarantined into ``games.features``;
gameplay-mode categories stay in ``games.tags`` on purpose.
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
    }
)


def is_feature_flag(tag: str) -> bool:
    return tag.lower() in STEAM_FEATURE_FLAGS


def split_features(tags: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split a tag list into (real_tags, feature_flags), preserving order."""
    real_tags: list[str] = []
    features: list[str] = []
    for tag in tags:
        (features if is_feature_flag(tag) else real_tags).append(tag)
    return real_tags, features
