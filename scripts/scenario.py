"""Scripted resilience walk-through against a running stack (no Claude needed).

  docker compose run --rm --no-deps mcp python scripts/scenario.py

Steps: warm cache -> rate limit -> supplier down -> breaker opens -> cached + stale answers
-> live price refused -> supplier up -> breaker recovers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date, timedelta
from typing import Any

import httpx
from mcp.client.client import Client
from redis.asyncio import Redis

from mcp_server.cache import FRESH_PREFIX, search_key
from mcp_server.validation import Stay


def say(msg: str) -> None:
    print(f"\n>>> {msg}", flush=True)


def parse(result: Any) -> tuple[bool, Any]:
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        return False, text
    return True, json.loads(text)


def brief(ok: bool, body: Any) -> str:
    if not ok:
        return f"ERROR: {body}"
    if "results" in body:
        first = body["results"][0] if body["results"] else {}
        return (f"{len(body['results'])} hotels, source={body.get('source')}, stale={body['stale']}, "
                f"price_as_of={first.get('price_as_of')}, notice={body.get('notice', '-')}")
    return json.dumps(body)[:160]


async def breaker_state(redis: Redis) -> str:
    return (await redis.hget("mh:breaker:supplier", "state")) or "closed"


async def main(mcp_url: str, supplier_url: str, redis_url: str) -> None:
    redis = Redis.from_url(redis_url, decode_responses=True)
    admin = httpx.AsyncClient(base_url=supplier_url)
    await admin.post("/admin/up")
    await redis.delete("mh:breaker:supplier", "mh:breaker:supplier:failures", "mh:breaker:supplier:probe")

    fri = date.today() + timedelta(days=(4 - date.today().weekday()) % 7 or 7)
    ci, co = fri.isoformat(), (fri + timedelta(days=2)).isoformat()
    warm = {"city": "Goa", "check_in": ci, "check_out": co, "guests": 2, "max_price_inr": 6000}

    async with Client(mcp_url) as client:
        say("1. Warm search (cache miss -> supplier)")
        print(brief(*parse(await client.call_tool("search_hotels", warm))))
        say("   Same search again (fresh cache hit)")
        print(brief(*parse(await client.call_tool("search_hotels", warm))))

        say("2. Burst of 45 searches from this client (bucket: 30 burst, 10/s refill)")
        results = await asyncio.gather(*(client.call_tool("search_hotels", warm) for _ in range(45)))
        limited = [parse(r)[1] for r in results if not parse(r)[0]]
        print(f"{45 - len(limited)} served, {len(limited)} rate limited. e.g. {limited[0] if limited else '-'}")
        await asyncio.sleep(4)

        say("3. Supplier DOWN; a few uncached searches hit it and fail")
        await admin.post("/admin/down")
        for nights in range(1, 7):
            other = {**warm, "check_out": (fri + timedelta(days=nights + 3)).isoformat()}
            ok, body = parse(await client.call_tool("search_hotels", other))
            print(f"   uncached search #{nights}: {brief(ok, body)[:110]}  | breaker={await breaker_state(redis)}")

        say("4. Warm search while breaker is OPEN (still in fresh cache)")
        print(brief(*parse(await client.call_tool("search_hotels", warm))))

        say("5. Fresh copy expires -> last-known-good served with stale=true")
        stay = Stay(date.fromisoformat(ci), date.fromisoformat(co), 2)
        await redis.delete(FRESH_PREFIX + search_key("Goa", stay, 6000))
        print(brief(*parse(await client.call_tool("search_hotels", warm))))

        say("6. check_live_price while OPEN (must not pretend)")
        print(brief(*parse(await client.call_tool("check_live_price", {
            "hotel_id": "GOA-001", "check_in": ci, "check_out": co}))))

        say("7. Supplier UP; wait for cooldown, then the half-open probe closes the breaker")
        await admin.post("/admin/up")
        cooldown = int(os.getenv("BREAKER_COOLDOWN_S", "15"))
        await asyncio.sleep(cooldown + 1)
        ok, body = parse(await client.call_tool("search_hotels", warm))
        print(brief(ok, body), f"| breaker={await breaker_state(redis)}")

    await admin.aclose()
    await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", default=os.getenv("MCP_URL", "http://mcp:8000/mcp"))
    parser.add_argument("--supplier-url", default=os.getenv("SUPPLIER_URL", "http://supplier:8001"))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://redis:6379/0"))
    a = parser.parse_args()
    asyncio.run(main(a.mcp_url, a.supplier_url, a.redis_url))
