"""The confirmation gate for paid commands.

Two of the PRD's design principles live here rather than in the services:

- **Paid actions are typed, not clicked.** Confirmation is a deliberate keypress
  above the threshold, and the price is always stated before it.
- **Default to silence.** Below the threshold nothing is asked and nothing is
  printed beyond the price line.

The gate never spends. It decides whether to proceed to an enqueue.
"""

from __future__ import annotations

import sqlite3

import typer
from rich.console import Console
from rich.panel import Panel

from zipf.budget import Budget
from zipf.pricing import PriceEstimate
from zipf.services.budget import cached_balance

console = Console()


def confirm_spend(
    conn: sqlite3.Connection,
    budget: Budget,
    estimate: PriceEstimate,
    *,
    what: str,
    detail: str = "",
    assume_yes: bool = False,
) -> bool:
    """Show the bill and ask, when the amount warrants asking.

    Returns whether to proceed. A free plan is always allowed through: cached
    browsing and tier 0 must stay unlimited even in a month that hit the ceiling.
    """
    if estimate.is_free:
        return True

    remaining = budget.remaining(conn)
    if not budget.needs_confirmation(estimate):
        console.print(f"[dim]{what} · ${estimate.usd:.5f} · ${remaining:.2f} left this month[/]")
        return True

    rows = f"~{estimate.rows:,} rows" if estimate.rows is not None else "unknown rows"
    body = (
        f"  {rows:<22} not cached\n"
        f"  [bold]${estimate.usd:.4f}[/]{'':<15} tier {estimate.tier}, {estimate.queue} queue\n"
        f"  remaining this month: ${remaining:.2f}"
    )

    # The vendor balance is the limit that actually bites. Read from cache: the
    # gate must stay fast and must still work with no network.
    balance, _age = cached_balance(conn)
    if balance is not None:
        marker = "[red]" if estimate.usd > balance else ""
        close = "[/]" if marker else ""
        body += f"\n  vendor balance:       {marker}${balance:.2f}{close}"
    if detail:
        body = f"  {detail}\n{body}"

    console.print(Panel(body, title=f"pull {what}", title_align="left", expand=False))

    if assume_yes:
        console.print("[dim]--yes given, proceeding[/]")
        return True
    return typer.confirm("pull", default=False)
