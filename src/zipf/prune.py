"""Pruning free, superseded responses.

The cache is append-only because it holds things nobody will sell back at the
price already paid. That reasoning does not cover every row. The vendor balance
lookup is free, re-fetchable in a second, and returns a price list for every
DataForSEO endpoint in order to report one number — ten of them accumulated
3.58 MB, a fifth of the database, to answer "how much have I got left".

Three conditions must all hold before a row is removed, and each rules out a
different way this could destroy something:

1. **It cost nothing.** Enforced by the storage engine as well, so a mistake here
   aborts rather than deletes. Paid bytes are still permanent.
2. **Nothing is derived from it.** A capability with a projector is never
   touched, however free: deleting an autocomplete response would make
   ``zipf db rebuild`` lose the keywords it discovered, permanently.
3. **It is not the newest answer to its question, and it is past its TTL.** The
   newest response per ``params_hash`` always survives, so the confirmation gate
   and the status bar keep reading a cached balance offline.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from zipf import capabilities
from zipf.clock import now, to_iso
from zipf.db.connection import transaction
from zipf.projections.rebuild import PROJECTORS

#: Rows that are free, superseded, past their TTL, and read by no projector.
#: Both statements below share it so a preview cannot describe a different set
#: from the one the delete removes.
_CANDIDATES = """
FROM raw_response
WHERE capability = :capability
  AND cost_usd = 0
  AND fetched_at < :cutoff
  AND id NOT IN (
    SELECT MAX(id) FROM raw_response WHERE capability = :capability GROUP BY params_hash
  )
"""

_COUNT_SQL = f"SELECT COUNT(*) AS rows, COALESCE(SUM(LENGTH(body)), 0) AS bytes {_CANDIDATES}"

_DELETE_SQL = f"DELETE {_CANDIDATES}"


@dataclass(frozen=True)
class PruneStats:
    """What was removed, or would be."""

    rows: int = 0
    bytes: int = 0
    #: Rows per capability, so the readout names what went rather than a total.
    by_capability: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.rows == 0


def prunable_capabilities() -> tuple[str, ...]:
    """Registered capabilities whose bytes no projection reads.

    Derived from the projector registry rather than listed, so a capability that
    gains a projector stops being prunable on the same commit. Listing them would
    be a second place to remember, and forgetting would cost stored data.
    """
    return tuple(sorted(name for name in capabilities.REGISTRY if name not in PROJECTORS))


def _cutoff(capability: str) -> str:
    """The moment before which a response of this capability is stale."""
    return to_iso(now() - capabilities.get(capability).ttl)


def preview(conn: sqlite3.Connection) -> PruneStats:
    """What ``prune`` would remove. Reads only, and works on a read-only handle."""
    rows = 0
    total_bytes = 0
    by_capability: dict[str, int] = {}

    for capability in prunable_capabilities():
        row = conn.execute(
            _COUNT_SQL, {"capability": capability, "cutoff": _cutoff(capability)}
        ).fetchone()
        if row["rows"]:
            by_capability[capability] = int(row["rows"])
            rows += int(row["rows"])
            total_bytes += int(row["bytes"])

    return PruneStats(rows=rows, bytes=total_bytes, by_capability=by_capability)


def prune(conn: sqlite3.Connection) -> PruneStats:
    """Remove free, superseded responses. Returns what went.

    Measured before deleting, because the byte count cannot be recovered
    afterwards. The whole sweep is one transaction: a partial prune that reported
    a total it had not achieved would be worse than none.
    """
    planned = preview(conn)
    if planned.is_empty:
        return planned

    with transaction(conn):
        for capability in planned.by_capability:
            conn.execute(_DELETE_SQL, {"capability": capability, "cutoff": _cutoff(capability)})

    return planned


def reclaim(conn: sqlite3.Connection, path: Path) -> int:
    """VACUUM, returning bytes the file actually gave back.

    Deleting rows leaves the pages allocated, so without this the command reports
    freeing megabytes while the file on disk does not move — which reads as a
    command that did nothing. Runs outside a transaction because SQLite refuses
    to vacuum inside one.
    """
    before = path.stat().st_size
    conn.execute("VACUUM")
    return before - path.stat().st_size
