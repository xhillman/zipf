"""Time handling.

Every timestamp Zipf stores is ISO-8601 UTC with a ``Z`` suffix, as TEXT. UTC is
not a style preference: the budget month boundary is computed from these values,
and a local-time boundary would move twice a year.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(ISO_FORMAT)


def now_iso() -> str:
    return to_iso(now())


def from_iso(text: str) -> datetime:
    """Parse a stored timestamp back into an aware datetime."""
    return datetime.strptime(text, ISO_FORMAT).replace(tzinfo=UTC)


def month_start_iso(moment: datetime | None = None) -> str:
    """First instant of the current UTC calendar month."""
    m = (moment or now()).astimezone(UTC)
    return to_iso(m.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def humanise(delta: timedelta) -> str:
    """A compact age: ``12s``, ``4m``, ``3h``, ``6d``.

    Coarse on purpose. Whether a job ran 3h or 3h20m ago never changes a
    decision, and the extra precision costs column width.
    """
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def age_of(stored: str | None) -> str:
    """Compact age of a stored timestamp, or an em dash when absent."""
    if not stored:
        return "—"
    return humanise(now() - from_iso(stored))


def elapsed_between(start: str | None, end: str | None) -> str:
    """How long something took, when both ends are known."""
    if not start or not end:
        return "—"
    return humanise(from_iso(end) - from_iso(start))
