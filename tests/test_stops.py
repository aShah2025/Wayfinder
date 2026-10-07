"""Tests for picking a stop on the way (no internet needed)."""
from backend.scoring import shrink
from backend.stops import detour_km, distance_km

SJSU = {"lat": 37.3352, "lon": -121.8811}
SANTANA_ROW = {"lat": 37.3210, "lon": -121.9486}


def test_distance_between_sjsu_and_santana_row():
    # About 6.2 km in a straight line.
    assert 6.0 < distance_km(SJSU, SANTANA_ROW) < 6.4


def test_place_on_the_way_has_almost_no_detour():
    halfway = {"lat": (SJSU["lat"] + SANTANA_ROW["lat"]) / 2,
               "lon": (SJSU["lon"] + SANTANA_ROW["lon"]) / 2}
    assert detour_km(SJSU, halfway, SANTANA_ROW) < 0.01


def test_place_off_to_the_side_has_a_bigger_detour():
    off_route = {"lat": 37.36, "lon": -121.91}  # north, near the airport
    halfway = {"lat": 37.328, "lon": -121.915}
    assert detour_km(SJSU, off_route, SANTANA_ROW) > detour_km(SJSU, halfway, SANTANA_ROW)


def test_shrink_keeps_the_last_point():
    points = [[i, i] for i in range(500)]
    small = shrink(points, 80)
    assert len(small) <= 81
    assert small[0] == [0, 0] and small[-1] == [499, 499]


def test_make_safe_cleans_preferences_from_the_browser():
    from backend.ai_parser import DEFAULT_PREFERENCES, make_safe
    bad = DEFAULT_PREFERENCES.model_copy(update={
        "speed_mph": 0, "avoid_hills": 5, "prefer_bike_lanes": -9,
        "avoid_streets": ["", " ", "a", "Main St", "main st"] + [f"Street {i}" for i in range(20)],
    })
    safe = make_safe(bad)
    assert safe.speed_mph == 4
    assert safe.avoid_hills == 1.0
    assert safe.prefer_bike_lanes == -1.0
    assert safe.avoid_streets[0] == "Main St"
    assert len(safe.avoid_streets) == 5


def test_planner_rejects_places_outside_california():
    import asyncio
    import pytest
    from backend.planner import plan_ride
    from backend.routing import RouteError
    reno = {"lat": 39.53, "lon": -119.81}
    with pytest.raises(RouteError, match="California"):
        asyncio.run(plan_ride(SJSU, reno, ""))
    with pytest.raises(RouteError, match="California"):
        asyncio.run(plan_ride(SJSU, {"lat": 999, "lon": 0}, ""))


def test_california_outline():
    from backend.planner import in_california
    for city in [(37.33, -121.89), (37.77, -122.42), (34.05, -118.24), (32.72, -117.16),
                 (38.58, -121.49), (41.75, -124.2), (33.83, -116.55)]:   # SJ, SF, LA, SD, Sac, Crescent City, Palm Springs
        assert in_california({"lat": city[0], "lon": city[1]}), city
    for city in [(39.53, -119.81), (36.17, -115.14), (45.5, -122.7), (32.5, -114.0)]:  # Reno, Vegas, Portland, Arizona
        assert not in_california({"lat": city[0], "lon": city[1]}), city
