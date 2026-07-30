"""Exception hierarchy.

Every error carries two things: what went wrong, and — where one exists — what to
do about it. The CLI renders them on separate lines, so a message never has to
choose between being short and being actionable.

Messages are written for the person who hit them. Invariant names and design
decisions belong in code comments, where they already are; a user reading
"append-only (R2)" learns nothing they can act on.

Every failure mode a caller might handle differently gets its own type. Nothing
here is swallowed anywhere: errors are handled, logged, returned, or propagated.
"""

from __future__ import annotations


class ZipfError(Exception):
    """Base for every error Zipf raises."""

    def __init__(self, problem: str, *, fix: str | None = None) -> None:
        self.problem = problem
        self.fix = fix
        super().__init__(problem if fix is None else f"{problem} {fix}")


class InvalidRequestError(ZipfError):
    """A request cannot be satisfied as asked.

    Raised at the service boundary for input the user can correct, so the CLI
    can print one line instead of a traceback.
    """


class CapabilityUnknownError(ZipfError):
    """A capability name is not in the registry. There is no default TTL."""

    def __init__(self, name: str, known: str) -> None:
        super().__init__(
            f"There is no data source named {name!r}.",
            fix=f"Available sources: {known}.",
        )


class BudgetExceededError(ZipfError):
    """A fetch would push month-to-date spend past the ceiling.

    This fails the call. It does not warn, truncate, or downgrade the request.
    """

    def __init__(self, *, estimate: float, spent: float, ceiling: float) -> None:
        self.estimate = estimate
        self.spent = spent
        self.ceiling = ceiling
        remaining = max(0.0, ceiling - spent)
        super().__init__(
            f"This would cost ${estimate:.5f} but only ${remaining:.2f} is left of your "
            f"${ceiling:.2f} monthly limit (${spent:.5f} already spent).",
            fix="Raise monthly_ceiling_usd in your config file, or wait for the month to reset.",
        )


class CredentialMissingError(ZipfError):
    """A required secret is not present in the environment or .env."""

    def __init__(self, *, variable: str, needed_by: str) -> None:
        self.variable = variable
        self.needed_by = needed_by
        super().__init__(
            f"{needed_by} needs {variable}, which is not set.",
            fix=f"Add {variable} to the .env file in your project root, then run this again.",
        )


class ConfigMissingError(ZipfError):
    """A setting has no value, and no default that would be safe to guess.

    Distinct from a missing credential: this lives in config.toml rather than
    .env, and telling someone to set an environment variable that does not exist
    wastes their time.
    """

    def __init__(self, *, setting: str, config_path: str, flag: str | None = None) -> None:
        self.setting = setting
        alternative = f", or pass {flag} on this command" if flag else ""
        super().__init__(
            f"{setting} is not configured.",
            fix=f"Set {setting} in {config_path}{alternative}.",
        )


class DatabaseMissingError(ZipfError):
    """There is no database to read.

    Raised only by the read-only path. The read-write path creates the file, so
    the only way to reach this is to read before ever running ``zipf init``.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"There is no zipf database at {path}.",
            fix="Run `zipf init` to create it.",
        )


class VendorError(ZipfError):
    """A vendor returned an error, an unparseable body, or timed out."""

    def __init__(
        self,
        *,
        capability: str,
        detail: str,
        status: int | None = None,
        fix: str | None = None,
    ) -> None:
        self.capability = capability
        self.detail = detail
        self.status = status
        where = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{capability}{where}: {detail}", fix=fix)
