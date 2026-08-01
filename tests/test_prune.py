"""Pruning free, superseded responses.

Every test here is about something the prune must *not* remove. Getting the
removal right is one SQL statement; getting the exclusions wrong destroys data
nobody can restore, so that is where the coverage goes.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from zipf import prune as prune_cache
from zipf.clock import now, to_iso
from zipf.projections.rebuild import count_rows, project
from zipf.sources import autocomplete
from zipf.sources.dataforseo import account, labs


def _store(
    conn: sqlite3.Connection,
    capability: str,
    *,
    age_days: float = 0.0,
    cost: float = 0.0,
    body: bytes = b"{}",
    params_hash: str = "h",
) -> int:
    fetched_at = to_iso(now() - timedelta(days=age_days))
    cursor = conn.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES (?, ?, '{}', ?, ?, ?)",
        (capability, params_hash, body, cost, fetched_at),
    )
    return int(cursor.lastrowid or 0)


def test_a_stale_superseded_balance_lookup_is_removed(db: sqlite3.Connection) -> None:
    """The case this exists for: 700 KB of price list, read for one float."""
    _store(db, account.CAPABILITY, age_days=2, body=b"x" * 5000)
    _store(db, account.CAPABILITY, age_days=1, body=b"x" * 5000)
    newest = _store(db, account.CAPABILITY, age_days=0.5, body=b"x" * 5000)

    stats = prune_cache.prune(db)

    assert stats.rows == 2
    assert stats.bytes == 10_000
    remaining = db.execute("SELECT id FROM raw_response").fetchall()
    assert [row["id"] for row in remaining] == [newest], "the newest answer must survive"


def test_the_newest_response_always_survives(db: sqlite3.Connection) -> None:
    """``cached_balance`` reads it regardless of age, and must work offline.

    A prune that removed the last known balance would blank the vendor line on
    the confirmation gate until the next network call.
    """
    _store(db, account.CAPABILITY, age_days=90)

    assert prune_cache.prune(db).rows == 0
    assert count_rows(db, "raw_response") == 1


def test_a_fresh_response_survives_even_when_superseded(db: sqlite3.Connection) -> None:
    """Inside the TTL it is still a valid cache hit for its own params."""
    _store(db, account.CAPABILITY, age_days=0.001, params_hash="a")
    _store(db, account.CAPABILITY, age_days=0.0, params_hash="b")

    assert prune_cache.prune(db).rows == 0


def test_a_projected_capability_is_never_pruned(db: sqlite3.Connection) -> None:
    """The exclusion that matters most.

    Autocomplete is free and its responses go stale, but ``keyword`` rows are
    derived from these bytes. Deleting them would make a rebuild silently lose
    every keyword they discovered.
    """
    body = b'["crm", ["crm software", "crm pricing"], [], [], {}]'
    older = _store(db, autocomplete.CAPABILITY, age_days=400, body=body)
    _store(db, autocomplete.CAPABILITY, age_days=1, body=body, params_hash="h2")
    project(db, older)
    discovered = count_rows(db, "keyword")

    assert prune_cache.prune(db).rows == 0
    assert count_rows(db, "raw_response") == 2
    assert count_rows(db, "keyword") == discovered


def test_autocomplete_is_not_offered_as_prunable() -> None:
    """The selector is "nothing reads it", not "it was free"."""
    prunable = prune_cache.prunable_capabilities()

    assert account.CAPABILITY in prunable
    assert autocomplete.CAPABILITY not in prunable
    assert labs.SEARCH_VOLUME not in prunable


def test_a_paid_response_is_refused_by_the_database(db: sqlite3.Connection) -> None:
    """Defence in depth: the engine stops this even if the query were wrong."""
    _store(db, labs.SEARCH_VOLUME, age_days=400, cost=0.012)
    _store(db, labs.SEARCH_VOLUME, age_days=1, cost=0.012, params_hash="h2")

    with pytest.raises(sqlite3.IntegrityError) as caught:
        db.execute("DELETE FROM raw_response WHERE capability = ?", (labs.SEARCH_VOLUME,))

    assert "cost money cannot be deleted" in str(caught.value)
    assert count_rows(db, "raw_response") == 2


def test_preview_describes_exactly_what_prune_removes(db: sqlite3.Connection) -> None:
    """A readout that overstates what went is worse than no readout."""
    _store(db, account.CAPABILITY, age_days=3, body=b"y" * 900)
    _store(db, account.CAPABILITY, age_days=2, body=b"y" * 900)
    _store(db, account.CAPABILITY, age_days=0.0)

    planned = prune_cache.preview(db)
    assert count_rows(db, "raw_response") == 3, "preview must not remove anything"

    removed = prune_cache.prune(db)
    assert (planned.rows, planned.bytes) == (removed.rows, removed.bytes)
    assert planned.by_capability == {account.CAPABILITY: 2}


def test_pruning_an_empty_database_reports_nothing(db: sqlite3.Connection) -> None:
    stats = prune_cache.prune(db)

    assert stats.is_empty
    assert stats.by_capability == {}
