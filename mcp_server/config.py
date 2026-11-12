"""All configuration comes from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

SUPPORTED_CITIES: tuple[str, ...] = ("Goa", "Manali", "Jaipur", "Udaipur", "Kochi", "Rishikesh")


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    supplier_url: str = field(default_factory=lambda: os.getenv("SUPPLIER_URL", "http://localhost:8001"))
    supplier_timeout_s: float = field(default_factory=lambda: _env_float("SUPPLIER_TIMEOUT_S", 3.0))

    # cache
    search_ttl_s: int = field(default_factory=lambda: _env_int("CACHE_SEARCH_TTL_S", 600))
    details_ttl_s: int = field(default_factory=lambda: _env_int("CACHE_DETAILS_TTL_S", 86_400))
    stale_ttl_s: int = field(default_factory=lambda: _env_int("CACHE_STALE_TTL_S", 7 * 86_400))

    # rate limit (token bucket per client id)
    rate_capacity: int = field(default_factory=lambda: _env_int("RATE_LIMIT_BURST", 30))
    rate_refill_per_s: float = field(default_factory=lambda: _env_float("RATE_LIMIT_PER_S", 10.0))

    # circuit breaker
    breaker_failure_threshold: int = field(default_factory=lambda: _env_int("BREAKER_FAILURE_THRESHOLD", 5))
    breaker_window_s: int = field(default_factory=lambda: _env_int("BREAKER_WINDOW_S", 30))
    breaker_cooldown_s: int = field(default_factory=lambda: _env_int("BREAKER_COOLDOWN_S", 15))

    # load shedding: max concurrent supplier calls from this process
    supplier_max_inflight: int = field(default_factory=lambda: _env_int("SUPPLIER_MAX_INFLIGHT", 40))

    # HTTP transport
    http_host: str = field(default_factory=lambda: os.getenv("MCP_HTTP_HOST", "127.0.0.1"))
    http_port: int = field(default_factory=lambda: _env_int("MCP_HTTP_PORT", 8000))
    allowed_hosts: list[str] = field(
        default_factory=lambda: _env_list("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,mcp:*")
    )
