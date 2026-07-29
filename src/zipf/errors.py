"""Exception hierarchy.

Every failure mode that a caller might reasonably handle differently gets its own
type. Nothing here is swallowed anywhere: errors are handled, logged, returned,
or propagated.
"""

from __future__ import annotations


class ZipfError(Exception):
    """Base for every error Zipf raises."""


class InvalidRequestError(ZipfError):
    """A request cannot be satisfied as asked.

    Raised at the service boundary for input the user can correct, so the CLI
    can print one line instead of a traceback.
    """


class CapabilityUnknownError(ZipfError):
    """A capability name is not in the registry. There is no default TTL."""


class BudgetExceededError(ZipfError):
    """A fetch would push month-to-date spend past the ceiling.

    This fails the call. It does not warn, truncate, or downgrade the request.
    """

    def __init__(self, *, estimate: float, spent: float, ceiling: float) -> None:
        self.estimate = estimate
        self.spent = spent
        self.ceiling = ceiling
        super().__init__(
            f"budget ceiling reached: ${spent:.4f} spent + ${estimate:.4f} estimated "
            f"exceeds ${ceiling:.2f}"
        )


class CredentialMissingError(ZipfError):
    """A capability needs a credential that is not configured."""

    def __init__(self, *, capability: str, variable: str) -> None:
        self.capability = capability
        self.variable = variable
        super().__init__(f"capability {capability!r} requires {variable} to be set")


class VendorError(ZipfError):
    """A vendor returned an error, an unparseable body, or timed out."""

    def __init__(self, *, capability: str, detail: str, status: int | None = None) -> None:
        self.capability = capability
        self.detail = detail
        self.status = status
        where = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{capability}{where}: {detail}")
