"""Fetch and push the Steam wishlist.

Pull: IWishlistService/GetWishlist.

Auth reuses STEAM_API_KEY/STEAM_ID (same as steam_xml.py's owned-games fetch).
Unlike GetOwnedGames, this endpoint returns only appid/priority/date_added —
no title — so a wishlist item with no existing game_platforms row needs a
follow-up Steam Store lookup (steam_store.fetch_app_name) to name it.

A wishlist item that isn't owned anywhere yet gets a games row but no
game_platforms row (see game_wishlist's schema note) — so unlike an owned sync,
there's no steam_appid identifier to attach it to. Resolution order per item:
(1) an existing game_platforms/identifier row for the appid (owned, unchanged
from before); (2) the wishlist's OWN store_identifier — a re-synced item
resolves this way with ZERO network calls, since a prior sync already recorded
which game_id it belongs to (`get_wishlist_game_id_by_store_identifier`), so
name resolution (and the ~160-per-run rate-gated store lookups it used to cost
every single sync) is now first-sync-only; (3) only when both miss, a name
lookup (steam_store.fetch_app_name, falling back to steamspy.fetch_steamspy_name
for delisted apps appdetails no longer serves), guarded against attaching onto
a row that already owns steam under a DIFFERENT appid (see the collision guard
in fetch_wishlist below) before falling back to upsert_game's exact-name
matching — the same fallback GOG already relies on for lacking a stable store id.

date_added is read defensively (_parse_steam_added_at accepts epoch or ISO,
else falls back to sync time) — unlike the DekuDeals export, this endpoint's
exact response shape hasn't been confirmed against a live account yet.

Removal reconciliation: a game taken off your Steam wishlist (without being
bought) is deleted from game_wishlist too, via delete_stale_wishlist_entries —
but only when every fetched item resolved to a game_id this round. If any
item couldn't be resolved (a malformed entry, or both fetch_app_name AND the
SteamSpy fallback failing to name an unowned/delisted item), the removal pass
is skipped entirely rather than risk deleting a wishlist entry that's still
there and we simply failed to re-confirm. Before 2026-08, a single permanently
unnameable appid (a fully delisted app with no SteamSpy record either) meant
removal reconciliation never ran again for Steam; the SteamSpy fallback fixes
the two prod skips that caused this (2026-08-07 diagnosis), so the next sync
after this change may legitimately delete stale rows reconciliation had been
unable to reach for a while.

Push: push_to_steam_wishlist (issue #110 phase 2) adds one appid to the real
account wishlist, so a game wishlisted in-app also shows up on Steam itself.
Two write routes exist; neither had been exercised authenticated as of the
2026-08-06 investigation that shaped this implementation, so both are wired
with the official Web API preferred and the storefront AJAX endpoint as
fallback:

- Route B (preferred): IWishlistService/AddToWishlist, the same service
  family as the GetWishlist pull above. Auth is a Steam web access token —
  the JWT segment embedded in the steamLoginSecure cookie
  (steam_session.extract_web_access_token) — passed as the access_token query
  param.
- Route A (fallback): store.steampowered.com/api/addtowishlist, the same
  cookie-authenticated AJAX endpoint the logged-in store page itself calls.
  Needs steamLoginSecure + a matching sessionid (CSRF double-submit between
  cookie and form field).

Removal (taking an item off the real Steam wishlist) is deliberately out of
scope for phase 2 — this module only ever adds.
"""

import logging
import os
import re
import time
from datetime import UTC, datetime

import httpx

from .db import (
    STEAM_APP_ID,
    delete_stale_wishlist_entries,
    exact_name_steam_conflict,
    get_game_by_identifier,
    get_wishlist_game_id_by_store_identifier,
    upsert_game,
    upsert_wishlist_entry,
)
from .steam_session import _USER_AGENT as _STEAM_USER_AGENT
from .steam_session import (
    _decode_jwt_claims,
    extract_web_access_token,
    load_steam_web_cookies,
    new_sessionid,
)
from .steam_store import fetch_app_name
from .steam_xml import STEAM_API_KEY, STEAM_ID
from .steamspy import fetch_steamspy_name
from .title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
_ADD_WEBAPI_URL = "https://api.steampowered.com/IWishlistService/AddToWishlist/v1/"
_ADD_STOREFRONT_URL = "https://store.steampowered.com/api/addtowishlist"

# Both push failures look like auth rejection, not a dead network: route B
# returning 401/403, or route A's 200-with-success:false shape (the endpoint
# answers HTTP 200 even when the session is invalid/missing). Either means the
# stored Steam web session is no longer good and needs re-minting.
_AUTH_REJECTED_ERROR = (
    "Steam rejected the wishlist push (session no longer valid) — run "
    "create_session_ingest_link(provider=\"steam_refresh\") to re-store the "
    "Steam session token."
)
_TRANSIENT_PUSH_ERROR = (
    "Steam wishlist push failed transiently (network error on both the web API "
    "and storefront routes) — retry shortly."
)


class SteamWishlistPushError(RuntimeError):
    """A push to the real Steam wishlist failed; the message is user-facing."""


# One minted Steam web session is reused across pushes: with the preferred
# refresh-token configuration, every load_steam_web_cookies() call replays the
# full finalizelogin mint (finalize + per-domain transfer requests), so a
# 200-item batch push would fire hundreds of authentication requests before
# any actual push. Expiry comes from the steamLoginSecure JWT's exp claim
# (minus a safety margin); a token without a readable exp gets a conservative
# fallback TTL. An auth-shaped push failure drops the cache so the next call
# re-mints instead of replaying a session Steam already rejected.
_SESSION_TTL_FALLBACK_SECONDS = 600.0
_SESSION_EXPIRY_MARGIN_SECONDS = 60.0
_session_cache: dict[str, str] | None = None
_session_cache_expires_at = 0.0


def _invalidate_session_cache() -> None:
    global _session_cache, _session_cache_expires_at
    _session_cache = None
    _session_cache_expires_at = 0.0


def _session_expiry(cookies: dict[str, str]) -> float:
    """Wall-clock expiry for a minted session, from the access token's ``exp``.

    A near-expiry token yields a timestamp in the past, which simply means "no
    reuse" — the next push re-mints rather than pushing with a token about to
    lapse mid-request.
    """
    claims = _decode_jwt_claims(
        extract_web_access_token(cookies.get("steamLoginSecure", ""))
    )
    exp = claims.get("exp")
    now = time.time()
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > now:
        return min(float(exp) - _SESSION_EXPIRY_MARGIN_SECONDS, now + 86400.0)
    return now + _SESSION_TTL_FALLBACK_SECONDS


class _RouteAttemptError(Exception):
    """Internal: one write route failed. Never raised past push_to_steam_wishlist.

    ``auth_shaped`` distinguishes "the session looks rejected" (route B 401/403,
    or route A's HTTP-200-success:false shape) from a plain network failure, so
    the caller can pick which guidance to give once both routes are exhausted.
    """

    def __init__(self, detail: str, *, auth_shaped: bool) -> None:
        super().__init__(detail)
        self.detail = detail
        self.auth_shaped = auth_shaped


def _safe_json(response: httpx.Response) -> object:
    """``response.json()``, or None on a non-JSON/malformed body."""
    try:
        return response.json()
    except ValueError:
        return None


def _extract_wishlist_count(payload: object, *keys: str) -> int | None:
    """Best-effort wishlist count from a push response body.

    Neither write route's exact response shape has been confirmed against a
    live authenticated account (see module docstring), so this probes a few
    plausible key names rather than asserting one.
    """
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


async def _push_via_webapi(client: httpx.AsyncClient, appid: int, access_token: str) -> dict:
    """Route B: IWishlistService/AddToWishlist, authenticated via access_token.

    Raises :class:`_RouteAttemptError` on any non-2xx response or transport
    error; 401/403 are treated as auth-shaped.
    """
    try:
        resp = await client.post(
            _ADD_WEBAPI_URL,
            params={"access_token": access_token},
            data={"appid": str(appid)},
        )
    except httpx.HTTPError as exc:
        raise _RouteAttemptError(
            f"webapi network error ({exc.__class__.__name__})", auth_shaped=False
        ) from exc

    if not (200 <= resp.status_code < 300):
        raise _RouteAttemptError(
            f"webapi HTTP {resp.status_code}", auth_shaped=resp.status_code in (401, 403)
        )

    payload = _safe_json(resp)
    response_body = payload.get("response") if isinstance(payload, dict) else None
    wishlist_count = _extract_wishlist_count(response_body, "wishlist_count", "wishlistCount")
    return {"appid": appid, "via": "webapi", "wishlist_count": wishlist_count}


async def _push_via_storefront(
    client: httpx.AsyncClient, appid: int, cookies: dict[str, str]
) -> dict:
    """Route A: storefront AJAX addtowishlist, authenticated via cookies.

    ``sessionid`` is a CSRF double-submit — the form field must equal the
    cookie — so a cookie export missing it (a legacy static export can lack
    one) gets a freshly minted one used for both. Raises
    :class:`_RouteAttemptError` on a non-200 response, a network error, or the
    endpoint's HTTP-200-``success: false`` shape (the latter is auth-shaped:
    that is exactly how the unauthenticated probe on 2026-08-06 responded).
    """
    sessionid = cookies.get("sessionid") or new_sessionid()
    # Set the Cookie header directly rather than httpx's per-request `cookies=`
    # kwarg (deprecated: persistence across requests on a shared client is
    # ambiguous there, and this request needs exactly these two, no more).
    cookie_header = f"steamLoginSecure={cookies['steamLoginSecure']}; sessionid={sessionid}"
    try:
        resp = await client.post(
            _ADD_STOREFRONT_URL,
            headers={"Cookie": cookie_header},
            data={"appid": str(appid), "sessionid": sessionid},
        )
    except httpx.HTTPError as exc:
        raise _RouteAttemptError(
            f"storefront network error ({exc.__class__.__name__})", auth_shaped=False
        ) from exc

    if resp.status_code != 200:
        raise _RouteAttemptError(f"storefront HTTP {resp.status_code}", auth_shaped=False)

    payload = _safe_json(resp)
    if not (isinstance(payload, dict) and payload.get("success")):
        raise _RouteAttemptError("storefront HTTP 200 success:false", auth_shaped=True)

    wishlist_count = _extract_wishlist_count(payload, "wishlistCount", "wishlist_count")
    return {"appid": appid, "via": "storefront", "wishlist_count": wishlist_count}


async def push_to_steam_wishlist(
    appid: int, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    """Add ``appid`` to the account's real Steam wishlist (issue #110 phase 2).

    Contract: returns ``{"appid": int, "via": "webapi" | "storefront",
    "wishlist_count": int | None}`` on success; raises
    :class:`SteamWishlistPushError` with an actionable message on any failure
    (no session configured, expired session, endpoint rejection, network).
    Removal (taking an item off the real wishlist) is out of scope.

    Tries route B (IWishlistService/AddToWishlist) first — the same service
    family ``fetch_wishlist`` above already pulls from — then falls back to
    route A (the storefront AJAX endpoint) on any route B failure, reusing the
    same session cookies for both. Both routes were verified to exist (status
    codes / response shape on an unauthenticated request) on 2026-08-06, but
    neither has been exercised with a real logged-in session yet, hence the
    fallback rather than trusting route B alone. The minted session is cached
    in-process and reused across pushes (see ``_session_cache`` above) so a
    batch of pushes authenticates once, not once per item; an auth-shaped
    failure drops the cache.

    Raises ``SteamWishlistPushError`` (preserving the message verbatim) when
    no Steam session is configured or the stored refresh token has expired —
    both surface as a ``RuntimeError`` from ``load_steam_web_cookies``. When
    both write routes fail, the error names both failures briefly and, if
    either failure looks like a rejected session (route B 401/403, or route
    A's 200/success:false), points at re-running
    ``create_session_ingest_link(provider="steam_refresh")``; a failure that
    is pure network error on both routes instead says to retry.
    """
    global _session_cache, _session_cache_expires_at
    if _session_cache is not None and time.time() < _session_cache_expires_at:
        cookies = _session_cache
    else:
        try:
            cookies = await load_steam_web_cookies(transport=transport)
        except RuntimeError as exc:
            raise SteamWishlistPushError(str(exc)) from exc
        if not cookies.get("steamLoginSecure"):
            # A minted session always carries it; only a malformed legacy static
            # export can get here. Fail with guidance, not a KeyError.
            raise SteamWishlistPushError(
                "Stored Steam session has no steamLoginSecure cookie — re-store "
                "it via create_session_ingest_link(provider=\"steam_refresh\")."
            )
        _session_cache = cookies
        _session_cache_expires_at = _session_expiry(cookies)

    async with httpx.AsyncClient(
        timeout=30, transport=transport, headers={"User-Agent": _STEAM_USER_AGENT}
    ) as client:
        try:
            access_token = extract_web_access_token(cookies["steamLoginSecure"])
            return await _push_via_webapi(client, appid, access_token)
        except _RouteAttemptError as webapi_failure:
            try:
                return await _push_via_storefront(client, appid, cookies)
            except _RouteAttemptError as storefront_failure:
                logger.warning(
                    "Steam wishlist push failed for appid %s: webapi=%s storefront=%s",
                    appid,
                    webapi_failure.detail,
                    storefront_failure.detail,
                )
                detail = f"webapi: {webapi_failure.detail}; storefront: {storefront_failure.detail}"
                if webapi_failure.auth_shaped or storefront_failure.auth_shaped:
                    # Don't replay a session Steam just rejected — the next
                    # push should re-mint from the refresh token.
                    _invalidate_session_cache()
                    raise SteamWishlistPushError(f"{_AUTH_REJECTED_ERROR} ({detail})") from storefront_failure
                raise SteamWishlistPushError(f"{_TRANSIENT_PUSH_ERROR} ({detail})") from storefront_failure


def _basic_whitespace_clean(name: str) -> str:
    """Collapse/trim whitespace only — no trademark or suffix stripping.

    Used for the raw store/SteamSpy name in the collision-guard fallback
    below: normalize_catalog_title bundles trademark-glyph removal together
    with the trailing-variant (edition/suffix) stripping in one non-separable
    pass, and the raw name is deliberately kept UN-suffix-stripped there (that
    stripping is what caused the collision in the first place), so this
    reimplements only the cheap, uncontroversial whitespace part rather than
    picking apart normalize_catalog_title's internals.
    """
    return re.sub(r"\s+", " ", name).strip()


def _parse_steam_added_at(value: object) -> str | None:
    """Best-effort parse of a wishlist item's date_added into an ISO string.

    Steam Web API timestamps are conventionally Unix epoch seconds (int, or a
    numeric string); accept a plain ISO string too in case the field is ever
    returned pre-formatted. Returns None if absent/unparseable so the caller
    can fall back to sync time — never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        if value.isdigit():
            return _parse_steam_added_at(int(value))
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return None
        return value
    return None


async def fetch_wishlist() -> dict:
    """Fetch the Steam wishlist and upsert entries into game_wishlist.

    Returns {"added": int, "matched": int, "skipped": int, "removed": int}, or
    an "unconfigured" status dict (matching sync_dekudeals_wishlist's shape)
    if STEAM_API_KEY/STEAM_ID aren't set. removed is 0 whenever the removal
    reconciliation didn't run (see module docstring).

    added/matched report what happened to the WISHLIST row: added = a
    game_wishlist row was minted this run, matched = the row already existed
    and was updated in place. They deliberately do NOT report how the game
    row was resolved — wishlist-only games have no game_platforms/identifier
    rows, so before this fixed an unowned item resolved by name on EVERY sync,
    and counting that as "added" made a routine no-op re-sync read as 171
    additions (2026-08-06 prod test) and left added useless for spotting
    genuinely new items. As of 2026-08-07, a wishlist-only item resolves via
    its own stored ``game_wishlist.store_identifier`` on every sync after the
    first — no name lookup, no network call — so a name lookup (and the
    collision guard around it, see the per-item loop below) only runs the
    first time an appid is ever seen.
    """
    steam_api_key = os.getenv("STEAM_API_KEY", STEAM_API_KEY)
    steam_id = os.getenv("STEAM_ID", STEAM_ID)
    if not steam_api_key or not steam_id:
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "removed": 0,
            "sync_status": "unconfigured",
            "error_summary": "STEAM_API_KEY and STEAM_ID environment variables must be set",
            "error_classification": "missing_configuration",
        }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            WISHLIST_URL,
            params={"key": steam_api_key, "steamid": steam_id},
        )
        resp.raise_for_status()
        items = resp.json().get("response", {}).get("items", [])

        added = matched = skipped = 0
        # Second precision to match what the Steam sync writes from date_added
        # (epoch seconds) — a manual add and a synced row shouldn't be
        # distinguishable by timestamp format.
        fallback_now = datetime.now(UTC).isoformat(timespec="seconds")
        resolved_game_ids: set[int] = set()
        all_resolved = True

        for item in items:
            appid = item.get("appid")
            if appid is None:
                skipped += 1
                all_resolved = False
                continue
            item_added_at = _parse_steam_added_at(item.get("date_added")) or fallback_now

            # Resolve the game row, in order:
            #   1. an owned game_platforms/identifier row for this appid.
            #   2. this wishlist's OWN store_identifier from a prior sync —
            #      zero network calls, and the reason a re-sync no longer
            #      re-fetches ~160 store names every run.
            #   3. a name lookup (store, then SteamSpy for delisted apps),
            #      guarded against attaching onto a row that already owns
            #      steam under a DIFFERENT appid (see below).
            existing = await get_game_by_identifier(STEAM_APP_ID, str(appid))
            if existing is not None:
                game_id = existing["id"]
            else:
                game_id = await get_wishlist_game_id_by_store_identifier("steam", str(appid))

            if game_id is None:
                raw_name = await fetch_app_name(appid, client=client)
                if raw_name is None:
                    # appdetails says the app doesn't exist at all (fully
                    # delisted, e.g. appid 654050 "JYDGE") — SteamSpy retains
                    # names for retired games the store has forgotten, the
                    # same fallback the license audit already uses.
                    raw_name = await fetch_steamspy_name(appid)
                prepared_title = prepare_catalog_title(raw_name) if raw_name else None
                if prepared_title is None or raw_name is None:
                    skipped += 1
                    all_resolved = False
                    continue

                # Anti-collapse collision guard: Steam never returns a game
                # you already own on Steam in your wishlist (root CLAUDE.md,
                # "Game identity (anti-collapse)" — name is a cross-platform
                # reconciliation key, never within-platform), so an exact-name
                # match onto a row that owns steam under a DIFFERENT appid is
                # always wrong. Left unguarded, this attached the wishlisted
                # Dead Space 2023 remake (appid 1693980) onto the owned 2008
                # original, and TES IV: Oblivion Remastered (2623190) onto the
                # owned 2006 original once prepare_catalog_title's suffix
                # stripping erased the "Remastered"/"Oblivion Remastered" tail
                # — clear_fulfilled_wishlist_entries then deleted both,
                # silently, on every sync (2026-08-07 diagnosis). The guard
                # also fires on a row whose steam appid identifier differs
                # from this item's, owned or NOT — a refunded copy (ADR 0007)
                # keeps its identifiers, and identity doesn't lapse with
                # ownership.
                if await exact_name_steam_conflict(prepared_title, appid):
                    raw_cleaned = _basic_whitespace_clean(raw_name)
                    if raw_cleaned != prepared_title and not await exact_name_steam_conflict(
                        raw_cleaned, appid
                    ):
                        # The stripped suffix caused the collision (Oblivion
                        # Remastered case) — the raw, unstripped name is the
                        # honest identity and doesn't collide itself.
                        game_id = await upsert_game(appid, raw_cleaned)
                    else:
                        # Same name even unstripped (Dead Space case): mint a
                        # SEPARATE games row rather than collapsing two
                        # different games that happen to share a title within
                        # one platform.
                        game_id = await upsert_game(
                            appid, prepared_title, match_existing_by_name=False
                        )
                else:
                    game_id = await upsert_game(appid, prepared_title)

            # Count by what happened to the wishlist row, not by how the game
            # resolved (see docstring). The upsert reports created atomically,
            # so a concurrent sync/manual add can't skew the counters.
            upserted = await upsert_wishlist_entry(
                game_id, "steam", wishlisted_at=item_added_at, source="steam", store_identifier=str(appid)
            )
            if upserted["created"]:
                added += 1
            else:
                matched += 1
            resolved_game_ids.add(game_id)

    removed = 0
    if all_resolved:
        removed = await delete_stale_wishlist_entries("steam", "steam", resolved_game_ids)
    elif items:
        logger.info(
            "Skipping Steam wishlist removal-reconciliation: %d item(s) unresolved this sync", skipped
        )

    logger.info(
        "Steam wishlist sync: added=%d matched=%d skipped=%d removed=%d", added, matched, skipped, removed
    )
    return {"added": added, "matched": matched, "skipped": skipped, "removed": removed}
