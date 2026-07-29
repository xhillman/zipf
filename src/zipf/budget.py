"""Budget accounting.

Month-to-date spend is derived by summing ``raw_response.cost_usd``. There is no
ledger table on purpose: a second copy of a spend record can disagree with the
first, and it would disagree in the direction that costs money. The rows that
were paid for are the rows that count themselves.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from zipf.clock import month_start_iso
from zipf.errors import BudgetExceededError
from zipf.pricing import PriceEstimate

_SPEND_SQL = """
SELECT COALESCE(SUM(cost_usd), 0.0) AS spent
FROM raw_response
WHERE fetched_at >= ?
"""


@dataclass(frozen=True)
class Budget:
    """The monthly ceiling and the confirmation threshold."""

    ceiling_usd: float
    threshold_usd: float

    def spent_this_month(self, conn: sqlite3.Connection) -> float:
        row = conn.execute(_SPEND_SQL, (month_start_iso(),)).fetchone()
        return float(row["spent"])

    def remaining(self, conn: sqlite3.Connection) -> float:
        return max(0.0, self.ceiling_usd - self.spent_this_month(conn))

    def needs_confirmation(self, estimate: PriceEstimate) -> bool:
        return estimate.usd > self.threshold_usd

    def assert_within(self, conn: sqlite3.Connection, estimate: PriceEstimate) -> None:
        """Fail the call if it would breach the ceiling.

        Free calls are never gated. Cached browsing and the tier-0 sources stay
        usable even in a month where the ceiling has already been reached.
        """
        if estimate.is_free:
            return

        spent = self.spent_this_month(conn)
        if spent + estimate.usd > self.ceiling_usd:
            raise BudgetExceededError(estimate=estimate.usd, spent=spent, ceiling=self.ceiling_usd)
