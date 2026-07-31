"""Per-capability request pacing.

Vendors publish a requests-per-minute cap and reject everything above it. The
runner drains as fast as it can, which has never mattered at a handful of calls
and does the moment a discovery batch or an alphabet expansion runs in a loop.

Enforced inside ``fetch`` rather than inside the runner. R1 makes ``fetch`` the
only door to the network, so pacing there covers every caller — the runner, a
``--wait`` drain, and the alphabet expansion in ``zipf suggest``, which never
touches the runner at all. Pacing the runner alone would leave those unpaced.

Cache hits are never paced: they return before reaching the send path, which is
what keeps browsing stored data unlimited.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Final

logger = logging.getLogger(__name__)

#: The window a limit is expressed over. Vendors quote per-minute figures.
WINDOW_SECONDS: Final = 60.0

#: Refuse to wait longer than this in one acquisition. A limiter that blocks
#: indefinitely is indistinguishable from a hang, and the caller deserves an
#: error it can report rather than a command that never returns.
MAX_WAIT_SECONDS: Final = 2 * WINDOW_SECONDS


class RateLimitTimeoutError(RuntimeError):
    """Waiting for a slot exceeded ``MAX_WAIT_SECONDS``."""


class RateLimiter:
    """A sliding window of send times per capability.

    Sliding rather than a fixed bucket: a fixed window lets twice the limit
    through across a boundary, which is exactly the burst a vendor rejects.
    """

    def __init__(self) -> None:
        self._sent: dict[str, deque[float]] = defaultdict(deque)

    async def acquire(self, capability: str, per_minute: int | None) -> float:
        """Wait until this capability may send again. Returns seconds waited.

        A capability with no published limit is not paced. Guessing a number for
        a vendor that never stated one would slow calls for no reason.
        """
        if not per_minute or per_minute <= 0:
            return 0.0

        window = self._sent[capability]
        waited = 0.0

        while True:
            moment = time.monotonic()
            while window and moment - window[0] >= WINDOW_SECONDS:
                window.popleft()

            if len(window) < per_minute:
                window.append(moment)
                return waited

            delay = WINDOW_SECONDS - (moment - window[0])
            if waited + delay > MAX_WAIT_SECONDS:
                raise RateLimitTimeoutError(
                    f"{capability} would wait {waited + delay:.0f}s for a slot, "
                    f"above the {MAX_WAIT_SECONDS:.0f}s bound"
                )
            logger.info("%s rate limited: waiting %.1fs for a slot", capability, delay)
            waited += delay
            await asyncio.sleep(delay)

    def reset(self) -> None:
        """Forget every recorded send. For tests, and for nothing else."""
        self._sent.clear()


#: Process-wide, because a rate limit is a property of the account rather than of
#: any one caller. Two commands in one process share the vendor's allowance, so
#: they must share the ledger that tracks it.
LIMITER: Final = RateLimiter()
