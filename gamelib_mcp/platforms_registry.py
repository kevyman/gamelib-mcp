"""Single registry of every platform the library knows about.

Before this module existed, adding a platform meant editing ~7 scattered
registration points (frozensets and alias maps in ``tools/common.py``, two
sync dicts in ``tools/admin.py``, the metadata tuple and inspector aliases in
``lifecycle.py``, and the inspector dict in ``integrations/inspectors.py``).
All of those now derive from ``PLATFORMS`` below, so adding a platform is:
write its ``data/<platform>.py`` sync module, then add one ``PlatformSpec``.

Deliberately dependency-free (stdlib only): sync/wishlist/inspector functions
are referenced as ``(module_path, attr)`` strings and resolved lazily via
``resolve_platform_functions``, so importing this registry never drags in the
provider modules and cannot create import cycles. Resolution checks a caller
supplied namespace first — ``tools/admin.py`` passes its own module — so the
established test pattern of patching e.g. ``gamelib_mcp.tools.admin.sync_epic``
keeps working.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSpec:
    name: str
    # Public alias → this platform (e.g. "nintendo"/"switch" → switch2).
    aliases: tuple[str, ...] = ()
    # Whether games can be recorded against it manually (LIBRARY_PLATFORMS).
    library: bool = True
    # (module_path, attr) of the ownership/playtime sync coroutine, if any.
    sync: tuple[str, str] | None = None
    # (module_path, attr) of the wishlist sync coroutine, if any.
    wishlist_sync: tuple[str, str] | None = None
    # The name the integration inspector uses where it differs (switch2 is
    # synced as "switch2" but inspected as "nintendo").
    inspector_name: str | None = None
    # Attribute name of the readiness probe in integrations/inspectors.py.
    inspector_attr: str | None = None


PLATFORMS: tuple[PlatformSpec, ...] = (
    PlatformSpec(
        "steam",
        sync=("gamelib_mcp.data.steam_xml", "fetch_library"),
        wishlist_sync=("gamelib_mcp.data.steam_wishlist", "fetch_wishlist"),
        inspector_attr="inspect_steam",
    ),
    PlatformSpec(
        "epic",
        sync=("gamelib_mcp.data.epic", "sync_epic"),
        inspector_attr="inspect_epic",
    ),
    PlatformSpec(
        "gog",
        sync=("gamelib_mcp.data.gog", "sync_gog"),
        inspector_attr="inspect_gog",
    ),
    PlatformSpec(
        "switch2",
        aliases=("nintendo", "switch"),
        sync=("gamelib_mcp.data.nintendo", "sync_nintendo"),
        # Nintendo has no wishlist API; the switch2 wishlist comes from a
        # DekuDeals shared-wishlist export.
        wishlist_sync=("gamelib_mcp.data.dekudeals", "sync_dekudeals_wishlist"),
        inspector_name="nintendo",
        inspector_attr="inspect_nintendo",
    ),
    PlatformSpec(
        "ps5",
        sync=("gamelib_mcp.data.psn", "sync_psn"),
        inspector_attr="inspect_psn",
    ),
    # Manual-only stores: no sync backend, no inspector.
    PlatformSpec("itchio"),
    PlatformSpec("xbox"),
    PlatformSpec("ea", aliases=("origin",)),
    PlatformSpec("ubisoft", aliases=("uplay",)),  # Ubisoft Connect, formerly Uplay
    PlatformSpec("other"),
)

PLATFORMS_BY_NAME: dict[str, PlatformSpec] = {spec.name: spec for spec in PLATFORMS}

# Public alias → internal DB platform name.
PLATFORM_ALIASES: dict[str, str] = {
    alias: spec.name for spec in PLATFORMS for alias in spec.aliases
}

# Platforms with an automated sync backend (canonical, post-alias names).
SYNCABLE_PLATFORMS: frozenset[str] = frozenset(spec.name for spec in PLATFORMS if spec.sync)

# Every platform a game can be recorded against in the library (post-alias).
LIBRARY_PLATFORMS: frozenset[str] = frozenset(spec.name for spec in PLATFORMS if spec.library)

# Platforms with an automated wishlist sync backend. PSN has no public
# wishlist API — use add_game_to_platform(owned=False) for it instead.
WISHLIST_SYNCABLE_PLATFORMS: frozenset[str] = frozenset(
    spec.name for spec in PLATFORMS if spec.wishlist_sync
)

# Platforms whose per-run sync outcome is recorded in the meta table, in
# registry declaration order. These are the keys the library sync emits in
# its result dict and get_sync_status reads back.
SYNC_METADATA_PLATFORMS: tuple[str, ...] = tuple(spec.name for spec in PLATFORMS if spec.sync)

# Maps a sync-metadata platform key to the name the integration inspector
# uses, where they differ.
INSPECTOR_PLATFORM_ALIASES: dict[str, str] = {
    spec.name: spec.inspector_name
    for spec in PLATFORMS
    if spec.inspector_name and spec.inspector_name != spec.name
}


def resolve_platform_functions(
    kind: str, namespace: object | None = None
) -> dict[str, Callable]:
    """Build {platform: coroutine} for every spec carrying a ``kind`` ref.

    kind is "sync" or "wishlist_sync". When ``namespace`` (typically the
    calling module) defines an attribute with the referenced function's name,
    that binding wins — this is what lets tests patch the function on
    ``gamelib_mcp.tools.admin`` and have the sync pick the mock up.
    """
    result: dict[str, Callable] = {}
    for spec in PLATFORMS:
        ref: tuple[str, str] | None = getattr(spec, kind)
        if ref is None:
            continue
        module_path, attr = ref
        fn = getattr(namespace, attr, None) if namespace is not None else None
        if fn is None:
            fn = getattr(importlib.import_module(module_path), attr)
        result[spec.name] = fn
    return result
