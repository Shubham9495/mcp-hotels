"""AI traffic spike against the MCP server over streamable HTTP (stateless JSON-RPC POSTs).

Shape: 5 users for a few seconds, ramp to SPIKE_USERS (default 500) in ~20 s, hold, stop.
Most agents ask the same handful of popular questions (Goa this weekend, Manali in December),
a small share asks long-tail dates, and a few noisy bots hammer from one client id
so the rate limiter has something to do.

  make spike          (runs inside Docker, headless)
"""

from __future__ import annotations

import itertools
import json
import os
import random
from datetime import date, timedelta
from typing import Any

from locust import HttpUser, LoadTestShape, between, constant, task

MCP_PATH = os.getenv("MCP_PATH", "/mcp")
SPIKE_USERS = int(os.getenv("SPIKE_USERS", "500"))
RAMP_S = int(os.getenv("SPIKE_RAMP_S", "20"))
HOLD_S = int(os.getenv("SPIKE_HOLD_S", "60"))
AGENT_CLIENT_IDS = int(os.getenv("SPIKE_CLIENT_IDS", "100"))  # distinct AI platforms / agents

TODAY = date.today()
FRI = TODAY + timedelta(days=(4 - TODAY.weekday()) % 7 or 7)
DEC = date(TODAY.year if TODAY.month < 12 else TODAY.year + 1, 12, 20)


def stay(start: date, nights: int) -> tuple[str, str]:
    return start.isoformat(), (start + timedelta(days=nights)).isoformat()


# The "everyone is asking this" combinations: (city, check_in, check_out, max_price)
POPULAR: list[tuple[str, str, str, int | None]] = [
    ("Goa", *stay(FRI, 2), 6000),
    ("Goa", *stay(FRI, 2), None),
    ("Goa", *stay(FRI, 2), 5000),
    ("Manali", *stay(DEC, 3), None),
    ("Manali", *stay(DEC, 3), 8000),
    ("Jaipur", *stay(FRI + timedelta(days=7), 2), None),
]
CITIES = ["Goa", "Manali", "Jaipur", "Udaipur", "Kochi", "Rishikesh", "Pondicherry",
          "Thiruvananthapuram", "Ooty", "Munnar", "Shimla", "Darjeeling", "Coorg"]
_ids = itertools.count(1)


def rpc(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": next(_ids), "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


class McpUser(HttpUser):
    abstract = True
    client_id = "agent"

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"accept": "application/json, text/event-stream", "x-client-id": self.client_id}
        with self.client.post(MCP_PATH, json=rpc(tool, arguments), headers=headers,
                              name=tool, catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return None
            result = resp.json().get("result", {})
            if result.get("isError"):
                text = result["content"][0]["text"]
                resp.failure("rate limited" if "Rate limited" in text else text[:80])
                return None
            resp.success()
            return json.loads(result["content"][0]["text"])


class AiAgent(McpUser):
    """A typical AI assistant session: mostly searches, some price checks, rare bookings."""

    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        self.client_id = f"agent-{random.randrange(AGENT_CLIENT_IDS)}"
        self.last_hotel: tuple[str, str, str] | None = None

    @task(60)
    def popular_search(self) -> None:
        city, ci, co, max_price = random.choice(POPULAR)
        body = self.call("search_hotels", {"city": city, "check_in": ci, "check_out": co,
                                           "guests": 2, "max_price_inr": max_price})
        if body and body["results"]:
            self.last_hotel = (body["results"][0]["id"], ci, co)

    @task(6)
    def long_tail_search(self) -> None:
        ci, co = stay(TODAY + timedelta(days=random.randint(3, 120)), random.randint(1, 5))
        self.call("search_hotels", {"city": random.choice(CITIES), "check_in": ci, "check_out": co})

    @task(4)
    def details(self) -> None:
        if self.last_hotel:
            self.call("get_hotel_details", {"hotel_id": self.last_hotel[0]})

    @task(5)
    def live_price(self) -> None:
        if self.last_hotel:
            hotel_id, ci, co = self.last_hotel
            self.call("check_live_price", {"hotel_id": hotel_id, "check_in": ci, "check_out": co})

    @task(1)
    def book(self) -> None:
        if self.last_hotel and random.random() < 0.5:
            hotel_id, ci, co = self.last_hotel
            self.call("book_hotel", {"hotel_id": hotel_id, "check_in": ci, "check_out": co,
                                     "guest_name": "Load Test"})


class NoisyBot(McpUser):
    """A misbehaving client re-asking the same search in a tight loop -> gets rate limited."""

    fixed_count = 3
    wait_time = constant(0.05)
    client_id = "noisy-bot"

    @task
    def hammer(self) -> None:
        city, ci, co, max_price = POPULAR[0]
        self.call("search_hotels", {"city": city, "check_in": ci, "check_out": co, "max_price_inr": max_price})


class Spike(LoadTestShape):
    """5 users -> SPIKE_USERS within RAMP_S seconds, hold for HOLD_S, then stop."""

    warmup_s = 5

    def tick(self) -> tuple[int, float] | None:
        t = self.get_run_time()
        if t < self.warmup_s:
            return 5 + NoisyBot.fixed_count, 5
        if t < self.warmup_s + RAMP_S:
            target = 5 + (SPIKE_USERS - 5) * (t - self.warmup_s) / RAMP_S
            return int(target) + NoisyBot.fixed_count, max(25, SPIKE_USERS / RAMP_S * 1.5)
        if t < self.warmup_s + RAMP_S + HOLD_S:
            return SPIKE_USERS + NoisyBot.fixed_count, 100
        return None
