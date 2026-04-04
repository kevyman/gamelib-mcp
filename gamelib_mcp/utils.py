"""Shared utilities for gamelib_mcp tools."""

import json


def _parse_json(val: str | None) -> list:
    if not val:
        return []
    try:
        return json.loads(val)
    except ValueError:
        return []
