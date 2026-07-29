"""Job queue and runner behaviour.

The property that matters most is R5: enqueuing must not spend. After that,
retries must be bounded and a crash must not cause a double purchase.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from zipf.budget import Budget
from zipf.jobs import queue
from zipf.jobs.runner import MAX_JOBS_PER_RUN, JobRunner
from zipf.sources.autocomplete import CAPABILITY, ENDPOINT

SUGGEST_BODY = json.dumps(["crm", ["best crm", "free crm"], [], [], {}]).encode()


def _route(router: respx.MockRouter) -> respx.Route:
    return router.get(ENDPOINT).mock(return_value=httpx.Response(200, content=SUGGEST_BODY))


def test_r5_enqueue_spends_nothing(db: sqlite3.Connection) -> None:
    """A command enqueues and returns. No network, no rows, no cost."""
    job_id = queue.enqueue(db, CAPABILITY, {"seed": "crm"}, estimated_cost=0.0)

    assert job_id > 0
    assert db.execute("SELECT COUNT(*) AS n FROM raw_response").fetchone()["n"] == 0
    row = db.execute("SELECT status, attempts FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == queue.QUEUED
    assert row["attempts"] == 0


def test_claim_is_exclusive(db: sqlite3.Connection) -> None:
    """Two claims must not return the same job."""
    queue.enqueue(db, CAPABILITY, {"seed": "crm"})

    first = queue.claim(db)
    second = queue.claim(db)

    assert first is not None
    assert second is None
    assert first.attempts == 1


def test_claim_returns_jobs_in_order(db: sqlite3.Connection) -> None:
    ids = [queue.enqueue(db, CAPABILITY, {"seed": f"kw{i}"}) for i in range(3)]
    claimed = [job.id for _ in ids if (job := queue.claim(db)) is not None]
    assert claimed == ids


async def test_runner_completes_a_job_and_records_cost(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    route = _route(blocked_network)
    job_id = queue.enqueue(db, CAPABILITY, {"seed": "crm"})

    report = await JobRunner(db, budget).run_once()

    assert route.call_count == 1
    assert (report.claimed, report.done, report.failed) == (1, 1, 0)
    row = db.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == queue.DONE
    assert row["actual_cost"] == 0.0
    assert row["raw_id"] is not None
    assert row["finished_at"] is not None


async def test_vendor_failure_requeues_with_backoff(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    """A retryable failure comes back queued, but not immediately claimable."""
    blocked_network.get(ENDPOINT).mock(return_value=httpx.Response(503, content=b"down"))
    job_id = queue.enqueue(db, CAPABILITY, {"seed": "crm"})

    report = await JobRunner(db, budget).run_once()

    assert (report.claimed, report.requeued, report.done) == (1, 1, 0)
    row = db.execute("SELECT status, attempts, next_attempt_at FROM job WHERE id = ?", (job_id,))
    record = row.fetchone()
    assert record["status"] == queue.QUEUED
    assert record["attempts"] == 1
    assert record["next_attempt_at"] is not None, "retry was not delayed; backoff is defeated"
    assert queue.claim(db) is None, "a backed-off job was immediately re-claimable"


async def test_retries_are_bounded(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    """After MAX_ATTEMPTS the job fails for good rather than retrying forever."""
    blocked_network.get(ENDPOINT).mock(return_value=httpx.Response(503, content=b"down"))
    job_id = queue.enqueue(db, CAPABILITY, {"seed": "crm"})
    runner = JobRunner(db, budget)

    for _ in range(queue.MAX_ATTEMPTS):
        db.execute("UPDATE job SET next_attempt_at = NULL WHERE id = ?", (job_id,))
        await runner.run_once()

    row = db.execute("SELECT status, attempts FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == queue.FAILED
    assert row["attempts"] == queue.MAX_ATTEMPTS


async def test_budget_breach_is_terminal_not_retried(
    db: sqlite3.Connection, blocked_network: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying a breached ceiling cannot succeed, so it must not be attempted."""
    from zipf import capabilities
    from zipf.pricing import PriceEstimate

    base = capabilities.get(CAPABILITY)
    monkeypatch.setitem(
        capabilities.REGISTRY,
        CAPABILITY,
        capabilities.Capability(
            name=base.name,
            tier=1,
            ttl=base.ttl,
            build=base.build,
            parse=base.parse,
            price=lambda _p: PriceEstimate(usd=99.0, tier=1, rows=1),
        ),
    )
    _route(blocked_network)
    queue.enqueue(db, CAPABILITY, {"seed": "crm"})

    report = await JobRunner(db, Budget(ceiling_usd=1.0, threshold_usd=0.25)).run_once()

    assert (report.failed, report.requeued) == (1, 0)
    row = db.execute("SELECT status, attempts FROM job").fetchone()
    assert row["status"] == queue.FAILED
    assert row["attempts"] == 1, "a terminal failure consumed more than one attempt"


def test_recover_orphans_requeues_only_unpaid_work(db: sqlite3.Connection) -> None:
    """A crash mid-fetch requeues; a crash holding a vendor task does not.

    The second case already spent the money and the vendor still holds the
    result, so requeueing would buy the same rows twice.
    """
    unpaid = queue.enqueue(db, CAPABILITY, {"seed": "a"})
    paid = queue.enqueue(db, CAPABILITY, {"seed": "b"})
    db.execute("UPDATE job SET status = ? WHERE id = ?", (queue.RUNNING, unpaid))
    db.execute(
        "UPDATE job SET status = ?, vendor_task_id = 'vendor-123' WHERE id = ?",
        (queue.RUNNING, paid),
    )

    recovered = queue.recover_orphans(db)

    assert recovered == 1
    statuses = {
        row["id"]: row["status"] for row in db.execute("SELECT id, status FROM job").fetchall()
    }
    assert statuses[unpaid] == queue.QUEUED
    assert statuses[paid] == queue.RUNNING


def test_cancel_only_affects_queued_jobs(db: sqlite3.Connection) -> None:
    job_id = queue.enqueue(db, CAPABILITY, {"seed": "crm"})
    assert queue.cancel(db, job_id) is True

    running = queue.enqueue(db, CAPABILITY, {"seed": "other"})
    db.execute("UPDATE job SET status = ? WHERE id = ?", (queue.RUNNING, running))
    assert queue.cancel(db, running) is False


async def test_run_once_is_bounded(
    db: sqlite3.Connection, budget: Budget, blocked_network: respx.MockRouter
) -> None:
    """One drain claims at most MAX_JOBS_PER_RUN, however deep the queue is."""
    _route(blocked_network)
    for i in range(MAX_JOBS_PER_RUN + 5):
        queue.enqueue(db, CAPABILITY, {"seed": f"kw{i}"})

    report = await JobRunner(db, budget).run_once()

    assert report.claimed == MAX_JOBS_PER_RUN
    assert queue.pending_count(db) == 5
