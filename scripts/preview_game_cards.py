"""Render the game-cards MCP App with real library data for visual review.

Runs the actual tool code (discover_games / get_game_detail) against the local
database, injects the result as ``window.__PREVIEW_DATA__`` into the widget
HTML from gamelib_mcp.apps, and writes a standalone page that renders without
any MCP host — open it in a browser to judge layout and style.

Usage:
    python scripts/preview_game_cards.py grid  [-o out.html] [--limit 12] [--sort match]
    python scripts/preview_game_cards.py detail --name "Hades" [-o out.html]
    python scripts/preview_game_cards.py detail --name "Hades" --media        # live
    python scripts/preview_game_cards.py detail --name "Hades" --sample-media  # offline
    python scripts/preview_game_cards.py detail --name "Hades" --sample-media --big-studio
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelib_mcp.apps import GAME_CARDS_HTML

_STEAM_SHOTS = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1145350"

# Contract-exact media/similar blocks (the get_game_detail(media=True) keys,
# which are tools/game_media.py's output verbatim). --media asks the real tool
# for the real thing; these exist so the hero, the screenshot strip and the
# similar row are reachable with no network and no IGDB credentials.
SAMPLE_MEDIA: dict[str, Any] = {
    "source": "steam",
    "trailer": {
        "kind": "mp4",
        "url": "https://cdn.cloudflare.steamstatic.com/steam/apps/257061003/movie480.mp4",
        "hq_url": "https://cdn.cloudflare.steamstatic.com/steam/apps/257061003/movie_max.mp4",
        "poster": f"{_STEAM_SHOTS}/movie_thumbnail.jpg",
        "name": "Launch Trailer",
    },
    "screenshots": [
        {"thumb": f"{_STEAM_SHOTS}/ss_{i}_600x338.jpg", "full": f"{_STEAM_SHOTS}/ss_{i}.jpg"}
        for i in range(1, 7)
    ],
    "screenshot_count": 14,
    "screenshots_truncated": True,
    "short_description": "Battle beyond the Underworld using dark sorcery.",
}

SAMPLE_SIMILAR: dict[str, Any] = {
    "items": [
        {
            "igdb_id": 113112,
            "name": "Hades",
            "release_year": 2020,
            "cover_url": "https://images.igdb.com/igdb/image/upload/t_cover_big/co39vc.jpg",
            "owned": True,
            "unplayed": False,
            "my_rating": 9,
            "playtime_hours": 132.4,
        },
        {
            "igdb_id": 25311,
            "name": "Dead Cells",
            "release_year": 2018,
            "cover_url": None,
            "owned": True,
            "unplayed": False,
            "my_rating": 6,
            "playtime_hours": 2.8,
        },
        {
            "igdb_id": 26192,
            "name": "Wizard of Legend",
            "release_year": 2018,
            "cover_url": None,
            "owned": True,
            "unplayed": True,
            "my_rating": None,
            "playtime_hours": 0.0,
        },
        {
            "igdb_id": 119171,
            "name": "Returnal",
            "release_year": 2021,
            "cover_url": None,
            "owned": False,
            "unplayed": False,
            "my_rating": None,
            "playtime_hours": None,
        },
    ],
    "count": 8,
    "truncated": True,
}

# The "From the studio" strip, contract-exact (tools/game_media.py's annotated
# shape). previous_games are annotated against the library, so the badge rule
# — his rating beats the critic score, an owned-but-unrated game gets the
# ownership sticker — is visible in one glance.
SAMPLE_PEDIGREE: dict[str, Any] = {
    "developer": {
        "name": "Supergiant Games",
        "igdb_company_id": 1152,
        "founded_year": 2009,
        "country": 840,
    },
    "developer_names": ["Supergiant Games"],
    "publisher_name": "Supergiant Games",
    "previous_games": [
        {
            "igdb_id": 113112,
            "name": "Hades",
            "release_year": 2020,
            "critic_score": 93,
            "cover_url": "https://images.igdb.com/igdb/image/upload/t_cover_big/co39vc.jpg",
            "owned": True,
            "my_rating": 9,
            "playtime_hours": 132.4,
        },
        {
            "igdb_id": 19560,
            "name": "Pyre",
            "release_year": 2017,
            "critic_score": 84,
            "cover_url": None,
            "owned": True,
            "my_rating": None,
            "playtime_hours": 0.0,
        },
        {
            "igdb_id": 3277,
            "name": "Transistor",
            "release_year": 2014,
            "critic_score": 83,
            "cover_url": None,
            "owned": True,
            "my_rating": 8,
            "playtime_hours": 11.2,
        },
        {
            "igdb_id": 1465,
            "name": "Bastion",
            "release_year": 2011,
            "critic_score": 86,
            "cover_url": None,
            "owned": False,
            "my_rating": None,
            "playtime_hours": None,
        },
    ],
    "previous_count": 4,
    "previous_truncated": False,
    "catalog_size": 5,
    "catalog_truncated": False,
    "big_catalog": False,
    "library_track_record": {"owned_count": 3, "played_count": 2, "avg_my_rating": 8.5},
    "hypes": 41,
}

# The big-studio damper: over BIG_CATALOG_THRESHOLD developed games, the strip
# is a header line and nothing else — six arbitrary posters out of a catalogue
# this size say nothing about the game in front of you.
SAMPLE_PEDIGREE_BIG: dict[str, Any] = {
    "developer": {
        "name": "Ubisoft Montreal",
        "igdb_company_id": 104,
        "founded_year": 1997,
        "country": 124,
    },
    "developer_names": ["Ubisoft Montreal", "Ubisoft Toronto"],
    "publisher_name": "Ubisoft Entertainment",
    "previous_games": [],
    "previous_count": 0,
    "previous_truncated": False,
    "catalog_size": 30,
    "catalog_truncated": True,
    "big_catalog": True,
    "library_track_record": None,
    "hypes": 12,
}


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

    data = await get_game_detail(name=args.name, media=args.media)
    if args.sample_media:
        data["media"] = SAMPLE_MEDIA
        data["similar"] = SAMPLE_SIMILAR
        data["pedigree"] = SAMPLE_PEDIGREE_BIG if args.big_studio else SAMPLE_PEDIGREE
    return data


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
    parser.add_argument(
        "--media", action="store_true",
        help="detail mode: fetch the real trailer/screenshots/similar games",
    )
    parser.add_argument(
        "--sample-media", action="store_true",
        help="detail mode: splice in the built-in sample media/similar/pedigree "
             "blocks (no network — the offline way to see those sections)",
    )
    parser.add_argument(
        "--big-studio", action="store_true",
        help="with --sample-media: use the big-catalog pedigree sample, whose "
             "damper renders the studio header line and no poster row",
    )
    parser.add_argument("-o", "--out", type=Path, help="output path (default: stdout)")
    args = parser.parse_args()
    if args.mode == "detail" and not args.name:
        parser.error("detail mode requires --name")
    if args.mode == "grid" and (args.media or args.sample_media):
        parser.error("--media/--sample-media apply to detail mode only")
    if args.big_studio and not args.sample_media:
        parser.error("--big-studio selects between the sample pedigrees; pass --sample-media")

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
