"""One Redis client per process, backed by a *blocking* pool.

Under a spike, a non-blocking pool raises MaxConnectionsError the moment every connection
is busy. A blocking pool makes the request wait (up to `timeout`) for a free connection
instead, which is what we want: Redis calls take ~0.2 ms, so waiting is cheap.
"""

from __future__ import annotations

import os

from redis.asyncio import BlockingConnectionPool, Redis


def make_redis(url: str) -> Redis:
    pool = BlockingConnectionPool.from_url(
        url,
        max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "200")),
        timeout=float(os.getenv("REDIS_POOL_TIMEOUT_S", "2")),
        socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_S", "2")),
        decode_responses=True,
    )
    return Redis(connection_pool=pool)
