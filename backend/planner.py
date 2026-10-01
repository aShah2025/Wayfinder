"""
planner.py: the step-by-step recipe for planning a ride.

  1. AI reads the rider's words           -> preferences   (ai_parser.py)
     (+ find a stop on the way, if asked)  -> Overpass      (stops.py)
  2. Build several candidate routes        -> Valhalla      (routing.py)
  3. Re-route around streets to avoid      -> our own loop  (below)
  4. Measure + score every candidate       -> our own math  (scoring.py)
  5. Pick the lowest score and explain why -> real numbers  (scoring.py)
"""
import asyncio

from backend.ai_parser import parse_instructions, to_valhalla_settings
from backend.routing import DEFAULT_SETTINGS, RouteError, get_bike_route, get_road_details
from backend.scoring import explain_choice, measure_route, score_route
from backend.stops import find_stop

MAX_REROUTES = 3        # how many times we try to steer around an avoided street
MAX_AVOID_POINTS = 40   # don't send Valhalla an enormous list


def push_further(value):
    """Move a 0-1 setting halfway toward whichever end it's already leaning to."""
    if value < 0.5:
        return round(value / 2, 2)
    if value > 0.5:
        return round((value + 1) / 2, 2)
    return value


async def build_candidate(name, description, start, end, stop, settings, avoid_streets,
                          reroute=True):
    """Get one route, measure it, and (if reroute) re-route if it uses a street to avoid."""
    avoid_points = []
    route = await get_bike_route(start, end, settings, stop=stop)
    metrics = measure_route(await get_road_details(route["geometry"]), avoid_streets)

    # Our own avoidance loop: find the pieces of the route that are on an avoided
    # street, tell Valhalla to block those pieces, and ask again.
    for _ in range(MAX_REROUTES if reroute else 0):
        if not metrics["uses_avoided_street"]:
            break
        avoid_points = (avoid_points + metrics["avoided_street_points"])[:MAX_AVOID_POINTS]
        route = await get_bike_route(start, end, settings, avoid_points, stop)
        metrics = measure_route(await get_road_details(route["geometry"]), avoid_streets)

    route["name"] = name
    route["description"] = description
    route["metrics"] = metrics
    return route


async def plan_ride(start, end, instructions, previous_prefs=None):
    # Step 1: AI turns words into preferences.
    prefs = await parse_instructions(instructions, previous_prefs)
    ai_settings = to_valhalla_settings(prefs)

    # Every route goes through the same stop, so they can be compared fairly.
    stop = None
    if prefs.stop_type != "none":
        stop = await find_stop(start, end, prefs.stop_type)

    # The standard route uses the rider's own bike and speed (so times are comparable),
    # but ignores everything else they asked for.
    standard_settings = {
        **DEFAULT_SETTINGS,
        "bicycle_type": ai_settings["bicycle_type"],
        "cycling_speed": ai_settings["cycling_speed"],
    }

    stronger_settings = {
        **ai_settings,
        "use_hills": push_further(ai_settings["use_hills"]),
        "use_roads": push_further(ai_settings["use_roads"]),
    }

    # Step 2 + 3: three candidates, built at the same time to save waiting.
    # "Standard" is what an ordinary map app would give: our baseline to compare against.
    jobs = [
        build_candidate("Standard", "A typical bike route, ignoring your request",
                        start, end, stop, standard_settings, prefs.avoid_streets, reroute=False),
        build_candidate("Tailored", "Built from your instructions",
                        start, end, stop, ai_settings, prefs.avoid_streets),
        build_candidate("Extra tailored", "Your instructions, taken even further",
                        start, end, stop, stronger_settings, prefs.avoid_streets),
    ]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            print(f"[planner] a candidate route failed: {result}")
    candidates = [r for r in results if not isinstance(r, Exception)]
    if not candidates:
        first_error = results[0]
        raise first_error if isinstance(first_error, RouteError) else RouteError(str(first_error))

    # Different settings sometimes produce the exact same path. Keep only one copy.
    unique = []
    for route in candidates:
        if all(route["geometry"] != other["geometry"] for other in unique):
            unique.append(route)

    # Step 4: score every candidate with the rider's preferences.
    for route in unique:
        route["score"] = score_route(route, route["metrics"], prefs)

    # Step 5: lowest score wins. Explain it by comparing with the standard route.
    best = min(unique, key=lambda r: r["score"])
    standard = next((r for r in candidates if r["name"] == "Standard"), best)
    explanation = explain_choice(best, standard, prefs)
    if stop:
        explanation.insert(0, f"{stop['emoji']} stops at {stop['name']} on the way")
    elif prefs.stop_type != "none":
        explanation.insert(0, f"Couldn't find a {prefs.stop_type.replace('_', ' ')} stop near your route")

    # Tidy up what we send to the website.
    routes = []
    for route in unique:
        route["metrics"].pop("avoided_street_points", None)
        route["is_best"] = route is best
        routes.append(route)
    routes.sort(key=lambda r: r["score"])  # best first

    return {
        "understood": prefs.understood,
        "preferences": prefs.model_dump(),
        "explanation": explanation,
        "routes": routes,
        "stop": stop,
    }
