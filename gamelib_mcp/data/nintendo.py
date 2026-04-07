"""Nintendo Switch sync — nxapi play-activity (primary) with VGCS ownership fallback.

PRIMARY: nxapi CLI (requires NINTENDO_SESSION_TOKEN + nxapi installed)
  - Uses `nxapi nso play-activity --json`
  - Provides play history with accurate playtime in minutes
  - Only launched titles appear (Nintendo platform limitation — no workaround)

FALLBACK: Nintendo Account VGCS GraphQL API (requires NINTENDO_COOKIES_FILE)
  - Uses browser session cookies from accounts.nintendo.com
  - Provides full digital library ownership including unplayed titles
  - No playtime data — playtime_minutes stored as None
  - Activated automatically when nxapi fails or credentials are absent
  - To set/refresh cookies: use the set_nintendo_session MCP tool

Platform: all titles stored as "switch2" (NX and OUNCE both map to switch2).
"""

import asyncio
import json
import logging
import os
import re
import shutil

import httpx
from bs4 import BeautifulSoup

from gamelib_mcp.data.db import (
    load_fuzzy_candidates,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB

logger = logging.getLogger(__name__)

NXAPI_BIN = os.getenv("NXAPI_BIN", "nxapi")
NINTENDO_TITLE_ID = "nintendo_title_id"
PLATFORM = "switch2"

# VGCS GraphQL endpoint
_VGCS_URL = "https://accounts.nintendo.com/portal/vgcs/"
_SAVANNA_URL = "https://wb.lp1.savanna.srv.nintendo.net/graphql"
_VGCS_QUERY = """
query getVgcsVgcs(
  $idToken: String!
  $country: CountryCode!
  $language: LanguageCode!
  $shopId: Int!
  $limit: Int!
  $nasLanguage: String!
  $offset: Int!
  $order: RequestableVgcViewOrder!
  $sortBy: RequestableVgcViewSortBy!
  $vgcViewType: VgcViewTypeInput
  $vgcViewStatus: VgcViewStatusInput
) @inContext(country: $country, language: $language, shopId: $shopId) {
  account {
    vgc {
      vgcViews(
        idToken: $idToken,
        limit: $limit,
        nasLanguage: $nasLanguage,
        offset: $offset,
        order: $order,
        sortBy: $sortBy,
        isHidden: false,
        vgcViewType: $vgcViewType,
        vgcViewStatus: $vgcViewStatus,
      ) {
        offsetInfo { total offset }
        views {
          id
          applicationId
          applicationName
          apparentPlatform
        }
      }
    }
  }
}
"""

# shopId by Nintendo region flag (from #state JSON on the VGCS page)
_SHOP_ID_BY_REGION = {
    "isRegionNOA": 1,
    "isRegionNAL": 2,
    "isRegionNOE": 3,
}


# ---------------------------------------------------------------------------
# nxapi helpers
# ---------------------------------------------------------------------------

def _nxapi_available() -> bool:
    return shutil.which(NXAPI_BIN) is not None


async def _run_nxapi(*args: str) -> str:
    """Run an nxapi CLI command and return stdout."""
    # Pass token via env var rather than CLI flag to avoid exposure in ps output.
    token = os.environ.get("NINTENDO_SESSION_TOKEN")
    env = {**os.environ}
    if token:
        env["NXAPI_SESSION_TOKEN"] = token
    proc = await asyncio.create_subprocess_exec(
        NXAPI_BIN, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"nxapi {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode()[:300]}"
        )
    return stdout.decode()


async def fetch_nintendo_play_history() -> list[dict]:
    """
    Fetch play history via `nxapi nso play-activity --json`.

    Returns a list of dicts with keys:
      name (str), playtime_minutes (int | None), title_id (str | None)

    Playtime: nxapi reports totalPlayTime in minutes (confirmed via nxapi source and
    NSO-RPC which divides by 60 to get hours — see github.com/samuelthomas2774/nxapi
    issue #8). No unit conversion applied.
    """
    raw = await _run_nxapi("nso", "play-activity", "--json")
    data = json.loads(raw)

    items = data if isinstance(data, list) else data.get("items", data.get("titles", []))

    results = []
    for item in items:
        name = item.get("name") or item.get("title") or item.get("gameName")
        if not name:
            continue

        minutes = (
            item.get("totalPlayTime")
            or item.get("playingMinutes")
            or item.get("totalPlayedMinutes")
        )

        title_id = item.get("titleId") or item.get("id")
        if not title_id:
            shop_uri = item.get("shopUri", "")
            m = re.search(r"/([0-9a-fA-F]{16})/?(?:[?#]|$)", shop_uri)
            if m:
                title_id = m.group(1)

        results.append({
            "name": str(name),
            "playtime_minutes": int(minutes) if minutes else None,
            "title_id": str(title_id) if title_id else None,
        })

    return results


# ---------------------------------------------------------------------------
# VGCS fallback helpers
# ---------------------------------------------------------------------------

def _load_vgcs_cookies() -> dict[str, str] | None:
    """Load Nintendo session cookies from NINTENDO_COOKIES_FILE."""
    configured_path = os.getenv("NINTENDO_COOKIES_FILE", "data/nintendo_cookies.json")
    candidate_paths = [configured_path]
    fallback_path = "data/nintendo_cookies.json"
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
            logger.warning("Failed to load Nintendo cookies from %s: %s", path, exc)
            return None

    if raw is None:
        return None

    # Accept both {name: value} dict and Cookie Editor array [{name, value, ...}]
    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return None


def _parse_vgcs_page(html: str) -> tuple[str, str, str, int]:
    """
    Parse the VGCS page HTML.
    Returns (id_token, savanna_client_id, country_code, shop_id).
    """
    soup = BeautifulSoup(html, "html.parser")

    data_div = soup.find(id="data")
    if not data_div:
        raise RuntimeError("VGCS page missing #data div — session cookies may have expired")
    raw_data = data_div.get("data-json")
    if not raw_data:
        raise RuntimeError("VGCS #data div missing data-json attribute — page structure may have changed")
    page_data = json.loads(raw_data)
    id_token = page_data["idToken"]
    savanna_client_id = page_data["savannaClientId"]

    state_div = soup.find(id="state")
    if not state_div:
        raise RuntimeError("VGCS page missing #state div")
    raw_state = state_div.get("data-json")
    if not raw_state:
        raise RuntimeError("VGCS #state div missing data-json attribute — page structure may have changed")
    state = json.loads(raw_state)

    # Extract two-letter country code from "COUNTRY_NAME_BE" → "BE"
    country_label = state.get("user", {}).get("countryLabel", "")
    m = re.search(r"COUNTRY_NAME_(\w+)$", country_label)
    country = m.group(1) if m else "US"

    shop_id = next(
        (v for k, v in _SHOP_ID_BY_REGION.items() if state.get(k)),
        4,  # Japan default
    )

    return id_token, savanna_client_id, country, shop_id


async def fetch_nintendo_library_vgcs() -> list[dict]:
    """
    Fetch the full digital library via the Nintendo Account VGCS GraphQL API.

    Returns a list of dicts with keys:
      name (str), playtime_minutes (None — not available from this source),
      title_id (str | None)

    Requires NINTENDO_COOKIES_FILE to point at a valid session cookie JSON file.
    """
    cookies = _load_vgcs_cookies()
    if not cookies:
        raise RuntimeError("No Nintendo session cookies found (NINTENDO_COOKIES_FILE not set or missing)")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=30) as client:
        page_resp = await client.get(_VGCS_URL, headers=headers)
        page_resp.raise_for_status()

        id_token, savanna_client_id, country, shop_id = _parse_vgcs_page(page_resp.text)

        all_views: list[dict] = []
        limit = 300
        offset = 0

        while True:
            payload = {
                "query": _VGCS_QUERY,
                "variables": {
                    "idToken": id_token,
                    "country": country,
                    "language": "en",
                    "shopId": shop_id,
                    "limit": limit,
                    "nasLanguage": "en-US",
                    "offset": offset,
                    "order": "DESC",
                    "sortBy": "ACTIVATED_DATE",
                    "vgcViewType": None,
                    "vgcViewStatus": None,
                },
                "operationName": "getVgcsVgcs",
            }
            gql_resp = await client.post(
                _SAVANNA_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Nintendo-Savanna-Client-Id": savanna_client_id,
                    "Origin": "https://accounts.nintendo.com",
                    "Referer": "https://accounts.nintendo.com/",
                },
            )
            gql_resp.raise_for_status()
            gql_data = gql_resp.json()

            vgc_views = (
                gql_data
                .get("data", {})
                .get("account", {})
                .get("vgc", {})
                .get("vgcViews", {})
            )
            views = vgc_views.get("views", [])
            all_views.extend(views)

            total = vgc_views.get("offsetInfo", {}).get("total", 0)
            offset += len(views)
            if offset >= total or not views:
                break

    results = []
    for view in all_views:
        name = view.get("applicationName")
        if not name:
            continue
        results.append({
            "name": str(name),
            "playtime_minutes": None,
            "title_id": view.get("applicationId"),
        })
    return results


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

async def sync_nintendo() -> dict:
    """
    Sync Nintendo Switch titles into game_platforms (platform="switch2").

    Strategy:
    1. Try nxapi play-activity (requires NINTENDO_SESSION_TOKEN + nxapi binary).
       - Provides play history with playtime in minutes.
    2. If nxapi is unavailable or fails, fall back to VGCS GraphQL
       (requires NINTENDO_COOKIES_FILE with valid session cookies).
       - Provides full digital library ownership; playtime stored as None.
    3. If neither is available, skip silently.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    entries: list[dict] | None = None

    has_nxapi_token = bool(os.getenv("NINTENDO_SESSION_TOKEN"))
    has_vgcs_cookies = bool(_load_vgcs_cookies())

    # --- attempt nxapi ---
    if has_nxapi_token and _nxapi_available():
        try:
            entries = await fetch_nintendo_play_history()
            logger.info("Nintendo: fetched %d titles via nxapi", len(entries))
        except Exception as exc:
            logger.warning("nxapi play-activity failed, trying VGCS fallback: %s", exc)

    # --- attempt VGCS fallback ---
    if entries is None and has_vgcs_cookies:
        try:
            entries = await fetch_nintendo_library_vgcs()
            logger.info("Nintendo: fetched %d titles via VGCS fallback", len(entries))
        except Exception as exc:
            logger.warning("VGCS fallback failed: %s", exc)

    if entries is None:
        if not has_nxapi_token and not has_vgcs_cookies:
            logger.info(
                "Nintendo sync skipped — set NINTENDO_SESSION_TOKEN or NINTENDO_COOKIES_FILE"
            )
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for entry in entries:
        name = entry["name"]
        if not name:
            skipped += 1
            continue

        igdb_platform_id = PLATFORM_TO_IGDB.get(PLATFORM)
        game_id, igdb_game = await resolve_and_link_game(name, igdb_platform_id, candidates)
        if game_id in candidates:
            matched += 1
        else:
            candidates[game_id] = name
            added += 1

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform=PLATFORM,
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

        if entry["title_id"]:
            await upsert_game_platform_identifier(
                platform_id, NINTENDO_TITLE_ID, entry["title_id"]
            )

    logger.info(
        "Nintendo sync: added=%d matched=%d skipped=%d",
        added, matched, skipped,
    )
    return {"added": added, "matched": matched, "skipped": skipped}
