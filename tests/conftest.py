from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import fakeredis
import pytest

from mcp_server.cache import RedisCache
from mcp_server.config import Settings
from mcp_server.metrics import Metrics
from mcp_server.search import HotelService, SupplierError
from mcp_server.validation import Stay


class FakeSupplier:
    """In-memory stand-in for SupplierClient that counts calls and can fail on demand."""

    def __init__(self, latency_s: float = 0.05) -> None:
        self.latency_s = latency_s
        self.calls: dict[str, int] = {"search": 0, "details": 0, "price": 0}
        self.fail = False

    async def _hit(self, kind: str) -> None:
        self.calls[kind] += 1
        await asyncio.sleep(self.latency_s)
        if self.fail:
            raise SupplierError("supplier returned 503")

    async def search(self, city: str, stay: Stay, max_price: int | None) -> list[dict[str, Any]]:
        await self._hit("search")
        return [
            {"id": "GOA-001", "name": "A", "area": "Baga", "stars": 4, "rating": 4.5,
             "price_per_night_inr": 5000, "total_price_inr": 10000},
            {"id": "GOA-002", "name": "B", "area": "Anjuna", "stars": 3, "rating": 4.0,
             "price_per_night_inr": 3000, "total_price_inr": 6000},
        ]

    async def details(self, hotel_id: str) -> dict[str, Any]:
        await self._hit("details")
        return {"id": hotel_id, "name": "A"}

    async def price(self, hotel_id: str, stay: Stay) -> dict[str, Any]:
        await self._hit("price")
        return {"hotel_id": hotel_id, "price_per_night_inr": 5000, "total_price_inr": 10000}


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def supplier() -> FakeSupplier:
    return FakeSupplier()


@pytest.fixture
def settings() -> Settings:
    return Settings(breaker_failure_threshold=3, breaker_window_s=30, breaker_cooldown_s=5)


@pytest.fixture
def service(settings: Settings, supplier: FakeSupplier, redis: fakeredis.FakeAsyncRedis) -> HotelService:
    return HotelService(settings, supplier, RedisCache(redis, settings.stale_ttl_s), Metrics(redis))  # type: ignore[arg-type]


@pytest.fixture
def stay() -> Stay:
    ci = date.today() + timedelta(days=7)
    return Stay(ci, ci + timedelta(days=2), 2)
