"""Text formatting for user-visible output.

Sits above both shells rather than inside either: the CLI and the TUI are peers
(spec D2), so a formatter they share cannot live in one and be imported by the
other. These produce user-visible strings, and a pluralisation bug ships as
"50 distinct querys".
"""

from __future__ import annotations

VOWELS = frozenset("aeiou")
ABSENT = "[dim]—[/]"


def plural(count: int, noun: str) -> str:
    """``3 keywords``, ``1 keyword``, ``50 distinct queries``.

    Handles the consonant-plus-y case because the output uses it. Anything more
    irregular should be passed already-plural rather than guessed at here — a
    formatter that tries to conjugate English will be wrong in public.
    """
    if count == 1:
        return f"1 {noun}"
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in VOWELS:
        return f"{count:,} {noun[:-1]}ies"
    return f"{count:,} {noun}s"


def number(value: float | None, fmt: str = ",") -> str:
    return f"{value:{fmt}}" if value is not None else ABSENT


def money(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else ABSENT
