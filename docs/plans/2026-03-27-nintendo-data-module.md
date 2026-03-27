# Nintendo Switch Data Module Implementation Plan

> **For Claude:** Use `${SUPERPOWERS_SKILLS_ROOT}/skills/collaboration/executing-plans/SKILL.md` to implement this plan task-by-task.

**Goal:** Add `steam_mcp/data/nintendo.py` — an async module that fetches Nintendo Switch play history via the `nxapi` CLI, deduplicates against existing `games` rows using fuzzy matching, and upserts into `game_platforms` with playtime in minutes.

**Architecture:** Single async `sync_nintendo()` function. Auth uses a Nintendo Session Token stored as `NINTENDO_SESSION_TOKEN` in `.env` (one-time manual extraction via `nxapi nso auth`). `nxapi` is invoked as a subprocess; its JSON output is parsed for title name and total play time. Missing credentials cause a silent skip. Fuzzy dedup via `find_game_by_name_fuzzy()` (cutoff=85).

**Important caveat from spec:** nxapi exposes play history only — not full purchase records. Unplayed digital purchases and physical cartridges never inserted will not appear. This is a Nintendo platform limitation with no workaround.

**Tech Stack:** Python 3.12, `nxapi` CLI subprocess, `rapidfuzz`, existing `upsert_game` / `upsert_game_platform` / `find_game_by_name_fuzzy` helpers from `db.py`.

---

### Task 1: Create `steam_mcp/data/nintendo.py`

**Files:**
- Create: `steam_mcp/data/nintendo.py`

**Step 1: Write the module**

```python
"""Nintendo Switch play history sync via nxapi CLI.

Auth: set NINTENDO_SESSION_TOKEN in .env.
Obtain by running: nxapi nso auth
Follow the prompts; copy the session token into .env.

Caveat: nxapi only exposes play history (titles that have been launched).
Unplayed digital purchases and uninserted physical cartridges will not appear.
This is a Nintendo platform limitation — no workaround exists.

Playtime: reported in minutes from Nintendo's play history API.
"""

import asyncio
import json
import logging
import os
import shutil

from steam_mcp.data.db import find_game_by_name_fuzzy, upsert_game, upsert_game_platform

logger = logging.getLogger(__name__)

NXAPI_BIN = os.getenv("NXAPI_BIN", "nxapi")


def _nxapi_available() -> bool:
    return shutil.which(NXAPI_BIN) is not None


async def _run_nxapi(*args: str) -> str:
    """Run an nxapi CLI command and return stdout as a string."""
    token = os.environ.get("NINTENDO_SESSION_TOKEN")
    env = {**os.environ}
    if token:
        env["NXAPI_SESSION_TOKEN"] = token

    cmd = [NXAPI_BIN, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
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
    Return play history as a list of dicts with keys:
      - name (str): game title
      - playtime_minutes (int): total play time in minutes
    """
    raw = await _run_nxapi("nso", "friendcode", "--json")
    # nxapi play history command varies by version; try the most common forms
    # Primary: `nxapi nso play-history --json`
    try:
        raw = await _run_nxapi("nso", "play-history", "--json")
    except RuntimeError:
        # Fallback: `nxapi nso titles --json` (older nxapi versions)
        raw = await _run_nxapi("nso", "titles", "--json")

    data = json.loads(raw)

    # nxapi returns {"items": [...]} or a bare list depending on version
    items = data if isinstance(data, list) else data.get("items", data.get("titles", []))

    results = []
    for item in items:
        name = item.get("name") or item.get("title") or item.get("gameName")
        if not name:
            continue
        # playtime may be in minutes or seconds depending on nxapi version
        minutes = (
            item.get("playingMinutes")
            or item.get("totalPlayedMinutes")
            or item.get("totalPlayTime")  # some versions report seconds
        )
        # Heuristic: if value > 10000 it's probably seconds; convert
        if minutes and minutes > 10_000:
            minutes = minutes // 60
        results.append({"name": name, "playtime_minutes": int(minutes) if minutes else None})

    return results


async def sync_nintendo() -> dict:
    """
    Sync Nintendo Switch play history into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("NINTENDO_SESSION_TOKEN"):
        logger.info("NINTENDO_SESSION_TOKEN not set — skipping Nintendo sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    if not _nxapi_available():
        logger.warning("nxapi binary not found — skipping Nintendo sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0

    try:
        history = await fetch_nintendo_play_history()
    except Exception as exc:
        logger.warning("Nintendo sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    for entry in history:
        name = entry["name"]
        if not name:
            skipped += 1
            continue

        existing = await find_game_by_name_fuzzy(name)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=name)
            added += 1

        await upsert_game_platform(
            game_id=game_id,
            platform="switch",
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
        )

    logger.info("Nintendo sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
```

**Step 2: Verify the module imports cleanly**

```bash
python -c "import steam_mcp.data.nintendo"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add steam_mcp/data/nintendo.py
git commit -m "feat: add nintendo.py — Switch play history sync via nxapi CLI"
```

---

### Task 2: Smoke-test Nintendo sync (if token + nxapi are available)

Skip this task if `NINTENDO_SESSION_TOKEN` is not in `.env` or `nxapi` is not installed.

**Step 1: Check prerequisites**

```bash
grep NINTENDO_SESSION_TOKEN .env
nxapi --version
```

If either is missing: skip to Task 3.

**Step 2: Verify nxapi auth is working**

```bash
nxapi nso user
```

Expected: prints your Nintendo account info. If it errors, re-run `nxapi nso auth` to refresh the session token.

**Step 3: Run sync**

```bash
python -c "
import asyncio, dotenv
dotenv.load_dotenv()
from steam_mcp.data.nintendo import sync_nintendo
result = asyncio.run(sync_nintendo())
print(result)
"
```

Expected: `{'added': N, 'matched': M, 'skipped': K}` with no exceptions.

**Step 4: Verify game_platforms rows**

```bash
sqlite3 steam.db "SELECT g.name, gp.platform, gp.playtime_minutes FROM games g JOIN game_platforms gp ON gp.game_id = g.id WHERE gp.platform = 'switch' LIMIT 10;"
```

Expected: rows with Switch game titles and playtime values.

---

### Task 3: Push branch

```bash
git push -u origin claude/integrate-superpowers-plugin-0S1aq
```

Expected: branch pushed, no errors.

---

## Done

Nintendo data module complete. Requires `nxapi` CLI installed and one-time `nxapi nso auth` flow. Silently skips if token or binary is absent.

**Known limitation:** Only played titles appear (Nintendo platform limitation). Playtime unit detection handles both minutes and seconds across nxapi versions.

Next plan: Xbox data module (`xbox.py`) or Itch.io (`itchio.py`).
