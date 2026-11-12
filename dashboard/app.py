"""Metrics dashboard (port 8002): one HTML page polling /api/metrics every second."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from redis.asyncio import Redis

from mcp_server import metrics as m
from mcp_server.breaker import CircuitBreaker
from mcp_server.config import Settings

INDEX = Path(__file__).parent / "index.html"
WINDOW_S = int(os.getenv("DASHBOARD_WINDOW_S", "90"))

SERIES = (
    m.MCP_REQUESTS, m.SUPPLIER_LOOKS, m.CACHE_HITS, m.COALESCED, m.CACHE_MISSES,
    m.STALE_SERVED, m.RATE_LIMITED, m.SHED, m.BREAKER_REJECTED, m.SUPPLIER_ERRORS,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis
    app.state.metrics = m.Metrics(redis)
    app.state.breaker = CircuitBreaker(
        redis, "supplier", settings.breaker_failure_threshold, settings.breaker_window_s, settings.breaker_cooldown_s
    )
    yield
    await redis.aclose()


app = FastAPI(title="mcp-hotels dashboard", lifespan=lifespan)


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 1) if denominator else None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@app.get("/api/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    mx: m.Metrics = request.app.state.metrics
    totals = await mx.totals()
    series = {name: await mx.series(name, WINDOW_S) for name in SERIES}
    snap = await request.app.state.breaker.snapshot()

    served = totals[m.CACHE_HITS] + totals[m.COALESCED] + totals[m.CACHE_MISSES] + totals[m.STALE_SERVED]
    without_supplier = totals[m.CACHE_HITS] + totals[m.COALESCED] + totals[m.STALE_SERVED]
    last5 = {name: sum(values[-5:]) / 5 for name, values in series.items()}
    return {
        "window_s": WINDOW_S,
        "series": series,
        "now": {
            "mcp_rps": round(last5[m.MCP_REQUESTS], 1),
            "supplier_lps": round(last5[m.SUPPLIER_LOOKS], 1),
            "rate_limited_ps": round(last5[m.RATE_LIMITED], 1),
        },
        "totals": totals,
        "cache": {
            "hit_rate": round(totals[m.CACHE_HITS] / served, 4) if served else None,
            "served_without_supplier": round(without_supplier / served, 4) if served else None,
        },
        "look_to_book": {
            "bookings": totals[m.BOOKINGS],
            "looks_without_cache": totals[m.MCP_LOOKS],
            "looks_with_cache": totals[m.SUPPLIER_LOOKS],
            "ratio_without_cache": ratio(totals[m.MCP_LOOKS], totals[m.BOOKINGS]),
            "ratio_with_cache": ratio(totals[m.SUPPLIER_LOOKS], totals[m.BOOKINGS]),
        },
        "breaker": {
            "state": snap.state.value,
            "recent_failures": snap.recent_failures,
            "retry_in_s": round(snap.retry_in_s, 1),
        },
    }


@app.post("/api/reset")
async def reset(request: Request) -> dict[str, bool]:
    """Zero the counters (demo convenience). Does not touch the cache or the breaker."""
    await request.app.state.metrics.reset()
    return {"ok": True}
