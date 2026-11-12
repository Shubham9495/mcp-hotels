"""Token bucket per client id, stored in Redis so every MCP replica shares the same budget.

The whole read-refill-take step runs as one Lua script, so concurrent requests from the
same client can never overspend the bucket.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis

KEY_PREFIX = "mh:rl:"

# KEYS[1] bucket hash; ARGV: capacity, refill_per_s, now_s, cost
# Returns {allowed (0/1), retry_after_s (string), tokens_left (string)}
TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 5)
return {allowed, tostring(retry_after), tostring(tokens)}
"""


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_s: float
    tokens_left: float

    @property
    def retry_after_whole_s(self) -> int:
        return max(1, math.ceil(self.retry_after_s))


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        capacity: int,
        refill_per_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._capacity = capacity
        self._refill_per_s = refill_per_s
        self._clock = clock
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    async def acquire(self, client_id: str, cost: float = 1.0) -> RateDecision:
        try:
            allowed, retry, left = await self._script(
                keys=[KEY_PREFIX + client_id],
                args=[self._capacity, self._refill_per_s, self._clock(), cost],
            )
        except Exception:
            # Fail open: if Redis is unreachable we'd rather serve than reject everyone.
            return RateDecision(True, 0.0, float(self._capacity))
        return RateDecision(bool(int(allowed)), float(retry), float(left))
