"""Generate supplier/data/hotels.json — entirely fictional hotels (deterministic, seeded).

Run: uv run python scripts/gen_hotels.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

CITIES: dict[str, dict[str, list[str]]] = {
    "Goa": {
        "areas": ["Calangute", "Baga", "Anjuna", "Candolim", "Palolem", "Panjim", "Colva", "Morjim"],
        "themes": ["Sands", "Tide", "Coconut Grove", "Sunset Shack", "Lagoon", "Konkan Breeze", "Saltwater", "Feni House"],
    },
    "Manali": {
        "areas": ["Old Manali", "Mall Road", "Vashisht", "Solang", "Aleo", "Prini"],
        "themes": ["Pinecrest", "Deodar Lodge", "Snowline", "Beas Riverside", "Apple Orchard", "Rohtang View", "Cedar Nest"],
    },
    "Jaipur": {
        "areas": ["C-Scheme", "Bani Park", "Amer Road", "MI Road", "Malviya Nagar", "Civil Lines"],
        "themes": ["Pink Courtyard", "Haveli", "Rajwada", "Jharokha", "Sandstone", "Lotus Bagh", "Mirror Palace"],
    },
    "Udaipur": {
        "areas": ["Lake Pichola", "Fateh Sagar", "Hanuman Ghat", "Ambamata", "Lal Ghat", "Shobhagpura"],
        "themes": ["Lakeview", "Ghat House", "Aravalli", "Marble Terrace", "City of Lakes", "Mewar Court", "Sajjan Retreat"],
    },
    "Kochi": {
        "areas": ["Fort Kochi", "Mattancherry", "Marine Drive", "Kakkanad", "Edappally", "Cherai"],
        "themes": ["Backwater", "Spice Route", "Chinese Nets", "Monsoon", "Malabar", "Coir House", "Harbour Light"],
    },
    "Rishikesh": {
        "areas": ["Tapovan", "Laxman Jhula", "Ram Jhula", "Shivpuri", "Swarg Ashram", "Muni Ki Reti"],
        "themes": ["Ganga Ghat", "Yoga Nest", "Riverstone", "Himalayan Song", "Ashram Garden", "Rapids", "Bodhi"],
    },
    # Added later. New cities go at the END so the hotels above stay identical (same seed, same order).
    "Pondicherry": {
        "code": ["PDY"],
        "areas": ["White Town", "Promenade Beach", "Auroville", "Serenity Beach", "Heritage Town", "Paradise Beach"],
        "themes": ["French Quarter", "Bougainvillea", "Promenade", "Auro Garden", "Seawall", "Rue Romain", "Tamil Courtyard"],
    },
    "Thiruvananthapuram": {
        "code": ["TVM"],
        "areas": ["Kovalam", "Varkala", "Vellayambalam", "Kowdiar", "Poovar", "Shanghumugham"],
        "themes": ["Lighthouse", "Cliffside", "Travancore", "Palm Lagoon", "Kovalam Bay", "Coconut Coast", "Anantha"],
    },
    "Ooty": {
        "code": ["OOT"],
        "areas": ["Charing Cross", "Fern Hill", "Lovedale", "Coonoor Road", "Ooty Lake", "Elk Hill"],
        "themes": ["Nilgiri", "Tea Garden", "Eucalyptus", "Blue Mountain", "Rose Garden", "Toy Train", "Misty Valley"],
    },
    "Munnar": {
        "code": ["MNR"],
        "areas": ["Chithirapuram", "Pallivasal", "Devikulam", "Anachal", "Pothamedu", "Mattupetty"],
        "themes": ["Tea Estate", "Cardamom Hills", "Cloud Nine", "Neelakurinji", "Plantation", "High Range", "Spice Trail"],
    },
    "Shimla": {
        "code": ["SML"],
        "areas": ["The Ridge", "Mall Road", "Jakhoo", "Kufri", "Chotta Shimla", "Mashobra"],
        "themes": ["Colonial", "Cedar Ridge", "Snow Peak", "Viceregal", "Pine Valley", "Himachal", "Toy Train"],
    },
    "Darjeeling": {
        "code": ["DJL"],
        "areas": ["Chowrasta", "Mall Road", "Ghoom", "Lebong", "Happy Valley", "Jalapahar"],
        "themes": ["Kanchenjunga", "Tea Garden", "Himalayan", "Toy Train", "Monastery View", "Tiger Hill", "Rhododendron"],
    },
    "Coorg": {
        "code": ["CRG"],
        "areas": ["Madikeri", "Virajpet", "Kushalnagar", "Somwarpet", "Abbey Falls", "Siddapur"],
        "themes": ["Coffee Estate", "Misty Hills", "Kodava", "Cauvery", "Pepper Vine", "Rainforest", "Plantation"],
    },
}

KINDS = ["Resort", "Hotel", "Boutique Stay", "Inn", "Retreat", "Suites", "Homestay"]
AMENITIES = [
    "Free Wi-Fi", "Breakfast included", "Swimming pool", "Spa", "Airport shuttle", "Gym",
    "Restaurant", "Bar", "Parking", "Room service", "Air conditioning", "Pet friendly", "Rooftop terrace",
]
BASE_BY_STARS = {2: (1200, 2200), 3: (2200, 4200), 4: (4000, 7500), 5: (7500, 16000)}
HOTELS_PER_CITY = 10


def make_hotel(rng: random.Random, city: str, idx: int, used_names: set[str]) -> dict[str, object]:
    spec = CITIES[city]
    while True:
        name = f"The {rng.choice(spec['themes'])} {rng.choice(KINDS)}"
        if name not in used_names:
            used_names.add(name)
            break
    stars = rng.choices([2, 3, 4, 5], weights=[1, 3, 4, 2])[0]
    lo, hi = BASE_BY_STARS[stars]
    area = rng.choice(spec["areas"])
    amenities = sorted(rng.sample(AMENITIES, k=3 + stars))
    rating = round(min(5.0, max(3.0, rng.gauss(3.4 + stars * 0.25, 0.3))), 1)
    return {
        "id": f"{spec.get('code', [city[:3].upper()])[0]}-{idx:03d}",
        "name": name,
        "city": city,
        "area": area,
        "stars": stars,
        "rating": rating,
        "base_price_inr": int(rng.randint(lo, hi) // 50 * 50),
        "amenities": amenities,
        "description": f"{name} is a {stars}-star property in {area}, {city}, rated {rating} by guests.",
    }


def main() -> None:
    rng = random.Random(42)
    hotels: list[dict[str, object]] = []
    for city in CITIES:
        used: set[str] = set()
        hotels.extend(make_hotel(rng, city, i + 1, used) for i in range(HOTELS_PER_CITY))
    out = Path(__file__).resolve().parent.parent / "supplier" / "data" / "hotels.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(hotels, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(hotels)} hotels to {out}")


if __name__ == "__main__":
    main()
