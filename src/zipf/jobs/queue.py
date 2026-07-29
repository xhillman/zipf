"""The job table as a queue.

R5: no request spends money synchronously. A command enqueues and returns; the
runner drains. Everything here is plain SQL against ``job`` — there is no broker,
because there is one operator and one database.

Claiming is atomic via ``UPDATE ... RETURNING``, so hosting two runners by
accident duplicates no work.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from zipf.clock import now, now_iso, to_iso
from zipf.db.connection import transaction

#: Attempts before a job is abandoned. Bounds retry against a vendor that is
#: down rather than merely slow.
MAX_ATTEMPTS: Final = 3

#: Backoff before each retry, indexed by attempts already made.
BACKOFF: Final = (timedelta(seconds=2), timedelta(seconds=4), timedelta(seconds=8))

QUEUED: Final = "queued"
RUNNING: Final = "running"
DONE: Final = "done"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"


@dataclass(frozen=True)
class Job:
    id: int
    capability: str
    params: dict[str, Any]
    status: str
    attempts: int
    estimated_cost: float | None
    actual_cost: float | None
    error: str | None
    vendor_task_id: str | None
    raw_id: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            capability=row["capability"],
            params=json.loads(row["params_json"]),
            status=row["status"],
            attempts=row["attempts"],
            estimated_cost=row["estimated_cost"],
            actual_cost=row["actual_cost"],
            error=row["error"],
            vendor_task_id=row["vendor_task_id"],
            raw_id=row["raw_id"],
        )


def enqueue(
    conn: sqlite3.Connection,
    capability: str,
    params: Mapping[str, Any],
    *,
    estimated_cost: float | None = None,
) -> int:
    """Queue a job and return its id. Spends nothing."""
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO job (capability, params_json, estimated_cost, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                capability,
                json.dumps(dict(params), sort_keys=True, separators=(",", ":"), default=str),
                estimated_cost,
                QUEUED,
                now_iso(),
            ),
        )
        job_id = cursor.lastrowid

    if job_id is None:  # pragma: no cover - sqlite always reports a rowid here
        raise RuntimeError("job insert did not return a row id")
    return job_id


def claim(conn: sqlite3.Connection) -> Job | None:
    """Atomically take the oldest ready job, or return None.

    A job is ready when it is queued and its backoff has elapsed. The whole
    select-and-mark is one statement, so two runners cannot claim the same row.
    """
    with transaction(conn):
        row = conn.execute(
            "UPDATE job SET status = ?, attempts = attempts + 1 "
            "WHERE id = ("
            "  SELECT id FROM job "
            "  WHERE status = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "  ORDER BY id LIMIT 1"
            ") RETURNING *",
            (RUNNING, QUEUED, now_iso()),
        ).fetchone()

    return Job.from_row(row) if row is not None else None


def complete(
    conn: sqlite3.Connection, job_id: int, *, raw_id: int | None, actual_cost: float
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE job SET status = ?, raw_id = ?, actual_cost = ?, finished_at = ?, error = NULL "
            "WHERE id = ?",
            (DONE, raw_id, actual_cost, now_iso(), job_id),
        )


def fail(conn: sqlite3.Connection, job: Job, error: str, *, retryable: bool) -> str:
    """Record a failure. Returns the status the job ended in.

    A retryable failure with attempts remaining goes back to ``queued`` with a
    backoff. Everything else is terminal: retrying a missing credential or a
    breached ceiling wastes time and, for some vendors, money.
    """
    if retryable and job.attempts < MAX_ATTEMPTS:
        delay = BACKOFF[min(job.attempts - 1, len(BACKOFF) - 1)]
        with transaction(conn):
            conn.execute(
                "UPDATE job SET status = ?, error = ?, next_attempt_at = ? WHERE id = ?",
                (QUEUED, error, to_iso(now() + delay), job.id),
            )
        return QUEUED

    with transaction(conn):
        conn.execute(
            "UPDATE job SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (FAILED, error, now_iso(), job.id),
        )
    return FAILED


def recover_orphans(conn: sqlite3.Connection) -> int:
    """Requeue jobs left ``running`` by a crash. Returns how many.

    Jobs holding a ``vendor_task_id`` are left alone: the money was already
    spent and the vendor is still holding the result, so requeueing would pay
    twice for the same rows.
    """
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE job SET status = ?, next_attempt_at = NULL "
            "WHERE status = ? AND vendor_task_id IS NULL",
            (QUEUED, RUNNING),
        )
    return cursor.rowcount


def cancel(conn: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a job that has not started. Returns whether anything changed."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE job SET status = ?, finished_at = ? WHERE id = ? AND status = ?",
            (CANCELLED, now_iso(), job_id, QUEUED),
        )
    return cursor.rowcount > 0


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, capability, status, attempts, estimated_cost, actual_cost, "
        "error, created_at, finished_at FROM job ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM job WHERE status IN (?, ?)", (QUEUED, RUNNING)
    ).fetchone()
    return int(row["n"])
