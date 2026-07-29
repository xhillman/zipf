"""Time handling.

Every timestamp Zipf stores is ISO-8601 UTC with a ``Z`` suffix, as TEXT. UTC is
not a style preference: the budget month boundary is computed from these values,
and a local-time boundary would move twice a year.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
