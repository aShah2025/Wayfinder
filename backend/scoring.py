"""
scoring.py: Wayfinder's own judgment. No AI in this file.

1. measure_route() looks at every road piece of a route and adds up facts:
   how much climbing, the steepest hill, miles on busy roads, miles on bike lanes.
2. score_route() turns those facts into ONE number using the rider's preferences.
   The unit is "penalty minutes": the real riding time, plus extra imaginary
   minutes for everything the rider dislikes. Lowest score wins.
3. explain_choice() writes the explanation from the real numbers, so it can't
   make anything up.
"""

import re

KM_TO_MILES = 0.621371
METERS_TO_FEET = 3.28084

# Road types that usually carry lots of fast traffic.
BUSY_ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary"}
BUSY_SPEED_LIMIT_KMH = 56  # a posted car speed limit of 35 mph or more counts as busy

# Ways that are built for bikes.
BIKE_PATH_USES = {"cycleway", "path", "mountain_bike", "living_street"}
BIKE_LANE_TYPES = {"dedicated", "separated"}  # painted bike lane or protected bike lane


HIGHWAY_WORDS = r"(?:highway|hwy|state route|state highway|route|sr|ca|us|interstate|i)"


def simplify_name(name):
    """'Capitol Expressway' and 'capitol expy' should match, so normalize names."""
    name = name.lower().replace(".", "")
    # "Highway 1", "CA 1", "SR-1", "US 101", "I-280" all become "rt 1", "rt 101"...
    name = re.sub(rf"\b{HIGHWAY_WORDS}[\s-]*(\d+)\b", r"rt \1", name)
    short_forms = {
        "expressway": "expy", "avenue": "ave", "street": "st", "boulevard": "blvd",
        "road": "rd", "drive": "dr", "lane": "ln", "parkway": "pkwy", "highway": "hwy",
    }
    words = [short_forms.get(word, word) for word in name.split()]
    return " ".join(words)


def is_avoided_street(edge_names, avoid_streets):
    """
    True if this road piece is on one of the streets the rider wants to avoid.
    Matches whole words only, so avoiding "1st St" doesn't also avoid "21st St",
    but avoiding "Capitol Ave" still catches "North Capitol Ave".
    """
    for name in edge_names:
        road = f" {simplify_name(name)} "
        for avoid in avoid_streets:
            avoided = simplify_name(avoid)
            if avoided and f" {avoided} " in road:
                return True
    return False


def measure_route(road_details, avoid_streets=()):
    """Add up facts about a route from its list of road pieces (edges)."""
    edges = road_details["edges"]
    shape = road_details["shape"]

    climb_m = 0
    steepest = 0
    busy_km = 0
    bike_km = 0
    total_km = 0
    avoided_street_points = []  # spots on streets the rider said to avoid
    previous_elevation = None
    elevation_profile = []      # [miles from start, height in feet] for the chart

    for edge in edges:
        length = edge.get("length", 0)
        total_km += length

        # Climbing: add up every time the road goes UP from one piece to the next.
        elevation = edge.get("mean_elevation")
        if elevation is not None and previous_elevation is not None and elevation > previous_elevation:
            climb_m += elevation - previous_elevation
        if elevation is not None:
            previous_elevation = elevation
            elevation_profile.append([round(total_km * KM_TO_MILES, 2),
                                      round(elevation * METERS_TO_FEET)])

        # Steepest hill: ignore tiny pieces, where elevation data is noisy.
        if length >= 0.05:
            steepest = max(steepest, edge.get("max_upward_grade", 0))

        is_bike_way = (edge.get("use") in BIKE_PATH_USES
                       or edge.get("cycle_lane") in BIKE_LANE_TYPES)
        is_busy = (edge.get("use") == "road"
                   and edge.get("cycle_lane") != "separated"
                   and (edge.get("road_class") in BUSY_ROAD_CLASSES
                        or (edge.get("speed_limit") or 0) >= BUSY_SPEED_LIMIT_KMH))
        if is_bike_way:
            bike_km += length
        if is_busy:
            busy_km += length

        # Is this piece on a street the rider wants to avoid? Remember a spot in its
        # middle (not at an intersection, so we don't block the cross street).
        if avoid_streets and is_avoided_street(edge.get("names", []), avoid_streets):
            begin = edge["begin_shape_index"]
            end = edge["end_shape_index"]
            middle = (begin + end) // 2
            a, b = shape[middle], shape[min(middle + 1, end)]
            avoided_street_points.append([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2])

    return {
        "climb_ft": round(climb_m * METERS_TO_FEET),
        "steepest_grade": steepest,
        "busy_road_miles": round(busy_km * KM_TO_MILES, 2),
        "bike_lane_miles": round(bike_km * KM_TO_MILES, 2),
        "bike_lane_percent": round(100 * bike_km / total_km) if total_km else 0,
        "uses_avoided_street": len(avoided_street_points) > 0,
        "avoided_street_points": avoided_street_points,
        "elevation_profile": shrink(elevation_profile, 80),
    }


def shrink(points, max_points):
    """Keep at most max_points evenly spaced points, so we don't send thousands to the chart."""
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points)] + [points[-1]]


def score_breakdown(route, metrics, prefs):
    """
    How well a route fits this rider, in "penalty minutes", split into parts so the
    app can SHOW where the score comes from. Each preference (set by the AI) controls
    how much a dislike "costs". Hills and bike lanes can be negative, which flips a
    penalty into a bonus (someone training WANTS climbing).
    """
    # When speed matters a lot ("I'm late"), the comfort penalties shrink so that
    # time can win. (speed_importance 1.0 -> comfort counts 40% as much.)
    comfort = 1 - 0.6 * prefs.speed_importance
    return {
        # Real riding time (counts more if the rider is in a hurry).
        "time": route["duration_minutes"] * (0.5 + 1.5 * prefs.speed_importance),
        # Hills: if you hate them, every 8 ft of climbing feels like an extra minute,
        # and very steep pieces are extra painful: 8 minutes per % over 5%
        # (a 20% wall on a cargo bike can mean getting off and pushing).
        "hills": comfort * prefs.avoid_hills * (metrics["climb_ft"] / 8
                                                + max(0, metrics["steepest_grade"] - 5) * 8),
        # Busy roads: up to 15 penalty minutes per mile.
        "traffic": comfort * prefs.avoid_busy_roads * metrics["busy_road_miles"] * 15,
        # Bike lanes: up to 5 minutes off per mile if you like them (negative = bonus),
        # up to 5 minutes added per mile if you want to avoid them.
        "bike_lanes": -comfort * prefs.prefer_bike_lanes * metrics["bike_lane_miles"] * 5,
        # Using a street the rider told us to avoid is basically disqualifying.
        "avoided_street": 1000 if metrics["uses_avoided_street"] else 0,
    }


def score_route(route, metrics, prefs):
    """One number for how well a route fits this rider. LOWER is better."""
    return round(sum(score_breakdown(route, metrics, prefs).values()), 1)


def explain_choice(best, standard, prefs):
    """
    Explain the chosen route by comparing it with the standard route
    (what an ordinary map app would give). Built only from measured numbers.
    Lists the good parts (what the rider asked for) AND the costs (tradeoffs).
    """
    if best is standard or best["geometry"] == standard["geometry"]:
        reasons = ["The typical route already fits your request best. "
                   "None of the alternatives scored better."]
        if prefs.avoid_streets and not best["metrics"]["uses_avoided_street"]:
            reasons.append("Stays off " + ", ".join(prefs.avoid_streets) + " ✓")
        return reasons

    b, s = best["metrics"], standard["metrics"]
    reasons = []
    tradeoffs = []

    # Hills
    climb_change = b["climb_ft"] - s["climb_ft"]
    if prefs.avoid_hills > 0 and climb_change <= -10:
        reasons.append(f"{-climb_change} ft less climbing")
    if prefs.avoid_hills < 0 and climb_change >= 10:
        reasons.append(f"{climb_change} ft more climbing for your workout")
    if prefs.avoid_hills > 0 and climb_change >= 10:
        tradeoffs.append(f"{climb_change} ft more climbing")
    if prefs.avoid_hills < 0 and climb_change <= -10:
        tradeoffs.append(f"{-climb_change} ft less climbing than the typical route")
    if prefs.avoid_hills >= 0.5 and b["steepest_grade"] < s["steepest_grade"]:
        reasons.append(f"steepest hill is {b['steepest_grade']}% instead of {s['steepest_grade']}%")

    # Busy roads (always worth mentioning: fewer is good, more is a tradeoff)
    busy_change = round(b["busy_road_miles"] - s["busy_road_miles"], 2)
    if busy_change <= -0.1:
        reasons.append(f"{-busy_change:.1f} fewer miles on busy roads")
    elif busy_change >= 0.1:
        tradeoffs.append(f"{busy_change:.1f} more miles on busy roads")

    # Bike lanes: only call a change "good" if it's the direction the rider wanted
    lane_change = round(b["bike_lane_miles"] - s["bike_lane_miles"], 2)
    if prefs.prefer_bike_lanes > 0 and lane_change >= 0.1:
        reasons.append(f"{lane_change:.1f} more miles on bike lanes and paths")
    if prefs.prefer_bike_lanes < 0 and lane_change <= -0.1:
        reasons.append(f"{-lane_change:.1f} fewer miles on bike lanes, as you asked")
    if prefs.prefer_bike_lanes > 0 and lane_change <= -0.1:
        tradeoffs.append(f"{-lane_change:.1f} fewer miles on bike lanes")
    if prefs.prefer_bike_lanes < 0 and lane_change >= 0.1:
        tradeoffs.append(f"{lane_change:.1f} more miles on bike lanes")

    if prefs.avoid_streets and not b["uses_avoided_street"]:
        reasons.append("Stays off " + ", ".join(prefs.avoid_streets) + " ✓")

    # Time
    minute_change = best["duration_minutes"] - standard["duration_minutes"]
    if minute_change < 0:
        reasons.append(f"{-minute_change} min faster than the typical route")
    elif minute_change > 0:
        tradeoffs.append(f"{minute_change} extra min compared to the typical route")

    if not reasons:
        reasons.append("Slightly better overall fit for your preferences than the typical route")
    return reasons + ["Tradeoff: " + t for t in tradeoffs]


def find_unmet_requests(best, prefs, all_routes=None):
    """
    Be honest when the chosen route still doesn't do what the rider asked,
    because sometimes no route can (e.g. the only way there is a bike path).
    Every claim is checked against all the options we found.
    Returns a list of warnings shown as "Heads up" in the app.
    """
    m = best["metrics"]
    others = [r for r in (all_routes or []) if r is not best]
    warnings = []

    if prefs.prefer_bike_lanes <= -0.3 and m["bike_lane_percent"] >= 20:
        fewer = [r for r in others if r["metrics"]["bike_lane_percent"] < m["bike_lane_percent"]]
        note = ("Options with fewer bike lanes had other costs (see below)." if fewer
                else "This was the lowest of the options found.")
        warnings.append(f"Couldn't fully avoid bike lanes: {m['bike_lane_percent']}% of this "
                        f"route still uses them. {note}")
    if prefs.prefer_bike_lanes >= 0.7 and m["bike_lane_percent"] < 30:
        warnings.append(f"Only {m['bike_lane_percent']}% of this route has bike lanes or paths. "
                        "There aren't many in this area.")
    if prefs.avoid_hills >= 0.7 and m["steepest_grade"] >= 8:
        gentler = [r for r in others if r["metrics"]["steepest_grade"] < m["steepest_grade"]]
        note = ("Gentler options had other costs." if gentler
                else "None of the options avoided it.")
        warnings.append(f"This route still has a {m['steepest_grade']}% hill. {note}")
    if prefs.avoid_busy_roads >= 0.7 and m["busy_road_miles"] >= 1:
        calmer = [r for r in others if r["metrics"]["busy_road_miles"] < m["busy_road_miles"]]
        note = ("Calmer options had other costs." if calmer
                else "It was the calmest option found.")
        warnings.append(f"This route still has {m['busy_road_miles']:.1f} miles on busy roads. "
                        f"{note}")
    if m["uses_avoided_street"]:
        warnings.append("Couldn't find a route that completely avoids "
                        + ", ".join(prefs.avoid_streets) + ".")
    return warnings


def explain_not_fastest(best, fastest, prefs):
    """
    If we didn't pick the quickest route, say why, using real numbers.
    (Without this, "I asked for faster and it picked a slower route" looks like a bug.)
    """
    saved = best["duration_minutes"] - fastest["duration_minutes"]
    if fastest is best or saved < 1:
        return None
    b, f = best["metrics"], fastest["metrics"]
    costs = []
    if f["uses_avoided_street"] and not b["uses_avoided_street"]:
        costs.append("uses a street you asked to avoid")
    if f["busy_road_miles"] - b["busy_road_miles"] >= 0.1:
        costs.append(f"has {f['busy_road_miles'] - b['busy_road_miles']:.1f} more miles on busy roads")
    if prefs.avoid_hills >= 0.5 and f["steepest_grade"] - b["steepest_grade"] >= 2:
        costs.append(f"has a {f['steepest_grade']}% hill (this one tops out at {b['steepest_grade']}%)")
    if prefs.avoid_hills > 0 and f["climb_ft"] - b["climb_ft"] >= 10:
        costs.append(f"has {f['climb_ft'] - b['climb_ft']} ft more climbing")
    if prefs.prefer_bike_lanes > 0 and b["bike_lane_miles"] - f["bike_lane_miles"] >= 0.1:
        costs.append(f"has {b['bike_lane_miles'] - f['bike_lane_miles']:.1f} fewer miles on bike lanes")
    if not costs:
        return None
    hint = ("" if prefs.speed_importance >= 0.9
            else " Say \"fastest possible\" if speed matters most.")
    return f"Why not the fastest? It saves {saved} min, but it " + " and ".join(costs) + "." + hint
