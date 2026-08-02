"""What a spend confirmation states, computed once for both shells.

The CLI prints a panel and the TUI pushes a modal. They are two renderings of the
same four figures, and ``tui/confirm.py`` has always carried a docstring
promising that the two "cannot quote different numbers" — with nothing enforcing
it. Both computed the row-count fallback, the currency precisions, the cached
balance lookup and the can-I-afford-this comparison for themselves.

Layout deliberately stays in each shell. A printed panel indents its lines and a
modal does not; folding that in here would mean one shell rendering the other's
margins.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from zipf.budget import Budget
from zipf.pricing import PriceEstimate
from zipf.services.budget import cached_balance


@dataclass(frozen=True)
class Bill:
    """The figures a confirmation states, as values rather than as layout."""

    #: Rows the call is expected to return, or the honest admission that the
    #: count cannot be known until the response arrives.
    rows: str
    #: The estimate, at the precision the gate shows it.
    amount: str
    #: Tier and queue, which say how the call will actually run.
    terms: str
    #: What the monthly ceiling still allows.
    remaining: str
    #: The vendor balance, when one has ever been fetched.
    balance: str | None = None
    #: Whether this estimate exceeds that balance. The only figure on the readout
    #: that means "this will fail rather than merely cost", so the only one
    #: either shell renders in red.
    over_balance: bool = False


def bill_for(conn: sqlite3.Connection, budget: Budget, estimate: PriceEstimate) -> Bill:
    """Assemble the confirmation figures. Reads the cached balance; never fetches.

    The gate must stay instant and must still work with no network, so the vendor
    balance comes from the newest stored response rather than a fresh call.
    """
    balance, _age = cached_balance(conn)
    return Bill(
        rows=f"~{estimate.rows:,} rows" if estimate.rows is not None else "unknown rows",
        amount=f"${estimate.usd:.4f}",
        terms=f"tier {estimate.tier}, {estimate.queue} queue",
        remaining=f"${budget.remaining(conn):.2f}",
        balance=None if balance is None else f"${balance:.2f}",
        over_balance=balance is not None and estimate.usd > balance,
    )
