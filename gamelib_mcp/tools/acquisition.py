"""set_acquisition, set_acquisitions_batch, and get_spending_stats tools.

Acquisition data (when/where/for-how-much a game was obtained) lives on
game_platforms and is written exclusively through
``db.set_platform_acquisition`` — no sync writer touches those columns, so
there is no manual_overrides dance here. This module also owns the shared
purchase-source vocabulary; ``platforms.py`` will import it, so anything here
that needs a platforms.py helper must lazy-import it inside the function to
avoid an import cycle.
"""

import asyncio
import importlib
import re
import sys
from datetime import date

from fastmcp.exceptions import ToolError

from ..data.db import (
    ACQUISITION_FIELDS,
    clear_fulfilled_wishlist_entries,
    fts_ready,
    get_db,
    get_game_by_identifier,
    set_platform_acquisition,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
)
from ..data.purchases import IDENTIFIER_TYPES, PURCHASE_IMPORTERS, PurchaseRecord
from ..data.title_normalization import normalize_purchase_title, normalize_search_text

# The importer dict is resolved at call time; the imports below keep the
# fetchers bound on this module so tests can patch
# gamelib_mcp.tools.acquisition.<fetch_fn> (_resolve_purchase_fetchers checks
# this namespace first, mirroring platforms_registry.resolve_platform_functions).
# F401: referenced via getattr, not by name.
from ..data.purchases.gog_orders import fetch_gog_purchases  # noqa: F401
from ..data.purchases.humble import fetch_humble_purchases  # noqa: F401
from ..data.purchases.nintendo_ec import fetch_eshop_purchases  # noqa: F401
from ..data.purchases.steam_history import fetch_steam_purchases  # noqa: F401
from .common import (
    LIBRARY_PLATFORMS,
    validate_platform as _validate_platform,
)
from .search import (
    NORMALIZED_NAME_SQL,
    build_name_match,
    fuzzy_fallback_game_ids,
)

# Closed vocabulary for purchase_source. Two deliberately distinct no-cost
# sources: "free" = a no-strings giveaway (e.g. an Epic weekly free game) —
# yours forever; "subscription" = claimed via a paid membership (Game Pass,
# PS+ monthly, Humble Choice) — access may lapse with the subscription.
PURCHASE_SOURCES = frozenset({
    "steam", "gog", "epic", "eshop", "psn", "xbox",
    "humble", "fanatical", "itchio", "ea", "ubisoft",
    "physical", "gift", "free", "subscription", "other",
})

# Common storefront spellings → canonical source. Keys are compared after
# strip().lower(), so entries here stay lowercase.
SOURCE_ALIASES: dict[str, str] = {
    "steam store": "steam",
    "humble bundle": "humble",
    "humble-bundle": "humble",
    "humble choice": "humble",
    "humble monthly": "subscription",
    "nintendo": "eshop",
    "nintendo eshop": "eshop",
    "switch": "eshop",
    "switch2": "eshop",
    "playstation": "psn",
    "ps store": "psn",
    "playstation store": "psn",
    "ps5": "psn",
    "xbox store": "xbox",
    "microsoft store": "xbox",
    "epic games store": "epic",
    "egs": "epic",
    "gifted": "gift",
    "game pass": "subscription",
    "ps plus": "subscription",
    "itch": "itchio",
    "itch.io": "itchio",
    "origin": "ea",
    "uplay": "ubisoft",
    "retail": "physical",
    "disc": "physical",
    "cartridge": "physical",
}

# YYYY, YYYY-MM, or YYYY-MM-DD; calendar validity is checked separately below.
_ACQUIRED_AT_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_BATCH_ITEM_CAP = 200
_BATCH_ITEM_KEYS = frozenset({
    "name", "game_id", "platform", "identifier_type", "identifier_value",
    *ACQUISITION_FIELDS,
})


def _normalize_source(value: str) -> str:
    """strip/lower → alias map → vocabulary check; ToolError on a miss."""
    normalized = value.strip().lower()
    normalized = SOURCE_ALIASES.get(normalized, normalized)
    if normalized not in PURCHASE_SOURCES:
        raise ToolError(
            f"Unknown purchase_source '{value}'. Valid: {sorted(PURCHASE_SOURCES)}"
        )
    return normalized


def _validate_acquired_at(value: str) -> str:
    """Accept YYYY, YYYY-MM, or YYYY-MM-DD; stored exactly as given."""
    cleaned = value.strip()
    if not _ACQUIRED_AT_RE.match(cleaned):
        raise ToolError(
            f"acquired_at must be YYYY, YYYY-MM, or YYYY-MM-DD (got '{value}')"
        )
    if len(cleaned) == 7 and not 1 <= int(cleaned[5:7]) <= 12:
        raise ToolError(f"acquired_at month out of range in '{value}'")
    if len(cleaned) == 10:
        try:
            date.fromisoformat(cleaned)
        except ValueError:
            raise ToolError(f"acquired_at is not a real calendar date: '{value}'")
    return cleaned


def _validate_price(
    price_paid: float | None, price_currency: str | None
) -> tuple[float | None, str | None]:
    """Validate the price pair; a price without a currency defaults to USD."""
    if price_paid is None:
        if price_currency is not None:
            raise ToolError("price_currency requires price_paid")
        return None, None
    if price_paid < 0:
        raise ToolError("price_paid must not be negative")
    currency = (price_currency or "USD").strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise ToolError(
            f"price_currency must be a 3-letter ISO code (got '{price_currency}')"
        )
    return float(price_paid), currency


def _validated_fields(
    acquired_at: str | None,
    price_paid: float | None,
    price_currency: str | None,
    purchase_source: str | None,
    bundle_name: str | None,
) -> dict:
    """Map provided acquisition params to validated column values."""
    fields: dict = {}
    if acquired_at is not None:
        fields["acquired_at"] = _validate_acquired_at(str(acquired_at))
    price, currency = _validate_price(price_paid, price_currency)
    if price is not None:
        fields["price_paid"] = price
        fields["price_currency"] = currency
    if purchase_source is not None:
        fields["purchase_source"] = _normalize_source(purchase_source)
    if bundle_name is not None:
        cleaned = bundle_name.strip()
        if not cleaned:
            raise ToolError("bundle_name must not be empty (use clear to remove it)")
        fields["bundle_name"] = cleaned
    return fields


async def set_acquisition(
    name: str | None = None,
    game_id: int | None = None,
    platform: str | None = None,
    acquired_at: str | None = None,
    price_paid: float | None = None,
    price_currency: str | None = None,
    purchase_source: str | None = None,
    bundle_name: str | None = None,
    clear: list[str] | None = None,
    create_platform_row: bool = True,
) -> dict:
    """
    Record how one game was acquired on one platform.

    Resolve the game with game_id or name, then set any subset of acquired_at
    (YYYY / YYYY-MM / YYYY-MM-DD), price_paid (currency defaults to USD),
    purchase_source (see PURCHASE_SOURCES), and bundle_name. clear lists
    columns to reset to NULL. Missing game_platforms rows are created
    (owned=1) unless create_platform_row=False.
    """
    # Lazy import: platforms.py imports this module's vocabulary, so a
    # top-level import here would be a cycle.
    from .platforms import _resolve_game_row

    if platform is None:
        raise ToolError("platform is required")
    platform = _validate_platform(platform, LIBRARY_PLATFORMS)

    clear_list = list(dict.fromkeys(clear or []))
    invalid = [c for c in clear_list if c not in ACQUISITION_FIELDS]
    if invalid:
        raise ToolError(
            f"clear has unknown column(s): {invalid}. Valid: {sorted(ACQUISITION_FIELDS)}"
        )

    fields = _validated_fields(
        acquired_at, price_paid, price_currency, purchase_source, bundle_name
    )
    if not fields and not clear_list:
        raise ToolError("Provide at least one acquisition field to set or clear")
    conflict = set(fields) & set(clear_list)
    if conflict:
        raise ToolError(
            f"Cannot set and clear the same column(s) in one call: {sorted(conflict)}"
        )

    row = await _resolve_game_row(name, game_id)
    resolved_id = row["id"]

    async with get_db() as db:
        gp = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (resolved_id, platform),
        )

    platform_row_created = False
    if gp is None:
        if not create_platform_row:
            raise ToolError(
                f"'{row['name']}' has no {platform} platform row. Pass "
                "create_platform_row=True or add it first with add_game_to_platform."
            )
        gpid = await upsert_game_platform(resolved_id, platform, owned=1)
        platform_row_created = True
    else:
        gpid = gp["id"]

    for column in clear_list:
        fields[column] = None
    acquisition = await set_platform_acquisition(gpid, fields)

    return {
        "game_id": resolved_id,
        "name": row["name"],
        "platform": platform,
        "game_platform_id": gpid,
        "platform_row_created": platform_row_created,
        "acquisition": acquisition,
        "cleared": clear_list,
    }


async def _match_batch_game(
    name: str | None,
    game_id: int | None,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    *,
    fuzzy: bool = True,
):
    """Resolve one batch item to (row, match_type); (None, None) on a miss.

    Same tiers as platforms._resolve_game_row (id > tiered name > fuzzy) but
    never raises — a batch import reports unmatched items instead of failing.
    A store identifier (e.g. nintendo_title_id) is tried first: it's exact
    where names are fragile (renamed/localized library titles). An identifier
    miss is NOT terminal — a first import can predate the sync that attaches
    the id, so name matching may still save the item.

    fuzzy=False drops the token-sort fallback, keeping only exact/token name
    tiers — callers that would rather create a new row than risk a sequel-number
    near-miss ("BioShock 2" fuzzy-matching "BioShock") pass it.
    """
    if identifier_type is not None and identifier_value is not None:
        row = await get_game_by_identifier(identifier_type, str(identifier_value))
        if row is not None:
            return row, "identifier"

    if game_id is not None:
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (game_id,)
            )
        return row, ("id" if row is not None else None)

    # Try the raw purchase title first, then an edition/platform/upgrade-pack
    # -stripped form. A storefront title ("DAVE THE DIVER Nintendo Switch 2
    # Edition") carries suffixes no library row has, and token-AND matching
    # needs every query token present in the candidate — so the extra tokens
    # sink the match until peeled off. Raw goes first so a genuinely distinct
    # edition row (e.g. a separate "Remastered") still wins its exact match
    # before the stripped form would collapse it onto the base game.
    raw = name or ""
    queries = [raw]
    stripped = normalize_purchase_title(raw)
    if stripped and normalize_search_text(stripped) != normalize_search_text(raw):
        queries.append(stripped)

    for query in queries:
        match = build_name_match(query, column=NORMALIZED_NAME_SQL, use_fts=fts_ready())
        if not match.fuzzy_eligible:
            continue
        async with get_db() as db:
            row = await db.execute_fetchone(
                f"""SELECT g.id, g.name, {match.rank_sql} AS match_rank
                    FROM games g
                    WHERE {match.where_sql}
                    ORDER BY match_rank ASC, length(g.name) ASC, g.id ASC
                    LIMIT 1""",
                (*match.rank_params, *match.where_params),
            )
        if row is not None:
            return row, "name"

    for query in queries if fuzzy else []:
        fuzzy_ids = await fuzzy_fallback_game_ids(query)
        if fuzzy_ids:
            async with get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT id, name FROM games WHERE id = ?", (fuzzy_ids[0],)
                )
            if row is not None:
                return row, "fuzzy"
    return None, None


async def _apply_batch_item(
    item: dict,
    overwrite: bool,
    create_platform_rows: bool,
    create_missing: bool,
) -> dict:
    """Process one batch item into its per-item result dict. Never raises."""
    try:
        unknown = set(item) - _BATCH_ITEM_KEYS
        if unknown:
            raise ToolError(
                f"unknown key(s): {sorted(unknown)}. Valid: {sorted(_BATCH_ITEM_KEYS)}"
            )
        raw_platform = item.get("platform")
        if raw_platform is None:
            raise ToolError("platform is required")
        platform = _validate_platform(raw_platform, LIBRARY_PLATFORMS)
        if item.get("name") is None and item.get("game_id") is None:
            raise ToolError("Provide game_id or name")
        identifier_type = item.get("identifier_type")
        identifier_value = item.get("identifier_value")
        if (identifier_type is None) != (identifier_value is None):
            raise ToolError(
                "identifier_type and identifier_value must be provided together"
            )
        fields = _validated_fields(
            item.get("acquired_at"),
            item.get("price_paid"),
            item.get("price_currency"),
            item.get("purchase_source"),
            item.get("bundle_name"),
        )
        if not fields:
            raise ToolError("Provide at least one acquisition field")
    except ToolError as exc:
        return {
            "status": "error",
            "platform": item.get("platform"),
            "error": str(exc),
            "item": item,
        }

    row, match_type = await _match_batch_game(
        item.get("name"), item.get("game_id"), identifier_type, identifier_value
    )
    created = False
    if row is None:
        name = item.get("name")
        if not (create_missing and name):
            return {"status": "unmatched", "platform": platform, "item": item}
        # A purchase is a definitive ownership signal — stronger than the
        # playtime some platforms lean on to infer ownership — so a genuinely
        # new purchased title becomes an owned library game. The matcher
        # (identifier → edition-stripped name → fuzzy) has already missed, so
        # this is a real gap, not a near-duplicate.
        #
        # Create under the edition-STRIPPED title, not the raw storefront one:
        # an identifier-less import (eShop carries no title id) is reconciled by
        # a later ownership sync via NAME, and that sync prepares the clean title
        # ("Hollow Knight") — which can't adopt a row named "Hollow Knight –
        # Nintendo Switch 2 Edition-upgradepack", so the raw name would strand a
        # duplicate. Both forms already missed the library, so the clean name has
        # no base row to collide with. When present, the store identifier is
        # attached below as the stronger reconciliation key.
        create_name = normalize_purchase_title(str(name)).strip() or str(name).strip()
        new_id = await upsert_game(None, create_name)
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (new_id,)
            )
        created = True
        match_type = "created"
    resolved_id = row["id"]

    async with get_db() as db:
        gp = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (resolved_id, platform),
        )
        # A freshly created game has no platform row yet, and the purchase means
        # you own it here — so it always gets its owned row (the create_platform_rows
        # gate only governs games that already exist on some OTHER platform).
        if gp is None and not create_platform_rows and not created:
            platform_rows = await db.execute_fetchall(
                "SELECT platform FROM game_platforms WHERE game_id = ? ORDER BY platform",
                (resolved_id,),
            )
            return {
                "status": "no_platform_row",
                "game_id": resolved_id,
                "matched_name": row["name"],
                "match_type": match_type,
                "platform": platform,
                "platforms": [r["platform"] for r in platform_rows],
                "item": item,
            }
    gpid = gp["id"] if gp is not None else await upsert_game_platform(
        resolved_id, platform, owned=1
    )
    if gp is None and identifier_type is not None and match_type != "identifier":
        # A freshly created platform row would otherwise lose the store id the
        # item carried (an identifier match implies the id is already attached
        # to an existing row somewhere, so only non-identifier matches attach).
        await upsert_game_platform_identifier(
            gpid, identifier_type, str(identifier_value)
        )

    if overwrite:
        acquisition = await set_platform_acquisition(gpid, fields)
        status = "created" if created else "applied"
    else:
        # Pre-state read: only_if_null COALESCE writes can't tell us which
        # fields were actually filled, and "filled vs no_change" is the whole
        # point of importer mode.
        async with get_db() as db:
            pre = await db.execute_fetchone(
                f"SELECT {', '.join(ACQUISITION_FIELDS)} FROM game_platforms WHERE id = ?",
                (gpid,),
            )
        acquisition = await set_platform_acquisition(gpid, fields, only_if_null=True)
        newly_written = [col for col in fields if pre[col] is None]
        status = "created" if created else ("filled" if newly_written else "no_change")

    return {
        "status": status,
        "game_id": resolved_id,
        "matched_name": row["name"],
        "match_type": match_type,
        "platform": platform,
        "acquisition": acquisition,
    }


async def set_acquisitions_batch(
    items: list[dict],
    overwrite: bool = False,
    create_platform_rows: bool = False,
    create_missing: bool = False,
) -> dict:
    """
    Bulk-import acquisition data; per-item errors never fail the whole call.

    Each item: {name or game_id, platform, + any of the 5 acquisition fields,
    optionally identifier_type + identifier_value (both or neither) for
    identifier-first matching}.
    Default (overwrite=False) fills only NULL columns so a re-import never
    clobbers manual edits. With create_missing=True an item (name required)
    that matches no existing game — identifier, edition-stripped name, and
    fuzzy all miss — is created as an owned library game (status "created",
    store identifier attached); otherwise it lands in unmatched. Missing
    platform rows on an already-existing game land in no_platform_row unless
    create_platform_rows=True.
    """
    if not items:
        raise ToolError("items must not be empty")
    if len(items) > _BATCH_ITEM_CAP:
        raise ToolError(
            f"items is capped at {_BATCH_ITEM_CAP} per call (got {len(items)})"
        )

    results = []
    for item in items:
        results.append(
            await _apply_batch_item(
                item, overwrite, create_platform_rows, create_missing
            )
        )

    def _count(status: str) -> int:
        return sum(1 for r in results if r["status"] == status)

    return {
        "results": results,
        "total": len(items),
        "applied": _count("applied"),
        "filled": _count("filled"),
        "no_change": _count("no_change"),
        "created": _count("created"),
        # New owned games minted from purchases that matched nothing. Surfaced
        # by name so the caller can eyeball what was added — created games rows
        # have no delete tool, so a bad create wants to be visible.
        "created_details": [
            {
                "game_id": r["game_id"],
                "name": r["matched_name"],
                "platform": r["platform"],
            }
            for r in results
            if r["status"] == "created"
        ],
        "unmatched": [r["item"] for r in results if r["status"] == "unmatched"],
        "no_platform_row": _count("no_platform_row"),
        # Detail for the no_platform_row rows: which game matched but has no
        # platform row to write onto. Mirrors `unmatched` so a caller can triage
        # by name/id instead of an opaque count (import_purchases surfaces it).
        "no_platform_row_details": [
            {
                "game_id": r["game_id"],
                "matched_name": r["matched_name"],
                "match_type": r["match_type"],
                "platform": r["platform"],
                "platforms": r["platforms"],
            }
            for r in results
            if r["status"] == "no_platform_row"
        ],
        "errors": _count("error"),
    }


_BUNDLE_GAME_CAP = 50
_BUNDLE_GAME_KEYS = frozenset(
    {"name", "game_id", "identifier_type", "identifier_value", "price_paid"}
)


def _split_cents(remainder_cents: int, n: int) -> list[int]:
    """Split an integer cent amount into n parts summing exactly to it.

    The remainder cents (amount not evenly divisible) are handed to the first
    few parts, so the pieces differ by at most one cent and always re-sum to
    the original — a bundle total is never lost or invented to rounding.
    """
    base, extra = divmod(remainder_cents, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _bundle_game_price(item: dict) -> float | None:
    """Validate one bundle game's explicit price_paid (None if absent)."""
    price = item.get("price_paid")
    if price is None:
        return None
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        raise ToolError(f"price_paid must be a number (got {price!r})")
    if price < 0:
        raise ToolError("price_paid must not be negative")
    return float(price)


def _bundle_share_prices(
    games: list[dict], total_price: float | None
) -> tuple[list[float | None], float]:
    """Per-game prices + unallocated remainder for a bundle.

    Games carrying an explicit price_paid keep it; total_price (if given) is
    split evenly, in cents, across the games that don't — so a caller can pin
    a few known prices and let the rest share the leftover. Returns the
    aligned price list plus any remainder that had no game to land on (all
    games priced explicitly but their sum fell short of the total).
    """
    explicit = [_bundle_game_price(item) for item in games]
    if total_price is None:
        return explicit, 0.0

    total_cents = round(total_price * 100)
    explicit_cents = sum(round(p * 100) for p in explicit if p is not None)
    remainder_cents = total_cents - explicit_cents
    if remainder_cents < 0:
        raise ToolError(
            "explicit price_paid values exceed total_price "
            f"({explicit_cents / 100:.2f} > {total_price:.2f})"
        )

    unpriced_idx = [i for i, p in enumerate(explicit) if p is None]
    if not unpriced_idx:
        # Every game was priced explicitly; any shortfall can't be placed.
        return explicit, remainder_cents / 100

    shares = _split_cents(remainder_cents, len(unpriced_idx))
    prices = list(explicit)
    for idx, cents in zip(unpriced_idx, shares, strict=True):
        prices[idx] = cents / 100
    return prices, 0.0


async def split_bundle_acquisition(
    bundle_name: str,
    platform: str,
    games: list[dict],
    total_price: float | None = None,
    price_currency: str | None = None,
    acquired_at: str | None = None,
    purchase_source: str | None = None,
    create_missing: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Record one multi-game bundle purchase across its constituent games.

    A storefront bundle ("Portal: Companion Collection" = Portal + Portal 2)
    can't attach to a single library row. Provide the constituents the AI
    looked up and this splits the price across them, tagging each with the same
    bundle_name so get_spending_stats still groups the purchase.

    Each games[i] is {name or game_id, optional price_paid, optionally
    identifier_type + identifier_value together}. total_price is split evenly
    (to the cent, sum-preserving) across games without an explicit price_paid;
    games with an explicit price keep it and are excluded from the split.

    Games matched by identifier/id/name (edition-suffix stripping, no fuzzy —
    a near-miss is likelier a sequel than a typo) get an owned platform row
    created if missing, then the bundle acquisition written. Constituents that
    match nothing are created as new games when create_missing=True (name
    required) or reported as unmatched otherwise (their share lands in
    unallocated_price). overwrite=False (default) only fills NULL acquisition
    columns so a manual correction is never clobbered.

    dry_run=True resolves matches and computes the price split but writes
    nothing — statuses/prices show exactly what a real run would do. Constituent
    lists come from AI lookup and created games rows have no delete tool, so
    preview before any call that uses create_missing.
    """
    cleaned_bundle = bundle_name.strip()
    if not cleaned_bundle:
        raise ToolError("bundle_name must not be empty")
    platform = _validate_platform(platform, LIBRARY_PLATFORMS)
    if not games:
        raise ToolError("games must not be empty")
    if len(games) > _BUNDLE_GAME_CAP:
        raise ToolError(f"games is capped at {_BUNDLE_GAME_CAP} (got {len(games)})")

    # Structural validation up front: a bad item aborts the whole call before
    # any write, since the price split depends on every game being known.
    for item in games:
        unknown = set(item) - _BUNDLE_GAME_KEYS
        if unknown:
            raise ToolError(
                f"game has unknown key(s): {sorted(unknown)}. Valid: {sorted(_BUNDLE_GAME_KEYS)}"
            )
        if item.get("name") is None and item.get("game_id") is None:
            raise ToolError("each game needs a name or game_id")
        if (item.get("identifier_type") is None) != (item.get("identifier_value") is None):
            raise ToolError(
                "identifier_type and identifier_value must be provided together"
            )
        _bundle_game_price(item)

    # A currency is needed the moment any price will be written; borrow the
    # shared validator via a representative amount.
    _, currency = _validate_price(
        total_price if total_price is not None else 0.0, price_currency
    )
    validated_acquired_at = (
        _validate_acquired_at(str(acquired_at)) if acquired_at is not None else None
    )
    normalized_source = (
        _normalize_source(purchase_source) if purchase_source is not None else None
    )

    prices, unallocated = _bundle_share_prices(games, total_price)

    results = []
    for item, price in zip(games, prices, strict=True):
        results.append(
            await _apply_bundle_game(
                item,
                price,
                platform=platform,
                bundle_name=cleaned_bundle,
                currency=currency,
                acquired_at=validated_acquired_at,
                purchase_source=normalized_source,
                create_missing=create_missing,
                overwrite=overwrite,
                dry_run=dry_run,
            )
        )

    def _count(*statuses: str) -> int:
        return sum(1 for r in results if r["status"] in statuses)

    # Allocation is what actually landed on the rows (recorded_price), not the
    # proposed split: in fill-only mode a constituent that already had a price
    # keeps it, so the proposed share never persisted — counting it would claim
    # the bundle was fully recorded when it wasn't (rerun with overwrite=True).
    allocated = sum(
        r["recorded_price"]
        for r in results
        if r.get("recorded_price") is not None and r["status"] != "unmatched"
    )
    unallocated += sum(
        r["price_paid"]
        for r in results
        if r["price_paid"] is not None and r["status"] == "unmatched"
    )
    reconciled = total_price is None or abs(allocated + unallocated - total_price) < 0.01

    # Any price implies a currency was applied — a bare total_price check would
    # misreport explicit-per-game-price calls as currencyless.
    priced = any(p is not None for p in prices)

    return {
        "bundle_name": cleaned_bundle,
        "platform": platform,
        "dry_run": dry_run,
        "total_price": total_price,
        "price_currency": currency if priced else None,
        "games": results,
        # no_change rows had nothing written, so they don't count as recorded.
        "recorded": _count("applied", "filled", "created"),
        "created": _count("created"),
        "no_change": _count("no_change"),
        "unmatched": _count("unmatched"),
        "allocated_price": round(allocated, 2),
        "unallocated_price": round(unallocated, 2),
        "reconciled": reconciled,
    }


async def _apply_bundle_game(
    item: dict,
    price: float | None,
    *,
    platform: str,
    bundle_name: str,
    currency: str | None,
    acquired_at: str | None,
    purchase_source: str | None,
    create_missing: bool,
    overwrite: bool,
    dry_run: bool,
) -> dict:
    """Resolve (or create) one bundle constituent and write its acquisition.

    dry_run resolves and computes the exact same status a real run would
    return (including the filled-vs-no_change pre-read) but performs no write;
    its acquisition echo is the proposed field dict rather than row state.
    """
    fields: dict = {"bundle_name": bundle_name}
    if price is not None:
        fields["price_paid"] = price
        fields["price_currency"] = currency
    if acquired_at is not None:
        fields["acquired_at"] = acquired_at
    if purchase_source is not None:
        fields["purchase_source"] = purchase_source

    name = item.get("name")
    row, match_type = await _match_batch_game(
        name,
        item.get("game_id"),
        item.get("identifier_type"),
        item.get("identifier_value"),
        # A bundle constituent is a precise, AI-supplied title; a fuzzy near-miss
        # is likelier a distinct sequel than a typo, so match exactly or create.
        fuzzy=False,
    )

    created = False
    if row is None:
        if not (create_missing and name):
            return {
                "status": "unmatched",
                "name": name,
                "price_paid": price,
                # Nothing is persisted for an unmatched constituent — its share
                # is reported as unallocated, not allocated.
                "recorded_price": None,
                "item": item,
            }
        if dry_run:
            return {
                "status": "created",
                "game_id": None,
                "matched_name": str(name).strip(),
                "match_type": "created",
                "price_paid": price,
                "recorded_price": price,  # fresh row: the proposed price persists
                "acquisition": fields,
            }
        game_id = await upsert_game(None, str(name).strip())
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (game_id,)
            )
        created = True
        match_type = "created"

    resolved_id = row["id"]
    async with get_db() as db:
        gp = await db.execute_fetchone(
            "SELECT id FROM game_platforms WHERE game_id = ? AND platform = ?",
            (resolved_id, platform),
        )

    if dry_run:
        # recorded_price is what WOULD persist: fill-only leaves an existing
        # price untouched, so the proposed share is not what lands there.
        if overwrite:
            status = "applied"
            recorded_price = price
        elif gp is None:
            status = "filled"  # fresh row: every column is NULL
            recorded_price = price
        else:
            async with get_db() as db:
                pre = await db.execute_fetchone(
                    f"SELECT {', '.join(ACQUISITION_FIELDS)} FROM game_platforms WHERE id = ?",
                    (gp["id"],),
                )
            newly_written = [col for col in fields if pre[col] is None]
            status = "filled" if newly_written else "no_change"
            recorded_price = pre["price_paid"] if pre["price_paid"] is not None else price
        return {
            "status": status,
            "game_id": resolved_id,
            "matched_name": row["name"],
            "match_type": match_type,
            "price_paid": price,
            "recorded_price": recorded_price,
            "acquisition": fields,
        }

    # A bundle purchase means you now own each game on this platform, so the
    # platform row is created unconditionally (unlike set_acquisitions_batch).
    gpid = gp["id"] if gp is not None else await upsert_game_platform(
        resolved_id, platform, owned=1
    )
    identifier_type = item.get("identifier_type")
    if gp is None and identifier_type is not None and match_type not in ("identifier",):
        await upsert_game_platform_identifier(
            gpid, identifier_type, str(item.get("identifier_value"))
        )
    await clear_fulfilled_wishlist_entries(game_id=resolved_id, platform=platform)

    if overwrite:
        acquisition = await set_platform_acquisition(gpid, fields)
        status = "created" if created else "applied"
    else:
        async with get_db() as db:
            pre = await db.execute_fetchone(
                f"SELECT {', '.join(ACQUISITION_FIELDS)} FROM game_platforms WHERE id = ?",
                (gpid,),
            )
        acquisition = await set_platform_acquisition(gpid, fields, only_if_null=True)
        newly_written = [col for col in fields if pre[col] is None]
        status = "created" if created else ("filled" if newly_written else "no_change")

    return {
        "status": status,
        "game_id": resolved_id,
        "matched_name": row["name"],
        "match_type": match_type,
        "price_paid": price,
        # The price actually on the row now (fill-only may have preserved an
        # older one) — this is what get_spending_stats attributes to the bundle.
        "recorded_price": acquisition["price_paid"],
        "acquisition": acquisition,
    }


# How many proposed items a dry_run echoes back per source before truncating.
_DRY_RUN_ECHO_CAP = 200


def _resolve_purchase_fetchers(sources: list[str]) -> dict:
    """{source: fetch coroutine} for the selected importer sources.

    Prefers a name bound on THIS module (the imports at the top), so tests
    patching gamelib_mcp.tools.acquisition.fetch_eshop_purchases keep
    intercepting the fetch; falls back to importing the registry module.
    """
    namespace = sys.modules[__name__]
    fetchers = {}
    for source in sources:
        module_path, attr = PURCHASE_IMPORTERS[source]
        fn = getattr(namespace, attr, None)
        if fn is None:
            fn = getattr(importlib.import_module(module_path), attr)
        fetchers[source] = fn
    return fetchers


def _record_to_batch_item(record: PurchaseRecord, source: str) -> dict:
    """One PurchaseRecord → a set_acquisitions_batch item dict (no None fields)."""
    item: dict = {
        "name": record.title,
        "platform": record.platform,
        "purchase_source": record.purchase_source,
    }
    if record.acquired_at is not None:
        item["acquired_at"] = record.acquired_at
    if record.price_paid is not None:
        item["price_paid"] = record.price_paid
        # A currency without a price fails validation, so it only rides along
        # when the record actually carries a price.
        if record.price_currency is not None:
            item["price_currency"] = record.price_currency
    if record.bundle_name is not None:
        item["bundle_name"] = record.bundle_name
    if record.store_identifier is not None and source in IDENTIFIER_TYPES:
        # Lets the batch writer match identifier-first, so a renamed or
        # localized library title still resolves to the right game.
        item["identifier_type"] = IDENTIFIER_TYPES[source]
        item["identifier_value"] = record.store_identifier
    return item


async def _record_to_bundle_entry(record: PurchaseRecord) -> dict:
    """One bundle PurchaseRecord → a split_bundle_acquisition-shaped hand-off.

    Keys mirror the tool's parameters so a caller can look up the constituents
    and forward the rest verbatim (the record's title IS the bundle name).
    already_recorded flags bundles a previous split already wrote (the fetch
    can't know — it re-surfaces every bundle on every import forever), so a
    repeat import doesn't re-ask for the same lookup.
    """
    async with get_db() as db:
        existing = await db.execute_fetchone(
            """SELECT COUNT(*) AS c FROM game_platforms
               WHERE bundle_name = ? AND platform = ?""",
            (record.title, record.platform),
        )
    return {
        "bundle_name": record.title,
        "platform": record.platform,
        "total_price": record.price_paid,
        "price_currency": record.price_currency,
        "acquired_at": record.acquired_at,
        "purchase_source": record.purchase_source,
        "already_recorded": existing["c"] > 0,
    }


async def _import_one_source(
    source: str,
    fetch,
    dry_run: bool,
    overwrite: bool,
    create_platform_rows: bool,
    create_missing: bool,
) -> dict:
    """Fetch one source and (unless dry_run) push its records through the
    batch writer. Fetch exceptions propagate — the caller gathers them, and a
    mid-fetch failure must never partially import."""
    records, skipped = await fetch()
    # Multi-game bundles can't attach to a single row — divert them to a
    # dedicated bucket (with price/date) for split_bundle_acquisition instead
    # of feeding them to the single-game matcher, where they'd only ever miss.
    bundles = [await _record_to_bundle_entry(r) for r in records if r.is_bundle]
    importable = [r for r in records if not r.is_bundle]
    items = [_record_to_batch_item(r, source) for r in importable]

    if dry_run:
        # Run the matcher (not the writer) so the preview can name the genuinely
        # new games create_missing would mint — the "no delete tool" safety net.
        would_create: list[dict] = []
        if create_missing:
            for item in items:
                row, _ = await _match_batch_game(
                    item.get("name"),
                    item.get("game_id"),
                    item.get("identifier_type"),
                    item.get("identifier_value"),
                )
                if row is None and item.get("name"):
                    would_create.append(item)
        return {
            "source": source,
            "status": "ok",
            "dry_run": True,
            "fetched": len(records),
            "proposed": items[:_DRY_RUN_ECHO_CAP],
            "truncated": len(items) > _DRY_RUN_ECHO_CAP,
            "would_create": would_create[:_DRY_RUN_ECHO_CAP],
            "bundles_needing_split": bundles,
            "skipped": skipped,
        }

    applied = filled = no_change = created = no_platform_row = errors = 0
    unmatched: list[dict] = []
    created_details: list[dict] = []
    no_platform_row_details: list[dict] = []
    for start in range(0, len(items), _BATCH_ITEM_CAP):
        batch = await set_acquisitions_batch(
            items[start : start + _BATCH_ITEM_CAP],
            overwrite=overwrite,
            create_platform_rows=create_platform_rows,
            create_missing=create_missing,
        )
        applied += batch["applied"]
        filled += batch["filled"]
        no_change += batch["no_change"]
        created += batch["created"]
        no_platform_row += batch["no_platform_row"]
        errors += batch["errors"]
        unmatched.extend(batch["unmatched"])
        created_details.extend(batch["created_details"])
        no_platform_row_details.extend(batch["no_platform_row_details"])

    return {
        "source": source,
        "status": "ok",
        "fetched": len(records),
        "applied": applied,
        "filled": filled,
        "no_change": no_change,
        "created": created,
        "created_details": created_details,
        "unmatched": unmatched,
        "no_platform_row": no_platform_row,
        "no_platform_row_details": no_platform_row_details,
        "bundles_needing_split": bundles,
        "errors": errors,
        "skipped": skipped,
    }


async def import_purchases(
    sources: list[str] | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
    create_platform_rows: bool = False,
    create_missing: bool = True,
) -> dict:
    """
    Fetch purchase histories from registered storefront importers and record
    them through set_acquisitions_batch.

    sources None = every registered importer. Sources run concurrently; a
    fetch failure yields {status: "error"} for that source (nothing written
    for it — a partial fetch must not partially import) while the others
    proceed. dry_run previews the converted batch items (and, under
    create_missing, the would_create list) without writing.

    A purchase is a definitive ownership signal, so create_missing defaults
    True: a single-game purchase that matches no existing game is created as an
    owned library game (reported under each source's created/created_details).
    Set it False to route those to unmatched instead. Multi-game bundles are
    always diverted to each source's bundles_needing_split list (name, platform,
    total_price, date) rather than the single-game matcher — feed each to
    split_bundle_acquisition with its looked-up games.
    """
    if sources is None:
        selected = sorted(PURCHASE_IMPORTERS)
    else:
        unknown = [s for s in sources if s not in PURCHASE_IMPORTERS]
        if unknown:
            raise ToolError(
                f"Unknown purchase source(s): {unknown}. "
                f"Valid: {sorted(PURCHASE_IMPORTERS)}"
            )
        selected = list(dict.fromkeys(sources))
    if not selected:
        raise ToolError("sources must not be empty")

    fetchers = _resolve_purchase_fetchers(selected)
    outcomes = await asyncio.gather(
        *(
            _import_one_source(
                source,
                fetchers[source],
                dry_run,
                overwrite,
                create_platform_rows,
                create_missing,
            )
            for source in selected
        ),
        return_exceptions=True,
    )

    results: dict[str, dict] = {}
    for source, outcome in zip(selected, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            results[source] = {"source": source, "status": "error", "error": str(outcome)}
        else:
            results[source] = outcome

    def _total(key: str) -> int:
        return sum(r.get(key, 0) for r in results.values())

    totals = {
        "fetched": _total("fetched"),
        "applied": _total("applied"),
        "filled": _total("filled"),
        "no_change": _total("no_change"),
        "created": _total("created"),
        "unmatched": sum(len(r.get("unmatched", [])) for r in results.values()),
        "bundles_needing_split": sum(
            len(r.get("bundles_needing_split", [])) for r in results.values()
        ),
        # Per-item validation errors plus whole-source fetch failures.
        "errors": _total("errors")
        + sum(1 for r in results.values() if r["status"] == "error"),
    }
    return {"sources": results, "dry_run": dry_run, "totals": totals}


def _rounded(value) -> float | None:
    return None if value is None else round(value, 2)


async def get_spending_stats(
    year: int | None = None,
    platform: str | None = None,
    purchase_source: str | None = None,
) -> dict:
    """
    Aggregate spending over recorded acquisitions (owned rows only).

    Monetary aggregates group by currency and are never summed across
    currencies. Deliberately NOT filtered on is_primary_library_item —
    money spent on DLC/editions is still money spent.
    """
    where = ["gp.owned = 1"]
    params: list = []
    if platform is not None:
        where.append("gp.platform = ?")
        params.append(_validate_platform(platform, LIBRARY_PLATFORMS))
    if purchase_source is not None:
        where.append("gp.purchase_source = ?")
        params.append(_normalize_source(purchase_source))
    if year is not None:
        # substr(NULL,1,4) is NULL, so this also excludes rows with no
        # acquired_at — an undated purchase can't be attributed to a year.
        where.append("substr(gp.acquired_at, 1, 4) = ?")
        params.append(str(year))

    base = f"""FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               WHERE {' AND '.join(where)}"""
    priced = f"{base} AND gp.price_paid IS NOT NULL"

    async with get_db() as db:
        summary = await db.execute_fetchone(
            f"""SELECT COUNT(*) AS owned_rows,
                       SUM(CASE WHEN gp.price_paid IS NOT NULL THEN 1 ELSE 0 END) AS priced_rows,
                       SUM(CASE WHEN gp.price_paid = 0 THEN 1 ELSE 0 END) AS zero_cost_rows
                {base}""",
            params,
        )

        totals = await db.execute_fetchall(
            f"""SELECT gp.price_currency AS currency,
                       ROUND(SUM(gp.price_paid), 2) AS total_spent,
                       COUNT(*) AS priced_rows
                {priced}
                GROUP BY gp.price_currency
                ORDER BY total_spent DESC""",
            params,
        )

        by_year = await db.execute_fetchall(
            f"""SELECT substr(gp.acquired_at, 1, 4) AS year,
                       gp.price_currency AS currency,
                       ROUND(SUM(gp.price_paid), 2) AS spent,
                       COUNT(*) AS count
                {priced} AND gp.acquired_at IS NOT NULL
                GROUP BY year, gp.price_currency
                ORDER BY year DESC, spent DESC""",
            params,
        )

        by_source = await db.execute_fetchall(
            f"""SELECT gp.purchase_source, gp.price_currency AS currency,
                       ROUND(SUM(gp.price_paid), 2) AS spent,
                       COUNT(*) AS count
                {priced} AND gp.purchase_source IS NOT NULL
                GROUP BY gp.purchase_source, gp.price_currency
                ORDER BY spent DESC""",
            params,
        )

        by_platform = await db.execute_fetchall(
            f"""SELECT gp.platform, gp.price_currency AS currency,
                       ROUND(SUM(gp.price_paid), 2) AS spent,
                       COUNT(*) AS count
                {priced}
                GROUP BY gp.platform, gp.price_currency
                ORDER BY spent DESC""",
            params,
        )

        by_bundle = await db.execute_fetchall(
            f"""SELECT gp.bundle_name, gp.price_currency AS currency,
                       ROUND(SUM(gp.price_paid), 2) AS spent,
                       COUNT(*) AS count
                {priced} AND gp.bundle_name IS NOT NULL
                GROUP BY gp.bundle_name, gp.price_currency
                ORDER BY spent DESC""",
            params,
        )

        top_expensive = await db.execute_fetchall(
            f"""SELECT g.id AS game_id, g.name, gp.platform,
                       gp.price_paid, gp.price_currency AS currency,
                       gp.acquired_at, gp.purchase_source, gp.bundle_name
                {priced}
                ORDER BY gp.price_paid DESC
                LIMIT 10""",
            params,
        )

        played_priced = f"{priced} AND gp.playtime_minutes > 0"
        cph_overall = await db.execute_fetchall(
            f"""SELECT gp.price_currency AS currency,
                       SUM(gp.price_paid) AS total_spent,
                       SUM(gp.playtime_minutes) / 60.0 AS total_hours
                {played_priced}
                GROUP BY gp.price_currency
                ORDER BY total_spent DESC""",
            params,
        )

        value_select = f"""SELECT g.id AS game_id, g.name, gp.platform,
                       gp.price_paid, gp.price_currency AS currency,
                       ROUND(gp.playtime_minutes / 60.0, 1) AS playtime_hours,
                       ROUND(gp.price_paid / (gp.playtime_minutes / 60.0), 2)
                           AS cost_per_hour
                {played_priced}"""
        best_value = await db.execute_fetchall(
            f"{value_select} ORDER BY cost_per_hour ASC, gp.playtime_minutes DESC LIMIT 10",
            params,
        )
        worst_value = await db.execute_fetchall(
            f"{value_select} AND gp.price_paid > 0 ORDER BY cost_per_hour DESC LIMIT 10",
            params,
        )

        unpriced_playtime = await db.execute_fetchone(
            f"""SELECT COUNT(*) AS count
                {base} AND gp.playtime_minutes > 0 AND gp.price_paid IS NULL""",
            params,
        )

        unplayed = f"""{priced} AND gp.price_paid > 0
                AND (gp.playtime_minutes IS NULL OR gp.playtime_minutes = 0)"""
        unplayed_totals = await db.execute_fetchall(
            f"""SELECT gp.price_currency AS currency,
                       ROUND(SUM(gp.price_paid), 2) AS spent,
                       COUNT(*) AS count
                {unplayed}
                GROUP BY gp.price_currency
                ORDER BY spent DESC""",
            params,
        )
        unplayed_top = await db.execute_fetchall(
            f"""SELECT g.id AS game_id, g.name, gp.platform,
                       gp.price_paid, gp.price_currency AS currency
                {unplayed}
                ORDER BY gp.price_paid DESC
                LIMIT 10""",
            params,
        )

    owned_rows = summary["owned_rows"]
    priced_rows = summary["priced_rows"] or 0
    return {
        "owned_rows": owned_rows,
        "priced_rows": priced_rows,
        "coverage_pct": round(priced_rows / owned_rows * 100, 1) if owned_rows else 0.0,
        "zero_cost_rows": summary["zero_cost_rows"] or 0,
        "totals": [dict(r) for r in totals],
        "by_year": [dict(r) for r in by_year],
        "by_source": [dict(r) for r in by_source],
        "by_platform": [dict(r) for r in by_platform],
        "by_bundle": [dict(r) for r in by_bundle],
        "top_expensive": [dict(r) for r in top_expensive],
        "cost_per_hour": {
            "overall": [
                {
                    "currency": r["currency"],
                    "total_spent": _rounded(r["total_spent"]),
                    "total_hours": _rounded(r["total_hours"]),
                    "cost_per_hour": _rounded(r["total_spent"] / r["total_hours"]),
                }
                for r in cph_overall
            ],
            "best_value": [dict(r) for r in best_value],
            "worst_value": [dict(r) for r in worst_value],
            "unpriced_playtime_rows": unpriced_playtime["count"],
            "unplayed_spend": {
                "totals": [dict(r) for r in unplayed_totals],
                "top": [dict(r) for r in unplayed_top],
            },
        },
    }
