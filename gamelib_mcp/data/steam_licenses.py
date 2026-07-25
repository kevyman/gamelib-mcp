"""Steam license audit — ownership from the account's own license list.

``IPlayerService/GetOwnedGames`` (steam_xml.py) is the primary ownership sync,
but it silently omits some retired/delisted apps even with
``skip_unvetted_apps=0`` — apps the account still holds a license for and can
install/play (observed in prod: Burnout Paradise: The Ultimate Box, appid
24740, "Owned" per the account's license list, absent from GetOwnedGames).
Because the sync only ever ADDS rows, those games simply never get a platform
row: silent ownership loss.

The logged-in store endpoint ``/dynamicstore/userdata/`` returns
``rgOwnedApps`` — every appid the account holds a license for, retired or not.
This module diffs that list against the synced library and classifies each
missing appid so ownership never depends on an app still being purchasable:

* store appdetails says type "game"  → mint an owned Steam row (through
  bulk_upsert_steam_library, so identifier/name resolution and every
  anti-collapse guard apply);
* appdetails says any other type (dlc/music/demo/tool/…) → recorded, no games
  row — nested/tool content is catalog data, not a library item (ADR 0002);
* appdetails has nothing (the retired case) → SteamSpy name lookup. SteamSpy
  retains data for retired GAMES but generally not DLC/tools, so a hit names
  the app and mints an owned row flagged ``delisted=1``; a miss is recorded
  as unresolved (most likely retired DLC) for manual review.

Outcomes persist per-appid in the ``steam_license_audit`` meta key, so every
appid is classified exactly once across runs; each call probes at most
``limit`` new appids through the shared, quota-budgeted store gate and reports how
many remain. Auth reuses the same Steam session the purchase importer uses
(minted from the ``steam_refresh`` token, or the legacy ``steam_store`` cookies)
via ``data/steam_session.py`` — no new credential.

The inverse transition is handled by the primary sync: an audited app that
later reappears in GetOwnedGames gets its ``delisted`` flag cleared there
(delistings are reversed — GTA IV: Complete Edition superseded the retired
standalone GTA IV).
"""

import json
import logging
from datetime import datetime, timezone

import httpx

from .db import (
    STEAM_APP_ID,
    bulk_upsert_steam_library,
    get_db,
    get_meta,
    set_meta,
    set_steam_delisted,
)
from .steam_store import fetch_store_appdetails
from .steamspy import fetch_steamspy_name
from .title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

USERDATA_URL = "https://store.steampowered.com/dynamicstore/userdata/"

# meta key holding {appid_str: {"outcome": str, "name": str|None, "at": iso}}.
AUDIT_META_KEY = "steam_license_audit"

# meta key holding the last run's remaining-unclassified count, so offline
# consumers (detect_orphan_games) can say whether the audit has caught up
# without a network round-trip.
AUDIT_REMAINING_META_KEY = "steam_license_audit_remaining"

# Default probe cap per call: bounded refresh-time cost on the shared
# Quota-budgeted store gate; the audit is incremental, so steady-state runs probe 0.
DEFAULT_PROBE_LIMIT = 25

_AUTH_ERROR = (
    "Steam dynamicstore userdata returned no owned apps — the Steam store session "
    "is not authenticating (the cookie is missing, expired, or was exported for the "
    "wrong Steam domain, e.g. steamcommunity.com instead of the store). Preferred fix: "
    "create_session_ingest_link(provider=\"steam_refresh\") and paste the long-lived "
    "steamRefresh_steam token from login.steampowered.com (it mints the correct store "
    "cookie automatically). Legacy fallback: create_session_ingest_link(provider="
    "\"steam_store\") with a fresh export from a store.steampowered.com tab."
)

# Outcomes that are final; appids carrying one are never re-probed (unresolved
# is retriable via retry_unresolved=True).
_FINAL_OUTCOMES = frozenset({"minted", "minted_delisted", "skipped_non_game"})


def is_license_audit_configured() -> bool:
    """True when a Steam session (refresh token or legacy store cookies) is available."""
    from .steam_session import is_steam_session_configured

    return is_steam_session_configured()


async def fetch_owned_steam_appids(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> set[int]:
    """The account's owned appids from dynamicstore/userdata (license list).

    Raises RuntimeError when the session isn't authenticated: a logged-out
    request "succeeds" with empty arrays, and an account with zero owned apps
    is not this deployment's account (STEAM_API_KEY/STEAM_ID are required
    config), so empty == expired cookie, never an empty library.
    """
    # Mint fresh cookies from the refresh token (or fall back to legacy static
    # cookies). Lazy import mirrors the purchase importer and breaks the cycle.
    from .steam_session import load_steam_web_cookies

    cookies = await load_steam_web_cookies(transport=transport)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
    }
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=30, transport=transport
    ) as client:
        resp = await client.get(USERDATA_URL, headers=headers)
        if resp.status_code in (401, 403):
            raise RuntimeError(_AUTH_ERROR)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(_AUTH_ERROR)

    owned = payload.get("rgOwnedApps") if isinstance(payload, dict) else None
    if not isinstance(owned, list) or not owned:
        raise RuntimeError(_AUTH_ERROR)
    return {int(appid) for appid in owned if isinstance(appid, (int, str)) and str(appid).isdigit()}


async def _library_steam_appids() -> set[int]:
    """Every appid already attached to an OWNED Steam platform row.

    Restricted to owned=1: an appid sitting on an unowned manual/legacy stub
    still needs healing — the audit is reconciling ownership from licenses, so
    leaving such appids in the "missing" set lets bulk_upsert's identifier
    resolution find the stub and flip it owned.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT gpi.identifier_value
               FROM game_platform_identifiers gpi
               JOIN game_platforms gp ON gp.id = gpi.game_platform_id
               WHERE gpi.identifier_type = ?
                 AND gp.platform = 'steam'
                 AND gp.owned = 1""",
            (STEAM_APP_ID,),
        )
    return {
        int(row["identifier_value"])
        for row in rows
        if str(row["identifier_value"]).isdigit()
    }


async def _load_audit_map() -> dict[str, dict]:
    raw = await get_meta(AUDIT_META_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def audit_steam_licenses(
    limit: int = DEFAULT_PROBE_LIMIT,
    retry_unresolved: bool = False,
    *,
    mint: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Diff owned licenses against the library and heal missing ownership.

    Probes at most ``limit`` not-yet-classified appids this call (0 = no cap);
    everything else stays queued and is reported in ``remaining``. Returns an
    ``unconfigured`` status dict when no store session is stored; raises
    RuntimeError on an expired session (callers catch per-source, like the
    purchase importers).

    ``mint=False`` (report mode, used by check_library's ownership.license_gap)
    still probes and classifies appids but writes nothing: no games/game_platforms
    row is minted, and the appid's outcome is NOT recorded in the persisted
    ``steam_license_audit`` meta map — otherwise a preview run would make an
    appid look "settled" and vanish from future runs (real or preview) without
    ever actually healing it. What would have minted lands in ``would_mint``/
    ``would_mint_delisted`` instead of ``minted``/``minted_delisted``.
    """
    if not is_license_audit_configured():
        return {
            "status": "unconfigured",
            "error_summary": (
                "No Steam store session found — run "
                "create_session_ingest_link(provider=\"steam_refresh\") (preferred: the "
                "long-lived steamRefresh_steam token from login.steampowered.com) to "
                "enable the license audit. Legacy fallback: provider=\"steam_store\"."
            ),
        }

    owned = await fetch_owned_steam_appids(transport=transport)
    library = await _library_steam_appids()
    audit = await _load_audit_map()

    def _settled(appid: int) -> bool:
        entry = audit.get(str(appid))
        if entry is None:
            return False
        outcome = entry.get("outcome")
        if outcome in _FINAL_OUTCOMES or (
            isinstance(outcome, str) and outcome.startswith("skipped_")
        ):
            return True
        return outcome == "unresolved" and not retry_unresolved

    missing = sorted(a for a in owned - library if not _settled(a))
    to_probe = missing if not limit else missing[:limit]

    now = datetime.now(timezone.utc).isoformat()
    minted: list[dict] = []
    minted_delisted: list[dict] = []
    would_mint: list[dict] = []
    would_mint_delisted: list[dict] = []
    skipped: list[dict] = []
    unresolved: list[int] = []

    for appid in to_probe:
        data = await fetch_store_appdetails(appid)
        if data:
            app_type = (data.get("type") or "").strip().lower()
            raw_name = data.get("name") or ""
            prepared = prepare_catalog_title(raw_name)
            if app_type == "game" and prepared:
                if mint:
                    await bulk_upsert_steam_library(
                        [{"appid": appid, "name": prepared}], synced_at=now
                    )
                    # The flag records "absent from GetOwnedGames, ownership from
                    # the license audit" — set even with a live store page, and
                    # cleared by the primary sync if the API ever returns the app.
                    await set_steam_delisted([appid], True)
                    outcome = "minted"
                    minted.append({"appid": appid, "name": prepared})
                else:
                    outcome = "would_mint"
                    would_mint.append({"appid": appid, "name": prepared})
            else:
                # DLC/music/demo/tool — or a junk title prepare_catalog_title
                # rejects. Nested/tool content never mints a games row.
                outcome = f"skipped_{app_type or 'non_game'}"
                skipped.append({"appid": appid, "type": app_type or None, "name": raw_name or None})
            if mint:
                audit[str(appid)] = {"outcome": outcome, "name": raw_name or None, "at": now}
            continue

        # Retired from the store entirely. SteamSpy still knows real games.
        spy_name = await fetch_steamspy_name(appid)
        prepared = prepare_catalog_title(spy_name) if spy_name else None
        if prepared:
            if mint:
                await bulk_upsert_steam_library(
                    [{"appid": appid, "name": prepared}], synced_at=now
                )
                await set_steam_delisted([appid], True)
                audit[str(appid)] = {"outcome": "minted_delisted", "name": spy_name, "at": now}
                minted_delisted.append({"appid": appid, "name": prepared})
            else:
                would_mint_delisted.append({"appid": appid, "name": prepared})
        else:
            if mint:
                audit[str(appid)] = {"outcome": "unresolved", "name": None, "at": now}
            unresolved.append(appid)

    if mint:
        if to_probe:
            await set_meta(AUDIT_META_KEY, json.dumps(audit))
        await set_meta(AUDIT_REMAINING_META_KEY, str(max(0, len(missing) - len(to_probe))))

    result = {
        "status": "ok",
        "owned_licenses": len(owned),
        "library_appids": len(library),
        "unclassified": len(missing),
        "probed": len(to_probe),
        "minted": minted,
        "minted_delisted": minted_delisted,
        "skipped_non_game": skipped,
        "unresolved": unresolved,
        "remaining": max(0, len(missing) - len(to_probe)),
        "mint": mint,
        "would_mint": would_mint,
        "would_mint_delisted": would_mint_delisted,
    }
    logger.info(
        "Steam license audit: owned=%d library=%d probed=%d minted_delisted=%d "
        "skipped=%d unresolved=%d remaining=%d",
        len(owned),
        len(library),
        len(to_probe),
        len(minted_delisted),
        len(skipped),
        len(unresolved),
        result["remaining"],
    )
    return result
