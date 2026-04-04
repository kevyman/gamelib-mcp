# Epic Games + GOG Data Modules Implementation Plan

> **For Claude:** Use `${SUPERPOWERS_SKILLS_ROOT}/skills/collaboration/executing-plans/SKILL.md` to implement this plan task-by-task.

**Goal:** Add `gamelib_mcp/data/epic.py` and `gamelib_mcp/data/gog.py` — async modules that fetch owned games from Epic Games Store (via legendary CLI) and GOG (via OAuth2 API), deduplicate against existing `games` rows using fuzzy matching, and upsert into `game_platforms`.

**Architecture:** Each module exposes a single async `sync_<platform>(db_conn)` function. Fuzzy matching uses rapidfuzz `token_sort_ratio` (cutoff=85), same as the existing Backloggd integration. Credentials are read from env vars; missing credentials cause the module to return immediately with zero results (silent skip). No playtime data for either platform.

**Tech Stack:** Python 3.12, aiosqlite, `legendary` CLI subprocess, `aiohttp` for GOG OAuth2 + games API, `rapidfuzz`, existing `upsert_game` / `upsert_game_platform` helpers from `db.py`.

---

### Task 1: Add `legendary` and `aiohttp` dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Check current dependencies**

```bash
cat pyproject.toml
```

**Step 2: Add missing deps**

In `pyproject.toml`, add to the `dependencies` list if not already present:
- `"aiohttp>=3.9"`
- `"rapidfuzz>=3.0"` (may already exist for Backloggd)

Do NOT add `legendary` — it is an external CLI tool installed by the user, not a Python package dependency.

**Step 3: Sync**

```bash
uv sync
```

Expected: resolves without errors.

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add aiohttp dependency for GOG OAuth2 API"
```

---

### Task 2: Add fuzzy-match helper to `db.py`

**Files:**
- Modify: `gamelib_mcp/data/db.py` (append after existing helpers)

This helper is shared by all non-Steam platform modules.

**Step 1: Add `find_game_by_name_fuzzy()`**

```python
async def find_game_by_name_fuzzy(name: str, cutoff: int = 85) -> aiosqlite.Row | None:
    """Return the best-matching games row for a given title, or None if below cutoff."""
    from rapidfuzz import process, fuzz

    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id, name FROM games")

    if not rows:
        return None

    choices = {row["id"]: row["name"] for row in rows}
    result = process.extractOne(
        name,
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=cutoff,
    )
    if result is None:
        return None

    best_id = result[2]
    async with get_db() as db:
        return await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (best_id,))
```

**Step 2: Verify**

```bash
python -c "import gamelib_mcp.data.db"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add gamelib_mcp/data/db.py
git commit -m "feat: add find_game_by_name_fuzzy helper for cross-platform dedup"
```

---

### Task 3: Create `gamelib_mcp/data/epic.py`

**Files:**
- Create: `gamelib_mcp/data/epic.py`

**Step 1: Write the module**

```python
"""Epic Games Store library sync via legendary CLI.

Requires `legendary` to be installed and authenticated (`legendary auth`).
Set EPIC_LEGENDARY_PATH to the legendary config directory if non-default.
Playtime is not available from Epic.
"""

import json
import logging
import os
import asyncio
from datetime import datetime, timezone

from gamelib_mcp.data.db import find_game_by_name_fuzzy, upsert_game, upsert_game_platform

logger = logging.getLogger(__name__)

LEGENDARY_BIN = os.getenv("LEGENDARY_BIN", "legendary")


async def _run_legendary(*args: str) -> str:
    """Run a legendary CLI command and return stdout."""
    cmd = [LEGENDARY_BIN, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"legendary {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode()[:200]}"
        )
    return stdout.decode()


async def fetch_epic_library() -> list[dict]:
    """Return list of owned Epic games as dicts with at least 'title' and 'app_name'."""
    raw = await _run_legendary("list", "--json")
    data = json.loads(raw)
    # legendary --json returns a list of game objects
    if isinstance(data, list):
        return data
    # Some versions wrap in {"games": [...]}
    return data.get("games", [])


async def sync_epic() -> dict:
    """
    Sync Epic Games library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("EPIC_LEGENDARY_PATH") and not _legendary_available():
        logger.info("legendary not configured — skipping Epic sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    try:
        games = await fetch_epic_library()
    except Exception as exc:
        logger.warning("Epic sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    for game in games:
        title = game.get("title") or game.get("app_title") or game.get("app_name")
        if not title:
            skipped += 1
            continue

        existing = await find_game_by_name_fuzzy(title)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=title)
            added += 1

        await upsert_game_platform(
            game_id=game_id,
            platform="epic",
            playtime_minutes=None,  # Epic doesn't expose playtime
            owned=1,
        )

    logger.info("Epic sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}


def _legendary_available() -> bool:
    """Check if legendary binary is on PATH."""
    import shutil
    return shutil.which(LEGENDARY_BIN) is not None
```

**Step 2: Verify the module imports cleanly**

```bash
python -c "import gamelib_mcp.data.epic"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add gamelib_mcp/data/epic.py
git commit -m "feat: add epic.py — Epic Games library sync via legendary CLI"
```

---

### Task 4: Create `gamelib_mcp/data/gog.py`

**Files:**
- Create: `gamelib_mcp/data/gog.py`

GOG exposes an OAuth2 API. The refresh token is stored in `.env` as `GOG_REFRESH_TOKEN`. At runtime we exchange it for an access token, then hit the owned-games endpoint.

**Step 1: Write the module**

```python
"""GOG owned games sync via GOG OAuth2 API.

Set GOG_REFRESH_TOKEN in .env (obtained via python -m gamelib_mcp.setup_platform gog).
Playtime is not available from GOG's public API.
"""

import logging
import os
from datetime import datetime, timezone

import aiohttp

from gamelib_mcp.data.db import find_game_by_name_fuzzy, upsert_game, upsert_game_platform

logger = logging.getLogger(__name__)

_GOG_TOKEN_URL = "https://auth.gog.com/token"
_GOG_LIBRARY_URL = "https://embed.gog.com/user/data/games"
_GOG_GAME_DETAIL_URL = "https://api.gog.com/products/{game_id}?expand=downloads"

_CLIENT_ID = "46899977096215655"      # GOG public client ID (no secret needed for refresh)
_CLIENT_SECRET = "9d85c43b1718a031d5b64228ecd1a9eb"  # GOG public client secret


async def _get_access_token(session: aiohttp.ClientSession) -> str:
    """Exchange GOG_REFRESH_TOKEN for a short-lived access token."""
    refresh_token = os.environ["GOG_REFRESH_TOKEN"]
    async with session.post(
        _GOG_TOKEN_URL,
        params={
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["access_token"]


async def _fetch_owned_game_ids(session: aiohttp.ClientSession, access_token: str) -> list[int]:
    """Return list of owned GOG product IDs."""
    async with session.get(
        _GOG_LIBRARY_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data.get("owned", [])


async def _fetch_game_title(
    session: aiohttp.ClientSession,
    access_token: str,
    gog_id: int,
) -> str | None:
    """Fetch the title for a single GOG product ID."""
    url = _GOG_GAME_DETAIL_URL.format(game_id=gog_id)
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            return data.get("title")
    except Exception as exc:
        logger.debug("Could not fetch GOG title for id=%d: %s", gog_id, exc)
        return None


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("GOG_REFRESH_TOKEN"):
        logger.info("GOG_REFRESH_TOKEN not set — skipping GOG sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0

    try:
        async with aiohttp.ClientSession() as session:
            access_token = await _get_access_token(session)
            gog_ids = await _fetch_owned_game_ids(session, access_token)

            for gog_id in gog_ids:
                title = await _fetch_game_title(session, access_token, gog_id)
                if not title:
                    skipped += 1
                    continue

                existing = await find_game_by_name_fuzzy(title)
                if existing:
                    game_id = existing["id"]
                    matched += 1
                else:
                    game_id = await upsert_game(appid=None, name=title)
                    added += 1

                await upsert_game_platform(
                    game_id=game_id,
                    platform="gog",
                    playtime_minutes=None,  # GOG public API doesn't expose playtime
                    owned=1,
                )

    except Exception as exc:
        logger.warning("GOG sync failed: %s", exc)
        return {"added": added, "matched": matched, "skipped": skipped}

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
```

**Step 2: Verify the module imports cleanly**

```bash
python -c "import gamelib_mcp.data.gog"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add gamelib_mcp/data/gog.py
git commit -m "feat: add gog.py — GOG owned games sync via OAuth2 API"
```

---

### Task 5: Smoke-test Epic sync (if legendary is installed)

Skip this task if `legendary` is not installed.

**Step 1: Check legendary availability**

```bash
legendary --version
```

If command not found: skip to Task 6.

**Step 2: Check legendary auth status**

```bash
legendary status
```

Expected: shows account info if authenticated. If not authenticated, run `legendary auth` manually first.

**Step 3: Run sync in a Python REPL**

```bash
python -c "
import asyncio
from gamelib_mcp.data.epic import sync_epic
result = asyncio.run(sync_epic())
print(result)
"
```

Expected: `{'added': N, 'matched': M, 'skipped': K}` with no exceptions.

**Step 4: Verify game_platforms rows**

```bash
sqlite3 steam.db "SELECT g.name, gp.platform FROM games g JOIN game_platforms gp ON gp.game_id = g.id WHERE gp.platform = 'epic' LIMIT 10;"
```

Expected: rows with Epic game titles.

---

### Task 6: Smoke-test GOG sync (if GOG_REFRESH_TOKEN is set)

Skip this task if `GOG_REFRESH_TOKEN` is not in `.env`.

**Step 1: Check env**

```bash
grep GOG_REFRESH_TOKEN .env
```

If not present: skip to Task 7.

**Step 2: Run sync**

```bash
python -c "
import asyncio, dotenv, os
dotenv.load_dotenv()
from gamelib_mcp.data.gog import sync_gog
result = asyncio.run(sync_gog())
print(result)
"
```

Expected: `{'added': N, 'matched': M, 'skipped': K}` with no exceptions.

**Step 3: Verify game_platforms rows**

```bash
sqlite3 steam.db "SELECT g.name, gp.platform FROM games g JOIN game_platforms gp ON gp.game_id = g.id WHERE gp.platform = 'gog' LIMIT 10;"
```

Expected: rows with GOG game titles.

---

### Task 7: Push branch

```bash
git push -u origin claude/integrate-superpowers-plugin-0S1aq
```

Expected: branch pushed, no errors.

---

## Done

Epic and GOG data modules are complete. Credentials for Epic (`legendary` auth) and GOG (`GOG_REFRESH_TOKEN`) can be configured independently — missing credentials are silently skipped.

Next plan: remaining platform modules (PSN, Nintendo, Xbox, Itch.io) — or jump straight to tool changes if Epic + GOG are the only platforms needed.
