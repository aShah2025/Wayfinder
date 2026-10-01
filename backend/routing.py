"""
routing.py: gets bike routes from Valhalla.

Valhalla is a free, open-source routing engine that uses OpenStreetMap data
(a map of every road, path and bike lane, made by volunteers worldwide).
We send it a start, an end, and bike SETTINGS (how much to avoid hills,
how much to avoid busy roads...) and it finds the best path for those settings.

The settings are the important part: later, the AI will choose them based on
what the rider asks for.
"""
import os

import httpx

# The public Valhalla server. It's free for light use; the URL can be changed
# in .env if we ever run our own copy.
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://valhalla1.openstreetmap.de")

# Some servers ask apps to identify themselves politely.
HEADERS = {"User-Agent": "Wayfinder (ImpactHack student project)"}

# Default bike settings: a normal rider on a normal bike.
# use_hills / use_roads go from 0.0 (avoid as much as possible) to 1.0 (don't care).
DEFAULT_SETTINGS = {
    "bicycle_type": "Hybrid",   # Road, Hybrid, Cross, or Mountain
    "cycling_speed": 16,        # km/h (Valhalla's unit), about 10 mph
    "use_hills": 0.5,
    "use_roads": 0.5,
    "avoid_bad_surfaces": 0.25,
}


class RouteError(Exception):
    """Raised when Valhalla can't find a route (e.g. a point is in the ocean)."""


def decode_polyline(encoded, precision=6):
    """
    Valhalla squeezes the route's thousands of points into one short string
    (an "encoded polyline") to save space. This turns it back into a list of
    [longitude, latitude] points we can draw on the map.

    How it works: each number is stored as the DIFFERENCE from the previous
    point, packed into 5-bit chunks that are written as letters.
    """
    points = []
    index = lat = lon = 0
    factor = 10 ** precision

    while index < len(encoded):
        # Read two numbers in a row: first the latitude change, then longitude.
        changes = []
        for _ in range(2):
            result = shift = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:  # this was the last chunk of the number
                    break
            # The lowest bit says whether the number is negative.
            change = ~(result >> 1) if result & 1 else result >> 1
            changes.append(change)

        lat += changes[0]
        lon += changes[1]
        points.append([lon / factor, lat / factor])

    return points


async def get_bike_route(start, end, settings=None, avoid_points=None):
    """
    Ask Valhalla for a bike route from start to end.
    start and end look like {"lat": 37.33, "lon": -121.88}.
    avoid_points = optional list of [lon, lat] spots on roads the route must not use.
    Returns a simple dictionary the website can use.
    """
    bike_settings = {**DEFAULT_SETTINGS, **(settings or {})}

    request_body = {
        "locations": [
            {"lat": start["lat"], "lon": start["lon"]},
            {"lat": end["lat"], "lon": end["lon"]},
        ],
        "costing": "bicycle",
        "costing_options": {"bicycle": bike_settings},
        "directions_options": {"units": "miles"},
    }
    if avoid_points:
        # Valhalla snaps each point to the nearest road and won't use that road piece.
        request_body["exclude_locations"] = [
            {"lon": lon, "lat": lat} for lon, lat in avoid_points
        ]

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{VALHALLA_URL}/route", json=request_body, headers=HEADERS
        )

    data = response.json()
    if response.status_code != 200:
        raise RouteError(data.get("error", "Couldn't find a bike route."))

    trip = data["trip"]
    leg = trip["legs"][0]  # one "leg" = start to end with no stops in between

    return {
        "distance_miles": round(trip["summary"]["length"], 2),
        "duration_minutes": round(trip["summary"]["time"] / 60),
        "geometry": decode_polyline(leg["shape"]),
        "steps": [
            {
                "instruction": m["instruction"],
                "distance_miles": round(m["length"], 2),
            }
            for m in leg["maneuvers"]
        ],
        "settings_used": bike_settings,
        "encoded_shape": leg["shape"],  # kept so we can look up road details later
    }


async def get_road_details(encoded_shape):
    """
    Ask Valhalla about every road piece ("edge") along a route:
    what kind of road it is, whether it has a bike lane, how fast cars go,
    how steep it is, and its elevation.
    This is the raw data our scoring code uses to judge a route.
    """
    request_body = {
        "encoded_polyline": encoded_shape,
        "costing": "bicycle",
        "shape_match": "edge_walk",  # the shape came from Valhalla, so it matches exactly
        "filters": {
            "action": "include",
            "attributes": [
                "edge.length", "edge.names", "edge.road_class", "edge.use",
                "edge.cycle_lane", "edge.speed", "edge.mean_elevation",
                "edge.max_upward_grade", "edge.begin_shape_index",
                "edge.end_shape_index", "shape",
            ],
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{VALHALLA_URL}/trace_attributes", json=request_body, headers=HEADERS
        )
    data = response.json()
    if response.status_code != 200:
        raise RouteError(data.get("error", "Couldn't read road details."))

    return {"edges": data["edges"], "shape": decode_polyline(data["shape"])}
