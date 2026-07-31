"""Error messages.

Two properties are worth pinning: an error names something the reader can do, and
it never leaks an internal invariant or decision tag. Both are easy to regress
while adding a message in a hurry.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from zipf.db.connection import connect, open_ro
from zipf.db.migrate import pending_names
from zipf.errors import (
    BudgetExceededError,
    CapabilityUnknownError,
    ConfigMissingError,
    CredentialMissingError,
    DatabaseOutdatedError,
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


def test_a_database_behind_the_code_names_zipf_init(zipf_home: Path) -> None:
    """Updating zipf and running a read command must not give a traceback.

    The read-only path cannot migrate, so it has to say who can. This is the
    realistic failure: pull new code, run `zipf db stats`, hit a table that the
    unapplied migration was going to create.
    """
    database = zipf_home / "zipf.db"
    with connect(database) as writer:
        writer.execute(
            "DELETE FROM schema_migration WHERE name = (SELECT MAX(name) FROM schema_migration)"
        )

    with pytest.raises(DatabaseOutdatedError) as caught:
        open_ro(database)

    assert "zipf init" in str(caught.value.fix)
    assert "1 migration behind" in caught.value.problem
    assert "Nothing stored is lost" in str(caught.value.fix)


def test_a_current_database_opens_read_only(zipf_home: Path) -> None:
    conn = open_ro(zipf_home / "zipf.db")
    try:
        assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
    finally:
        conn.close()


def test_a_database_with_no_ledger_at_all_is_reported(tmp_path: Path) -> None:
    """A file that is a database but was never migrated by zipf."""
    stray = tmp_path / "stray.db"
    sqlite3.connect(stray).close()

    with pytest.raises(DatabaseOutdatedError) as caught:
        open_ro(stray)
    assert "zipf init" in str(caught.value.fix)


def test_the_read_write_path_is_not_gated_on_being_current(zipf_home: Path) -> None:
    """`zipf init` is the fix, so the path that runs it must not be refused.

    Only the read-only path checks. Gating the writable path would refuse the
    very command that migrates, leaving no way forward.
    """
    database = zipf_home / "zipf.db"
    with connect(database) as writer:
        writer.execute(
            "DELETE FROM schema_migration WHERE name = (SELECT MAX(name) FROM schema_migration)"
        )

    with connect(database) as writer:
        assert pending_names(writer), "the database is now behind"
        assert writer.execute("SELECT 1 AS n").fetchone()["n"] == 1


def test_pending_names_is_empty_on_a_migrated_database(db: sqlite3.Connection) -> None:
    assert pending_names(db) == []
