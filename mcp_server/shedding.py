"""Priority load shedding for supplier calls (in-process).

When concurrent supplier calls pile up, low-value work is refused first so bookings always
have room:  book_hotel > check_live_price > search_hotels.

With max_inflight=40: searches may use up to 60 % of the slots, live-price checks up to 90 %,
bookings all of them. A shed search still falls back to the stale cache when it can.
"""

from __future__ import annotations

from enum import IntEnum


class Priority(IntEnum):
    SEARCH = 0
    LIVE_PRICE = 1
    BOOK = 2


SHARE_BY_PRIORITY: dict[Priority, float] = {
    Priority.SEARCH: 0.6,
    Priority.LIVE_PRICE: 0.9,
    Priority.BOOK: 1.0,
}


class LoadShedder:
    def __init__(self, max_inflight: int) -> None:
        self._max = max_inflight
        self._inflight = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    def limit_for(self, priority: Priority) -> int:
        return max(1, int(self._max * SHARE_BY_PRIORITY[priority]))

    def try_acquire(self, priority: Priority) -> bool:
        if self._inflight >= self.limit_for(priority):
            return False
        self._inflight += 1
        return True

    def release(self) -> None:
        self._inflight = max(0, self._inflight - 1)
