"""
geocode.py: turns typed text like "Santana Row" into map coordinates.

"Geocoding" = going from a place name or address to a latitude/longitude.
We use Photon, a free search engine built on OpenStreetMap data.
"""
from backend.web import ServiceUnavailable, request_json

PHOTON_URL = "https://photon.komoot.io/api/"

# Only search inside California: [west, south, east, north] edges.
CALIFORNIA_BOX = "-124.48,32.53,-114.13,42.01"

# Prefer results near San Jose when there are several matches.
SAN_JOSE = {"lat": 37.3382, "lon": -121.8863}


def make_label(props):
    """Build a readable label like 'Santana Row, San Jose' from Photon's data."""
    street = " ".join(p for p in [props.get("housenumber"), props.get("street")] if p)
    parts = [props.get("name"), street, props.get("city")]

    label = []
    for part in parts:
        if part and part not in label:  # skip blanks and repeats
            label.append(part)
    return ", ".join(label)


def make_address(props):
    """The gray second line of a suggestion, like '1 Washington Square, San Jose'."""
    street = " ".join(p for p in [props.get("housenumber"), props.get("street")] if p)
    parts = [street, props.get("district") or props.get("locality"), props.get("city")]
    address = []
    for part in parts:
        if part and part not in address and part != props.get("name"):
            address.append(part)
    return ", ".join(address)


NEARBY_KM = 75  # results closer than this to the map's center are shown first


async def search_places(query, limit=5, near=None):
    """
    Return up to `limit` places matching the text, each with a label + coordinates.
    `near` = {"lat", "lon"} of where the rider is looking (defaults to San Jose).
    """
    from backend.stops import distance_km  # (imported here to avoid a circular import)

    near = near or SAN_JOSE
    params = {
        "q": query,
        "limit": limit + 3,  # ask for a few extra, since we drop repeats
        "bbox": CALIFORNIA_BOX,
        "lat": near["lat"],
        "lon": near["lon"],
        "lang": "en",
    }
    try:
        status, data = await request_json("GET", PHOTON_URL, params=params, timeout=10)
    except ServiceUnavailable:
        return []  # search is busy: show no suggestions instead of crashing
    if status != 200:
        return []

    results = []
    seen_labels = set()
    for feature in data.get("features", []):
        coordinates = feature.get("geometry", {}).get("coordinates")
        label = make_label(feature.get("properties", {}))
        if not coordinates or not label or label in seen_labels:
            continue  # skip broken entries and repeats (OSM often lists a place twice)
        seen_labels.add(label)

        lon, lat = coordinates
        props = feature.get("properties", {})
        results.append({
            "label": label,
            "name": props.get("name") or label.split(",")[0],
            "address": make_address(props),
            "lat": lat,
            "lon": lon,
        })

    # Keep the search engine's order, but move far-away matches (like a same-named
    # place 100 miles away) below the nearby ones.
    results.sort(key=lambda place: distance_km(near, place) > NEARBY_KM)
    return results[:limit]
