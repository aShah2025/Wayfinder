from dotenv import load_dotenv

# Read secrets (like the Gemini API key) from the .env file.
# This runs first so the other files can see those settings.
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.ai_parser import AIUnavailable, RidePreferences
from backend.geocode import search_places
from backend.planner import plan_ride
from backend.routing import RouteError

app = FastAPI()


@app.get("/api/health")
def health():
    return {"status": "ok", "message": "Wayfinder is alive"}


# Search for places as the user types, e.g. /api/search?q=santana row
@app.get("/api/search")
async def search(q: str):
    return await search_places(q)


# These classes describe what the website must send us.
# FastAPI checks the data automatically and rejects anything wrong.
class Point(BaseModel):
    lat: float
    lon: float


class RouteRequest(BaseModel):
    start: Point
    end: Point
    instructions: str = ""  # what the rider typed, e.g. "avoid hills"
    # The preferences from the last route, so follow-ups like "ok but shorter" work.
    previous_preferences: RidePreferences | None = None


# Plan a ride. The website sends start, end and instructions as JSON.
@app.post("/api/route")
async def route(request: RouteRequest):
    try:
        return await plan_ride(
            request.start.model_dump(),
            request.end.model_dump(),
            request.instructions,
            request.previous_preferences,
        )
    except RouteError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except AIUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error))


# Serve the website. This must stay LAST, because "/" matches every address.
app.mount("/", StaticFiles(directory="frontend", html=True))
