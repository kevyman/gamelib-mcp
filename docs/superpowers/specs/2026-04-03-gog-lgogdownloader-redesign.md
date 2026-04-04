# GOG Module Redesign: lgogdownloader

**Date:** 2026-04-03
**Branch:** feat/epic-gog-data-modules
**Status:** Approved

## Context

The current `gog.py` uses hardcoded GOG Galaxy public OAuth credentials (`client_id`/`client_secret`) to exchange a `GOG_REFRESH_TOKEN` for an access token, then calls GOG's API directly. These credentials are brittle, have proven unreliable, and are not ours to depend on.

A secondary concern: the Epic module was noted as relying on a CLI, raising worry about cloud auth. **This concern does not apply to Epic** — `epic.py` reads Legendary's local config files directly (`user.json`, `metadata/*.json`) and never invokes the `legendary` CLI as a subprocess. The Docker setup mounts `~/.config/legendary/` as a read-only volume; no interactive auth happens at runtime. No changes needed to `epic.py`.

GOG is the module that needs redesigning.

## Approach

Use **lgogdownloader** — a well-maintained Linux CLI for GOG — as the auth and library-listing mechanism. lgogdownloader handles its own OAuth flow via `lgogdownloader --login` (browser-based, run once locally) and stores the session in its cache directory. At runtime, it operates non-interactively using those stored credentials.

This mirrors the Epic pattern: auth happens once on the developer's machine, credential files are mounted into Docker as a read-only volume, and the container never performs an interactive browser flow.

## Changes

### `gamelib_mcp/data/gog.py` — full rewrite

**Remove:**
- All OAuth logic: `_GOG_TOKEN_URL`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_get_access_token`, `_fetch_owned_game_ids`, `_fetch_game_title`
- `aiohttp` import (only used by this module; `aiohttp` is removed from dependencies)

**Add:**
- `shutil.which("lgogdownloader")` check — skip silently if not in PATH
- Cache dir resolution: default `~/.cache/lgogdownloader`, overridden by `LGOGDOWNLOADER_CACHE_PATH` env var. Skip silently if the dir doesn't exist (no session stored).
- Async subprocess call: `lgogdownloader --list-games [--conf-dir <path>]`
- Line-by-line parsing of stdout (one game title per line)
- Fuzzy match + upsert using existing helpers (same as current)

**Not included:** GOG product IDs are not exposed by `--list-games`, so `upsert_game_platform_identifier` is not called. This can be added later if lgogdownloader gains structured output or another ID source is found.

**Playtime:** remains unavailable (GOG platform limitation, unchanged).

**Skip conditions** (any of these → silent skip, return zeros):
- lgogdownloader binary not in PATH
- Cache directory does not exist

### `Dockerfile` — add system package

```dockerfile
RUN apt-get update && apt-get install -y lgogdownloader && rm -rf /var/lib/apt/lists/*
```

Add before the `pip install` step.

### `docker-compose.yml` — add GOG volume mount

```yaml
environment:
  LGOGDOWNLOADER_CACHE_PATH: /lgogdownloader-cache
volumes:
  - ${LGOGDOWNLOADER_HOST_PATH:-./data/lgogdownloader}:/lgogdownloader-cache:ro
```

Add alongside the existing `EPIC_LEGENDARY_PATH` / legendary volume entries.

### `pyproject.toml` — remove `aiohttp`

`aiohttp` is only imported by `gog.py`. Remove from the `dependencies` list and run `uv sync`.

### `.env.example` — replace GOG OAuth vars

Remove:
```
GOG_REFRESH_TOKEN=
GOG_CLIENT_ID=
GOG_CLIENT_SECRET=
```

Add:
```
LGOGDOWNLOADER_CACHE_PATH=   # optional; defaults to ~/.cache/lgogdownloader
```

### `gamelib_mcp/tools/admin.py` — update skip condition

The `refresh_library` fan-out currently checks `os.getenv("GOG_REFRESH_TOKEN")` to decide whether to skip GOG. Change to mirror `sync_gog()`'s own skip logic: check `shutil.which("lgogdownloader")` and whether the cache dir exists.

### `gamelib_mcp/setup_platform.py` — replace GOG handler

Replace the `_setup_gog()` function (which implements a full OAuth code-exchange flow) with a simple instructions printer:

```
Install lgogdownloader, then run: lgogdownloader --login
Follow the browser prompts to authenticate with your GOG account.
Mount ~/.cache/lgogdownloader/ (or LGOGDOWNLOADER_CACHE_PATH) into Docker as a read-only volume.
See deploy.md for the docker-compose snippet.
```

### `deploy.md` — add GOG section

Document the one-time local auth step and the docker-compose volume mount pattern.

## What Does Not Change

- `epic.py` — no changes; already cloud-safe
- Fuzzy matching logic — unchanged
- Database schema — unchanged
- All other platform modules — unchanged

## Auth Flow (One-Time Setup)

```
1. Install lgogdownloader (apt install lgogdownloader or build from source)
2. Run: lgogdownloader --login
3. Complete browser OAuth with your GOG account
4. lgogdownloader stores the session in ~/.cache/lgogdownloader/
5. Add to docker-compose.yml:
     LGOGDOWNLOADER_CACHE_PATH: /lgogdownloader-cache
     volumes:
       - ~/.cache/lgogdownloader:/lgogdownloader-cache:ro
6. Deploy — lgogdownloader runs non-interactively in the container using the mounted session
```

lgogdownloader refreshes the session automatically on each invocation; no manual token rotation needed.
