#!/usr/bin/env python3
"""Cross-validated backtest of the taste profile (``tag_affinity`` + discover's match score).

Two consumers read the profile and neither had a measurement: ``discover_games``
(what to play) and ``get_assessment_context``'s fit check (whether to buy). This
harness holds out ratings, recomputes the profile without them, scores the
held-out games with ``discover_games``' OWN scoring SQL, and reports how well
the prediction tracks the rating the model never saw — plus a rating-free
control (does the match score track playtime on unrated games?).

Run it against a SNAPSHOT, never a live database: see README.md. The input file
is copied into a temp directory before anything is opened for writing, so the
snapshot is never mutated.

Usage:
    python evals/taste-profile-eval/eval_profile.py --db ~/backups/gamelib-nightly.bak
    python evals/taste-profile-eval/eval_profile.py --db snap.bak --baseline
    python evals/taste-profile-eval/eval_profile.py --db snap.bak --folds 0 --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import shutil
import sys
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# Importable as a module (tests do that) and runnable as a script from any cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gamelib_mcp.data import db as db_module  # noqa: E402
from gamelib_mcp.data.db.affinity import (  # noqa: E402
    SOURCE_WEIGHTS,
    get_affinity_scale,
    recompute_tag_affinity,
)
from gamelib_mcp.tools.discover import (  # noqa: E402
    _IDF_DF_FLOOR,
    _MATCH_PRIOR,
    _MATCH_SCORE_SQL,
    _SCORING_CTES,
    VIBE_TAG_PROMINENCE_CUTOFF,
)

# The eval measures the SHIPPED model, so the score comes from discover's own
# CTEs and expression rather than a re-implementation — a local copy of the
# formula would quietly start measuring a different model the first time
# discover.py changed.
_SCORE_QUERY = (
    _SCORING_CTES
    + f"""
SELECT game_rollup.game_id AS game_id,
       game_rollup.name AS name,
       game_rollup.total_playtime_minutes AS total_playtime_minutes,
       {_MATCH_SCORE_SQL} AS match_score
FROM game_rollup
"""
)

# Ratings rows are lifted out and put back verbatim between folds, so the
# working copy ends each fold byte-equivalent (for our purposes) to its start.
_RATING_COLUMNS = (
    "id",
    "game_id",
    "source",
    "raw_score",
    "normalized_score",
    "review_text",
    "synced_at",
)

# SQLite's default host-parameter cap is 999 on old builds; chunk well under it.
_PARAM_CHUNK = 400

# "Loved" for precision@10 and the low/high separation gap, on the normalized
# 0-10 rating scale the affinity recompute consumes.
LOVED_CUT = 8.0
DISLIKED_CUT = 4.0
PRECISION_AT = 10

DEFAULT_MIN_RATINGS = 20


class EvalError(RuntimeError):
    """A refusal the CLI reports as a message rather than a traceback."""


# ── statistics (pure Python — no scipy in this project's dependency set) ──────


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ranks with ties averaged (the tie correction Spearman's rho needs)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rho, or None when it is undefined (<3 pairs, or no variance)."""
    if len(xs) != len(ys):
        raise ValueError("spearman(): unequal input lengths")
    if len(xs) < 3:
        return None
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


# ── working copy ─────────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _working_copy(source: Path, workdir: Path) -> AsyncIterator[Path]:
    """Copy the snapshot into ``workdir`` and point the app's DB layer at it.

    The input is only ever READ: every write below lands on the copy. The
    -wal sidecar is copied along when present so a snapshot taken from a live
    WAL database still recovers its last committed transactions; -shm is
    deliberately NOT copied (SQLite rebuilds it, and a stale one is unsafe).

    DATABASE_URL is swapped exactly the way tests/conftest.py does, including
    the readiness flags, and restored afterwards — the module is importable
    into a test process that has its own database configured.
    """
    destination = (workdir / "eval-working-copy.sqlite").resolve()
    shutil.copyfile(source, destination)
    wal = Path(f"{source}-wal")
    if wal.exists():
        shutil.copyfile(wal, f"{destination}-wal")

    previous_url = os.environ.get("DATABASE_URL")
    previous_ready = db_module._DB_READY_PATH
    previous_fts = db_module._FTS_READY_PATH
    os.environ["DATABASE_URL"] = f"file:{destination}"
    db_module._DB_READY_PATH = None
    db_module._FTS_READY_PATH = None
    try:
        # Migrates the copy if the snapshot predates the current schema, and is
        # a no-op on a current one. Pooling stays off (the default), so every
        # get_db() closes its aiosqlite worker thread on exit.
        await db_module.init_db()
        yield destination
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        db_module._DB_READY_PATH = previous_ready
        db_module._FTS_READY_PATH = previous_fts


# ── data access on the working copy ──────────────────────────────────────────


@dataclass
class RatedGame:
    game_id: int
    actual: float
    sources: list[str] = field(default_factory=list)


async def _load_rated_games() -> tuple[list[RatedGame], list[dict[str, Any]]]:
    """Rated games with usable tags, plus every ratings row (for restore).

    ``actual`` is the SOURCE-WEIGHTED mean of a game's ``normalized_score``
    values, using the same SOURCE_WEIGHTS the recompute applies (a Steam review
    is half an observation) — so the target is on exactly the 0-10 scale, and
    under exactly the source weighting, that the model is trained on.
    """
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT r.game_id, r.source, r.normalized_score
            FROM ratings r
            JOIN games g ON g.id = r.game_id
            WHERE r.normalized_score IS NOT NULL
              AND g.tags IS NOT NULL
              AND g.tags NOT IN ('', '[]')
            """
        )
        all_ratings = await db.execute_fetchall(
            f"SELECT {', '.join(_RATING_COLUMNS)} FROM ratings"
        )

    accumulator: dict[int, list[tuple[float, float, str]]] = {}
    for row in rows:
        weight = SOURCE_WEIGHTS.get(row["source"], 0.5)
        accumulator.setdefault(row["game_id"], []).append(
            (weight, float(row["normalized_score"]), row["source"])
        )

    rated: list[RatedGame] = []
    for game_id, signals in accumulator.items():
        total_weight = sum(w for w, _, _ in signals)
        if total_weight <= 0:
            continue
        rated.append(
            RatedGame(
                game_id=game_id,
                actual=sum(w * s for w, s, _ in signals) / total_weight,
                sources=sorted({source for _, _, source in signals}),
            )
        )
    rated.sort(key=lambda item: item.game_id)
    return rated, [dict(row) for row in all_ratings]


async def _score_library(
    game_ids: Sequence[int] | None = None,
) -> dict[int, dict[str, Any]]:
    """match_score + rolled-up playtime for owned primary games.

    ``game_ids`` narrows only the OUTER select: lib_size and lib_tag_df still
    span the whole owned library, so IDF is identical to what discover_games
    computes — the filter is a cost saver (one correlated subquery per row),
    not a change of model. Leave-one-out on a real library would otherwise
    rescore three thousand games per fold.
    """
    sql = _SCORE_QUERY
    params: tuple[int, ...] = ()
    if game_ids is not None and len(game_ids) <= _PARAM_CHUNK:
        placeholders = ",".join("?" * len(game_ids))
        sql += f"WHERE game_rollup.game_id IN ({placeholders})"
        params = tuple(game_ids)
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(sql, params)
    return {
        row["game_id"]: {
            "name": row["name"],
            "match_score": row["match_score"],
            "playtime_minutes": row["total_playtime_minutes"],
        }
        for row in rows
    }


def _chunks(values: Sequence[int]) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), _PARAM_CHUNK):
        yield values[start : start + _PARAM_CHUNK]


async def _hold_out(game_ids: Sequence[int]) -> dict[int, int]:
    """Remove a fold's ratings AND its playtime signal; return the farmed flags.

    Deleting the ratings alone would leak: the recompute feeds a 0.3-weight
    playtime pseudo-rating for owned, unrated, non-farmed games with >=2h, so a
    game whose rating we just deleted would come straight back into the profile
    through its own playtime. Flagging it farmed for the duration is the one
    switch that excludes it from that query without touching affinity.py, and
    the flag is irrelevant to the scoring CTE (which selects, never filters on,
    is_farmed).
    """
    farmed: dict[int, int] = {}
    async with db_module.get_db() as db:
        for chunk in _chunks(game_ids):
            placeholders = ",".join("?" * len(chunk))
            rows = await db.execute_fetchall(
                f"SELECT id, is_farmed FROM games WHERE id IN ({placeholders})",
                tuple(chunk),
            )
            for row in rows:
                farmed[row["id"]] = row["is_farmed"]
            await db.execute(
                f"DELETE FROM ratings WHERE game_id IN ({placeholders})", tuple(chunk)
            )
            await db.execute(
                f"UPDATE games SET is_farmed = 1 WHERE id IN ({placeholders})",
                tuple(chunk),
            )
        await db.commit()
    return farmed


async def _restore(
    game_ids: Sequence[int],
    ratings_rows: Sequence[dict[str, Any]],
    farmed: dict[int, int],
) -> None:
    held = set(game_ids)
    columns = ", ".join(_RATING_COLUMNS)
    placeholders = ", ".join("?" * len(_RATING_COLUMNS))
    async with db_module.get_db() as db:
        for row in ratings_rows:
            if row["game_id"] in held:
                await db.execute(
                    f"INSERT OR REPLACE INTO ratings ({columns}) VALUES ({placeholders})",
                    tuple(row[column] for column in _RATING_COLUMNS),
                )
        for game_id, flag in farmed.items():
            await db.execute(
                "UPDATE games SET is_farmed = ? WHERE id = ?", (flag, game_id)
            )
        await db.commit()


async def _neutralise_affinity(rng: random.Random) -> None:
    """Baseline control: shuffle affinity across tags, keeping the distribution.

    Zeroing every score would make the prediction constant and Spearman
    undefined; a permutation is the honest null — same scale, same spread, no
    correspondence between a tag and its score.
    """
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall("SELECT tag, affinity_score FROM tag_affinity")
        tags = [row["tag"] for row in rows]
        scores = [row["affinity_score"] for row in rows]
        rng.shuffle(scores)
        for tag, score in zip(tags, scores, strict=True):
            await db.execute(
                "UPDATE tag_affinity SET affinity_score = ? WHERE tag = ?", (score, tag)
            )
        await db.commit()


async def _rebuild_profile(baseline: bool, rng: random.Random) -> float | None:
    await recompute_tag_affinity()
    if baseline:
        await _neutralise_affinity(rng)
    scale = await get_affinity_scale()
    weight = scale.get("shrinkage_weight")
    return float(weight) if isinstance(weight, (int, float)) else None


# ── the evaluation ───────────────────────────────────────────────────────────


def _make_folds(game_ids: Sequence[int], folds: int, seed: int) -> list[list[int]]:
    shuffled = list(game_ids)
    random.Random(seed).shuffle(shuffled)
    if folds <= 0:  # leave-one-out
        return [[game_id] for game_id in shuffled]
    folds = min(folds, len(shuffled))
    buckets: list[list[int]] = [[] for _ in range(folds)]
    for index, game_id in enumerate(shuffled):
        buckets[index % folds].append(game_id)
    return [bucket for bucket in buckets if bucket]


async def run_eval(
    db_path: str | Path,
    *,
    folds: int = 10,
    seed: int = 0,
    baseline: bool = False,
    min_ratings: int = DEFAULT_MIN_RATINGS,
) -> dict[str, Any]:
    """Cross-validate the taste profile against a snapshot; return the metrics.

    ``folds=0`` means leave-one-out. ``baseline=True`` scores with the taste
    model neutralised (affinity shuffled across tags), which is the "no taste
    model" control every other number should be read against. The snapshot at
    ``db_path`` is never written to.
    """
    source = Path(db_path).expanduser()
    if not source.is_file():
        raise EvalError(f"No such database snapshot: {source}")

    started = time.monotonic()
    rng = random.Random(seed)

    with TemporaryDirectory(prefix="taste-profile-eval-") as tmpdir:
        async with _working_copy(source, Path(tmpdir)):
            rated, ratings_rows = await _load_rated_games()
            if len(rated) < min_ratings:
                raise EvalError(
                    f"Only {len(rated)} rated games with tags in {source.name}; "
                    f"this harness needs at least {min_ratings} to say anything "
                    f"(--min-ratings lowers the bar, at the cost of meaning)."
                )

            # 1. Full model, no hold-out: the rating-free control signal.
            full_k = await _rebuild_profile(baseline, rng)
            scored = await _score_library()
            rated_ids = {item.game_id for item in rated}
            playtime = _playtime_metrics(scored, rated_ids)

            # 2. K-fold over the rated games.
            fold_ids = _make_folds([item.game_id for item in rated], folds, seed)
            actual_by_id = {item.game_id: item.actual for item in rated}
            pairs: list[dict[str, Any]] = []
            fold_reports: list[dict[str, Any]] = []

            for index, held in enumerate(fold_ids):
                farmed = await _hold_out(held)
                try:
                    fold_k = await _rebuild_profile(baseline, rng)
                    fold_scores = await _score_library(held)
                finally:
                    await _restore(held, ratings_rows, farmed)

                fold_pairs = [
                    {
                        "fold": index,
                        "game_id": game_id,
                        "predicted": fold_scores[game_id]["match_score"],
                        "actual": actual_by_id[game_id],
                    }
                    for game_id in held
                    if game_id in fold_scores
                    and fold_scores[game_id]["match_score"] is not None
                ]
                pairs.extend(fold_pairs)
                fold_reports.append(
                    {
                        "fold": index,
                        "n_held_out": len(held),
                        "n_scored": len(fold_pairs),
                        "shrinkage_k": fold_k,
                        "spearman": spearman(
                            [p["predicted"] for p in fold_pairs],
                            [p["actual"] for p in fold_pairs],
                        ),
                    }
                )

    pooled = _pooled_metrics(pairs)
    elapsed = time.monotonic() - started
    return {
        "config": {
            "db_file": source.name,
            "folds": "leave-one-out" if folds <= 0 else folds,
            "n_folds_run": len(fold_reports),
            "seed": seed,
            "baseline": baseline,
            "min_ratings": min_ratings,
            "match_prior": _MATCH_PRIOR,
            "idf_df_floor": _IDF_DF_FLOOR,
            "vibe_tag_prominence_cutoff": VIBE_TAG_PROMINENCE_CUTOFF,
            "source_weights": dict(SOURCE_WEIGHTS),
            "rating_target": "source-weighted mean normalized_score (0-10)",
            "full_model_shrinkage_k": full_k,
        },
        "counts": {
            "n_rated": len(rated),
            "n_rated_scored": pooled["n"],
            "n_unrated_scored": playtime["n"],
            "n_unrated_played": playtime["n_played"],
        },
        "pooled": {
            "spearman": pooled["spearman"],
            "precision_at_k": pooled["precision_at_k"],
            "precision_k": pooled["precision_k"],
            "separation": pooled["separation"],
        },
        "playtime_control": playtime,
        "folds": fold_reports,
        "wall_seconds": round(elapsed, 2),
    }


def _pooled_metrics(pairs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    predicted = [p["predicted"] for p in pairs]
    actual = [p["actual"] for p in pairs]

    ranked = sorted(pairs, key=lambda p: p["predicted"], reverse=True)
    top = ranked[:PRECISION_AT]
    precision = (
        sum(1 for p in top if p["actual"] >= LOVED_CUT) / len(top) if top else None
    )

    low = _mean(p["predicted"] for p in pairs if p["actual"] <= DISLIKED_CUT)
    high = _mean(p["predicted"] for p in pairs if p["actual"] >= LOVED_CUT)
    return {
        "n": len(pairs),
        "spearman": spearman(predicted, actual),
        "precision_at_k": precision,
        "precision_k": len(top),
        "separation": {
            "mean_predicted_disliked": low,
            "mean_predicted_loved": high,
            "gap": (high - low) if (low is not None and high is not None) else None,
            "n_disliked": sum(1 for p in pairs if p["actual"] <= DISLIKED_CUT),
            "n_loved": sum(1 for p in pairs if p["actual"] >= LOVED_CUT),
        },
    }


def _playtime_metrics(
    scored: dict[int, dict[str, Any]], rated_ids: set[int]
) -> dict[str, Any]:
    """Rating-free control: does the full-model match score track playtime?

    Caveat recorded in the README: unrated owned games with >=2h ARE the
    playtime pseudo-rating population, so the full model has seen this
    signal. It is a consistency check on the shipped model, not a hold-out.
    """
    predicted: list[float] = []
    played: list[float] = []
    for game_id, row in scored.items():
        if game_id in rated_ids or row["match_score"] is None:
            continue
        predicted.append(row["match_score"])
        played.append(math.log1p(max(0.0, float(row["playtime_minutes"] or 0))))

    nonzero = [(p, t) for p, t in zip(predicted, played, strict=True) if t > 0]
    return {
        "n": len(predicted),
        "n_played": len(nonzero),
        "spearman": spearman(predicted, played),
        "spearman_played_only": spearman(
            [p for p, _ in nonzero], [t for _, t in nonzero]
        ),
    }


# ── reporting ────────────────────────────────────────────────────────────────


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(metrics: dict[str, Any]) -> str:
    config = metrics["config"]
    counts = metrics["counts"]
    pooled = metrics["pooled"]
    separation = pooled["separation"]
    control = metrics["playtime_control"]

    lines = [
        (
            f"# Taste-profile backtest — {config['db_file']}"
            + (" (BASELINE: taste model neutralised)" if config["baseline"] else "")
        ),
        "",
        "| setting | value |",
        "| --- | --- |",
        f"| folds | {config['folds']} ({config['n_folds_run']} run) |",
        f"| seed | {config['seed']} |",
        f"| rated games (with tags) | {counts['n_rated']} |",
        f"| held-out games scored | {counts['n_rated_scored']} |",
        (
            f"| unrated owned games scored | {counts['n_unrated_scored']}"
            f" ({counts['n_unrated_played']} with playtime) |"
        ),
        f"| _MATCH_PRIOR | {config['match_prior']} |",
        f"| _IDF_DF_FLOOR | {config['idf_df_floor']} |",
        f"| VIBE_TAG_PROMINENCE_CUTOFF | {config['vibe_tag_prominence_cutoff']} |",
        f"| shrinkage k (full model) | {_fmt(config['full_model_shrinkage_k'])} |",
        f"| rating target | {config['rating_target']} |",
        f"| wall time | {metrics['wall_seconds']}s |",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| Spearman rho (held-out) | {_fmt(pooled['spearman'])} |",
        (
            f"| precision@{pooled['precision_k']} (rating >= {LOVED_CUT:.0f}) |"
            f" {_fmt(pooled['precision_at_k'])} |"
        ),
        (
            f"| mean predicted, rating <= {DISLIKED_CUT:.0f} |"
            f" {_fmt(separation['mean_predicted_disliked'], 4)}"
            f" (n={separation['n_disliked']}) |"
        ),
        (
            f"| mean predicted, rating >= {LOVED_CUT:.0f} |"
            f" {_fmt(separation['mean_predicted_loved'], 4)}"
            f" (n={separation['n_loved']}) |"
        ),
        f"| separation gap | {_fmt(separation['gap'], 4)} |",
        f"| playtime Spearman (unrated owned) | {_fmt(control['spearman'])} |",
        f"| playtime Spearman (played only) | {_fmt(control['spearman_played_only'])} |",
        "",
        "| fold | held out | scored | shrinkage k | Spearman |",
        "| --- | --- | --- | --- | --- |",
    ]
    for fold in metrics["folds"]:
        lines.append(
            f"| {fold['fold']} | {fold['n_held_out']} | {fold['n_scored']} |"
            f" {_fmt(fold['shrinkage_k'], 1)} | {_fmt(fold['spearman'])} |"
        )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-validated backtest of the taste profile against a database "
            "snapshot. The snapshot is copied before use and never modified."
        )
    )
    parser.add_argument(
        "--db",
        required=True,
        help="SQLite snapshot (nightly .bak, or `sqlite3 <db> \".backup out.bak\"`).",
    )
    parser.add_argument(
        "--folds", type=int, default=10, help="K-fold count; 0 = leave-one-out."
    )
    parser.add_argument("--seed", type=int, default=0, help="Fold-shuffle seed.")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Score with the taste model neutralised (affinity shuffled).",
    )
    parser.add_argument("--json", dest="json_path", help="Write the metrics here.")
    parser.add_argument(
        "--min-ratings",
        type=int,
        default=DEFAULT_MIN_RATINGS,
        help=f"Refuse to run below this many rated games (default {DEFAULT_MIN_RATINGS}).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        metrics = asyncio.run(
            run_eval(
                args.db,
                folds=args.folds,
                seed=args.seed,
                baseline=args.baseline,
                min_ratings=args.min_ratings,
            )
        )
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render_markdown(metrics))
    if args.json_path:
        Path(args.json_path).expanduser().write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
