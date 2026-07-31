"""Projection and rebuild.

Two callers, one parser. ``project`` runs on the hot path after each fetch;
``rebuild`` replays the whole archive. Both go through the same projector, so a
parser fix applies identically to new data and to everything already bought.

Rebuild is the fix for a wrong number. There is never an ``UPDATE`` (R3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from zipf.db.connection import transaction
from zipf.errors import InvalidRequestError
from zipf.projections import domain_keyword, gsc_query, keyword, keyword_month
from zipf.projections.base import Projector

PROJECTORS: dict[str, Projector] = {
    keyword.AUTOCOMPLETE.capability: keyword.AUTOCOMPLETE,
    gsc_query.SEARCH_ANALYTICS.capability: gsc_query.SEARCH_ANALYTICS,
    keyword.SEARCH_VOLUME.capability: keyword.SEARCH_VOLUME,
    keyword.BULK_KEYWORD_DIFFICULTY.capability: keyword.BULK_KEYWORD_DIFFICULTY,
    keyword.SEARCH_INTENT.capability: keyword.SEARCH_INTENT,
    keyword_month.KEYWORDS_FOR_KEYWORDS.capability: keyword_month.KEYWORDS_FOR_KEYWORDS,
    domain_keyword.RANKED_KEYWORDS.capability: domain_keyword.RANKED_KEYWORDS,
    domain_keyword.DOMAIN_INTERSECTION.capability: domain_keyword.DOMAIN_INTERSECTION,
}

# params_json is selected because some projections cannot be derived from the
# body alone: a domain intersection response lists positions without naming
# which domain each belongs to.
_COLUMNS = "id, capability, body, fetched_at, params_json"

_SELECT_ONE = f"SELECT {_COLUMNS} FROM raw_response WHERE id = ?"

# Deterministic replay order. fetched_at alone is not enough: two rows can share
# a second, and a rebuild that reorders them is a rebuild that can disagree with
# itself. id breaks the tie, and id is monotonic with insertion.
_REPLAY_ORDER = "ORDER BY fetched_at ASC, id ASC"


@dataclass(frozen=True)
class RebuildStats:
    capabilities: tuple[str, ...]
    tables_cleared: tuple[str, ...]
    rows_replayed: int
    rows_written: int
    #: Row counts per table before and after, so a caller can show the effect
    #: rather than asserting one. A rebuild that silently loses rows is the
    #: failure worth seeing, and it is invisible in a total.
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    @property
    def net_change(self) -> dict[str, int]:
        return {table: self.after[table] - self.before.get(table, 0) for table in self.after}

    @property
    def lost_rows(self) -> dict[str, int]:
        """Tables that came back smaller. Should always be empty."""
        return {table: delta for table, delta in self.net_change.items() if delta < 0}


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Row count for one projection table.

    The table name is interpolated because it comes from the projector registry,
    never from input, and SQLite cannot parameterise an identifier.
    """
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def project(conn: sqlite3.Connection, raw_id: int) -> int:
    """Derive projection rows from one stored response.

    Returns the number of source records projected. A capability with no
    projector is a no-op: the bytes are cached and can be projected later once a
    projector exists, which is the whole point of caching at the HTTP boundary.
    """
    row = conn.execute(_SELECT_ONE, (raw_id,)).fetchone()
    if row is None:
        raise ValueError(f"no raw_response row with id {raw_id}")

    projector = PROJECTORS.get(row["capability"])
    if projector is None:
        return 0
    return projector.apply(conn, row)


def _closure(targets: list[Projector]) -> list[Projector]:
    """Expand a projector selection to everything sharing its tables.

    Rebuilding one capability clears whole tables. If another capability also
    writes one of those tables, it must be replayed too, or the rebuild silently
    deletes data it never restores. ``keyword`` is written by both autocomplete
    and Labs, so this is not hypothetical.
    """
    selected = {p.capability: p for p in targets}
    tables = {table for p in targets for table in p.tables}

    changed = True
    while changed:
        changed = False
        for projector in PROJECTORS.values():
            if projector.capability in selected:
                continue
            if tables.isdisjoint(projector.tables):
                continue
            selected[projector.capability] = projector
            tables.update(projector.tables)
            changed = True

    return [selected[name] for name in sorted(selected)]


def rebuild(conn: sqlite3.Connection, capability: str | None = None) -> RebuildStats:
    """Drop the affected projection tables and replay every stored response.

    ``raw_response`` is never touched. Running this twice must produce identical
    tables; that property is asserted by the R3 invariant test.
    """
    if capability is None:
        projectors = sorted(PROJECTORS.values(), key=lambda p: p.capability)
    else:
        target = PROJECTORS.get(capability)
        if target is None:
            known = ", ".join(sorted(PROJECTORS)) or "none registered"
            raise InvalidRequestError(
                f"Nothing is projected from {capability!r}.",
                fix=f"Rebuildable sources: {known}.",
            )
        projectors = _closure([target])

    names = tuple(p.capability for p in projectors)
    tables = tuple(sorted({table for p in projectors for table in p.tables}))
    placeholders = ",".join("?" for _ in names)

    replayed = 0
    written = 0
    before = {table: count_rows(conn, table) for table in tables}

    with transaction(conn):
        for table in tables:
            # Table names come from the projector registry, never from input.
            conn.execute(f"DELETE FROM {table}")

        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM raw_response "
            f"WHERE capability IN ({placeholders}) {_REPLAY_ORDER}",
            names,
        ).fetchall()

        by_capability = {p.capability: p for p in projectors}
        for row in rows:
            written += by_capability[row["capability"]].apply(conn, row)
            replayed += 1

    return RebuildStats(
        capabilities=names,
        tables_cleared=tables,
        rows_replayed=replayed,
        rows_written=written,
        before=before,
        after={table: count_rows(conn, table) for table in tables},
    )
