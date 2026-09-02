"""Session-file save paths for the cookie/token ingest flow.

Every function here is reached through ``session_ingest.py`` (the single-use
``/ingest/{nonce}`` paste form), not as an MCP tool: a pasted cookie export or
one-time login link is exactly the kind of secret that must stay out of the
chat. ``session_ingest`` resolves them by name off THIS module —
``IngestProvider.setter_name`` for the save path, ``prepare_name`` for the one
interactive login (Nintendo Parental Controls) — so a provider entry there and
a function here are two halves of one registry.

Split out of ``tools/admin.py`` unchanged; ``create_session_ingest_link`` (the
MCP tool that mints the link) stays there.
"""

import asyncio
import json
import logging
import os
from collections.abc import Callable

from fastmcp.exceptions import ToolError

from ..data.db import default_data_dir

logger = logging.getLogger(__name__)


def _steam_login_secure_audience(cookie_value: str) -> list[str] | None:
    """Best-effort read of a ``steamLoginSecure`` token's ``aud`` claim.

    Steam issues a *separate* ``steamLoginSecure`` per domain, each a JWT whose
    ``aud`` names the domain it authenticates (``web:store`` vs ``web:community``
    vs ``web:help``). The value is ``steamid||<JWT>`` (the ``||`` is often
    URL-encoded). Returns the audience strings, or ``None`` when it can't be
    determined — callers must not block on ambiguity, only on a confirmed
    wrong-domain token. Reuses ``steam_session._decode_jwt_claims`` (no signature
    check needed — we only read a public claim).
    """
    import urllib.parse

    from ..data.steam_session import _decode_jwt_claims

    parts = urllib.parse.unquote(cookie_value).split("||")
    if len(parts) < 2:
        return None
    aud = _decode_jwt_claims(parts[1]).get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        return aud
    return None


def _require_cookie(cookie_name: str, hint: str) -> Callable[[dict[str, str]], None]:
    """Build a validator that rejects an export lacking ``cookie_name``.

    Cookie *names* are not secrets (unlike values), so listing what was found
    helps the user see they exported the wrong thing. The ToolError text surfaces
    on the ingest paste page, so it must never contain a cookie value.
    """

    def _validate(normalized: dict[str, str]) -> None:
        if cookie_name not in normalized:
            found = ", ".join(sorted(normalized)) or "(none)"
            raise ToolError(
                f"This export is missing the required '{cookie_name}' cookie, so it "
                f"won't work. {hint} Cookies found in your paste: {found}."
            )

    return _validate


def _validate_steam_store_cookies(normalized: dict[str, str]) -> None:
    """Reject a steam_store export that can't authenticate the store.

    Two silent-failure traps this catches at paste time (both bit the owner):
    - ``steamLoginSecure`` absent entirely.
    - ``steamLoginSecure`` present but issued for the *wrong Steam domain*
      (e.g. exported from steamcommunity.com → ``aud: web:community``). The store
      endpoints answer such a request with ``200`` + an empty logged-out payload,
      not a ``401``, so the failure otherwise looks like "cookie missing/expired".
    """
    _require_cookie(
        "steamLoginSecure",
        "Export it from a store.steampowered.com tab while logged in — or better, "
        'use the long-lived refresh token: create_session_ingest_link(provider="steam_refresh").',
    )(normalized)
    aud = _steam_login_secure_audience(normalized["steamLoginSecure"])
    if aud is not None and not any("store" in a for a in aud):
        raise ToolError(
            f"That 'steamLoginSecure' cookie is for the wrong Steam domain (audience "
            f"{aud}, not the store) — Steam issues a different cookie per domain and "
            "the license audit / purchase import only work with the store one. "
            "Re-export it from a store.steampowered.com tab (NOT steamcommunity.com) "
            '— or better, use create_session_ingest_link(provider="steam_refresh"), '
            "which mints the correct store cookie for you automatically."
        )



def _write_private_json(path: str, payload: object) -> None:
    """Write ``payload`` as JSON readable by the owning user only (mode 0600).

    Everything routed through here is a long-lived credential — store cookies,
    the ~200-day Steam refresh token, the Parental Controls session token — so
    the mode is set at creation (no window at the umask default, typically
    0644) and re-applied on overwrite, which also tightens a file written
    before this guard existed the next time it is saved. The containing
    directory is deliberately left alone: in production it is the shared
    ``/data`` mount that the backup user has to traverse.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode argument applies only when the file is created; a file saved
    # before this guard keeps its 0644 through O_TRUNC, so tighten the open
    # descriptor BEFORE the secret is written, not after.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

def _save_session_cookies(
    cookies: str,
    env_var: str,
    default_filename: str,
    label: str,
    validate: Callable[[dict[str, str]], None] | None = None,
    bare_value_cookie: str | None = None,
) -> dict:
    """Normalize a pasted cookie-export JSON and save it as {name: value}.

    Shared by every cookie-based session setter. Accepts either a JSON object
    ({"cookie_name": "value", ...}) or a Cookie Editor / EditThisCookie array
    ([{"name": ..., "value": ...}, ...]); saves to the path in ``env_var``,
    falling back to ``default_filename`` inside ``default_data_dir()`` (the
    DB's writable directory — a mounted ``/data`` volume in production) so a
    relative ``data/`` that the non-root container process can't create never
    triggers ``PermissionError: [Errno 13] Permission denied: 'data'``.

    ``bare_value_cookie`` enables single-value pastes: for providers that hinge on
    one cookie (the Steam token/login cookie), a paste that isn't JSON (doesn't
    start with ``{`` or ``[``) is treated as that cookie's raw value, so the user
    can paste the value straight from DevTools without hand-formatting JSON.

    ``validate`` runs on the normalized {name: value} dict *before* anything is
    written, so a known-bad paste (missing/wrong-domain cookie) is rejected with
    a clear ToolError instead of being saved as a silently useless file.
    """
    text = cookies.strip()
    if bare_value_cookie and text[:1] not in ("{", "["):
        # A cookie export is always a JSON object/array; anything else is the
        # bare cookie value pasted directly (e.g. the raw steamRefresh_steam
        # token, which may itself be `<steamid>||<jwt>`).
        normalized: dict = {bare_value_cookie: text}
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid JSON: {exc}") from exc

        if isinstance(raw, list):
            normalized = {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
        elif isinstance(raw, dict):
            normalized = raw
        else:
            raise ToolError("Expected a JSON object or array")

    if not normalized:
        raise ToolError("No valid cookies found in input")

    if validate is not None:
        validate(normalized)

    path = os.getenv(env_var) or str(default_data_dir() / default_filename)
    _write_private_json(path, normalized)

    logger.info("%s session cookies saved to %s (%d cookies)", label, path, len(normalized))
    return {"cookie_count": len(normalized), "path": path}


async def set_nintendo_session(cookies: str) -> dict:
    """
    Store Nintendo Account session cookies (accounts.nintendo.com).

    This one login session drives BOTH Switch ownership sync (VGCS) AND eShop
    purchase-history import (import_purchases sources=["eshop"], via a silent
    OAuth handshake) — no separate cookie export for purchases.

    Accepts either:
    - A JSON object: {"cookie_name": "value", ...}
    - A JSON array (Cookie Editor / EditThisCookie format):
      [{"name": "...", "value": "..."}, ...]

    How to get your cookies:
    1. Open https://accounts.nintendo.com/portal/vgcs/ in your browser
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in NINTENDO_COOKIES_FILE
    (defaults to nintendo_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "NINTENDO_COOKIES_FILE", "nintendo_cookies.json", "Nintendo"
    )


async def set_humble_session(cookies: str) -> dict:
    """
    Store Humble Bundle session cookies for purchase-history import.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). Only the ``_simpleauth_sess`` cookie is strictly needed,
    but exporting/storing all humblebundle.com cookies is fine.

    How to get your cookies:
    1. Open https://www.humblebundle.com/ in your browser (logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in HUMBLE_COOKIES_FILE
    (defaults to humble_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "HUMBLE_COOKIES_FILE", "humble_cookies.json", "Humble Bundle"
    )


async def set_epic_session(cookies: str) -> dict:
    """
    Store Epic Games session cookies for purchase-history import.

    Epic's order history lives on the account WEBSITE (www.epicgames.com), not
    in the launcher API — Legendary's session (data/epic.py) covers ownership
    and playtime but cannot see orders or prices, so this is a separate,
    browser-exported session.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). Export ALL cookies from a signed-in www.epicgames.com tab —
    the site's auth is spread across several cookies (EPIC_BEARER_TOKEN and
    friends), so no single-cookie paste is supported.

    How to get your cookies:
    1. Open https://www.epicgames.com/account/transactions in your browser
       (logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in EPIC_COOKIES_FILE
    (defaults to epic_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies, "EPIC_COOKIES_FILE", "epic_cookies.json", "Epic Games"
    )


async def set_steam_refresh_session(cookies: str) -> dict:
    """
    Store the long-lived Steam refresh token for on-demand session minting.

    This is the PREFERRED Steam session path. The ``steamRefresh_steam`` JWT set
    on ``login.steampowered.com`` (after a "remember me" login) is good for ~200
    days; ``data/steam_session.py`` replays the browser's silent mint on every run
    to produce fresh ``steamLoginSecure`` store cookies, so there is no re-pasting
    when the short-lived cookie lapses. Supersedes set_steam_store_session, which
    remains as a fallback. Unrelated to STEAM_API_KEY.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). Only the ``steamRefresh_steam`` cookie is needed.

    How to get your token:
    1. Sign in at https://login.steampowered.com/ (or store/community) with the
       "remember me" box checked, then open https://login.steampowered.com/
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON (steamRefresh_steam)
    4. Pass that JSON string to this tool

    Saved to the path in STEAM_REFRESH_TOKEN_FILE
    (defaults to steam_refresh_token.json beside the database).
    """
    return _save_session_cookies(
        cookies,
        "STEAM_REFRESH_TOKEN_FILE",
        "steam_refresh_token.json",
        "Steam refresh token",
        validate=_require_cookie(
            "steamRefresh_steam",
            "It only appears on login.steampowered.com after you sign in with the "
            "'Remember me' box checked — a store or community page export won't have it.",
        ),
        bare_value_cookie="steamRefresh_steam",
    )


async def set_steam_store_session(cookies: str) -> dict:
    """
    Store Steam store session cookies (LEGACY — prefer set_steam_refresh_session).

    The short-lived ``steamLoginSecure`` cookie lapses in ~weeks and must be
    re-pasted; set_steam_refresh_session mints it on demand from a ~200-day token
    instead. This path is kept as a fallback for deployments not yet migrated.

    Accepts the same JSON shapes as set_nintendo_session (object or Cookie
    Editor array). Only the ``steamLoginSecure`` cookie is strictly required;
    ``sessionid`` is recommended too (the history load-more endpoint wants
    it). These store.steampowered.com cookies are unrelated to STEAM_API_KEY.

    How to get your cookies:
    1. Open https://store.steampowered.com/account/ in your browser (logged in)
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in STEAM_STORE_COOKIES_FILE
    (defaults to steam_store_cookies.json beside the database).
    """
    return _save_session_cookies(
        cookies,
        "STEAM_STORE_COOKIES_FILE",
        "steam_store_cookies.json",
        "Steam store",
        validate=_validate_steam_store_cookies,
        bare_value_cookie="steamLoginSecure",
    )


async def prepare_nintendo_pctl_login() -> dict[str, str]:
    """Mint a Parental Controls sign-in URL and the PKCE verifier that redeems it.

    Step 1 of the `nintendo_pctl` ingest flow, run once per link when the form
    is first rendered. Nintendo's login URL embeds a challenge derived from a
    per-login verifier, and only that verifier can exchange the code the user
    pastes back — so it is stored on the ingest link and handed to
    set_nintendo_pctl_session below. Builds the URL locally (PKCE), no network.
    """
    import aiohttp
    from pynintendoparental.authenticator import Authenticator

    async with aiohttp.ClientSession() as session:
        auth = Authenticator(client_session=session)
        return {"login_url": auth.login_url, "verifier": auth._auth_code_verifier}


async def set_nintendo_pctl_session(
    response: str,
    *,
    state: dict[str, str] | None = None,
) -> dict:
    """
    Store the Parental Controls session token for Switch playtime sync.

    Step 2 of the `nintendo_pctl` ingest flow: `response` is the `npf…://auth`
    link copied off Nintendo's "Select this person" button (or a bare session
    token), and `state` carries the verifier from prepare_nintendo_pctl_login.
    Not an MCP tool — that link contains a one-time code redeemable for a
    long-lived token, exactly the kind of secret create_session_ingest_link
    exists to keep out of the chat.

    The Parental Controls API reports per-game playtime for any console
    registered to Parental Controls, whichever account owns each game — so
    titles played on your console under another account show up too. This is
    the playtime source for switch2 (VGCS provides ownership).

    Saved to NINTENDO_PCTL_SESSION_FILE (defaults to nintendo_pctl_session.json
    beside the database).
    """
    import aiohttp
    from pynintendoparental.authenticator import Authenticator

    from ..data.nintendo_pctl import _token_file_path

    text = (response or "").strip()
    if not text:
        raise ToolError("Nothing pasted — copy the npf:// link and try again.")

    if "session_token_code" in text or text.startswith("npf"):
        verifier = (state or {}).get("verifier")
        if not verifier:
            raise ToolError(
                "This login link expired. Ask your assistant for a fresh "
                "create_session_ingest_link(provider=\"nintendo_pctl\") link."
            )
        async with aiohttp.ClientSession() as session:
            auth = Authenticator(client_session=session)
            auth._auth_code_verifier = verifier
            try:
                await auth.async_complete_login(response_token=text)
            except Exception:
                # Deliberately NOT interpolated into the message: the failure
                # text can quote the token that was submitted, and no ingest
                # page may echo a submitted secret back (see session_ingest).
                logger.exception("Parental Controls login exchange failed")
                raise ToolError(
                    "Nintendo rejected that link. Codes are single-use and "
                    "expire quickly — sign in again with the button above and "
                    "paste the fresh link."
                ) from None
            token = auth._session_token
    else:
        token = text  # treat as a bare session token

    if not token:
        raise ToolError("No session token obtained")

    path = _token_file_path()

    def _write_token_file() -> None:
        _write_private_json(path, {"session_token": token})

    # Small write, but it runs on the event loop that is also serving the ingest
    # form request; keep the blocking filesystem calls off it.
    await asyncio.to_thread(_write_token_file)

    logger.info("Nintendo Parental Controls session token saved to %s", path)
    return {"status": "stored", "path": path}
