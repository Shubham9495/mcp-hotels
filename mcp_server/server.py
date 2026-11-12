"""MCP tool definitions - a thin layer over `HotelService`.

Run:
  uv run mcp-hotels                 # stdio (Claude Desktop / Claude Code)
  uv run mcp-hotels --http          # streamable HTTP on :8000/mcp
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Coroutine
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from redis.asyncio import Redis

from mcp_server import metrics as m
from mcp_server.cache import RedisCache
from mcp_server.config import SUPPORTED_CITIES, Settings
from mcp_server.metrics import Metrics
from mcp_server.search import HotelNotFound, HotelService, SupplierClient, SupplierError
from mcp_server.validation import InvalidInput, normalize_city, normalize_hotel_id, validate_stay

log = logging.getLogger("mcp_hotels")

CITIES = ", ".join(SUPPORTED_CITIES)

mcp = MCPServer(
    name="mcp-hotels",
    title="Hotel Search (demo)",
    instructions=(
        "Demo hotel search for Indian leisure destinations. ALL DATA IS FICTIONAL. "
        f"Cities: {CITIES}. Prices are in INR per night. Dates are YYYY-MM-DD. "
        "Typical flow: search_hotels -> (optional) get_hotel_details -> check_live_price -> book_hotel. "
        "If a result has stale=true, tell the user the price is a cached price from price_as_of "
        "and confirm it with check_live_price before booking."
    ),
)


class _ServiceHolder:
    """Process-wide singleton, created on first use (works for stdio and HTTP alike)."""

    service: HotelService | None = None

    @classmethod
    def get(cls) -> HotelService:
        if cls.service is None:
            settings = Settings()
            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            cls.service = HotelService(
                settings,
                SupplierClient(settings.supplier_url, settings.supplier_timeout_s),
                RedisCache(redis, settings.stale_ttl_s),
                Metrics(redis),
            )
        return cls.service


SERVICE = _ServiceHolder


def client_id_from(ctx: Context) -> str:
    """Rate-limit identity: X-Client-Id header, else MCP session id, else remote IP, else 'stdio'."""
    headers = ctx.headers or {}
    if cid := headers.get("x-client-id"):
        return cid[:64]
    if sid := headers.get("mcp-session-id"):
        return f"session:{sid[:64]}"
    request = getattr(ctx.request_context, "request", None)
    client = getattr(request, "client", None)
    if client is not None and getattr(client, "host", None):
        return f"ip:{client.host}"
    return "stdio"


async def run_tool(tool: str, coro: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """Count the request, then translate service exceptions into friendly tool errors."""
    metrics = SERVICE.get().metrics
    await metrics.incr(m.MCP_REQUESTS)
    await metrics.incr(f"{m.MCP_REQUESTS}:{tool}")
    try:
        return await coro
    except InvalidInput as exc:
        raise ToolError(str(exc)) from None
    except HotelNotFound:
        raise ToolError("No hotel with that id. Use search_hotels to find valid ids (e.g. 'GOA-001').") from None
    except SupplierError as exc:
        raise ToolError(f"The hotel supplier is unavailable right now ({exc}). Please try again shortly.") from None


@mcp.tool(
    title="Search hotels",
    description=(
        "Search available hotels in one city for a date range. Returns up to `limit` hotels (max 10), "
        "cheapest first, each with id, name, area, stars, rating, price_per_night_inr, total_price_inr, "
        "price_as_of (UTC timestamp of the price) and stale (true = cached price served because the "
        f"supplier is unavailable). Supported cities: {CITIES}. Dates are YYYY-MM-DD; check_out is the "
        "departure day. Use max_price_inr for a per-night budget and min_stars for a star-rating floor. "
        "Search prices may be up to 10 minutes old; use check_live_price before booking."
    ),
)
async def search_hotels(
    city: str,
    check_in: str,
    check_out: str,
    ctx: Context,
    guests: int = 2,
    max_price_inr: int | None = None,
    min_stars: int | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        stay = validate_stay(check_in, check_out, guests)
        clean_city = normalize_city(city)
        if max_price_inr is not None and max_price_inr <= 0:
            raise InvalidInput("max_price_inr must be a positive number of rupees per night.")
        if min_stars is not None and not 1 <= min_stars <= 5:
            raise InvalidInput("min_stars must be between 1 and 5.")
        return await SERVICE.get().search(clean_city, stay, max_price_inr, min_stars, max(1, min(limit, 10)))

    return await run_tool("search_hotels", go())


@mcp.tool(
    title="Get hotel details",
    description=(
        "Get static details for one hotel by id (e.g. 'GOA-001'): name, city, area, stars, rating, "
        "amenities and a short description. Does not include prices."
    ),
)
async def get_hotel_details(hotel_id: str, ctx: Context) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return await SERVICE.get().details(normalize_hotel_id(hotel_id))

    return await run_tool("get_hotel_details", go())


@mcp.tool(
    title="Check live price",
    description=(
        "Get the real-time price for one hotel and stay directly from the supplier, never from cache. "
        "Use this right before booking, or when the user wants to confirm a price shown by search_hotels "
        "(especially if it was stale). Returns price_per_night_inr, total_price_inr and quoted_at."
    ),
)
async def check_live_price(
    hotel_id: str, check_in: str, check_out: str, ctx: Context, guests: int = 2
) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        stay = validate_stay(check_in, check_out, guests)
        return await SERVICE.get().live_price(normalize_hotel_id(hotel_id), stay)

    return await run_tool("check_live_price", go())


@mcp.tool(
    title="Book hotel (mock)",
    description=(
        "Book a hotel for the given stay. THIS IS A MOCK: it re-checks the live price and returns a fake "
        "confirmation id; no real reservation or payment happens. Only call this after the user has "
        "explicitly confirmed the hotel, dates, guests and the live price."
    ),
)
async def book_hotel(
    hotel_id: str, check_in: str, check_out: str, guest_name: str, ctx: Context, guests: int = 2
) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        stay = validate_stay(check_in, check_out, guests)
        name = guest_name.strip()
        if not name or len(name) > 100:
            raise InvalidInput("guest_name is required (max 100 characters).")
        return await SERVICE.get().book(normalize_hotel_id(hotel_id), stay, name)

    return await run_tool("book_hotel", go())


def main() -> None:
    parser = argparse.ArgumentParser(description="mcp-hotels MCP server (mock data)")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default=None, help="HTTP bind host (default from MCP_HTTP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default from MCP_HTTP_PORT)")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep per-request supplier calls out of the logs
    if not args.http:
        mcp.run("stdio")
        return
    settings = Settings()
    mcp.run(
        "streamable-http",
        host=args.host or settings.http_host,
        port=args.port or settings.http_port,
        stateless_http=True,   # no sticky sessions -> any replica can serve any request
        json_response=True,    # plain JSON responses: simplest for load tests and proxies
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in settings.allowed_hosts],
        ),
    )


if __name__ == "__main__":
    main()
