"""Mock hotel supplier API (port 8001). All data is fictional.

Simulates a slow upstream inventory system: every call sleeps 300-800 ms, prices move
with dates, and admin endpoints let the demo inject failures or take it fully down.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from mcp_server.metrics import SUPPLIER_LOOKS, Metrics

DATA_FILE = Path(__file__).parent / "data" / "hotels.json"
LATENCY_MIN_MS = int(os.getenv("SUPPLIER_LATENCY_MIN_MS", "300"))
LATENCY_MAX_MS = int(os.getenv("SUPPLIER_LATENCY_MAX_MS", "800"))
PRICE_NOISE = float(os.getenv("SUPPLIER_PRICE_NOISE", "0.03"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MAX_NIGHTS = 30


@dataclass
class SupplierState:
    """The only mutable process state: demo failure switches."""

    failure_rate: float = 0.0
    down: bool = False


STATE = SupplierState()
HOTELS: dict[str, dict[str, Any]] = {h["id"]: h for h in json.loads(DATA_FILE.read_text(encoding="utf-8"))}


# ---------- pricing ----------

def night_multiplier(night: date) -> float:
    """Deterministic seasonality: Fri/Sat nights +25 %, December +35 %, Christmas–NYE +70 %."""
    m = 1.0
    if night.weekday() in (4, 5):
        m *= 1.25
    if night.month == 12:
        m *= 1.7 if night.day >= 24 else 1.35
    return m


def quote(hotel: dict[str, Any], check_in: date, check_out: date, guests: int) -> dict[str, Any]:
    nights = [date.fromordinal(d) for d in range(check_in.toordinal(), check_out.toordinal())]
    guest_factor = 1.0 + 0.15 * max(0, guests - 2)
    nightly = [hotel["base_price_inr"] * night_multiplier(n) * guest_factor for n in nights]
    noise = 1.0 + random.uniform(-PRICE_NOISE, PRICE_NOISE)
    total = round(sum(nightly) * noise)
    return {
        "hotel_id": hotel["id"],
        "nights": len(nights),
        "price_per_night_inr": round(total / len(nights)),
        "total_price_inr": total,
        "currency": "INR",
        "quoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def parse_stay(check_in: str, check_out: str) -> tuple[date, date]:
    try:
        ci, co = date.fromisoformat(check_in), date.fromisoformat(check_out)
    except ValueError:
        raise HTTPException(400, "dates must be YYYY-MM-DD")
    if not 0 < (co - ci).days <= MAX_NIGHTS:
        raise HTTPException(400, f"check_out must be 1-{MAX_NIGHTS} nights after check_in")
    return ci, co


def public_summary(hotel: dict[str, Any]) -> dict[str, Any]:
    return {k: hotel[k] for k in ("id", "name", "city", "area", "stars", "rating")}


# ---------- app ----------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    app.state.metrics = Metrics(redis)
    yield
    await redis.aclose()


app = FastAPI(title="Mock Hotel Supplier", lifespan=lifespan)


@app.middleware("http")
async def simulate_upstream(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Latency + injected failures for every non-admin call."""
    if request.url.path.startswith(("/admin", "/health")):
        return await call_next(request)
    await asyncio.sleep(random.uniform(LATENCY_MIN_MS, LATENCY_MAX_MS) / 1000)
    if STATE.down:
        return JSONResponse({"detail": "supplier is down (demo)"}, status_code=503)
    if random.random() < STATE.failure_rate:
        return JSONResponse({"detail": "transient supplier failure (demo)"}, status_code=503)
    return await call_next(request)


async def count_look(request: Request) -> None:
    await request.app.state.metrics.incr(SUPPLIER_LOOKS)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "hotels": len(HOTELS), "down": STATE.down, "failure_rate": STATE.failure_rate}


@app.get("/hotels/search")
async def search(
    request: Request,
    city: str,
    check_in: str,
    check_out: str,
    guests: int = Query(2, ge=1, le=8),
    max_price: int | None = Query(None, ge=0),
) -> dict[str, Any]:
    ci, co = parse_stay(check_in, check_out)
    await count_look(request)
    results = []
    for hotel in HOTELS.values():
        if hotel["city"].lower() != city.strip().lower():
            continue
        q = quote(hotel, ci, co, guests)
        if max_price is not None and q["price_per_night_inr"] > max_price:
            continue
        results.append({**public_summary(hotel), **q})
    results.sort(key=lambda r: r["price_per_night_inr"])
    return {"city": city, "check_in": check_in, "check_out": check_out, "guests": guests, "results": results}


@app.get("/hotels/{hotel_id}")
async def details(hotel_id: str) -> dict[str, Any]:
    hotel = HOTELS.get(hotel_id.upper())
    if hotel is None:
        raise HTTPException(404, f"unknown hotel {hotel_id}")
    return {k: v for k, v in hotel.items() if k != "base_price_inr"}


@app.get("/hotels/{hotel_id}/price")
async def price(
    request: Request, hotel_id: str, check_in: str, check_out: str, guests: int = Query(2, ge=1, le=8)
) -> dict[str, Any]:
    hotel = HOTELS.get(hotel_id.upper())
    if hotel is None:
        raise HTTPException(404, f"unknown hotel {hotel_id}")
    ci, co = parse_stay(check_in, check_out)
    await count_look(request)
    return {**quote(hotel, ci, co, guests), "guests": guests}


# ---------- demo admin ----------

class FailureRate(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)


@app.post("/admin/failure-rate")
async def set_failure_rate(body: FailureRate) -> dict[str, Any]:
    STATE.failure_rate = body.rate
    return {"failure_rate": STATE.failure_rate, "down": STATE.down}


@app.post("/admin/down")
async def go_down() -> dict[str, Any]:
    STATE.down = True
    return {"failure_rate": STATE.failure_rate, "down": STATE.down}


@app.post("/admin/up")
async def go_up() -> dict[str, Any]:
    STATE.down = False
    STATE.failure_rate = 0.0
    return {"failure_rate": STATE.failure_rate, "down": STATE.down}
