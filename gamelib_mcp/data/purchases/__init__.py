"""Purchase-history importers — normalized records from storefront accounts.

Each importer module exposes a zero-argument coroutine
``async def fetch_x() -> tuple[list[PurchaseRecord], list[dict]]`` returning
``(records, skipped)``: ``records`` are importable purchases normalized to the
acquisition vocabulary (``tools/acquisition.py``), ``skipped`` is a list of
``{"title"/"description", "reason"}`` dicts for rows deliberately not imported
(refunds, consumables, non-game items). Fetchers RAISE on auth/network/parse
failure — the ``import_purchases`` orchestrator catches per source, so one
broken storefront never poisons another's import.

``PURCHASE_IMPORTERS`` mirrors ``platforms_registry``: fetchers are referenced
as ``(module_path, attr)`` strings and resolved lazily, so importing this
package never drags in provider modules, and resolution can prefer names bound
on a caller-supplied namespace (which keeps the
``patch("gamelib_mcp.tools.acquisition.fetch_eshop_purchases", ...)`` test
pattern working). Adding an importer = write
``data/purchases/<source>.py`` + one entry here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gamelib_mcp.data.db import GOG_PRODUCT_ID, STEAM_APP_ID


@dataclass(frozen=True)
class PurchaseRecord:
    """One normalized purchase, ready to become a set_acquisitions_batch item."""

    title: str
    # Library platform the purchase lands on (e.g. "switch2", "steam").
    platform: str
    # Value from the tools.acquisition PURCHASE_SOURCES vocabulary.
    purchase_source: str
    acquired_at: str | None  # YYYY-MM-DD
    price_paid: float | None
    price_currency: str | None
    bundle_name: str | None = None
    # Provider id when available (e.g. a Nintendo title id).
    store_identifier: str | None = None
    # Free-form annotation (e.g. "refund"); used for skip reporting.
    note: str | None = None
    # A multi-game bundle whose `title` is the bundle name, not a single game.
    # import_purchases diverts these to `bundles_needing_split` instead of the
    # single-game matcher (which they'd only ever miss) — a human/AI looks up
    # the constituents and calls split_bundle_acquisition.
    is_bundle: bool = False
    # A nested content-type hint (e.g. "dlc") from the store's own item
    # typing when the source exposes one (eShop), or from an addon-ish name
    # (Humble, which has no item typing). None = no signal.
    content_type: str | None = None


# source key → (module_path, attr) of the fetch coroutine, resolved lazily.
PURCHASE_IMPORTERS: dict[str, tuple[str, str]] = {
    "epic": ("gamelib_mcp.data.purchases.epic_orders", "fetch_epic_purchases"),
    "eshop": ("gamelib_mcp.data.purchases.nintendo_ec", "fetch_eshop_purchases"),
    "gog": ("gamelib_mcp.data.purchases.gog_orders", "fetch_gog_purchases"),
    "humble": ("gamelib_mcp.data.purchases.humble", "fetch_humble_purchases"),
    "steam": ("gamelib_mcp.data.purchases.steam_history", "fetch_steam_purchases"),
}

# source key → game_platform_identifiers.identifier_type carried by that
# source's PurchaseRecord.store_identifier, letting set_acquisitions_batch
# match identifier-first (a renamed/localized library title still resolves).
# No "eshop" entry — the eShop GraphQL API returns no product id, so it falls
# back to title matching. No "epic" entry either: order items carry an
# offerId, which is a different id space from the epic_artifact_id the library
# sync stores — matching one against the other would never hit.
#
# "humble" maps to STEAM_APP_ID because a Humble key carries `steam_app_id`
# and nothing else: only its STEAM records ever set store_identifier (see
# data/purchases/humble.py::_tpk_steam_appid), so there is no second id space
# for this entry to be wrong about. Attaching an identifier to a Humble GOG
# key would silently make it match against Steam appids — hence the invariant
# is enforced in the importer and covered by a test.
IDENTIFIER_TYPES: dict[str, str] = {
    "gog": GOG_PRODUCT_ID,
    "humble": STEAM_APP_ID,
    "steam": STEAM_APP_ID,
}


# In-game currency / consumable packs sold as storefront line items ("1,000
# V-Bucks", "Rocket League® - Credits x1100", "2,800 Apex Coins", "EA SPORTS FC
# 24 - 1050 FC Points", "Quake Champions Early Access plus 50 Shards, 100
# Platinum, 2000 Favor"). No storefront types these, so the NAME is the only
# signal: a count attached to a currency noun — either order — or an
# unambiguous brand. A bare noun with no number never trips it, so a game
# legitimately titled "…Coins"/"…Platinum" is safe.
_CURRENCY_NOUN = (
    r"(?:v-?bucks|show-?bucks|credits?|coins?|points|gems|gold\s+bars|shards?"
    r"|crowns?|tokens?|favor)"
)
# Only reachable through the "plus <count> <noun>" join below: "Platinum" alone
# names editions far more often than currency ("Cities XL Platinum").
_JOINED_CURRENCY_NOUN = rf"(?:{_CURRENCY_NOUN}|platinum|gold)"
_CONSUMABLE_NAME_RE = re.compile(
    rf"\bv-?bucks\b|\bshow-?bucks\b"
    # Count then noun, with up to two words riding between ("2,800 Apex Coins").
    rf"|\b\d[\d,.]*\+?\s*(?:[\w'&.®™-]+\s+){{0,2}}{_CURRENCY_NOUN}\b"
    # Noun then count ("Credits x1100").
    rf"|\b{_CURRENCY_NOUN}\s*x\s*\d"
    # "<game> plus 50 Shards, 100 Platinum, 2000 Favor" — the currency tail is
    # bolted onto a real game name, so the pack must be recognized by the
    # "plus <count> <noun>" join rather than by the leading title.
    rf"|\bplus\s+\d[\d,.]*\s*(?:[\w'&.®™-]+\s+){{0,2}}{_JOINED_CURRENCY_NOUN}\b",
    re.IGNORECASE,
)


def is_consumable_title(title: str) -> bool:
    """True for an in-game currency/consumable line item, not a game.

    Shared by the importers: fed to the matcher such a line either lands in
    unmatched or, under create_missing, mints a phantom owned "game".
    """
    return bool(_CONSUMABLE_NAME_RE.search(title or ""))


_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def normalize_purchase_date(value: object) -> str | None:
    """Extract YYYY-MM-DD from an ISO-ish timestamp; None when unparseable."""
    if not isinstance(value, str):
        return None
    match = _DATE_PREFIX_RE.match(value.strip())
    return match.group(1) if match else None
