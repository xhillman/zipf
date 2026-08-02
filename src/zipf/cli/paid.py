"""The decision every paid command takes, and the gate that guards it.

Two of the PRD's design principles live here rather than in the services:

- **Paid actions are typed, not clicked.** Confirmation is a deliberate keypress
  above the threshold, and the price is always stated before it.
- **Default to silence.** Below the threshold nothing is asked and nothing is
  printed beyond the price line.

Nothing here spends. It decides whether to proceed to an enqueue, and the runner
is the only thing that reaches a vendor (R5).

``resolve`` exists because ``vol``, ``gap``, ``ideas`` and ``ranks`` were four
copies of one five-way branch. The branch is the same every time — already
owned, ``--cached``, ``--dry-run``, approved, declined — while every message
inside it differs, so the structure moved here and the wording stayed at the call
site where a reader can see which command says what.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.panel import Panel

from zipf import confirmation
from zipf.budget import Budget
from zipf.pricing import Plan, PriceEstimate

console = Console()


@dataclass(frozen=True)
class Purchase:
    """One priced request, and what to say at each branch of the decision.

    The messages are built by the caller rather than assembled from parts here.
    Every one of them names a different thing — keywords, a domain pair, a seed
    count — and a formatter general enough to write all four would be harder to
    read than the four strings it replaced.
    """

    plan: Plan
    #: Names the purchase in the confirmation panel's title.
    what: str
    #: Printed when the request is already answered by stored data.
    owned: str
    #: Printed when ``--cached`` refused to buy what is missing.
    skipped: str
    #: Printed when ``--dry-run`` priced the request without buying it.
    priced: str
    #: Queues the work and describes what was queued. Called only after the gate
    #: has said yes, so a command cannot enqueue by taking the wrong branch.
    queue_work: Callable[[sqlite3.Connection], str]
    #: Extra context under the confirmation title, where the request needs it.
    detail: str = ""


def resolve(
    conn: sqlite3.Connection,
    budget: Budget,
    purchase: Purchase,
    *,
    dry_run: bool = False,
    cached: bool = False,
    yes: bool = False,
) -> bool:
    """Take the buy-or-not decision and print its outcome. Returns whether work was queued.

    Order is load-bearing and matches the CLI's promise: what you already own is
    read without a prompt, then the two flags that refuse to spend, and only then
    the gate. Checking the gate earlier would price a request that was never
    going to be bought.

    ``--wait`` is deliberately not handled here. Draining the queue is what the
    command does *after* deciding, and folding it in would make this function
    both decide and execute.
    """
    if purchase.plan.is_free:
        console.print(purchase.owned)
        return False
    if cached:
        console.print(purchase.skipped)
        return False
    if dry_run:
        console.print(purchase.priced)
        return False

    approved = confirm_spend(
        conn,
        budget,
        purchase.plan.estimate,
        what=purchase.what,
        detail=purchase.detail,
        assume_yes=yes,
    )
    if not approved:
        console.print("[yellow]cancelled[/] nothing queued, nothing spent")
        return False

    console.print(purchase.queue_work(conn))
    return True


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

    if not budget.needs_confirmation(estimate):
        remaining = budget.remaining(conn)
        console.print(f"[dim]{what} · ${estimate.usd:.5f} · ${remaining:.2f} left this month[/]")
        return True

    # Every figure below comes from `confirmation`, so this panel and the TUI's
    # modal cannot quote different numbers. Only the margins are decided here.
    bill = confirmation.bill_for(conn, budget, estimate)
    body = (
        f"  {bill.rows:<22} not cached\n"
        f"  [bold]{bill.amount}[/]{'':<15} {bill.terms}\n"
        f"  remaining this month: {bill.remaining}"
    )
    if bill.balance is not None:
        marker, close = ("[red]", "[/]") if bill.over_balance else ("", "")
        body += f"\n  vendor balance:       {marker}{bill.balance}{close}"
    if detail:
        body = f"  {detail}\n{body}"

    console.print(Panel(body, title=f"pull {what}", title_align="left", expand=False))

    if assume_yes:
        console.print("[dim]--yes given, proceeding[/]")
        return True
    return typer.confirm("pull", default=False)
