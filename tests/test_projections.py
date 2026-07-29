"""Projection and rebuild behaviour.

R3 is the load-bearing one: if a number is wrong, the fix is a rebuild. That is
only true if rebuild is deterministic and never destroys data it cannot restore.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from zipf.projections.rebuild import PROJECTORS, rebuild
from zipf.sources import autocomplete, gsc


def _insert_raw(
    conn: sqlite3.Connection, capability: str, body: bytes, fetched_at: str, params_hash: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO raw_response "
        "(capability, params_hash, params_json, body, cost_usd, fetched_at) "
        "VALUES (?, ?, '{}', ?, 0.0, ?)",
        (capability, params_hash, body, fetched_at),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _suggest_body(terms: list[str]) -> bytes:
    return json.dumps(["seed", terms, [], [], {}]).encode()


def _gsc_body(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "rows": [
                {
                    "keys": [r["query"], r["page"], r["date"]],
                    "clicks": r["clicks"],
                    "impressions": r["impressions"],
                    "ctr": r["ctr"],
                    "position": r["position"],
                }
                for r in rows
            ]
        }
    ).encode()


def _table_digest(conn: sqlite3.Connection, table: str) -> str:
    """Order-independent fingerprint of a table's contents."""
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    rendered = sorted(repr(tuple(row)) for row in rows)
    return hashlib.sha256("\n".join(rendered).encode()).hexdigest()


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    _insert_raw(
        db,
        autocomplete.CAPABILITY,
        _suggest_body(["best crm", "crm for freelancers"]),
        "2026-07-01T00:00:00Z",
        "h1",
    )
    _insert_raw(
        db,
        autocomplete.CAPABILITY,
        _suggest_body(["free crm", "best crm"]),  # deliberate overlap
        "2026-07-02T00:00:00Z",
        "h2",
    )
    _insert_raw(
        db,
        gsc.CAPABILITY,
        _gsc_body(
            [
                {
                    "query": "best crm",
                    "page": "https://example.com/crm",
                    "date": "2026-07-01",
                    "clicks": 3,
                    "impressions": 100,
                    "ctr": 0.03,
                    "position": 8.4,
                }
            ]
        ),
        "2026-07-03T00:00:00Z",
        "h3",
    )
    return db


def test_r3_rebuild_is_deterministic(seeded: sqlite3.Connection) -> None:
    """R3: rebuilding twice must produce identical tables."""
    rebuild(seeded)
    first = {t: _table_digest(seeded, t) for t in ("keyword", "gsc_query")}

    rebuild(seeded)
    second = {t: _table_digest(seeded, t) for t in ("keyword", "gsc_query")}

    assert first == second


def test_rebuild_never_touches_raw_response(seeded: sqlite3.Connection) -> None:
    before = _table_digest(seeded, "raw_response")
    rebuild(seeded)
    assert _table_digest(seeded, "raw_response") == before


def test_rebuild_reconstructs_from_bytes_alone(seeded: sqlite3.Connection) -> None:
    """Projections are disposable; the bytes are the source of truth."""
    rebuild(seeded)
    expected_keywords = {r["keyword"] for r in seeded.execute("SELECT keyword FROM keyword")}
    assert expected_keywords == {"best crm", "crm for freelancers", "free crm"}

    seeded.execute("DELETE FROM keyword")
    seeded.execute("DELETE FROM gsc_query")

    stats = rebuild(seeded)
    recovered = {r["keyword"] for r in seeded.execute("SELECT keyword FROM keyword")}
    assert recovered == expected_keywords
    assert stats.rows_replayed == 3


def test_gsc_position_stays_out_of_observation(seeded: sqlite3.Connection) -> None:
    """D3: averaged positions must never enter observation.position."""
    rebuild(seeded)

    gsc_rows = seeded.execute("SELECT position FROM gsc_query").fetchall()
    assert [row["position"] for row in gsc_rows] == [8.4]

    observations = seeded.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"]
    assert observations == 0


def test_targeted_rebuild_replays_everything_sharing_a_table(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A targeted rebuild must not delete rows another capability owns.

    ``keyword`` is written by autocomplete now and by Labs later. Clearing it for
    one capability while replaying only that capability would silently drop the
    other's rows.
    """
    from zipf.projections.base import Projector

    def apply_other(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
        conn.execute(
            "INSERT INTO keyword (keyword, volume, updated_at, raw_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (keyword) DO UPDATE SET volume = excluded.volume",
            ("paid term", 8100, row["fetched_at"], row["id"]),
        )
        return 1

    monkeypatch.setitem(
        PROJECTORS,
        "labs.search_volume",
        Projector(capability="labs.search_volume", tables=("keyword",), apply=apply_other),
    )
    _insert_raw(seeded, "labs.search_volume", b"{}", "2026-07-04T00:00:00Z", "h4")

    rebuild(seeded, capability=autocomplete.CAPABILITY)

    keywords = {r["keyword"] for r in seeded.execute("SELECT keyword FROM keyword")}
    assert "paid term" in keywords, "targeted rebuild dropped another capability's rows"
    assert "best crm" in keywords


def test_free_discovery_does_not_clobber_a_paid_volume(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autocomplete replaying after Labs must not erase the volume Labs paid for.

    This is what ``ON CONFLICT DO NOTHING`` in the autocomplete projector buys.
    Replay is ordered by ``fetched_at``, so the paid row is written first and the
    free discovery of the same term comes second.
    """
    from zipf.projections.base import Projector

    def apply_volume(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
        conn.execute(
            "INSERT INTO keyword (keyword, volume, updated_at, raw_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (keyword) DO UPDATE SET volume = excluded.volume",
            ("free crm", 9900, row["fetched_at"], row["id"]),
        )
        return 1

    monkeypatch.setitem(
        PROJECTORS,
        "labs.search_volume",
        Projector(capability="labs.search_volume", tables=("keyword",), apply=apply_volume),
    )
    # Earlier than the 2026-07-02 autocomplete row that also yields "free crm".
    _insert_raw(seeded, "labs.search_volume", b"{}", "2026-07-01T12:00:00Z", "h5")

    rebuild(seeded)

    row = seeded.execute("SELECT volume FROM keyword WHERE keyword = 'free crm'").fetchone()
    assert row["volume"] == 9900, "free discovery overwrote a paid volume"
