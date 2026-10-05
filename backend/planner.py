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

from backend.ai_parser import (DEFAULT_PREFERENCES, AIUnavailable, parse_instructions,
                                to_valhalla_settings)
from backend.routing import (DEFAULT_SETTINGS, RouteError, get_bike_route, get_local_pool,
                             get_road_details)
from backend.scoring import (explain_choice, explain_not_fastest, find_unmet_requests,
                             measure_route, score_breakdown, score_route)
from backend.stops import distance_km, find_stop

MAX_REROUTES = 3        # how many times we try to steer around an avoided street
MAX_AVOID_POINTS = 40   # don't send Valhalla an enormous list
MAX_STRAIGHT_LINE_KM = 140  # the free Valhalla server refuses routes over 150 km


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
        # Keep the most recent spots if the list gets long.
        avoid_points = (avoid_points + metrics["avoided_street_points"])[-MAX_AVOID_POINTS:]
        try:
            new_route = await get_bike_route(start, end, settings, avoid_points, stop)
            new_metrics = measure_route(await get_road_details(new_route["geometry"]),
                                        avoid_streets)
        except RouteError:
            # Blocking those spots made the trip impossible (e.g. the destination is ON
            # the avoided street). Keep the last good route; a warning will explain.
            break
        route, metrics = new_route, new_metrics

    route["name"] = name
    route["description"] = description
    route["metrics"] = metrics
    return route


def describe_route(route, routes, best):
    """A clear name for a route card, based on what the route actually is."""
    if route is best and route["name"] == "Standard":
        return "Best match for you", "Same as the typical route: it already fits your request"
    if route is best:
        return "Best match for you", "Lowest score for what you asked for"
    if route["name"] == "Standard":
        return "Typical route", "What a regular map app would give you, for comparison"

    # Otherwise, name it after what it does best among all the options.
    m = route["metrics"]
    if route["duration_minutes"] == min(r["duration_minutes"] for r in routes):
        return "Fastest", "Quickest option"
    if m["busy_road_miles"] == min(r["metrics"]["busy_road_miles"] for r in routes):
        return "Calmest", "Fewest miles on busy roads"
    if m["climb_ft"] == min(r["metrics"]["climb_ft"] for r in routes):
        return "Flattest", "Least climbing"
    if m["bike_lane_miles"] == max(r["metrics"]["bike_lane_miles"] for r in routes):
        return "Most bike lanes", "Most miles on bike lanes and paths"
    return "Alternative", "Another option built from your request"


async def plan_ride(start, end, instructions, previous_prefs=None):
    # Quick sanity checks before doing any slow work.
    straight_line = distance_km(start, end)
    if straight_line < 0.05:
        raise RouteError("Your start and destination are the same place.")
    if straight_line > MAX_STRAIGHT_LINE_KM:
        raise RouteError("That trip is too long. Wayfinder plans bike rides up to about 90 miles.")

    # Step 1: AI turns words into preferences.
    # If the AI is down, still give the rider a route with normal settings, and say so.
    ai_warning = None
    try:
        prefs = await parse_instructions(instructions, previous_prefs)
    except AIUnavailable:
        prefs = (previous_prefs or DEFAULT_PREFERENCES).model_copy(
            update={"understood": "The AI is busy, so this route uses "
                    + ("your previous settings." if previous_prefs else "normal settings.")})
        ai_warning = ("Couldn't reach the AI to read your instructions. "
                      "Try again in a moment to get a route tailored to them.")
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

    # Step 2 + 3: candidate routes, built at the same time to save waiting.
    # "Standard" is what an ordinary map app would give: our baseline to compare against.
    jobs = [
        build_candidate("Standard", "A typical bike route, ignoring your request",
                        start, end, stop, standard_settings, prefs.avoid_streets, reroute=False),
        build_candidate("Tailored", "Built from your instructions",
                        start, end, stop, ai_settings, prefs.avoid_streets),
        build_candidate("Extra tailored", "Your instructions, taken even further",
                        start, end, stop, stronger_settings, prefs.avoid_streets),
    ]
    # With our OWN routing engine, extra routes cost milliseconds, so explore more
    # strategies (the extremes of Valhalla's dials) and let the scoring pick.
    # On the free public server we stick to 3, to stay within its limits.
    if get_local_pool():
        for use_roads, use_hills in [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]:
            jobs.append(build_candidate(
                "Explore", "Another strategy", start, end, stop,
                {**ai_settings, "use_roads": use_roads, "use_hills": use_hills},
                prefs.avoid_streets))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            print(f"[planner] a candidate route failed: {result}")
    candidates = [r for r in results if not isinstance(r, Exception)]
    if not candidates:
        first_error = results[0]
        raise first_error if isinstance(first_error, RouteError) else RouteError(str(first_error))

    # Different settings sometimes produce the same path (sometimes with tiny coordinate
    # differences). Treat routes with the same length, time, climb, and traffic as one.
    def fingerprint(route):
        m = route["metrics"]
        return (route["duration_minutes"], round(route["distance_miles"], 1),
                m["climb_ft"], round(m["busy_road_miles"], 1), m["bike_lane_percent"])

    unique = []
    for route in candidates:
        if all(fingerprint(route) != fingerprint(other) for other in unique):
            unique.append(route)

    # Step 4: score every candidate with the rider's preferences.
    for route in unique:
        route["score"] = score_route(route, route["metrics"], prefs)
        route["score_parts"] = {part: round(value, 1) for part, value
                                in score_breakdown(route, route["metrics"], prefs).items()}

    # Step 5: lowest score wins. Explain it by comparing with the standard route.
    best = min(unique, key=lambda r: r["score"])
    standard = next((r for r in candidates if r["name"] == "Standard"), None)
    if standard:
        explanation = explain_choice(best, standard, prefs)
    else:
        explanation = ["Couldn't build a standard route to compare against this time."]
    fastest = min(unique, key=lambda r: r["duration_minutes"])
    not_fastest = explain_not_fastest(best, fastest, prefs)
    if not_fastest:
        explanation.append(not_fastest)
    warnings = find_unmet_requests(best, prefs, unique)  # be honest about what we couldn't do
    if ai_warning:
        warnings.insert(0, ai_warning)
    if stop:
        explanation.insert(0, f"{stop['emoji']} stops at {stop['name']} on the way")
    elif prefs.stop_type != "none":
        explanation.insert(0, f"Couldn't find a {prefs.stop_type.replace('_', ' ')} stop near your route")

    # Tidy up what we send to the website, and give each route a clear name.
    for route in unique:
        route["metrics"].pop("avoided_street_points", None)
        route["is_best"] = route is best
        route["is_typical"] = route["name"] == "Standard"
        route["label"], route["description"] = describe_route(route, unique, best)

    # Show at most 4 options: the best, the typical route (for comparison), and up to
    # two others that each stand out for something (Fastest, Calmest, Flattest...).
    routes = [best]
    typical = next((r for r in unique if r["is_typical"] and r is not best), None)
    if typical:
        routes.append(typical)
    for route in sorted(unique, key=lambda r: r["score"]):
        if len(routes) >= 4:
            break
        if route in routes or route["label"] == "Alternative":
            continue
        if route["label"] not in [r["label"] for r in routes]:
            routes.append(route)

    return {
        "understood": prefs.understood,
        "preferences": prefs.model_dump(),
        "explanation": explanation,
        "warnings": warnings,
        "routes": routes,
        "stop": stop,
    }
