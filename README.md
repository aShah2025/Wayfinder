# 🚲 Wayfinder

**Bike routes that actually listen to you.**

Type how you want to ride, like *"I'm riding with my 8-year-old, avoid big hills, stay off Capitol Expressway, and grab coffee on the way."* Wayfinder builds several real bike routes, measures every street on them, and picks the one that best fits what you said. Then it explains its choice with real numbers.

Built for ImpactHack 2026. Works anywhere in California, demoed in San Jose.

## Why it's not just a chatbot

The AI (Google Gemini) does exactly **one** job: turning your words into a strict form of route settings. Everything after that is Wayfinder's own code working with real map data:

```
"avoid hills, I'm with my kid"
        │
        ▼
  Gemini fills in a form ──► { avoid_hills: 0.8, avoid_busy_roads: 0.9, speed_mph: 7, ... }
        │
        ▼
  Build 3 candidate routes (Valhalla routing engine + OpenStreetMap)
        │
        ▼
  Re-route around any street you banned (our own loop)
        │
        ▼
  Measure every street: climbing, steepest hill, busy-road miles, bike-lane miles
        │
        ▼
  Score each route in "penalty minutes" using YOUR preferences → lowest wins
        │
        ▼
  Explain the pick by comparing it with a standard route (real numbers, not AI text)
```

## Features

- 🗣️ **Plain-English instructions**: hills, traffic, bike lanes, speed, bike type, streets to avoid
- 🔁 **Follow-ups**: "ok but I'm late now" adjusts your ride instead of starting over
- ⚖️ **Side-by-side route options** with climb, busy-road miles, and bike-lane %
- 🚫 **Avoid any street by name**, with automatic re-routing
- ☕ **Stops on the way** (coffee, water, restrooms, bike shops...), picked for the smallest detour
- ⛰️ **Elevation chart** for every route
- 📍 **Use my location**, works on phones

## Run it yourself

You need Python 3.11+ and a free Gemini API key from [aistudio.google.com](https://aistudio.google.com).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env        # then paste your key into .env
uvicorn backend.main:app --reload
```

Open **http://localhost:8000**.

Run the tests:

```bash
pytest
```

## Built with

| Part | Technology |
|---|---|
| Backend | Python, FastAPI, httpx, Pydantic |
| AI | Google Gemini (`gemini-3.5-flash-lite` with automatic fallbacks), structured JSON output |
| Routing | [Valhalla](https://github.com/valhalla/valhalla) open-source routing engine |
| Map data | [OpenStreetMap](https://www.openstreetmap.org) |
| Place search | [Photon](https://photon.komoot.io) (addresses), [Overpass API](https://overpass-api.de) (stops) |
| Frontend | HTML, CSS, JavaScript, [MapLibre GL JS](https://maplibre.org), [OpenFreeMap](https://openfreemap.org) tiles |
| Testing | pytest |

See [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for a walkthrough of every file.

## Project layout

```
backend/
  main.py        web server and API endpoints
  ai_parser.py   the only AI step: words → route preferences
  planner.py     the recipe: candidates → avoid streets → score → pick
  routing.py     talks to Valhalla (routes + road details)
  scoring.py     measures and scores routes, writes the explanation
  stops.py       finds a stop on the way with the smallest detour
  geocode.py     address search
frontend/
  index.html, style.css, app.js
tests/           automated tests
```

## License

MIT. Map data © OpenStreetMap contributors.
