# PSN Data Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `steam_mcp/data/psn.py` — an async module that fetches the user's PS5 game library and playtime via PSNAWP, deduplicates against existing `games` rows using fuzzy matching, and upserts into `game_platforms`.

**Architecture:** Single async `sync_psn()` function. Auth uses an NPSSO cookie (one-time manual extraction from browser, stored as `PSN_NPSSO` in `.env`). PSNAWP's `client.title_stats()` is used to fetch the game library — it returns each played title's `name`, `play_count`, and `play_duration` (a `datetime.timedelta`). `play_duration` is converted to minutes for `playtime_minutes`. Fuzzy dedup via `find_game_by_name_fuzzy()` (cutoff=85). Candidates are pre-loaded via `load_fuzzy_candidates()` for efficiency, matching the Epic/GOG pattern.

**Schema context (v2):** The `games` table has no `appid` column — platform-specific IDs live in `game_platform_identifiers`. `upsert_game_platform` returns the `game_platforms.id` (platform_id), which can be passed to `upsert_game_platform_identifier` if a platform identifier is available. PSNAWP's `title_stats()` does not expose a canonical PSN title ID, so no identifier is stored.

**Tech Stack:** Python 3.12, `PSNAWP` library, `rapidfuzz`, existing `upsert_game` / `upsert_game_platform` / `find_game_by_name_fuzzy` / `load_fuzzy_candidates` helpers from `db.py`.

---

### Task 1: Add `PSNAWP` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Check current dependencies**

```bash
cat pyproject.toml
```

- [ ] **Step 2: Add `psnawp` to the `dependencies` list**

Add: `"psnawp>=2.1"`

- [ ] **Step 3: Sync**

```bash
uv sync
```

Expected: resolves without errors.

- [ ] **Step 4: Verify import**

```bash
python -c "from psnawp_api import PSNAWP; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add psnawp dependency for PSN library sync"
```

---

### Task 2: Create `steam_mcp/data/psn.py`

**Files:**
- Create: `steam_mcp/data/psn.py`

- [ ] **Step 1: Write the module**

```python
"""PlayStation Network library sync via PSNAWP.

Auth: set PSN_NPSSO in .env.
Obtain the NPSSO cookie by visiting https://ca.account.sony.com/api/v1/ssocookie
while logged in to your PSN account in a browser. Copy the `npsso` value.

Library source: client.title_stats() — returns all titles the user has played,
with name, play_count, and play_duration (datetime.timedelta). Only played titles
appear; unplayed purchases will not show up (PSN platform limitation).
"""

import logging
import os

from steam_mcp.data.db import (
    find_game_by_name_fuzzy,
    load_fuzzy_candidates,
    upsert_game,
    upsert_game_platform,
)

logger = logging.getLogger(__name__)


def _get_psnawp():
    """Return an authenticated PSNAWP instance, or raise if not configured."""
    from psnawp_api import PSNAWP  # lazy import — optional dependency
    npsso = os.environ.get("PSN_NPSSO")
    if not npsso:
        raise EnvironmentError("PSN_NPSSO not set")
    return PSNAWP(npsso)


async def fetch_psn_library() -> list[dict]:
    """
    Return a list of dicts with 'name' and 'playtime_minutes' for each played PS5 title.

    Uses client.title_stats() which returns name, play_count, and play_duration
    (a datetime.timedelta). Runs PSNAWP synchronously in an executor.
    """
    import asyncio

    def _fetch():
        psnawp = _get_psnawp()
        client = psnawp.me()
        results = []
        for entry in client.title_stats():
            name = entry.name
            if not name:
                continue
            minutes = None
            if entry.play_duration is not None:
                minutes = int(entry.play_duration.total_seconds() // 60)
            results.append({"name": name, "playtime_minutes": minutes})
        return results

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


async def sync_psn() -> dict:
    """
    Sync PSN library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("PSN_NPSSO"):
        logger.info("PSN_NPSSO not set — skipping PSN sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0

    try:
        entries = await fetch_psn_library()
    except Exception as exc:
        logger.warning("PSN sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    candidates = await load_fuzzy_candidates()

    for entry in entries:
        name = entry["name"]
        if not name:
            skipped += 1
            continue

        existing = await find_game_by_name_fuzzy(name, candidates=candidates)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=name)
            candidates[game_id] = name
            added += 1

        await upsert_game_platform(
            game_id=game_id,
            platform="ps5",
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
        )

    logger.info("PSN sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "import steam_mcp.data.psn"
```

Expected: no output, no errors.

- [ ] **Step 3: Commit**

```bash
git add steam_mcp/data/psn.py
git commit -m "feat: add psn.py — PSN library sync via PSNAWP title_stats (name + playtime)"
```

---

### Task 3: Wire into `refresh_library`

**Files:**
- Modify: `steam_mcp/tools/admin.py`

- [ ] **Step 1: Add `sync_psn` to the fan-out in `refresh_library`**

In `steam_mcp/tools/admin.py`, import `sync_psn` from `steam_mcp.data.psn` and add it to the platform sync fan-out alongside `sync_epic` and `sync_gog`.

- [ ] **Step 2: Commit**

```bash
git add steam_mcp/tools/admin.py
git commit -m "feat: wire PSN sync into refresh_library"
```

---

### Task 4: Smoke-test PSN sync (if PSN_NPSSO is set)

Skip this task if `PSN_NPSSO` is not in `.env`.

- [ ] **Step 1: Check env**

```bash
grep PSN_NPSSO .env
```

If not present: skip to Task 5.

- [ ] **Step 2: Run sync**

```bash
python -c "
import asyncio, dotenv
dotenv.load_dotenv()
from steam_mcp.data.psn import sync_psn
result = asyncio.run(sync_psn())
print(result)
"
```

Expected: `{'added': N, 'matched': M, 'skipped': K}` with no exceptions.

- [ ] **Step 3: Verify game_platforms rows**

```bash
sqlite3 steam.db "SELECT g.name, gp.platform FROM games g JOIN game_platforms gp ON gp.game_id = g.id WHERE gp.platform = 'ps5' LIMIT 10;"
```

Expected: rows with PSN game titles and `platform='ps5'`.

---

### Task 5: Update `.env.example` and `deploy.md`

- [ ] **Step 1: Add PSN_NPSSO to `.env.example`**

Add in the platform credentials section:
```
PSN_NPSSO=   # NPSSO cookie from https://ca.account.sony.com/api/v1/ssocookie
```

- [ ] **Step 2: Add PSN section to `deploy.md`**

Add a brief "PSN Setup" section explaining the one-time NPSSO extraction. No server-side auth needed — NPSSO goes directly in `.env`.

- [ ] **Step 3: Commit**

```bash
git add .env.example deploy.md
git commit -m "docs: add PSN_NPSSO to .env.example and deploy.md"
```

---

## Done

PSN data module complete. Requires one-time manual NPSSO cookie extraction. Silently skips if `PSN_NPSSO` is absent.

**Known limitation:** Only played titles appear in the library (`title_stats()` tracks play history, not purchases). Unplayed digital purchases will not show up — this is a PSN platform limitation.
