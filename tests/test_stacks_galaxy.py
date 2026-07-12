"""Galaxy-embedding math in scripts/export_stacks.py (issue #78).

The script isn't a package module, so load it by path. These tests pin the
properties the feature depends on: determinism across re-exports, tag
hygiene (canonicalization, feature-flag quarantine, prominence cap), and —
the point of the cluster-first pipeline — that semantic clusters become
visually separated islands (no cluster mixes disjoint tastes, members stay
inside their island's radius, constellation edges never bridge islands).
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "export_stacks",
    Path(__file__).resolve().parent.parent / "scripts" / "export_stacks.py",
)
assert _SPEC and _SPEC.loader
export_stacks = importlib.util.module_from_spec(_SPEC)
sys.modules["export_stacks"] = export_stacks
_SPEC.loader.exec_module(export_stacks)


def _clique_games(n_per: int = 30) -> list[dict]:
    """Two disjoint tag cliques + a couple of thin-tag stragglers."""
    games = []
    rogue = ["roguelike", "deckbuilder", "turn-based", "dungeon crawler"]
    cozy = ["cozy", "farming-sim", "relaxing", "life sim"]
    for i in range(n_per):
        games.append({"id": i + 1, "tags": rogue[: 2 + i % 3]})
    for i in range(n_per):
        games.append({"id": 100 + i, "tags": cozy[: 2 + i % 3]})
    games.append({"id": 900, "tags": ["roguelike"]})   # thin: uncharted
    games.append({"id": 901, "tags": []})              # untagged: uncharted
    return games


def test_prominent_tags_canonicalizes_caps_and_quarantines() -> None:
    raw = [
        "Roguelike",            # lowercased
        "achievements",         # feature flag: quarantined
        "ThirdPerson",          # synonym -> third-person
        "roguelike",            # duplicate after canonicalization
        "a", "b", "c", "d", "e", "f", "g", "h",
    ]
    tags = export_stacks.prominent_tags(raw)
    assert tags[0] == "roguelike"
    assert "achievements" not in tags
    assert "third-person" in tags
    assert len(tags) == export_stacks.GALAXY_TAG_CAP
    assert len(set(tags)) == len(tags)


def test_prominent_tags_accepts_json_string_and_none() -> None:
    assert export_stacks.prominent_tags('["Cozy", "Farming"]') == ["cozy", "farming"]
    assert export_stacks.prominent_tags(None) == []


def test_embedding_is_deterministic() -> None:
    a = _clique_games()
    b = _clique_games()
    meta_a = export_stacks.galaxy_embedding(a, {})
    meta_b = export_stacks.galaxy_embedding(b, {})
    assert meta_a is not None and meta_b is not None
    assert [g["pos"] for g in a] == [g["pos"] for g in b]
    assert meta_a["clusters"] == meta_b["clusters"]


def test_cliques_separate_and_thin_games_go_uncharted() -> None:
    games = _clique_games()
    meta = export_stacks.galaxy_embedding(games, {})
    assert meta is not None

    def centroid(gs: list[dict]) -> tuple[float, float, float]:
        xs = [g["pos"] for g in gs]
        n = len(xs)
        return (
            sum(p[0] for p in xs) / n,
            sum(p[1] for p in xs) / n,
            sum(p[2] for p in xs) / n,
        )

    rogues = [g for g in games if "roguelike" in g["tags"] and not g.get("uncharted")]
    cozies = [g for g in games if "cozy" in g["tags"] and not g.get("uncharted")]
    ca, cb = centroid(rogues), centroid(cozies)
    inter = math.dist(ca, cb)

    def mean_spread(gs: list[dict], c: tuple[float, float, float]) -> float:
        return sum(math.dist(g["pos"], c) for g in gs) / len(gs)

    # the two taste families sit far apart relative to their own width —
    # islands with guaranteed gaps, not lobes of one blob
    assert inter > 1.5 * mean_spread(rogues, ca)
    assert inter > 1.5 * mean_spread(cozies, cb)

    # thin/untagged games are parked on the shell outside the charted volume
    for g in games:
        if g.get("uncharted"):
            r = math.hypot(g["pos"][0], g["pos"][2])
            assert r == pytest.approx(export_stacks.GALAXY_RADIUS * 1.12, rel=0.01)

    # cluster labels name the anchor tags
    labels = {c["label"] for c in meta["clusters"]}
    assert labels & {"roguelike", "deckbuilder", "turn-based", "dungeon crawler",
                     "cozy", "farming-sim", "relaxing", "life sim"}


def test_clusters_are_semantic_islands() -> None:
    """Clustering happens in tag space, so no cluster mixes the two tastes;
    every member sits inside its island's exported radius; constellation
    edges stay within an island; colors/labels are unique and well-formed."""
    games = _clique_games()
    meta = export_stacks.galaxy_embedding(games, {})
    assert meta is not None

    charted = [g for g in games if not g.get("uncharted")]
    assert all("cl" in g for g in charted)
    rogue_cl = {g["cl"] for g in charted if "roguelike" in g["tags"]}
    cozy_cl = {g["cl"] for g in charted if "cozy" in g["tags"]}
    assert rogue_cl.isdisjoint(cozy_cl)

    clusters = meta["clusters"]
    for g in charted:
        c = clusters[g["cl"]]
        assert math.dist(g["pos"], c["pos"]) <= c["r"] + 0.1

    assert meta["edges"]
    for a, b in meta["edges"]:
        assert games[a]["cl"] == games[b]["cl"]

    colors = [c["color"] for c in clusters]
    assert all(re.fullmatch(r"#[0-9a-f]{6}", col) for col in colors)
    assert len(set(colors)) == len(colors)
    labels = [c["label"] for c in clusters]
    assert len(set(labels)) == len(labels)
    assert all(c["r"] > 0 and c["count"] >= 1 for c in clusters)


def test_affinity_is_idf_weighted_mean() -> None:
    idf = {"rare": 2.0, "common": 0.5}
    affinity = {"rare": 1.0, "common": -1.0}
    aff = export_stacks.affinity_of(["rare", "common"], affinity, idf)
    assert aff == pytest.approx((2.0 * 1.0 + 0.5 * -1.0) / 2.5)
    # tags without affinity dilute toward neutral instead of being skipped
    diluted = export_stacks.affinity_of(["rare", "unknown-but-weighty"], affinity,
                                        {"rare": 2.0, "unknown-but-weighty": 2.0})
    assert diluted == pytest.approx(0.5)
    assert export_stacks.affinity_of([], affinity, idf) is None


def test_galaxy_skips_when_library_is_untagged() -> None:
    games = [{"id": i, "tags": []} for i in range(50)]
    assert export_stacks.galaxy_embedding(games, {}) is None
