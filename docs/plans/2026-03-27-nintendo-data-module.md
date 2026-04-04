# Nintendo Switch Data Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `steam_mcp/data/nintendo.py` — an async module that fetches Nintendo Switch play history via the `nxapi` CLI, deduplicates against existing `games` rows using fuzzy matching, and upserts into `game_platforms` with playtime in minutes.

**Architecture:** Single async `sync_nintendo()` function. Auth uses a Nintendo Session Token stored as `NINTENDO_SESSION_TOKEN` in `.env` (one-time manual extraction via `nxapi nso auth`). `nxapi` is invoked as a subprocess; its JSON output is parsed for title name and total play time. Missing credentials cause a silent skip. Fuzzy dedup via `find_game_by_name_fuzzy()` (cutoff=85). Candidates are pre-loaded via `load_fuzzy_candidates()` for efficiency, matching the Epic/GOG pattern.

**Schema context (v2):** The `games` table has no `appid` column — platform-specific IDs live in `game_platform_identifiers`. `upsert_game_platform` returns the `game_platforms.id` (platform_id). If nxapi exposes a stable Nintendo title ID (e.g. `titleId`), store it via `upsert_game_platform_identifier` using a `nintendo_title_id` identifier type. If not present in the output, skip the identifier upsert — do not store None.

**Important caveat from spec:** nxapi exposes play history only — not full purchase records. Unplayed digital purchases and physical cartridges never inserted will not appear. This is a Nintendo platform limitation with no workaround.

**Tech Stack:** Python 3.12, `nxapi` CLI subprocess, `rapidfuzz`, existing `upsert_game` / `upsert_game_platform` / `find_game_by_name_fuzzy` / `load_fuzzy_candidates` helpers from `db.py`.

---

### Task 1: Verify nxapi play history output format

Before writing code, confirm the exact JSON output of `nxapi nso play-history --json` on your machine.

**Files:** none (investigation only)

- [ ] **Step 1: Check nxapi version and available commands**

```bash
nxapi --version
nxapi nso --help
```

- [ ] **Step 2: Run play history and capture output**

```bash
nxapi nso play-history --json 2>/dev/null | head -60
```

Note the exact shape — possibilities:
- `{"items": [{"name": "...", "titleId": "...", "playingMinutes": N}, ...]}`
- `[{"name": "...", "totalPlayedMinutes": N}, ...]`
- `{"titles": [{"gameName": "...", "totalPlayTime": N}, ...]}`

- [ ] **Step 3: Note the key names for title name and playtime**

Record which keys are used for:
- `name`: `name`, `title`, or `gameName`
- `playtime`: `playingMinutes`, `totalPlayedMinutes`, or `totalPlayTime`
- `title_id` (if present): `titleId`, `id`, or similar

These determine which `.get()` fallbacks to keep in `fetch_nintendo_play_history()`.

- [ ] **Step 4: Check if `--json` is a flag or requires a separate subcommand**

Some nxapi versions use `nxapi nso titles --json` instead of `nxapi nso play-history --json`. Note which works on your installed version.

---

### Task 2: Create `steam_mcp/data/nintendo.py`

**Files:**
- Create: `steam_mcp/data/nintendo.py`

- [ ] **Step 1: Write the module**

Adjust the key names in `fetch_nintendo_play_history` based on Task 1 findings.

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

from steam_mcp.data.db import (
    find_game_by_name_fuzzy,
    load_fuzzy_candidates,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
)

logger = logging.getLogger(__name__)

NXAPI_BIN = os.getenv("NXAPI_BIN", "nxapi")
NINTENDO_TITLE_ID = "nintendo_title_id"


def _nxapi_available() -> bool:
    return shutil.which(NXAPI_BIN) is not None


async def _run_nxapi(*args: str) -> str:
    """Run an nxapi CLI command and return stdout as a string."""
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
    Return play history as a list of dicts with keys:
      - name (str): game title
      - playtime_minutes (int | None): total play time in minutes
      - title_id (str | None): Nintendo title ID if available
    """
    # Primary: `nxapi nso play-history --json`; fallback: `nxapi nso titles --json`
    try:
        raw = await _run_nxapi("nso", "play-history", "--json")
    except RuntimeError:
        raw = await _run_nxapi("nso", "titles", "--json")

    data = json.loads(raw)

    # nxapi returns {"items": [...]} or {"titles": [...]} or a bare list
    items = data if isinstance(data, list) else data.get("items", data.get("titles", []))

    results = []
    for item in items:
        name = item.get("name") or item.get("title") or item.get("gameName")
        if not name:
            continue

        # Playtime may be in minutes or seconds depending on nxapi version
        minutes = (
            item.get("playingMinutes")
            or item.get("totalPlayedMinutes")
            or item.get("totalPlayTime")
        )
        # Heuristic: values >10000 are likely seconds; convert
        if minutes and minutes > 10_000:
            minutes = minutes // 60

        title_id = item.get("titleId") or item.get("id")

        results.append({
            "name": str(name),
            "playtime_minutes": int(minutes) if minutes else None,
            "title_id": str(title_id) if title_id else None,
        })

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

    candidates = await load_fuzzy_candidates()

    for entry in history:
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

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="switch",
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
        )

        if entry["title_id"]:
            await upsert_game_platform_identifier(
                platform_id, NINTENDO_TITLE_ID, entry["title_id"]
            )

    logger.info("Nintendo sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "import steam_mcp.data.nintendo"
```

Expected: no output, no errors.

- [ ] **Step 3: Commit**

```bash
git add steam_mcp/data/nintendo.py
git commit -m "feat: add nintendo.py — Switch play history sync via nxapi CLI"
```

---

### Task 3: Wire into `refresh_library`

**Files:**
- Modify: `steam_mcp/tools/admin.py`

- [ ] **Step 1: Add `sync_nintendo` to the fan-out in `refresh_library`**

In `steam_mcp/tools/admin.py`, import `sync_nintendo` from `steam_mcp.data.nintendo` and add it to the platform sync fan-out alongside `sync_epic` and `sync_gog`.

- [ ] **Step 2: Commit**

```bash
git add steam_mcp/tools/admin.py
git commit -m "feat: wire Nintendo sync into refresh_library"
```

---

### Task 4: Smoke-test Nintendo sync (if token + nxapi are available)

Skip this task if `NINTENDO_SESSION_TOKEN` is not in `.env` or `nxapi` is not installed.

- [ ] **Step 1: Check prerequisites**

```bash
grep NINTENDO_SESSION_TOKEN .env
nxapi --version
```

If either is missing: skip to Task 5.

- [ ] **Step 2: Verify nxapi auth is working**

```bash
nxapi nso user
```

Expected: prints your Nintendo account info. If it errors, re-run `nxapi nso auth` to refresh the session token.

- [ ] **Step 3: Run sync**

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

- [ ] **Step 4: Verify game_platforms rows**

```bash
sqlite3 steam.db "SELECT g.name, gp.platform, gp.playtime_minutes FROM games g JOIN game_platforms gp ON gp.game_id = g.id WHERE gp.platform = 'switch' LIMIT 10;"
```

Expected: rows with Switch game titles and playtime values.

---

### Task 5: Update `.env.example` and `deploy.md`

- [ ] **Step 1: Add NINTENDO_SESSION_TOKEN to `.env.example`**

Add in the platform credentials section:
```
NINTENDO_SESSION_TOKEN=   # from: nxapi nso auth
NXAPI_BIN=nxapi           # optional: path to nxapi binary if not in PATH
```

- [ ] **Step 2: Add Nintendo section to `deploy.md`**

Add a brief "Nintendo in Docker" section:
- Install nxapi on the host machine (`npm install -g nxapi`)
- Run `nxapi nso auth` once to authenticate
- Copy the session token to `.env` on the server
- nxapi does not need to be installed in Docker — the subprocess runs on the host (or install it in the Dockerfile if desired)

- [ ] **Step 3: Commit**

```bash
git add .env.example deploy.md
git commit -m "docs: add NINTENDO_SESSION_TOKEN to .env.example and deploy.md"
```

---

## Done

Nintendo data module complete. Requires `nxapi` CLI installed and one-time `nxapi nso auth` flow. Silently skips if token or binary is absent.

**Known limitation:** Only played titles appear (Nintendo platform limitation). Playtime unit detection handles both minutes and seconds across nxapi versions. Nintendo title IDs are stored in `game_platform_identifiers` when available.
