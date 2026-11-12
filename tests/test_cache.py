from __future__ import annotations

import asyncio

import pytest

from mcp_server import metrics as m
from mcp_server.cache import FRESH_PREFIX, CacheEntry, RedisCache, SingleFlight, search_key
from mcp_server.search import HotelService
from mcp_server.validation import Stay
from tests.conftest import FakeSupplier

# ---------- SingleFlight ----------


async def test_singleflight_runs_once_for_concurrent_identical_calls() -> None:
    flights: SingleFlight[int] = SingleFlight()
    calls = 0

    async def work() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 42

    results = await asyncio.gather(*(flights.do("k", work) for _ in range(50)))
    assert calls == 1
    assert all(value == 42 for value, _ in results)
    assert sum(shared for _, shared in results) == 49
    assert flights.inflight() == 0


async def test_singleflight_different_keys_run_separately() -> None:
    flights: SingleFlight[str] = SingleFlight()
    calls: list[str] = []

    def work_for(key: str):  # type: ignore[no-untyped-def]
        async def work() -> str:
            calls.append(key)
            await asyncio.sleep(0.01)
            return key
        return work

    await asyncio.gather(*(flights.do(k, work_for(k)) for k in ["a", "b", "a", "b", "c"]))
    assert sorted(calls) == ["a", "b", "c"]


async def test_singleflight_error_reaches_all_waiters_and_next_call_retries() -> None:
    flights: SingleFlight[int] = SingleFlight()
    attempts = 0

    async def failing() -> int:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.02)
        raise RuntimeError("boom")

    results = await asyncio.gather(*(flights.do("k", failing) for _ in range(5)), return_exceptions=True)
    assert attempts == 1
    assert all(isinstance(r, RuntimeError) for r in results)

    async def ok() -> int:
        return 7

    assert await flights.do("k", ok) == (7, False)


async def test_cancelled_follower_does_not_cancel_leader() -> None:
    flights: SingleFlight[int] = SingleFlight()

    async def slow() -> int:
        await asyncio.sleep(0.05)
        return 1

    leader = asyncio.create_task(flights.do("k", slow))
    await asyncio.sleep(0)
    follower = asyncio.create_task(flights.do("k", slow))
    await asyncio.sleep(0.01)
    follower.cancel()
    assert await leader == (1, False)


# ---------- RedisCache ----------


async def test_cache_put_writes_fresh_and_stale_copies(redis) -> None:  # type: ignore[no-untyped-def]
    cache = RedisCache(redis, stale_ttl_s=3600)
    entry = CacheEntry(data={"x": 1}, fetched_at="2026-01-01T00:00:00+00:00")
    await cache.put("k", entry, ttl_s=60)
    assert await cache.get_fresh("k") == entry
    assert await cache.get_stale("k") == entry
    assert 0 < await redis.ttl(FRESH_PREFIX + "k") <= 60

    await redis.delete(FRESH_PREFIX + "k")  # fresh copy expired
    assert await cache.get_fresh("k") is None
    assert await cache.get_stale("k") == entry


def test_search_key_includes_all_dimensions(stay: Stay) -> None:
    base = search_key("Goa", stay, 6000)
    assert base == search_key("goa", stay, 6000)
    assert base != search_key("Goa", stay, 5000)
    assert base != search_key("Goa", Stay(stay.check_in, stay.check_out, 3), 6000)
    assert search_key("Goa", stay, None).endswith(":pany")


# ---------- HotelService read path ----------


async def test_spike_of_identical_searches_makes_one_supplier_call(
    service: HotelService, supplier: FakeSupplier, stay: Stay
) -> None:
    results = await asyncio.gather(*(service.search("Goa", stay, 6000, None, 5) for _ in range(100)))
    assert supplier.calls["search"] == 1
    assert all(r["results"] and not r["stale"] for r in results)

    await service.search("Goa", stay, 6000, None, 5)  # now served from Redis
    assert supplier.calls["search"] == 1

    totals = await service.metrics.totals()
    assert totals[m.CACHE_MISSES] == 1
    assert totals[m.COALESCED] == 99
    assert totals[m.CACHE_HITS] == 1
    assert totals[m.MCP_LOOKS] == 101


async def test_min_stars_and_limit_are_applied_after_cache(
    service: HotelService, supplier: FakeSupplier, stay: Stay
) -> None:
    four_star = await service.search("Goa", stay, None, 4, 5)
    one = await service.search("Goa", stay, None, None, 1)
    assert [h["id"] for h in four_star["results"]] == ["GOA-001"]
    assert len(one["results"]) == 1
    assert supplier.calls["search"] == 1
    assert one["results"][0]["price_as_of"] == four_star["results"][0]["price_as_of"]


async def test_live_price_always_calls_supplier(service: HotelService, supplier: FakeSupplier, stay: Stay) -> None:
    for _ in range(3):
        await service.live_price("GOA-001", stay)
    assert supplier.calls["price"] == 3


async def test_details_are_cached(service: HotelService, supplier: FakeSupplier) -> None:
    await service.details("GOA-001")
    await service.details("GOA-001")
    assert supplier.calls["details"] == 1


@pytest.mark.parametrize("n_books", [1, 3])
async def test_bookings_counter(service: HotelService, stay: Stay, n_books: int) -> None:
    for _ in range(n_books):
        await service.book("GOA-001", stay, "Test")
    assert (await service.metrics.totals())[m.BOOKINGS] == n_books
