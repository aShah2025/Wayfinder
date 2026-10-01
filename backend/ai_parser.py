"""
ai_parser.py: the ONLY place Wayfinder uses AI.

The AI (Google Gemini) reads what the rider typed, like
    "avoid big hills, I'm riding with my 8 year old, stay off Capitol Expressway"
and fills in a strict form (RidePreferences below) with numbers and lists.

The AI never picks the route. It only translates human words into settings.
Our own code (planner.py + scoring.py) uses those settings to find, measure,
and choose the route.
"""
import json
import os
from typing import Literal

from google import genai
from pydantic import BaseModel, Field

# Models to try, in order. Google's models are sometimes overloaded ("503"),
# so if one fails we automatically try the next. "lite" models are fast and
# plenty smart for filling in a form.
MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-flash-latest",
]


class AIUnavailable(Exception):
    """Raised when every Gemini model failed."""


class RidePreferences(BaseModel):
    """The form the AI must fill in. Gemini is forced to answer in exactly this shape."""

    understood: str = Field(
        description="One short friendly sentence restating what the rider wants, "
        "e.g. 'Gentle, low-traffic ride for you and your kid.'"
    )
    avoid_hills: float = Field(
        description="0.0 = doesn't care about hills, 1.0 = avoid hills as much as possible"
    )
    avoid_busy_roads: float = Field(
        description="0.0 = fine riding with fast traffic, 1.0 = stay away from busy roads"
    )
    prefer_bike_lanes: float = Field(
        description="0.0 = doesn't care, 1.0 = strongly wants bike lanes and bike paths"
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


DEFAULT_PREFERENCES = RidePreferences(
    understood="A normal, balanced bike ride.",
    avoid_hills=0.5,
    avoid_busy_roads=0.5,
    prefer_bike_lanes=0.5,
    speed_importance=0.5,
    avoid_unpaved=0.3,
    bicycle_type="Hybrid",
    speed_mph=10,
    avoid_streets=[],
)

INSTRUCTIONS_FOR_AI = """You turn a cyclist's request into route settings for a bike
routing engine in California. Fill in every field.

Guidelines:
- Start from a balanced rider (0.5 for each preference, Hybrid bike, 10 mph) and only
  move a value when the request gives a reason to.
- Riding with kids, beginners, nervous riders, or "safest route" -> raise avoid_busy_roads
  and prefer_bike_lanes, lower speed_mph.
- Heavy loads, cargo bikes, tired legs, injuries, older riders -> raise avoid_hills.
- "Training", "workout", "I want hills" -> lower avoid_hills (toward 0).
- "Fastest", "in a hurry", "late" -> raise speed_importance.
- Skinny tires / road bike -> Road and raise avoid_unpaved. Gravel or trails -> Cross or Mountain.
- Only list streets in avoid_streets if the rider names them. Use full official names
  (e.g. "Capitol Expy" -> "Capitol Expressway").
"""


def clamp(value, low=0.0, high=1.0):
    """Keep a number inside a range, in case the AI goes out of bounds."""
    return max(low, min(high, value))


async def parse_instructions(text, previous=None):
    """
    Turn the rider's words into RidePreferences.
    `previous` = the preferences from the last route, so follow-ups like
    "ok but shorter" can ADJUST them instead of starting over.
    """
    if not text.strip():
        return previous or DEFAULT_PREFERENCES

    prompt = INSTRUCTIONS_FOR_AI
    if previous:
        prompt += (
            "\nThis is a FOLLOW-UP. The rider's current settings are below. Keep them, "
            "and change only what the new message asks for:\n"
            + json.dumps(previous.model_dump())
        )
    prompt += f'\n\nRider\'s request: "{text}"'

    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options={"timeout": 15000},  # give up on a model after 15 seconds
    )

    prefs = None
    for model in MODELS:
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": RidePreferences,  # forces Gemini to fill in our exact form
                    "automatic_function_calling": {"disable": True},
                },
            )
            prefs = response.parsed
            if prefs:
                break
        except Exception as error:
            print(f"[ai_parser] {model} failed: {str(error)[:100]}")

    if prefs is None:
        raise AIUnavailable("The AI is busy right now. Please try again in a moment.")

    # Double-check the AI's numbers are in range. Never fully trust AI output!
    for field in ["avoid_hills", "avoid_busy_roads", "prefer_bike_lanes",
                  "speed_importance", "avoid_unpaved"]:
        setattr(prefs, field, clamp(getattr(prefs, field)))
    prefs.speed_mph = clamp(prefs.speed_mph, 4, 25)
    return prefs


def to_valhalla_settings(prefs):
    """
    Convert our preferences into Valhalla's bicycle settings.
    Valhalla's scale is backwards from ours: use_hills 0.0 means AVOID hills.
    """
    return {
        "bicycle_type": prefs.bicycle_type,
        "cycling_speed": round(prefs.speed_mph * 1.609, 1),  # Valhalla wants km/h
        "use_hills": round(1 - prefs.avoid_hills, 2),
        "use_roads": round(1 - prefs.avoid_busy_roads, 2),
        "avoid_bad_surfaces": round(prefs.avoid_unpaved, 2),
    }
