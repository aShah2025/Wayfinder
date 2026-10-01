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

KM_TO_MILES = 0.621371
METERS_TO_FEET = 3.28084

# Road types that usually carry lots of fast traffic.
BUSY_ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary"}
BUSY_SPEED_KMH = 56  # 35 mph or faster counts as busy

# Ways that are built for bikes.
BIKE_PATH_USES = {"cycleway", "path", "mountain_bike", "living_street"}
BIKE_LANE_TYPES = {"dedicated", "separated"}  # painted bike lane or protected bike lane


def simplify_name(name):
    """'Capitol Expressway' and 'capitol expy' should match, so normalize names."""
    name = name.lower().replace(".", "")
    short_forms = {
        "expressway": "expy", "avenue": "ave", "street": "st", "boulevard": "blvd",
        "road": "rd", "drive": "dr", "lane": "ln", "parkway": "pkwy", "highway": "hwy",
    }
    words = [short_forms.get(word, word) for word in name.split()]
    return " ".join(words)


def is_avoided_street(edge_names, avoid_streets):
    """True if this road piece is on one of the streets the rider wants to avoid."""
    for name in edge_names:
        for avoid in avoid_streets:
            a, b = simplify_name(name), simplify_name(avoid)
            if a == b or a in b or b in a:
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

    for edge in edges:
        length = edge.get("length", 0)
        total_km += length

        # Climbing: add up every time the road goes UP from one piece to the next.
        elevation = edge.get("mean_elevation")
        if elevation is not None and previous_elevation is not None and elevation > previous_elevation:
            climb_m += elevation - previous_elevation
        if elevation is not None:
            previous_elevation = elevation

        # Steepest hill: ignore tiny pieces, where elevation data is noisy.
        if length >= 0.05:
            steepest = max(steepest, edge.get("max_upward_grade", 0))

        is_bike_way = (edge.get("use") in BIKE_PATH_USES
                       or edge.get("cycle_lane") in BIKE_LANE_TYPES)
        is_busy = (edge.get("use") == "road"
                   and edge.get("cycle_lane") != "separated"
                   and (edge.get("road_class") in BUSY_ROAD_CLASSES
                        or edge.get("speed", 0) >= BUSY_SPEED_KMH))
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
    }


def score_route(route, metrics, prefs):
    """
    One number for how well a route fits this rider. LOWER is better.
    Each preference (0 to 1, set by the AI) controls how much a dislike "costs".
    """
    score = route["duration_minutes"] * (0.5 + prefs.speed_importance)

    # Climbing: if you hate hills, every 8 ft of climbing feels like an extra minute.
    score += prefs.avoid_hills * metrics["climb_ft"] / 8
    # Very steep pieces (over 5%) are extra painful.
    score += prefs.avoid_hills * max(0, metrics["steepest_grade"] - 5) * 4

    # Busy roads: up to 15 penalty minutes per mile.
    score += prefs.avoid_busy_roads * metrics["busy_road_miles"] * 15

    # Bike lanes and paths are a bonus: up to 5 minutes off per mile.
    score -= prefs.prefer_bike_lanes * metrics["bike_lane_miles"] * 5

    # Using a street the rider told us to avoid is basically disqualifying.
    if metrics["uses_avoided_street"]:
        score += 1000

    return round(score, 1)


def explain_choice(best, standard, prefs):
    """
    Explain the chosen route by comparing it with the standard route
    (what an ordinary map app would give). Built only from measured numbers.
    """
    if best is standard or best["geometry"] == standard["geometry"]:
        return ["The standard bike route already fits what you asked for."]

    b, s = best["metrics"], standard["metrics"]
    reasons = []

    climb_saved = s["climb_ft"] - b["climb_ft"]
    if prefs.avoid_hills >= 0.5 and climb_saved >= 10:
        reasons.append(f"{climb_saved} ft less climbing")
    if prefs.avoid_hills <= 0.2 and -climb_saved >= 10:
        reasons.append(f"{-climb_saved} ft more climbing for your workout")
    if b["steepest_grade"] < s["steepest_grade"] and prefs.avoid_hills >= 0.5:
        reasons.append(f"steepest hill is {b['steepest_grade']}% instead of {s['steepest_grade']}%")

    busy_saved = round(s["busy_road_miles"] - b["busy_road_miles"], 2)
    if busy_saved >= 0.1:
        reasons.append(f"{busy_saved} fewer miles on busy roads")

    lanes_gained = round(b["bike_lane_miles"] - s["bike_lane_miles"], 2)
    if lanes_gained >= 0.1:
        reasons.append(f"{lanes_gained} more miles on bike lanes and paths")

    if s["uses_avoided_street"] and not b["uses_avoided_street"]:
        reasons.append("stays off " + ", ".join(prefs.avoid_streets))

    extra_minutes = best["duration_minutes"] - standard["duration_minutes"]
    if extra_minutes > 0:
        reasons.append(f"costs {extra_minutes} extra min compared to the standard route")
    elif extra_minutes < 0:
        reasons.append(f"{-extra_minutes} min faster than the standard route")

    return reasons or ["Slightly better fit for your preferences than the standard route."]
