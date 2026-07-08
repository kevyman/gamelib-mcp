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


# source key → (module_path, attr) of the fetch coroutine, resolved lazily.
PURCHASE_IMPORTERS: dict[str, tuple[str, str]] = {
    "eshop": ("gamelib_mcp.data.purchases.nintendo_ec", "fetch_eshop_purchases"),
    "gog": ("gamelib_mcp.data.purchases.gog_orders", "fetch_gog_purchases"),
    "humble": ("gamelib_mcp.data.purchases.humble", "fetch_humble_purchases"),
    "steam": ("gamelib_mcp.data.purchases.steam_history", "fetch_steam_purchases"),
}

# source key → game_platform_identifiers.identifier_type carried by that
# source's PurchaseRecord.store_identifier, letting set_acquisitions_batch
# match identifier-first (a renamed/localized library title still resolves).
# No "humble" entry — Humble orders carry no store identifiers. "eshop" uses
# the literal value of gamelib_mcp.data.nintendo.NINTENDO_TITLE_ID: importing
# data.nintendo here would drag httpx/bs4/igdb into this deliberately light
# package (see module docstring).
IDENTIFIER_TYPES: dict[str, str] = {
    "eshop": "nintendo_title_id",  # gamelib_mcp.data.nintendo.NINTENDO_TITLE_ID
    "gog": GOG_PRODUCT_ID,
    "steam": STEAM_APP_ID,
}


_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def normalize_purchase_date(value: object) -> str | None:
    """Extract YYYY-MM-DD from an ISO-ish timestamp; None when unparseable."""
    if not isinstance(value, str):
        return None
    match = _DATE_PREFIX_RE.match(value.strip())
    return match.group(1) if match else None
