"""In-memory liveness counters for the best-effort provider fetchers.

Every provider in this package swallows its own transport/parse failures and
answers ``None`` (or an empty list, or a status dict). That is the data layer's
contract — enrichment must never propagate a provider outage to a caller such
as ``get_game_detail`` — and it is exactly why the background enrichment run
could report "processed 300 rows, 0 failed" straight through a dead provider:
by the time ``enrich_bg`` sees the answer, the failure has already been turned
into a None that also means "this provider has no entry for that game".

So the failure is recorded where it happens instead. A provider calls
``record_failure`` at the point it swallows a transport/parse failure and
``record_success`` where it actually got an answer; ``enrich_bg`` diffs
snapshots around each claimed batch and attributes the delta to the rows it
just handled.

Two rules keep the signal honest:

- **"Not found" is not a failure.** A provider that answered and had no entry
  for the game did its job; that row is processed, not failed.
- **Counts are process-wide, not per caller.** A lazy ``get_game_detail``
  enrichment failing while a background pass runs inflates that pass's failure
  count by one, which is the intended reading: the provider is down for
  everyone. ``enrich_bg`` clamps a batch's failures to the rows it attempted so
  the ratio in the WARNING stays meaningful.

Deliberately in memory and per process, like the rest of the run stats: this is
a liveness signal for what is happening now, not history. Nothing here is
persisted, and nothing here fails a caller.
"""

import threading
from typing import Any

# Same cap as enrich_bg's run stats: a stored error is a diagnostic hint in a
# log line or a /health payload, never a payload of its own.
LAST_ERROR_CAP = 200

_LOCK = threading.Lock()
_COUNTERS: dict[str, dict[str, Any]] = {}


def _blank() -> dict[str, Any]:
    return {"successes": 0, "failures": 0, "last_error": None}


def record_failure(provider: str, exc: BaseException | str) -> None:
    """Count one swallowed transport/parse failure for ``provider``.

    ``exc`` may be the exception itself or a description of a failure shape
    that never raised (an HTTP status the provider handled by returning None,
    a status dict that means "the fetch did not work").
    """
    detail = exc if isinstance(exc, str) else repr(exc)
    with _LOCK:
        entry = _COUNTERS.setdefault(provider, _blank())
        entry["failures"] += 1
        entry["last_error"] = detail[:LAST_ERROR_CAP]


def record_success(provider: str) -> None:
    """Count one answered fetch — including an authoritative "not found"."""
    with _LOCK:
        _COUNTERS.setdefault(provider, _blank())["successes"] += 1


def snapshot() -> dict[str, dict[str, Any]]:
    """Per-provider {successes, failures, last_error}, as an independent copy."""
    with _LOCK:
        return {provider: dict(values) for provider, values in _COUNTERS.items()}


def failures(provider: str) -> int:
    """Failure count recorded for ``provider`` so far this process."""
    with _LOCK:
        return int(_COUNTERS.get(provider, {}).get("failures", 0))


def reset() -> None:
    """Drop every counter (tests, and anything that wants a clean baseline)."""
    with _LOCK:
        _COUNTERS.clear()
