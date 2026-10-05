"""
Automated tests for Wayfinder's own logic (no internet or AI needed).

Run them from the Wayfinder folder with:   pytest
Each test_ function checks one thing. If any check is wrong, pytest shows exactly which.
"""
from backend.ai_parser import DEFAULT_PREFERENCES, clamp, to_valhalla_settings
from backend.routing import decode_polyline
from backend.scoring import (explain_choice, find_unmet_requests, is_avoided_street,
                             measure_route, score_route)


# ---------- helpers to build fake data ----------

def make_edge(length_km=1.0, elevation=10, use="road", road_class="residential",
              cycle_lane="none", speed_limit=40, grade=0, names=("Main Street",)):
    """A fake road piece shaped like Valhalla's data."""
    return {
        "length": length_km, "mean_elevation": elevation, "use": use,
        "road_class": road_class, "cycle_lane": cycle_lane, "speed_limit": speed_limit,
        "max_upward_grade": grade, "names": list(names),
        "begin_shape_index": 0, "end_shape_index": 1,
    }


def make_details(edges):
    return {"edges": edges, "shape": [[-121.9, 37.3], [-121.8, 37.4]]}


def prefs_with(**changes):
    """Default preferences with a few values changed."""
    return DEFAULT_PREFERENCES.model_copy(update=changes)


# ---------- routing.py ----------

def test_decode_polyline_known_example():
    # Google's documented example (precision 5): 3 points.
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert points == [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]


# ---------- ai_parser.py ----------

def test_clamp_keeps_ai_numbers_in_range():
    assert clamp(1.7) == 1.0
    assert clamp(-0.3) == 0.0
    assert clamp(0.4) == 0.4


def test_valhalla_settings_flip_the_scale():
    # Our "avoid_hills 0.9" means Valhalla's "use_hills 0.1".
    settings = to_valhalla_settings(
        prefs_with(avoid_hills=0.9, avoid_busy_roads=0.2, prefer_bike_lanes=0.0, speed_mph=10))
    assert settings["use_hills"] == 0.1
    assert settings["use_roads"] == 0.8
    assert settings["cycling_speed"] == 16.1  # 10 mph in km/h


def test_avoiding_bike_lanes_turns_valhalla_toward_roads():
    settings = to_valhalla_settings(prefs_with(prefer_bike_lanes=-1.0, avoid_busy_roads=0.5))
    assert settings["use_roads"] >= 0.9


def test_wanting_bike_lanes_turns_valhalla_toward_paths():
    settings = to_valhalla_settings(prefs_with(prefer_bike_lanes=1.0, avoid_busy_roads=0.8))
    assert settings["use_roads"] == 0.0


def test_traffic_preference_still_matters_with_default_bike_lanes():
    # Bug found in review: avoid_busy_roads used to have no effect at all here.
    calm = to_valhalla_settings(prefs_with(avoid_busy_roads=0.9))
    brave = to_valhalla_settings(prefs_with(avoid_busy_roads=0.1))
    assert calm["use_roads"] < brave["use_roads"]


def test_wanting_hills_never_goes_past_valhallas_max():
    assert to_valhalla_settings(prefs_with(avoid_hills=-1.0))["use_hills"] == 1.0


# ---------- scoring.py: measuring ----------

def test_climb_only_counts_uphill():
    # Up 10 m, down 20 m, up 5 m = 15 m of climbing (about 49 ft).
    edges = [make_edge(elevation=e) for e in [100, 110, 90, 95]]
    assert measure_route(make_details(edges))["climb_ft"] == 49


def test_busy_roads_and_bike_lanes_are_counted():
    edges = [
        make_edge(length_km=1.0, road_class="primary"),            # busy
        make_edge(length_km=1.0, speed_limit=64),                  # 40 mph limit = busy
        make_edge(length_km=1.0, use="cycleway"),                  # bike path
        make_edge(length_km=1.0, cycle_lane="dedicated"),          # painted bike lane
        make_edge(length_km=1.0, road_class="primary", cycle_lane="separated"),  # protected: not busy
    ]
    metrics = measure_route(make_details(edges))
    assert metrics["busy_road_miles"] == round(2 * 0.621371, 2)
    assert metrics["bike_lane_miles"] == round(3 * 0.621371, 2)
    assert metrics["bike_lane_percent"] == 60


def test_steepest_hill_ignores_tiny_pieces():
    edges = [make_edge(length_km=0.01, grade=20), make_edge(length_km=0.5, grade=7)]
    assert measure_route(make_details(edges))["steepest_grade"] == 7


def test_avoided_street_names_match_abbreviations():
    assert is_avoided_street(["Capitol Expy"], ["Capitol Expressway"])
    assert is_avoided_street(["North Capitol Avenue"], ["capitol ave"])
    assert not is_avoided_street(["Monterey Road"], ["Capitol Expressway"])


def test_avoided_street_matches_whole_words_only():
    # Bugs found in review: substring matching caught the wrong streets.
    assert not is_avoided_street(["South 21st Street"], ["1st Street"])
    assert not is_avoided_street(["Almaden Expressway"], ["Alma"])
    assert is_avoided_street(["North 1st Street"], ["1st Street"])


def test_blank_avoided_street_matches_nothing():
    assert not is_avoided_street(["Main Street"], ["", "  "])


def test_route_on_avoided_street_is_flagged():
    edges = [make_edge(names=["Capitol Expressway"])]
    metrics = measure_route(make_details(edges), avoid_streets=["Capitol Expy"])
    assert metrics["uses_avoided_street"]
    assert len(metrics["avoided_street_points"]) == 1


# ---------- scoring.py: choosing ----------

def fake_route(minutes, climb=0, busy=0.0, bike=0.0, avoided=False, geometry=None):
    return {
        "duration_minutes": minutes,
        "geometry": geometry or [[minutes, climb]],
        "metrics": {
            "climb_ft": climb, "steepest_grade": 0, "busy_road_miles": busy,
            "bike_lane_miles": bike, "uses_avoided_street": avoided,
        },
    }


def test_hill_hater_prefers_flatter_route_even_if_slower():
    prefs = prefs_with(avoid_hills=1.0, speed_importance=0.2)
    hilly = fake_route(20, climb=300)
    flat = fake_route(25, climb=20)
    assert score_route(flat, flat["metrics"], prefs) < score_route(hilly, hilly["metrics"], prefs)


def test_hurried_rider_prefers_faster_route():
    prefs = prefs_with(avoid_hills=0.1, avoid_busy_roads=0.1, speed_importance=1.0)
    slow = fake_route(30, busy=0.0)
    fast = fake_route(20, busy=1.0)
    assert score_route(fast, fast["metrics"], prefs) < score_route(slow, slow["metrics"], prefs)


def test_avoided_street_is_basically_disqualified():
    prefs = prefs_with()
    banned = fake_route(10, avoided=True)
    long_way = fake_route(40)
    assert score_route(long_way, long_way["metrics"], prefs) < score_route(banned, banned["metrics"], prefs)


def test_explanation_uses_real_numbers():
    prefs = prefs_with(avoid_hills=0.9)
    standard = fake_route(20, climb=300, busy=2.0)
    best = fake_route(23, climb=100, busy=0.5)
    reasons = explain_choice(best, standard, prefs)
    assert "200 ft less climbing" in reasons
    assert "1.5 fewer miles on busy roads" in reasons
    assert "Tradeoff: 3 extra min compared to the typical route" in reasons


# ---------- avoiding bike lanes (the bug a user found) ----------

def test_bike_lane_avoider_prefers_route_with_fewer_bike_lanes():
    prefs = prefs_with(prefer_bike_lanes=-1.0)
    lots_of_lanes = fake_route(30, bike=3.4)
    few_lanes = fake_route(30, bike=1.6)
    assert (score_route(few_lanes, few_lanes["metrics"], prefs)
            < score_route(lots_of_lanes, lots_of_lanes["metrics"], prefs))


def test_explanation_never_praises_bike_lanes_to_someone_avoiding_them():
    prefs = prefs_with(prefer_bike_lanes=-1.0)
    standard = fake_route(30, bike=1.0, geometry=[[0, 0]])
    best = fake_route(30, bike=2.0, geometry=[[1, 1]])
    reasons = explain_choice(best, standard, prefs)
    benefits = [r for r in reasons if not r.startswith("Tradeoff:")]
    assert not any("more miles on bike lanes" in r for r in benefits)
    assert "Tradeoff: 1.0 more miles on bike lanes" in reasons  # honestly listed as a cost


def test_explanation_credits_fewer_bike_lanes_when_asked():
    prefs = prefs_with(prefer_bike_lanes=-1.0)
    standard = fake_route(30, bike=3.43, busy=2.85)
    best = fake_route(31, bike=1.6, busy=3.42)
    reasons = explain_choice(best, standard, prefs)
    assert "1.8 fewer miles on bike lanes, as you asked" in reasons
    assert "Tradeoff: 0.6 more miles on busy roads" in reasons


def test_warns_when_bike_lanes_could_not_be_avoided():
    prefs = prefs_with(prefer_bike_lanes=-1.0)
    best = fake_route(25)
    best["metrics"]["bike_lane_percent"] = 45
    warnings = find_unmet_requests(best, prefs)
    assert any("Couldn't fully avoid bike lanes: 45%" in w for w in warnings)


def test_no_warnings_when_request_was_met():
    prefs = prefs_with(prefer_bike_lanes=-1.0)
    best = fake_route(25)
    best["metrics"]["bike_lane_percent"] = 5
    assert find_unmet_requests(best, prefs) == []


# ---------- explaining choices (added after the judge-style review) ----------

def test_why_not_fastest_explains_the_tradeoff():
    from backend.scoring import explain_not_fastest
    best = fake_route(45, busy=0.5)
    fastest = fake_route(43, busy=1.8)
    line = explain_not_fastest(best, fastest, prefs_with())
    assert "saves 2 min, but it has 1.3 more miles on busy roads" in line


def test_why_not_fastest_is_silent_when_best_is_fastest():
    from backend.scoring import explain_not_fastest
    best = fake_route(30)
    assert explain_not_fastest(best, best, prefs_with()) is None


def test_fastest_possible_really_picks_the_fastest():
    # Bug found in testing: "fastest possible, I'm late" chose a route 5 min slower,
    # because busy-road penalties outweighed the time saved.
    prefs = prefs_with(speed_importance=1.0, avoid_busy_roads=0.5, prefer_bike_lanes=0.3)
    calm = fake_route(32, busy=1.29, bike=3.0)
    fast = fake_route(27, busy=3.42, bike=1.6)
    assert score_route(fast, fast["metrics"], prefs) < score_route(calm, calm["metrics"], prefs)


def test_warning_does_not_claim_lowest_when_another_option_is_lower():
    prefs = prefs_with(prefer_bike_lanes=-1.0)
    best = fake_route(37)
    best["metrics"]["bike_lane_percent"] = 54
    other = fake_route(32)
    other["metrics"]["bike_lane_percent"] = 32
    warning = find_unmet_requests(best, prefs, [best, other])[0]
    assert "lowest of the options" not in warning


def test_score_breakdown_adds_up_to_the_score():
    from backend.scoring import score_breakdown
    prefs = prefs_with(avoid_hills=0.8, avoid_busy_roads=0.9, prefer_bike_lanes=0.5)
    route = fake_route(30, climb=120, busy=1.2, bike=2.0)
    parts = score_breakdown(route, route["metrics"], prefs)
    assert round(sum(parts.values()), 1) == score_route(route, route["metrics"], prefs)
    assert parts["bike_lanes"] < 0  # liking bike lanes is a bonus


def test_route_names_describe_the_route():
    from backend.planner import describe_route
    standard = {**fake_route(25, busy=2.0), "name": "Standard"}
    calm = {**fake_route(28, busy=0.5), "name": "Tailored"}
    quick = {**fake_route(24, busy=2.5), "name": "Extra tailored"}
    routes = [standard, calm, quick]
    assert describe_route(calm, routes, best=calm)[0] == "Best match for you"
    assert describe_route(standard, routes, best=calm)[0] == "Typical route"
    assert describe_route(quick, routes, best=calm)[0] == "Fastest"


# ---------- found by the stress-test agent ----------

def test_highway_names_match_their_openstreetmap_names():
    assert is_avoided_street(["CA 1", "Cabrillo Highway South"], ["Highway 1"])
    assert is_avoided_street(["US 101"], ["Hwy 101"])
    assert is_avoided_street(["I-280"], ["Interstate 280"])
    assert not is_avoided_street(["CA 17"], ["Highway 1"])
    assert not is_avoided_street(["US 101"], ["Highway 1"])


def test_flattest_rider_avoids_a_20_percent_wall_even_with_a_bit_more_climbing():
    # Berkeley case: 951 ft with a 20% wall vs 1102 ft topping out at 12%.
    prefs = prefs_with(avoid_hills=1.0, speed_importance=0.3)
    wall = fake_route(30, climb=951)
    wall["metrics"]["steepest_grade"] = 20
    gentle = fake_route(33, climb=1102)
    gentle["metrics"]["steepest_grade"] = 12
    assert score_route(gentle, gentle["metrics"], prefs) < score_route(wall, wall["metrics"], prefs)


def test_hurry_turns_valhalla_toward_direct_roads():
    rushed = to_valhalla_settings(prefs_with(speed_importance=1.0, avoid_busy_roads=0.5))
    assert rushed["use_roads"] >= 0.8
