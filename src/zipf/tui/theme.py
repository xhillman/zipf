"""The TUI palette, defined once.

The production interface uses the same charcoal palette as
``dev/tui-mockup-2``. Spend and free are the only hues: orange means a command
can cost money, and green means the result is already owned. Everything else
uses neutral value, weight, and surfaces for hierarchy.

Textual resolves theme variables in both stylesheet rules and Rich markup, so
the palette lives here and no TUI call site needs to repeat a hex value.
"""

from __future__ import annotations

from typing import Final

from textual.theme import Theme

BACKGROUND: Final = "#0b0d10"
SURFACE: Final = "#121519"
PANEL: Final = "#1a1e24"
TEXT: Final = "#c3c9d3"
BRIGHT: Final = "#eef1f5"
DIM: Final = "#5d6775"
FAINT: Final = "#333a44"
HOVER: Final = "#171b21"
PLAN: Final = "#0f1216"
SPEND: Final = "#d78700"
FREE: Final = "#6aa84f"

ZIPF_THEME: Final = Theme(
    name="zipf",
    dark=True,
    primary=BRIGHT,
    secondary=DIM,
    accent=BRIGHT,
    foreground=TEXT,
    success=FREE,
    warning=SPEND,
    error=BRIGHT,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    boost=HOVER,
    variables={
        "bright": BRIGHT,
        "dim": DIM,
        "faint": FAINT,
        "hover": HOVER,
        "plan": PLAN,
        "spend": SPEND,
        "free": FREE,
    },
)
