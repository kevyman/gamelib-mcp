# Tool Changes Implementation Plan

> **For Claude:** Use `${SUPERPOWERS_SKILLS_ROOT}/skills/collaboration/executing-plans/SKILL.md` to implement this plan task-by-task.

**Goal:** Update 5 existing MCP tools and add 2 new ones to support cross-platform library data: playtime by platform, owned_on list, platform filters, hardware-preference-aware suggested_platform, and platform breakdown/sync tools.

**Architecture:** All changes are in `gamelib_mcp/tools/` and `gamelib_mcp/main.py`. DB queries are updated to JOIN `game_platforms` where needed. Hardware preference is read from `meta` table key `hardware_preference` (JSON list, e.g. `["switch2","steam_deck","ps5"]`). No new dependencies required.

**Tech Stack:** Python 3.12, aiosqlite, existing `get_db()` context manager, FastMCP tool registration in `main.py`.

---

### Task 1: Update `get_game_detail` — playtime by platform + owned_on

**Files:**
- Modify: `gamelib_mcp/tools/detail.py`

**Step 1: Update the DB lookup to use `games.id` instead of `appid`**

The game lookup after migration uses `games.id` as the primary key. Update both fetches:

Change line 20:
```python
row = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (appid,))
```
To:
```python
row = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (appid,))
# (no change — appid lookup still valid, just now returns id too)
```

After the enrichment block, update the re-fetch on line 43 and add a platforms query:

Replace:
```python
    async with get_db() as db:
        row = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (game_appid,))
        rating = await db.execute_fetchone(
            "SELECT source, raw_score, normalized_score, review_text FROM ratings WHERE appid = ? ORDER BY source",
            (game_appid,),
        )
```

With:
```python
    async with get_db() as db:
        row = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (game_appid,))
        platforms = await db.execute_fetchall(
            "SELECT platform, playtime_minutes FROM game_platforms WHERE game_id = ? AND owned = 1",
            (row["id"],),
        )
        rating = await db.execute_fetchone(
            "SELECT source, raw_score, normalized_score, review_text FROM ratings WHERE game_id = ? ORDER BY source",
            (row["id"],),
        )
```

**Step 2: Replace the `playtime_hours` / `playtime_2weeks_hours` fields in the result dict**

Replace:
```python
    result = {
        "appid": row["appid"],
        "name": row["name"],
        "playtime_hours": round(row["playtime_forever"] / 60, 1) if row["playtime_forever"] else 0,
        "playtime_2weeks_hours": round(row["playtime_2weeks"] / 60, 1) if row["playtime_2weeks"] else 0,
        "last_played_date": last_played_date,
```

With:
```python
    by_platform = [
        {"platform": p["platform"], "minutes": p["playtime_minutes"]}
        for p in platforms
        if p["playtime_minutes"] is not None
    ]
    total_minutes = sum(p["playtime_minutes"] for p in platforms if p["playtime_minutes"])
    owned_on = [p["platform"] for p in platforms]

    result = {
        "appid": row["appid"],
        "name": row["name"],
        "playtime": {
            "total_minutes": total_minutes,
            "by_platform": by_platform,
        },
        "owned_on": owned_on,
        "last_played_date": last_played_date,
```

**Step 3: Remove the now-dead `last_played_date` block** (it references `row["rtime_last_played"]` which still exists — keep it, just verify it compiles).

**Step 4: Verify**

```bash
python -c "import gamelib_mcp.tools.detail"
```

Expected: no output, no errors.

**Step 5: Commit**

```bash
git add gamelib_mcp/tools/detail.py
git commit -m "feat: get_game_detail — playtime by platform + owned_on field"
```

---

### Task 2: Update `search_games` and `get_library_stats` — add platform filter

**Files:**
- Modify: `gamelib_mcp/tools/library.py`

**Step 1: Update `search_games` signature and query**

Replace:
```python
async def search_games(query: str, limit: int = 20) -> list[dict]:
    """Find games in library by name substring match."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT appid, name, playtime_forever, playtime_2weeks,
                      hltb_main, metacritic_score,
                      protondb_tier, steam_review_desc, is_farmed
               FROM games
               WHERE lower(name) LIKE lower(?)
               ORDER BY playtime_forever DESC
               LIMIT ?""",
            (f"%{query}%", limit),
        )
    return [_format_game(r) for r in rows]
```

With:
```python
async def search_games(query: str, limit: int = 20, platform: str | None = None) -> list[dict]:
    """Find games in library by name substring match, optionally filtered by platform."""
    params: list = [f"%{query}%"]
    platform_join = ""
    platform_where = ""
    if platform:
        platform_join = "JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = ? AND gp.owned = 1"
        params.insert(0, platform)
        platform_where = ""

    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.appid, g.name, g.hltb_main, g.metacritic_score,
                       g.protondb_tier, g.steam_review_desc, g.is_farmed
                FROM games g
                {platform_join}
                WHERE lower(g.name) LIKE lower(?)
                ORDER BY g.name ASC
                LIMIT ?""",
            (*params, limit),
        )
    return [_format_game(r) for r in rows]
```

**Step 2: Update `search_games_batch` the same way**

Replace the inner query in `search_games_batch`:
```python
            rows = await db.execute_fetchall(
                """SELECT appid, name, playtime_forever, playtime_2weeks,
                          hltb_main, metacritic_score, protondb_tier,
                          steam_review_desc, is_farmed
                   FROM games
                   WHERE lower(name) LIKE lower(?)
                   ORDER BY playtime_forever DESC
                   LIMIT ?""",
                (f"%{query}%", limit_per_query),
            )
```

With:
```python
            rows = await db.execute_fetchall(
                """SELECT appid, name, hltb_main, metacritic_score,
                          protondb_tier, steam_review_desc, is_farmed
                   FROM games
                   WHERE lower(name) LIKE lower(?)
                   ORDER BY name ASC
                   LIMIT ?""",
                (f"%{query}%", limit_per_query),
            )
```

**Step 3: Update `get_library_stats` — add `platform` param and fix playtime references**

Replace the function signature:
```python
async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
) -> dict:
```

With:
```python
async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "name",
    limit: int = 50,
    platform: str | None = None,
) -> dict:
```

Update `SORT_COLUMNS` — remove `playtime_forever` references since playtime now lives in `game_platforms`:

```python
SORT_COLUMNS = {
    "name": "g.name",
    "metacritic": "g.metacritic_score",
    "hltb": "g.hltb_main",
}
```

Remove the `filter == "unplayed"`, `"played"`, `"recent"` conditions that reference `playtime_forever` and `playtime_2weeks` (those columns no longer exist on `games`). Replace with simpler platform-aware versions:

```python
    platform_join = ""
    if platform:
        platform_join = "JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = ? AND gp.owned = 1"
        params.insert(0, platform)

    if filter == "farmed":
        conditions.append("g.is_farmed = 1")
```

Update the main SELECT and aggregate queries to remove `playtime_forever`/`playtime_2weeks` references:

```python
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT g.appid, g.name, g.hltb_main, g.metacritic_score,
                       g.protondb_tier, g.steam_review_desc, g.is_farmed
                FROM games g
                {platform_join}
                {where}
                ORDER BY {sort_col} {sort_dir} NULLS LAST
                LIMIT ?""",
            (*params, limit),
        )

        total = await db.execute_fetchone("SELECT COUNT(*) as c FROM games")
        farmed = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM games WHERE is_farmed = 1"
        )

    stats = {
        "total_games": total["c"],
        "farmed_games": farmed["c"],
        "filter": filter,
        "platform": platform,
        "sort_by": sort_by,
        "results": [_format_game(r) for r in rows],
    }
    return stats
```

**Step 4: Update `_format_game` — remove playtime fields**

Replace:
```python
def _format_game(row: aiosqlite.Row) -> dict:
    return {
        "appid": row["appid"],
        "name": row["name"],
        "playtime_hours": round(row["playtime_forever"] / 60, 1) if row["playtime_forever"] else 0,
        "playtime_2weeks_hours": round(row["playtime_2weeks"] / 60, 1) if row["playtime_2weeks"] else 0,
        "hltb_main": row["hltb_main"],
        "metacritic_score": row["metacritic_score"],
        "protondb_tier": row["protondb_tier"],
        "steam_review_desc": row["steam_review_desc"],
        "is_farmed": bool(row["is_farmed"]),
    }
```

With:
```python
def _format_game(row: aiosqlite.Row) -> dict:
    return {
        "appid": row["appid"],
        "name": row["name"],
        "hltb_main": row["hltb_main"],
        "metacritic_score": row["metacritic_score"],
        "protondb_tier": row["protondb_tier"],
        "steam_review_desc": row["steam_review_desc"],
        "is_farmed": bool(row["is_farmed"]),
    }
```

**Step 5: Verify**

```bash
python -c "import gamelib_mcp.tools.library"
```

Expected: no output, no errors.

**Step 6: Commit**

```bash
git add gamelib_mcp/tools/library.py
git commit -m "feat: add platform filter to search_games + get_library_stats, drop playtime columns"
```

---

### Task 3: Update `get_recommendations` — add `suggested_platform`

**Files:**
- Modify: `gamelib_mcp/tools/discover.py`

**Step 1: Add hardware preference lookup at the top of `get_recommendations`**

After `async with get_db() as db:` opens the connection for the main query, fetch hardware prefs first:

```python
async def get_recommendations(
    max_hltb_hours: float | None = None,
    unplayed_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    from ..data.db import get_meta
    import json

    hw_pref_raw = await get_meta("hardware_preference")
    hw_pref: list[str] = json.loads(hw_pref_raw) if hw_pref_raw else []
```

**Step 2: Update the SELECT to include `games.id` and remove `playtime_forever`**

Replace the query inside `get_recommendations`:
```python
        rows = await db.execute_fetchall(
            f"""SELECT g.appid, g.name, g.playtime_forever,
                       AVG(ta.affinity_score) as match_score,
                       g.hltb_main, g.metacritic_score,
                       g.steam_review_desc, g.protondb_tier, g.tags
                FROM games g
                JOIN json_each(g.tags) je ON 1=1
                JOIN tag_affinity ta ON ta.tag = lower(je.value)
                {where}
                GROUP BY g.appid
                ORDER BY match_score DESC
                LIMIT ?""",
            (*params, limit),
        )
```

With:
```python
        rows = await db.execute_fetchall(
            f"""SELECT g.id, g.appid, g.name,
                       AVG(ta.affinity_score) as match_score,
                       g.hltb_main, g.metacritic_score,
                       g.steam_review_desc, g.protondb_tier, g.tags
                FROM games g
                JOIN json_each(g.tags) je ON 1=1
                JOIN tag_affinity ta ON ta.tag = lower(je.value)
                {where}
                GROUP BY g.id
                ORDER BY match_score DESC
                LIMIT ?""",
            (*params, limit),
        )
```

**Step 3: After the query, fetch platforms per result and compute `suggested_platform`**

Replace the return list comprehension:
```python
    return [
        {
            "appid": r["appid"],
            ...
        }
        for r in rows
    ]
```

With:
```python
    results = []
    async with get_db() as db:
        for r in rows:
            platforms = await db.execute_fetchall(
                "SELECT platform FROM game_platforms WHERE game_id = ? AND owned = 1",
                (r["id"],),
            )
            owned_platforms = [p["platform"] for p in platforms]
            suggested = next(
                (hw for hw in hw_pref if hw in owned_platforms),
                owned_platforms[0] if owned_platforms else None,
            )
            results.append({
                "appid": r["appid"],
                "name": r["name"],
                "match_score": round(r["match_score"], 3) if r["match_score"] else 0,
                "hltb_main": r["hltb_main"],
                "metacritic_score": r["metacritic_score"],
                "steam_review_desc": r["steam_review_desc"],
                "protondb_tier": r["protondb_tier"],
                "tags": _parse_json(r["tags"]),
                "owned_on": owned_platforms,
                "suggested_platform": suggested,
            })
    return results
```

**Step 4: Also remove `playtime_forever` reference from the `unplayed_only` condition**

Change:
```python
    if unplayed_only:
        conditions.append("(g.playtime_forever = 0 OR g.is_farmed = 1)")
```

To:
```python
    if unplayed_only:
        conditions.append("""NOT EXISTS (
            SELECT 1 FROM game_platforms gp
            WHERE gp.game_id = g.id AND gp.playtime_minutes > 0
        ) OR g.is_farmed = 1""")
```

**Step 5: Verify**

```bash
python -c "import gamelib_mcp.tools.discover"
```

Expected: no output, no errors.

**Step 6: Commit**

```bash
git add gamelib_mcp/tools/discover.py
git commit -m "feat: get_recommendations — add suggested_platform via hardware preference"
```

---

### Task 4: Update `refresh_library` — add `platforms` param and fan out

**Files:**
- Modify: `gamelib_mcp/tools/admin.py`

**Step 1: Update `refresh_library` to accept platforms and call platform sync modules**

Replace:
```python
async def refresh_library() -> dict:
    """Force re-sync Steam XML library feed."""
    result = await fetch_library()
    return result
```

With:
```python
async def refresh_library(platforms: list[str] | None = None) -> dict:
    """
    Re-sync game library. Defaults to all configured platforms.
    platforms: subset list e.g. ["steam", "epic", "gog"] or None for all.
    """
    import os

    _ALL_PLATFORMS = ["steam", "epic", "gog", "ps5", "switch", "xbox", "itchio"]
    targets = platforms or _ALL_PLATFORMS

    results = {}

    if "steam" in targets:
        results["steam"] = await fetch_library()

    platform_syncs = {
        "epic":   ("gamelib_mcp.data.epic",    "sync_epic",    "EPIC_LEGENDARY_PATH"),
        "gog":    ("gamelib_mcp.data.gog",     "sync_gog",     "GOG_REFRESH_TOKEN"),
        "ps5":    ("gamelib_mcp.data.psn",     "sync_psn",     "PSN_NPSSO"),
        "switch": ("gamelib_mcp.data.nintendo","sync_nintendo", "NINTENDO_SESSION_TOKEN"),
    }

    for platform, (module_path, fn_name, env_key) in platform_syncs.items():
        if platform not in targets:
            continue
        if not os.getenv(env_key):
            results[platform] = {"skipped": True, "reason": f"{env_key} not set"}
            continue
        try:
            import importlib
            module = importlib.import_module(module_path)
            fn = getattr(module, fn_name)
            results[platform] = await fn()
        except Exception as exc:
            results[platform] = {"error": str(exc)}

    return results
```

**Step 2: Verify**

```bash
python -c "import gamelib_mcp.tools.admin"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add gamelib_mcp/tools/admin.py
git commit -m "feat: refresh_library — fan out to all configured platform sync modules"
```

---

### Task 5: Create `gamelib_mcp/tools/platforms.py` — new tools

**Files:**
- Create: `gamelib_mcp/tools/platforms.py`

**Step 1: Write the module**

```python
"""get_platform_breakdown and sync_platform tools."""

from ..data.db import get_db


async def get_platform_breakdown() -> dict:
    """
    Return per-platform game counts, total unique games, and overlap count
    (games owned on 2+ platforms — the did-I-buy-this-twice list).
    """
    async with get_db() as db:
        # Per-platform counts
        platform_rows = await db.execute_fetchall(
            """SELECT platform, COUNT(DISTINCT game_id) as count
               FROM game_platforms
               WHERE owned = 1
               GROUP BY platform
               ORDER BY count DESC"""
        )

        # Total unique games
        total = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM games"
        )

        # Games owned on 2+ platforms
        overlap_rows = await db.execute_fetchall(
            """SELECT g.name, g.appid, COUNT(gp.platform) as platform_count,
                      GROUP_CONCAT(gp.platform) as platforms
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
               GROUP BY g.id
               HAVING platform_count >= 2
               ORDER BY platform_count DESC"""
        )

    return {
        "by_platform": [
            {"platform": r["platform"], "owned_games": r["count"]}
            for r in platform_rows
        ],
        "total_unique_games": total["c"],
        "overlap_count": len(overlap_rows),
        "overlap_games": [
            {
                "name": r["name"],
                "appid": r["appid"],
                "owned_on": r["platforms"].split(","),
            }
            for r in overlap_rows
        ],
    }


async def sync_platform(platform: str) -> dict:
    """
    Sync a single platform on demand.
    platform: steam | epic | gog | ps5 | switch
    """
    import os
    import importlib

    _PLATFORM_MAP = {
        "steam":  ("gamelib_mcp.data.steam_xml", "fetch_library",    None),
        "epic":   ("gamelib_mcp.data.epic",       "sync_epic",        "EPIC_LEGENDARY_PATH"),
        "gog":    ("gamelib_mcp.data.gog",        "sync_gog",         "GOG_REFRESH_TOKEN"),
        "ps5":    ("gamelib_mcp.data.psn",        "sync_psn",         "PSN_NPSSO"),
        "switch": ("gamelib_mcp.data.nintendo",   "sync_nintendo",    "NINTENDO_SESSION_TOKEN"),
    }

    if platform not in _PLATFORM_MAP:
        return {"error": f"Unknown platform '{platform}'. Valid: {list(_PLATFORM_MAP)}"}

    module_path, fn_name, env_key = _PLATFORM_MAP[platform]

    if env_key and not os.getenv(env_key):
        return {"error": f"{env_key} not set — cannot sync {platform}"}

    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name)
        return await fn()
    except Exception as exc:
        return {"error": str(exc)}
```

**Step 2: Verify**

```bash
python -c "import gamelib_mcp.tools.platforms"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add gamelib_mcp/tools/platforms.py
git commit -m "feat: add get_platform_breakdown and sync_platform tools"
```

---

### Task 6: Register new tools and update signatures in `main.py`

**Files:**
- Modify: `gamelib_mcp/main.py`

**Step 1: Update `search_games` registration to include `platform` param**

Replace:
```python
@mcp.tool()
async def search_games(query: str, limit: int = 20) -> list[dict]:
    """Find games in the Steam library by name substring."""
    from .tools.library import search_games as _search
    return await _search(query, limit)
```

With:
```python
@mcp.tool()
async def search_games(query: str, limit: int = 20, platform: str | None = None) -> list[dict]:
    """Find games in the library by name substring. platform: steam|epic|gog|ps5|switch"""
    from .tools.library import search_games as _search
    return await _search(query, limit, platform)
```

**Step 2: Update `get_library_stats` registration**

Add `platform` param:
```python
@mcp.tool()
async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "name",
    limit: int = 50,
    platform: str | None = None,
) -> dict:
    """
    Get filtered/sorted library list + aggregate stats.

    filter: all | farmed
    sort_by: name | metacritic | hltb
    platform: steam | epic | gog | ps5 | switch (optional filter)
    """
    from .tools.library import get_library_stats as _stats
    return await _stats(filter, max_hltb_hours, min_metacritic, protondb_tier, sort_by, limit, platform)
```

**Step 3: Update `refresh_library` registration**

```python
@mcp.tool()
async def refresh_library(platforms: list[str] | None = None) -> dict:
    """Re-sync game library. platforms: list like ['steam','epic'] or omit for all configured."""
    from .tools.admin import refresh_library as _refresh
    return await _refresh(platforms)
```

**Step 4: Add `get_platform_breakdown` and `sync_platform` registrations**

Add after the `detect_farmed_games` tool block:

```python
@mcp.tool()
async def get_platform_breakdown() -> dict:
    """
    Show game counts per platform, total unique games, and overlap list
    (games you own on multiple platforms).
    """
    from .tools.platforms import get_platform_breakdown as _breakdown
    return await _breakdown()


@mcp.tool()
async def sync_platform(platform: str) -> dict:
    """
    Sync a single platform on demand.
    platform: steam | epic | gog | ps5 | switch
    """
    from .tools.platforms import sync_platform as _sync
    return await _sync(platform)
```

**Step 5: Verify the whole app loads**

```bash
python -c "import gamelib_mcp.main"
```

Expected: no output, no errors.

**Step 6: Commit**

```bash
git add gamelib_mcp/main.py
git commit -m "feat: register get_platform_breakdown, sync_platform; update tool signatures for platform support"
```

---

### Task 7: Create `gamelib_mcp/setup_platform.py`

**Files:**
- Create: `gamelib_mcp/setup_platform.py`

The spec requires a setup script that handles OAuth browser flows for GOG and Epic, writing the resulting tokens to `.env`.

**Step 1: Write the script**

```python
"""Platform credential setup helper.

Usage: python -m gamelib_mcp.setup_platform <platform>

Supported platforms:
  gog    — opens GOG OAuth2 flow, writes GOG_REFRESH_TOKEN to .env
  epic   — prints legendary auth instructions (browser flow managed by legendary CLI)
  psn    — prints manual NPSSO cookie extraction instructions
  switch — prints nxapi session token extraction instructions
"""

import sys


def _setup_gog() -> None:
    """Run GOG OAuth2 flow and write refresh token to .env."""
    import asyncio
    import os
    import webbrowser

    import aiohttp
    from dotenv import set_key

    CLIENT_ID = "46899977096215655"
    CLIENT_SECRET = "9d85c43b1718a031d5b64228ecd1a9eb"
    AUTH_URL = (
        f"https://auth.gog.com/auth?client_id={CLIENT_ID}"
        "&redirect_uri=https://embed.gog.com/on_login_success?origin=client"
        "&response_type=code&layout=client2"
    )

    print("Opening GOG login page in your browser...")
    webbrowser.open(AUTH_URL)
    code = input(
        "\nAfter logging in, copy the 'code' query parameter from the redirect URL and paste it here:\n> "
    ).strip()

    async def _exchange():
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://auth.gog.com/token",
                params={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "https://embed.gog.com/on_login_success?origin=client",
                },
            ) as resp:
                resp.raise_for_status()
                return (await resp.json())["refresh_token"]

    refresh_token = asyncio.run(_exchange())
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    set_key(env_path, "GOG_REFRESH_TOKEN", refresh_token)
    print(f"GOG_REFRESH_TOKEN written to .env")


def _setup_epic() -> None:
    print(
        "Epic Games auth is handled by the legendary CLI.\n"
        "Run:  legendary auth\n"
        "Follow the browser prompts, then set EPIC_LEGENDARY_PATH in .env if legendary\n"
        "uses a non-default config directory."
    )


def _setup_psn() -> None:
    print(
        "PSN auth requires a one-time manual step:\n"
        "1. Log in to your PSN account in a browser.\n"
        "2. Visit: https://ca.account.sony.com/api/v1/ssocookie\n"
        "3. Copy the value of the 'npsso' field.\n"
        "4. Add to .env:  PSN_NPSSO=<value>"
    )


def _setup_switch() -> None:
    print(
        "Nintendo Switch auth requires nxapi and a one-time session token:\n"
        "1. Install nxapi: https://github.com/samuelthomas2774/nxapi\n"
        "2. Run: nxapi nso auth\n"
        "3. Follow the prompts to authenticate with your Nintendo account.\n"
        "4. Copy the session token and add to .env:  NINTENDO_SESSION_TOKEN=<value>"
    )


_HANDLERS = {
    "gog": _setup_gog,
    "epic": _setup_epic,
    "psn": _setup_psn,
    "switch": _setup_switch,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in _HANDLERS:
        print(f"Usage: python -m gamelib_mcp.setup_platform <platform>")
        print(f"Platforms: {', '.join(_HANDLERS)}")
        sys.exit(1)
    _HANDLERS[sys.argv[1]]()
```

**Step 2: Verify the script loads**

```bash
python -m gamelib_mcp.setup_platform
```

Expected: prints usage and platform list, exits with code 1.

**Step 3: Commit**

```bash
git add gamelib_mcp/setup_platform.py
git commit -m "feat: add setup_platform script for GOG OAuth and platform credential guidance"
```

---

### Task 8: Update `.env.example`

**Files:**
- Modify: `.env.example`

**Step 1: Append new platform env vars**

Read the current `.env.example`, then append the following block after the existing entries:

```
# Cross-platform library credentials (all optional — missing vars silently skip that platform)
PSN_NPSSO=                  # PSN NPSSO cookie — see: python -m gamelib_mcp.setup_platform psn
EPIC_LEGENDARY_PATH=        # Path to legendary config dir (optional, default is ~/.config/legendary)
GOG_REFRESH_TOKEN=          # GOG OAuth2 refresh token — run: python -m gamelib_mcp.setup_platform gog
NINTENDO_SESSION_TOKEN=     # Nintendo session token — see: python -m gamelib_mcp.setup_platform switch

# Hardware preference for get_recommendations suggested_platform (comma-separated, highest priority first)
HARDWARE_PREFERENCE=switch2,steam_deck,ps5
```

**Step 2: Commit**

```bash
git add .env.example
git commit -m "chore: add cross-platform credential vars to .env.example"
```

---

### Task 9: Push branch

```bash
git push -u origin claude/integrate-superpowers-plugin-0S1aq
```

Expected: branch pushed, no errors.

---

## Done

All spec tool changes are implemented:
- `get_game_detail` → playtime by platform + owned_on
- `search_games` + `get_library_stats` → platform filter
- `get_recommendations` → suggested_platform via hardware preference
- `refresh_library` → fans out to all configured platforms
- `get_platform_breakdown` + `sync_platform` → new tools registered
- `setup_platform` script → OAuth flows for GOG + instructions for Epic/PSN/Switch
- `.env.example` → all new credential vars documented

The MCP server is now fully cross-platform aware.
