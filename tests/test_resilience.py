from __future__ import annotations

import asyncio

import pytest

from mcp_server import metrics as m
from mcp_server.breaker import BreakerState, CircuitBreaker
from mcp_server.ratelimit import RateLimiter
from mcp_server.search import HotelService, SupplierError, SupplierUnavailable
from mcp_server.shedding import LoadShedder, Priority
from mcp_server.validation import Stay
from tests.conftest import FakeSupplier


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(redis, clock: FakeClock) -> CircuitBreaker:  # type: ignore[no-untyped-def]
    return CircuitBreaker(redis, "test", failure_threshold=3, window_s=10, cooldown_s=5, clock=clock)


# ---------- circuit breaker ----------


async def test_breaker_opens_after_threshold_failures(breaker: CircuitBreaker) -> None:
    for _ in range(2):
        await breaker.record_failure()
    assert (await breaker.snapshot()).state is BreakerState.CLOSED
    assert await breaker.allow()
    await breaker.record_failure()
    assert (await breaker.snapshot()).state is BreakerState.OPEN
    assert not await breaker.allow()


async def test_failures_outside_window_do_not_count(breaker: CircuitBreaker, clock: FakeClock) -> None:
    await breaker.record_failure()
    await breaker.record_failure()
    clock.advance(11)
    await breaker.record_failure()
    assert (await breaker.snapshot()).state is BreakerState.CLOSED


async def test_half_open_allows_exactly_one_probe(breaker: CircuitBreaker, clock: FakeClock) -> None:
    for _ in range(3):
        await breaker.record_failure()
    clock.advance(5)
    decisions = await asyncio.gather(*(breaker.allow() for _ in range(10)))
    assert sum(decisions) == 1
    assert (await breaker.snapshot()).state is BreakerState.HALF_OPEN


async def test_probe_success_closes_breaker(breaker: CircuitBreaker, clock: FakeClock) -> None:
    for _ in range(3):
        await breaker.record_failure()
    clock.advance(5)
    assert await breaker.allow()
    await breaker.record_success()
    snap = await breaker.snapshot()
    assert snap.state is BreakerState.CLOSED and snap.recent_failures == 0
    assert await breaker.allow()


async def test_probe_failure_reopens_with_fresh_cooldown(breaker: CircuitBreaker, clock: FakeClock) -> None:
    for _ in range(3):
        await breaker.record_failure()
    clock.advance(5)
    assert await breaker.allow()
    await breaker.record_failure()
    snap = await breaker.snapshot()
    assert snap.state is BreakerState.OPEN and snap.retry_in_s == pytest.approx(5)
    clock.advance(4)
    assert not await breaker.allow()


async def test_late_success_does_not_close_open_breaker(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        await breaker.record_failure()
    await breaker.record_success()  # a slow call that started before the breaker opened
    assert (await breaker.snapshot()).state is BreakerState.OPEN


# ---------- rate limiter ----------


async def test_token_bucket_allows_burst_then_limits(redis, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    limiter = RateLimiter(redis, capacity=5, refill_per_s=1.0, clock=clock)
    decisions = [await limiter.acquire("agent-1") for _ in range(6)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert decisions[-1].retry_after_whole_s == 1

    other = await limiter.acquire("agent-2")  # buckets are per client
    assert other.allowed

    clock.advance(2)
    assert (await limiter.acquire("agent-1")).allowed
    assert (await limiter.acquire("agent-1")).allowed
    assert not (await limiter.acquire("agent-1")).allowed


async def test_token_bucket_never_exceeds_capacity(redis, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    limiter = RateLimiter(redis, capacity=3, refill_per_s=10.0, clock=clock)
    await limiter.acquire("a")
    clock.advance(3600)
    results = [(await limiter.acquire("a")).allowed for _ in range(4)]
    assert results == [True, True, True, False]


# ---------- load shedding ----------


def test_shedder_reserves_capacity_for_higher_priorities() -> None:
    shedder = LoadShedder(max_inflight=10)
    searches = sum(shedder.try_acquire(Priority.SEARCH) for _ in range(20))
    assert searches == 6
    prices = sum(shedder.try_acquire(Priority.LIVE_PRICE) for _ in range(20))
    assert prices == 3
    books = sum(shedder.try_acquire(Priority.BOOK) for _ in range(20))
    assert books == 1
    shedder.release()
    assert not shedder.try_acquire(Priority.SEARCH)
    assert shedder.try_acquire(Priority.BOOK)


# ---------- stale fallback through the service ----------


async def test_supplier_down_serves_stale_copy(
    service: HotelService, supplier: FakeSupplier, stay: Stay, redis  # type: ignore[no-untyped-def]
) -> None:
    first = await service.search("Goa", stay, 6000, None, 5)
    assert first["stale"] is False

    await redis.delete(*[k async for k in redis.scan_iter("mh:c:fresh:*")])  # fresh TTL expired
    supplier.fail = True
    second = await service.search("Goa", stay, 6000, None, 5)
    assert second["stale"] is True
    assert all(h["stale"] for h in second["results"])
    assert second["results"][0]["price_as_of"] == first["results"][0]["price_as_of"]
    assert "cached prices" in second["notice"]
    assert (await service.metrics.totals())[m.STALE_SERVED] == 1


async def test_supplier_down_without_stale_copy_raises(
    service: HotelService, supplier: FakeSupplier, stay: Stay
) -> None:
    supplier.fail = True
    with pytest.raises(SupplierError):
        await service.search("Manali", stay, None, None, 5)


async def test_open_breaker_short_circuits_supplier_and_still_serves_stale(
    service: HotelService, supplier: FakeSupplier, stay: Stay, redis  # type: ignore[no-untyped-def]
) -> None:
    await service.search("Goa", stay, None, None, 5)  # warm the last-known-good copy
    await redis.delete(*[k async for k in redis.scan_iter("mh:c:fresh:*")])
    supplier.fail = True
    for _ in range(3):  # threshold is 3 in the test settings
        with pytest.raises(SupplierError):
            await service.live_price("GOA-001", stay)
    assert (await service.breaker.snapshot()).state is BreakerState.OPEN

    calls_before = dict(supplier.calls)
    result = await service.search("Goa", stay, None, None, 5)
    assert result["stale"] is True
    with pytest.raises(SupplierUnavailable):
        await service.live_price("GOA-001", stay)
    assert supplier.calls == calls_before  # breaker kept traffic off the failing supplier
