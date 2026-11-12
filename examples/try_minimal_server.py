"""Start examples/minimal_server.py over stdio, list its tools and call them (like an AI app would).

Run: uv run python examples/try_minimal_server.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

SERVER = Path(__file__).with_name("minimal_server.py")


async def main() -> None:
    async with Client(StdioServerParameters(command=sys.executable, args=[str(SERVER)])) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])

        result = await client.call_tool("find_restaurants", {"cuisine": "indian"})
        print("find_restaurants ->", result.content[0].text)

        result = await client.call_tool(
            "reserve_table", {"restaurant_id": "XYZ", "date": "2026-11-20", "time": "20:00", "people": 2}
        )
        print("reserve_table with a bad id -> error:" if result.is_error else "ok:", result.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
