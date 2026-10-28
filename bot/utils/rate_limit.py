"""In-memory sliding-window rate limiting for interactive actions."""

from collections import deque
from math import ceil
from time import monotonic
from typing import Callable


class SlidingWindowRateLimiter:
    """Limit how often one key can perform an action in a rolling window."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = monotonic,
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[int, deque[float]] = {}

    def _prune(self, key: int, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def retry_after(self, key: int) -> int:
        """Seconds until ``key`` may act again, or 0 when it is allowed now."""
        now = self._clock()
        hits = self._prune(key, now)
        if len(hits) < self.limit:
            return 0
        return max(1, ceil(self.window_seconds - (now - hits[0])))

    def consume(self, key: int) -> int:
        """Record one action for ``key``.

        Returns 0 when the action is allowed, otherwise the whole seconds
        remaining before it should be retried.
        """
        now = self._clock()
        hits = self._prune(key, now)
        if len(hits) >= self.limit:
            return max(1, ceil(self.window_seconds - (now - hits[0])))
        hits.append(now)
        self._hits[key] = hits
        return 0

    def reset(self) -> None:
        self._hits.clear()


COUNTER_OFFER_LIMITER = SlidingWindowRateLimiter(limit=5, window_seconds=60)
