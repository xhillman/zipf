"""Job descriptions and timing.

`labs.domain_intersection` names an endpoint, not a request. Three gap pulls have
to be tellable apart, which is what these cover.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from zipf.clock import elapsed_between, humanise
from zipf.jobs import describe, queue
from zipf.jobs.describe import NOTHING, job_depth, job_kind, job_subject, status_style


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"target1": "them.com", "target2": "mine.com"}, "them.com vs mine.com"),
        ({"keywords": ["best crm"]}, "best crm"),
        ({"keywords": ["best crm", "free crm", "crm pricing"]}, "best crm +2"),
        ({"domain": "ahrefs.com", "limit": 100}, "ahrefs.com"),
        ({"seed": "crm software", "lang": "en"}, "crm software"),
        ({}, NOTHING),
    ],
)
def test_subject_names_the_request(params: dict[str, object], expected: str) -> None:
    assert job_subject(params) == expected


def test_a_domain_pair_beats_either_domain_alone() -> None:
    """Most specific wins: 'them vs mine' says more than either name."""
    params = {"target1": "them.com", "target2": "mine.com", "domain": "them.com"}
    assert job_subject(params) == "them.com vs mine.com"


def test_gsc_subject_includes_the_window() -> None:
    subject = job_subject(
        {"site_url": "sc-domain:x.dev", "start_date": "2026-04-27", "end_date": "2026-07-26"}
    )
    assert subject == "sc-domain:x.dev 2026-04-27→2026-07-26"


def test_an_empty_keyword_list_is_not_described_as_a_keyword() -> None:
    assert job_subject({"keywords": []}) == NOTHING


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"limit": 1000}, "1,000 rows"),
        ({"keywords": ["a", "b"]}, "2 keywords"),
        ({"keywords": ["a"]}, "1 keyword"),
        ({"seed": "x"}, NOTHING),
    ],
)
def test_depth_reports_what_was_paid_for(params: dict[str, object], expected: str) -> None:
    assert job_depth(params) == expected


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ("labs.domain_intersection", "gap"),
        ("labs.search_volume", "vol"),
        ("gsc.search_analytics", "gsc import"),
        # Unmapped capabilities degrade to the endpoint rather than failing.
        ("labs.something_new", "something_new"),
        ("bare", "bare"),
    ],
)
def test_kind_names_the_command_not_the_endpoint(capability: str, expected: str) -> None:
    assert job_kind(capability) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "0s"),
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=4), "4m"),
        (timedelta(hours=21), "21h"),
        (timedelta(days=6), "6d"),
        (timedelta(seconds=-5), "0s"),  # clock skew must not print a negative age
    ],
)
def test_humanise_is_coarse_and_never_negative(delta: timedelta, expected: str) -> None:
    assert humanise(delta) == expected


def test_elapsed_is_absent_until_both_ends_are_known() -> None:
    assert elapsed_between(None, "2026-07-29T00:00:00Z") == "—"
    assert elapsed_between("2026-07-29T00:00:00Z", None) == "—"
    assert elapsed_between("2026-07-29T00:00:00Z", "2026-07-29T00:02:00Z") == "2m"


def test_claiming_a_job_records_when_it_started(db: sqlite3.Connection) -> None:
    """Duration must measure the run, not the queue wait before it."""
    queue.enqueue(db, "autocomplete.suggest", {"seed": "x"})

    job = queue.claim(db)

    assert job is not None
    assert job.started_at is not None
    row = queue.get(db, job.id)
    assert row is not None
    assert row["started_at"] is not None
    assert row["created_at"] <= row["started_at"]


def test_get_returns_none_for_an_unknown_job(db: sqlite3.Connection) -> None:
    assert queue.get(db, 999) is None


def test_recent_carries_what_the_list_view_needs(db: sqlite3.Connection) -> None:
    """The list renders params and timings, so recent() must select them."""
    queue.enqueue(db, "labs.domain_intersection", {"target1": "a.com", "target2": "b.com"})

    row = queue.recent(db)[0]

    # sqlite3.Row membership tests values, not column names, so the key list is
    # taken explicitly.
    selected = set(row.keys())
    for column in ("params_json", "created_at", "started_at", "finished_at", "attempts"):
        assert column in selected, f"recent() is missing {column}"


def test_every_job_status_has_one_style_for_both_shells() -> None:
    """One palette, so the same word is never a different colour in the two windows.

    There were two maps and they had drifted: the TUI knew ``cancelled`` and the
    CLI did not, so a cancelled job was dim in one window and white in the other.
    Every status the queue can write must resolve, or the shell that meets it
    first silently invents a colour.
    """
    for status in (queue.QUEUED, queue.RUNNING, queue.DONE, queue.FAILED, queue.CANCELLED):
        assert status in describe.STATUS_STYLES, f"{status} has no style"
        assert status_style(status) != "white", f"{status} fell through to the default"


def test_an_unknown_status_stays_plain() -> None:
    """A status nobody has styled must not borrow another status's meaning."""
    assert status_style("something new") == "white"
