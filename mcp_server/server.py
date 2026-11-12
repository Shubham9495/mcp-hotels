"""MCP tool definitions - a thin layer over `HotelService`.

Run:
  uv run mcp-hotels                 # stdio (Claude Desktop / Claude Code)
  uv run mcp-hotels --http          # streamable HTTP on :8000/mcp
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Coroutine
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from mcp_server import metrics as m
from mcp_server.breaker import CircuitBreaker
from mcp_server.cache import RedisCache
from mcp_server.config import SUPPORTED_CITIES, Settings
from mcp_server.metrics import Metrics
from mcp_server.ratelimit import RateLimiter
from mcp_server.redis_client import make_redis
from mcp_server.search import HotelNotFound, HotelService, SupplierClient, SupplierError, SupplierUnavailable
from mcp_server.shedding import LoadShedder
from mcp_server.validation import InvalidInput, normalize_city, normalize_hotel_id, validate_stay

# Plain, cheap logging to stderr (stdout belongs to the stdio transport). This must run before
# MCPServer() so the SDK's pretty RichHandler is not installed: rendering rich tracebacks for
# every failed call during a spike was measured to pin the event loop at 100 % CPU.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logging.getLogger("mcp").setLevel(os.getenv("MCP_SDK_LOG_LEVEL", "WARNING"))
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("mcp_hotels")

CITIES = ", ".join(SUPPORTED_CITIES)

mcp = MCPServer(
    log_level="WARNING",  # also silences uvicorn's per-request access log in HTTP mode
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
    """Process-wide singletons, created on first use (works for stdio and HTTP alike)."""

    service: HotelService | None = None
    limiter: RateLimiter | None = None

    @classmethod
    def _build(cls) -> None:
        settings = Settings()
        redis = make_redis(settings.redis_url)
        cls.service = HotelService(
            settings,
            SupplierClient(settings.supplier_url, settings.supplier_timeout_s),
            RedisCache(redis, settings.stale_ttl_s),
            Metrics(redis),
            CircuitBreaker(
                redis, "supplier", settings.breaker_failure_threshold,
                settings.breaker_window_s, settings.breaker_cooldown_s,
            ),
            LoadShedder(settings.supplier_max_inflight),
        )
        cls.limiter = RateLimiter(redis, settings.rate_capacity, settings.rate_refill_per_s)

    @classmethod
    def get(cls) -> HotelService:
        if cls.service is None:
            cls._build()
        assert cls.service is not None
        return cls.service

    @classmethod
    def rate_limiter(cls) -> RateLimiter:
        if cls.limiter is None:
            cls._build()
        assert cls.limiter is not None
        return cls.limiter


SERVICE = _ServiceHolder

# Bookings are never rate limited: they are the revenue the whole system exists for.
RATE_LIMITED_TOOLS = frozenset({"search_hotels", "get_hotel_details", "check_live_price"})

UNAVAILABLE_MESSAGES: dict[str, str] = {
    "search_hotels": "No cached results exist for this exact search and the supplier can't be reached",
    "get_hotel_details": "Hotel details aren't cached and the supplier can't be reached",
    "check_live_price": "The live price can't be confirmed right now - do not book on a cached price",
    "book_hotel": "The booking could not be completed and nothing was booked",
}


def client_id_from(ctx: Context) -> str:
    """Rate-limit identity: X-Client-Id, else MCP session id, else caller IP (Cloudflare header or socket), else 'stdio'."""
    headers = ctx.headers or {}
    if cid := headers.get("x-client-id"):
        return cid[:64]
    if sid := headers.get("mcp-session-id"):
        return f"session:{sid[:64]}"
    if cf_ip := headers.get("cf-connecting-ip"):  # real caller IP when reached via a Cloudflare tunnel
        return f"ip:{cf_ip[:64]}"
    request = getattr(ctx.request_context, "request", None)
    client = getattr(request, "client", None)
    if client is not None and getattr(client, "host", None):
        return f"ip:{client.host}"
    return "stdio"


async def run_tool(tool: str, ctx: Context, coro: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """Count, rate-limit, run, and translate service exceptions into friendly tool errors."""
    metrics = SERVICE.get().metrics
    await metrics.incr_many(m.MCP_REQUESTS, f"{m.MCP_REQUESTS}:{tool}")

    if tool in RATE_LIMITED_TOOLS:
        decision = await SERVICE.rate_limiter().acquire(client_id_from(ctx))
        if not decision.allowed:
            coro.close()
            await metrics.incr(m.RATE_LIMITED)
            raise ToolError(
                f"Rate limited: too many requests from this client. Retry after {decision.retry_after_whole_s} s."
            )
    try:
        return await coro
    except InvalidInput as exc:
        raise ToolError(str(exc)) from None
    except HotelNotFound:
        raise ToolError("No hotel with that id. Use search_hotels to find valid ids (e.g. 'GOA-001').") from None
    except SupplierUnavailable as exc:
        raise ToolError(
            f"{UNAVAILABLE_MESSAGES[tool]} ({exc.reason}). Retry after {exc.retry_after_s} s."
        ) from None
    except SupplierError as exc:
        raise ToolError(f"{UNAVAILABLE_MESSAGES[tool]} ({exc}). Please try again shortly.") from None
    except Exception as exc:  # never let an internal hiccup become a traceback storm in the logs
        log.warning("tool %s failed unexpectedly: %s: %s", tool, type(exc).__name__, exc)
        raise ToolError("Temporary internal error - please retry in a moment.") from None


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

    return await run_tool("search_hotels", ctx, go())


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

    return await run_tool("get_hotel_details", ctx, go())


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

    return await run_tool("check_live_price", ctx, go())


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

    return await run_tool("book_hotel", ctx, go())


def http_app() -> Starlette:
    """ASGI app factory for streamable HTTP (used by uvicorn workers: --factory)."""
    settings = Settings()
    return mcp.streamable_http_app(
        stateless_http=True,   # no sticky sessions -> any worker/replica can serve any request
        json_response=True,    # plain JSON responses: simplest for load tests and proxies
        host=settings.http_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in settings.allowed_hosts] + settings.extra_allowed_origins,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="mcp-hotels MCP server (mock data)")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default=None, help="HTTP bind host (default from MCP_HTTP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default from MCP_HTTP_PORT)")
    parser.add_argument("--workers", type=int, default=None, help="HTTP worker processes (default MCP_WORKERS or 1)")
    args = parser.parse_args()
    if not args.http:
        mcp.run("stdio")
        return
    settings = Settings()
    # Each worker is a separate process. Cache, rate limits, breaker and metrics live in Redis,
    # so they are shared; single-flight coalescing and load shedding are per worker.
    uvicorn.run(
        "mcp_server.server:http_app",
        factory=True,
        host=args.host or settings.http_host,
        port=args.port or settings.http_port,
        workers=args.workers or int(os.getenv("MCP_WORKERS", "1")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
