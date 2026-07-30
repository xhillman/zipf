"""Human descriptions of queued work.

``labs.domain_intersection`` tells you which endpoint ran. It does not tell you
*which domains*, so three gap pulls are indistinguishable in a job list. The
subject closes that gap.

Descriptions key on the **shape of the params**, not on the capability name. A new
capability that takes a ``domain`` is described correctly the day it is added,
with no registry to keep in step — and the failure mode is a generic label rather
than a wrong one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NOTHING = "—"


def _keywords(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return NOTHING
    first = str(values[0])
    return first if len(values) == 1 else f"{first} +{len(values) - 1}"


def job_subject(params: Mapping[str, Any]) -> str:
    """A short phrase naming what a job was about.

    Ordered most specific first: a domain pair is more informative than either
    domain alone, and a keyword list is more informative than the market it was
    requested in.
    """
    if "target1" in params and "target2" in params:
        return f"{params['target1']} vs {params['target2']}"
    if "keywords" in params:
        return _keywords(params["keywords"])
    if "domain" in params:
        return str(params["domain"])
    if "site_url" in params:
        start, end = params.get("start_date"), params.get("end_date")
        window = f" {start}→{end}" if start and end else ""
        return f"{params['site_url']}{window}"
    if "seed" in params:
        return str(params["seed"])
    return NOTHING


def job_depth(params: Mapping[str, Any]) -> str:
    """The requested depth, where the caller chose one and pays for it."""
    if "limit" in params:
        return f"{int(params['limit']):,} rows"
    if "keywords" in params and isinstance(params["keywords"], list):
        count = len(params["keywords"])
        return f"{count} keyword{'s' if count != 1 else ''}"
    return NOTHING


#: Capability to the command a user would have typed. A job list is read by
#: someone recalling what they ran, not by someone recalling which endpoint
#: served it. Unmapped capabilities fall back to the bare endpoint name.
COMMAND_NAMES: dict[str, str] = {
    "labs.domain_intersection": "gap",
    "labs.search_volume": "vol",
    "labs.ranked_keywords": "ranks",
    "autocomplete.suggest": "suggest",
    "gsc.search_analytics": "gsc import",
    "gsc.sites": "gsc sites",
    "dataforseo.user_data": "balance",
}


def job_kind(capability: str) -> str:
    """The command this job came from, or the endpoint if it has no command."""
    if capability in COMMAND_NAMES:
        return COMMAND_NAMES[capability]
    _, _, tail = capability.partition(".")
    return tail or capability
