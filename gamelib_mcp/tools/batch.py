"""Shared plumbing for *_batch tool variants.

Conventions (mirroring set_acquisitions_batch): items arrive as plain dicts and
are validated at runtime against a frozen per-tool key set; the only whole-call
failures are an empty items list or exceeding the item cap. Every other problem
is isolated to a per-item result with status="error" carrying the message and
the original item payload, so one bad item never halts or rolls back the rest.
Results preserve input order.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp.exceptions import ToolError

BATCH_ITEM_CAP = 200
# Detail payloads are an order of magnitude larger than other batch results.
DETAIL_BATCH_ITEM_CAP = 50


def check_batch_items(items: list, cap: int = BATCH_ITEM_CAP) -> None:
    """Whole-call preconditions — the only errors a batch tool ever raises."""
    if not items:
        raise ToolError("items must not be empty")
    if len(items) > cap:
        raise ToolError(f"items is capped at {cap} per call (got {len(items)})")


async def apply_batch_item(
    item: Any,
    allowed_keys: frozenset[str],
    apply: Callable[..., Awaitable[dict]],
) -> dict:
    """Run one batch item through ``apply``, isolating failures per item.

    ``apply`` receives the item's keys as keyword arguments. A result that
    already carries a "status" is passed through as-is (tool-specific statuses
    like "refused"/"stale_id"); otherwise it's wrapped as status="ok".
    The catch is deliberately Exception-wide (never BaseException, so
    cancellation still propagates): item values arrive untyped inside a dict —
    the wire layer only validates top-level params — so a wrongly-typed value
    can surface as AttributeError/sqlite errors deep in an impl, and any such
    escape mid-loop would abandon a half-applied batch. One bad item must cost
    one item, never the batch.
    """
    try:
        if not isinstance(item, dict):
            raise ToolError("each item must be an object")
        unknown = set(item) - allowed_keys
        if unknown:
            raise ToolError(
                f"unknown key(s): {sorted(unknown)}. Valid: {sorted(allowed_keys)}"
            )
        result = await apply(**item)
    except Exception as exc:
        payload = item if isinstance(item, dict) else {"item": item}
        # ToolError messages are already user-facing; anything else names its
        # class so an unexpected failure is diagnosable from the result alone.
        message = str(exc) if isinstance(exc, ToolError) else f"{type(exc).__name__}: {exc}"
        return {"status": "error", "error": message, "item": payload}
    if "status" in result:
        return result
    return {"status": "ok", **result}


def count_status(results: list[dict], status: str) -> int:
    return sum(1 for r in results if r["status"] == status)
