"""Hotel service: everything between the MCP tools and the supplier.

Read path for search/details:  fresh cache -> single-flight supplier call -> (on failure) stale copy
Live price / booking:          always the supplier (never cached)
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx

from mcp_server import metrics as m
from mcp_server.breaker import BreakerState, CircuitBreaker
from mcp_server.cache import CacheEntry, RedisCache, SingleFlight, details_key, search_key
from mcp_server.config import Settings
from mcp_server.metrics import Metrics
from mcp_server.shedding import LoadShedder, Priority
from mcp_server.validation import Stay

T = TypeVar("T")

SOURCE_SUPPLIER = "supplier"      # fetched just now (this request or one it was coalesced with)
SOURCE_CACHE = "cache"            # fresh cache hit (within TTL)
SOURCE_STALE = "stale_cache"      # last-known-good copy, served because the supplier could not answer


class SupplierError(Exception):
    """Supplier call failed (5xx, timeout, connection error)."""


class SupplierUnavailable(SupplierError):
    """We chose not to call the supplier (breaker open or load shed)."""

    def __init__(self, reason: str, retry_after_s: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


class HotelNotFound(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SupplierClient:
    """Raw HTTP calls to the (mock) supplier."""

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._http.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        except httpx.HTTPError as exc:
            raise SupplierError(f"supplier unreachable: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            raise HotelNotFound(path)
        if resp.status_code >= 500:
            raise SupplierError(f"supplier returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    async def search(self, city: str, stay: Stay, max_price: int | None) -> list[dict[str, Any]]:
        data = await self._get("/hotels/search", {
            "city": city, "check_in": stay.check_in.isoformat(), "check_out": stay.check_out.isoformat(),
            "guests": stay.guests, "max_price": max_price,
        })
        return data["results"]

    async def details(self, hotel_id: str) -> dict[str, Any]:
        return await self._get(f"/hotels/{hotel_id}")

    async def price(self, hotel_id: str, stay: Stay) -> dict[str, Any]:
        return await self._get(f"/hotels/{hotel_id}/price", {
            "check_in": stay.check_in.isoformat(), "check_out": stay.check_out.isoformat(), "guests": stay.guests,
        })


def to_search_result(raw: dict[str, Any], price_as_of: str, stale: bool) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "name": raw["name"],
        "area": raw["area"],
        "stars": raw["stars"],
        "rating": raw["rating"],
        "price_per_night_inr": raw["price_per_night_inr"],
        "total_price_inr": raw["total_price_inr"],
        "price_as_of": price_as_of,
        "stale": stale,
    }


class HotelService:
    def __init__(
        self,
        settings: Settings,
        supplier: SupplierClient,
        cache: RedisCache,
        metrics: Metrics,
        breaker: CircuitBreaker,
        shedder: LoadShedder,
    ) -> None:
        self.settings = settings
        self.supplier = supplier
        self.cache = cache
        self.metrics = metrics
        self.breaker = breaker
        self.shedder = shedder
        self._flights: SingleFlight[CacheEntry] = SingleFlight()

    # ----- supplier guard: breaker -> load shedding -> call -> record outcome -----

    async def _call_supplier(self, fn: Callable[[], Awaitable[T]], priority: Priority) -> T:
        if not await self.breaker.allow():
            await self.metrics.incr(m.BREAKER_REJECTED)
            snap = await self.breaker.snapshot()
            raise SupplierUnavailable("circuit breaker open", max(1, round(snap.retry_in_s)))
        if not self.shedder.try_acquire(priority):
            await self.metrics.incr(m.SHED)
            raise SupplierUnavailable("server busy, low-priority request shed", 2)
        await self.metrics.incr(m.SUPPLIER_CALLS)
        try:
            result = await fn()
        except SupplierError:
            await self.metrics.incr(m.SUPPLIER_ERRORS)
            await self.breaker.record_failure()
            raise
        except HotelNotFound:
            await self.breaker.record_success()  # the supplier answered; it is healthy
            raise
        finally:
            self.shedder.release()
        await self.breaker.record_success()
        return result

    # ----- cached read path -----

    async def _cached(
        self, key: str, ttl_s: int, fetch: Callable[[], Awaitable[Any]]
    ) -> tuple[CacheEntry, str]:
        """Return (entry, source) where source is one of SOURCE_*.

        Fresh hit -> coalesced supplier call -> (supplier failed / breaker open / shed) last-known-good.
        """
        entry = await self.cache.get_fresh(key)
        if entry is not None:
            await self.metrics.incr(m.CACHE_HITS)
            return entry, SOURCE_CACHE

        async def load() -> CacheEntry:
            data = await self._call_supplier(fetch, Priority.SEARCH)
            fresh = CacheEntry(data=data, fetched_at=utc_now_iso())
            await self.cache.put(key, fresh, ttl_s)
            return fresh

        try:
            entry, shared = await self._flights.do(key, load)
        except SupplierError:
            stale = await self.cache.get_stale(key)
            if stale is None:
                raise
            await self.metrics.incr(m.STALE_SERVED)
            return stale, SOURCE_STALE
        await self.metrics.incr(m.COALESCED if shared else m.CACHE_MISSES)
        return entry, SOURCE_SUPPLIER

    async def _supplier_notice(self, source: str, fetched_at: str) -> str | None:
        if source == SOURCE_STALE:
            return (
                f"Supplier unavailable - these are cached prices from {fetched_at} (UTC) and may have changed. "
                "Tell the user, and confirm with check_live_price before booking."
            )
        if await self.breaker.state() is not BreakerState.CLOSED:
            return (
                f"The supplier is currently unavailable. These prices come from the cache (as of {fetched_at} UTC); "
                "live price checks and bookings may fail until it recovers."
            )
        return None

    # ----- public operations -----

    async def search(
        self, city: str, stay: Stay, max_price: int | None, min_stars: int | None, limit: int
    ) -> dict[str, Any]:
        await self.metrics.incr(m.MCP_LOOKS)
        entry, source = await self._cached(
            search_key(city, stay, max_price),
            self.settings.search_ttl_s,
            lambda: self.supplier.search(city, stay, max_price),
        )
        stale = source == SOURCE_STALE
        raw: list[dict[str, Any]] = entry.data
        hotels = [h for h in raw if min_stars is None or h["stars"] >= min_stars][:limit]
        result: dict[str, Any] = {
            "city": city,
            "check_in": stay.check_in.isoformat(),
            "check_out": stay.check_out.isoformat(),
            "nights": stay.nights,
            "guests": stay.guests,
            "results": [to_search_result(h, entry.fetched_at, stale) for h in hotels],
            "total_matches": len(raw),
            "stale": stale,
            "source": source,
        }
        if notice := await self._supplier_notice(source, entry.fetched_at):
            result["notice"] = notice
        return result

    async def details(self, hotel_id: str) -> dict[str, Any]:
        entry, source = await self._cached(
            details_key(hotel_id), self.settings.details_ttl_s, lambda: self.supplier.details(hotel_id)
        )
        return {**entry.data, "stale": source == SOURCE_STALE, "source": source}

    async def live_price(self, hotel_id: str, stay: Stay) -> dict[str, Any]:
        await self.metrics.incr(m.MCP_LOOKS)
        return await self._call_supplier(lambda: self.supplier.price(hotel_id, stay), Priority.LIVE_PRICE)

    async def book(self, hotel_id: str, stay: Stay, guest_name: str) -> dict[str, Any]:
        await self.metrics.incr(m.MCP_LOOKS)  # the booking's price re-check is a look either way
        quote = await self._call_supplier(lambda: self.supplier.price(hotel_id, stay), Priority.BOOK)
        await self.metrics.incr(m.BOOKINGS)
        return {
            "status": "CONFIRMED (MOCK - no real booking was made)",
            "confirmation_id": f"MOCK-{secrets.token_hex(4).upper()}",
            "hotel_id": hotel_id,
            "guest_name": guest_name,
            "check_in": stay.check_in.isoformat(),
            "check_out": stay.check_out.isoformat(),
            "guests": stay.guests,
            "total_price_inr": quote["total_price_inr"],
            "booked_at": utc_now_iso(),
        }
