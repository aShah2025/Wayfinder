"""
Checks that the AI turns different requests into the RIGHT settings.

This uses the real Gemini AI (needs internet + your API key), so it's separate from
the fast `pytest` tests. Run it after changing the AI instructions:

    python tests/check_ai.py

Each check says which way a setting should go, e.g. "avoid bike lanes" must make
prefer_bike_lanes NEGATIVE. Prints PASS/FAIL for each one.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from backend.ai_parser import parse_instructions  # noqa: E402

# (what the rider types, the setting to check, what it must be)
CHECKS = [
    ("avoid bike lanes", "prefer_bike_lanes", lambda v: v <= -0.5),
    ("no bike paths please, regular streets only", "prefer_bike_lanes", lambda v: v <= -0.5),
    ("I love bike lanes", "prefer_bike_lanes", lambda v: v >= 0.7),
    ("I want a hilly workout", "avoid_hills", lambda v: v <= -0.3),
    ("flattest route possible, bad knees", "avoid_hills", lambda v: v >= 0.8),
    ("I'm riding with my 6 year old", "avoid_busy_roads", lambda v: v >= 0.7),
    ("I'm riding with my 6 year old", "speed_mph", lambda v: v <= 8),
    ("I don't mind traffic, I'm experienced", "avoid_busy_roads", lambda v: v <= 0.3),
    ("fastest way, I'm late", "speed_importance", lambda v: v >= 0.8),
    ("no rush, scenic and relaxed", "speed_importance", lambda v: v <= 0.3),
    ("I'm on my e-bike", "speed_mph", lambda v: v >= 15),
    ("road bike with skinny tires", "bicycle_type", lambda v: v == "Road"),
    ("road bike with skinny tires", "avoid_unpaved", lambda v: v >= 0.7),
    ("I'm on a mountain bike, dirt is fine", "bicycle_type", lambda v: v == "Mountain"),
    ("stay off Monterey Rd and Capitol Expy", "avoid_streets", lambda v: len(v) == 2),
    ("need to refill my water bottle", "stop_type", lambda v: v == "water"),
    ("my tire is going flat", "stop_type", lambda v: v == "bike_shop"),
    ("just get me there", "stop_type", lambda v: v == "none"),
]


async def main():
    failures = 0
    cache = {}
    for text, field, is_ok in CHECKS:
        if text not in cache:
            cache[text] = await parse_instructions(text)
        value = getattr(cache[text], field)
        passed = is_ok(value)
        failures += not passed
        print(f"{'PASS' if passed else 'FAIL'}  {text!r:48} {field} = {value}")

    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} passed")


asyncio.run(main())
