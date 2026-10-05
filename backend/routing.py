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

from backend.web import ServiceUnavailable, request_json

# The public Valhalla server. It's free for light use; the URL can be changed
# in .env if we ever run our own copy.
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://valhalla1.openstreetmap.de")

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


async def call_valhalla(endpoint, request_body):
    """Send a request to Valhalla (with automatic retries). Returns (status, data)."""
    try:
        return await request_json("POST", f"{VALHALLA_URL}/{endpoint}",
                                  json=request_body, timeout=30)
    except ServiceUnavailable:
        raise RouteError("The free routing server is busy right now. "
                         "Please try again in a few seconds.")


def friendly_error(valhalla_message):
    """Turn Valhalla's technical error messages into something a rider understands."""
    message = valhalla_message.lower()
    if "distance" in message and ("exceed" in message or "limit" in message):
        return "That trip is too long. Wayfinder plans bike rides up to about 90 miles."
    if "no suitable edges" in message or "no path could be found" in message \
            or "locations are disconnected" in message:
        return "Couldn't find a bike route there. One of the points may not be near a road."
    return "Couldn't find a bike route: " + (valhalla_message or "unknown error")


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


async def get_bike_route(start, end, settings=None, avoid_points=None, stop=None):
    """
    Ask Valhalla for a bike route from start to end.
    start and end look like {"lat": 37.33, "lon": -121.88}.
    avoid_points = optional list of [lon, lat] spots on roads the route must not use.
    stop = optional place to visit on the way (from stops.py).
    Returns a simple dictionary the website can use.
    """
    bike_settings = {**DEFAULT_SETTINGS, **(settings or {})}

    locations = [{"lat": start["lat"], "lon": start["lon"]}]
    if stop:
        locations.append({"lat": stop["lat"], "lon": stop["lon"]})
    locations.append({"lat": end["lat"], "lon": end["lon"]})

    request_body = {
        "locations": locations,
        "costing": "bicycle",
        "costing_options": {"bicycle": bike_settings},
        "directions_options": {"units": "miles"},
    }
    if avoid_points:
        # Valhalla snaps each point to the nearest road and won't use that road piece.
        request_body["exclude_locations"] = [
            {"lon": lon, "lat": lat} for lon, lat in avoid_points
        ]

    status, data = await call_valhalla("route", request_body)
    if status != 200:
        raise RouteError(friendly_error(data.get("error", "")))

    trip = data["trip"]

    # A "leg" is one part of the trip: start -> end, or start -> stop and stop -> end.
    # Glue the legs together into one line and one list of steps.
    geometry = []
    steps = []
    for leg_number, leg in enumerate(trip["legs"]):
        points = decode_polyline(leg["shape"])
        geometry += points if leg_number == 0 else points[1:]  # skip the repeated joining point

        is_last_leg = leg_number == len(trip["legs"]) - 1
        for m in leg["maneuvers"]:
            steps.append({"instruction": m["instruction"], "distance_miles": round(m["length"], 2)})
        if not is_last_leg:
            steps[-1]["instruction"] = f"Arrive at your stop: {stop['name']}."

    return {
        "distance_miles": round(trip["summary"]["length"], 2),
        "duration_minutes": round(trip["summary"]["time"] / 60),
        "geometry": geometry,
        "steps": steps,
        "settings_used": bike_settings,
    }


async def get_road_details(geometry):
    """
    Ask Valhalla about every road piece ("edge") along a route:
    what kind of road it is, whether it has a bike lane, how fast cars go,
    how steep it is, and its elevation.
    This is the raw data our scoring code uses to judge a route.
    geometry = the route's list of [lon, lat] points.
    """
    request_body = {
        "shape": [{"lon": lon, "lat": lat} for lon, lat in geometry],
        "costing": "bicycle",
        # Try an exact match first; if that fails, snap the line to the nearest roads.
        "shape_match": "walk_or_snap",
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
    status, data = await call_valhalla("trace_attributes", request_body)
    if status != 200:
        raise RouteError("Couldn't read road details: " + data.get("error", "unknown error"))

    return {"edges": data["edges"], "shape": decode_polyline(data["shape"])}
