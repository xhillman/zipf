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

from zipf.budget import Budget
from zipf.pricing import PriceEstimate
from zipf.services.budget import cached_balance


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
        estimate = self._estimate
        rows = f"~{estimate.rows:,} rows" if estimate.rows is not None else "unknown rows"
        lines = []
        if self._detail:
            lines.append(f"[dim]{self._detail}[/]")
        lines.append(f"{rows:<22} not cached")
        lines.append(
            f"[bold]${estimate.usd:.4f}[/]{'':<14} tier {estimate.tier}, {estimate.queue} queue"
        )
        lines.append(f"remaining this month: ${self._budget.remaining(self._conn):.2f}")

        # The vendor balance is the limit that actually bites. Read from cache so
        # the gate stays instant and still works with no network.
        balance, _age = cached_balance(self._conn)
        if balance is not None:
            short = estimate.usd > balance
            marker = "[red]" if short else ""
            close = "[/]" if short else ""
            lines.append(f"vendor balance:       {marker}${balance:.2f}{close}")
        return "\n".join(lines)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)
