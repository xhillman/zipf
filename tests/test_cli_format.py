"""Output formatting.

These produce strings a person reads. "50 distinct querys" shipped once; the
pluralisation cases below are the ones the effect lines actually use.
"""

from __future__ import annotations

import sqlite3

import pytest

from zipf.cli.format import ABSENT, money, number, plural
from zipf.projections.rebuild import count_rows, rebuild


@pytest.mark.parametrize(
    ("count", "noun", "expected"),
    [
        (1, "keyword", "1 keyword"),
        (0, "keyword", "0 keywords"),
        (3, "keyword", "3 keywords"),
        (1_500, "keyword", "1,500 keywords"),
        # Consonant plus y: the case that shipped wrong.
        (50, "distinct query", "50 distinct queries"),
        (1, "distinct query", "1 distinct query"),
        (2, "entry", "2 entries"),
        # Vowel plus y takes a plain s.
        (2, "day", "2 days"),
        (3, "seed", "3 seeds"),
        (2, "stored response", "2 stored responses"),
    ],
)
def test_plural_matches_the_nouns_the_output_uses(count: int, noun: str, expected: str) -> None:
    assert plural(count, noun) == expected


def test_a_single_letter_noun_does_not_index_out_of_range() -> None:
    """Guards the noun[-2] lookup in the consonant-y branch."""
    assert plural(2, "y") == "2 ys"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1234, "1,234"), (0, "0"), (None, ABSENT)],
)
def test_number_marks_absence_rather_than_printing_zero(value: float | None, expected: str) -> None:
    assert number(value) == expected


def test_number_takes_a_format() -> None:
    assert number(0.125, ".2f") == "0.12"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(41.92, "$41.92"), (0.0, "$0.00"), (None, ABSENT)],
)
def test_money_distinguishes_free_from_unknown(value: float | None, expected: str) -> None:
    """A cpc of zero is a measurement; a missing cpc is not."""
    assert money(value) == expected


def test_rebuild_reports_before_and_after_per_table(db: sqlite3.Connection) -> None:
    """The effect line depends on these, so an empty dict would print nothing."""
    stats = rebuild(db)

    assert set(stats.before) == set(stats.tables_cleared)
    assert set(stats.after) == set(stats.tables_cleared)
    assert stats.lost_rows == {}


def test_rebuild_net_change_is_zero_when_nothing_changed(db: sqlite3.Connection) -> None:
    import json

    db.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES ('autocomplete.suggest', 'h', '{}', ?, 0.0, '2026-07-01T00:00:00Z')",
        (json.dumps(["s", ["alpha", "beta"], [], [], {}]).encode(),),
    )
    rebuild(db)
    assert count_rows(db, "keyword") == 2

    stats = rebuild(db)

    assert stats.net_change["keyword"] == 0
    assert stats.lost_rows == {}


def test_rebuild_flags_a_table_that_came_back_smaller(db: sqlite3.Connection) -> None:
    """A rebuild that loses rows is the failure worth surfacing loudly."""
    from zipf.projections.rebuild import RebuildStats

    stats = RebuildStats(
        capabilities=("x",),
        tables_cleared=("keyword",),
        rows_replayed=1,
        rows_written=0,
        before={"keyword": 10},
        after={"keyword": 4},
    )

    assert stats.lost_rows == {"keyword": -6}
