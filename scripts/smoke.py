"""End-to-end smoke test: call every tool through a real MCP client.

  uv run python scripts/smoke.py                    # HTTP, http://localhost:8000/mcp
  uv run python scripts/smoke.py --stdio            # spawn the server over stdio
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta
from typing import Any

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters


def next_friday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(4 - today.weekday()) % 7 or 7)


def show(label: str, result: Any) -> dict[str, Any] | None:
    text = "".join(getattr(c, "text", "") for c in result.content)
    flag = "ERROR" if result.is_error else "ok"
    print(f"\n=== {label} [{flag}]")
    print(text[:900])
    return None if result.is_error else json.loads(text)


async def run(target: str | StdioServerParameters) -> None:
    fri = next_friday()
    ci, co = fri.isoformat(), (fri + timedelta(days=2)).isoformat()
    async with Client(target) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])
        found = show("search_hotels Goa", await client.call_tool("search_hotels", {
            "city": "goa", "check_in": ci, "check_out": co, "guests": 2, "max_price_inr": 6000, "min_stars": 4,
        }))
        hotel_id = found["results"][0]["id"] if found and found["results"] else "GOA-001"
        show("get_hotel_details", await client.call_tool("get_hotel_details", {"hotel_id": hotel_id}))
        show("check_live_price", await client.call_tool("check_live_price", {
            "hotel_id": hotel_id, "check_in": ci, "check_out": co, "guests": 2}))
        show("book_hotel", await client.call_tool("book_hotel", {
            "hotel_id": hotel_id, "check_in": ci, "check_out": co, "guests": 2, "guest_name": "Demo User"}))
        show("bad city (expected error)", await client.call_tool("search_hotels", {
            "city": "Paris", "check_in": ci, "check_out": co}))
        show("bad dates (expected error)", await client.call_tool("search_hotels", {
            "city": "Goa", "check_in": co, "check_out": ci}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000/mcp")
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    target: str | StdioServerParameters = args.url
    if args.stdio:
        target = StdioServerParameters(command="uv", args=["run", "--no-sync", "mcp-hotels"])
    asyncio.run(run(target))


if __name__ == "__main__":
    main()
