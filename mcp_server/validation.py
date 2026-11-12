"""Input validation with friendly, model-readable error messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from mcp_server.config import SUPPORTED_CITIES

MAX_NIGHTS = 30
MAX_DAYS_AHEAD = 365
MAX_GUESTS = 8


class InvalidInput(ValueError):
    """Raised with a message meant to be shown to the model/user as-is."""


@dataclass(frozen=True)
class Stay:
    check_in: date
    check_out: date
    guests: int

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


def normalize_city(city: str) -> str:
    wanted = city.strip().lower()
    for supported in SUPPORTED_CITIES:
        if supported.lower() == wanted:
            return supported
    raise InvalidInput(
        f"Sorry, '{city}' isn't covered. Supported cities: {', '.join(SUPPORTED_CITIES)}."
    )


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise InvalidInput(f"{label} '{value}' is not a valid date; use YYYY-MM-DD (e.g. 2026-11-20).") from None


def validate_stay(check_in: str, check_out: str, guests: int, today: date | None = None) -> Stay:
    today = today or date.today()
    ci = _parse_date(check_in, "check_in")
    co = _parse_date(check_out, "check_out")
    if ci < today:
        raise InvalidInput(f"check_in {ci} is in the past (today is {today}).")
    if (ci - today).days > MAX_DAYS_AHEAD:
        raise InvalidInput(f"check_in must be within {MAX_DAYS_AHEAD} days from today.")
    if co <= ci:
        raise InvalidInput("check_out must be after check_in.")
    if (co - ci).days > MAX_NIGHTS:
        raise InvalidInput(f"Stays are limited to {MAX_NIGHTS} nights.")
    if not 1 <= guests <= MAX_GUESTS:
        raise InvalidInput(f"guests must be between 1 and {MAX_GUESTS}.")
    return Stay(ci, co, guests)


def normalize_hotel_id(hotel_id: str) -> str:
    cleaned = hotel_id.strip().upper()
    if not cleaned or len(cleaned) > 20 or not all(c.isalnum() or c == "-" for c in cleaned):
        raise InvalidInput(f"'{hotel_id}' is not a valid hotel id (looks like 'GOA-001').")
    return cleaned
