"""
Automated tests for Wayfinder's own logic (no internet or AI needed).

Run them from the Wayfinder folder with:   pytest
Each test_ function checks one thing. If any check is wrong, pytest shows exactly which.
"""
from backend.ai_parser import DEFAULT_PREFERENCES, clamp, to_valhalla_settings
from backend.routing import decode_polyline
from backend.scoring import explain_choice, is_avoided_street, measure_route, score_route


# ---------- helpers to build fake data ----------

def make_edge(length_km=1.0, elevation=10, use="road", road_class="residential",
              cycle_lane="none", speed=40, grade=0, names=("Main Street",)):
    """A fake road piece shaped like Valhalla's data."""
    return {
        "length": length_km, "mean_elevation": elevation, "use": use,
        "road_class": road_class, "cycle_lane": cycle_lane, "speed": speed,
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
    settings = to_valhalla_settings(prefs_with(avoid_hills=0.9, avoid_busy_roads=0.2, speed_mph=10))
    assert settings["use_hills"] == 0.1
    assert settings["use_roads"] == 0.8
    assert settings["cycling_speed"] == 16.1  # 10 mph in km/h


# ---------- scoring.py: measuring ----------

def test_climb_only_counts_uphill():
    # Up 10 m, down 20 m, up 5 m = 15 m of climbing (about 49 ft).
    edges = [make_edge(elevation=e) for e in [100, 110, 90, 95]]
    assert measure_route(make_details(edges))["climb_ft"] == 49


def test_busy_roads_and_bike_lanes_are_counted():
    edges = [
        make_edge(length_km=1.0, road_class="primary"),            # busy
        make_edge(length_km=1.0, speed=60),                        # fast = busy
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
    assert "costs 3 extra min compared to the standard route" in reasons
