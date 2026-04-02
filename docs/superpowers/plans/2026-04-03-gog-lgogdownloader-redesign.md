# GOG lgogdownloader Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken GOG OAuth implementation in `gog.py` with a lgogdownloader-based approach that works reliably in local and Docker/cloud environments.

**Architecture:** `sync_gog()` shells out to `lgogdownloader --list-games` non-interactively, using the session stored in lgogdownloader's cache dir. The cache dir is mounted read-only into Docker — auth happens once locally, never in the cloud. `aiohttp` (previously only used by GOG) is removed. The `upsert_game_platform_identifier` call is dropped since `--list-games` doesn't expose GOG product IDs.

**Tech Stack:** Python 3.12, `asyncio.create_subprocess_exec`, lgogdownloader CLI (system package), existing `find_game_by_name_fuzzy` / `upsert_game` / `upsert_game_platform` helpers from `db.py`.

---

## File Map

| File | Change |
|------|--------|
| `steam_mcp/data/gog.py` | Full rewrite |
| `tests/test_gog.py` | Create |
| `pyproject.toml` | Remove `aiohttp` dep |
| `Dockerfile` | Add `lgogdownloader` apt install |
| `docker-compose.yml` | Add GOG volume + env var |
| `.env.example` | Replace GOG OAuth vars |
| `deploy.md` | Add GOG auth section |

---

### Task 1: Verify lgogdownloader output format

Before writing any code, confirm the exact output of `lgogdownloader --list-games` on your machine. This determines how the parser should work.

**Files:** none (investigation only)

- [ ] **Step 1: Install lgogdownloader**

```bash
sudo apt install lgogdownloader
```

- [ ] **Step 2: Authenticate once**

```bash
lgogdownloader --login
```

Follow the prompts (lgogdownloader will open a GOG login URL and ask you to paste the resulting redirect URL back). This writes your session to `~/.cache/lgogdownloader/`.

- [ ] **Step 3: Run and capture output**

```bash
lgogdownloader --list-games 2>/dev/null | head -20
```

Note the exact format. Expected possibilities:
- One title per line: `Cyberpunk 2077`
- Slug format: `cyberpunk_2077`
- With ID: `Cyberpunk 2077 (123456789)`
- With header line: `Found 42 games (0 hidden)\nCyberpunk 2077\n...`

- [ ] **Step 4: Check for a JSON flag**

```bash
lgogdownloader --help 2>&1 | grep -i json
lgogdownloader --help 2>&1 | grep -i output
```

If a `--json` or `--output-format=json` flag exists, note it — it would let us get game IDs in a future improvement.

- [ ] **Step 5: Note whether titles or slugs are output**

If slugs (e.g., `cyberpunk_2077`), the `_parse_lgogdownloader_output()` function in Task 3 must convert them: replace `_` with space, title-case the result. If titles, no conversion needed. **This is the only thing that may need adjustment from the plan's code.**

---

### Task 2: Write failing tests for `gog.py`

Write all tests before touching `gog.py`. They will fail until Task 3.

**Files:**
- Create: `tests/test_gog.py`

- [ ] **Step 1: Create test file**

```python
# tests/test_gog.py

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from steam_mcp.data import gog


class ParseOutputTests(unittest.TestCase):
    """Tests for _parse_lgogdownloader_output() — pure function, no I/O."""

    def test_parses_titles_one_per_line(self):
        output = "Cyberpunk 2077\nThe Witcher 3: Wild Hunt\nGOG Galaxy\n"
        result = gog._parse_lgogdownloader_output(output)
        self.assertEqual(result, ["Cyberpunk 2077", "The Witcher 3: Wild Hunt", "GOG Galaxy"])

    def test_skips_blank_lines(self):
        output = "Game One\n\nGame Two\n\n"
        result = gog._parse_lgogdownloader_output(output)
        self.assertEqual(result, ["Game One", "Game Two"])

    def test_skips_found_header_line(self):
        output = "Found 3 games (0 hidden)\nGame One\nGame Two\nGame Three\n"
        result = gog._parse_lgogdownloader_output(output)
        self.assertEqual(result, ["Game One", "Game Two", "Game Three"])

    def test_strips_whitespace(self):
        output = "  Game With Spaces  \n  Another Game  \n"
        result = gog._parse_lgogdownloader_output(output)
        self.assertEqual(result, ["Game With Spaces", "Another Game"])

    def test_empty_output_returns_empty_list(self):
        self.assertEqual(gog._parse_lgogdownloader_output(""), [])


class SyncGogSkipTests(unittest.TestCase):
    """Tests for the three silent-skip conditions in sync_gog()."""

    def test_skips_when_lgogdownloader_not_in_path(self):
        with patch("shutil.which", return_value=None):
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_skips_when_cache_dir_missing(self):
        with (
            patch("shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("steam_mcp.data.gog._cache_dir", return_value=Path("/nonexistent/path")),
        ):
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})

    def test_skips_on_nonzero_returncode(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"auth error"))

        with (
            patch("shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("steam_mcp.data.gog._cache_dir", return_value=Path("/tmp")),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
        ):
            result = asyncio.run(gog.sync_gog())
        self.assertEqual(result, {"added": 0, "matched": 0, "skipped": 0})


class SyncGogSyncTests(unittest.TestCase):
    """Integration-style tests for the sync loop."""

    def _make_proc(self, stdout: bytes, returncode: int = 0):
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.communicate = AsyncMock(return_value=(stdout, b""))
        return mock_proc

    def test_matched_game_increments_matched(self):
        mock_proc = self._make_proc(b"Cyberpunk 2077\n")
        mock_existing = {"id": 42}

        with (
            patch("shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("steam_mcp.data.gog._cache_dir", return_value=Path("/tmp")),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
            patch("steam_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("steam_mcp.data.gog.find_game_by_name_fuzzy", AsyncMock(return_value=mock_existing)),
            patch("steam_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
        ):
            result = asyncio.run(gog.sync_gog())

        self.assertEqual(result, {"added": 0, "matched": 1, "skipped": 0})

    def test_unmatched_game_increments_added(self):
        mock_proc = self._make_proc(b"New Unknown Game\n")

        with (
            patch("shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("steam_mcp.data.gog._cache_dir", return_value=Path("/tmp")),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
            patch("steam_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("steam_mcp.data.gog.find_game_by_name_fuzzy", AsyncMock(return_value=None)),
            patch("steam_mcp.data.gog.upsert_game", AsyncMock(return_value=99)),
            patch("steam_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
        ):
            result = asyncio.run(gog.sync_gog())

        self.assertEqual(result, {"added": 1, "matched": 0, "skipped": 0})

    def test_upsert_game_platform_called_with_none_playtime(self):
        """GOG doesn't expose playtime — must always pass playtime_minutes=None."""
        mock_proc = self._make_proc(b"Some Game\n")
        upsert_platform_mock = AsyncMock(return_value=1)

        with (
            patch("shutil.which", return_value="/usr/bin/lgogdownloader"),
            patch("steam_mcp.data.gog._cache_dir", return_value=Path("/tmp")),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)),
            patch("steam_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
            patch("steam_mcp.data.gog.find_game_by_name_fuzzy", AsyncMock(return_value=None)),
            patch("steam_mcp.data.gog.upsert_game", AsyncMock(return_value=1)),
            patch("steam_mcp.data.gog.upsert_game_platform", upsert_platform_mock),
        ):
            asyncio.run(gog.sync_gog())

        upsert_platform_mock.assert_called_once_with(
            game_id=1,
            platform="gog",
            playtime_minutes=None,
            owned=1,
        )
```

- [ ] **Step 2: Run tests — verify they all fail**

```bash
python -m pytest tests/test_gog.py -v 2>&1 | head -40
```

Expected: all tests fail with `ImportError` or `AttributeError` (functions don't exist yet). If any pass unexpectedly, investigate before proceeding.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_gog.py
git commit -m "test: add failing tests for gog lgogdownloader redesign"
```

---

### Task 3: Rewrite `gog.py`

Replace the entire file. **If Task 1 revealed that `--list-games` outputs slugs instead of titles**, adjust `_parse_lgogdownloader_output()` to convert slugs: `title = line.replace("_", " ").title()`.

**Files:**
- Rewrite: `steam_mcp/data/gog.py`

- [ ] **Step 1: Rewrite the file**

```python
"""GOG owned games sync via lgogdownloader CLI.

One-time local setup:
  1. Install lgogdownloader (apt install lgogdownloader)
  2. Run: lgogdownloader --login
  3. Mount ~/.cache/lgogdownloader/ into Docker (see deploy.md)

Playtime is not available from lgogdownloader output.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from steam_mcp.data.db import (
    find_game_by_name_fuzzy,
    load_fuzzy_candidates,
    upsert_game,
    upsert_game_platform,
)

logger = logging.getLogger(__name__)

_LGOGDOWNLOADER_BIN = "lgogdownloader"


def _cache_dir() -> Path:
    """Return the lgogdownloader cache directory."""
    override = os.getenv("LGOGDOWNLOADER_CACHE_PATH")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "lgogdownloader"


def _subprocess_env() -> dict:
    """
    Build env dict for lgogdownloader subprocess.

    lgogdownloader respects XDG_CACHE_HOME for its session storage.
    We set it to the parent of _cache_dir() so that lgogdownloader
    finds its data at the expected path.
    """
    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = str(_cache_dir().parent)
    return env


def _parse_lgogdownloader_output(stdout: str) -> list[str]:
    """
    Parse lgogdownloader --list-games stdout into a list of game titles.

    Skips blank lines and the 'Found N games' header line that some
    versions emit. If your lgogdownloader version outputs slugs
    (e.g. witcher_3_wild_hunt) instead of titles, convert here:
        title = line.replace("_", " ").title()
    """
    titles = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("found ") and "game" in line.lower():
            continue
        titles.append(line)
    return titles


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms via lgogdownloader.

    Silent skip conditions:
    - lgogdownloader binary not in PATH
    - lgogdownloader cache dir does not exist (no session stored)

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not shutil.which(_LGOGDOWNLOADER_BIN):
        logger.info("lgogdownloader not in PATH — skipping GOG sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    cache_path = _cache_dir()
    if not cache_path.exists():
        logger.info(
            "lgogdownloader cache dir not found (%s) — skipping GOG sync", cache_path
        )
        return {"added": 0, "matched": 0, "skipped": 0}

    try:
        proc = await asyncio.create_subprocess_exec(
            _LGOGDOWNLOADER_BIN,
            "--list-games",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
    except Exception as exc:
        logger.warning("GOG sync failed (subprocess error): %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    if proc.returncode != 0:
        logger.warning(
            "lgogdownloader --list-games failed (rc=%d): %s",
            proc.returncode,
            stderr_bytes.decode()[:300],
        )
        return {"added": 0, "matched": 0, "skipped": 0}

    titles = _parse_lgogdownloader_output(stdout_bytes.decode())
    if not titles:
        logger.info("GOG sync: no games found in lgogdownloader output")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for title in titles:
        existing = await find_game_by_name_fuzzy(title, candidates=candidates)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=title)
            candidates[game_id] = title
            added += 1

        await upsert_game_platform(
            game_id=game_id,
            platform="gog",
            playtime_minutes=None,
            owned=1,
        )

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "import steam_mcp.data.gog; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Run the tests — all must pass**

```bash
python -m pytest tests/test_gog.py -v
```

Expected: all 9 tests pass. If any fail, fix `gog.py` before continuing.

- [ ] **Step 4: Commit**

```bash
git add steam_mcp/data/gog.py
git commit -m "feat: rewrite gog.py — replace OAuth with lgogdownloader subprocess"
```

---

### Task 4: Remove `aiohttp` dependency

`aiohttp` was only imported by `gog.py`. The new implementation uses no external HTTP library.

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Verify nothing else imports aiohttp**

```bash
grep -r "aiohttp" steam_mcp/
```

Expected: no output. If any file still imports it, do not remove the dependency yet — investigate first.

- [ ] **Step 2: Remove from pyproject.toml**

In `pyproject.toml`, remove the line:
```
    "aiohttp>=3.9",
```

- [ ] **Step 3: Sync and verify**

```bash
uv sync
python -c "import steam_mcp.main; print('ok')"
```

Expected: `ok` with no import errors.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: remove aiohttp dep — no longer used after gog.py rewrite"
```

---

### Task 5: Update `Dockerfile`

Add `lgogdownloader` as a system package so the container can run it non-interactively against the mounted session.

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Read the current Dockerfile**

Current contents:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY steam_mcp/ steam_mcp/
RUN pip install -e .
CMD ["python", "-m", "steam_mcp.main"]
```

- [ ] **Step 2: Add lgogdownloader install**

Replace with:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y lgogdownloader && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY steam_mcp/ steam_mcp/
RUN pip install -e .
CMD ["python", "-m", "steam_mcp.main"]
```

- [ ] **Step 3: Verify the build succeeds**

```bash
docker compose build steam-mcp 2>&1 | tail -10
```

Expected: `Successfully built ...` or `=> exporting to image`. If `lgogdownloader` is not found in the Debian repos (unlikely but possible for `python:3.12-slim` which is Debian-based), try `apt-cache show lgogdownloader` inside the container to confirm availability.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "chore: add lgogdownloader to Docker image"
```

---

### Task 6: Update `docker-compose.yml`

Wire the GOG cache directory as a read-only volume, mirroring the existing Epic/Legendary pattern.

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Read current docker-compose.yml**

Current relevant section:
```yaml
services:
  steam-mcp:
    environment:
      EPIC_LEGENDARY_PATH: /legendary
    volumes:
      - ./data/steam:/data
      - ${EPIC_LEGENDARY_HOST_PATH:-./data/legendary}:/legendary:ro
    env_file: .env
```

- [ ] **Step 2: Add GOG volume and env var**

```yaml
services:
  steam-mcp:
    environment:
      EPIC_LEGENDARY_PATH: /legendary
      LGOGDOWNLOADER_CACHE_PATH: /cache/lgogdownloader
    volumes:
      - ./data/steam:/data
      - ${EPIC_LEGENDARY_HOST_PATH:-./data/legendary}:/legendary:ro
      - ${LGOGDOWNLOADER_HOST_PATH:-./data/lgogdownloader}:/cache/lgogdownloader:ro
    env_file: .env
```

The mount point `/cache/lgogdownloader` means the container's `XDG_CACHE_HOME` will be set to `/cache` by `_subprocess_env()`, and lgogdownloader will look for its data at `/cache/lgogdownloader`. ✓

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "chore: add lgogdownloader volume mount to docker-compose"
```

---

### Task 7: Update `.env.example` and `deploy.md`

**Files:**
- Modify: `.env.example`
- Modify: `deploy.md`

- [ ] **Step 1: Update `.env.example`**

Remove these three lines:
```
# (remove if present)
GOG_REFRESH_TOKEN=
GOG_CLIENT_ID=
GOG_CLIENT_SECRET=
```

Add this line in the platform credentials section:
```
LGOGDOWNLOADER_HOST_PATH=   # host path to lgogdownloader cache; defaults to ./data/lgogdownloader
```

- [ ] **Step 2: Add GOG section to `deploy.md`**

In `deploy.md`, after the "Epic in Docker" section, add:

```markdown
### GOG in Docker

GOG sync uses lgogdownloader. Auth is done once on the host machine; the session is mounted read-only into the container.

**One-time local setup:**

```bash
# On your local machine (not the server)
sudo apt install lgogdownloader
lgogdownloader --login   # opens browser OAuth, stores session to ~/.cache/lgogdownloader/
```

**Copy the session to the server:**

```bash
rsync -av ~/.cache/lgogdownloader/ root@178.104.53.83:~/mcps/data/lgogdownloader/
```

**Server `.env`** (add):
```
LGOGDOWNLOADER_HOST_PATH=/root/mcps/data/lgogdownloader
```

lgogdownloader refreshes its session automatically on each `--list-games` call — no manual token rotation needed. If the session expires, re-run `lgogdownloader --login` locally and rsync again.
```

- [ ] **Step 3: Commit**

```bash
git add .env.example deploy.md
git commit -m "docs: update .env.example and deploy.md for lgogdownloader GOG setup"
```

---

### Task 8: Final smoke test

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass (gog, epic, db migration).

- [ ] **Step 2: Verify the module stack imports cleanly**

```bash
python -c "import steam_mcp.main; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Optional — live smoke test if lgogdownloader is authenticated**

```bash
python -c "
import asyncio
from steam_mcp.data.gog import sync_gog
print(asyncio.run(sync_gog()))
"
```

Expected: `{'added': N, 'matched': M, 'skipped': 0}` or `{'added': 0, 'matched': 0, 'skipped': 0}` if not authenticated locally (silent skip is correct).

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/epic-gog-data-modules
```

---

## Note for tool-changes plan

When implementing `steam_mcp/tools/admin.py` (the `refresh_library` fan-out from `docs/plans/2026-03-27-tool-changes.md`), the GOG skip condition must check `shutil.which("lgogdownloader") and _cache_dir().exists()` — not `os.getenv("GOG_REFRESH_TOKEN")`. Import `_cache_dir` from `steam_mcp.data.gog` or duplicate the logic inline.
