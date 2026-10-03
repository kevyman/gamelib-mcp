"""Simulated MCP Apps ``hostContext`` for the offline widget previews.

The widgets apply ``window.__PREVIEW_HOST_CONTEXT__`` exactly as they would the
``hostContext`` of a real ``ui/initialize`` answer (theme, displayMode,
availableDisplayModes, deviceCapabilities, safeAreaInsets, styles.variables),
so a preview page can show Claude's own tokens, dark mode and touch mode with
no host. The values below are Claude's published style variables
(https://claude.com/docs/connectors/building/mcp-apps/design-guidelines,
"Style variables"); the host font itself is not available offline, so the
font stack falls through to the local sans-serif.

Used by scripts/preview_game_cards.py and scripts/preview_eval_card.py.
"""

import json
from typing import Any

_SHARED: dict[str, str] = {
    "--font-sans": "Anthropic Sans, sans-serif",
    "--font-weight-normal": "400",
    "--font-weight-semibold": "600",
    "--font-text-xs-size": "12px",
    "--font-text-sm-size": "14px",
    "--font-heading-md-size": "16px",
    "--font-heading-lg-size": "20px",
    "--font-text-xs-line-height": "1.4",
    "--font-text-sm-line-height": "1.4",
    "--font-heading-md-line-height": "1.4",
    "--font-heading-lg-line-height": "1.25",
    "--border-radius-xs": "4px",
    "--border-radius-sm": "6px",
    "--border-radius-md": "8px",
    "--border-radius-lg": "10px",
    "--border-radius-xl": "12px",
    "--border-radius-full": "9999px",
    "--border-width-regular": "0.5px",
    "--shadow-sm": "0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1)",
    "--shadow-md": "0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1)",
}

CLAUDE_TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "--color-background-primary": "#FFFFFF",
        "--color-background-secondary": "#F5F4ED",
        "--color-background-tertiary": "#FAF9F5",
        "--color-background-inverse": "#141413",
        "--color-background-success": "#E9F1DC",
        "--color-background-warning": "#F6EEDF",
        "--color-background-danger": "#F7ECEC",
        "--color-text-primary": "#141413",
        "--color-text-secondary": "#3D3D3A",
        "--color-text-tertiary": "#73726C",
        "--color-text-inverse": "#FFFFFF",
        "--color-text-success": "#265B19",
        "--color-text-warning": "#5A4815",
        "--color-text-danger": "#7F2C28",
        "--color-border-primary": "rgba(31, 30, 29, 0.4)",
        "--color-border-tertiary": "rgba(31, 30, 29, 0.15)",
        "--color-border-success": "#437426",
        "--color-border-warning": "#805C1F",
        "--color-border-danger": "#A73D39",
        **_SHARED,
    },
    "dark": {
        "--color-background-primary": "#30302E",
        "--color-background-secondary": "#262624",
        "--color-background-tertiary": "#141413",
        "--color-background-inverse": "#FAF9F5",
        "--color-background-success": "#1B4614",
        "--color-background-warning": "#483A0F",
        "--color-background-danger": "#602A28",
        "--color-text-primary": "#FAF9F5",
        "--color-text-secondary": "#C2C0B6",
        "--color-text-tertiary": "#9C9A92",
        "--color-text-inverse": "#141413",
        "--color-text-success": "#7AB948",
        "--color-text-warning": "#D1A041",
        "--color-text-danger": "#EE8884",
        "--color-border-primary": "rgba(222, 220, 209, 0.4)",
        "--color-border-tertiary": "rgba(222, 220, 209, 0.15)",
        "--color-border-success": "#599130",
        "--color-border-warning": "#A87829",
        "--color-border-danger": "#CD5C58",
        **_SHARED,
    },
}

# The chat surface the widget sits on (preview-only: the widget page itself is
# transparent and paints nothing behind its panels).
_CHAT_BACKGROUND = {"light": "#FAF9F5", "dark": "#262624"}


def host_context(theme: str, *, touch: bool = False) -> dict[str, Any]:
    """A Claude-like hostContext for ``theme`` ("light" or "dark")."""
    return {
        "theme": theme,
        "displayMode": "inline",
        "availableDisplayModes": ["inline", "fullscreen"],
        "deviceCapabilities": {"touch": touch, "hover": not touch},
        "safeAreaInsets": {"top": 0, "right": 0, "bottom": 0, "left": 0},
        "styles": {"variables": CLAUDE_TOKENS[theme]},
    }


def inject(html: str, data: Any, context: dict[str, Any] | None, extra_js: str = "") -> str:
    """Splice the preview globals (and, with a context, the chat background)."""
    preview_globals = "window.__PREVIEW_DATA__ = " + json.dumps(data) + ";"
    if context is not None:
        preview_globals += " window.__PREVIEW_HOST_CONTEXT__ = " + json.dumps(context) + ";"
    preview_globals += extra_js
    if context is not None:
        background = _CHAT_BACKGROUND.get(context.get("theme", "light"), "#FAF9F5")
        html = html.replace(
            "</head>",
            f"<style>html {{ background: {background}; }}</style>\n</head>",
            1,
        )
    return html.replace("<script>", "<script>" + preview_globals + "</script>\n<script>", 1)
