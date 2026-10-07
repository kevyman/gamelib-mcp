"""Log which protocol era and client capabilities each MCP client negotiates.

ADR 0005's remaining open question is what each registered client actually
negotiates (2025-11-25 handshake vs 2026-07-28 ``server/discover``) and what it
declares (elicitation modes, extensions such as tasks). This middleware is the
evidence for the MRTR / elicitation / tasks adoption decisions: one INFO line
per distinct (client, version, protocol, capabilities) tuple per process, DEBUG
on every request. Observation only — it never alters or fails a request.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CLIENT_CAPABILITIES_META_KEY, CLIENT_INFO_META_KEY

logger = logging.getLogger(__name__)

UNKNOWN = "?"
# Distinct tuples remembered for the first-seen INFO line. Once full, new tuples
# still log at DEBUG but no longer at INFO, so a misbehaving client that varies
# its version string per request cannot grow this set or flood the log.
MAX_SEEN_TUPLES = 64

ClientEra = tuple[str, str, str, str]


def _safe(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) and value else UNKNOWN


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` off a raw wire dict or an SDK model alike."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_wire_dict(capabilities: Any) -> Mapping[str, Any] | None:
    """Client capabilities in wire (camelCase) form, from a dict or an SDK model."""
    if isinstance(capabilities, Mapping):
        return capabilities
    dump = getattr(capabilities, "model_dump", None)
    if dump is None:
        return None
    dumped = dump(mode="json", by_alias=True, exclude_none=True)
    return dumped if isinstance(dumped, Mapping) else None


def summarize_capabilities(capabilities: Any) -> str:
    """Sorted, comma-joined top-level capability keys.

    ``elicitation`` expands to ``elicitation.<mode>`` and ``extensions`` to
    ``ext:<id>``; either stays bare when it declares no sub-keys. An empty
    declaration is ``none``; an unreadable one is ``?``.
    """
    wire = _as_wire_dict(capabilities)
    if wire is None:
        return UNKNOWN
    keys: list[str] = []
    for key, value in wire.items():
        if key == "elicitation" and isinstance(value, Mapping) and value:
            keys.extend(f"elicitation.{mode}" for mode in value)
        elif key == "extensions" and isinstance(value, Mapping) and value:
            keys.extend(f"ext:{ext_id}" for ext_id in value)
        else:
            keys.append(str(key))
    return ",".join(sorted(keys)) or "none"


def extract_client_era(context: MiddlewareContext[Any]) -> ClientEra:
    """(client_name, client_version, protocol_version, capability_summary).

    Modern era (2026-07-28): every request's ``_meta`` carries
    ``io.modelcontextprotocol/clientInfo`` and ``/clientCapabilities``.
    Legacy era (2025-11-25): the session's ``client_params`` hold
    ``client_info`` and ``capabilities`` once ``initialize`` has run; during
    ``initialize`` itself they are read off the request's own params.
    Each field degrades to ``?`` independently; this never raises.
    """
    fastmcp_context = _safe(lambda: context.fastmcp_context)
    rc = _safe(lambda: fastmcp_context.request_context) if fastmcp_context else None
    if rc is None:
        return (UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN)

    protocol = _text(_safe(lambda: rc.protocol_version))
    meta = _safe(lambda: rc.meta) or {}
    if not isinstance(meta, Mapping):
        meta = {}

    if CLIENT_INFO_META_KEY in meta or CLIENT_CAPABILITIES_META_KEY in meta:
        info = meta.get(CLIENT_INFO_META_KEY)
        capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    else:
        params = _safe(lambda: rc.session.client_params)
        if params is None and _safe(lambda: context.method) == "initialize":
            params = _safe(lambda: context.message.params)
        info = _safe(lambda: _field(params, "client_info"))
        capabilities = _safe(lambda: _field(params, "capabilities"))

    name = _text(_safe(lambda: _field(info, "name")))
    version = _text(_safe(lambda: _field(info, "version")))
    summary = _safe(lambda: summarize_capabilities(capabilities)) or UNKNOWN
    return (name, version, protocol, summary)


class ClientEraLogMiddleware(Middleware):
    """Log each distinct client-era tuple once at INFO, every request at DEBUG."""

    def __init__(self) -> None:
        self._seen: set[ClientEra] = set()

    def _observe(self, context: MiddlewareContext[Any]) -> None:
        era = extract_client_era(context)
        message = "client era: name=%s version=%s protocol=%s capabilities=%s"
        if era not in self._seen and len(self._seen) < MAX_SEEN_TUPLES:
            self._seen.add(era)
            logger.info(message, *era)
        logger.debug(message, *era)

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        # on_request also sees the legacy-era ``initialize`` request (FastMCP 4
        # wraps it in both on_initialize and on_request), so a separate
        # on_initialize hook would double-observe it.
        try:
            self._observe(context)
        except Exception:
            logger.debug("client era observation failed", exc_info=True)
        return await call_next(context)
