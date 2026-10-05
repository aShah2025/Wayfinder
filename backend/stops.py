"""
stops.py: finds a place to stop on the way, like "grab coffee on the way".

The AI only says WHAT kind of stop (e.g. "coffee"). This file finds WHICH place:
it searches OpenStreetMap (through the free Overpass API) for every matching
place in the area, then picks the one that adds the smallest detour.
"""
import math

from backend.web import ServiceUnavailable, request_json

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Each stop type -> the OpenStreetMap tag that marks those places, plus an emoji.
STOP_TYPES = {
    "coffee":    {"tag": '["amenity"="cafe"]', "emoji": "☕"},
    "food":      {"tag": '["amenity"~"^(restaurant|fast_food)$"]', "emoji": "🍔"},
    "water":     {"tag": '["amenity"="drinking_water"]', "emoji": "🚰"},
    "restroom":  {"tag": '["amenity"="toilets"]', "emoji": "🚻"},
    "bike_shop": {"tag": '["shop"="bicycle"]', "emoji": "🔧"},
    "park":      {"tag": '["leisure"="park"]', "emoji": "🌳"},
    "grocery":   {"tag": '["shop"~"^(supermarket|convenience)$"]', "emoji": "🛒"},
}

# Unnamed water fountains and restrooms are still useful; other places need a name.
UNNAMED_OK = {"water", "restroom"}


def distance_km(a, b):
    """Straight-line distance between two {"lat", "lon"} points (haversine formula)."""
    lat1, lon1, lat2, lon2 = map(math.radians, [a["lat"], a["lon"], b["lat"], b["lon"]])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def detour_km(start, place, end):
    """How much farther it is to go start -> place -> end instead of straight there."""
    return distance_km(start, place) + distance_km(place, end) - distance_km(start, end)


async def find_stop(start, end, stop_type):
    """Return the best place of this type between start and end, or None."""
    if stop_type not in STOP_TYPES:
        return None

    # Search a box around the trip, with a little extra space on each side.
    margin = 0.01  # about 1 km
    south = min(start["lat"], end["lat"]) - margin
    north = max(start["lat"], end["lat"]) + margin
    west = min(start["lon"], end["lon"]) - margin
    east = max(start["lon"], end["lon"]) + margin

    # Overpass's query language: "find nodes, ways, or relations (nwr) with this tag
    # in this box, and give me their center points".
    query = (f'[out:json][timeout:15];'
             f'nwr{STOP_TYPES[stop_type]["tag"]}({south},{west},{north},{east});'
             f'out center 200;')

    try:
        status, data = await request_json("POST", OVERPASS_URL, data={"data": query},
                                          timeout=20, attempts=2)
    except ServiceUnavailable:
        return None  # the stop is a bonus, so don't break the whole route if this fails
    if status != 200:
        return None
    elements = data.get("elements", [])

    best = None
    for element in elements:
        point = element.get("center", element)  # ways/relations put their point in "center"
        if "lat" not in point:
            continue
        name = element.get("tags", {}).get("name")
        if not name and stop_type not in UNNAMED_OK:
            continue

        place = {"lat": point["lat"], "lon": point["lon"]}
        if distance_km(start, place) < 0.3:
            continue  # right next to the start isn't really "on the way"
        extra = detour_km(start, place, end)
        if best is None or extra < best["detour_km"]:
            best = {
                **place,
                "name": name or stop_type.replace("_", " ").title(),
                "type": stop_type,
                "emoji": STOP_TYPES[stop_type]["emoji"],
                "detour_km": round(extra, 2),
            }
    return best
