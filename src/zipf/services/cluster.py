"""Collapse restatements of one query into a single row.

A real 100-row gap pull returned fifteen rows reading ``button in html with
css``, ``html css button``, ``css html buttons``, and so on — all at 22,200
volume, all ranking the same URL. They are one opportunity written fifteen ways,
and presenting them as fifteen makes a keyword list look four times richer than
it is.

The signal is the **token set**. Two keywords built from the same words, ignoring
order, filler words, and plurals, are the same query. Google agrees: it serves
them the same SERP and reports them the same volume.

This is presentation, not projection. It runs at read time over rows already
stored, so it costs nothing, changes no schema, and can be re-tuned later without
re-buying anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Words that carry no topical meaning in a search query. Removing them makes
#: "button in html with css" and "html css button" identical.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)

_SPLIT = re.compile(r"[^a-z0-9]+")


def singular(token: str) -> str:
    """Strip a plural 's' without mangling words that legitimately end in one.

    Deliberately naive. ``buttons`` -> ``button`` is the case that matters;
    ``css`` and ``class`` must survive untouched, which the ``ss`` guard handles.
    A real stemmer would collapse more, and would also collapse things a reader
    would not expect.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def cluster_key(keyword: str) -> tuple[str, ...]:
    """The identity of a query, independent of how it was phrased."""
    tokens = (singular(token) for token in _SPLIT.split(keyword.lower()) if token)
    meaningful = sorted({token for token in tokens if token not in STOPWORDS})
    # A query made entirely of stopwords keeps its words rather than becoming
    # indistinguishable from every other such query.
    return tuple(meaningful) if meaningful else tuple(sorted(_SPLIT.split(keyword.lower())))


@dataclass(frozen=True)
class Cluster:
    """One query, and every phrasing of it that was found."""

    keyword: str
    variants: list[str]
    volume: int | None
    position: int | None
    url: str | None
    rows: list[dict[str, Any]]

    @property
    def variant_count(self) -> int:
        return len(self.variants)

    @property
    def has_variants(self) -> bool:
        return len(self.variants) > 1


def _representative(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pick the phrasing to show: best rank first, then the plainest wording.

    Shortest-wins as the tie-break tends to select the canonical form —
    "html css button" over "button in css and html".
    """
    return min(
        rows,
        key=lambda row: (
            row["position"] if row.get("position") is not None else 10**6,
            len(row["keyword"]),
            row["keyword"],
        ),
    )


def cluster_rows(rows: Iterable[dict[str, Any]]) -> list[Cluster]:
    """Group rows by query identity, preserving the incoming order of first sight.

    Volume is taken as the maximum across variants, never the sum. They are the
    same query, so adding them would multiply one opportunity into a fictitious
    several.
    """
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(cluster_key(row["keyword"]), []).append(row)

    clusters: list[Cluster] = []
    for members in grouped.values():
        best = _representative(members)
        volumes = [row["volume"] for row in members if row.get("volume") is not None]
        positions = [row["position"] for row in members if row.get("position") is not None]
        clusters.append(
            Cluster(
                keyword=best["keyword"],
                variants=[row["keyword"] for row in members],
                volume=max(volumes) if volumes else None,
                position=min(positions) if positions else None,
                url=best.get("url"),
                rows=members,
            )
        )

    clusters.sort(
        key=lambda c: (
            0 if c.volume is not None else 1,
            -(c.volume or 0),
            c.position if c.position is not None else 10**6,
        )
    )
    return clusters
