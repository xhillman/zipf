"""Budget reporting: what you have set, what you have spent, what exists.

Three numbers, and they mean different things:

- **spent** — month-to-date, derived from ``raw_response``. Ground truth for what
  Zipf has bought.
- **ceiling** — a policy the user chose. Zipf enforces it.
- **balance** — what the vendor will actually let you spend. Zipf cannot enforce
  it, but a ceiling above it is decorative, so it is always shown.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from zipf.budget import Budget
from zipf.clock import from_iso, now
from zipf.fetch import fetch
from zipf.sources.dataforseo import account


@dataclass(frozen=True)
class BudgetStatus:
    spent: float
    ceiling: float
    remaining: float
    threshold: float
    balance: float | None
    balance_age: timedelta | None
    balance_error: str | None = None

    @property
    def ceiling_exceeds_balance(self) -> bool:
        """A ceiling above the funded balance cannot stop anything."""
        return self.balance is not None and self.ceiling > self.balance

    @property
    def effective_limit(self) -> float:
        """The smaller of what you allowed and what you actually have."""
        if self.balance is None:
            return self.remaining
        return min(self.remaining, self.balance)


def cached_balance(conn: sqlite3.Connection) -> tuple[float | None, timedelta | None]:
    """Read the last known vendor balance without touching the network.

    Used by the confirmation gate, which must stay fast and must work offline.
    """
    row = conn.execute(
        "SELECT body, fetched_at FROM raw_response WHERE capability = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (account.CAPABILITY,),
    ).fetchone()
    if row is None:
        return None, None

    try:
        parsed = account.parse(row["body"], {})
    # A stale or unreadable body must never block a purchase decision.
    except Exception:
        return None, None
    return parsed["balance"], now() - from_iso(row["fetched_at"])


async def status(conn: sqlite3.Connection, budget: Budget, *, refresh: bool = True) -> BudgetStatus:
    """Assemble the full picture, refreshing the vendor balance by default.

    ``zipf budget`` is a command the user runs deliberately to look at money, so
    accuracy beats the few hundred milliseconds. Background consumers such as the
    TUI status bar pass ``refresh=False`` and read the cached value.
    """
    spent = budget.spent_this_month(conn)
    balance: float | None
    age: timedelta | None
    error: str | None = None

    if refresh:
        try:
            result = await fetch(conn, account.CAPABILITY, {}, budget=budget, force=True)
            balance = result.parsed()["balance"]
            age = timedelta(0)
        # A balance lookup is informational; it must never break the readout.
        except Exception as exc:
            balance, age = cached_balance(conn)
            error = f"{type(exc).__name__}: {exc}"
    else:
        balance, age = cached_balance(conn)

    return BudgetStatus(
        spent=spent,
        ceiling=budget.ceiling_usd,
        remaining=budget.remaining(conn),
        threshold=budget.threshold_usd,
        balance=balance,
        balance_age=age,
        balance_error=error,
    )
