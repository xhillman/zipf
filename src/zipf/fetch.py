"""The metered effect boundary.

Exactly one function in this codebase touches the network (R1). Cost, freshness,
and sockets appear together here and nowhere else.

The order of operations in :func:`fetch` is fixed and load-bearing. Check
freshness before pricing, price before the ceiling, and the ceiling before the
socket. Reordering any two changes what gets billed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import httpx

from zipf import capabilities
from zipf.budget import Budget
from zipf.capabilities import Capability
from zipf.clock import from_iso, now, now_iso
from zipf.config import missing_credentials
from zipf.db.connection import transaction
from zipf.errors import CredentialMissingError, VendorError, ZipfError
from zipf.jobs.describe import job_kind
from zipf.pricing import PriceEstimate
from zipf.projections.rebuild import project
from zipf.ratelimit import LIMITER

logger = logging.getLogger(__name__)

# Bounds. Anything sized by the outside world gets a limit and a defined
# behaviour when the limit is reached (spec §2.5).
TIMEOUT: Final = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
FREE_CONCURRENCY: Final = 8

# Serialising paid calls makes check-then-spend safe without a reservation
# protocol. There is one operator, so the lost parallelism costs nothing.
_paid_lock = asyncio.Lock()
_free_semaphore = asyncio.Semaphore(FREE_CONCURRENCY)

_CACHE_LOOKUP_SQL = """
SELECT id, body, cost_usd, fetched_at
FROM raw_response
WHERE capability = ? AND params_hash = ?
ORDER BY fetched_at DESC
LIMIT 1
"""

_INSERT_SQL = """
INSERT INTO raw_response (capability, params_hash, params_json, body, cost_usd, fetched_at)
VALUES (?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class FetchResult:
    """What a fetch produced, and what it cost."""

    capability: str
    params_hash: str
    params: Mapping[str, Any]
    plan: PriceEstimate
    cached: bool
    cost_usd: float
    raw_id: int | None = None
    body: bytes | None = None
    age: timedelta | None = None
    #: Set when the bytes were stored but could not be projected. The fetch
    #: still succeeded; a rebuild after fixing the projector recovers the rows.
    projection_error: str | None = None

    def parsed(self) -> Any:
        """Run the capability's parser over the body."""
        if self.body is None:
            raise ValueError(f"{self.capability}: no body to parse (dry run)")
        return capabilities.get(self.capability).parse(self.body, self.params)


def normalise_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Put params into the one canonical form that the cache key is built from.

    Strings are stripped and lowercased, lists are sorted, and ``None`` values
    are dropped. Two requests that differ only in the whitespace or casing of a
    keyword are the same request, and must not be bought twice.
    """

    def canon(value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, Mapping):
            return {k: canon(v) for k, v in sorted(value.items()) if v is not None}
        if isinstance(value, list | tuple | set):
            return sorted((canon(v) for v in value if v is not None), key=repr)
        return value

    return {k: canon(v) for k, v in sorted(params.items()) if v is not None}


def hash_params(normalised: Mapping[str, Any]) -> str:
    canonical = json.dumps(normalised, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_credentials(cap: Capability) -> None:
    """Fail before the socket if a required credential is not configured."""
    missing = missing_credentials(cap.requires)
    if missing:
        raise CredentialMissingError(
            variable=" and ".join(missing), needed_by=f"`zipf {job_kind(cap.name)}`"
        )


def _lookup_cached(
    conn: sqlite3.Connection, cap: Capability, params_hash: str
) -> tuple[sqlite3.Row, timedelta] | None:
    """Return the newest cached row and its age, if one exists."""
    row = conn.execute(_CACHE_LOOKUP_SQL, (cap.name, params_hash)).fetchone()
    if row is None:
        return None
    return row, now() - from_iso(row["fetched_at"])


async def _build_request(cap: Capability, normalised: Mapping[str, Any]) -> httpx.Request:
    """Build the request and attach credentials.

    Auth headers are applied here rather than inside ``build`` so that they never
    reach the params that produced ``params_hash``.
    """
    request = cap.build(normalised)
    if cap.auth is not None:
        request.headers.update(await cap.auth())
    return request


async def _send(cap: Capability, request: httpx.Request) -> bytes:
    """Send the request. The only place an AsyncClient is created (R1).

    Paced first. This runs only on a cache miss, so stored data is never delayed
    — and because every network path funnels through here, a capability's limit
    holds whether the caller is the job runner, an inline drain, or a loop inside
    a service.
    """
    waited = await LIMITER.acquire(cap.name, cap.rate_limit_per_minute)
    if waited:
        logger.info("%s waited %.1fs for a rate limit slot", cap.name, waited)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.send(request)
        except httpx.HTTPError as exc:
            raise VendorError(capability=cap.name, detail=str(exc)) from exc

    if response.status_code >= 400:
        raise VendorError(
            capability=cap.name,
            detail=response.text[:200],
            status=response.status_code,
        )
    return response.content


def _persist(
    conn: sqlite3.Connection,
    cap: Capability,
    params_hash: str,
    normalised: Mapping[str, Any],
    body: bytes,
    cost_usd: float,
) -> int:
    with transaction(conn):
        cursor = conn.execute(
            _INSERT_SQL,
            (
                cap.name,
                params_hash,
                json.dumps(normalised, sort_keys=True, separators=(",", ":"), default=str),
                body,
                cost_usd,
                now_iso(),
            ),
        )
        raw_id = cursor.lastrowid
        if raw_id is None:  # pragma: no cover - sqlite always reports a rowid here
            raise VendorError(capability=cap.name, detail="insert did not return a row id")

    return raw_id


def _project_without_losing_bytes(conn: sqlite3.Connection, raw_id: int) -> str | None:
    """Project a stored response, surviving a broken projector.

    The bytes commit before this runs, and a projection failure is reported
    rather than raised. Vendor schema drift is certain (PRD §14), and the whole
    reason the cache sits at the HTTP boundary is that a broken parser must cost
    a rebuild, never a re-purchase. Rolling the insert back would mean paying for
    bytes and then discarding them, repeatedly, for as long as the parser is
    wrong.

    Returns an error description if projection failed, otherwise ``None``.
    """
    try:
        with transaction(conn):
            project(conn, raw_id)
    except (sqlite3.Error, ZipfError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "projection failed for raw_response id=%s: %s; bytes are kept, run `zipf db rebuild` "
            "after fixing the projector",
            raw_id,
            exc,
        )
        return f"{type(exc).__name__}: {exc}"
    return None


async def fetch(
    conn: sqlite3.Connection,
    capability: str,
    params: Mapping[str, Any],
    *,
    budget: Budget,
    force: bool = False,
    dry_run: bool = False,
) -> FetchResult:
    """Answer a capability request from cache, or buy it once and keep it.

    Args:
        conn: Read-write connection. The insert commits before projection, so
            a broken projector costs a rebuild rather than the purchase.
        capability: Registry name, e.g. ``autocomplete.suggest``.
        params: Request parameters, normalised before hashing.
        budget: Ceiling enforcement.
        force: Ignore a fresh cache entry and re-fetch. Never part of the hash.
        dry_run: Return the plan and the bill without fetching.
    """
    cap = capabilities.get(capability)
    normalised = normalise_params(params)
    params_hash = hash_params(normalised)

    # 1. TTL short-circuit. Target: >95% of calls end here, at zero cost.
    cached = _lookup_cached(conn, cap, params_hash)
    if cached is not None and not force:
        row, age = cached
        if age < cap.ttl:
            return FetchResult(
                capability=cap.name,
                params_hash=params_hash,
                params=normalised,
                plan=cap.price(normalised),
                cached=True,
                cost_usd=0.0,
                raw_id=row["id"],
                body=row["body"],
                age=age,
            )

    # 2. Price declaration, before the call rather than from the invoice.
    plan = cap.price(normalised)

    # 3. Ceiling check. This fails the call; it does not warn.
    budget.assert_within(conn, plan)

    # 4. Dry run: the plan and the bill, without the socket.
    if dry_run:
        return FetchResult(
            capability=cap.name,
            params_hash=params_hash,
            params=normalised,
            plan=plan,
            cached=False,
            cost_usd=0.0,
        )

    _assert_credentials(cap)

    # 5. Execute and persist.
    guard = _free_semaphore if plan.is_free else _paid_lock
    async with guard:
        body = await _send(cap, await _build_request(cap, normalised))

        # Validate before persisting. A vendor error that reached us as HTTP 200
        # must not enter the cache, or it is served for the rest of the TTL.
        if cap.validate is not None:
            cap.validate(body)

        # Prefer what the vendor says it charged over what we predicted.
        charged = cap.cost_from_response(body) if cap.cost_from_response else None
        cost_usd = charged if charged is not None else plan.usd

        raw_id = _persist(conn, cap, params_hash, normalised, body, cost_usd)
        projection_error = _project_without_losing_bytes(conn, raw_id)

    return FetchResult(
        capability=cap.name,
        params_hash=params_hash,
        params=normalised,
        plan=plan,
        cached=False,
        cost_usd=cost_usd,
        raw_id=raw_id,
        body=body,
        age=timedelta(0),
        projection_error=projection_error,
    )
