"""The job runner.

One class, two hosts (spec D1). The TUI mounts it as a background task; before
the TUI exists, ``zipf jobs run`` hosts the same class in the foreground. There
is no daemon and no second writer.

The runner is the only thing that calls ``fetch`` for a queued job, which is
what makes R5 true: commands enqueue and return, and spending happens here.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from zipf.budget import Budget
from zipf.errors import (
    BudgetExceededError,
    CapabilityUnknownError,
    CredentialMissingError,
    VendorError,
    ZipfError,
)
from zipf.fetch import fetch
from zipf.jobs import queue
from zipf.jobs.queue import Job

logger = logging.getLogger(__name__)

#: Bound on one drain. Without it a job that requeues itself could spin a single
#: ``run_once`` forever.
MAX_JOBS_PER_RUN: Final = 100

#: Failures worth retrying. Everything else is a fact about the request that a
#: second attempt cannot change.
RETRYABLE: Final = (VendorError, sqlite3.OperationalError)

#: Failures that are terminal by nature. Listed explicitly so a new error type
#: defaults to non-retryable rather than silently burning attempts.
TERMINAL: Final = (BudgetExceededError, CredentialMissingError, CapabilityUnknownError)


@dataclass(frozen=True)
class RunReport:
    claimed: int
    done: int
    requeued: int
    failed: int

    @property
    def finished(self) -> bool:
        return self.claimed == 0


class JobRunner:
    """Drains the job queue against a database and a budget."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        budget: Budget,
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._conn = conn
        self._budget = budget
        self._on_event = on_event

    def _emit(self, message: str) -> None:
        logger.info(message)
        if self._on_event is not None:
            self._on_event(message)

    async def _execute(self, job: Job) -> None:
        """Run one claimed job to a terminal state."""
        try:
            result = await fetch(
                self._conn,
                job.capability,
                job.params,
                budget=self._budget,
            )
        except TERMINAL as exc:
            queue.fail(self._conn, job, str(exc), retryable=False)
            self._emit(f"job {job.id} failed: {exc}")
            raise
        except RETRYABLE as exc:
            status = queue.fail(self._conn, job, str(exc), retryable=True)
            verb = "requeued" if status == queue.QUEUED else "failed"
            self._emit(f"job {job.id} {verb} after attempt {job.attempts}: {exc}")
            raise
        except ZipfError as exc:
            queue.fail(self._conn, job, str(exc), retryable=False)
            self._emit(f"job {job.id} failed: {exc}")
            raise

        queue.complete(self._conn, job.id, raw_id=result.raw_id, actual_cost=result.cost_usd)
        source = "cache" if result.cached else f"${result.cost_usd:.5f}"
        self._emit(f"job {job.id} done: {job.capability} · {source}")

    async def run_once(self) -> RunReport:
        """Claim and run every ready job, up to ``MAX_JOBS_PER_RUN``."""
        claimed = done = requeued = failed = 0

        for _ in range(MAX_JOBS_PER_RUN):
            job = queue.claim(self._conn)
            if job is None:
                break
            claimed += 1

            try:
                await self._execute(job)
            # _execute has already recorded the terminal state; this only
            # counts the outcome, so every exception type is expected here.
            except Exception:
                refreshed = self._conn.execute(
                    "SELECT status FROM job WHERE id = ?", (job.id,)
                ).fetchone()
                if refreshed is not None and refreshed["status"] == queue.QUEUED:
                    requeued += 1
                else:
                    failed += 1
            else:
                done += 1

        return RunReport(claimed=claimed, done=done, requeued=requeued, failed=failed)

    async def run_forever(self, poll_s: float = 2.0) -> None:
        """Drain continuously. Hosted by the TUI, or by `zipf jobs run --watch`."""
        recovered = queue.recover_orphans(self._conn)
        if recovered:
            self._emit(f"recovered {recovered} job(s) left running by an earlier crash")

        while True:
            await self.run_once()
            await asyncio.sleep(poll_s)

    async def drain(self, poll_s: float = 1.0) -> RunReport:
        """Run until the queue is empty, honouring retry backoff.

        Used by the CLI, where a run should end rather than idle. A requeued job
        is waited for; a queue with nothing ready and nothing pending stops.
        """
        recovered = queue.recover_orphans(self._conn)
        if recovered:
            self._emit(f"recovered {recovered} job(s) left running by an earlier crash")

        totals = RunReport(claimed=0, done=0, requeued=0, failed=0)
        while True:
            report = await self.run_once()
            totals = RunReport(
                claimed=totals.claimed + report.claimed,
                done=totals.done + report.done,
                requeued=totals.requeued + report.requeued,
                failed=totals.failed + report.failed,
            )
            if queue.pending_count(self._conn) == 0:
                return totals
            if report.claimed == 0:
                # Nothing ready yet, but work remains: a backoff is in flight.
                await asyncio.sleep(poll_s)
