"""Steam web-session minting from a long-lived refresh token.

Steam's logged-in store pages (purchase history, the license list) authenticate
with a short-lived ``steamLoginSecure`` access-token cookie that lapses in about
a day. Storing that cookie directly means re-pasting constantly. Instead, when a
browser stays logged in it silently mints fresh access cookies from a much
longer-lived **refresh token** — the ``steamRefresh_steam`` JWT set on
``login.steampowered.com`` after a "remember me" login, good for ~200 days.

This module reuses *that* token (stored via
``create_session_ingest_link(provider="steam_refresh")`` →
``STEAM_REFRESH_TOKEN_FILE``) and replays the browser's silent mint on demand,
so a single stored credential authenticates every run with no keep-warm loop and
no dependence on process uptime. It mirrors the Nintendo eShop pattern in
``data/purchases/nintendo_ec.py`` (long-lived account session → short-lived
session minted per use).

The mint is the standard Steam web flow (plain httpx, no protobuf):

1. POST ``login.steampowered.com/jwt/finalizelogin`` as ``multipart/form-data``
   with ``nonce`` = the refresh token, a self-generated ``sessionid``, and a
   ``redir``. The JSON response carries ``transfer_info``: a list of per-domain
   ``{url, params}`` targets.
2. POST each transfer ``url`` (multipart) with ``steamID`` plus the entry's
   ``params`` verbatim. Each response ``Set-Cookie``s ``steamLoginSecure`` for
   its own domain into the shared jar.
3. Return the ``store.steampowered.com`` ``steamLoginSecure`` plus the generated
   ``sessionid`` (the store history load-more endpoint cross-checks the cookie
   against the form ``sessionid``, so the same value must be used for both).

Reference implementation: node-steam-session's ``getWebCookies``.

If no refresh token is configured, :func:`load_steam_web_cookies` falls back to
the legacy static ``steam_store`` cookie file so existing deployments keep
working. When the refresh token itself expires (~200 days), finalizelogin
returns no transfer targets and the caller is told to re-export it.
"""

import base64
import json
import logging
import os
import secrets

import httpx

from gamelib_mcp.data.db import default_data_dir

logger = logging.getLogger(__name__)

_FINALIZE_URL = "https://login.steampowered.com/jwt/finalizelogin"
# Steam's own web login uses this redir; the value is echoed, not followed here.
_REDIR = "https://steamcommunity.com/login/home/?goto="
_STORE_HOST = "store.steampowered.com"

_REFRESH_TOKEN_ENV = "STEAM_REFRESH_TOKEN_FILE"
_REFRESH_TOKEN_FILENAME = "steam_refresh_token.json"
# The browser cookie carrying the refresh token, exported from login.steampowered.com.
_REFRESH_COOKIE_NAME = "steamRefresh_steam"

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# Raised when finalizelogin accepts the request but returns no transfer targets
# (or rejects it outright): the long-lived refresh token has expired, so a fresh
# browser export from login.steampowered.com is the only fix.
_REFRESH_EXPIRED_ERROR = (
    "Steam refresh token has expired — sign in again with 'remember me' in your "
    "browser, then run create_session_ingest_link(provider=\"steam_refresh\") and "
    "open the link to paste a fresh steamRefresh_steam export from "
    "login.steampowered.com."
)
# Transient mint failure (network / 5xx / a transfer hop failed): retryable, and
# distinct from the expired-token case so the user isn't told to re-paste a token
# that is still fine.
_MINT_ERROR = (
    "Steam session minting failed transiently (login.steampowered.com was "
    "unreachable or errored) — retry shortly; no re-export is needed."
)
_NOT_CONFIGURED_ERROR = (
    "No Steam session is configured — run "
    "create_session_ingest_link(provider=\"steam_refresh\") (preferred: paste the "
    "long-lived steamRefresh_steam token from login.steampowered.com) or "
    "create_session_ingest_link(provider=\"steam_store\") (legacy: paste "
    "steamLoginSecure from store.steampowered.com)."
)
_STEAMID_UNRESOLVED_ERROR = (
    "Cannot mint Steam cookies: no steamID64 available. Set STEAM_ID (already "
    "required for Steam ownership sync)."
)


def _load_cookies(env_var: str, default_filename: str, label: str) -> dict[str, str] | None:
    """Load a stored cookie export as {name: value}.

    Mirrors data/purchases/nintendo_ec.py::_load_cookies (the established repo
    convention, duplicated per module): configured path (``env_var``) first, then
    the default beside the database; accepts both {name: value} and Cookie Editor
    array JSON.
    """
    fallback_path = str(default_data_dir() / default_filename)
    configured_path = os.getenv(env_var) or fallback_path
    candidate_paths = [configured_path]
    if configured_path != fallback_path:
        candidate_paths.append(fallback_path)

    raw = None
    for path in candidate_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            break
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed to load %s from %s: %s", label, path, exc)
            return None

    if raw is None:
        return None

    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _load_steam_refresh_token() -> str | None:
    """Return the stored Steam refresh token, or None when not configured.

    Reads ``STEAM_REFRESH_TOKEN_FILE`` (or ``steam_refresh_token.json`` beside the
    database). The export usually contains the single ``steamRefresh_steam``
    cookie; a lone-value export under any key is accepted too.
    """
    raw = _load_cookies(_REFRESH_TOKEN_ENV, _REFRESH_TOKEN_FILENAME, "Steam refresh token")
    if not raw:
        return None
    token = raw.get(_REFRESH_COOKIE_NAME)
    if token:
        return token
    if len(raw) == 1:
        return next(iter(raw.values()))
    return None


def _decode_jwt_claims(token: str) -> dict:
    """Base64url-decode a JWT's claim segment. No signature verification.

    We only read the ``sub`` (steamID64) and ``exp`` claims, so the signature is
    irrelevant and no crypto dependency is needed. Any malformed input → {}.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _resolve_steamid(refresh_token: str) -> str:
    """The steamID64 for the transfer step: STEAM_ID first, else the token's sub.

    STEAM_ID is already required config and is the id used everywhere else, so it
    wins; the JWT ``sub`` is a fallback. A mismatch would mint cookies for the
    wrong account, so it is logged.
    """
    env_id = os.getenv("STEAM_ID")
    token_sub = _decode_jwt_claims(refresh_token).get("sub")
    if env_id and token_sub and str(env_id) != str(token_sub):
        logger.warning(
            "STEAM_ID (%s) does not match the Steam refresh token subject (%s); "
            "using STEAM_ID.",
            env_id,
            token_sub,
        )
    steamid = str(env_id) if env_id else (str(token_sub) if token_sub else None)
    if not steamid or not steamid.isdigit():
        raise RuntimeError(_STEAMID_UNRESOLVED_ERROR)
    return steamid


def _new_sessionid() -> str:
    """A fresh sessionid in Steam's format (24 hex chars)."""
    return secrets.token_hex(12)


def _extract_login_secure(client: httpx.AsyncClient) -> str | None:
    """The store-domain ``steamLoginSecure`` from the jar after the transfer dance.

    Steam issues a distinct ``steamLoginSecure`` per domain; both consumers scrape
    ``store.steampowered.com``, so that domain's value is preferred. Falls back to
    any ``steamLoginSecure`` present.
    """
    candidates: dict[str, str] = {}
    for cookie in client.cookies.jar:
        if cookie.name == "steamLoginSecure" and cookie.value:
            candidates[cookie.domain or ""] = cookie.value
    if not candidates:
        return None
    for domain, value in candidates.items():
        if _STORE_HOST in domain:
            return value
    return next(iter(candidates.values()))


async def mint_steam_web_cookies(
    refresh_token: str,
    steamid: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """Mint fresh store cookies from a refresh token via finalizelogin → transfer.

    Returns ``{"steamLoginSecure": ..., "sessionid": ...}`` ready to drop into an
    ``httpx.AsyncClient(cookies=...)``. Raises ``_REFRESH_EXPIRED_ERROR`` when the
    token is rejected (no transfer targets / 401 / no cookie produced) and
    ``_MINT_ERROR`` on a transient failure.
    """
    sessionid = _new_sessionid()
    headers = {"User-Agent": _USER_AGENT, "Referer": _REDIR}
    # multipart/form-data is required — a urlencoded body (httpx data=) is
    # rejected by finalizelogin. files={k: (None, v)} forces multipart.
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, transport=transport
    ) as client:
        try:
            resp = await client.post(
                _FINALIZE_URL,
                files={
                    "nonce": (None, refresh_token),
                    "sessionid": (None, sessionid),
                    "redir": (None, _REDIR),
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(_MINT_ERROR) from exc

        if resp.status_code in (401, 403):
            raise RuntimeError(_REFRESH_EXPIRED_ERROR)
        if resp.status_code >= 500:
            raise RuntimeError(_MINT_ERROR)
        if resp.status_code != 200:
            raise RuntimeError(_REFRESH_EXPIRED_ERROR)
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(_REFRESH_EXPIRED_ERROR)

        transfer_info = payload.get("transfer_info") if isinstance(payload, dict) else None
        if not isinstance(transfer_info, list) or not transfer_info:
            # finalizelogin accepted the request but named no cookie targets:
            # the refresh token is no longer valid.
            raise RuntimeError(_REFRESH_EXPIRED_ERROR)

        # finalizelogin echoes the authoritative steamID for the token; prefer it.
        authoritative_id = str(payload.get("steamID") or steamid)

        for entry in transfer_info:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            files: dict[str, tuple[None, str]] = {"steamID": (None, authoritative_id)}
            params = entry.get("params")
            if isinstance(params, dict):
                for key, value in params.items():
                    files[str(key)] = (None, "" if value is None else str(value))
            try:
                await client.post(url, files=files, headers=headers)
            except httpx.HTTPError as exc:
                raise RuntimeError(_MINT_ERROR) from exc

        login_secure = _extract_login_secure(client)
        if not login_secure:
            raise RuntimeError(_REFRESH_EXPIRED_ERROR)
        return {"steamLoginSecure": login_secure, "sessionid": sessionid}


async def load_steam_web_cookies(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, str]:
    """Shared entry point for both Steam store-session consumers.

    Mints fresh cookies from the refresh token when configured; otherwise falls
    back to the legacy static ``steam_store`` cookie file; otherwise raises. Both
    ``fetch_steam_purchases`` and ``fetch_owned_steam_appids`` call this so they
    authenticate identically.
    """
    token = _load_steam_refresh_token()
    if token:
        steamid = _resolve_steamid(token)
        return await mint_steam_web_cookies(token, steamid, transport=transport)

    # Lazy import breaks the cycle (steam_history has no top-level dependency here;
    # this module reads its legacy loader only on the fallback path).
    from .purchases.steam_history import _load_steam_cookies

    legacy = _load_steam_cookies()
    if legacy:
        return legacy
    raise RuntimeError(_NOT_CONFIGURED_ERROR)


def is_steam_session_configured() -> bool:
    """True when either a refresh token or legacy static store cookies are stored."""
    if _load_steam_refresh_token():
        return True
    from .purchases.steam_history import _load_steam_cookies

    return bool(_load_steam_cookies())
