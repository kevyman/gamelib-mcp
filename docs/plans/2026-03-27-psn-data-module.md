# PSN Data Module Implementation Plan

> **For Claude:** Use `${SUPERPOWERS_SKILLS_ROOT}/skills/collaboration/executing-plans/SKILL.md` to implement this plan task-by-task.

**Goal:** Add `steam_mcp/data/psn.py` — an async module that fetches the user's PS5 game library and playtime via PSNAWP, deduplicates against existing `games` rows using fuzzy matching, and upserts into `game_platforms`.

**Architecture:** Single async `sync_psn()` function. Auth uses an NPSSO cookie (one-time manual extraction from browser, stored as `PSN_NPSSO` in `.env`). PSNAWP is used to fetch the trophy title list as a proxy for the game library (it's the only public source of PS5 game ownership). Playtime comes from PS5 trophy timestamps (last trophy date used as a proxy — actual playtime minutes are not available from PSN's public API for PS5; we store `NULL`). Fuzzy dedup via `find_game_by_name_fuzzy()` (cutoff=85, already added in epic/gog plan).

**Tech Stack:** Python 3.12, `PSNAWP` library, `rapidfuzz`, existing `upsert_game` / `upsert_game_platform` / `find_game_by_name_fuzzy` helpers from `db.py`.

---

### Task 1: Add `PSNAWP` dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Check current dependencies**

```bash
cat pyproject.toml
```

**Step 2: Add `psnawp` to the `dependencies` list**

Add: `"psnawp>=2.1"`

**Step 3: Sync**

```bash
uv sync
```

Expected: resolves without errors.

**Step 4: Verify import**

```bash
python -c "from psnawp_api import PSNAWP; print('ok')"
```

Expected: `ok`

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add psnawp dependency for PSN library sync"
```

---

### Task 2: Create `steam_mcp/data/psn.py`

**Files:**
- Create: `steam_mcp/data/psn.py`

**Step 1: Write the module**

```python
"""PlayStation Network library sync via PSNAWP.

Auth: set PSN_NPSSO in .env.
Obtain the NPSSO cookie by visiting https://ca.account.sony.com/api/v1/ssocookie
while logged in to your PSN account in a browser. Copy the `npsso` value.

Playtime: PSN's public API does not expose playtime minutes for PS5 titles.
playtime_minutes is stored as NULL. Trophy data is NOT synced — library only.

Library source: trophy title list (all games where at least one trophy has been
earned). Games with no trophies will not appear. This is a PSN platform limitation.
"""

import logging
import os

from steam_mcp.data.db import find_game_by_name_fuzzy, upsert_game, upsert_game_platform

logger = logging.getLogger(__name__)


def _get_psnawp():
    """Return an authenticated PSNAWP instance, or raise if not configured."""
    from psnawp_api import PSNAWP  # lazy import — optional dependency
    npsso = os.environ.get("PSN_NPSSO")
    if not npsso:
        raise EnvironmentError("PSN_NPSSO not set")
    return PSNAWP(npsso)


async def fetch_psn_library() -> list[str]:
    """
    Return a list of PS5 game title strings from the user's trophy library.

    Runs PSNAWP synchronously in an executor to avoid blocking the event loop.
    """
    import asyncio

    def _fetch():
        psnawp = _get_psnawp()
        client = psnawp.me()
        titles = []
        for title in client.trophy_titles():
            # Filter to PS5 titles only; skip PS4 back-compat
            if hasattr(title, "np_service_name") and "ps5" in (title.np_service_name or "").lower():
                titles.append(title.title_name)
            elif not hasattr(title, "np_service_name"):
                # Older PSNAWP versions — include all, can't filter
                titles.append(title.title_name)
        return titles

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
        titles = await fetch_psn_library()
    except Exception as exc:
        logger.warning("PSN sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    for title in titles:
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
            platform="ps5",
            playtime_minutes=None,  # not available via public PSN API
            owned=1,
        )

    logger.info("PSN sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
```

**Step 2: Verify the module imports cleanly**

```bash
python -c "import steam_mcp.data.psn"
```

Expected: no output, no errors.

**Step 3: Commit**

```bash
git add steam_mcp/data/psn.py
git commit -m "feat: add psn.py — PSN library sync via PSNAWP trophy title list"
```

---

### Task 3: Smoke-test PSN sync (if PSN_NPSSO is set)

Skip this task if `PSN_NPSSO` is not in `.env`.

**Step 1: Check env**

```bash
grep PSN_NPSSO .env
```

If not present: skip to Task 4.

**Step 2: Run sync**

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

**Step 3: Verify game_platforms rows**

```bash
sqlite3 steam.db "SELECT g.name, gp.platform FROM games g JOIN game_platforms gp ON gp.game_id = g.id WHERE gp.platform = 'ps5' LIMIT 10;"
```

Expected: rows with PSN game titles and `platform='ps5'`.

---

### Task 4: Push branch

```bash
git push -u origin claude/integrate-superpowers-plugin-0S1aq
```

Expected: branch pushed, no errors.

---

## Done

PSN data module complete. Requires one-time manual NPSSO cookie extraction. Silently skips if `PSN_NPSSO` is absent.

**Known limitation:** Only games with at least one trophy earned appear in the library (PSN platform limitation — no workaround via public API).

Next plan: Nintendo Switch data module (`nintendo.py` via nxapi) or Xbox (`xbox.py`).
