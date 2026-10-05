"""
ai_parser.py: the ONLY place Wayfinder uses AI.

The AI (Google Gemini) reads what the rider typed, like
    "avoid big hills, I'm riding with my 8 year old, stay off Capitol Expressway"
and fills in a strict form (RidePreferences below) with numbers and lists.

The AI never picks the route. It only translates human words into settings.
Our own code (planner.py + scoring.py) uses those settings to find, measure,
and choose the route.
"""
import asyncio
import json
import os
from typing import Literal

from google import genai
from pydantic import BaseModel, Field

# Models to try, in order. Google's models are sometimes overloaded ("503") and the
# free tier allows 15 requests per minute PER MODEL ("429"), so if one fails we
# automatically try the next. Each is a genuinely different model with its own limit.
# "lite" models are fast and plenty smart for filling in a form.
MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-flash-latest",
]

# Remember answers we've already gotten, so asking the exact same thing again
# (like rehearsing a demo) is instant and doesn't use up the free AI limit.
_answer_cache = {}


class AIUnavailable(Exception):
    """Raised when every Gemini model failed."""


class RidePreferences(BaseModel):
    """The form the AI must fill in. Gemini is forced to answer in exactly this shape."""

    understood: str = Field(
        description="One short friendly sentence restating what the rider wants, "
        "e.g. 'Gentle, low-traffic ride for you and your kid.'"
    )
    avoid_hills: float = Field(
        description="-1.0 = WANTS hills (training/workout), 0.0 = doesn't care, "
        "1.0 = avoid hills as much as possible"
    )
    avoid_busy_roads: float = Field(
        description="0.0 = fine riding with fast traffic, 1.0 = stay away from busy roads"
    )
    prefer_bike_lanes: float = Field(
        description="-1.0 = AVOID bike lanes and bike paths (ride on regular roads), "
        "0.0 = doesn't care, 1.0 = strongly wants bike lanes and bike paths"
    )
    speed_importance: float = Field(
        description="0.0 = happy to take longer, 1.0 = wants the quickest route"
    )
    avoid_unpaved: float = Field(
        description="0.0 = gravel/dirt is fine, 1.0 = paved roads only"
    )
    bicycle_type: Literal["Road", "Hybrid", "Cross", "Mountain"] = Field(
        description="Road = skinny-tire road bike, Hybrid = normal city bike (default), "
        "Cross = gravel bike, Mountain = mountain bike"
    )
    speed_mph: float = Field(
        description="Typical riding speed in mph. Normal adult ~10, kids or casual ~7, "
        "fit/road riders ~15, e-bikes ~17"
    )
    avoid_streets: list[str] = Field(
        description="Exact names of streets the rider wants to avoid, written out in full, "
        "e.g. ['Capitol Expressway', 'Monterey Road']. Empty list if none."
    )
    stop_type: Literal[
        "none", "coffee", "food", "water", "restroom", "bike_shop", "park", "grocery"
    ] = Field(
        description="A kind of place the rider wants to stop at on the way, or 'none'"
    )


DEFAULT_PREFERENCES = RidePreferences(
    understood="A normal, balanced bike ride.",
    avoid_hills=0.5,
    avoid_busy_roads=0.5,
    prefer_bike_lanes=0.3,
    speed_importance=0.5,
    avoid_unpaved=0.3,
    bicycle_type="Hybrid",
    speed_mph=10,
    avoid_streets=[],
    stop_type="none",
)

INSTRUCTIONS_FOR_AI = """You turn a cyclist's request into route settings for a bike
routing engine in California. Fill in every field.

Guidelines:
- Start from a balanced rider (avoid_hills 0.5, avoid_busy_roads 0.5, prefer_bike_lanes 0.3,
  speed_importance 0.5, Hybrid bike, 10 mph) and only move a value when the request gives
  a reason to.
- Some values can go NEGATIVE, meaning the rider wants the opposite:
  "avoid bike lanes", "no bike paths", "just regular roads" -> prefer_bike_lanes -0.7 to -1.0,
  and (unless they ALSO ask for quiet streets) lower avoid_busy_roads to about 0.2, since
  they're choosing to ride with traffic.
  "I want hills", "hilly workout", "training climbs" -> avoid_hills -0.5 to -1.0.
- Riding with kids, beginners, nervous riders, or "safest route" -> raise avoid_busy_roads
  and prefer_bike_lanes, lower speed_mph.
- Heavy loads, cargo bikes, tired legs, injuries, older riders -> raise avoid_hills.
- "Fastest", "in a hurry", "late" -> raise speed_importance.
- Skinny tires / road bike -> Road and raise avoid_unpaved. Gravel or trails -> Cross or Mountain.
- stop_type: only set it if the rider asks to stop somewhere ("grab coffee" -> coffee,
  "refill my bottle" -> water, "need a bathroom" -> restroom, "flat tire" -> bike_shop).
- Only list streets in avoid_streets if the rider names them. Use full official names
  (e.g. "Capitol Expy" -> "Capitol Expressway").
"""


def clamp(value, low=0.0, high=1.0):
    """Keep a number inside a range, in case the AI goes out of bounds."""
    return max(low, min(high, value))


def make_safe(prefs):
    """
    Never fully trust preferences, whether they came from the AI or from the browser.
    Force every number into its allowed range and clean up the street list.
    """
    prefs = prefs.model_copy()
    for field in ["avoid_busy_roads", "speed_importance", "avoid_unpaved"]:
        setattr(prefs, field, clamp(getattr(prefs, field)))
    for field in ["avoid_hills", "prefer_bike_lanes"]:  # these two can be negative
        setattr(prefs, field, clamp(getattr(prefs, field), -1.0, 1.0))
    prefs.speed_mph = clamp(prefs.speed_mph, 4, 25)

    streets = []
    for street in prefs.avoid_streets:
        street = street.strip()[:60]
        if len(street) >= 3 and street.lower() not in [s.lower() for s in streets]:
            streets.append(street)
    prefs.avoid_streets = streets[:5]  # a blank or 1-letter name would match every road
    prefs.understood = prefs.understood.strip()[:200]
    return prefs


async def parse_instructions(text, previous=None):
    """
    Turn the rider's words into RidePreferences.
    `previous` = the preferences from the last route, so follow-ups like
    "ok but shorter" can ADJUST them instead of starting over.
    """
    if not text.strip():
        return make_safe(previous) if previous else DEFAULT_PREFERENCES

    prompt = INSTRUCTIONS_FOR_AI
    if previous:
        prompt += (
            "\nThis is a FOLLOW-UP. The rider's current settings are below. Keep them, "
            "and change only what the new message asks for:\n"
            + json.dumps(previous.model_dump())
        )
    prompt += f'\n\nRider\'s request: "{text}"'

    if prompt in _answer_cache:
        return _answer_cache[prompt].model_copy()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[ai_parser] GEMINI_API_KEY is missing from .env")
        raise AIUnavailable("The AI isn't set up (missing API key).")

    client = genai.Client(
        api_key=api_key,
        # Give up on a model after 10 seconds (Google's minimum) and move straight to
        # the next one, instead of the library quietly retrying the same busy model.
        http_options={"timeout": 10000, "retry_options": {"attempts": 1}},
    )

    prefs = None
    for model in MODELS:
        try:
            # A hard 12-second limit per model, so a stuck connection can't hang the app.
            response = await asyncio.wait_for(client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": RidePreferences,  # forces Gemini to fill in our exact form
                    "automatic_function_calling": {"disable": True},
                },
            ), timeout=12)
            prefs = response.parsed
            if prefs:
                break
        except Exception as error:
            print(f"[ai_parser] {model} failed: {type(error).__name__} {str(error)[:100]}")

    if prefs is None:
        raise AIUnavailable("The AI is busy right now. Please try again in a moment.")

    # Double-check the AI's answer. Never fully trust AI output!
    prefs = make_safe(prefs)
    if len(_answer_cache) < 500:
        _answer_cache[prompt] = prefs
    return prefs.model_copy()


def to_valhalla_settings(prefs):
    """
    Convert our preferences into Valhalla's bicycle settings.
    Valhalla's scale is backwards from ours: use_hills 0.0 means AVOID hills.

    Valhalla's "use_roads" is one dial for two of our preferences:
    low = stick to bike paths/lanes (and away from traffic), high = regular roads are fine.
    """
    # Blend both preferences into the one dial: avoiding traffic turns it down,
    # wanting bike lanes turns it down more, AVOIDING bike lanes (negative) turns it up.
    use_roads = clamp(1 - prefs.avoid_busy_roads - 0.4 * prefs.prefer_bike_lanes)
    use_hills = clamp(1 - prefs.avoid_hills)
    if prefs.speed_importance >= 0.7:
        # In a hurry: direct roads and hills are fine, since detours cost time.
        use_roads = max(use_roads, prefs.speed_importance * 0.8)
        use_hills = max(use_hills, 0.5)

    return {
        "bicycle_type": prefs.bicycle_type,
        "cycling_speed": round(prefs.speed_mph * 1.609, 1),  # Valhalla wants km/h
        "use_hills": round(use_hills, 2),  # wanting hills (negative avoid_hills) -> 1.0
        "use_roads": round(use_roads, 2),
        "avoid_bad_surfaces": round(prefs.avoid_unpaved, 2),
    }
