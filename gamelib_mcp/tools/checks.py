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

Phase A ships the 8 migrated checks (one runner covers both ``ownership.orphan``
and ``nesting.phantom_parent``). Phase B appends checks 9-18 from the design doc
to ``CHECKS`` — the registry, envelope, and selection semantics here are built so
that is a pure addition.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError

from ..data.db import get_meta, set_meta
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
        # Phase B: superseded_base takes owned_child_count > 0 (the edition-
        # supersession shape gets a concrete merge suggestion there); Phase A
        # reports every phantom parent here regardless of owned_child_count.
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
    result = await revalidate_igdb_matches(dry_run=not apply, limit=limit)
    findings = [
        _finding(
            "extid.igdb_drift",
            "warning",
            f"'{m['name']}' is linked to IGDB id {m['igdb_id']} "
            f"('{m['igdb_name']}') — names don't match",
            game_id=m["game_id"],
            name=m["name"],
            evidence={
                "igdb_id": m["igdb_id"],
                "igdb_name": m["igdb_name"],
                "classification_reset": m["classification_reset"],
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
    extras = {
        "igdb_configured": result["igdb_configured"],
        "checked": result["checked"],
        "reset_count": result["reset_count"],
        "classification_reset_count": result["classification_reset_count"],
        "skipped_overridden": result["skipped_overridden"],
        "unresolved_igdb_ids": result["unresolved_igdb_ids"],
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

    findings = []
    for entry in result.get(mint_key, []):
        findings.append(
            _finding(
                "ownership.license_gap",
                "warning",
                f"Owned Steam license {entry['appid']} ('{entry.get('name')}') "
                "is absent from the library",
                name=entry.get("name"),
                evidence={
                    "appid": entry["appid"],
                    "name": entry.get("name"),
                    "classified_type": "game",
                    "would_mint": not apply,
                },
                suggested_action=(
                    None
                    if apply
                    else {
                        "tool": "check_library",
                        "args": {
                            "checks": ["ownership.license_gap"],
                            "apply": ["ownership.license_gap"],
                        },
                        "note": apply_suggestion_note,
                    }
                ),
            )
        )
    for entry in result.get(delisted_key, []):
        findings.append(
            _finding(
                "ownership.license_gap",
                "warning",
                f"Retired Steam license {entry['appid']} ('{entry.get('name')}') "
                "is absent from the library",
                name=entry.get("name"),
                evidence={
                    "appid": entry["appid"],
                    "name": entry.get("name"),
                    "classified_type": "retired_game",
                    "would_mint": not apply,
                },
                suggested_action=(
                    None
                    if apply
                    else {
                        "tool": "check_library",
                        "args": {
                            "checks": ["ownership.license_gap"],
                            "apply": ["ownership.license_gap"],
                        },
                        "note": delisted_suggestion_note,
                    }
                ),
            )
        )

    extras = {
        k: v
        for k, v in result.items()
        if k not in ("minted", "minted_delisted", "would_mint", "would_mint_delisted")
    }
    return findings, extras


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
            option_keys=frozenset({"limit"}),
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
    if checks is None:
        run_set = {check_id for check_id, spec in CHECKS.items() if spec.network is None}
    else:
        run_set = set()
        for selector in checks:
            run_set |= _resolve_selector(selector)
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
        except Exception as exc:  # per-check isolation: one bad check never kills the run
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
