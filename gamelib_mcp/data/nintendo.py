"""Nintendo Switch sync — VGCS ownership.

OWNERSHIP: Nintendo Account VGCS GraphQL API (requires NINTENDO_COOKIES_FILE)
  - Uses browser session cookies from accounts.nintendo.com
  - Provides full digital library ownership including unplayed titles
  - No playtime data — playtime_minutes stored as None
  - To set/refresh cookies: use create_session_ingest_link(provider="nintendo")

PLAYTIME: Parental Controls API (NINTENDO_PCTL_SESSION_FILE, via create_session_ingest_link(provider="nintendo_pctl"))
  - Handled separately in nintendo_pctl.py

Platform: all titles stored as "switch2" (NX and OUNCE both map to switch2).
"""

import json
import logging
import os
import re

import httpx
from bs4 import BeautifulSoup

from gamelib_mcp.data.db import (
    adopt_platform_identifier,
    default_data_dir,
    find_conflicting_fuzzy_key,
    get_game_by_identifier,
    load_fuzzy_candidates,
    repair_misclassified_platform_row,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB_ANY, resolve_and_link_game
from gamelib_mcp.data.title_normalization import (
    normalize_search_text,
    prepare_catalog_title,
)

logger = logging.getLogger(__name__)

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


def _classify_nintendo_sync_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("auth", "expired", "session", "token", "login")):
        return "auth_stale"
    if any(token in lowered for token in ("not in path", "command not found", "executable", "binary")):
        return "missing_runtime_dependency"
    return "unexpected"


# ---------------------------------------------------------------------------
# VGCS helpers
# ---------------------------------------------------------------------------

def _load_vgcs_cookies() -> dict[str, str] | None:
    """Load Nintendo session cookies from NINTENDO_COOKIES_FILE."""
    fallback_path = str(default_data_dir() / "nintendo_cookies.json")
    configured_path = os.getenv("NINTENDO_COOKIES_FILE") or fallback_path
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
    if not raw_data or not isinstance(raw_data, str):
        raise RuntimeError("VGCS #data div missing data-json attribute — page structure may have changed")
    page_data = json.loads(raw_data)
    id_token = page_data["idToken"]
    savanna_client_id = page_data["savannaClientId"]

    state_div = soup.find(id="state")
    if not state_div:
        raise RuntimeError("VGCS page missing #state div")
    raw_state = state_div.get("data-json")
    if not raw_state or not isinstance(raw_state, str):
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

async def _sync_nintendo_ownership() -> dict:
    """
    Sync Nintendo Switch titles into game_platforms (platform="switch2").

    Strategy:
    1. Use VGCS GraphQL (requires NINTENDO_COOKIES_FILE with valid session cookies).
       - Provides full digital library ownership; playtime stored as None.
    2. If not configured, skip silently.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    entries: list[dict] | None = None

    has_vgcs_cookies = bool(_load_vgcs_cookies())
    vgcs_error: Exception | None = None

    if has_vgcs_cookies:
        try:
            entries = await fetch_nintendo_library_vgcs()
            logger.info("Nintendo: fetched %d titles via VGCS", len(entries))
        except Exception as exc:
            vgcs_error = exc
            logger.warning("VGCS sync failed: %s", exc)

    if entries is None:
        if vgcs_error is not None:
            classification = _classify_nintendo_sync_error(str(vgcs_error))
            return {
                "added": 0,
                "matched": 0,
                "skipped": 0,
                "sync_status": "stale" if classification == "auth_stale" else "failed",
                "error_summary": f"VGCS sync failed: {vgcs_error}",
                "error_classification": classification,
            }
        logger.info("Nintendo sync skipped — set NINTENDO_COOKIES_FILE")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": "Nintendo sync skipped: set NINTENDO_COOKIES_FILE",
            "error_classification": "missing_configuration",
        }

    added = matched = skipped = 0
    prepared_entries = []
    for entry in entries:
        name = prepare_catalog_title(entry["name"])
        if not name:
            skipped += 1
            continue
        prepared_entries.append((entry, name))

    current_titles = {normalize_search_text(name) for _entry, name in prepared_entries}
    candidates = await load_fuzzy_candidates()

    for entry, name in prepared_entries:
        igdb_platform_id = PLATFORM_TO_IGDB_ANY.get(PLATFORM)
        title_id = entry.get("title_id")

        # Prefer the stable Nintendo application id so a re-sync matches the existing
        # game directly; name/fuzzy resolution (which now refuses to attach onto an
        # existing switch2-owning row) is reserved for genuinely new titles.
        existing = (
            await get_game_by_identifier(NINTENDO_TITLE_ID, title_id) if title_id else None
        )
        # Identifier miss but a same-name switch2 row exists without any
        # nintendo_title_id: adopt the identifier onto it instead of letting the
        # exclude_platform guard fork a stranded duplicate.
        adopted_game_id = (
            await adopt_platform_identifier(
                name=name,
                platform=PLATFORM,
                identifier_type=NINTENDO_TITLE_ID,
                identifier_value=title_id,
            )
            if existing is None and title_id
            else None
        )
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            matched += 1
        elif adopted_game_id is not None:
            game_id = adopted_game_id
            igdb_game = None
            matched += 1
        else:
            conflicting_game_id = find_conflicting_fuzzy_key(name, candidates)
            game_id, igdb_game = await resolve_and_link_game(
                name, igdb_platform_id, candidates, platform=PLATFORM
            )
            if game_id in candidates:
                matched += 1
            else:
                candidates[game_id] = name
                added += 1

            if conflicting_game_id is not None and conflicting_game_id != game_id:
                conflicting_title = candidates.get(conflicting_game_id)
                if conflicting_title and normalize_search_text(conflicting_title) not in current_titles:
                    await repair_misclassified_platform_row(
                        source_game_id=conflicting_game_id,
                        target_game_id=game_id,
                        platform=PLATFORM,
                    )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform=PLATFORM,
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
            from_source=True,
        )

        if igdb_game is not None and igdb_platform_id:
            release_date = next(
                (
                    igdb_game.platform_release_dates[pid]
                    for pid in igdb_platform_id
                    if pid in igdb_game.platform_release_dates
                ),
                None,
            )
            if release_date is not None:
                await upsert_game_platform_enrichment(
                    platform_id,
                    platform_release_date=release_date,
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


async def sync_nintendo() -> dict:
    """Sync the switch2 platform: VGCS ownership, then Parental Controls playtime.

    The two layers are independent — VGCS (`_sync_nintendo_ownership`) provides
    ownership of the digital library; Parental Controls (`sync_nintendo_pctl`)
    provides per-game playtime and surfaces games played on this console under
    another account. A failure in one must not abort the other. Ownership keys
    stay at the top level (so per-platform sync metadata still reports switch2
    ownership status); the playtime result is nested under "playtime".
    """
    ownership = await _sync_nintendo_ownership()

    try:
        from gamelib_mcp.data.nintendo_pctl import sync_nintendo_pctl

        playtime = await sync_nintendo_pctl()
    except Exception as exc:  # defensive: never let playtime break ownership sync
        logger.warning("Parental Controls playtime layer failed: %s", exc)
        playtime = {
            "sync_status": "failed",
            "error_summary": str(exc),
            "error_classification": _classify_nintendo_sync_error(str(exc)),
        }

    result = {**ownership, "playtime": playtime}

    # If ownership succeeded but the playtime layer *actually failed* (not merely
    # unconfigured), surface that error at the top level so per-platform sync
    # metadata records it — otherwise a stale/expired Parental Controls token is
    # invisible in the control plane (build_platform_sync_metadata only inspects
    # the top-level sync_status/error_summary).
    if not ownership.get("error_summary") and playtime.get("sync_status") in ("failed", "stale"):
        result["sync_status"] = playtime["sync_status"]
        result["error_summary"] = f"Parental Controls playtime: {playtime.get('error_summary')}"
        result["error_classification"] = playtime.get("error_classification")

    return result
