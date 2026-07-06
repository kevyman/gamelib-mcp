"""Export the game library for The Stacks 3D visualization.

Reads a gamelib SQLite DB and produces, in --out (default stacks/assets/):
  - library.json    one record per owned primary game (name, platform family,
                    playtime, scores, release year, atlas tile index)
  - atlas_N.jpg     4096x4096 texture-atlas sheets of cover art, 128x192 tiles

Cover sources: IGDB cover_image_id first, Steam library_600x900 capsule as
fallback, generated placeholder tile otherwise. Downloads are cached in
--cache so re-runs only fetch new games.

Usage:
    .venv/bin/python scripts/export_stacks.py --db /tmp/prod-gamelib.db
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import httpx
from rapidfuzz import fuzz
from PIL import Image, ImageDraw, ImageFont

TILE_W, TILE_H = 128, 192
ATLAS_SIZE = 4096
COLS = ATLAS_SIZE // TILE_W  # 32
ROWS = ATLAS_SIZE // TILE_H  # 21
TILES_PER_SHEET = COLS * ROWS  # 672

DOWNLOAD_CONCURRENCY = 12

FAMILY_COLORS = {
    "nintendo": "#e60012",
    "sony": "#0070d1",
    "xbox": "#107c10",
    "pc": "#5a6472",
}
# Priority when playtime can't break the tie (PC last: it dominates the
# library, so any console ownership is the more interesting color).
FAMILY_PRIORITY = ["nintendo", "sony", "xbox", "pc"]


def family_of(platform: str) -> str:
    if platform.startswith("switch"):
        return "nintendo"
    if platform.startswith("ps") or platform == "psn":
        return "sony"
    if platform.startswith("xbox"):
        return "xbox"
    return "pc"


def load_games(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.release_date, g.cover_image_id,
                   g.is_farmed, g.completion_status, g.hltb_main
            FROM games g
            WHERE g.is_primary_library_item = 1
              AND EXISTS (SELECT 1 FROM game_platforms gp
                          WHERE gp.game_id = g.id AND gp.owned = 1)
            ORDER BY g.id
            """
        ).fetchall()

        platforms: dict[int, dict[str, int]] = {}
        for r in conn.execute(
            "SELECT game_id, platform, COALESCE(playtime_minutes, 0) AS m"
            " FROM game_platforms WHERE owned = 1"
        ):
            platforms.setdefault(r["game_id"], {})[r["platform"]] = r["m"]

        # switch2 playtime lives in nintendo_play_summary daily rows, not
        # game_platforms (see CLAUDE.md playtime-history pattern).
        for r in conn.execute(
            """
            SELECT gp.game_id, SUM(nps.playtime_minutes) AS m
            FROM nintendo_play_summary nps
            JOIN game_platform_identifiers i
              ON i.identifier_type = 'nintendo_title_id'
             AND i.identifier_value = nps.application_id
            JOIN game_platforms gp ON gp.id = i.game_platform_id
            WHERE nps.period_type = 'day' AND gp.owned = 1
            GROUP BY gp.game_id
            """
        ):
            plats = platforms.get(r["game_id"])
            if plats and "switch2" in plats:
                plats["switch2"] = max(plats["switch2"], r["m"])

        appids: dict[int, str] = {}
        for r in conn.execute(
            """
            SELECT gp.game_id, i.identifier_value
            FROM game_platform_identifiers i
            JOIN game_platforms gp ON gp.id = i.game_platform_id
            WHERE i.identifier_type = 'steam_appid' AND i.is_primary = 1
            """
        ):
            appids.setdefault(r["game_id"], r["identifier_value"])

        critic: dict[int, int] = {}
        for r in conn.execute(
            """
            SELECT gp.game_id,
                   MAX(COALESCE(e.opencritic_score, e.metacritic_score)) AS s
            FROM game_platform_enrichment e
            JOIN game_platforms gp ON gp.id = e.game_platform_id
            WHERE COALESCE(e.opencritic_score, e.metacritic_score) IS NOT NULL
            GROUP BY gp.game_id
            """
        ):
            critic[r["game_id"]] = round(r["s"])

        user_rating: dict[int, float] = {}
        for r in conn.execute(
            "SELECT game_id, AVG(normalized_score) AS s FROM ratings GROUP BY game_id"
        ):
            user_rating[r["game_id"]] = round(r["s"], 1)
    finally:
        conn.close()

    games = []
    for r in rows:
        plats = platforms.get(r["id"], {})
        if not plats:
            continue
        fams = {family_of(p) for p in plats}
        best = max(
            plats.items(),
            key=lambda kv: (kv[1], -FAMILY_PRIORITY.index(family_of(kv[0]))),
        )
        family = family_of(best[0]) if best[1] > 0 else min(
            fams, key=FAMILY_PRIORITY.index
        )
        year = None
        if r["release_date"]:
            try:
                year = int(str(r["release_date"])[:4])
            except ValueError:
                year = None
        games.append(
            {
                "id": r["id"],
                "name": r["name"],
                "family": family,
                "platforms": plats,
                "minutes": sum(plats.values()),
                "year": year,
                "critic": critic.get(r["id"]),
                "user": user_rating.get(r["id"]),
                "hltb": r["hltb_main"],
                "farmed": bool(r["is_farmed"]),
                "status": r["completion_status"],
                "_cover_id": r["cover_image_id"],
                "_appid": appids.get(r["id"]),
            }
        )
    return games


def cover_urls(g: dict) -> tuple[str | None, str | None]:
    """(atlas-resolution url, full-resolution url for the popout card)."""
    if g["_cover_id"]:
        base = "https://images.igdb.com/igdb/image/upload"
        return (
            f"{base}/t_cover_big/{g['_cover_id']}.jpg",
            f"{base}/t_cover_big_2x/{g['_cover_id']}.jpg",
        )
    if g["_appid"]:
        url = (
            "https://cdn.cloudflare.steamstatic.com/steam/apps/"
            f"{g['_appid']}/library_600x900.jpg"
        )
        return url, url
    return None, None


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


async def steam_search_cover_url(
    client: httpx.AsyncClient, name: str, cache_dir: Path
) -> str | None:
    """Last-resort cover lookup: Steam public store search by name.

    Only accepted on a near-exact normalized name match, so PSN-only games
    with Steam ports (Spider-Man, God of War) resolve without pulling in
    wrong-game art for demos/obscure titles.
    """
    key = cache_dir / ("search_" + hashlib.sha1(name.encode()).hexdigest() + ".json")
    if key.exists():
        cached = json.loads(key.read_text())
        return cached.get("url")
    url = None
    try:
        resp = await client.get(
            "https://store.steampowered.com/api/storesearch",
            params={"term": name, "cc": "us", "l": "english"},
        )
        if resp.status_code == 200:
            na = _norm_name(name)
            num = re.search(r"(\d+)$", na)
            for item in resp.json().get("items", [])[:3]:
                ni = _norm_name(item["name"])
                ni_num = re.search(r"(\d+)$", ni)
                # exact, a remaster of the same title, or fuzzy with matching
                # trailing number (else "Spider-Man" grabs Spider-Man 2's art)
                if (
                    ni == na
                    or ni in (f"{na} remastered", f"{na} remaster")
                    or (
                        fuzz.ratio(na, ni) >= 92
                        and (num and num.group(1)) == (ni_num and ni_num.group(1))
                    )
                ):
                    url = (
                        "https://cdn.cloudflare.steamstatic.com/steam/apps/"
                        f"{item['id']}/library_600x900.jpg"
                    )
                    break
    except (httpx.HTTPError, ValueError, KeyError):
        return None  # transient: not cached, retried next run
    key.write_text(json.dumps({"url": url}))
    return url


async def fetch_covers(games: list[dict], cache_dir: Path) -> dict[int, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, Path] = {}
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    done = 0

    async def download(client: httpx.AsyncClient, url: str) -> Path | None:
        path = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".img")
        miss = cache_dir / (path.name + ".miss")
        if path.exists():
            return path
        if miss.exists():
            return None
        async with sem:
            try:
                resp = await client.get(url)
            except httpx.HTTPError:
                return None  # transient: no .miss marker, retried next run
        if resp.status_code == 200 and resp.content:
            path.write_bytes(resp.content)
            return path
        if resp.status_code in (404, 410):
            miss.touch()  # only permanent misses; 429/5xx retry next run
        return None

    async def fetch(client: httpx.AsyncClient, g: dict) -> None:
        nonlocal done
        url, _ = cover_urls(g)
        path = await download(client, url) if url else None
        if path is None:
            # no provider cover — try a strict-match Steam store search
            s_url = await steam_search_cover_url(client, g["name"], cache_dir)
            if s_url:
                path = await download(client, s_url)
                if path is not None:
                    g["_fallback_cover"] = s_url
        if path is not None:
            results[g["id"]] = path
        done += 1
        if done % 250 == 0:
            print(f"  covers: {done}/{len(games)}")

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        await asyncio.gather(*(fetch(client, g) for g in games))
    return results


def _placeholder_tile(g: dict) -> Image.Image:
    tile = Image.new("RGB", (TILE_W, TILE_H), "#23262d")
    draw = ImageDraw.Draw(tile)
    draw.rectangle([0, 0, TILE_W, 8], fill=FAMILY_COLORS[g["family"]])
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    words, lines, cur = g["name"].split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if draw.textlength(cand, font=font) <= TILE_W - 14:
            cur = cand
        else:
            lines.append(cur)
            cur = w
        if len(lines) == 7:
            break
    lines.append(cur)
    y = TILE_H // 2 - len(lines) * 8
    for line in lines:
        draw.text((TILE_W // 2, y), line, font=font, fill="#c8cdd6", anchor="ma")
        y += 17
    return tile


def _cover_tile(path: Path) -> Image.Image | None:
    try:
        img = Image.open(path).convert("RGB")
    except OSError:
        return None
    # Cover-fit crop to the 2:3 tile.
    scale = max(TILE_W / img.width, TILE_H / img.height)
    img = img.resize(
        (round(img.width * scale), round(img.height * scale)), Image.LANCZOS
    )
    x = (img.width - TILE_W) // 2
    y = (img.height - TILE_H) // 2
    return img.crop((x, y, x + TILE_W, y + TILE_H))


def build_atlases(
    games: list[dict], covers: dict[int, Path], out_dir: Path
) -> int:
    sheets: list[Image.Image] = []
    placeholders = 0
    for idx, g in enumerate(games):
        sheet_no, i = divmod(idx, TILES_PER_SHEET)
        if sheet_no == len(sheets):
            sheets.append(Image.new("RGB", (ATLAS_SIZE, ATLAS_SIZE), "#15171c"))
        tile = None
        if g["id"] in covers:
            tile = _cover_tile(covers[g["id"]])
        if tile is None:
            tile = _placeholder_tile(g)
            g["ph"] = True  # frontend sinks placeholder tiles to pile bottoms
            placeholders += 1
        col, row = i % COLS, i // COLS
        sheets[sheet_no].paste(tile, (col * TILE_W, row * TILE_H))
        g["tile"] = idx
    for stale in out_dir.glob("atlas_*.jpg"):
        stale.unlink()  # fewer sheets than last run must not leave old ones
    for n, sheet in enumerate(sheets):
        sheet.save(out_dir / f"atlas_{n}.jpg", quality=82, optimize=True)
    print(f"  atlases: {len(sheets)} sheets, {placeholders} placeholder tiles")
    return len(sheets)


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=repo / "data" / "gamelib.db")
    ap.add_argument("--out", type=Path, default=repo / "stacks" / "assets")
    ap.add_argument("--cache", type=Path, default=repo / "stacks" / ".cover_cache")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")
    args.out.mkdir(parents=True, exist_ok=True)

    games = load_games(args.db)
    print(f"  games: {len(games)} owned primary library items")

    covers = asyncio.run(fetch_covers(games, args.cache))
    print(f"  covers resolved: {len(covers)}")

    sheet_count = build_atlases(games, covers, args.out)

    for g in games:
        _, full = cover_urls(g)
        g["cover"] = g.pop("_fallback_cover", None) or full
        del g["_cover_id"], g["_appid"]

    payload = {
        "meta": {
            "tile": [TILE_W, TILE_H],
            "atlasSize": ATLAS_SIZE,
            "cols": COLS,
            "rows": ROWS,
            "tilesPerSheet": TILES_PER_SHEET,
            "sheets": sheet_count,
            "familyColors": FAMILY_COLORS,
        },
        "games": games,
    }
    out_json = args.out / "library.json"
    out_json.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"  wrote {out_json} ({out_json.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
