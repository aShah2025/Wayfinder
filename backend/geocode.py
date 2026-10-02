"""
geocode.py: turns typed text like "Santana Row" into map coordinates.

"Geocoding" = going from a place name or address to a latitude/longitude.
We use Photon, a free search engine built on OpenStreetMap data.
"""
import httpx

PHOTON_URL = "https://photon.komoot.io/api/"
HEADERS = {"User-Agent": "Wayfinder (ImpactHack student project)"}

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


async def search_places(query, limit=5):
    """Return up to `limit` places matching the text, each with a label + coordinates."""
    params = {
        "q": query,
        "limit": limit,
        "bbox": CALIFORNIA_BOX,
        "lat": SAN_JOSE["lat"],
        "lon": SAN_JOSE["lon"],
        "lang": "en",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(PHOTON_URL, params=params, headers=HEADERS)
    response.raise_for_status()

    results = []
    seen_labels = set()
    for feature in response.json()["features"]:
        label = make_label(feature["properties"])
        if label in seen_labels:  # OpenStreetMap often has several entries for one place
            continue
        seen_labels.add(label)

        lon, lat = feature["geometry"]["coordinates"]
        results.append({"label": label, "lat": lat, "lon": lon})
    return results
