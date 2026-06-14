"""App lifecycle: startup/shutdown lifespan, background task orchestration.

This module owns everything that used to crowd ``main.py``: the FastMCP
``lifespan`` context manager, the startup library refresh, background enrichment
scheduling, the periodic refresh loop, and the per-platform sync-metadata
helpers consumed by the startup refresh.

It deliberately does NOT import ``gamelib_mcp.tools.admin`` at module load time.
The tool layer (``admin.py``) imports orchestration primitives from here at the
top level; this module reaches back into ``admin.refresh_library`` lazily (via
the patchable ``_admin_refresh_library`` global), so the dependency is a clean
one-way edge ``tools.admin -> lifecycle`` with no import cycle.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)

# Platforms whose per-run sync outcome is recorded in the meta table.
SYNC_METADATA_PLATFORMS = ("steam", "epic", "gog", "nintendo", "ps5")

# Lazily bound to tools.admin.refresh_library on first startup refresh. Kept as a
# module-level name so tests can patch it directly.
_admin_refresh_library = None

_LIBRARY_REFRESH_TASK: asyncio.Task | None = None
_LIBRARY_REFRESH_LOCK: asyncio.Lock | None = None
_PERIODIC_REFRESH_TASK: asyncio.Task | None = None
_PERIODIC_REFRESH_LOCK: asyncio.Lock | None = None
_ENRICHMENT_TASK: asyncio.Task | None = None
_ENRICHMENT_LOCK: asyncio.Lock | None = None
_ENRICHMENT_RERUN_REQUESTED = False
_RATINGS_SYNC_TASK: asyncio.Task | None = None
_LIBRARY_REFRESH_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_PERIODIC_REFRESH_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_ENRICHMENT_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_RATINGS_SYNC_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()

RATINGS_SYNC_INTERVAL_SECONDS = 7 * 24 * 3600  # weekly


# ── Per-platform sync metadata ───────────────────────────────────────────────

def classify_platform_sync_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("refresh token rejected", "expired", "npsso", "reauth", "auth", "login")):
        return "auth_stale"
    if any(token in lowered for token in ("not in path", "binary", "command not found", "executable", "no such file")):
        return "missing_runtime_dependency"
    if any(token in lowered for token in ("not set", "missing", "not configured", "no credentials")):
        return "missing_configuration"
    if any(token in lowered for token in ("timeout", "timed out", "network", "connection", "dns")):
        return "network"
    return "unexpected"


def _platform_sync_error_summary(payload: dict) -> str | None:
    error = payload.get("error")
    if isinstance(error, str) and error:
        return error

    summary = payload.get("error_summary")
    if not isinstance(summary, str) or not summary:
        summary = payload.get("summary")
    if not isinstance(summary, str) or not summary:
        return None

    status = payload.get("sync_status")
    if isinstance(status, str) and status not in {"ready", "success", "synced", "ok"}:
        return summary
    return None


def _platform_sync_error_classification(payload: dict, summary: str) -> str:
    classification = payload.get("error_classification")
    if isinstance(classification, str) and classification:
        return classification
    status = payload.get("sync_status")
    if status == "unconfigured":
        return "missing_configuration"
    if status == "degraded":
        return "missing_runtime_dependency"
    return classify_platform_sync_error(summary)


def build_platform_sync_metadata(refresh_result: dict, finished_at: str) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {}
    for platform in SYNC_METADATA_PLATFORMS:
        payload = refresh_result.get(platform)
        if not isinstance(payload, dict):
            continue

        prefix = f"integration_sync_{platform}"
        error = _platform_sync_error_summary(payload)
        metadata[f"{prefix}_last_attempt_at"] = finished_at
        metadata[f"{prefix}_last_finished_at"] = finished_at
        metadata[f"{prefix}_last_error_summary"] = error
        metadata[f"{prefix}_last_error_classification"] = (
            _platform_sync_error_classification(payload, error) if error else None
        )
        if not error:
            metadata[f"{prefix}_last_success_at"] = finished_at

    return metadata


# ── Per-event-loop locks ─────────────────────────────────────────────────────

def _get_library_refresh_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LIBRARY_REFRESH_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LIBRARY_REFRESH_LOCKS[loop] = lock
    return lock


def _get_periodic_refresh_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _PERIODIC_REFRESH_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _PERIODIC_REFRESH_LOCKS[loop] = lock
    return lock


def _get_enrichment_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _ENRICHMENT_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _ENRICHMENT_LOCKS[loop] = lock
    return lock


def _clear_library_refresh_task(task: asyncio.Task) -> None:
    global _LIBRARY_REFRESH_TASK
    if _LIBRARY_REFRESH_TASK is task:
        _LIBRARY_REFRESH_TASK = None


def _clear_periodic_refresh_task(task: asyncio.Task) -> None:
    global _PERIODIC_REFRESH_TASK
    if _PERIODIC_REFRESH_TASK is task:
        _PERIODIC_REFRESH_TASK = None


def _clear_enrichment_task(task: asyncio.Task) -> None:
    global _ENRICHMENT_TASK
    if _ENRICHMENT_TASK is task:
        _ENRICHMENT_TASK = None


def _get_ratings_sync_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _RATINGS_SYNC_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _RATINGS_SYNC_LOCKS[loop] = lock
    return lock


def _clear_ratings_sync_task(task: asyncio.Task) -> None:
    global _RATINGS_SYNC_TASK
    if _RATINGS_SYNC_TASK is task:
        _RATINGS_SYNC_TASK = None


async def _run_startup_ratings_sync() -> None:
    import time
    from .data.db import get_meta
    from .tools.ratings import sync_ratings

    # _run_startup_refresh drains background enrichment before completing, so
    # library_sync_status stays "in_progress" until all writes settle. Polling
    # is safer than awaiting the task handle directly.
    deadline = time.monotonic() + 900  # 15 min absolute ceiling
    while time.monotonic() < deadline:
        status = await get_meta("library_sync_status")
        if status != "in_progress":
            break
        await asyncio.sleep(15)

    try:
        logger.info("Running startup ratings sync")
        await sync_ratings()
        logger.info("Startup ratings sync complete")
    except Exception:
        logger.exception("Startup ratings sync failed")


async def _run_periodic_ratings_loop() -> None:
    from .tools.ratings import sync_ratings

    while True:
        await asyncio.sleep(RATINGS_SYNC_INTERVAL_SECONDS)
        try:
            logger.info("Running scheduled weekly ratings sync")
            await sync_ratings()
            logger.info("Weekly ratings sync complete")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic ratings sync failed")


async def _ensure_periodic_ratings_loop() -> asyncio.Task:
    global _RATINGS_SYNC_TASK

    async with _get_ratings_sync_lock():
        if _RATINGS_SYNC_TASK is not None and not _RATINGS_SYNC_TASK.done():
            return _RATINGS_SYNC_TASK

        _RATINGS_SYNC_TASK = asyncio.create_task(_run_periodic_ratings_loop())
        _RATINGS_SYNC_TASK.add_done_callback(_clear_ratings_sync_task)
        return _RATINGS_SYNC_TASK


# ── Background enrichment ────────────────────────────────────────────────────

async def _run_background_enrich() -> None:
    from .data.enrich_bg import background_enrich

    await background_enrich()


async def _schedule_background_enrich() -> asyncio.Task:
    global _ENRICHMENT_TASK
    global _ENRICHMENT_RERUN_REQUESTED

    async with _get_enrichment_lock():
        if _ENRICHMENT_TASK is not None and not _ENRICHMENT_TASK.done():
            _ENRICHMENT_RERUN_REQUESTED = True
            return _ENRICHMENT_TASK

        _ENRICHMENT_RERUN_REQUESTED = False
        _ENRICHMENT_TASK = asyncio.create_task(_run_background_enrich())
        _ENRICHMENT_TASK.add_done_callback(_clear_enrichment_task)
        return _ENRICHMENT_TASK


async def _drain_background_enrich_reruns() -> None:
    global _ENRICHMENT_RERUN_REQUESTED

    while True:
        task = await _schedule_background_enrich()
        should_exit = False
        try:
            await task
        finally:
            async with _get_enrichment_lock():
                if not _ENRICHMENT_RERUN_REQUESTED:
                    should_exit = True
                else:
                    _ENRICHMENT_RERUN_REQUESTED = False
        if should_exit:
            return


# ── Library refresh ──────────────────────────────────────────────────────────

def _library_refresh_interval_seconds() -> float | None:
    raw_value = os.getenv("LIBRARY_REFRESH_INTERVAL_HOURS", "24").strip()
    if not raw_value:
        return 24 * 3600

    try:
        hours = float(raw_value)
    except ValueError:
        logger.warning(
            "Invalid LIBRARY_REFRESH_INTERVAL_HOURS=%r; defaulting to 24 hours",
            raw_value,
        )
        return 24 * 3600

    if hours <= 0:
        logger.info("Periodic library refresh disabled via LIBRARY_REFRESH_INTERVAL_HOURS=%s", raw_value)
        return None

    return hours * 3600


def _summarize_refresh_result(result: object) -> str | None:
    if not isinstance(result, dict):
        return None

    errors: list[str] = []
    for platform, payload in result.items():
        if not isinstance(payload, dict):
            continue
        error = payload.get("error") or payload.get("error_summary")
        if error:
            errors.append(f"{platform}: {error}")

    return "; ".join(errors) if errors else None


async def _run_startup_refresh() -> dict:
    global _admin_refresh_library
    from .data.db import set_meta_many

    if _admin_refresh_library is None:
        from .tools.admin import run_library_sync
        _admin_refresh_library = run_library_sync

    started_at = datetime.now(timezone.utc).isoformat()
    await set_meta_many(
        {
            "library_sync_status": "in_progress",
            "library_sync_started_at": started_at,
            "library_sync_finished_at": None,
            "library_sync_error": None,
        }
    )

    final_error: str | None = None
    cancelled = False
    refresh_result: dict | None = None
    try:
        refresh_result = await _admin_refresh_library()
        final_error = _summarize_refresh_result(refresh_result)
        if final_error:
            logger.warning("Startup library refresh completed with partial errors: %s", final_error)
    except asyncio.CancelledError:
        cancelled = True
        final_error = "cancelled"
        raise
    except Exception as exc:
        logger.exception("Startup library refresh failed")
        final_error = str(exc)
    finally:
        finished_at = datetime.now(timezone.utc).isoformat()
        finished_meta = {
            "library_sync_status": "idle",
            "library_sync_finished_at": finished_at,
            "library_sync_error": final_error,
        }
        if refresh_result is not None:
            finished_meta.update(build_platform_sync_metadata(refresh_result, finished_at))
        await asyncio.shield(
            set_meta_many(
                finished_meta
            )
        )
        if cancelled:
            logger.info("Startup library refresh cancelled")

    if refresh_result is not None:
        await _drain_background_enrich_reruns()

    return refresh_result or {}


async def _ensure_startup_refresh() -> asyncio.Task:
    global _LIBRARY_REFRESH_TASK

    async with _get_library_refresh_lock():
        if _LIBRARY_REFRESH_TASK is not None and not _LIBRARY_REFRESH_TASK.done():
            return _LIBRARY_REFRESH_TASK

        _LIBRARY_REFRESH_TASK = asyncio.create_task(_run_startup_refresh())
        _LIBRARY_REFRESH_TASK.add_done_callback(_clear_library_refresh_task)
        return _LIBRARY_REFRESH_TASK


def get_startup_refresh_task() -> asyncio.Task | None:
    """Current startup-refresh task, if one is in flight (used by refresh_library)."""
    return _LIBRARY_REFRESH_TASK


async def _run_periodic_refresh_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _ensure_startup_refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic library refresh scheduling failed")


async def _ensure_periodic_refresh_loop(interval_seconds: float | None = None) -> asyncio.Task | None:
    global _PERIODIC_REFRESH_TASK

    resolved_interval = interval_seconds if interval_seconds is not None else _library_refresh_interval_seconds()
    if resolved_interval is None:
        return None

    async with _get_periodic_refresh_lock():
        if _PERIODIC_REFRESH_TASK is not None and not _PERIODIC_REFRESH_TASK.done():
            return _PERIODIC_REFRESH_TASK

        _PERIODIC_REFRESH_TASK = asyncio.create_task(_run_periodic_refresh_loop(resolved_interval))
        _PERIODIC_REFRESH_TASK.add_done_callback(_clear_periodic_refresh_task)
        return _PERIODIC_REFRESH_TASK


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(app):
    """Startup: init DB, sync library if stale, kick off HLTB pre-warm."""
    from .data.db import clear_all_enrichment_claims, init_db, get_meta, set_meta
    from .data.steam_xml import STALE_HOURS

    await init_db()
    await clear_all_enrichment_claims()
    logger.info("Database initialized")

    # Seed hardware preference from env if not yet set
    hw_pref_env = os.getenv("HARDWARE_PREFERENCE")
    if hw_pref_env and not await get_meta("hardware_preference"):
        import json
        await set_meta("hardware_preference", json.dumps(hw_pref_env.split(",")))
        logger.info("Seeded hardware_preference from HARDWARE_PREFERENCE env var")

    # Refresh library if stale or missing
    last_sync = await get_meta("library_synced_at")
    needs_refresh = True
    if last_sync:
        try:
            dt = datetime.fromisoformat(last_sync)
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            needs_refresh = age_hours > STALE_HOURS
        except ValueError:
            pass

    if needs_refresh:
        logger.info("Library stale or missing — scheduling background refresh...")
        # Mark in_progress now so the startup ratings sync task sees it immediately
        # before the refresh task has had a chance to write it itself.
        await set_meta("library_sync_status", "in_progress")
        await _ensure_startup_refresh()
        await _schedule_background_enrich()
    else:
        # Background enrichment: store/provider metadata, ratings, and discovery signals
        await _schedule_background_enrich()

    await _ensure_periodic_refresh_loop()

    # Sync ratings on startup if never run or stale (>7 days), then schedule weekly
    last_ratings_sync = await get_meta("ratings_synced_at")
    ratings_stale = True
    if last_ratings_sync:
        try:
            dt = datetime.fromisoformat(last_ratings_sync)
            age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
            ratings_stale = age_seconds > RATINGS_SYNC_INTERVAL_SECONDS
        except ValueError:
            pass
    if ratings_stale:
        logger.info("Ratings stale or missing — scheduling background ratings sync...")
        asyncio.create_task(_run_startup_ratings_sync())
    await _ensure_periodic_ratings_loop()

    yield

    await _cancel_task(_PERIODIC_REFRESH_TASK)
    await _cancel_task(_LIBRARY_REFRESH_TASK)
    await _cancel_task(_ENRICHMENT_TASK)
    await _cancel_task(_RATINGS_SYNC_TASK)
    logger.info("Shutdown")
