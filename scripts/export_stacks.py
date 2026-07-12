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
import colorsys
import hashlib
import json
import math
import random
import re
import sqlite3
import sys
from pathlib import Path

import httpx
from rapidfuzz import fuzz
from PIL import Image, ImageDraw, ImageFont

from gamelib_mcp.data.tag_synonyms import canonical_tag
from gamelib_mcp.data.tags import split_features

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


# ---------------------------------------------------------------------------
# Galaxy embedding (issue #78): cluster FIRST, embed SECOND. A single global
# force layout collapses into one undifferentiated blob and positional
# k-means afterwards just carves that blob into arbitrary chunks — the
# readable embedding maps (Nomic Atlas, Map of GitHub, PixPlot; see
# "Cluster and then Embed", arXiv:2509.03373) all do the reverse: find
# semantic clusters in the high-dimensional space, lay the clusters out as
# separated islands, then lay members out locally inside their island.
# All offline so the frontend stays dumb; deterministic seed so re-exports
# don't reshuffle the galaxy.
# ---------------------------------------------------------------------------

GALAXY_SEED = 78
GALAXY_RADIUS = 60.0        # scene units; frontend lifts the cloud off the floor
GALAXY_TAG_CAP = 8          # mirror VIBE_TAG_PROMINENCE_CUTOFF: prominent tags only
GALAXY_MIN_TAGS = 2         # thinner-tagged games go to the "uncharted" shell
GALAXY_MIN_CHARTED = 24     # fewer tagged games than this: skip the mode
KNN_K = 12
KMEANS_MAX_K = 16
MIN_CLUSTER = 5             # smaller clusters merge into their nearest sibling
MEMBER_SPACING = 2.2        # cluster radius = MEMBER_SPACING * cbrt(count)
ANCHOR_GAP = 6.0            # guaranteed empty space between any two islands
ANCHOR_SPREAD = 42.0        # extra distance for dissimilar clusters
Y_SQUASH = 0.6              # oblate galaxy: labels spread on screen, disc read
EDGES_PER_GAME = 2          # constellation lines: strongest in-cluster neighbors


def prominent_tags(raw_tags: object, cap: int = GALAXY_TAG_CAP) -> list[str]:
    """First `cap` vote-ranked real tags, canonicalized and deduplicated.

    Accepts the games.tags JSON string or an already-parsed list. Feature
    flags ("achievements", "controller", ...) never describe taste, so they
    are quarantined the same way the affinity vocabulary quarantines them —
    GTA V's low-vote "racing" tag must not put it in the racing nebula.
    """
    if raw_tags is None:
        return []
    tags = json.loads(raw_tags) if isinstance(raw_tags, str) else list(raw_tags)
    real, _features = split_features(str(t) for t in tags)
    out: list[str] = []
    for tag in real:
        c = canonical_tag(tag)
        if c and c not in out:
            out.append(c)
        if len(out) == cap:
            break
    return out


def tfidf_vectors(
    tag_lists: list[list[str]],
) -> tuple[list[dict[str, float]], dict[str, float]]:
    """L2-normalized TF-IDF vectors (tf is binary — a tag is there or not)."""
    n = len(tag_lists)
    df: dict[str, int] = {}
    for tags in tag_lists:
        for t in tags:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(n / d) for t, d in df.items() if n > 0}
    vecs: list[dict[str, float]] = []
    for tags in tag_lists:
        v = {t: idf[t] for t in tags if idf.get(t, 0) > 0}
        norm = math.sqrt(sum(w * w for w in v.values()))
        vecs.append({t: w / norm for t, w in v.items()} if norm else {})
    return vecs, idf


def knn_edges(
    vecs: list[dict[str, float]], k: int = KNN_K
) -> list[tuple[int, int, float]]:
    """Cosine-similarity kNN via an inverted tag index (only pairs sharing a
    tag can have nonzero similarity, which keeps this fast in pure Python)."""
    by_tag: dict[str, list[int]] = {}
    for i, v in enumerate(vecs):
        for t in v:
            by_tag.setdefault(t, []).append(i)
    edges: dict[tuple[int, int], float] = {}
    for i, v in enumerate(vecs):
        if not v:
            continue
        sims: dict[int, float] = {}
        for t, w in v.items():
            for j in by_tag[t]:
                if j != i:
                    sims[j] = sims.get(j, 0.0) + w * vecs[j][t]
        for j, s in sorted(sims.items(), key=lambda kv: -kv[1])[:k]:
            key = (i, j) if i < j else (j, i)
            if s > edges.get(key, 0.0):
                edges[key] = s
    return [(i, j, s) for (i, j), s in edges.items()]


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def _mean_centers(
    vecs: list[dict[str, float]], assign: list[int], k: int
) -> list[dict[str, float]]:
    sums: list[dict[str, float]] = [{} for _ in range(k)]
    for v, a in zip(vecs, assign):
        s = sums[a]
        for t, w in v.items():
            s[t] = s.get(t, 0.0) + w
    out = []
    for s in sums:
        norm = math.sqrt(sum(w * w for w in s.values()))
        out.append({t: w / norm for t, w in s.items()} if norm else {})
    return out


def spherical_kmeans(
    vecs: list[dict[str, float]], k: int, seed: int = GALAXY_SEED, iters: int = 40
) -> tuple[list[int], list[dict[str, float]]]:
    """Deterministic spherical k-means over sparse unit TF-IDF vectors.

    Clustering happens HERE, in tag space — not on 3D positions after the
    fact. Farthest-point seeding after one seeded random pick; every tie
    breaks on index, so re-exports are stable."""
    n = len(vecs)
    k = max(1, min(k, n))
    rng = random.Random(seed)
    centers = [dict(vecs[rng.randrange(n)])]
    best = [_dot(v, centers[0]) for v in vecs]
    while len(centers) < k:
        far = min(range(n), key=lambda idx: (best[idx], idx))
        centers.append(dict(vecs[far]))
        for idx, v in enumerate(vecs):
            s = _dot(v, centers[-1])
            if s > best[idx]:
                best[idx] = s
    assign = [-1] * n
    for _ in range(iters):
        changed = False
        for i, v in enumerate(vecs):
            b, bs = 0, -1.0
            for c_i, c in enumerate(centers):
                s = _dot(v, c)
                if s > bs:
                    b, bs = c_i, s
            if b != assign[i]:
                assign[i] = b
                changed = True
        if not changed:
            break
        centers = _mean_centers(vecs, assign, len(centers))
    return assign, centers


def merge_small_clusters(
    assign: list[int],
    vecs: list[dict[str, float]],
    centers: list[dict[str, float]],
    min_size: int = MIN_CLUSTER,
) -> tuple[list[int], list[dict[str, float]]]:
    """Fold sub-min_size clusters into their members' nearest big cluster,
    then compact ids so cluster 0 is the largest (colors/labels rank-stable)."""
    counts: dict[int, int] = {}
    for a in assign:
        counts[a] = counts.get(a, 0) + 1
    big = [c_i for c_i in range(len(centers)) if counts.get(c_i, 0) >= min_size]
    if not big:
        big = [max(counts, key=lambda c: (counts[c], -c))]
    for i, v in enumerate(vecs):
        if assign[i] not in big:
            assign[i] = max(big, key=lambda c_i: (_dot(v, centers[c_i]), -c_i))
    counts = {}
    for a in assign:
        counts[a] = counts.get(a, 0) + 1
    order = sorted(counts, key=lambda c: (-counts[c], c))
    idmap = {old: new for new, old in enumerate(order)}
    new_assign = [idmap[a] for a in assign]
    return new_assign, _mean_centers(vecs, new_assign, len(order))


def layout_cluster_anchors(
    centers: list[dict[str, float]],
    radii: list[float],
    seed: int = GALAXY_SEED,
) -> list[list[float]]:
    """Islands, not a blob: every anchor pair is sprung toward an ideal
    distance of (r_i + r_j + gap) plus extra for dissimilar clusters — the
    Kamada-Kawai idea at cluster granularity. Related nebulae end up
    adjacent, unrelated ones across the galaxy, and no two ever touch."""
    k = len(centers)
    if k == 1:
        return [[0.0, 0.0, 0.0]]
    rng = random.Random(seed + 1)
    pos = [[rng.uniform(-20, 20) for _ in range(3)] for _ in range(k)]
    sims = [[_dot(centers[i], centers[j]) for j in range(k)] for i in range(k)]
    for it in range(400):
        step = 0.1 * (1.0 - it / 440) / (k - 1)
        for i in range(k):
            mx = my = mz = 0.0
            for j in range(k):
                if j == i:
                    continue
                dx = pos[i][0] - pos[j][0]
                dy = pos[i][1] - pos[j][1]
                dz = pos[i][2] - pos[j][2]
                d = math.sqrt(dx * dx + dy * dy + dz * dz) or 1e-6
                want = (
                    radii[i] + radii[j] + ANCHOR_GAP
                    + (1.0 - max(0.0, sims[i][j])) * ANCHOR_SPREAD
                )
                f = (want - d) / d  # >0 pushes apart, <0 pulls together
                mx += f * dx
                my += f * dy
                mz += f * dz
            pos[i][0] += mx * step
            pos[i][1] += my * step
            pos[i][2] += mz * step
    # hard resolution pass: springs approximate, this guarantees — any pair
    # still closer than touching + gap is projected apart symmetrically
    for _ in range(80):
        clean = True
        for i in range(k):
            for j in range(i + 1, k):
                dx = pos[i][0] - pos[j][0]
                dy = pos[i][1] - pos[j][1]
                dz = pos[i][2] - pos[j][2]
                d = math.sqrt(dx * dx + dy * dy + dz * dz) or 1e-6
                need = radii[i] + radii[j] + ANCHOR_GAP
                if d < need:
                    clean = False
                    push = (need - d) / d * 0.5
                    for axis, delta in ((0, dx), (1, dy), (2, dz)):
                        pos[i][axis] += delta * push
                        pos[j][axis] -= delta * push
        if clean:
            break
    return pos


def fit_anchors(
    pos: list[list[float]], radii: list[float]
) -> tuple[list[list[float]], list[float]]:
    """Center, squash y (disc read), and scale anchors AND radii together so
    the whole charted volume exactly fills GALAXY_RADIUS — uniform scaling
    keeps the guaranteed inter-island gaps."""
    n = len(pos)
    cx = sum(p[0] for p in pos) / n
    cy = sum(p[1] for p in pos) / n
    cz = sum(p[2] for p in pos) / n
    pos = [[p[0] - cx, (p[1] - cy) * Y_SQUASH, p[2] - cz] for p in pos]
    extent = max(
        math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2) + r
        for p, r in zip(pos, radii)
    ) or 1.0
    k = GALAXY_RADIUS / extent
    return (
        [[p[0] * k, p[1] * k, p[2] * k] for p in pos],
        [r * k for r in radii],
    )


def layout_members(
    count: int,
    edges_local: list[tuple[int, int, float]],
    radius: float,
    seed: int,
) -> list[list[float]]:
    """Local force layout inside one island: kNN springs give internal
    structure, all-pairs (small) or sampled (large) repulsion spreads the
    cloud, gravity + a hard clamp keep everything inside the island."""
    rng = random.Random(seed)
    pos = []
    for _ in range(count):
        while True:
            p = [rng.uniform(-1.0, 1.0) for _ in range(3)]
            if p[0] ** 2 + p[1] ** 2 + p[2] ** 2 <= 1.0:
                break
        pos.append([c * radius * 0.85 for c in p])
    if count < 2:
        return [[0.0, 0.0, 0.0]] * count
    rep = radius * radius * 0.02
    for it in range(120):
        step = 0.12 * (1.0 - it / 130)
        for a, b, s in edges_local:
            rest = radius * 0.45 * (1.35 - s)
            dx = pos[b][0] - pos[a][0]
            dy = pos[b][1] - pos[a][1]
            dz = pos[b][2] - pos[a][2]
            d = math.sqrt(dx * dx + dy * dy + dz * dz) or 1e-6
            f = (d - rest) / d * 0.4 * s * step
            pos[a][0] += f * dx
            pos[a][1] += f * dy
            pos[a][2] += f * dz
            pos[b][0] -= f * dx
            pos[b][1] -= f * dy
            pos[b][2] -= f * dz
        for i in range(count):
            others = range(count) if count <= 90 else (
                rng.randrange(count) for _ in range(8)
            )
            for j in others:
                if j == i:
                    continue
                dx = pos[i][0] - pos[j][0]
                dy = pos[i][1] - pos[j][1]
                dz = pos[i][2] - pos[j][2]
                d2 = dx * dx + dy * dy + dz * dz + 1e-3
                f = min(rep / d2, 1.5) * step
                pos[i][0] += dx * f
                pos[i][1] += dy * f
                pos[i][2] += dz * f
            # gravity + clamp inside the island radius
            r = math.sqrt(sum(c * c for c in pos[i])) or 1e-6
            pull = 0.02 * step if r < radius else (r - radius) / r + 0.02 * step
            pos[i][0] -= pos[i][0] * pull
            pos[i][1] -= pos[i][1] * pull
            pos[i][2] -= pos[i][2] * pull
    return [[p[0], p[1] * 0.75, p[2]] for p in pos]


def cluster_color(rank: int) -> str:
    """Golden-angle hue walk — the Nomic-Atlas-style categorical palette:
    consecutive cluster ranks land far apart on the wheel, deterministic."""
    h = (rank * 137.508) % 360.0 / 360.0
    r, g, b = colorsys.hls_to_rgb(h, 0.62, 0.72)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def cluster_label(
    member_tags: list[list[str]],
    df: dict[str, int],
    total: int,
    used: set[str] | None = None,
) -> str | None:
    """The most-overrepresented tag among a cluster's members: highest ratio
    of in-cluster frequency to library-wide frequency, needing >=3 carriers.
    Tags already naming another cluster are skipped so labels stay unique."""
    counts: dict[str, int] = {}
    for tags in member_tags:
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    best, best_score = None, 0.0
    for t, c in counts.items():
        if c < 3 or (used and t in used):
            continue
        score = (c / len(member_tags)) / (df.get(t, 1) / total)
        if score > best_score:
            best, best_score = t, score
    return best


def affinity_of(
    tags: list[str], affinity: dict[str, float], idf: dict[str, float]
) -> float | None:
    """IDF-weighted mean affinity over ALL the game's tags, the way
    discover_games scores — tags without affinity dilute toward neutral."""
    if not tags:
        return None
    num = sum(idf.get(t, 0.0) * affinity.get(t, 0.0) for t in tags)
    den = sum(idf.get(t, 0.0) for t in tags)
    return num / den if den else None


def galaxy_embedding(
    games: list[dict], affinity: dict[str, float]
) -> dict[str, object] | None:
    """Compute pos/aff/cl per game (mutates the dicts) and return meta:
    labeled+colored clusters and the constellation edge list.

    Pipeline: spherical k-means in TAG space -> cluster islands laid out
    with hard separation -> members laid out locally inside their island.
    Games with fewer than GALAXY_MIN_TAGS tags are parked in an "uncharted"
    shell at the galaxy edge rather than polluting the clusters.
    """
    tag_lists = [g["tags"] for g in games]
    core_idx = [i for i, t in enumerate(tag_lists) if len(t) >= GALAXY_MIN_TAGS]
    if len(core_idx) < GALAXY_MIN_CHARTED:
        return None   # not enough tagged games to chart anything

    core_tags = [tag_lists[i] for i in core_idx]
    vecs, idf = tfidf_vectors(core_tags)
    n = len(vecs)

    k = max(4, min(KMEANS_MAX_K, n // 25))
    assign, centers = spherical_kmeans(vecs, k)
    assign, centers = merge_small_clusters(assign, vecs, centers)
    n_clusters = max(assign) + 1

    counts = [0] * n_clusters
    for a in assign:
        counts[a] += 1
    radii = [max(3.0, MEMBER_SPACING * c ** (1 / 3)) for c in counts]
    anchors = layout_cluster_anchors(centers, radii)
    anchors, radii = fit_anchors(anchors, radii)

    # members: local layout per island, sprung on the in-cluster kNN edges
    members: list[list[int]] = [[] for _ in range(n_clusters)]
    local_of: list[int] = [0] * n
    for i, a in enumerate(assign):
        local_of[i] = len(members[a])
        members[a].append(i)
    edges = knn_edges(vecs)
    intra: list[list[tuple[int, int, float]]] = [[] for _ in range(n_clusters)]
    for i, j, s in edges:
        if assign[i] == assign[j]:
            intra[assign[i]].append((local_of[i], local_of[j], s))

    pos: list[list[float]] = [[0.0, 0.0, 0.0]] * n
    for c_i in range(n_clusters):
        local = layout_members(
            counts[c_i], intra[c_i], radii[c_i], GALAXY_SEED + 10 + c_i
        )
        ax, ay, az = anchors[c_i]
        for li, i in enumerate(members[c_i]):
            p = local[li]
            pos[i] = [ax + p[0], ay + p[1], az + p[2]]

    for local_i, i in enumerate(core_idx):
        games[i]["pos"] = [round(c, 2) for c in pos[local_i]]
        games[i]["cl"] = assign[local_i]

    # uncharted shell: deterministic ring just outside the charted volume
    shell_r = GALAXY_RADIUS * 1.12
    for i, g in enumerate(games):
        if "pos" not in g:
            theta = (hash_angle(g["id"], 1)) * 2 * math.pi
            y = (hash_angle(g["id"], 2) - 0.5) * GALAXY_RADIUS * 0.5
            g["pos"] = [
                round(shell_r * math.cos(theta), 2),
                round(y, 2),
                round(shell_r * math.sin(theta), 2),
            ]
            g["uncharted"] = True

    df: dict[str, int] = {}
    for tags in core_tags:
        for t in tags:
            df[t] = df.get(t, 0) + 1
    clusters = []
    used: set[str] = set()
    for c_i in range(n_clusters):
        label = cluster_label(
            [core_tags[m] for m in members[c_i]], df, len(core_tags), used
        )
        if label:
            used.add(label)
        clusters.append(
            {
                "label": label or "misc",
                "pos": [round(v, 2) for v in anchors[c_i]],
                "r": round(radii[c_i], 2),
                "count": counts[c_i],
                "color": cluster_color(c_i),
            }
        )

    # constellation edges: each game's strongest same-cluster neighbors,
    # exported as index pairs into the games array for the line pass
    strongest: dict[int, list[tuple[float, int]]] = {}
    for i, j, s in edges:
        if assign[i] != assign[j]:
            continue
        strongest.setdefault(i, []).append((s, j))
        strongest.setdefault(j, []).append((s, i))
    pairs: set[tuple[int, int]] = set()
    for i, lst in strongest.items():
        for s, j in sorted(lst, key=lambda t: (-t[0], t[1]))[:EDGES_PER_GAME]:
            pairs.add((min(i, j), max(i, j)))
    game_edges = [[core_idx[i], core_idx[j]] for i, j in sorted(pairs)]

    for g in games:
        aff = affinity_of(g["tags"], affinity, idf)
        if aff is not None:
            g["aff"] = round(aff, 3)

    return {"clusters": clusters, "edges": game_edges}


def hash_angle(game_id: int, salt: int) -> float:
    """Deterministic [0, 1) from a game id (same spirit as the JS jitter)."""
    h = (game_id * 2654435761 + salt * 40503) & 0xFFFFFFFF
    h = ((h ^ (h >> 15)) * 2246822519) & 0xFFFFFFFF
    return (h & 0xFFFF) / 0x10000


def family_of(platform: str) -> str:
    if platform.startswith("switch"):
        return "nintendo"
    if platform.startswith("ps") or platform == "psn":
        return "sony"
    if platform.startswith("xbox"):
        return "xbox"
    return "pc"


def load_games(db_path: Path) -> tuple[list[dict], dict[str, float]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.release_date, g.cover_image_id,
                   g.is_farmed, g.completion_status, g.hltb_main, g.tags
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

        affinity: dict[str, float] = {}
        for r in conn.execute(
            "SELECT tag, affinity_score FROM tag_affinity"
            " WHERE affinity_score IS NOT NULL"
        ):
            affinity[canonical_tag(r["tag"])] = r["affinity_score"]
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
                "tags": prominent_tags(r["tags"]),
                "_cover_id": r["cover_image_id"],
                "_appid": appids.get(r["id"]),
            }
        )
    return games, affinity


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

    games, affinity = load_games(args.db)
    print(f"  games: {len(games)} owned primary library items")

    galaxy = galaxy_embedding(games, affinity)
    if galaxy:
        charted = sum(1 for g in games if not g.get("uncharted"))
        print(
            f"  galaxy: {charted} charted, {len(games) - charted} uncharted, "
            f"{len(galaxy['clusters'])} labeled clusters"
        )
    else:
        print("  galaxy: skipped (not enough tagged games)")

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
            **(galaxy or {}),
        },
        "games": games,
    }
    out_json = args.out / "library.json"
    out_json.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"  wrote {out_json} ({out_json.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
