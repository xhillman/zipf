"""The modal that stands between a typed command and a charge.

The only screen in the app that uses the reserved spend colour. That is the whole
point of reserving it: by the time this is on screen you should already know what
it is about without reading a word.

It states the same facts as the CLI's confirmation and reads them from the same
places — ``PriceEstimate``, ``Budget.remaining``, and the cached vendor balance —
so the two shells cannot quote different numbers. The layouts differ because one
is a printed panel and the other is a modal, which is a rendering choice rather
than a disagreement about the bill.
"""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from zipf import confirmation
from zipf.budget import Budget
from zipf.pricing import PriceEstimate


class ConfirmModal(ModalScreen[bool]):
    """Asks before spending. Returns whether to proceed; never spends itself."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "approve", "pull"),
        Binding("escape", "decline", "cancel"),
    ]

    def __init__(
        self,
        estimate: PriceEstimate,
        *,
        conn: sqlite3.Connection,
        budget: Budget,
        what: str,
        detail: str = "",
    ) -> None:
        super().__init__()
        self._estimate = estimate
        self._conn = conn
        self._budget = budget
        self._what = what
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(f"pull {self._what}", id="confirm-title")
            yield Static(self._body(), id="confirm-body")
            yield Static("[bold]enter[/] pull    [bold]esc[/] cancel", id="confirm-keys")

    def _body(self) -> str:
        """The bill, laid out for a modal. Every figure comes from ``confirmation``."""
        bill = confirmation.bill_for(self._conn, self._budget, self._estimate)
        lines = []
        if self._detail:
            lines.append(f"[dim]{self._detail}[/]")
        lines.append(f"{bill.rows:<22} not cached")
        lines.append(f"[bold]{bill.amount}[/]{'':<14} {bill.terms}")
        lines.append(f"remaining this month: {bill.remaining}")
        if bill.balance is not None:
            marker, close = ("[red]", "[/]") if bill.over_balance else ("", "")
            lines.append(f"vendor balance:       {marker}{bill.balance}{close}")
        return "\n".join(lines)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)
