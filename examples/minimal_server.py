"""Minimal MCP server template: a restaurant-table finder with in-memory data.

A complete, working MCP server in one file. Copy it to start a new MCP app.
Explained in docs/MCP-GUIDE.md.

Run (stdio):  uv run python examples/minimal_server.py
Try it:       uv run python examples/try_minimal_server.py
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

# 1. Create the server: its name and instructions are what the AI reads first.
mcp = MCPServer(
    name="restaurants",
    instructions="Find and reserve restaurant tables in Bengaluru. Use these tools for any restaurant question.",
)

# 2. Your business data/logic: plain Python, nothing MCP-specific.
RESTAURANTS = [
    {"id": "BLR-001", "name": "Spice Garden", "cuisine": "Indian", "price_for_two": 1200},
    {"id": "BLR-002", "name": "Pasta Street", "cuisine": "Italian", "price_for_two": 1800},
]


# 3. Tools: a typed Python function + a description written for the AI.
@mcp.tool(
    title="Find restaurants",
    annotations=ToolAnnotations(read_only_hint=True),
    description="Find restaurants by cuisine (e.g. 'Indian') and a max price for two in INR.",
)
async def find_restaurants(cuisine: str, max_price_for_two: int | None = None) -> dict[str, Any]:
    hits = [r for r in RESTAURANTS if r["cuisine"].lower() == cuisine.strip().lower()
            and (max_price_for_two is None or r["price_for_two"] <= max_price_for_two)]
    return {"results": hits, "count": len(hits)}


@mcp.tool(
    title="Reserve table",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    description="Reserve a table. Only call after the user confirms restaurant, date, time and party size.",
)
async def reserve_table(restaurant_id: str, date: str, time: str, people: int) -> dict[str, Any]:
    if not any(r["id"] == restaurant_id for r in RESTAURANTS):
        raise ToolError(f"No restaurant '{restaurant_id}'. Use find_restaurants to get valid ids.")
    return {"status": "CONFIRMED", "restaurant_id": restaurant_id, "date": date, "time": time, "people": people}


# 4. Transport: stdio for Claude Desktop (HTTP variant: see the explanation).
if __name__ == "__main__":
    mcp.run("stdio")
