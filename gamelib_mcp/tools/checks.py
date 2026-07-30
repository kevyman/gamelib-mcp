"""``check_library`` — a consolidated registry of read-mostly data-integrity checks.

Replaces the ad-hoc ``detect_*``/``audit_steam_licenses``/``revalidate_igdb_matches``
MCP tools with one tool backed by a registry (``CHECKS``). Every check is an
adapter over an EXISTING function in ``tools/admin.py`` or ``data/steam_licenses.py``
— their logic and unit tests are untouched; this module only reshapes their
output into the uniform finding envelope below and adds selection/suppression/
apply-gating plumbing.

Philosophy: report-only by default. Every finding carries a machine-readable
``suggested_action`` naming an existing repair tool (merge_games / update_game /
split_game / delete_game / ...) so a human (or a future agent) can act on it.
Three checks also WRITE, but only when explicitly named in ``apply``:
``playtime.farming`` (marks is_farmed), ``ownership.license_gap`` (mints owned
Steam rows for retired/missed licenses), and ``extid.igdb_drift`` (resets bad
IGDB links so background enrichment re-resolves them).

Phase A shipped the 8 migrated checks (one runner covers both ``ownership.orphan``
and ``nesting.phantom_parent``). Phase B adds 10 new offline checks (identity/
nesting/wishlist/playtime/spend/enrich/sync) as a pure registry addition, and
completes the phantom-parent/superseded-base split: a phantom parent with at
least one owned child now reports ONLY under ``nesting.superseded_base`` (with
a merge-to-heir suggestion), never under ``nesting.phantom_parent`` too.
"""

import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastmcp.exceptions import ToolError

from ..data.db import (
    NINTENDO_BASELINE_DEVICE_ID,
    NINTENDO_TITLE_ID_TYPE,
    STEAM_APP_ID,
    get_db,
    get_meta,
    set_meta,
    titles_conflict_on_identity,
)
from ..data.title_normalization import (
    is_edition_variant_of,
    normalize_purchase_title,
    normalize_search_text,
)
from ..platforms_registry import SYNCABLE_PLATFORMS
from .admin import (
    detect_collapsed_games,
    detect_cross_platform_collapses,
    detect_farmed_games,
    detect_misclassified_dlc,
    detect_orphan_games,
    detect_stranded_duplicates,
    revalidate_igdb_matches,
)

# meta KV key holding a JSON list of {"check": str, "game_id": int} suppression
# entries. Tool config, not library data — filtering suppressed findings never
# touches games/game_platforms/etc., so it doesn't violate the report-only stance.
SUPPRESSIONS_META_KEY = "check_suppressions"

_SEVERITY_RANK = {"notice": 0, "warning": 1, "error": 2}
_VALID_SEVERITIES = frozenset(_SEVERITY_RANK)

# (findings, extras) — extras land under summary[check_id], and under
# applied[check_id] too when that check id was actually applied.
CheckOutcome = tuple[list[dict[str, Any]], dict[str, Any]]


def _finding(
    check: str,
    severity: str,
    message: str,
    *,
    game_id: int | None = None,
    name: str | None = None,
    evidence: dict[str, Any] | None = None,
    suggested_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one envelope-conformant finding. Every adapter goes through this."""
    assert severity in _VALID_SEVERITIES, f"invalid severity {severity!r}"
    return {
        "check": check,
        "severity": severity,
        "game_id": game_id,
        "name": name,
        "message": message,
        "evidence": evidence or {},
        "suggested_action": suggested_action,
    }


@dataclass(frozen=True)
class CheckSpec:
    id: str
    category: str
    description: str
    # None = offline (participates in the default checks=None run and needs no
    # selection/credential gating). Otherwise a short label ("igdb",
    # "steam+steamspy") documenting what the check reaches over the network.
    network: str | None
    writes_on_apply: bool
    default_severity: str
    runner: Callable[..., Awaitable[CheckOutcome]]
    option_keys: frozenset[str] = frozenset()
    # For network checks that need stored credentials/sessions (igdb creds, a
    # Steam session) — None for checks with no such prerequisite (e.g. a public
    # API needs no gating beyond being selected).
    configured: Callable[[], bool] | None = None
    unconfigured_reason: str | None = None


def _spec(
    check_id: str,
    *,
    description: str,
    network: str | None,
    writes_on_apply: bool,
    default_severity: str,
    runner: Callable[..., Awaitable[CheckOutcome]],
    option_keys: frozenset[str] = frozenset(),
    configured: Callable[[], bool] | None = None,
    unconfigured_reason: str | None = None,
) -> CheckSpec:
    return CheckSpec(
        id=check_id,
        category=check_id.split(".", 1)[0],
        description=description,
        network=network,
        writes_on_apply=writes_on_apply,
        default_severity=default_severity,
        runner=runner,
        option_keys=option_keys,
        configured=configured,
        unconfigured_reason=unconfigured_reason,
    )


def _igdb_configured() -> bool:
    from ..data.igdb import igdb_credentials_configured

    return igdb_credentials_configured()


def _steam_session_configured() -> bool:
    from ..data.steam_licenses import is_license_audit_configured

    return is_license_audit_configured()


# --- adapters: playtime.farming (was detect_farmed_games) -------------------


async def _run_playtime_farming(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    threshold_hours = options.get("threshold_hours", 8.0)
    min_games_per_day = options.get("min_games_per_day", 8)
    result = await detect_farmed_games(
        dry_run=not apply,
        threshold_hours=threshold_hours,
        min_games_per_day=min_games_per_day,
    )
    findings: list[dict[str, Any]] = []
    apply_suggestion = {
        "tool": "check_library",
        "args": {"checks": ["playtime.farming"], "apply": ["playtime.farming"]},
        "note": "marks is_farmed=1 (manual overrides respected)",
    }
    if result["candidates"]:
        findings.append(
            _finding(
                "playtime.farming",
                "notice",
                f"{result['candidates']} game(s) across {len(result['farming_days'])} "
                "day(s) look like ArchiSteamFarm card-farming sessions",
                evidence={
                    "farming_days": result["farming_days"],
                    "candidate_count": result["candidates"],
                    "steam_appids": result["steam_appids"],
                },
                suggested_action=None if apply else apply_suggestion,
            )
        )
        # detect_farmed_games only samples up to 10 candidates — mirror that
        # cap here rather than pretending every candidate got its own finding.
        for sample in result["sample_games"]:
            findings.append(
                _finding(
                    "playtime.farming",
                    "notice",
                    f"'{sample['name']}' played {sample['playtime_hours']}h, last "
                    f"played {sample['last_played']} — looks like a farming session",
                    game_id=sample["game_id"],
                    name=sample["name"],
                    evidence=sample,
                    suggested_action=None if apply else apply_suggestion,
                )
            )
    extras = {
        "threshold_hours": threshold_hours,
        "min_games_per_day": min_games_per_day,
        "candidates": result["candidates"],
        "farming_days_detected": len(result["farming_days"]),
        "marked": apply and bool(result["candidates"]),
    }
    return findings, extras


# --- adapters: identity.same_store_collapse (was detect_collapsed_games) ----


async def _run_identity_same_store_collapse(
    *, apply: bool, options: dict[str, Any]
) -> CheckOutcome:
    result = await detect_collapsed_games()
    findings = [
        _finding(
            "identity.same_store_collapse",
            "error",
            f"'{c['name']}' holds {c['identifier_count']} distinct "
            f"{c['identifier_type']} values on {c['platform']} — looks over-merged",
            game_id=c["game_id"],
            name=c["name"],
            evidence={
                "platform": c["platform"],
                "identifier_type": c["identifier_type"],
                "identifier_values": c["identifier_values"],
            },
            suggested_action={
                "tool": "split_game",
                "args": {
                    "source_game_id": c["game_id"],
                    "platform": c["platform"],
                    "identifier_values": c["identifier_values"][1:],
                },
                "note": (
                    "review which identifier belongs to which game before "
                    "applying; set new_name"
                ),
            },
        )
        for c in result["candidates"]
    ]
    return findings, {"collapsed_count": result["collapsed_count"]}


# --- adapters: identity.stranded_duplicate (was detect_stranded_duplicates) -


async def _run_identity_stranded_duplicate(
    *, apply: bool, options: dict[str, Any]
) -> CheckOutcome:
    result = await detect_stranded_duplicates()
    findings = [
        _finding(
            "identity.stranded_duplicate",
            "warning",
            f"'{c['duplicate_name']}' looks like an identifier-less duplicate "
            f"of '{c['name']}' on {c['platform']}",
            game_id=c["duplicate_game_id"],
            name=c["duplicate_name"],
            evidence=c,
            suggested_action={
                "tool": "merge_games",
                "args": {
                    "source_game_id": c["duplicate_game_id"],
                    "target_game_id": c["game_id"],
                },
                "note": "the identifier-less duplicate merges into the identified row",
            },
        )
        for c in result["candidates"]
    ]
    return findings, {"stranded_count": result["stranded_count"]}


# --- adapters: identity.cross_store_collapse (was detect_cross_platform_collapses)


async def _run_identity_cross_store_collapse(
    *, apply: bool, options: dict[str, Any]
) -> CheckOutcome:
    limit = options.get("limit", 0)
    result = await detect_cross_platform_collapses(limit=limit)
    findings = [
        _finding(
            "identity.cross_store_collapse",
            "error",
            f"'{c['name']}' Steam appid {c['steam_appid']} resolves to IGDB "
            f"'{c['steam_true_igdb_name']}', not this row's '{c['row_igdb_name']}'",
            game_id=c["game_id"],
            name=c["name"],
            evidence={
                "steam_appid": c["steam_appid"],
                "row_igdb_id": c["row_igdb_id"],
                "row_igdb_name": c["row_igdb_name"],
                "steam_true_igdb_id": c["steam_true_igdb_id"],
                "steam_true_igdb_name": c["steam_true_igdb_name"],
            },
            suggested_action={
                "tool": "split_game",
                "args": {
                    "source_game_id": c["game_id"],
                    "platform": "steam",
                    "identifier_values": [str(c["steam_appid"])],
                },
                "note": "set a distinct new_name so the split row does not re-resolve onto this identity",
            },
        )
        for c in result["candidates"]
    ]
    return findings, {"checked": result["checked"], "igdb_configured": result["igdb_configured"]}


# --- adapters: ownership.orphan + nesting.phantom_parent (was detect_orphan_games)


async def _run_ownership_orphan(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    result = await detect_orphan_games()
    findings = [
        _finding(
            "ownership.orphan",
            "warning",
            f"'{o['name']}' has no ownership and no wishlist entry",
            game_id=o["game_id"],
            name=o["name"],
            evidence={"igdb_id": o.get("igdb_id")},
            suggested_action={
                "tool": "delete_game",
                "args": {"game_id": o["game_id"], "confirm": False},
                "note": (
                    "run ownership.license_gap first — this can be a "
                    "retired-but-owned Steam app"
                ),
            },
        )
        for o in result["orphans"]
    ]
    extras = {
        "wishlist_only_count": result["wishlist_only_count"],
        "license_audit": result["license_audit"],
    }
    return findings, extras


async def _run_nesting_phantom_parent(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    result = await detect_orphan_games()
    findings = []
    for p in result["phantom_parents"]:
        if p["owned_child_count"]:
            # Phase B: a parent with at least one owned child gets the concrete
            # merge-to-heir suggestion under nesting.superseded_base instead —
            # never report the same parent under both check ids.
            continue
        findings.append(
            _finding(
                "nesting.phantom_parent",
                "warning",
                f"'{p['name']}' has no ownership or wishlist entry, but "
                f"{p['child_count']} row(s) nest under it",
                game_id=p["game_id"],
                name=p["name"],
                evidence={
                    "igdb_id": p.get("igdb_id"),
                    "child_count": p["child_count"],
                    "owned_child_count": p["owned_child_count"],
                    "remediation": p["remediation"],
                },
                suggested_action=None,
            )
        )
    return findings, {}


# --- adapters: nesting.misclassified (was detect_misclassified_dlc) --------


async def _run_nesting_misclassified(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    limit = options.get("limit", 25)
    # Flipped from the old tool's default (probe_steam=True): the consolidated
    # default run must stay fully offline, so this check only reaches the
    # network when the caller opts in via options.
    probe_steam = options.get("probe_steam", False)
    probe_offset = options.get("probe_offset", 0)
    result = await detect_misclassified_dlc(
        limit=limit, probe_steam=probe_steam, probe_offset=probe_offset
    )
    findings = [
        _finding(
            "nesting.misclassified",
            "warning",
            f"'{c['name']}' looks misclassified ({c['reason']})",
            game_id=c["game_id"],
            name=c["name"],
            evidence={"reason": c["reason"], **c["evidence"]},
            suggested_action=(
                {"tool": "update_game", "args": c["suggested_update"]}
                if c["suggested_update"]
                else None
            ),
        )
        for c in result["candidates"]
    ]
    extras = {
        "counts": result["counts"],
        "probed": result["probed"],
        "probe_remaining": result["probe_remaining"],
        "next_probe_offset": result["next_probe_offset"],
        "skipped": result["skipped"],
        "probe_steam": probe_steam,
    }
    return findings, extras


# --- adapters: extid.igdb_drift (was revalidate_igdb_matches) --------------


async def _run_extid_igdb_drift(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    limit = options.get("limit")
    include_edition_suffix = options.get("include_edition_suffix", False)
    result = await revalidate_igdb_matches(
        dry_run=not apply, limit=limit, include_edition_suffix=include_edition_suffix
    )
    # After an apply the link is already gone, so the present tense would
    # describe a state that no longer exists (the same trap ownership.
    # license_gap's findings had). Say what happened instead.
    findings = [
        _finding(
            "extid.igdb_drift",
            "notice" if apply else "warning",
            (
                f"'{m['name']}' was linked to IGDB id {m['igdb_id']} "
                f"('{m['igdb_name']}') — link reset, background enrichment will "
                "re-resolve it"
                + (" (classification reset too)" if m["classification_reset"] else "")
                if apply
                else f"'{m['name']}' is linked to IGDB id {m['igdb_id']} "
                f"('{m['igdb_name']}') — names don't match"
            ),
            game_id=m["game_id"],
            name=m["name"],
            evidence={
                "igdb_id": m["igdb_id"],
                "igdb_name": m["igdb_name"],
                "classification_reset": m["classification_reset"],
                "drift_kind": m.get("drift_kind", "wrong_entity"),
                "reset": apply,
            },
            suggested_action=(
                None
                if apply
                else {
                    "tool": "check_library",
                    "args": {"checks": ["extid.igdb_drift"], "apply": ["extid.igdb_drift"]},
                    "note": (
                        "resets igdb linkage (and classification, if attributable "
                        "to the bad match) so background enrichment re-resolves it"
                    ),
                }
            ),
        )
        for m in result["mismatches"]
    ]
    # Store-authoritative links are never reset — but they are not always
    # RIGHT: IGDB's external_games maps Steam appid 212680 (FTL) to 178437,
    # a junk duplicate named "Faster than light?", while the real record
    # exists separately. Silently excluding them would hide that, so they
    # report as notices whose only repair is a hand pin (which outranks the
    # mapping in backfill_missing_games, unlike a reset).
    findings.extend(
        _finding(
            "extid.igdb_drift",
            "notice",
            f"'{m['name']}' is linked to IGDB id {m['igdb_id']} ('{m['igdb_name']}') "
            "— IGDB's own store mapping says this appid IS that game, so the link "
            "is left alone. If IGDB is wrong (a junk duplicate record), pin the "
            "correct id by hand; a reset would just be re-applied.",
            game_id=m["game_id"],
            name=m["name"],
            evidence={
                "igdb_id": m["igdb_id"],
                "igdb_name": m["igdb_name"],
                "drift_kind": "store_authoritative",
                "reset": False,
            },
            suggested_action={
                "tool": "update_game",
                "args": {"game_id": m["game_id"], "igdb_id": m["igdb_id"]},
                "note": (
                    "replace igdb_id with the correct one if IGDB's mapping is "
                    "wrong; pinning it also stops this finding recurring"
                ),
            },
        )
        for m in result["store_authoritative_matches"]
    )
    extras = {
        "igdb_configured": result["igdb_configured"],
        "checked": result["checked"],
        "reset_count": result["reset_count"],
        "classification_reset_count": result["classification_reset_count"],
        "skipped_overridden": result["skipped_overridden"],
        "unresolved_igdb_ids": result["unresolved_igdb_ids"],
        # Correct edition→base links the name comparison alone would have
        # called drift; excluded from findings (and from any reset) unless
        # options.include_edition_suffix asks for them.
        "edition_suffix_count": result["edition_suffix_count"],
        "edition_suffix_examples": result["edition_suffix_matches"][:10],
        # Links IGDB's external_games mapping vouches for despite the name
        # difference — never reset (a reset would just be re-applied).
        "store_authoritative_count": result["store_authoritative_count"],
        "store_authoritative_examples": result["store_authoritative_matches"][:10],
    }
    return findings, extras


# --- adapters: ownership.license_gap (was audit_steam_licenses) ------------


async def _run_ownership_license_gap(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    from ..data.steam_licenses import DEFAULT_PROBE_LIMIT, audit_steam_licenses

    limit = options.get("limit", DEFAULT_PROBE_LIMIT)
    retry_unresolved = options.get("retry_unresolved", False)
    result = await audit_steam_licenses(
        limit=limit, retry_unresolved=retry_unresolved, mint=apply
    )
    if result.get("status") == "unconfigured":
        # Defensive only: run_library_checks already gates this check on
        # is_license_audit_configured() before invoking the runner.
        return [], {"status": "unconfigured"}

    mint_key = "minted" if apply else "would_mint"
    delisted_key = "minted_delisted" if apply else "would_mint_delisted"
    apply_suggestion_note = "mints an owned Steam row for this license"
    delisted_suggestion_note = "mints a delisted=1 owned Steam row for this retired license"
    apply_suggestion = {
        "tool": "check_library",
        "args": {
            "checks": ["ownership.license_gap"],
            "apply": ["ownership.license_gap"],
        },
    }

    def _gap_finding(entry: dict[str, Any], *, retired: bool) -> dict[str, Any]:
        """One license-gap finding, phrased for the run that produced it.

        After an apply the license is no longer absent — saying so (with the
        minted game_id) is the difference between "healed" and "declined to
        mint", which `would_mint: false` alone could not express.
        """
        label = "Retired Steam license" if retired else "Owned Steam license"
        note = delisted_suggestion_note if retired else apply_suggestion_note
        evidence: dict[str, Any] = {
            "appid": entry["appid"],
            "name": entry.get("name"),
            "classified_type": "retired_game" if retired else "game",
            "would_mint": not apply,
            "minted": apply,
        }
        if apply:
            evidence["delisted"] = retired
            evidence["game_id"] = entry.get("game_id")
            message = (
                f"{label} {entry['appid']} ('{entry.get('name')}') was missing and "
                f"has been minted as game_id {entry.get('game_id')}"
                + (" (delisted=1)" if retired else "")
            )
            return _finding(
                "ownership.license_gap",
                "notice",
                message,
                game_id=entry.get("game_id"),
                name=entry.get("name"),
                evidence=evidence,
                suggested_action=None,
            )
        return _finding(
            "ownership.license_gap",
            "warning",
            f"{label} {entry['appid']} ('{entry.get('name')}') is absent from the library",
            name=entry.get("name"),
            evidence=evidence,
            suggested_action={**apply_suggestion, "note": note},
        )

    findings = [
        _gap_finding(entry, retired=False) for entry in result.get(mint_key, [])
    ]
    findings.extend(
        _gap_finding(entry, retired=True) for entry in result.get(delisted_key, [])
    )

    extras = {
        k: v
        for k, v in result.items()
        if k not in ("minted", "minted_delisted", "would_mint", "would_mint_delisted")
    }
    if apply:
        extras["minted_game_ids"] = [
            entry.get("game_id")
            for entry in (*result.get("minted", []), *result.get("minted_delisted", []))
        ]
    return findings, extras


# --- adapters: nesting.superseded_base (Phase B, new) ----------------------


async def _children_with_substance(parent_id: int) -> list[dict[str, Any]]:
    """Nested rows under ``parent_id`` with ownership/playtime/identifier counts."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT c.id AS game_id, c.name, c.content_type,
                      EXISTS(
                          SELECT 1 FROM game_platforms gp
                           WHERE gp.game_id = c.id AND gp.owned = 1
                      ) AS owned,
                      COALESCE(
                          (SELECT SUM(gp.playtime_minutes) FROM game_platforms gp
                            WHERE gp.game_id = c.id AND gp.owned = 1),
                          0
                      ) AS playtime_minutes,
                      (SELECT COUNT(*) FROM game_platform_identifiers gpi
                         JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                        WHERE gp.game_id = c.id) AS identifier_count
               FROM games c
               WHERE c.parent_game_id = ?
               ORDER BY c.id""",
            (parent_id,),
        )
    return [dict(row) for row in rows]


def _is_edition_heir(child: dict[str, Any], parent_name: str) -> bool:
    """Whether an owned child is an EDITION of its parent (a supersession heir).

    Owning DLC/expansions without the base game is a legitimate ownership state
    (Epic giveaways, Humble keys, a route pack for Train Sim World), not a data
    error — merging such a child into the parent would rename a base-game row
    to a DLC title and flatten its siblings underneath it. So the heir has to
    actually BE the game: either typed as an ``edition``, or named as an
    edition-suffixed form of the parent ("Pinball FX Classic" under "Pinball
    FX"). Everything else is reported under ownership.dlc_without_base instead.
    """
    if (child.get("content_type") or "") == "edition":
        return True
    return is_edition_variant_of(child.get("name") or "", parent_name)


async def _run_nesting_superseded_base(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    """Phantom parents whose owned child is an EDITION — the supersession shape.

    Shares detect_orphan_games' phantom_parents detector with nesting.phantom_parent
    (which takes owned_child_count == 0); this half picks the strongest owned
    EDITION child as the merge heir (most playtime, then most store identifiers,
    then lowest id) and emits a concrete merge_games suggestion, per the
    edition-becomes-canonical supersession stance. A phantom parent whose only
    owned children are DLC/expansions is not a supersession at all — it reports
    under ownership.dlc_without_base.
    """
    result = await detect_orphan_games()
    findings = []
    for p in result["phantom_parents"]:
        if not p["owned_child_count"]:
            continue
        parent_id = p["game_id"]
        children = await _children_with_substance(parent_id)
        owned_children = [
            c for c in children if c["owned"] and _is_edition_heir(c, p["name"])
        ]
        if not owned_children:
            continue
        heir = min(
            owned_children,
            key=lambda c: (-(c["playtime_minutes"] or 0), -c["identifier_count"], c["game_id"]),
        )
        async with get_db() as db:
            wishlist_row = await db.execute_fetchone(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (parent_id,)
            )
            identifier_row = await db.execute_fetchone(
                """SELECT COUNT(*) AS c FROM game_platform_identifiers gpi
                     JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                    WHERE gp.game_id = ?""",
                (parent_id,),
            )
        findings.append(
            _finding(
                "nesting.superseded_base",
                "warning",
                f"Owned edition '{heir['name']}' nests under '{p['name']}', which isn't "
                "owned anywhere — the edition should become the canonical row",
                game_id=parent_id,
                name=p["name"],
                evidence={
                    "children": [
                        {
                            "game_id": c["game_id"],
                            "name": c["name"],
                            "content_type": c["content_type"],
                            "owned": bool(c["owned"]),
                            "playtime_minutes": c["playtime_minutes"],
                        }
                        for c in children
                    ],
                    "heir_game_id": heir["game_id"],
                    "parent_has_wishlist": wishlist_row is not None,
                    "parent_identifier_count": identifier_row["c"] if identifier_row else 0,
                },
                suggested_action={
                    "tool": "merge_games",
                    "args": {"source_game_id": parent_id, "target_game_id": heir["game_id"]},
                    "note": (
                        "owned edition becomes canonical primary; merge transfers "
                        "ratings/series/spend/wishlist and re-points siblings"
                    ),
                },
            )
        )
    return findings, {}


# --- adapters: ownership.dlc_without_base -----------------------------------


async def _run_ownership_dlc_without_base(
    *, apply: bool, options: dict[str, Any]
) -> CheckOutcome:
    """Owned DLC/expansions whose base game is owned nowhere.

    The other half of the phantom-parent-with-an-owned-child split: where
    nesting.superseded_base takes the edition shape (the child IS the game and
    should become canonical), this takes the DLC shape, which is a legitimate
    ownership state and NOT a merge candidate — a route pack bought without
    Train Sim World, an Epic giveaway DLC. Informational, no suggested_action:
    the only "repairs" are buying the base game or, if the parent row is pure
    noise, deleting it once its children are re-pointed.
    """
    result = await detect_orphan_games()
    findings = []
    for p in result["phantom_parents"]:
        if not p["owned_child_count"]:
            continue
        parent_id = p["game_id"]
        children = await _children_with_substance(parent_id)
        owned_children = [c for c in children if c["owned"]]
        if any(_is_edition_heir(c, p["name"]) for c in owned_children):
            # An edition heir exists — nesting.superseded_base owns this row.
            continue
        names = ", ".join(f"'{c['name']}'" for c in owned_children[:3])
        findings.append(
            _finding(
                "ownership.dlc_without_base",
                "notice",
                f"{len(owned_children)} owned nested item(s) ({names}"
                + (", …" if len(owned_children) > 3 else "")
                + f") hang off '{p['name']}', which isn't owned anywhere — normal "
                "when DLC was bought (or gifted) without its base game",
                game_id=parent_id,
                name=p["name"],
                evidence={
                    "owned_children": [
                        {
                            "game_id": c["game_id"],
                            "name": c["name"],
                            "content_type": c["content_type"],
                            "playtime_minutes": c["playtime_minutes"],
                        }
                        for c in owned_children
                    ],
                    "child_count": p["child_count"],
                },
                suggested_action=None,
            )
        )
    return findings, {}


# --- adapters: identity.unlinked_edition (Phase B, new) ---------------------


async def _run_identity_unlinked_edition(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    """Owned primary pairs where one name is an edition-suffixed form of the other.

    Reuses normalize_purchase_title (SKU/edition-suffix stripping) + normalize_search_text
    (the same normalization games.name_normalized is built from) rather than inventing new
    fuzzy logic; titles_conflict_on_identity guards against a base title colliding with a
    numbered sequel.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.name_normalized, g.parent_game_id
               FROM games g
               WHERE g.is_primary_library_item = 1
                 AND EXISTS (
                     SELECT 1 FROM game_platforms gp WHERE gp.game_id = g.id AND gp.owned = 1
                 )"""
        )
    owned_primary = [dict(row) for row in rows]
    by_normalized: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in owned_primary:
        norm = row["name_normalized"] or normalize_search_text(row["name"])
        by_normalized[norm].append(row)

    findings = []
    seen_pairs: set[tuple[int, int]] = set()
    for edition in owned_primary:
        stripped_norm = normalize_search_text(normalize_purchase_title(edition["name"]))
        for base in by_normalized.get(stripped_norm, []):
            if base["game_id"] == edition["game_id"]:
                continue
            if base["name"] == edition["name"]:
                continue
            if (
                base["parent_game_id"] == edition["game_id"]
                or edition["parent_game_id"] == base["game_id"]
            ):
                continue
            if titles_conflict_on_identity(base["name"], edition["name"]):
                continue
            pair_key = (min(base["game_id"], edition["game_id"]), max(base["game_id"], edition["game_id"]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            findings.append(
                _finding(
                    "identity.unlinked_edition",
                    "notice",
                    f"'{edition['name']}' looks like an edition of '{base['name']}' but "
                    "lives as an unrelated sibling row — human call: merge_games if this "
                    "is the same purchase, or update_game(parent_game_id=...) if the two "
                    "are intentionally distinct",
                    game_id=edition["game_id"],
                    name=edition["name"],
                    evidence={
                        "edition_game_id": edition["game_id"],
                        "edition_name": edition["name"],
                        "base_game_id": base["game_id"],
                        "base_name": base["name"],
                    },
                    suggested_action=None,
                )
            )
    return findings, {"checked": len(owned_primary)}


# --- adapters: nesting.dangling_parent (Phase B, new) -----------------------


async def _run_nesting_dangling_parent(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.parent_game_id,
                      p.id AS parent_id, p.name AS parent_name,
                      p.is_primary_library_item AS parent_is_primary
               FROM games g
               LEFT JOIN games p ON p.id = g.parent_game_id
               WHERE g.parent_game_id IS NOT NULL
                 AND (
                     p.id IS NULL
                     OR g.parent_game_id = g.id
                     OR p.is_primary_library_item = 0
                 )
               ORDER BY g.id"""
        )
    findings = []
    for row in rows:
        if row["parent_id"] is None:
            reason = "missing_parent"
            detail = f"parent_game_id {row['parent_game_id']} does not exist"
        elif row["game_id"] == row["parent_game_id"]:
            reason = "self_parent"
            detail = "parent_game_id points at itself"
        else:
            reason = "parent_not_primary"
            detail = f"parent '{row['parent_name']}' is itself nested content"
        findings.append(
            _finding(
                "nesting.dangling_parent",
                "error",
                f"'{row['name']}' has a broken parent link ({detail})",
                game_id=row["game_id"],
                name=row["name"],
                evidence={
                    "reason": reason,
                    "parent_game_id": row["parent_game_id"],
                    "parent_name": row["parent_name"],
                },
                suggested_action={
                    "tool": "update_game",
                    "args": {"game_id": row["game_id"], "parent_game_id": 0},
                    "note": (
                        "clears the broken link (parent_game_id=0 is update_game's detach "
                        "sentinel); repoint at the correct primary parent instead via "
                        "parent_name/parent_game_id if one exists"
                    ),
                },
            )
        )
    return findings, {}


# --- adapters: wishlist.already_owned (Phase B, new) ------------------------


async def _run_wishlist_already_owned(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT w.id AS wishlist_id, w.game_id, w.platform, w.wishlisted_at, w.source,
                      g.name
               FROM game_wishlist w
               JOIN games g ON g.id = w.game_id
               WHERE EXISTS (
                   SELECT 1 FROM game_platforms gp
                    WHERE gp.game_id = w.game_id AND gp.platform = w.platform AND gp.owned = 1
               )
               ORDER BY w.id"""
        )
    findings = [
        _finding(
            "wishlist.already_owned",
            "warning",
            f"'{row['name']}' is wishlisted on {row['platform']} but already owned there "
            "— re-run sync(targets=[\"library\"]) (clear_fulfilled_wishlist_entries should have "
            "cleared this; the sweep missed it, or the row was hand-edited)",
            game_id=row["game_id"],
            name=row["name"],
            evidence={
                "platform": row["platform"],
                "source": row["source"],
                "wishlisted_at": row["wishlisted_at"],
            },
            suggested_action=None,
        )
        for row in rows
    ]
    return findings, {}


# --- adapters: playtime.snapshot_regression (Phase B, new) ------------------


async def _run_playtime_snapshot_regression(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT ph.game_id, ph.platform, ph.snapshot_date, ph.playtime_minutes, g.name
               FROM play_history ph
               JOIN games g ON g.id = ph.game_id
               ORDER BY ph.game_id, ph.platform, ph.snapshot_date"""
        )
    worst: dict[tuple[int, str], dict[str, Any]] = {}
    prev_by_key: dict[tuple[int, str], Any] = {}
    for row in rows:
        key = (row["game_id"], row["platform"])
        prev = prev_by_key.get(key)
        if prev is not None and row["playtime_minutes"] < prev["playtime_minutes"]:
            drop = prev["playtime_minutes"] - row["playtime_minutes"]
            existing = worst.get(key)
            if existing is None or drop > existing["drop_minutes"]:
                worst[key] = {
                    "name": row["name"],
                    "prev_date": prev["snapshot_date"],
                    "prev_minutes": prev["playtime_minutes"],
                    "next_date": row["snapshot_date"],
                    "next_minutes": row["playtime_minutes"],
                    "drop_minutes": drop,
                }
        prev_by_key[key] = row

    findings = [
        _finding(
            "playtime.snapshot_regression",
            "error",
            f"'{info['name']}' playtime on {platform} dropped from "
            f"{info['prev_minutes']}m ({info['prev_date']}) to {info['next_minutes']}m "
            f"({info['next_date']}) — cumulative totals should never decrease; "
            "investigate an identity swap or sync bug before deleting rows",
            game_id=game_id,
            name=info["name"],
            evidence={
                "platform": platform,
                "prev_date": info["prev_date"],
                "prev_minutes": info["prev_minutes"],
                "next_date": info["next_date"],
                "next_minutes": info["next_minutes"],
                "drop_minutes": info["drop_minutes"],
            },
            suggested_action=None,
        )
        for (game_id, platform), info in worst.items()
    ]
    return findings, {"snapshot_rows_checked": len(rows)}


# --- adapters: playtime.orphan_switch_summary (Phase B, new) ----------------


async def _run_playtime_orphan_switch_summary(
    *, apply: bool, options: dict[str, Any]
) -> CheckOutcome:
    # The manual-baseline sentinel device row (see set_switch2_playtime_baseline)
    # represents user-entered pre-tracking playtime for a game that ALREADY has a
    # nintendo_title_id identifier by the time it's written — excluded here so a
    # baseline never masquerades as "real" orphaned Parental Controls playtime.
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT nps.application_id,
                      SUM(nps.playtime_minutes) AS total_minutes,
                      MIN(nps.period_key) AS first_day,
                      MAX(nps.period_key) AS last_day,
                      MAX(nps.app_name) AS app_name
               FROM nintendo_play_summary nps
               WHERE nps.period_type = 'day'
                 AND nps.device_id != ?
                 AND NOT EXISTS (
                     SELECT 1 FROM game_platform_identifiers gpi
                      WHERE gpi.identifier_type = ? AND gpi.identifier_value = nps.application_id
                 )
               GROUP BY nps.application_id
               HAVING SUM(nps.playtime_minutes) > 0
               ORDER BY total_minutes DESC""",
            (NINTENDO_BASELINE_DEVICE_ID, NINTENDO_TITLE_ID_TYPE),
        )
    findings = [
        _finding(
            "playtime.orphan_switch_summary",
            "notice",
            (
                f"Nintendo title {row['application_id']}"
                + (f" ('{row['app_name']}')" if row["app_name"] else "")
                + f" has {row['total_minutes']}m of Parental Controls playtime with no "
                "matching library game — identify the game and use add_game_to_platform, "
                "or fix its nintendo_title_id identifier"
            ),
            evidence={
                "application_id": row["application_id"],
                "app_name": row["app_name"],
                "total_minutes": row["total_minutes"],
                "first_day": row["first_day"],
                "last_day": row["last_day"],
            },
            suggested_action=None,
        )
        for row in rows
    ]
    return findings, {}


# --- adapters: spend.duplicate_purchase (Phase B, new) ----------------------


async def _run_spend_duplicate_purchase(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT gp.id AS gp_id, gp.game_id, gp.platform, gp.acquired_at, gp.price_paid,
                      gp.price_currency, gp.purchase_source, gp.bundle_name,
                      g.name, g.name_normalized, g.parent_game_id
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               -- owned = 1: a retired row (refund/revoked key/lapsed
               -- subscription, see add_game_to_platform's unowned_at) keeps its
               -- acquisition record for history, and "bought it twice, refunded
               -- one" is the shape this check must NOT call a duplicate.
               WHERE gp.owned = 1
                 AND gp.price_paid > 0 AND gp.acquired_at IS NOT NULL"""
        )
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row["acquired_at"],
            row["price_paid"],
            row["price_currency"],
            row["purchase_source"],
            row["bundle_name"],
        )
        groups[key].append(dict(row))

    findings = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                root_a = a["parent_game_id"] or a["game_id"]
                root_b = b["parent_game_id"] or b["game_id"]
                norm_a = a["name_normalized"] or normalize_search_text(a["name"])
                norm_b = b["name_normalized"] or normalize_search_text(b["name"])
                same_row_identity = a["game_id"] == b["game_id"] or norm_a == norm_b
                if not (root_a == root_b or same_row_identity):
                    # Cross-family identical rows are legit (bundle splits share
                    # acquired_at/source/bundle_name but have different prices —
                    # matching here already means the prices coincided too).
                    continue
                if not same_row_identity and key[4] is not None:
                    # Same family (parent+child, or two children of one parent)
                    # under one bundle_name: that is exactly what
                    # split_bundle_acquisition writes — a base game and its DLC
                    # each carrying the bundle's per-item share. Not a duplicate.
                    continue
                findings.append(
                    _finding(
                        "spend.duplicate_purchase",
                        "warning",
                        f"'{a['name']}' ({a['platform']}) and '{b['name']}' ({b['platform']}) "
                        "carry an identical acquisition record — possible duplicate import; "
                        "review before clearing one via set_acquisition",
                        game_id=a["game_id"],
                        name=a["name"],
                        evidence={
                            "acquired_at": key[0],
                            "price_paid": key[1],
                            "price_currency": key[2],
                            "purchase_source": key[3],
                            "bundle_name": key[4],
                            "rows": [
                                {
                                    "game_id": a["game_id"],
                                    "name": a["name"],
                                    "platform": a["platform"],
                                },
                                {
                                    "game_id": b["game_id"],
                                    "name": b["name"],
                                    "platform": b["platform"],
                                },
                            ],
                        },
                        suggested_action=None,
                    )
                )
    return findings, {"priced_rows_checked": len(rows)}


# --- adapters: spend.price_anomaly (Phase B, new) ---------------------------


async def _run_spend_price_anomaly(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    async with get_db() as db:
        free_rows = await db.execute_fetchall(
            """SELECT gp.game_id, gp.platform, gp.price_paid, gp.price_currency, g.name
               FROM game_platforms gp JOIN games g ON g.id = gp.game_id
               WHERE gp.purchase_source = 'free' AND gp.price_paid > 0"""
        )
        # (c) price_paid >> P95 of its currency is skipped for v1 per the design
        # doc — (a)+(b) suffice and avoid a fiddly percentile computation here.
        currency_rows = await db.execute_fetchall(
            """SELECT gp.game_id, gp.platform, gp.price_paid, gp.price_currency, g.name
               FROM game_platforms gp JOIN games g ON g.id = gp.game_id
               WHERE gp.price_currency IN (
                   SELECT price_currency FROM game_platforms
                   WHERE price_currency IS NOT NULL
                   GROUP BY price_currency HAVING COUNT(*) = 1
               )"""
        )
    findings = []
    for row in free_rows:
        findings.append(
            _finding(
                "spend.price_anomaly",
                "notice",
                f"'{row['name']}' is marked purchase_source='free' but price_paid is "
                f"{row['price_paid']} {row['price_currency'] or ''}".strip(),
                game_id=row["game_id"],
                name=row["name"],
                evidence={
                    "kind": "free_with_price",
                    "platform": row["platform"],
                    "price_paid": row["price_paid"],
                    "price_currency": row["price_currency"],
                },
                suggested_action={
                    "tool": "set_acquisition",
                    "args": {
                        "game_id": row["game_id"],
                        "platform": row["platform"],
                        "clear": ["price_paid"],
                    },
                    "note": "or fix purchase_source instead if this wasn't actually free",
                },
            )
        )
    for row in currency_rows:
        findings.append(
            _finding(
                "spend.price_anomaly",
                "notice",
                f"'{row['name']}' is the only acquisition row using currency "
                f"'{row['price_currency']}' — possible typo",
                game_id=row["game_id"],
                name=row["name"],
                evidence={
                    "kind": "singleton_currency",
                    "platform": row["platform"],
                    "price_currency": row["price_currency"],
                    "price_paid": row["price_paid"],
                },
                suggested_action=None,
            )
        )
    return findings, {
        "free_with_price_count": len(free_rows),
        "singleton_currency_count": len(currency_rows),
    }


# --- adapters: enrich.coverage (Phase B, new) -------------------------------


def _worst_offenders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda r: r["playtime_minutes"] or 0, reverse=True)
    return [
        {
            "game_id": r["game_id"],
            "name": r["name"],
            "playtime_hours": round((r["playtime_minutes"] or 0) / 60, 1),
        }
        for r in ranked[:10]
    ]


async def _run_enrich_coverage(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id, g.name, g.tags, g.igdb_id, g.cover_image_id, g.hltb_main,
                      EXISTS(
                          SELECT 1 FROM game_platform_identifiers gpi
                            JOIN game_platforms gp2 ON gp2.id = gpi.game_platform_id
                           WHERE gp2.game_id = g.id AND gpi.identifier_type = ?
                      ) AS has_steam_appid,
                      COALESCE(
                          (SELECT SUM(gp3.playtime_minutes) FROM game_platforms gp3
                            WHERE gp3.game_id = g.id AND gp3.owned = 1),
                          0
                      ) AS playtime_minutes
               FROM games g
               WHERE g.is_primary_library_item = 1
                 AND COALESCE(g.is_farmed, 0) = 0
                 AND EXISTS (SELECT 1 FROM game_platforms gp WHERE gp.game_id = g.id AND gp.owned = 1)""",
            (STEAM_APP_ID,),
        )
    population = [dict(row) for row in rows]
    total = len(population)

    fields: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("tags", lambda r: not r["tags"] or r["tags"] == "[]"),
        ("igdb_id", lambda r: r["igdb_id"] is None),
        ("cover", lambda r: r["cover_image_id"] is None and not r["has_steam_appid"]),
        ("hltb_main", lambda r: r["hltb_main"] is None),
    ]

    findings = []
    for field_name, is_missing in fields:
        if not total:
            break
        missing_rows = [r for r in population if is_missing(r)]
        if not missing_rows:
            continue
        pct = round(100.0 * len(missing_rows) / total, 1)
        findings.append(
            _finding(
                "enrich.coverage",
                "notice",
                f"{len(missing_rows)}/{total} owned games ({pct}%) are missing {field_name} "
                "— get_game_detail triggers lazy enrichment per game; sync(targets=[\"library\"]) "
                "for bulk background enrichment",
                evidence={
                    "field": field_name,
                    "missing": len(missing_rows),
                    "total": total,
                    "pct": pct,
                    "worst_offenders": _worst_offenders(missing_rows),
                },
                suggested_action=None,
            )
        )
    return findings, {"total_games": total}


# --- adapters: sync.staleness (Phase B, new) --------------------------------

# switch2 playtime is served from nintendo_play_summary, not play_history (see
# CLAUDE.md's playtime-history pattern) — it never writes snapshots by design,
# so it is exempt from the "no recent snapshots" gap check below.
_SNAPSHOT_EXEMPT_PLATFORMS = frozenset({"switch2"})


async def _run_sync_staleness(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    stale_days = options.get("stale_days", 7)
    now = datetime.now(UTC)

    async with get_db() as db:
        placeholders = ",".join("?" for _ in SYNCABLE_PLATFORMS)
        platform_rows = await db.execute_fetchall(
            f"""SELECT platform, MAX(last_synced) AS last_synced, COUNT(*) AS owned_count
                FROM game_platforms
                WHERE owned = 1 AND platform IN ({placeholders})
                GROUP BY platform""",
            tuple(SYNCABLE_PLATFORMS),
        )

    findings = []
    stale_platforms: list[str] = []
    for row in platform_rows:
        last_synced_raw = row["last_synced"]
        age_days: float | None = None
        stale = True
        if last_synced_raw:
            try:
                last_synced_dt = datetime.fromisoformat(last_synced_raw)
            except ValueError:
                stale = True
            else:
                if last_synced_dt.tzinfo is None:
                    last_synced_dt = last_synced_dt.replace(tzinfo=UTC)
                age_days = (now - last_synced_dt).total_seconds() / 86400
                stale = age_days > stale_days
        if stale:
            stale_platforms.append(row["platform"])
            findings.append(
                _finding(
                    "sync.staleness",
                    "notice",
                    f"{row['platform']} hasn't synced in over {stale_days} day(s) "
                    f"(last_synced={last_synced_raw or 'never'})",
                    evidence={
                        "platform": row["platform"],
                        "last_synced": last_synced_raw,
                        "owned_row_count": row["owned_count"],
                        "age_days": round(age_days, 1) if age_days is not None else None,
                    },
                    suggested_action={
                        "tool": "sync",
                        "args": {"targets": ["library"]},
                        "note": "also check get_integration_status for credential/session issues",
                    },
                )
            )

    # Snapshot-writer health. record_play_history_snapshots writes only when a
    # game's cumulative playtime CHANGES, so "no recent snapshot rows" is the
    # normal state of a healthy but idle library — never evidence by itself.
    # The real failure signal is divergence on a recently-synced platform:
    # current game_platforms.playtime_minutes ahead of the latest snapshot (or
    # playtime with no snapshot at all) means the post-sync writer owed a write
    # it never made (it logs a warning but deliberately never fails the sync).
    gap_platforms: list[str] = []
    async with get_db() as db:
        for row in platform_rows:
            platform = row["platform"]
            if platform in stale_platforms or platform in _SNAPSHOT_EXEMPT_PLATFORMS:
                continue
            divergent = await db.execute_fetchall(
                """SELECT g.id AS game_id, g.name,
                          gp.playtime_minutes AS current_minutes,
                          (SELECT ph.playtime_minutes FROM play_history ph
                            WHERE ph.game_id = gp.game_id AND ph.platform = gp.platform
                            ORDER BY ph.snapshot_date DESC LIMIT 1) AS last_snapshot_minutes
                   FROM game_platforms gp
                   JOIN games g ON g.id = gp.game_id
                   WHERE gp.platform = ? AND gp.owned = 1
                     AND COALESCE(gp.playtime_minutes, 0) > 0
                     AND COALESCE(gp.playtime_minutes, 0) >
                         COALESCE((SELECT ph.playtime_minutes FROM play_history ph
                                    WHERE ph.game_id = gp.game_id
                                      AND ph.platform = gp.platform
                                    ORDER BY ph.snapshot_date DESC LIMIT 1), 0)
                   ORDER BY gp.playtime_minutes DESC""",
                (platform,),
            )
            if not divergent:
                continue
            gap_platforms.append(platform)
            findings.append(
                _finding(
                    "sync.staleness",
                    "notice",
                    f"{platform} synced recently, but {len(divergent)} game(s) have "
                    "current playtime ahead of (or missing from) their latest "
                    "play_history snapshot — the post-sync snapshot writer may be "
                    "failing silently",
                    evidence={
                        "platform": platform,
                        "last_synced": row["last_synced"],
                        "divergent_games": len(divergent),
                        "examples": [
                            {
                                "game_id": d["game_id"],
                                "name": d["name"],
                                "current_minutes": d["current_minutes"],
                                "last_snapshot_minutes": d["last_snapshot_minutes"],
                            }
                            for d in divergent[:5]
                        ],
                    },
                    suggested_action=None,
                )
            )

    return findings, {
        "stale_days": stale_days,
        "platforms_checked": [row["platform"] for row in platform_rows],
        "stale_platforms": stale_platforms,
        "snapshot_gap_platforms": gap_platforms,
    }


# --- adapters: sync.platform_error (new) ------------------------------------

# How long a platform may go without a SUCCESSFUL sync before it is reported,
# even when nothing errored (a sync that never runs raises no error at all).
_SYNC_SUCCESS_STALE_HOURS = 48


async def _run_sync_platform_error(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    """Report platforms whose last sync FAILED, or that haven't succeeded lately.

    Distinct from sync.staleness, which measures how old the library DATA is
    (game_platforms.last_synced). This one reads the sync run's own outcome from
    meta, and it is the check that would have caught the three-day silent Steam
    failure: the rows kept their old last_synced, every other platform in the
    same scheduled run succeeded, and nothing surfaced the error unless someone
    called get_sync_status by hand.
    """
    from ..data.db import get_meta_prefix

    stale_hours = options.get("stale_hours", _SYNC_SUCCESS_STALE_HOURS)
    now = datetime.now(UTC)

    states = await get_meta_prefix("sync_platform_state_")
    integ = await get_meta_prefix("integration_sync_")

    findings: list[dict[str, Any]] = []
    failing: list[str] = []
    unconfigured: list[str] = []
    never_synced: list[str] = []
    for platform in sorted(SYNCABLE_PLATFORMS):
        state = states.get(f"sync_platform_state_{platform}")
        error = integ.get(f"integration_sync_{platform}_last_error_summary")
        classification = integ.get(f"integration_sync_{platform}_last_error_classification")
        last_success_raw = integ.get(f"integration_sync_{platform}_last_success_at")

        if state == "unconfigured" or classification == "missing_configuration":
            # A platform the owner never set up is a choice, not a defect —
            # counted in the summary, never a finding on every run.
            unconfigured.append(platform)
            continue

        age_hours: float | None = None
        if last_success_raw:
            try:
                last_success = datetime.fromisoformat(last_success_raw)
            except ValueError:
                last_success = None
            if last_success is not None:
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=UTC)
                age_hours = (now - last_success).total_seconds() / 3600
        elif state is not None:
            never_synced.append(platform)

        stale = age_hours is not None and age_hours > stale_hours
        if not error and not stale:
            continue

        failing.append(platform)
        age_days = round(age_hours / 24, 1) if age_hours is not None else None
        if error:
            message = (
                f"{platform}'s last sync failed: {error}"
                + (
                    f" — last success {last_success_raw} ({age_days} day(s) ago)"
                    if age_days is not None
                    else " — it has never synced successfully"
                )
            )
        else:
            message = (
                f"{platform} has not synced successfully in "
                f"{age_days} day(s) (last success {last_success_raw}), "
                f"past the {stale_hours}h threshold"
            )
        findings.append(
            _finding(
                "sync.platform_error",
                # A failed sync is a warning, not a notice: everything derived
                # from that platform is silently frozen until it is fixed.
                "warning",
                message,
                evidence={
                    "platform": platform,
                    "state": state,
                    "error": error,
                    "error_classification": classification,
                    "last_success_at": last_success_raw,
                    "hours_since_success": round(age_hours, 1) if age_hours is not None else None,
                },
                suggested_action={
                    "tool": "sync",
                    "args": {"targets": ["library"], "platforms": [platform]},
                    "note": (
                        "re-auth first — see get_integration_status"
                        if classification == "auth_stale"
                        else "retry this platform on its own; check "
                        "get_integration_status if it fails again"
                    ),
                },
            )
        )

    return findings, {
        "stale_hours": stale_hours,
        "failing_platforms": failing,
        "unconfigured_platforms": unconfigured,
        "never_synced_platforms": never_synced,
    }


# --- adapters: ownership.unseen_in_source (new) -----------------------------

# Successful syncs a row must be absent from before it is worth reporting. Three
# is deliberately conservative: one source page dropping out of a single run is
# routine (26 Epic giveaway rows went stale on one date in production, all of
# them permanent entitlements that cannot be lost), so a single miss is noise.
_UNSEEN_MIN_MISSED_SYNCS = 3
_UNSEEN_ROW_LIMIT = 50


async def _run_ownership_unseen_in_source(
    *, apply: bool, options: dict[str, Any]
) -> CheckOutcome:
    """Owned rows the platform's own source has stopped returning.

    Reads game_platforms.last_seen_in_source (v34) against the platform's recent
    SUCCESSFUL sync timestamps — a failed run must never make a row look
    abandoned, which is what made the pre-v34 signal useless: "not seen this
    run" and "no longer owned" produced identical rows.

    Report-only, and deliberately so. A source omitting a row is not proof of a
    refund; it is equally a dropped page, a store retirement, or a provider
    quirk. The finding hands the judgement call to a human, whose remedies are
    add_game_to_platform(unowned_at=…) if ownership really ended and
    add_game_to_platform(delisted=True) if the store page is simply gone.
    """
    from ..lifecycle import successful_sync_history

    min_missed = max(1, int(options.get("min_missed_syncs", _UNSEEN_MIN_MISSED_SYNCS)))
    limit = max(1, int(options.get("limit", _UNSEEN_ROW_LIMIT)))

    findings: list[dict[str, Any]] = []
    cutoffs: dict[str, str] = {}
    insufficient: list[str] = []
    for platform in sorted(SYNCABLE_PLATFORMS):
        history = await successful_sync_history(platform)
        if len(history) < min_missed:
            insufficient.append(platform)
            continue
        cutoffs[platform] = history[min_missed - 1]

    if not cutoffs:
        return findings, {
            "min_missed_syncs": min_missed,
            "platforms_checked": [],
            "platforms_insufficient_history": insufficient,
            "unseen_rows": 0,
        }

    rows: list[dict[str, Any]] = []
    async with get_db() as db:
        for platform, cutoff in cutoffs.items():
            platform_rows = await db.execute_fetchall(
                """SELECT gp.game_id, gp.platform, gp.last_seen_in_source,
                          gp.playtime_minutes, gp.acquired_at, gp.price_paid,
                          gp.price_currency, g.name
                     FROM game_platforms gp
                     JOIN games g ON g.id = gp.game_id
                    WHERE gp.platform = ?
                      AND gp.owned = 1
                      AND COALESCE(gp.delisted, 0) = 0
                      -- NULL means the row was never stamped at all: hand-added,
                      -- or older than the column. Absence of evidence, not
                      -- evidence of absence — those rows are not judged here.
                      AND gp.last_seen_in_source IS NOT NULL
                      AND gp.last_seen_in_source < ?
                 ORDER BY gp.last_seen_in_source ASC
                    LIMIT ?""",
                (platform, cutoff, limit),
            )
            rows.extend(dict(row) for row in platform_rows)

    for row in rows:
        platform = row["platform"]
        findings.append(
            _finding(
                "ownership.unseen_in_source",
                "notice",
                f"'{row['name']}' is still marked owned on {platform} but the "
                f"{platform} source has not returned it in the last {min_missed} "
                f"successful sync(s) (last seen {row['last_seen_in_source']}) — "
                "confirm whether it was refunded/revoked or just delisted",
                game_id=row["game_id"],
                name=row["name"],
                evidence={
                    "platform": platform,
                    "last_seen_in_source": row["last_seen_in_source"],
                    "missed_since": cutoffs[platform],
                    "playtime_minutes": row["playtime_minutes"],
                    "acquired_at": row["acquired_at"],
                    "price_paid": row["price_paid"],
                    "price_currency": row["price_currency"],
                },
                suggested_action={
                    "tool": "add_game_to_platform",
                    "args": {
                        "game_id": row["game_id"],
                        "platform": platform,
                        "unowned_at": datetime.now(UTC).date().isoformat(),
                    },
                    "note": (
                        "ONLY if ownership really ended (refund, revoked key, "
                        "lapsed subscription). If the store page is simply gone "
                        "but you still own it, use delisted=True instead"
                    ),
                },
            )
        )

    return findings, {
        "min_missed_syncs": min_missed,
        "platforms_checked": sorted(cutoffs),
        "platforms_insufficient_history": insufficient,
        "unseen_rows": len(rows),
    }


# --- adapter: completion.unclassified ---------------------------------------

# The heuristic itself stays in tools/completion.py (unchanged, with its own
# unit tests) — this only adapts its suggestions into the finding envelope, the
# same way the migrated detectors in tools/admin.py are adapted. It was its own
# MCP tool (suggest_completion_status) until ADR 0004; it always was a
# report-only heuristic whose remedy is an update_game call, which is exactly
# what a check is.
_COMPLETION_DEFAULT_LIMIT = 25


async def _run_completion_unclassified(*, apply: bool, options: dict[str, Any]) -> CheckOutcome:
    from .completion import suggest_completion_status

    limit = options.get("limit", _COMPLETION_DEFAULT_LIMIT)
    result = await suggest_completion_status(limit)
    suggestions = result.get("suggestions", [])
    findings = [
        _finding(
            "completion.unclassified",
            "notice",
            f"'{item['name']}' looks {item['suggested_status']} but has no "
            f"completion_status set — {item['reason']}",
            game_id=item["game_id"],
            name=item["name"],
            evidence={
                "suggested_status": item["suggested_status"],
                "playtime_hours": item.get("playtime_hours"),
                "hltb_main": item.get("hltb_main"),
                "last_played": item.get("last_played"),
            },
            suggested_action={
                "tool": "update_game",
                "args": {
                    "game_id": item["game_id"],
                    "completion_status": item["suggested_status"],
                },
            },
        )
        for item in suggestions
    ]
    # Ordering is the heuristic's own confidence order (completed, then
    # evergreen, then abandoned); preserve it rather than re-sorting.
    return findings, {"limit": limit, "suggested": len(suggestions)}


# --- registry ----------------------------------------------------------------

CHECKS: dict[str, CheckSpec] = {
    spec.id: spec
    for spec in [
        _spec(
            "playtime.farming",
            description=(
                "ArchiSteamFarm card-farming sessions (many low-playtime Steam "
                "games last played on the same day)"
            ),
            network=None,
            writes_on_apply=True,
            default_severity="notice",
            runner=_run_playtime_farming,
            option_keys=frozenset({"threshold_hours", "min_games_per_day"}),
        ),
        _spec(
            "identity.same_store_collapse",
            description=(
                "One platform row carrying more than one distinct store "
                "identifier of the same type — an over-merge"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="error",
            runner=_run_identity_same_store_collapse,
        ),
        _spec(
            "identity.stranded_duplicate",
            description=(
                "Same-name, same-platform game pair where one side lacks the "
                "store identifier the other carries"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_identity_stranded_duplicate,
        ),
        _spec(
            "identity.cross_store_collapse",
            description=(
                "A multi-platform game whose Steam appid is a different IGDB "
                "game than the row's stored igdb_id (needs IGDB)"
            ),
            network="igdb",
            writes_on_apply=False,
            default_severity="error",
            runner=_run_identity_cross_store_collapse,
            option_keys=frozenset({"limit"}),
            configured=_igdb_configured,
            unconfigured_reason="unconfigured:igdb",
        ),
        _spec(
            "ownership.orphan",
            description="Primary library rows with no ownership and no wishlist entry",
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_ownership_orphan,
        ),
        _spec(
            "nesting.phantom_parent",
            description=(
                "Zero-ownership, zero-wishlist rows that other rows nest "
                "under (never deletable — merge or reclassify)"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_nesting_phantom_parent,
        ),
        _spec(
            "nesting.misclassified",
            description=(
                "Primary rows that are really nested content (DLC/soundtrack/"
                "edition/etc.), with a ready-to-apply update_game suggestion"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_nesting_misclassified,
            option_keys=frozenset({"limit", "probe_steam", "probe_offset"}),
        ),
        _spec(
            "extid.igdb_drift",
            description=(
                "A stored igdb_id whose IGDB name no longer matches the "
                "library row (needs IGDB)"
            ),
            network="igdb",
            writes_on_apply=True,
            default_severity="warning",
            runner=_run_extid_igdb_drift,
            option_keys=frozenset({"limit", "include_edition_suffix"}),
            configured=_igdb_configured,
            unconfigured_reason="unconfigured:igdb",
        ),
        _spec(
            "ownership.license_gap",
            description=(
                "An owned Steam license absent from the library (GetOwnedGames "
                "omits some retired apps; needs a stored Steam session)"
            ),
            network="steam+steamspy",
            writes_on_apply=True,
            default_severity="warning",
            runner=_run_ownership_license_gap,
            option_keys=frozenset({"limit", "retry_unresolved"}),
            configured=_steam_session_configured,
            unconfigured_reason="unconfigured:steam_session",
        ),
        # --- Phase B: new checks (9-18) --------------------------------------
        _spec(
            "nesting.superseded_base",
            description=(
                "A phantom parent (no ownership/wishlist) that DOES have an owned "
                "child — the edition-supersession shape; suggests merging the "
                "parent into its strongest owned child (the heir)"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_nesting_superseded_base,
        ),
        _spec(
            "ownership.dlc_without_base",
            description=(
                "Owned DLC/expansion rows whose base game is owned nowhere — a "
                "legitimate ownership state, reported so it stops looking like "
                "an edition supersession"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_ownership_dlc_without_base,
        ),
        _spec(
            "identity.unlinked_edition",
            description=(
                "Two owned primary rows where one name is an edition/SKU-suffixed "
                "form of the other's, but they live as unrelated sibling rows"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_identity_unlinked_edition,
        ),
        _spec(
            "nesting.dangling_parent",
            description=(
                "A parent_game_id pointing at a missing row, itself, or a row "
                "that is itself nested (broken parent chain)"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="error",
            runner=_run_nesting_dangling_parent,
        ),
        _spec(
            "wishlist.already_owned",
            description=(
                "A game_wishlist row whose (game, platform) is already owned — "
                "the fulfillment sweep should have cleared it"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_wishlist_already_owned,
        ),
        _spec(
            "playtime.snapshot_regression",
            description=(
                "A play_history snapshot with LOWER playtime than an earlier "
                "snapshot for the same game+platform (cumulative totals must "
                "never decrease)"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="error",
            runner=_run_playtime_snapshot_regression,
        ),
        _spec(
            "playtime.orphan_switch_summary",
            description=(
                "Nintendo Parental Controls playtime for an application_id with "
                "no matching nintendo_title_id identifier in the library"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_playtime_orphan_switch_summary,
        ),
        _spec(
            "spend.duplicate_purchase",
            description=(
                "Two same-family/same-name game_platforms rows sharing an "
                "identical acquisition record — likely the same purchase "
                "imported twice"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_spend_duplicate_purchase,
        ),
        _spec(
            "spend.price_anomaly",
            description=(
                "A free-source row with a nonzero price, or a price_currency "
                "that appears on exactly one acquisition row (typo smell)"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_spend_price_anomaly,
        ),
        _spec(
            "enrich.coverage",
            description=(
                "Library-wide coverage gaps (tags/igdb_id/cover/hltb_main) over "
                "owned, non-farmed primary games, with worst offenders by playtime"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_enrich_coverage,
        ),
        _spec(
            "sync.staleness",
            description=(
                "A syncable platform whose last sync is older than stale_days, "
                "or one that synced recently but has current playtime ahead of "
                "its latest play_history snapshots (silent snapshot-writer failure)"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_sync_staleness,
            option_keys=frozenset({"stale_days"}),
        ),
        _spec(
            "sync.platform_error",
            description=(
                "A platform whose last sync FAILED, or that has not succeeded "
                "within stale_hours — the run's own outcome, as opposed to "
                "sync.staleness's age of the synced data"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="warning",
            runner=_run_sync_platform_error,
            option_keys=frozenset({"stale_hours"}),
        ),
        _spec(
            "ownership.unseen_in_source",
            description=(
                "An owned row the platform's own source has stopped returning "
                "across min_missed_syncs consecutive SUCCESSFUL syncs — a "
                "refund/revoked key/lapsed subscription candidate, never an "
                "automatic conclusion"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_ownership_unseen_in_source,
            option_keys=frozenset({"min_missed_syncs", "limit"}),
        ),
        _spec(
            "completion.unclassified",
            description=(
                "An owned, played game with no completion_status that the "
                "playtime-vs-HowLongToBeat heuristic reads as completed, "
                "evergreen, or abandoned"
            ),
            network=None,
            writes_on_apply=False,
            default_severity="notice",
            runner=_run_completion_unclassified,
            option_keys=frozenset({"limit"}),
        ),
    ]
}

for _spec_obj in CHECKS.values():
    assert _spec_obj.category == _spec_obj.id.split(".", 1)[0]
del _spec_obj


def _all_categories() -> set[str]:
    return {spec.category for spec in CHECKS.values()}


def _resolve_selector(selector: str) -> set[str]:
    if selector in CHECKS:
        return {selector}
    if selector in _all_categories():
        return {check_id for check_id, spec in CHECKS.items() if spec.category == selector}
    valid = sorted(set(CHECKS) | _all_categories())
    raise ToolError(f"Unknown check id or category {selector!r}. Valid: {valid}")


def _resolve_run_set(checks: list[str] | None, include_network: bool) -> set[str]:
    """The set of check ids to run.

    ``include_network`` only widens the DEFAULT selection. With an explicit
    ``checks`` list the selection is exactly that list: naming a network check
    is itself sufficient to run it, and include_network must not smuggle the
    other network checks in (asking for ownership.license_gap used to also run
    extid.igdb_drift and identity.cross_store_collapse).
    """
    if checks is not None:
        run_set: set[str] = set()
        for selector in checks:
            run_set |= _resolve_selector(selector)
        return run_set
    run_set = {check_id for check_id, spec in CHECKS.items() if spec.network is None}
    if include_network:
        run_set |= {check_id for check_id, spec in CHECKS.items() if spec.network is not None}
    return run_set


def _validate_apply(apply_ids: list[str], run_set: set[str]) -> None:
    for check_id in apply_ids:
        spec = CHECKS.get(check_id)
        if spec is None:
            raise ToolError(f"Unknown check id {check_id!r} in apply. Valid: {sorted(CHECKS)}")
        if not spec.writes_on_apply:
            writers = sorted(cid for cid, s in CHECKS.items() if s.writes_on_apply)
            raise ToolError(
                f"Check {check_id!r} is report-only (no writes). apply-capable "
                f"checks: {writers}"
            )
        if check_id not in run_set:
            raise ToolError(
                f"Check {check_id!r} is in apply but not selected to run — add it "
                "to `checks` (or leave `checks` unset if it's part of the default "
                "offline set)"
            )


def _validate_options(check_options: dict[str, Any]) -> None:
    for check_id, opts in check_options.items():
        spec = CHECKS.get(check_id)
        if spec is None:
            raise ToolError(f"Unknown check id {check_id!r} in options. Valid: {sorted(CHECKS)}")
        if not isinstance(opts, dict):
            raise ToolError(f"options[{check_id!r}] must be an object")
        unknown = set(opts) - spec.option_keys
        if unknown:
            raise ToolError(
                f"Unknown option key(s) {sorted(unknown)} for check {check_id!r}. "
                f"Valid: {sorted(spec.option_keys)}"
            )


def _validate_suppression_entries(entries: list[dict] | None, label: str) -> list[dict[str, Any]]:
    if not entries:
        return []
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict) or "check" not in entry or "game_id" not in entry:
            raise ToolError(f"each {label} entry must be an object with 'check' and 'game_id'")
        check_id = entry["check"]
        if check_id not in CHECKS:
            raise ToolError(f"Unknown check id {check_id!r} in {label}. Valid: {sorted(CHECKS)}")
        game_id = entry["game_id"]
        if not isinstance(game_id, int) or isinstance(game_id, bool):
            raise ToolError(f"{label} entry game_id must be an int")
        cleaned.append({"check": check_id, "game_id": game_id})
    return cleaned


async def _load_suppressions() -> list[dict[str, Any]]:
    raw = await get_meta(SUPPRESSIONS_META_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


async def _save_suppressions(entries: list[dict[str, Any]]) -> None:
    await set_meta(SUPPRESSIONS_META_KEY, json.dumps(entries))


def _merge_suppressions(
    current: list[dict[str, Any]], new_entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    existing = {(e["check"], e["game_id"]) for e in current}
    result = list(current)
    added = 0
    for entry in new_entries:
        key = (entry["check"], entry["game_id"])
        if key not in existing:
            result.append(entry)
            existing.add(key)
            added += 1
    return result, added


def _remove_suppressions(
    current: list[dict[str, Any]], remove_entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    remove_keys = {(e["check"], e["game_id"]) for e in remove_entries}
    result = [e for e in current if (e["check"], e["game_id"]) not in remove_keys]
    return result, len(current) - len(result)


def _max_severity(findings: list[dict[str, Any]]) -> str | None:
    if not findings:
        return None
    return max((f["severity"] for f in findings), key=lambda s: _SEVERITY_RANK.get(s, -1))


def _catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": spec.id,
            "category": spec.category,
            "description": spec.description,
            "network": spec.network,
            "writes_on_apply": spec.writes_on_apply,
            "default_severity": spec.default_severity,
            "options": sorted(spec.option_keys),
        }
        for spec in CHECKS.values()
    ]


async def run_library_checks(
    checks: list[str] | None = None,
    include_network: bool = False,
    limit_per_check: int = 25,
    apply: list[str] | None = None,
    options: dict[str, dict[str, Any]] | None = None,
    list_checks: bool = False,
    suppress: list[dict[str, Any]] | None = None,
    unsuppress: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the selected checks and return findings + bookkeeping. See CHECKS."""
    if list_checks:
        return {
            "findings": [],
            "summary": {},
            "checks_run": [],
            "checks_skipped": [],
            "applied": {},
            "errors": [],
            "suppressed_count": 0,
            "suppressions_changed": 0,
            "catalog": _catalog(),
        }

    apply_ids = list(apply or [])
    check_options = options or {}
    run_set = _resolve_run_set(checks, include_network)
    _validate_apply(apply_ids, run_set)
    _validate_options(check_options)

    suppress_entries = _validate_suppression_entries(suppress, "suppress")
    unsuppress_entries = _validate_suppression_entries(unsuppress, "unsuppress")
    suppressions = await _load_suppressions()
    suppressions_changed = 0
    if suppress_entries:
        suppressions, added = _merge_suppressions(suppressions, suppress_entries)
        suppressions_changed += added
    if unsuppress_entries:
        suppressions, removed = _remove_suppressions(suppressions, unsuppress_entries)
        suppressions_changed += removed
    if suppress_entries or unsuppress_entries:
        await _save_suppressions(suppressions)
    suppressed_keys = {(s["check"], s["game_id"]) for s in suppressions}

    findings: list[dict[str, Any]] = []
    summary: dict[str, dict[str, Any]] = {}
    checks_run: list[str] = []
    checks_skipped: list[dict[str, Any]] = []
    applied: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    suppressed_count = 0

    for check_id, spec in CHECKS.items():
        if check_id not in run_set:
            if spec.network is not None:
                checks_skipped.append({"check": check_id, "reason": "not_selected_network"})
            continue
        if spec.network is not None and spec.configured is not None and not spec.configured():
            checks_skipped.append({"check": check_id, "reason": spec.unconfigured_reason})
            continue

        try:
            check_findings, extras = await spec.runner(
                apply=check_id in apply_ids, options=check_options.get(check_id, {})
            )
        except Exception as exc:  # noqa: BLE001 - per-check isolation: one bad check never kills the run
            errors.append({"check": check_id, "error": str(exc)})
            continue

        checks_run.append(check_id)
        kept = []
        for finding in check_findings:
            key = (finding["check"], finding.get("game_id"))
            if finding.get("game_id") is not None and key in suppressed_keys:
                suppressed_count += 1
                continue
            kept.append(finding)

        truncated = bool(limit_per_check) and len(kept) > limit_per_check
        if truncated:
            kept = kept[:limit_per_check]

        findings.extend(kept)
        summary[check_id] = {
            "findings": len(kept),
            "max_severity": _max_severity(kept),
            "truncated": truncated,
            **extras,
        }
        if check_id in apply_ids:
            applied[check_id] = extras

    return {
        "findings": findings,
        "summary": summary,
        "checks_run": checks_run,
        "checks_skipped": checks_skipped,
        "applied": applied,
        "errors": errors,
        "suppressed_count": suppressed_count,
        "suppressions_changed": suppressions_changed,
        "catalog": [],
    }
