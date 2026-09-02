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
  anti-collapse guard apply). The row is NOT flagged ``delisted``: a live
  store page proves the app is still listed, and GetOwnedGames also omits
  apps that are simply never-launched (the normal state of a bundle redeemed
  this week);
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
from datetime import UTC, datetime

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

# Report-mode classifications: probe facts cached by mint=False runs so the
# scan advances across report-only calls without re-spending store quota on
# already-classified appids. Deliberately NOT final — the appid stays in the
# missing set (and keeps re-appearing as a would_mint finding, served from
# this cache) until a mint run actually heals it. The cached "name" is the
# prepared catalog title, ready to mint from directly.
_REPORT_CLASSIFIED = frozenset({"classified_game", "classified_retired_game"})


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


async def _game_ids_for_appids(appids: list[int]) -> dict[int, int]:
    """{appid: games.id} for freshly minted Steam rows.

    A mint run reports the row it created, so a caller (check_library's
    ownership.license_gap) can say "minted as game_id N" instead of leaving the
    finding indistinguishable from "still absent".
    """
    if not appids:
        return {}
    resolved: dict[int, int] = {}
    async with get_db() as db:
        for start in range(0, len(appids), 500):
            chunk = [str(a) for a in appids[start : start + 500]]
            placeholders = ",".join("?" * len(chunk))
            rows = await db.execute_fetchall(
                f"""SELECT gpi.identifier_value AS appid, gp.game_id AS game_id
                    FROM game_platform_identifiers gpi
                    JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                    WHERE gpi.identifier_type = ?
                      AND gp.platform = 'steam'
                      AND gpi.identifier_value IN ({placeholders})""",
                (STEAM_APP_ID, *chunk),
            )
            for row in rows:
                if str(row["appid"]).isdigit():
                    resolved[int(row["appid"])] = row["game_id"]
    return resolved


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
    probes and classifies appids but never touches library data: what would
    have minted lands in ``would_mint``/``would_mint_delisted`` instead of
    ``minted``/``minted_delisted``. Classifications ARE persisted so repeated
    report runs advance the scan instead of re-probing the same first batch:
    skips and unresolved are recorded exactly as in mint mode (they are facts
    about the appid, identical either way), while mintable games are cached as
    non-final ``classified_game``/``classified_retired_game`` entries — still
    counted in ``unclassified``, re-emitted as would-mint results on every
    report run (from cache, no re-probe), never consuming probe slots, and
    healed directly from the cached name by the next mint run. A preview can
    therefore never make a real gap look "settled" and vanish.
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
    # Split cached report-mode classifications out of the probe queue: they are
    # served/minted from cache below, so probe slots only go to new appids and
    # a report-only scan can walk the whole gap list across calls.
    cached_classified = [
        a for a in missing if (audit.get(str(a)) or {}).get("outcome") in _REPORT_CLASSIFIED
    ]
    cached_set = set(cached_classified)
    unclassified_queue = [a for a in missing if a not in cached_set]
    to_probe = unclassified_queue if not limit else unclassified_queue[:limit]

    now = datetime.now(UTC).isoformat()
    minted: list[dict] = []
    minted_delisted: list[dict] = []
    would_mint: list[dict] = []
    would_mint_delisted: list[dict] = []
    skipped: list[dict] = []
    unresolved: list[int] = []
    audit_dirty = False

    for appid in cached_classified:
        entry = audit[str(appid)]
        prepared = entry.get("name")
        if not prepared:
            # Junk cache entry — drop it so the appid re-probes next call.
            del audit[str(appid)]
            audit_dirty = True
            continue
        retired = entry.get("outcome") == "classified_retired_game"
        if mint:
            await bulk_upsert_steam_library([{"appid": appid, "name": prepared}], synced_at=now)
            if retired:
                # Only the store-lookup-failed classification means "retired";
                # a live store page (classified_game) must not be flagged.
                await set_steam_delisted([appid], True)
            outcome = "minted_delisted" if retired else "minted"
            audit[str(appid)] = {"outcome": outcome, "name": prepared, "at": now}
            audit_dirty = True
            (minted_delisted if retired else minted).append({"appid": appid, "name": prepared})
        else:
            (would_mint_delisted if retired else would_mint).append(
                {"appid": appid, "name": prepared}
            )

    # One client for the whole probe loop: each fetch_store_appdetails call
    # would otherwise open (and tear down) its own connection pool per appid.
    # 15s matches the per-request timeout that call already passes.
    async with httpx.AsyncClient(timeout=15) as probe_client:
        for appid in to_probe:
            data = await fetch_store_appdetails(appid, probe_client)
            if data:
                app_type = (data.get("type") or "").strip().lower()
                raw_name = data.get("name") or ""
                prepared = prepare_catalog_title(raw_name)
                if app_type == "game" and prepared:
                    if mint:
                        await bulk_upsert_steam_library(
                            [{"appid": appid, "name": prepared}], synced_at=now
                        )
                        # NO delisted flag here: appdetails just served a live store
                        # page for this appid, so it is not retired. GetOwnedGames
                        # omits never-launched apps too (a freshly redeemed bundle
                        # is the common case) — absence there is not evidence of a
                        # delisting, and flagging it made every delisted-filtered
                        # view under-report.
                        audit[str(appid)] = {"outcome": "minted", "name": raw_name or None, "at": now}
                        minted.append({"appid": appid, "name": prepared})
                    else:
                        audit[str(appid)] = {"outcome": "classified_game", "name": prepared, "at": now}
                        would_mint.append({"appid": appid, "name": prepared})
                else:
                    # DLC/music/demo/tool — or a junk title prepare_catalog_title
                    # rejects. Nested/tool content never mints a games row. The skip
                    # is a mint-independent fact, so report mode records it too.
                    audit[str(appid)] = {
                        "outcome": f"skipped_{app_type or 'non_game'}",
                        "name": raw_name or None,
                        "at": now,
                    }
                    skipped.append({"appid": appid, "type": app_type or None, "name": raw_name or None})
                audit_dirty = True
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
                    audit[str(appid)] = {
                        "outcome": "classified_retired_game",
                        "name": prepared,
                        "at": now,
                    }
                    would_mint_delisted.append({"appid": appid, "name": prepared})
            else:
                # Mint-independent fact (retriable via retry_unresolved either way).
                audit[str(appid)] = {"outcome": "unresolved", "name": None, "at": now}
                unresolved.append(appid)
            audit_dirty = True

    if mint and (minted or minted_delisted):
        # Report the rows this run actually created, so a healed license is
        # visibly healed rather than re-reading as "absent from the library".
        game_ids = await _game_ids_for_appids(
            [entry["appid"] for entry in (*minted, *minted_delisted)]
        )
        for entry in (*minted, *minted_delisted):
            entry["game_id"] = game_ids.get(entry["appid"])

    if audit_dirty:
        await set_meta(AUDIT_META_KEY, json.dumps(audit))
    # Remaining = appids still needing a PROBE (cached classifications don't —
    # they only await a mint run). Report mode now advances this too.
    await set_meta(
        AUDIT_REMAINING_META_KEY, str(max(0, len(unclassified_queue) - len(to_probe)))
    )

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
        "remaining": max(0, len(unclassified_queue) - len(to_probe)),
        "classified_from_cache": len(cached_classified),
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
