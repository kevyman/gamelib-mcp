"""Drop the duplicate text block MCP tool results carry by default.

The MCP spec says a tool returning structured content "SHOULD also return the
serialized JSON in a TextContent block" for backwards compatibility, and
FastMCP does exactly that: every result ships the same payload twice, once in
``content[0].text`` and once in ``structuredContent``. Measured across 21
representative calls against a real library, that duplication was 156,683 of
325,668 response chars — 48% of everything this server sends back, on every
call.

Both clients registered against this deployment were tested empirically on
2026-07-27 with a differential probe (each channel carrying a distinct
``_channel`` marker, so whichever a client surfaced identified what it
consumed). claude.ai and chatgpt.com both reported ``STRUCTURED_CONTENT``.
Neither reads the text block, so it is pure overhead here — see ADR 0004.

This is a per-deployment optimization, NOT a general one: the spec's SHOULD
exists for clients that need the text block, and a client that reads it would
see empty results. If one is ever added, set MCP_DUPLICATE_TEXT_CONTENT=1 to
restore spec-default behavior without a code change, and re-run the probe in
ADR 0004 before turning it back off.
"""

import os

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

ENV_VAR = "MCP_DUPLICATE_TEXT_CONTENT"


def duplicate_text_content_enabled() -> bool:
    """True when the spec-default duplicate text block should be kept."""
    return os.getenv(ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


class StructuredOnlyMiddleware(Middleware):
    """Strip text blocks from tool results that already carry structured content.

    Deliberately narrow:
    - A result with no ``structured_content`` is untouched. Dropping its text
      would leave the client nothing at all. (Note FastMCP populates structured
      content even for a scalar return, wrapping it as ``{"result": ...}``, so
      this guard fires rarely — but it is what makes the middleware safe for a
      tool that returns raw content on purpose.)
    - Only TEXT blocks are dropped. Images, audio, resource links, and embedded
      resources are not duplicates of the structured payload, so they always
      survive — a future tool returning one keeps working without revisiting
      this.
    - Errors are untouched by construction: a failing tool raises through
      ``call_next`` instead of returning a ``ToolResult``, so an error message
      can never be stripped from a client that needs it to self-correct.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if not isinstance(result, ToolResult) or result.structured_content is None:
            return result

        kept = [block for block in (result.content or []) if getattr(block, "type", None) != "text"]
        if len(kept) == len(result.content or []):
            return result
        return ToolResult(
            content=kept,
            structured_content=result.structured_content,
            meta=result.meta,
            is_error=result.is_error,
        )
