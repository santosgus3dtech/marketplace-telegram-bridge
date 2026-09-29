"""Small bounded in-memory rate limiter for the single-process public API."""

import asyncio
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Limit requests per key without an external cache or unbounded memory growth."""

    def __init__(
        self,
        *,
        requests: int,
        window_seconds: float,
        max_keys: int,
    ) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._buckets: dict[str, deque[float]] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, *, now: float | None = None) -> bool:
        """Return whether one request fits in the key's sliding window."""

        timestamp = time.monotonic() if now is None else now
        cutoff = timestamp - self.window_seconds
        async with self._lock:
            stale_keys = [
                candidate for candidate, seen_at in self._last_seen.items() if seen_at <= cutoff
            ]
            for candidate in stale_keys:
                self._last_seen.pop(candidate, None)
                self._buckets.pop(candidate, None)

            if key not in self._buckets and len(self._buckets) >= self.max_keys:
                oldest_key = min(self._last_seen, key=self._last_seen.__getitem__)
                self._last_seen.pop(oldest_key, None)
                self._buckets.pop(oldest_key, None)

            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            self._last_seen[key] = timestamp
            if len(bucket) >= self.requests:
                return False
            bucket.append(timestamp)
            return True
