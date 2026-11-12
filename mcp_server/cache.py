"""Redis cache with a long-lived "last known good" copy, plus single-flight coalescing.

Two keys per cached item:
  mh:c:fresh:<key>  short TTL (10 min search / 24 h details) — served as a normal hit
  mh:c:lkg:<key>    long TTL (7 days)                         — served with stale=true only
                                                                 when the supplier can't answer
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from redis.asyncio import Redis

from mcp_server.validation import Stay

T = TypeVar("T")

FRESH_PREFIX = "mh:c:fresh:"
STALE_PREFIX = "mh:c:lkg:"


@dataclass(frozen=True)
class CacheEntry:
    data: Any
    fetched_at: str  # ISO-8601 UTC — becomes `price_as_of` for search results

    def dumps(self) -> str:
        return json.dumps({"data": self.data, "fetched_at": self.fetched_at}, separators=(",", ":"))

    @classmethod
    def loads(cls, raw: str) -> CacheEntry:
        obj = json.loads(raw)
        return cls(data=obj["data"], fetched_at=obj["fetched_at"])


def search_key(city: str, stay: Stay, max_price: int | None) -> str:
    price = "any" if max_price is None else str(max_price)
    return f"search:{city.lower()}:{stay.check_in}:{stay.check_out}:g{stay.guests}:p{price}"


def details_key(hotel_id: str) -> str:
    return f"hotel:{hotel_id}"


class RedisCache:
    def __init__(self, redis: Redis, stale_ttl_s: int) -> None:
        self._redis = redis
        self._stale_ttl_s = stale_ttl_s

    async def get_fresh(self, key: str) -> CacheEntry | None:
        raw = await self._redis.get(FRESH_PREFIX + key)
        return CacheEntry.loads(raw) if raw else None

    async def get_stale(self, key: str) -> CacheEntry | None:
        raw = await self._redis.get(STALE_PREFIX + key)
        return CacheEntry.loads(raw) if raw else None

    async def put(self, key: str, entry: CacheEntry, ttl_s: int) -> None:
        payload = entry.dumps()
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.set(FRESH_PREFIX + key, payload, ex=ttl_s)
            pipe.set(STALE_PREFIX + key, payload, ex=self._stale_ttl_s)
            await pipe.execute()

    async def clear(self) -> None:
        for prefix in (FRESH_PREFIX, STALE_PREFIX):
            async for key in self._redis.scan_iter(match=prefix + "*", count=500):
                await self._redis.delete(key)


class SingleFlight(Generic[T]):
    """Coalesce concurrent calls with the same key into ONE execution (in-process).

    The first caller (the leader) runs `fn`; everyone arriving while it is in flight awaits
    the same future. Returns (value, shared) where shared=True for followers.
    A follower being cancelled never cancels the leader's work (asyncio.shield).
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future[T]] = {}

    def inflight(self) -> int:
        return len(self._inflight)

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing), True

        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await fn()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
                future.exception()  # mark retrieved: no "never retrieved" warning if nobody waited
            raise
        else:
            future.set_result(result)
            return result, False
        finally:
            self._inflight.pop(key, None)
