"""Near-duplicate clustering.

Drawn from a real gap pull, where fifteen rows described one opportunity. The
tests fix both directions: restatements must collapse, and genuinely different
queries must not.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from zipf.services import gap
from zipf.services.cluster import Cluster, cluster_key, cluster_rows, singular


def _row(keyword: str, volume: int | None = 1000, position: int | None = 10) -> dict[str, Any]:
    return {"keyword": keyword, "volume": volume, "position": position, "url": "https://x.com/a"}


@pytest.mark.parametrize(
    "phrasings",
    [
        # The exact case that motivated this, from a live joshwcomeau.com pull.
        ["button in html with css", "html css button", "css html buttons", "buttons html css"],
        # Hyphenation is a phrasing choice, not a different query.
        ["box shadow bottom css", "box-shadow bottom css", "css box-shadow bottom"],
    ],
)
def test_restatements_share_a_key(phrasings: list[str]) -> None:
    keys = {cluster_key(phrase) for phrase in phrasings}
    assert len(keys) == 1, f"phrasings of one query produced {len(keys)} keys"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("svg file", "svg file type"),  # a narrower query, not a restatement
        ("css grid", "css flexbox"),
        ("what is svg", "what is svg format"),
    ],
)
def test_different_queries_keep_different_keys(left: str, right: str) -> None:
    assert cluster_key(left) != cluster_key(right)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("buttons", "button"),
        ("shadows", "shadow"),
        ("css", "css"),  # would become 'cs' under a naive rule
        ("class", "class"),
        ("is", "is"),
        ("svg", "svg"),
    ],
)
def test_singular_does_not_mangle_words_ending_in_s(token: str, expected: str) -> None:
    assert singular(token) == expected


def test_a_query_of_only_stopwords_keeps_its_words() -> None:
    """Stripping every token would make unrelated queries collide."""
    assert cluster_key("the and of") != cluster_key("in with at")


def test_volume_is_the_maximum_never_the_sum() -> None:
    """Variants are one query. Summing would invent demand that does not exist."""
    clusters = cluster_rows(
        [
            _row("html css button", volume=22200),
            _row("css html buttons", volume=22200),
            _row("button in html with css", volume=22200),
        ]
    )

    assert len(clusters) == 1
    assert clusters[0].volume == 22200
    assert clusters[0].variant_count == 3


def test_the_representative_is_the_best_ranking_phrasing() -> None:
    clusters = cluster_rows(
        [
            _row("button in html with css", position=15),
            _row("html css button", position=12),
            _row("css html buttons", position=13),
        ]
    )

    assert clusters[0].keyword == "html css button"
    assert clusters[0].position == 12


def test_shortest_phrasing_breaks_a_rank_tie() -> None:
    clusters = cluster_rows(
        [_row("button in css and html", position=12), _row("html css button", position=12)]
    )
    assert clusters[0].keyword == "html css button"


def test_clusters_sort_by_volume_with_unknowns_last() -> None:
    clusters = cluster_rows(
        [_row("alpha", volume=None), _row("beta", volume=500), _row("gamma", volume=9000)]
    )
    assert [c.keyword for c in clusters] == ["gamma", "beta", "alpha"]


def test_clustering_is_lossless() -> None:
    """Every input row must survive into exactly one cluster."""
    rows = [_row(k) for k in ("html css button", "css html buttons", "svg file", "css grid")]

    clusters = cluster_rows(rows)

    recovered = sorted(variant for cluster in clusters for variant in cluster.variants)
    assert recovered == sorted(row["keyword"] for row in rows)


def _seed_gap(conn: sqlite3.Connection, keyword: str, position: int, observed_at: str) -> None:
    conn.execute(
        "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
        "VALUES ('them.com', ?, ?, 'https://them.com/a', ?)",
        (keyword, position, observed_at),
    )


def test_gap_read_uses_only_the_latest_observation(db: sqlite3.Connection) -> None:
    """domain_keyword keeps rank history, so a second pull must not double rows."""
    _seed_gap(db, "css grid", 14, "2026-07-01T00:00:00Z")
    _seed_gap(db, "css grid", 9, "2026-07-08T00:00:00Z")

    rows = gap.read_rows(db, "them.com", "mine.com")

    assert len(rows) == 1, "a repeated pull listed the same keyword twice"
    assert rows[0]["position"] == 9, "the stale rank won over the current one"


def test_gap_read_excludes_keywords_you_already_rank_for(db: sqlite3.Connection) -> None:
    _seed_gap(db, "css grid", 14, "2026-07-01T00:00:00Z")
    db.execute(
        "INSERT INTO domain_keyword (domain, keyword, position, url, observed_at) "
        "VALUES ('mine.com', 'css grid', 30, 'https://mine.com/a', '2026-07-01T00:00:00Z')"
    )

    assert gap.read_rows(db, "them.com", "mine.com") == []


def test_gap_read_returns_clusters(db: sqlite3.Connection) -> None:
    for keyword in ("html css button", "css html buttons", "svg file"):
        _seed_gap(db, keyword, 12, "2026-07-01T00:00:00Z")

    clusters = gap.read(db, "them.com", "mine.com")

    assert all(isinstance(c, Cluster) for c in clusters)
    assert len(clusters) == 2
