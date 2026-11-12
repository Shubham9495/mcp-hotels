"""Hotel service: everything between the MCP tools and the supplier."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import httpx

from mcp_server.config import Settings
from mcp_server.validation import Stay


class SupplierError(Exception):
    """Supplier call failed (5xx, timeout, connection error)."""


class HotelNotFound(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SupplierClient:
    """Raw HTTP calls to the (mock) supplier."""

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._http.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        except httpx.HTTPError as exc:
            raise SupplierError(f"supplier unreachable: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            raise HotelNotFound(path)
        if resp.status_code >= 500:
            raise SupplierError(f"supplier returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    async def search(self, city: str, stay: Stay, max_price: int | None) -> list[dict[str, Any]]:
        data = await self._get("/hotels/search", {
            "city": city, "check_in": stay.check_in.isoformat(), "check_out": stay.check_out.isoformat(),
            "guests": stay.guests, "max_price": max_price,
        })
        return data["results"]

    async def details(self, hotel_id: str) -> dict[str, Any]:
        return await self._get(f"/hotels/{hotel_id}")

    async def price(self, hotel_id: str, stay: Stay) -> dict[str, Any]:
        return await self._get(f"/hotels/{hotel_id}/price", {
            "check_in": stay.check_in.isoformat(), "check_out": stay.check_out.isoformat(), "guests": stay.guests,
        })


def to_search_result(raw: dict[str, Any], price_as_of: str, stale: bool) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "name": raw["name"],
        "area": raw["area"],
        "stars": raw["stars"],
        "rating": raw["rating"],
        "price_per_night_inr": raw["price_per_night_inr"],
        "total_price_inr": raw["total_price_inr"],
        "price_as_of": price_as_of,
        "stale": stale,
    }


class HotelService:
    def __init__(self, settings: Settings, supplier: SupplierClient) -> None:
        self.settings = settings
        self.supplier = supplier

    async def search(
        self, city: str, stay: Stay, max_price: int | None, min_stars: int | None, limit: int
    ) -> dict[str, Any]:
        raw = await self.supplier.search(city, stay, max_price)
        as_of = utc_now_iso()
        hotels = [h for h in raw if min_stars is None or h["stars"] >= min_stars][:limit]
        return {
            "city": city,
            "check_in": stay.check_in.isoformat(),
            "check_out": stay.check_out.isoformat(),
            "nights": stay.nights,
            "guests": stay.guests,
            "results": [to_search_result(h, as_of, stale=False) for h in hotels],
            "total_matches": len(raw),
            "stale": False,
        }

    async def details(self, hotel_id: str) -> dict[str, Any]:
        return await self.supplier.details(hotel_id)

    async def live_price(self, hotel_id: str, stay: Stay) -> dict[str, Any]:
        return await self.supplier.price(hotel_id, stay)

    async def book(self, hotel_id: str, stay: Stay, guest_name: str) -> dict[str, Any]:
        quote = await self.supplier.price(hotel_id, stay)
        return {
            "status": "CONFIRMED (MOCK - no real booking was made)",
            "confirmation_id": f"MOCK-{secrets.token_hex(4).upper()}",
            "hotel_id": hotel_id,
            "guest_name": guest_name,
            "check_in": stay.check_in.isoformat(),
            "check_out": stay.check_out.isoformat(),
            "guests": stay.guests,
            "total_price_inr": quote["total_price_inr"],
            "booked_at": utc_now_iso(),
        }
