"""Shared DataForSEO request building and envelope handling.

Two vendor behaviours drive everything here:

1. **Errors arrive as HTTP 200.** A rejected task returns a normal HTTP response
   with a non-20000 ``status_code`` in the body. Checking the HTTP status alone
   would cache an error and serve it for the whole TTL, so :func:`validate` runs
   before anything is persisted.

2. **The response reports what you were actually charged.** ``cost`` on the
   envelope is authoritative; the pre-call estimate is only a gate. Both are
   stored so estimator drift is measurable rather than assumed.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from zipf.config import load_settings
from zipf.errors import CredentialMissingError, VendorError

API_ROOT: Final = "https://api.dataforseo.com/v3"

#: The vendor's "everything worked" code. Anything else is a failure.
STATUS_OK: Final = 20000

#: Default market. 2840 is the United States in the vendor's location taxonomy.
DEFAULT_LOCATION_CODE: Final = 2840
DEFAULT_LANGUAGE_CODE: Final = "en"


def auth_header(capability: str) -> dict[str, str]:
    """Basic auth header, or a clear failure naming what is missing."""
    settings = load_settings()
    if not settings.dataforseo_login or not settings.dataforseo_password:
        raise CredentialMissingError(
            variable="DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD",
            needed_by="Paid keyword data",
        )
    raw = f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def build_task_request(capability: str, path: str, task: Mapping[str, Any]) -> httpx.Request:
    """Build a POST for a single-task Labs endpoint.

    Every DataForSEO endpoint takes an array of tasks. Zipf sends exactly one per
    request so that one ``fetch`` is one cache entry: batching several tasks into
    a request would make them share a TTL and expire together.
    """
    return httpx.Request(
        "POST",
        f"{API_ROOT}{path}",
        json=[dict(task)],
        headers={"Content-Type": "application/json", **auth_header(capability)},
    )


def decode(capability: str, body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise VendorError(capability=capability, detail=f"body was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VendorError(capability=capability, detail="response was not a JSON object")
    return payload


def validate(capability: str, body: bytes) -> None:
    """Reject a vendor-level error before it can be cached.

    Runs before persistence. A response that fails here is never written to
    ``raw_response``, so the next call retries instead of reading a stored error
    for the rest of the TTL.
    """
    payload = decode(capability, body)

    status = payload.get("status_code")
    if status != STATUS_OK:
        raise VendorError(
            capability=capability,
            detail=f"envelope status {status}: {payload.get('status_message')}",
        )

    tasks = payload.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        raise VendorError(capability=capability, detail="envelope carried no tasks")

    for task in tasks:
        task_status = task.get("status_code")
        if task_status != STATUS_OK:
            raise VendorError(
                capability=capability,
                detail=f"task status {task_status}: {task.get('status_message')}",
            )


def actual_cost(body: bytes) -> float | None:
    """The amount actually charged, as reported by the vendor."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    cost = payload.get("cost") if isinstance(payload, dict) else None
    return float(cost) if isinstance(cost, int | float) else None


def task_results(capability: str, body: bytes) -> list[dict[str, Any]]:
    """Return the ``result`` list of the first (and only) task."""
    payload = decode(capability, body)
    tasks = payload.get("tasks") or []
    if not tasks:
        return []

    result = tasks[0].get("result")
    if result is None:
        return []
    if not isinstance(result, list):
        raise VendorError(capability=capability, detail="task result was not a list")
    return [item for item in result if isinstance(item, dict)]


def items_of(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the ``items`` arrays that Labs nests inside each result."""
    items: list[dict[str, Any]] = []
    for result in results:
        nested = result.get("items")
        if isinstance(nested, list):
            items.extend(item for item in nested if isinstance(item, dict))
    return items


def monthly_series(container: Mapping[str, Any]) -> list[dict[str, int]]:
    """The twelve-month volume series, oldest first.

    Both API families report ``monthly_searches`` with the same entry shape, and
    both feed the same ``keyword_month`` table — Google Ads at the top level of a
    keyword record, Labs inside ``keyword_info``. One normaliser here means the
    two cannot disagree about what a month row looks like.

    Kept as a list rather than flattened into a single figure: one number cannot
    say that a seasonal peak is a peak.
    """
    series: list[dict[str, int]] = []
    for entry in container.get("monthly_searches") or []:
        if not isinstance(entry, dict):
            continue
        year, month, volume = entry.get("year"), entry.get("month"), entry.get("search_volume")
        if isinstance(year, int) and isinstance(month, int):
            series.append({"year": year, "month": month, "volume": volume or 0})
    return sorted(series, key=lambda row: (row["year"], row["month"]))
