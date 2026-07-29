"""Render the game-cards MCP App with real library data for visual review.

Runs the actual tool code (discover_games / get_game_detail) against the local
database, injects the result as ``window.__PREVIEW_DATA__`` into the widget
HTML from gamelib_mcp.apps, and writes a standalone page that renders without
any MCP host — open it in a browser to judge layout and style.

Usage:
    python scripts/preview_game_cards.py grid  [-o out.html] [--limit 12] [--sort match]
    python scripts/preview_game_cards.py detail --name "Hades" [-o out.html]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelib_mcp.apps import GAME_CARDS_HTML


async def _build_data(args: argparse.Namespace) -> dict:
    if args.mode == "grid":
        from gamelib_mcp.tools.discover import discover_games

        return await discover_games(
            vibes=args.vibes or None,
            sort_by=args.sort,
            limit=args.limit,
            response_format="detailed",
        )
    from gamelib_mcp.tools.detail import get_game_detail

    return await get_game_detail(name=args.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["grid", "detail"])
    parser.add_argument("--name", help="game name for detail mode")
    parser.add_argument("--vibes", nargs="*", help="vibe filters for grid mode")
    parser.add_argument("--sort", default="match", choices=["match", "critic", "value"])
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument(
        "--open", type=int, default=None, metavar="N",
        help="grid mode: auto-open the detail overlay for card N (0-based)",
    )
    parser.add_argument("-o", "--out", type=Path, help="output path (default: stdout)")
    args = parser.parse_args()
    if args.mode == "detail" and not args.name:
        parser.error("detail mode requires --name")

    data = asyncio.run(_build_data(args))
    preview_globals = "window.__PREVIEW_DATA__ = " + json.dumps(data) + ";"
    if args.open is not None:
        preview_globals += f" window.__PREVIEW_OPEN_INDEX__ = {args.open};"
    html = GAME_CARDS_HTML.replace(
        "<script>",
        "<script>" + preview_globals + "</script>\n<script>",
        1,
    )
    if args.out:
        args.out.write_text(html)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()
