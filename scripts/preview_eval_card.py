"""Render the evaluation-card MCP App with sample payloads for visual review.

Injects one of the sample ``record_assessment`` responses below as
``window.__PREVIEW_DATA__`` into the widget HTML from gamelib_mcp.apps_eval
and writes a standalone page that renders without any MCP host — open it in a
browser to judge layout and style.

Unlike scripts/preview_game_cards.py this runs no tool code: the evaluation
package is assembled from live media fetches and model-authored presentation
fields, so hand-written samples (which follow the package contract exactly)
are what makes every branch — mp4 hero, YouTube hero, media-less card, the
compact note cards — reachable offline.

Usage:
    python scripts/preview_eval_card.py            # sample 0
    python scripts/preview_eval_card.py 1 -o eval.html
    python scripts/preview_eval_card.py --list
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelib_mcp.apps_eval import EVAL_CARD_HTML

_STEAM_SHOTS = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1145350"

# 0 — the common case: Steam candidate, mp4 trailer, six screenshots, every
# section populated, buy_now.
SAMPLE_FULL_STEAM: dict[str, Any] = {
    "recorded": True,
    "game_id": 4821,
    "name": "Hades II",
    "verdict": "buy_now",
    "package": {
        "game": {
            "game_id": 4821,
            "name": "Hades II",
            "release_year": 2025,
            "cover_url": "https://images.igdb.com/igdb/image/upload/t_cover_big/co7fzt.jpg",
        },
        "verdict": "buy_now",
        "summary": "The rare sequel that is denser than the original without losing its pace.",
        "presentation": {
            "elevator_pitch": (
                "Supergiant took the tightest action roguelike ever made, doubled the "
                "systems, and kept every run readable — this is the one you clear a "
                "weekend for."
            ),
            "for_you_if": [
                "You put 132h into Hades and rated it 9/10",
                "Run-based games are the only thing you finish on a weeknight",
                "You rate hand-drawn art direction above technical fidelity",
            ],
            "not_for_you_if": [
                "You bounced off Dead Cells twice at under 3h",
                "You want a story you can finish in one sitting",
            ],
            "why_care": [
                {
                    "kind": "studio",
                    "text": "Supergiant's first-ever sequel, after four "
                            "stand-alone games in fourteen years",
                },
                {
                    "kind": "people",
                    "text": "Darren Korb and Ashley Barrett are back on the "
                            "soundtrack, credited on every Supergiant game",
                },
                {
                    "kind": "anticipation",
                    "text": "Two years in early access before the 1.0 this "
                            "verdict is about",
                },
            ],
        },
        "comparisons": [
            {
                "name": "Hades",
                "relation": "ancestor",
                "note": "Same core loop, half the systems.",
                "game_id": 512,
                "owned": True,
                "my_rating": 9,
                "playtime_hours": 132.4,
            },
            {
                "name": "Rogue Legacy 2",
                "relation": "similar",
                "note": "Lighter, more forgiving meta-progression.",
                "game_id": 990,
                "owned": True,
                "my_rating": 7,
                "playtime_hours": 14.0,
            },
        ],
        "craft": {
            "adjusted": 0.93,
            "positive_pct": 96,
            "review_count": 114203,
            "trajectory": "improving",
            "opencritic_score": 91,
        },
        "fit_call": "strong fit",
        "flags": ["Early Access history — 1.0 shipped 2025-09"],
        "anchors": [
            {
                "game_id": 512,
                "name": "Hades",
                "rating": 9,
                "playtime_hours": 132.4,
                "completion_status": "completed",
                "cover_url": "https://images.igdb.com/igdb/image/upload/t_cover_big/co39vc.jpg",
            },
            {
                "game_id": 771,
                "name": "Slay the Spire",
                "rating": 10,
                "playtime_hours": 244.0,
                "completion_status": "evergreen",
                "cover_url": None,
            },
            {
                "game_id": 812,
                "name": "Dead Cells",
                "rating": 6,
                "playtime_hours": 2.8,
                "completion_status": "abandoned",
                "cover_url": None,
            },
        ],
        "ownership": {
            "owned": False,
            "wishlisted": True,
            "platforms": [],
            "completion_status": None,
            "my_rating": None,
            "playtime_hours": None,
            "price_paid": None,
            "price_currency": None,
            "purchase_source": None,
            "bundle_name": None,
        },
        "time": {
            "hltb_main_hours": 26.0,
            "hltb_extra_hours": 41.5,
            "recent_weekly_minutes": 245,
        },
        "price": {"seen": 29.99, "currency": "EUR", "platform": "steam", "target": 19.99},
        "media": {
            "source": "steam",
            "trailer": {
                "kind": "mp4",
                "url": "https://cdn.cloudflare.steamstatic.com/steam/apps/257061003/movie480.mp4",
                "hq_url": (
                    "https://cdn.cloudflare.steamstatic.com/steam/apps/257061003/movie_max.mp4"
                ),
                "poster": (
                    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
                    "1145350/movie_thumbnail.jpg"
                ),
                "name": "Hades II — 1.0 Launch Trailer",
            },
            "screenshots": [
                {
                    "thumb": f"{_STEAM_SHOTS}/ss_{i}_600x338.jpg",
                    "full": f"{_STEAM_SHOTS}/ss_{i}.jpg",
                }
                for i in range(1, 7)
            ],
            "screenshot_count": 14,
            "screenshots_truncated": True,
            "short_description": (
                "Battle beyond the Underworld using dark sorcery to take on the Titan of Time."
            ),
        },
        "similar": {
            "items": [
                {
                    "igdb_id": 113112,
                    "name": "Hades",
                    "release_year": 2020,
                    "cover_url": (
                        "https://images.igdb.com/igdb/image/upload/t_cover_big/co39vc.jpg"
                    ),
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
                    "igdb_id": 9630,
                    "name": "Slay the Spire",
                    "release_year": 2019,
                    "cover_url": None,
                    "owned": True,
                    "unplayed": False,
                    "my_rating": 10,
                    "playtime_hours": 244.0,
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
            ],
            "count": 8,
            "truncated": True,
        },
        # The "From the studio" strip, contract-exact (tools/game_media.py's
        # annotated shape). The badge rule is visible in one row: his rating
        # beats the critic score, and an owned-but-unrated game gets the
        # ownership sticker instead.
        "pedigree": {
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
                    "cover_url": (
                        "https://images.igdb.com/igdb/image/upload/t_cover_big/co39vc.jpg"
                    ),
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
            "library_track_record": {
                "owned_count": 3,
                "played_count": 2,
                "avg_my_rating": 8.5,
            },
            "hypes": 41,
        },
        "past": {
            "items": [
                {
                    "assessed_at": "2026-05-12T18:04:11Z",
                    "verdict": "wishlist_for_sale",
                    "summary": "Great, but full price at launch week.",
                    "price_seen": 39.99,
                    "price_currency": "EUR",
                },
                {
                    "assessed_at": "2025-11-02T09:41:00Z",
                    "verdict": "try_demo",
                    "summary": "Early access — wait for 1.0.",
                    "price_seen": 29.99,
                    "price_currency": "EUR",
                },
            ],
            "count": 4,
            "truncated": True,
        },
        "errors": [],
    },
}

# 1 — non-Steam candidate: IGDB media (YouTube trailer), switch2, a lineage
# with ancestors, descendants and a better-version call-out.
SAMPLE_IGDB_YOUTUBE: dict[str, Any] = {
    "recorded": True,
    "game_id": 6610,
    "name": "Metroid Prime 4: Beyond",
    "verdict": "wishlist_for_sale",
    "package": {
        "game": {
            "game_id": 6610,
            "name": "Metroid Prime 4: Beyond",
            "release_year": 2026,
            "cover_url": "https://images.igdb.com/igdb/image/upload/t_cover_big/co8v2p.jpg",
        },
        "verdict": "wishlist_for_sale",
        "summary": "Everything you liked about Prime, at a price that never drops on Nintendo.",
        "presentation": {
            "elevator_pitch": (
                "Eighteen years later the scan-and-explore loop is intact — but Nintendo "
                "prices never move, so this is a patience purchase, not a launch one."
            ),
            "for_you_if": [
                "Metroid Prime Remastered is the only Switch game you finished twice",
                "You want a single-player campaign with no live service attached",
            ],
            "not_for_you_if": [
                "You abandoned both first-person games you started last year",
            ],
            "why_care": [
                {
                    "kind": "anticipation",
                    "text": "Announced in 2017, rebooted in 2019, and finally "
                            "dated — eighteen years after Prime 3",
                },
                {
                    "kind": "moment",
                    "text": "Retro Studios' first release since the 2023 "
                            "Remastered port",
                },
            ],
        },
        "comparisons": [
            {
                "name": "Metroid Prime",
                "relation": "ancestor",
                "note": "The 2002 original this is still iterating on.",
                "game_id": 233,
                "owned": True,
                "my_rating": 9,
                "playtime_hours": 21.5,
            },
            {
                "name": "Metroid Prime Remastered",
                "relation": "better_version",
                "note": "You already own the definitive version of the original.",
                "game_id": 5120,
                "owned": True,
                "my_rating": 9,
                "playtime_hours": 12.0,
            },
            {
                "name": "Metroid Dread",
                "relation": "descendant",
                "note": "The 2D line that grew out of Prime's revival.",
                "game_id": 4402,
                "owned": False,
                "my_rating": None,
                "playtime_hours": None,
            },
            {
                "name": "Control",
                "relation": "similar",
                "note": "Same scan-the-world curiosity, different genre.",
                "game_id": 3311,
                "owned": True,
                "my_rating": 8,
                "playtime_hours": 19.0,
            },
        ],
        "craft": {
            "adjusted": 0.81,
            "positive_pct": 84,
            "review_count": 1840,
            "trajectory": "stable",
            "opencritic_score": 86,
        },
        "fit_call": "probable fit",
        "flags": ["Switch 2 exclusive — no discount history"],
        "anchors": [
            {
                "game_id": 5120,
                "name": "Metroid Prime Remastered",
                "rating": 9,
                "playtime_hours": 12.0,
                "completion_status": "completed",
                "cover_url": None,
            },
            {
                "game_id": 3311,
                "name": "Control",
                "rating": 8,
                "playtime_hours": 19.0,
                "completion_status": None,
                "cover_url": None,
            },
        ],
        "ownership": {
            "owned": False,
            "wishlisted": True,
            "platforms": [],
            "completion_status": None,
            "my_rating": None,
            "playtime_hours": None,
            "price_paid": None,
            "price_currency": None,
            "purchase_source": None,
            "bundle_name": None,
        },
        "time": {
            "hltb_main_hours": 18.0,
            "hltb_extra_hours": None,
            "recent_weekly_minutes": None,
        },
        "price": {"seen": 69.99, "currency": "EUR", "platform": "switch2", "target": 49.99},
        "media": {
            "source": "igdb",
            "trailer": {
                "kind": "youtube",
                "video_id": "dQw4w9WgXcQ",
                "poster": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                "name": "Metroid Prime 4: Beyond — Overview Trailer",
            },
            "screenshots": [
                {
                    "thumb": (
                        "https://images.igdb.com/igdb/image/upload/t_screenshot_med/sc9a1x.jpg"
                    ),
                    "full": (
                        "https://images.igdb.com/igdb/image/upload/t_screenshot_huge/sc9a1x.jpg"
                    ),
                },
                {
                    "thumb": (
                        "https://images.igdb.com/igdb/image/upload/t_screenshot_med/sc9a1y.jpg"
                    ),
                    "full": (
                        "https://images.igdb.com/igdb/image/upload/t_screenshot_huge/sc9a1y.jpg"
                    ),
                },
            ],
            "screenshot_count": 2,
            "screenshots_truncated": False,
            "short_description": None,
        },
        "similar": None,
        # The big-studio damper: over BIG_CATALOG_THRESHOLD developed games the
        # strip is a header line and nothing else, since six arbitrary posters
        # out of a catalogue this size say nothing about this game.
        "pedigree": {
            "developer": {
                "name": "Retro Studios",
                "igdb_company_id": 203,
                "founded_year": 1998,
                "country": 840,
            },
            "developer_names": ["Retro Studios", "Nintendo EPD"],
            "publisher_name": "Nintendo",
            "previous_games": [],
            "previous_count": 0,
            "previous_truncated": False,
            "catalog_size": 30,
            "catalog_truncated": True,
            "big_catalog": True,
            "library_track_record": None,
            "hypes": 208,
        },
        "past": None,
        "errors": ["hltb: completionist time unavailable"],
    },
}

# 2 — the degraded case: no media, no similar games, no presentation fields,
# and a partial-data error. Every optional block must simply be absent.
SAMPLE_MINIMAL: dict[str, Any] = {
    "recorded": True,
    "game_id": 9042,
    "name": "Grim Harvest",
    "verdict": "skip",
    "package": {
        "game": {
            "game_id": 9042,
            "name": "Grim Harvest",
            "release_year": None,
            "cover_url": None,
        },
        "verdict": "skip",
        "summary": "Thin survival crafter with no hook you haven't already abandoned twice.",
        "presentation": None,
        "comparisons": [],
        "craft": None,
        "fit_call": "probable miss",
        "flags": ["No review sample — 41 reviews", "Solo dev, last patch 2024"],
        "anchors": [
            {
                "game_id": 4110,
                "name": "Valheim",
                "rating": 5,
                "playtime_hours": 4.2,
                "completion_status": "abandoned",
                "cover_url": None,
            }
        ],
        "ownership": {
            "owned": False,
            "wishlisted": False,
            "platforms": [],
            "completion_status": None,
            "my_rating": None,
            "playtime_hours": None,
            "price_paid": None,
            "price_currency": None,
            "purchase_source": None,
            "bundle_name": None,
        },
        "time": None,
        "price": {"seen": 14.99, "currency": "EUR", "platform": "steam", "target": None},
        "media": None,
        "similar": None,
        "pedigree": None,
        "past": None,
        "errors": ["igdb: no match for 'Grim Harvest'", "steam: no appid resolved"],
    },
}

# 3 — a recorded verdict with no package (the plain bookkeeping response).
SAMPLE_COMPACT: dict[str, Any] = {
    "recorded": True,
    "game_id": 771,
    "name": "Slay the Spire II",
    "verdict": "play_what_you_own",
    "created": False,
}

# 4 — the void mode: one misfiled verdict hard-deleted.
SAMPLE_VOID: dict[str, Any] = {
    "voided": True,
    "assessment_id": 318,
    "game_id": 512,
    "name": "Alan Wake",
    "suggested_action": None,
}

SAMPLES: list[tuple[str, dict[str, Any]]] = [
    ("full Steam package (mp4 trailer, 6 screenshots, buy_now)", SAMPLE_FULL_STEAM),
    ("IGDB package (YouTube trailer, switch2, lineage, big-studio pedigree)", SAMPLE_IGDB_YOUTUBE),
    ("minimal package (no media/similar/pedigree/presentation, skip)", SAMPLE_MINIMAL),
    ("compact recorded verdict (no package)", SAMPLE_COMPACT),
    ("voided assessment", SAMPLE_VOID),
]


def build_html(data: dict[str, Any]) -> str:
    """Inject a payload as the widget's preview global."""
    preview_globals = "window.__PREVIEW_DATA__ = " + json.dumps(data) + ";"
    return EVAL_CARD_HTML.replace(
        "<script>",
        "<script>" + preview_globals + "</script>\n<script>",
        1,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample", nargs="?", type=int, default=0, choices=range(len(SAMPLES)),
        help="sample payload index (default: 0)",
    )
    parser.add_argument("--list", action="store_true", help="list the sample payloads and exit")
    parser.add_argument("-o", "--out", type=Path, help="output path (default: stdout)")
    args = parser.parse_args()

    if args.list:
        for i, (label, _) in enumerate(SAMPLES):
            print(f"{i}  {label}")
        return

    label, data = SAMPLES[args.sample]
    html = build_html(data)
    if args.out:
        args.out.write_text(html)
        print(f"wrote {args.out} ({label})", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()
