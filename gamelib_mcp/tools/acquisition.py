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

from ..data.content import (
    NESTED_CONTENT_TYPES,
    PRIMARY_CONTENT_TYPES,
    ContentClassification,
    derive_is_primary,
    parent_name_candidates,
)
from ..data.db import (
    ACQUISITION_FIELDS,
    apply_content_classification,
    clear_fulfilled_wishlist_entries,
    fts_ready,
    get_db,
    get_game_by_identifier,
    resolve_parent_game,
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
from ..data.purchases.epic_orders import fetch_epic_purchases  # noqa: F401
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
# "key_reseller" covers third-party key shops (GAMIVO, Kinguin, G2A, Green Man
# Gaming, IndieGala, CDKeys, …) — a real acquisition channel that would
# otherwise collapse into the unanalysable "other" bucket; per-vendor aliases
# below map onto it so provenance survives normalization.
PURCHASE_SOURCES = frozenset({
    "steam", "gog", "epic", "eshop", "psn", "xbox",
    "humble", "fanatical", "itchio", "ea", "ubisoft",
    "key_reseller",
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
    "gamivo": "key_reseller",
    "kinguin": "key_reseller",
    "g2a": "key_reseller",
    "green man gaming": "key_reseller",
    "greenmangaming": "key_reseller",
    "gmg": "key_reseller",
    "indiegala": "key_reseller",
    "cdkeys": "key_reseller",
    "eneba": "key_reseller",
    "instant gaming": "key_reseller",
}

# YYYY, YYYY-MM, or YYYY-MM-DD; calendar validity is checked separately below.
_ACQUIRED_AT_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_BATCH_ITEM_CAP = 200
_BATCH_ITEM_KEYS = frozenset({
    "name", "game_id", "platform", "identifier_type", "identifier_value",
    "content_type",
    *ACQUISITION_FIELDS,
})

# Every valid content_type an item may carry (primary + nested vocabularies).
_VALID_CONTENT_TYPES = PRIMARY_CONTENT_TYPES | NESTED_CONTENT_TYPES


def _validate_content_type(value) -> str | None:
    """Validate an item's optional content_type. None = no signal (allowed)."""
    if value is None:
        return None
    if value not in _VALID_CONTENT_TYPES:
        raise ToolError(
            f"unknown content_type '{value}'. Valid: {sorted(_VALID_CONTENT_TYPES)}"
        )
    return value


async def _addon_mint_fields(
    create_name: str,
    content_type: str | None,
    *,
    exclude_game_id: int | None = None,
) -> tuple[dict, int | None, str | None]:
    """Extra upsert_game fields for a create terminal, DLC-aware.

    For a NESTED content_type (dlc/expansion/edition/…) the new row is minted
    nested: content_type set, is_primary_library_item=0, and — when a
    parent_name_candidates candidate resolves to an EXISTING PRIMARY games row —
    its parent_game_id linked. Candidates run longest-first (suffix-stripped
    forms included), so "Deus Ex: Mankind Divided Season Pass" parents under
    "Deus Ex: Mankind Divided" rather than the first franchise entry a bare
    colon-split would reach. A candidate resolving to a nested row is skipped
    and the next (shorter) candidate tried: "Game: Expansion: Soundtrack" must
    parent under "Game", not under the "Game: Expansion" addon (update_game
    rejects such chains, and nothing walks them). A parent is NEVER minted from
    a title guess (resolve_parent_game(create=False)); an unresolved parent
    leaves the addon parentless. A primary/None content_type returns no extra
    fields, so the row mints as a base_game default exactly as before. Returns
    (fields, parent_game_id, parent_name); parent_* are None when unresolved.
    """
    if content_type is None or content_type not in NESTED_CONTENT_TYPES:
        return {}, None, None
    fields: dict = {
        "content_type": content_type,
        "is_primary_library_item": int(derive_is_primary(content_type)),
    }
    for candidate in parent_name_candidates(create_name):
        parent_id = await resolve_parent_game(
            candidate, create=False, exclude_game_id=exclude_game_id
        )
        if parent_id is None:
            continue
        async with get_db() as db:
            prow = await db.execute_fetchone(
                "SELECT name, is_primary_library_item FROM games WHERE id = ?",
                (parent_id,),
            )
        if prow is None or not prow["is_primary_library_item"]:
            continue
        fields["parent_game_id"] = parent_id
        return fields, parent_id, prow["name"]
    return fields, None, None


async def _reclassify_matched_nested(
    game_id: int, item_name: str | None, content_type: str | None
) -> tuple[bool, int | None, str | None]:
    """Apply a nested importer hint to a MATCHED row still at the default.

    ADR 0002's precedence chain places the importer hint above the default: a
    DLC purchase exact-matching a row that is still base_game/primary (a
    pre-classification phantom mint, or a manual seed) records its spend but
    would otherwise keep inflating game counts forever. A row that is already
    nested is left entirely alone — a split-title guess must never second-guess
    an existing classification or clobber a curated parent link — and
    apply_content_classification's guards (manual overrides, default-clobber,
    self-parent, compare-and-swap) cover the rest. Returns
    (reclassified, parent_game_id, parent_name).
    """
    if content_type is None or content_type not in NESTED_CONTENT_TYPES:
        return False, None, None
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT name, is_primary_library_item FROM games WHERE id = ?",
            (game_id,),
        )
    if row is None or not row["is_primary_library_item"]:
        return False, None, None
    guess_source = (item_name or row["name"] or "").strip()
    _, parent_id, parent_name = await _addon_mint_fields(
        guess_source, content_type, exclude_game_id=game_id
    )
    applied = await apply_content_classification(
        game_id,
        ContentClassification(content_type=content_type, is_primary_library_item=False),
        source="purchase_import",
        parent_game_id=parent_id,
    )
    if not applied:
        return False, None, None
    return True, parent_id, parent_name


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
    exact_only: bool = False,
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

    exact_only restricts the name tiers to an EXACT normalized match (rank 0 of
    build_name_match) — no prefix, substring, token-AND, or fuzzy — and is set
    for items carrying a NESTED content_type (DLC/expansion/edition). Rationale:
    a DLC title that token- or substring-matched its base game ("Hollow Knight:
    Silksong Pack" onto "Hollow Knight") would attach the DLC's spend onto the
    base row, corrupting both the base game's price data and its content
    classification. Identifier and explicit game_id tiers stay safe under
    exact_only and are unaffected.
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
    # The stripped tier is for reconciling BASE-GAME purchases whose storefront
    # titles carry suffixes. Nested items must never use it: stripping an
    # edition/upgrade suffix yields the base game's own name ("Hades: Deluxe
    # Edition" -> "Hades"), which exact-matches the base row at rank 0 and would
    # attach the nested item's spend to it — the exact corruption exact_only
    # exists to prevent.
    if (
        not exact_only
        and stripped
        and normalize_search_text(stripped) != normalize_search_text(raw)
    ):
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
            # Nested items accept only rank-0 (exact normalized) matches; a
            # broader tier here would risk collapsing a DLC onto its base game.
            if exact_only and row["match_rank"] != 0:
                continue
            return row, "name"

    for query in queries if (fuzzy and not exact_only) else []:
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
    dry_run: bool = False,
) -> dict:
    """Process one batch item into its per-item result dict. Never raises.

    ``dry_run=True`` runs the SAME validation and matching path but skips every
    write, returning the status the wet run would produce — so a preview's
    counters are trustworthy instead of a separate approximation. Two
    documented divergences: statuses are computed against the CURRENT database
    (several lines targeting the same row each report "filled", where a wet run
    would fill once and then report "no_change"), and the reclassify-on-match
    repair for nested hints is not simulated (it never changes the status).
    """
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
        content_type = _validate_content_type(item.get("content_type"))
    except ToolError as exc:
        return {
            "status": "error",
            "platform": item.get("platform"),
            "error": str(exc),
            "item": item,
        }

    # A nested item (DLC/expansion/edition) must not ride the broad name tiers
    # that could attach its spend onto the base game — restrict to exact matches.
    nested = content_type is not None and content_type in NESTED_CONTENT_TYPES
    row, match_type = await _match_batch_game(
        item.get("name"),
        item.get("game_id"),
        identifier_type,
        identifier_value,
        exact_only=nested,
    )
    created = False
    mint_content_type: str | None = None
    mint_parent_id: int | None = None
    mint_parent_name: str | None = None
    if row is None:
        name = item.get("name")
        if not (create_missing and name):
            return {"status": "unmatched", "platform": platform, "item": item}
        # A purchase is a definitive ownership signal — stronger than the
        # playtime some platforms lean on to infer ownership — so a genuinely
        # new purchased title becomes an owned library game. The matcher
        # (identifier → edition-stripped name → fuzzy) has already missed, so
        # this is a real gap, not a near-duplicate.
        if nested:
            # A nested row's identity IS its full storefront title. Stripping an
            # edition/upgrade suffix can collapse the name onto the base game's
            # own ("Hades: Deluxe Edition" -> "Hades"), and upsert_game's name
            # adoption would then DEMOTE the real base game to nested content
            # instead of creating a child row. So: mint under the raw title,
            # never adopt an existing row by name, and resolve the parent from
            # the raw title too (stripping can delete the very separator the
            # parent guess needs).
            create_name = str(name).strip()
        else:
            # Create under the edition-STRIPPED title, not the raw storefront
            # one: an identifier-less import (eShop carries no title id) is
            # reconciled by a later ownership sync via NAME, and that sync
            # prepares the clean title ("Hollow Knight") — which can't adopt a
            # row named "Hollow Knight – Nintendo Switch 2 Edition-upgradepack",
            # so the raw name would strand a duplicate. Both forms already
            # missed the library, so the clean name has no base row to collide
            # with. When present, the store identifier is attached below as the
            # stronger reconciliation key.
            create_name = (
                normalize_purchase_title(str(name)).strip() or str(name).strip()
            )
        # A nested content_type mints a nested row (content_type + is_primary=0)
        # linked to an existing parent when a parent candidate resolves; a
        # primary/None content_type mints a base_game default (unchanged).
        mint_fields, mint_parent_id, mint_parent_name = await _addon_mint_fields(
            create_name, content_type
        )
        if mint_fields:
            mint_content_type = content_type
        if dry_run:
            result: dict = {
                "status": "created",
                "game_id": None,
                "matched_name": create_name,
                "match_type": "created",
                "platform": platform,
                "acquisition": fields,
            }
            if mint_content_type is not None:
                result["content_type"] = mint_content_type
                if mint_parent_id is not None:
                    result["parent_game_id"] = mint_parent_id
                    result["parent_name"] = mint_parent_name
            return result
        new_id = await upsert_game(
            None, create_name, match_existing_by_name=not nested, **mint_fields
        )
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (new_id,)
            )
        created = True
        match_type = "created"
    resolved_id = row["id"]

    reclassified = False
    if nested and not created and not dry_run:
        # The exact match can land on a row still classified base_game/primary
        # (a phantom minted before classification existed, or a manual seed) —
        # the importer hint reclassifies it so the spend doesn't land on a row
        # that keeps inflating game counts. Guarded; already-nested rows and
        # pinned/classified rows are untouched.
        reclassified, mint_parent_id, mint_parent_name = (
            await _reclassify_matched_nested(
                resolved_id, item.get("name"), content_type
            )
        )
        if reclassified:
            mint_content_type = content_type

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

    # Family-conflict guard: a FUZZY match about to create a platform row must
    # not do so when the row's content family (parent/children/siblings)
    # already owns that platform — "Fallout New Vegas Ultimate ROW" fuzzy-
    # matching the edition child would otherwise mint a SECOND steam row right
    # next to the base game's real one, splitting spend and ownership across
    # the family. Exact/identifier matches are unaffected.
    if gp is None and match_type == "fuzzy":
        async with get_db() as db:
            family_owner = await db.execute_fetchone(
                """SELECT g.id, g.name
                   FROM games g
                   JOIN game_platforms gp2 ON gp2.game_id = g.id
                        AND gp2.platform = ? AND gp2.owned = 1
                   JOIN games matched ON matched.id = ?
                   WHERE g.id != matched.id
                     AND (g.id = matched.parent_game_id
                          OR g.parent_game_id = matched.id
                          OR (g.parent_game_id IS NOT NULL
                              AND g.parent_game_id = matched.parent_game_id))
                   LIMIT 1""",
                (platform, resolved_id),
            )
        if family_owner is not None:
            return {
                "status": "family_conflict",
                "game_id": resolved_id,
                "matched_name": row["name"],
                "match_type": match_type,
                "platform": platform,
                "conflicting_game_id": family_owner["id"],
                "conflicting_name": family_owner["name"],
                "item": item,
            }

    if dry_run:
        # Same status derivation as the wet branches below, computed from the
        # current pre-state instead of a write. A missing platform row means
        # every acquisition column is fresh (a wet run would create the row).
        if gp is None:
            newly_written = list(fields)
        else:
            async with get_db() as db:
                pre = await db.execute_fetchone(
                    f"SELECT {', '.join(ACQUISITION_FIELDS)} FROM game_platforms WHERE id = ?",
                    (gp["id"],),
                )
            newly_written = [col for col in fields if pre[col] is None]
        acquisition = dict(fields)
        if overwrite:
            status = "applied"
        else:
            status = "filled" if newly_written else "no_change"
        return {
            "status": status,
            "game_id": resolved_id,
            "matched_name": row["name"],
            "match_type": match_type,
            "platform": platform,
            "acquisition": acquisition,
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

    result = {
        "status": status,
        "game_id": resolved_id,
        "matched_name": row["name"],
        "match_type": match_type,
        "platform": platform,
        "acquisition": acquisition,
    }
    # A DLC/expansion/edition minted OR reclassified here carries its
    # content_type (and parent, when linked) so the import surfaces what was
    # created/repaired and its family tie.
    if (created or reclassified) and mint_content_type is not None:
        result["content_type"] = mint_content_type
        if mint_parent_id is not None:
            result["parent_game_id"] = mint_parent_id
            result["parent_name"] = mint_parent_name
    if reclassified:
        result["reclassified"] = True
    return result


async def set_acquisitions_batch(
    items: list[dict],
    overwrite: bool = False,
    create_platform_rows: bool = False,
    create_missing: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Bulk-import acquisition data; per-item errors never fail the whole call.

    dry_run=True previews without writing: every item runs the SAME validation
    and matching path and returns the same statuses/counters a wet run would
    (unmatched, created, filled, no_change, …), so a preview is a faithful
    audit of what the wet run will do — not a separate approximation. Created
    games report game_id null. Statuses are computed against the current
    database, so several lines targeting the same row each report "filled"
    where a wet run would fill once and then report "no_change".

    Each item: {name or game_id, platform, + any of the 5 acquisition fields,
    optionally identifier_type + identifier_value (both or neither) for
    identifier-first matching, optionally content_type (a primary or nested
    vocabulary value, e.g. "dlc")}.
    Default (overwrite=False) fills only NULL columns so a re-import never
    clobbers manual edits. An item carrying a NESTED content_type (dlc/
    expansion/edition/…) matches by identifier, game_id, or EXACT name only —
    never the prefix/substring/token/fuzzy tiers — so a DLC's spend can't
    collapse onto its base game; when such a match lands on a row still at the
    default base_game classification (a phantom minted before classification
    existed), the hint reclassifies it nested with a resolved parent (result
    carries reclassified=true) — manual overrides, already-classified, and
    already-nested rows are never touched. With create_missing=True an item (name
    required) that matches no existing game is created as an owned library game
    (status "created", store identifier attached); a nested content_type mints
    it nested (is_primary_library_item=0) linked to an existing parent resolved
    from the title when possible (created_details then carries content_type and
    parent_game_id/parent_name). Missing platform rows on an already-existing
    game land in no_platform_row unless create_platform_rows=True.
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
                item, overwrite, create_platform_rows, create_missing, dry_run
            )
        )

    def _count(status: str) -> int:
        return sum(1 for r in results if r["status"] == status)

    return {
        "results": results,
        "total": len(items),
        "dry_run": dry_run,
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
                # DLC-aware minting surfaces the minted content_type and, when a
                # parent was linked, its id/name so an import shows the family tie.
                **({"content_type": r["content_type"]} if r.get("content_type") else {}),
                **(
                    {
                        "parent_game_id": r["parent_game_id"],
                        "parent_name": r["parent_name"],
                    }
                    if r.get("parent_game_id")
                    else {}
                ),
            }
            for r in results
            if r["status"] == "created"
        ],
        "unmatched": [r["item"] for r in results if r["status"] == "unmatched"],
        # Fuzzy matches refused because the matched row's content family
        # already owns the platform — writing would fork a second platform row
        # inside the family (see _apply_batch_item's guard). Resolve manually:
        # the conflicting_* fields name the row that already owns it.
        "family_conflict": _count("family_conflict"),
        "family_conflict_details": [
            {
                "game_id": r["game_id"],
                "matched_name": r["matched_name"],
                "platform": r["platform"],
                "conflicting_game_id": r["conflicting_game_id"],
                "conflicting_name": r["conflicting_name"],
                "item": r["item"],
            }
            for r in results
            if r["status"] == "family_conflict"
        ],
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
    {"name", "game_id", "identifier_type", "identifier_value", "price_paid",
     "content_type"}
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
    identifier_type + identifier_value together, optional content_type}. A
    constituent carrying a NESTED content_type (dlc/expansion/edition) matches
    by exact name only and, under create_missing, mints nested (is_primary=0)
    linked to a resolved parent — same DLC-aware guard as set_acquisitions_batch.
    total_price is split evenly (to the cent, sum-preserving) across games
    without an explicit price_paid; games with an explicit price keep it and are
    excluded from the split.

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
        _validate_content_type(item.get("content_type"))

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

    content_type = _validate_content_type(item.get("content_type"))
    # A nested constituent (DLC/expansion/edition) restricts to exact name
    # matches so its share can't collapse onto the base game row.
    nested = content_type is not None and content_type in NESTED_CONTENT_TYPES
    name = item.get("name")
    row, match_type = await _match_batch_game(
        name,
        item.get("game_id"),
        item.get("identifier_type"),
        item.get("identifier_value"),
        # A bundle constituent is a precise, AI-supplied title; a fuzzy near-miss
        # is likelier a distinct sequel than a typo, so match exactly or create.
        fuzzy=False,
        exact_only=nested,
    )

    created = False
    mint_content_type: str | None = None
    mint_parent_id: int | None = None
    mint_parent_name: str | None = None
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
        create_name = str(name).strip()
        mint_fields, mint_parent_id, mint_parent_name = await _addon_mint_fields(
            create_name, content_type
        )
        if mint_fields:
            mint_content_type = content_type
        if dry_run:
            created_result = {
                "status": "created",
                "game_id": None,
                "matched_name": create_name,
                "match_type": "created",
                "price_paid": price,
                "recorded_price": price,  # fresh row: the proposed price persists
                "acquisition": fields,
            }
            if mint_content_type is not None:
                created_result["content_type"] = mint_content_type
                if mint_parent_id is not None:
                    created_result["parent_game_id"] = mint_parent_id
                    created_result["parent_name"] = mint_parent_name
            return created_result
        # A nested constituent must never ADOPT an existing row by name —
        # upsert_game's lower(name) match could seize the base game itself and
        # demote it with the mint fields (see _apply_batch_item's create path).
        game_id = await upsert_game(
            None, create_name, match_existing_by_name=not nested, **mint_fields
        )
        async with get_db() as db:
            row = await db.execute_fetchone(
                "SELECT id, name FROM games WHERE id = ?", (game_id,)
            )
        created = True
        match_type = "created"

    resolved_id = row["id"]
    reclassified = False
    if nested and not created and not dry_run:
        # Same repair as _apply_batch_item: an exact match landing on a row
        # still at the default classification (phantom mint / manual seed) is
        # reclassified by the importer hint — guarded, already-nested rows and
        # pinned rows untouched. Skipped on dry_run (read-only preview).
        reclassified, mint_parent_id, mint_parent_name = (
            await _reclassify_matched_nested(resolved_id, name, content_type)
        )
        if reclassified:
            mint_content_type = content_type

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

    bundle_result = {
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
    if (created or reclassified) and mint_content_type is not None:
        bundle_result["content_type"] = mint_content_type
        if mint_parent_id is not None:
            bundle_result["parent_game_id"] = mint_parent_id
            bundle_result["parent_name"] = mint_parent_name
    if reclassified:
        bundle_result["reclassified"] = True
    return bundle_result


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
    if record.content_type is not None:
        # DLC-aware matching/minting downstream: a nested content_type restricts
        # matching to exact and mints the row nested with a resolved parent.
        item["content_type"] = record.content_type
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


# Multi-game compilation markers in purchase SKU names. Only consulted for
# items that ALSO missed every matching tier — a real library row named
# "Halo: The Master Chief Collection" matches first and is never diverted.
# The tail-anchored "Complete" alternative catches compilation SKUs like
# "Hexcells Complete" without touching mid-name uses.
_BUNDLE_SUSPECT_RE = re.compile(
    r"\b(?:bundle|collection|anthology|trilogy|tetralogy|quadrilogy|saga"
    r"|franchise\s+pack|complete\s+pack)\b|\bcomplete\s*$",
    re.IGNORECASE,
)


def _is_zero_price_promo(item: dict) -> bool:
    """Free/gift promo line (zero price) — real spend never looks like this."""
    price = item.get("price_paid")
    return (price is not None and float(price) == 0.0) or item.get(
        "purchase_source"
    ) in ("free", "gift")


async def _divert_unmatched_bundle_suspects(
    items: list[dict],
) -> tuple[list[dict], list[dict]]:
    """(importable_items, diverted_bundle_entries) for compilation-named misses.

    A multi-game bundle SKU ("Metro Redux", "Far Cry Franchise Pack") can never
    attach to a single row: fed to the batch writer it either lands in
    unmatched or — worse, under create_missing — mints a phantom base game
    named after the bundle. An item whose title carries a compilation marker
    AND misses every matching tier is therefore diverted to
    bundles_needing_split for split_bundle_acquisition. Items that match stay
    items: the marker alone is not evidence (many single games are named
    "…Collection").
    """
    importable: list[dict] = []
    diverted: list[dict] = []
    for item in items:
        name = item.get("name") or ""
        if not _BUNDLE_SUSPECT_RE.search(name):
            importable.append(item)
            continue
        content_type = item.get("content_type")
        nested = content_type is not None and content_type in NESTED_CONTENT_TYPES
        row, _ = await _match_batch_game(
            name,
            item.get("game_id"),
            item.get("identifier_type"),
            item.get("identifier_value"),
            exact_only=nested,
        )
        if row is not None:
            importable.append(item)
            continue
        async with get_db() as db:
            existing = await db.execute_fetchone(
                """SELECT COUNT(*) AS c FROM game_platforms
                   WHERE bundle_name = ? AND platform = ?""",
                (name, item.get("platform")),
            )
        diverted.append(
            {
                "bundle_name": name,
                "platform": item.get("platform"),
                "total_price": item.get("price_paid"),
                "price_currency": item.get("price_currency"),
                "acquired_at": item.get("acquired_at"),
                "purchase_source": item.get("purchase_source"),
                "already_recorded": existing["c"] > 0,
                "reason": "compilation-named purchase matched no library row",
            }
        )
    return importable, diverted


async def _import_one_source(
    source: str,
    fetch,
    dry_run: bool,
    overwrite: bool,
    create_platform_rows: bool,
    create_missing: bool,
) -> dict:
    """Fetch one source and push its records through the batch writer.

    dry_run reuses the batch writer's own dry_run mode, so a preview runs the
    identical matching path and reports the identical counters — plus a
    ``proposed`` echo of the converted items (capped at ``_DRY_RUN_ECHO_CAP``
    with a ``truncated`` flag; the counters themselves are never truncated).
    Fetch exceptions propagate — the caller gathers them, and a mid-fetch
    failure must never partially import."""
    records, skipped = await fetch()
    # Multi-game bundles can't attach to a single row — divert them to a
    # dedicated bucket (with price/date) for split_bundle_acquisition instead
    # of feeding them to the single-game matcher, where they'd only ever miss.
    bundles = [await _record_to_bundle_entry(r) for r in records if r.is_bundle]
    importable = [r for r in records if not r.is_bundle]
    items = [_record_to_batch_item(r, source) for r in importable]

    # Second bundle net: sources that can't flag bundles themselves (Steam's
    # history page is just SKU names) get compilation-named MISSES diverted
    # here instead of minted/unmatched.
    items, suspect_bundles = await _divert_unmatched_bundle_suspects(items)
    bundles.extend(suspect_bundles)

    applied = filled = no_change = created = no_platform_row = errors = 0
    family_conflict = 0
    unmatched: list[dict] = []
    created_details: list[dict] = []
    no_platform_row_details: list[dict] = []
    family_conflict_details: list[dict] = []
    for start in range(0, len(items), _BATCH_ITEM_CAP):
        batch = await set_acquisitions_batch(
            items[start : start + _BATCH_ITEM_CAP],
            overwrite=overwrite,
            create_platform_rows=create_platform_rows,
            create_missing=create_missing,
            dry_run=dry_run,
        )
        applied += batch["applied"]
        filled += batch["filled"]
        no_change += batch["no_change"]
        created += batch["created"]
        no_platform_row += batch["no_platform_row"]
        family_conflict += batch["family_conflict"]
        errors += batch["errors"]
        unmatched.extend(batch["unmatched"])
        created_details.extend(batch["created_details"])
        no_platform_row_details.extend(batch["no_platform_row_details"])
        family_conflict_details.extend(batch["family_conflict_details"])

    # Zero-price promo lines (bonus packs, wallpapers, costume sets claimed for
    # free) are expected to miss — reporting them beside real unmatched spend
    # buries the misses that matter. Split, don't drop: they stay auditable.
    unmatched_free = [i for i in unmatched if _is_zero_price_promo(i)]
    unmatched = [i for i in unmatched if not _is_zero_price_promo(i)]

    result = {
        "source": source,
        "status": "ok",
        "fetched": len(records),
        "applied": applied,
        "filled": filled,
        "no_change": no_change,
        "created": created,
        "created_details": created_details,
        "unmatched": unmatched,
        "unmatched_free": unmatched_free,
        "no_platform_row": no_platform_row,
        "no_platform_row_details": no_platform_row_details,
        "family_conflict": family_conflict,
        "family_conflict_details": family_conflict_details,
        "bundles_needing_split": bundles,
        "errors": errors,
        "skipped": skipped,
    }
    if dry_run:
        result["dry_run"] = True
        result["proposed"] = items[:_DRY_RUN_ECHO_CAP]
        result["truncated"] = len(items) > _DRY_RUN_ECHO_CAP
        # Faithful preview of what create_missing would mint (game_id null),
        # including the parent a nested mint would link — the "no delete tool"
        # safety net.
        result["would_create"] = created_details[:_DRY_RUN_ECHO_CAP]
    return result


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
    proceed. dry_run previews without writing, running the SAME matching path
    and reporting the SAME counters as a wet run (unmatched, created, filled,
    no_change, …) plus a proposed echo of the converted items and, under
    create_missing, a would_create list of what would be minted — so a preview
    is a faithful audit, not a separate approximation. Zero-price promo lines
    (free/gift claims) that miss land in unmatched_free rather than unmatched,
    keeping real spend misses visible. A compilation-named purchase ("Metro
    Redux", "Far Cry Franchise Pack") that matches no library row is diverted
    to bundles_needing_split instead of being minted or reported unmatched.

    A purchase is a definitive ownership signal, so create_missing defaults
    True: a single-game purchase that matches no existing game is created as an
    owned library game (reported under each source's created/created_details).
    A record whose content_type is nested (e.g. an eShop DLC purchase) matches
    by exact name only and, when minted, is created nested (is_primary=0) linked
    to a resolved parent — so a DLC never becomes a phantom base game nor
    attaches its spend onto the base row. Set create_missing False to route
    unmatched purchases to unmatched instead. Multi-game bundles are
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
        "unmatched_free": sum(
            len(r.get("unmatched_free", [])) for r in results.values()
        ),
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
    money spent on DLC/editions is still money spent. by_family rolls spend
    up per content family (base game + its DLC/expansions, rooted at
    COALESCE(parent_game_id, id)) — surfaced only for families with a real
    nested contributor, top 10 per currency — as a content-grouped counterpart
    to by_bundle's purchase grouping.
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

        # by_family: content-grouped spend (base game + its DLC/expansions),
        # rooted at COALESCE(parent_game_id, id). Only families with a real
        # nested contributor (a priced row that is is_primary_library_item=0 AND
        # parent_game_id IS NOT NULL) are surfaced. Qualification is decided
        # ACROSS the whole family — not per currency group — so a base game
        # bought in USD with its expansion bought in EUR still surfaces both
        # rows (one per currency; amounts are never summed across currencies).
        # The qualifying filter also EXCLUDES orphan nested rows (parent NULL,
        # is_primary=0): they root a singleton family whose only rows have a
        # NULL parent — a lone addon with no base is noise the collapse
        # detectors handle, not a family. base_spent is the root row's own
        # spend (parent_game_id IS NULL), addon_spent the children's; the
        # root's name is taken from a join so it shows even when unpriced.
        # Playtime is the ROOT game's summed playtime across ALL its platforms
        # (unfiltered — "across platforms"), so family_cost_per_hour is null
        # when the base game has no playtime.
        by_family_rows = await db.execute_fetchall(
            f"""WITH fam AS (
                    SELECT COALESCE(g.parent_game_id, g.id) AS family_root,
                           gp.price_currency AS currency,
                           gp.price_paid AS price_paid,
                           g.id AS game_id,
                           g.parent_game_id AS parent_game_id,
                           g.is_primary_library_item AS is_primary
                    {priced}
                ),
                qualifying AS (
                    SELECT DISTINCT family_root
                    FROM fam
                    WHERE parent_game_id IS NOT NULL AND is_primary = 0
                )
                SELECT f.family_root AS family_game_id,
                       gr.name AS family_name,
                       f.currency AS currency,
                       ROUND(SUM(CASE WHEN f.parent_game_id IS NULL
                                      THEN f.price_paid ELSE 0 END), 2) AS base_spent,
                       ROUND(SUM(CASE WHEN f.parent_game_id IS NOT NULL
                                      THEN f.price_paid ELSE 0 END), 2) AS addon_spent,
                       ROUND(SUM(f.price_paid), 2) AS total_spent,
                       COUNT(DISTINCT CASE WHEN f.parent_game_id IS NOT NULL
                                           THEN f.game_id END) AS addon_count,
                       ROUND(pt.total_minutes / 60.0, 1) AS family_playtime_hours
                FROM fam f
                JOIN qualifying q ON q.family_root = f.family_root
                JOIN games gr ON gr.id = f.family_root
                LEFT JOIN (
                    SELECT game_id, SUM(playtime_minutes) AS total_minutes
                    FROM game_platforms
                    WHERE playtime_minutes IS NOT NULL
                    GROUP BY game_id
                ) pt ON pt.game_id = f.family_root
                GROUP BY f.family_root, f.currency
                ORDER BY total_spent DESC""",
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

    # Cap at the top 10 families per currency (rows arrive ordered by
    # total_spent DESC across all currencies, so per-currency relative order is
    # preserved) and derive cost-per-hour. A null/zero root playtime yields a
    # null family_cost_per_hour (no division).
    by_family: list[dict] = []
    _family_seen: dict[str, int] = {}
    for r in by_family_rows:
        currency = r["currency"]
        if _family_seen.get(currency, 0) >= 10:
            continue
        _family_seen[currency] = _family_seen.get(currency, 0) + 1
        hours = r["family_playtime_hours"]
        total = r["total_spent"]
        by_family.append({
            "family_game_id": r["family_game_id"],
            "family_name": r["family_name"],
            "currency": currency,
            "base_spent": r["base_spent"],
            "addon_spent": r["addon_spent"],
            "total_spent": total,
            "addon_count": r["addon_count"],
            "family_playtime_hours": hours,
            "family_cost_per_hour": round(total / hours, 2) if hours else None,
        })

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
        # Content-grouped spend (base game + its DLC/expansions), distinct from
        # by_bundle's purchase grouping. Per currency, top 10 families by total.
        "by_family": by_family,
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
