"""Redis-backed counters shared by the supplier, the MCP server and the dashboard.

Every counter keeps a running total plus one short-lived bucket per second, so the
dashboard can compute both totals and per-second rates without any extra process.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from redis.asyncio import Redis

PREFIX = "mh:m"
BUCKET_TTL_S = 300

# Counter names (one place, so producers and the dashboard agree).
MCP_REQUESTS = "mcp_requests"            # every tool call reaching the MCP server
MCP_LOOKS = "mcp_looks"                  # search + live-price tool calls = looks the supplier WOULD see without a cache
CACHE_HITS = "cache_hits"
CACHE_MISSES = "cache_misses"
COALESCED = "coalesced"                  # callers that piggy-backed on an in-flight identical request
SUPPLIER_LOOKS = "supplier_looks"        # counted BY THE SUPPLIER: search + price calls it actually served
SUPPLIER_CALLS = "supplier_calls"        # every outbound call the MCP server made (incl. details)
SUPPLIER_ERRORS = "supplier_errors"
STALE_SERVED = "stale_served"
RATE_LIMITED = "rate_limited"
SHED = "shed"
BREAKER_REJECTED = "breaker_rejected"
BOOKINGS = "bookings"

ALL_COUNTERS: tuple[str, ...] = (
    MCP_REQUESTS, MCP_LOOKS, CACHE_HITS, CACHE_MISSES, COALESCED, SUPPLIER_LOOKS, SUPPLIER_CALLS,
    SUPPLIER_ERRORS, STALE_SERVED, RATE_LIMITED, SHED, BREAKER_REJECTED, BOOKINGS,
)


def _total_key(name: str) -> str:
    return f"{PREFIX}:total:{name}"


def _bucket_key(name: str, second: int) -> str:
    return f"{PREFIX}:sec:{name}:{second}"


class Metrics:
    """Thin counter API. Failures to record metrics never break the request path."""

    def __init__(self, redis: Redis, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._clock = clock

    async def incr(self, name: str, amount: int = 1) -> None:
        second = int(self._clock())
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.incrby(_total_key(name), amount)
                pipe.incrby(_bucket_key(name, second), amount)
                pipe.expire(_bucket_key(name, second), BUCKET_TTL_S)
                await pipe.execute()
        except Exception:  # metrics are best-effort
            pass

    async def totals(self, names: Iterable[str] = ALL_COUNTERS) -> dict[str, int]:
        names = list(names)
        values = await self._redis.mget([_total_key(n) for n in names])
        return {n: int(v or 0) for n, v in zip(names, values)}

    async def series(self, name: str, seconds: int = 60) -> list[int]:
        """Per-second counts for the last `seconds` COMPLETE seconds (oldest first)."""
        now = int(self._clock())
        keys = [_bucket_key(name, s) for s in range(now - seconds, now)]
        values = await self._redis.mget(keys)
        return [int(v or 0) for v in values]

    async def rate(self, name: str, window: int = 3) -> float:
        """Average per-second rate over the last `window` complete seconds."""
        data = await self.series(name, window)
        return sum(data) / window if window else 0.0

    async def reset(self) -> None:
        async for key in self._redis.scan_iter(match=f"{PREFIX}:*", count=500):
            await self._redis.delete(key)
