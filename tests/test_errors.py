"""Error messages.

Two properties are worth pinning: an error names something the reader can do, and
it never leaks an internal invariant or decision tag. Both are easy to regress
while adding a message in a hurry.
"""

from __future__ import annotations

import re

import pytest

from zipf.errors import (
    BudgetExceededError,
    CapabilityUnknownError,
    ConfigMissingError,
    CredentialMissingError,
    InvalidRequestError,
    VendorError,
    ZipfError,
)

#: Internal shorthand that means nothing to a reader: R1-R7 invariants and
#: D1-D13 decisions from the spec.
INTERNAL_TAG = re.compile(r"\b[RD]\d{1,2}\b")

EVERY_ERROR: list[ZipfError] = [
    CapabilityUnknownError("labs.nope", "labs.search_volume, autocomplete.suggest"),
    BudgetExceededError(estimate=5.0, spent=0.99, ceiling=1.0),
    CredentialMissingError(variable="DATAFORSEO_LOGIN", needed_by="`zipf vol`"),
    ConfigMissingError(setting="own_domain", config_path="/tmp/config.toml", flag="--mine"),
    InvalidRequestError("Something was asked for that cannot be done.", fix="Try it differently."),
    VendorError(capability="labs.search_volume", detail="upstream is down", status=503),
]


@pytest.mark.parametrize("error", EVERY_ERROR, ids=lambda e: type(e).__name__)
def test_no_error_leaks_an_internal_tag(error: ZipfError) -> None:
    rendered = f"{error.problem} {error.fix or ''}"
    leaked = INTERNAL_TAG.findall(rendered)
    assert leaked == [], f"{type(error).__name__} leaked {leaked}: {rendered}"


@pytest.mark.parametrize("error", EVERY_ERROR, ids=lambda e: type(e).__name__)
def test_every_error_says_something(error: ZipfError) -> None:
    """A blank problem is worse than a bad one: the CLI would print nothing."""
    assert error.problem.strip()


def test_a_budget_error_names_the_numbers_and_the_remedy() -> None:
    error = BudgetExceededError(estimate=5.0, spent=0.99, ceiling=1.0)

    assert "$5.00000" in error.problem
    assert "$0.01" in error.problem, "the remaining amount was not stated"
    assert error.fix is not None
    assert "monthly_ceiling_usd" in error.fix


def test_a_missing_credential_points_at_dotenv_not_config() -> None:
    error = CredentialMissingError(variable="GSC_CLIENT_ID", needed_by="Search Console")

    assert "GSC_CLIENT_ID" in error.problem
    assert error.fix is not None
    assert ".env" in error.fix


def test_a_missing_setting_points_at_the_config_file_not_dotenv() -> None:
    """The distinction that motivated a separate error type.

    Telling someone to set an environment variable that does not exist sends them
    to the wrong file.
    """
    error = ConfigMissingError(
        setting="gsc_site_url", config_path="/home/me/.config/zipf/config.toml", flag="--site"
    )

    assert error.fix is not None
    assert "/home/me/.config/zipf/config.toml" in error.fix
    assert "--site" in error.fix
    assert ".env" not in error.fix


def test_a_setting_with_no_flag_omits_the_flag_clause() -> None:
    error = ConfigMissingError(setting="own_domain", config_path="/tmp/c.toml")

    assert error.fix is not None
    assert "or pass" not in error.fix


def test_the_fix_is_carried_separately_so_the_cli_can_lay_it_out() -> None:
    error = InvalidRequestError("A problem.", fix="A remedy.")

    assert error.problem == "A problem."
    assert error.fix == "A remedy."
    # str() stays complete for logs and tracebacks, which have no two-line layout.
    assert str(error) == "A problem. A remedy."


def test_an_error_without_a_fix_does_not_invent_one() -> None:
    error = InvalidRequestError("A problem with no obvious remedy.")

    assert error.fix is None
    assert str(error) == "A problem with no obvious remedy."


def test_an_unknown_source_lists_the_real_ones() -> None:
    error = CapabilityUnknownError("labs.nope", "labs.search_volume")

    assert "labs.nope" in error.problem
    assert error.fix is not None
    assert "labs.search_volume" in error.fix


def test_a_vendor_error_keeps_the_status_code() -> None:
    """Vendor detail is diagnostic, so it survives verbatim."""
    error = VendorError(capability="labs.search_volume", detail="rate limited", status=429)

    assert "429" in error.problem
    assert "rate limited" in error.problem
