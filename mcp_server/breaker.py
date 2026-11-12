"""Circuit breaker around supplier calls, with its state in Redis.

Keeping the state in Redis means every MCP process (the HTTP server, Claude Desktop's stdio
process, extra replicas) trips and recovers together, and the dashboard can show it.

  CLOSED    -- failures >= threshold within window -->  OPEN
  OPEN      -- cooldown elapsed, first caller       -->  HALF_OPEN (that caller is the probe)
  HALF_OPEN -- probe succeeds                       -->  CLOSED
  HALF_OPEN -- probe fails                          -->  OPEN (cooldown restarts)
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from redis.asyncio import Redis


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerSnapshot:
    state: BreakerState
    opened_at: float | None
    recent_failures: int
    retry_in_s: float  # seconds until a probe is allowed (0 when not OPEN)


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        name: str,
        failure_threshold: int,
        window_s: int,
        cooldown_s: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._threshold = failure_threshold
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._state_key = f"mh:breaker:{name}"
        self._failures_key = f"mh:breaker:{name}:failures"
        self._probe_key = f"mh:breaker:{name}:probe"

    # ----- reads -----

    async def _load(self) -> tuple[BreakerState, float | None]:
        raw = await self._redis.hgetall(self._state_key)
        state = BreakerState(raw.get("state", BreakerState.CLOSED))
        opened_at = float(raw["opened_at"]) if raw.get("opened_at") else None
        return state, opened_at

    async def state(self) -> BreakerState:
        """Cheap read of the current state (one HGETALL)."""
        return (await self._load())[0]

    async def snapshot(self) -> BreakerSnapshot:
        state, opened_at = await self._load()
        now = self._clock()
        await self._redis.zremrangebyscore(self._failures_key, 0, now - self._window_s)
        failures = await self._redis.zcard(self._failures_key)
        retry_in = 0.0
        if state is BreakerState.OPEN and opened_at is not None:
            retry_in = max(0.0, opened_at + self._cooldown_s - now)
        return BreakerSnapshot(state, opened_at, failures, retry_in)

    # ----- decisions -----

    async def allow(self) -> bool:
        """May this call go to the supplier?"""
        state, opened_at = await self._load()
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN and opened_at is not None and self._clock() - opened_at < self._cooldown_s:
            return False
        # Cooldown over (or a half-open probe is stuck past its lease): exactly one caller wins the probe.
        won = await self._redis.set(self._probe_key, "1", nx=True, ex=self._cooldown_s)
        if won:
            await self._redis.hset(self._state_key, mapping={"state": BreakerState.HALF_OPEN})
        return bool(won)

    async def record_success(self) -> None:
        # Only the half-open probe closes the breaker; a slow call that started before the
        # breaker opened and happens to succeed late is not evidence of recovery.
        state, _ = await self._load()
        if state is BreakerState.HALF_OPEN:
            await self._close()

    async def record_failure(self) -> None:
        state, _ = await self._load()
        now = self._clock()
        if state is BreakerState.HALF_OPEN:
            await self._open(now)
            return
        if state is BreakerState.OPEN:
            return
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(self._failures_key, {f"{now}:{uuid.uuid4().hex[:8]}": now})
            pipe.zremrangebyscore(self._failures_key, 0, now - self._window_s)
            pipe.zcard(self._failures_key)
            pipe.expire(self._failures_key, self._window_s * 2)
            _, _, count, _ = await pipe.execute()
        if count >= self._threshold:
            await self._open(now)

    # ----- transitions -----

    async def _open(self, now: float) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(self._state_key, mapping={"state": BreakerState.OPEN, "opened_at": str(now)})
            pipe.delete(self._probe_key)
            await pipe.execute()

    async def _close(self) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(self._state_key, mapping={"state": BreakerState.CLOSED, "opened_at": ""})
            pipe.delete(self._failures_key, self._probe_key)
            await pipe.execute()

    async def reset(self) -> None:
        await self._redis.delete(self._state_key, self._failures_key, self._probe_key)
