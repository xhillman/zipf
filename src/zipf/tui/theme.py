"""The palette, defined once.

Textual resolves a *theme* variable in both places a colour is chosen — a CSS
rule and a markup tag — while a variable declared in the stylesheet resolves
only in CSS. So the palette lives here rather than in ``zipf.tcss``, and nothing
in the interface ever writes a hex code.

Two visual registers (PRD §9.1). ``$free`` and ``$spend`` are the whole
interaction design: what you already own looks different from what would cost
money, everywhere, without reading a word. ``$spend`` is reserved — nothing that
does not spend may use it, because the register only carries meaning while it
stays exclusive.

The rest is identity rather than judgement. ``$cyan`` names things, ``$violet``
marks what is selected, ``$dim`` carries labels. None of them may mean money.

Every surface is the terminal's own background, so whatever translucency the
terminal is configured with survives. See ``TERMINAL`` below for why that is
not spelled ``transparent``.
"""

from __future__ import annotations

from typing import Final

from textual.theme import Theme

INK: Final = "#e6e0d2"  # warm off-white: values, and anything you read
DIM: Final = "#7a7d87"  # labels, separators, everything secondary
CYAN: Final = "#428dee"  # identity — the view you are in, a field's name
VIOLET: Final = "#a864e8"  # selection — the row and the keyword under the cursor
FREE: Final = "#8ed77e"  # owned, cached, free: paid for once and yours
SPEND: Final = "#e0b44f"  # RESERVED: money about to leave. Nothing else.
ALERT: Final = "#e8695f"  # a command that cannot run

#: The ANSI "default" colour, which is the only value that actually lets the
#: terminal's own background through.
#:
#: ``transparent`` does *not* do this. It parses to ``Color(0, 0, 0, a=0)`` — a
#: fully transparent *black* — and Textual composites it down to opaque black
#: before emitting, so every cell is painted. ``ansi_default`` parses to
#: ``Color(0, 0, 0, ansi=-1)`` and is emitted as the terminal's default
#: background instead, which is what leaves a translucent terminal translucent.
TERMINAL: Final = "ansi_default"

ZIPF_THEME: Final = Theme(
    name="zipf",
    dark=True,
    # The built-ins Textual's own widgets reach for, pointed at this palette so
    # a stock widget cannot arrive wearing a colour from somewhere else.
    primary=CYAN,
    secondary=VIOLET,
    accent=VIOLET,
    foreground=INK,
    success=FREE,
    warning=SPEND,
    error=ALERT,
    background=TERMINAL,
    surface=TERMINAL,
    panel=TERMINAL,
    # Textual's translucent white overlay, used behind several stock
    # widgets. Left as a colour it would tint every one of them grey.
    boost=TERMINAL,
    # Named for what they mean rather than what they look like, so a later
    # change of hue does not turn every call site into a lie.
    variables={
        "ink": INK,
        "dim": DIM,
        "cyan": CYAN,
        "violet": VIOLET,
        "free": FREE,
        "spend": SPEND,
        "alert": ALERT,
    },
)
