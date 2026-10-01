# How Wayfinder Works

A plain-English tour of the whole project. If you can explain this page, you can explain Wayfinder.

---

## The big picture

Wayfinder has two halves that talk to each other:

| | Frontend (the page) | Backend (the server) |
|---|---|---|
| Language | HTML + CSS + JavaScript | Python |
| Runs on | The rider's browser | My computer (or a server) |
| Job | Show the map, collect input, draw results | Think: AI, routing, measuring, scoring |
| Files | `frontend/` | `backend/` |

They talk using **JSON**, which is basically a Python dictionary written as text. The page sends a request to an **API endpoint** (a web address that runs Python code, like `/api/route`), and the server sends JSON back.

---

## What happens when you click "Find my route"

```
Browser (app.js)                      Server (Python)                         Outside services
────────────────                      ───────────────                         ────────────────
send start, end, instructions  ──►    main.py: /api/route
                                       │
                                       ├─ ai_parser.py  ─────────────────────► Gemini (AI)
                                       │    words → RidePreferences form
                                       │
                                       ├─ stops.py (only if a stop was asked) ► Overpass (OpenStreetMap)
                                       │    find the place with the smallest detour
                                       │
                                       ├─ planner.py builds 3 candidates, at the same time:
                                       │    Standard / Tailored / Extra tailored
                                       │      └─ routing.py ─────────────────► Valhalla: route
                                       │      └─ routing.py ─────────────────► Valhalla: road details
                                       │      └─ if it uses a banned street: block it, re-route
                                       │
                                       ├─ scoring.py: measure + score each route
                                       ├─ scoring.py: explain the winner
                                       ▼
draw routes, cards, chart      ◄──    JSON: routes, scores, explanation
```

Typical time: 3–8 seconds.

---

## File by file

### `backend/main.py`: the front door
- Creates the FastAPI server (`app = FastAPI()`).
- `@app.get("/api/health")`: a simple "are you alive?" check.
- `@app.get("/api/search")`: address search as you type.
- `@app.post("/api/route")`: plans a ride. `RouteRequest` describes exactly what the page must send, and FastAPI automatically rejects bad data.
- `app.mount("/", ...)`: serves the website files. It's last because `/` matches everything.
- `load_dotenv()` reads the secret API key from `.env`, so the key is never written in the code.

### `backend/ai_parser.py`: the ONLY AI step
- `RidePreferences` is the **form** the AI must fill in: `avoid_hills`, `avoid_busy_roads`, `prefer_bike_lanes`, `speed_importance`, `avoid_unpaved`, `bicycle_type`, `speed_mph`, `avoid_streets`, `stop_type`, plus `understood` (a one-sentence summary shown to the rider).
- Most preferences go from 0 to 1, but **hills and bike lanes go from −1 to +1**, because riders can want the *opposite*: "I want a hilly workout" → `avoid_hills: -1`, "avoid bike lanes" → `prefer_bike_lanes: -1`. (This was a real bug a tester found: "avoid bike lanes" used to just mean "don't care.")
- **Structured output:** we give Gemini the form as a `response_schema`, so it *has* to answer in exactly that shape. It can't ramble or invent a route.
- **Never trust AI output blindly:** `clamp()` forces every number back into a safe range.
- **Follow-ups:** the previous preferences are sent along, and the AI is told to change only what the new message asks for.
- **Reliability:** if one Gemini model is overloaded (this really happened while building it), it automatically tries the next of 4 models.
- `to_valhalla_settings()` converts our scale into Valhalla's. Valhalla's scale is backwards: `use_hills: 0` means *avoid* hills.

### `backend/planner.py`: the recipe
1. Ask the AI for preferences.
2. If the rider wants a stop, find it once, so every route goes through the same stop and the comparison stays fair.
3. Build **3 candidates** at the same time (`asyncio.gather`):
   - **Standard**: normal settings. It uses the rider's bike and speed so times are comparable, but ignores everything else they asked. This is "what a regular map app would give you," our baseline.
   - **Tailored**: the AI's settings.
   - **Extra tailored**: the AI's settings pushed further (`push_further()` moves each value halfway toward the end it already leans to).
4. **Avoid-street loop:** Valhalla can't avoid a street *by name*. So we find which road pieces of the route are on that street, give Valhalla those spots as `exclude_locations`, and ask again, up to 3 times. We use a point in the *middle* of each piece, not at an intersection, so we don't accidentally block the cross street.
5. Remove duplicate routes (different settings sometimes give the same path).
6. Score, pick the lowest, and explain.

### `backend/routing.py`: talking to Valhalla
- **Valhalla** is a free, open-source routing engine. We use the public server.
- `get_bike_route()` sends start, (stop), end, and bike settings. It gets back distance, time, steps, and the route line.
- **Encoded polyline:** Valhalla squeezes thousands of points into a short string to save space. `decode_polyline()` unpacks it. Each number is stored as the *difference* from the previous point, packed into 5-bit chunks written as letters.
- `get_road_details()` asks Valhalla about **every road piece ("edge")** on a route: road type, bike lane, car speed, elevation, steepness. It uses `walk_or_snap` matching, which means it tries an exact match first and otherwise snaps the line to the nearest roads. That fixed a real bug where some routes failed.

### `backend/scoring.py`: Wayfinder's own judgment (no AI)
**Measuring** (`measure_route`): loop over every road piece and add up:
- **Climb (ft):** every time elevation goes *up* from one piece to the next.
- **Steepest hill (%):** ignoring tiny pieces under 50 m, where elevation data is noisy.
- **Busy-road miles:** regular roads that are major (primary/secondary/trunk) **or** have cars going 35+ mph, *unless* the bike lane is physically separated.
- **Bike-lane miles:** bike paths, cycleways, and painted or protected bike lanes.
- **Avoided street?** Names are normalized first ("Capitol Expy" = "Capitol Expressway").
- **Elevation profile:** for the chart, shrunk to at most 80 points.

**Scoring** (`score_route`) uses **"penalty minutes"**. Lower is better.
```
score = riding time × (0.5 + speed_importance)
      + avoid_hills       × climb_ft / 8               (every 8 ft of climbing ≈ 1 minute)
      + avoid_hills       × (steepest% − 5) × 4         (extra pain for steep pieces over 5%)
      + avoid_busy_roads  × busy miles × 15            (up to 15 min per busy mile)
      − prefer_bike_lanes × bike-lane miles × 5        (bonus if you like them, penalty if negative)
      + 1000 if it uses a street you banned           (basically disqualified)
```
In one sentence: *"Each route's score is its real time plus imaginary extra minutes for the things you dislike, and the AI's preferences decide how much each dislike costs."*

**Explaining** (`explain_choice`) compares the winner with the Standard route and writes reasons **only from measured numbers**, like "1.55 fewer miles on busy roads." It only praises changes in the direction the rider asked for, and it lists the costs too ("Tradeoff: 0.57 more miles on busy roads"). The AI never writes the explanation, so it can't make anything up.

**Being honest** (`find_unmet_requests`): sometimes *no* route can do what you asked, for example if the only way there is a bike path. Instead of pretending, the app shows a **⚠️ Heads up**, like "Couldn't fully avoid bike lanes: 33% of this route still uses them."

### `backend/stops.py`: a stop on the way
- The AI picks the stop **type** (coffee, water, restroom, bike shop, food, park, grocery).
- We ask **Overpass** (a search engine for OpenStreetMap data) for every place with that tag in a box around the trip.
- **Detour formula:** `detour = dist(start → place) + dist(place → end) − dist(start → end)`. Smallest detour wins. Places within 300 m of the start are skipped, since that isn't "on the way."
- Distances use the **haversine formula**, which gives the straight-line distance on a round Earth.
- If Overpass fails, the ride still works. The stop is a bonus.

### `backend/geocode.py`: address search
- Uses **Photon** to turn "Santana Row" into latitude/longitude, limited to a box around California and biased toward San Jose. Duplicate results are removed.

### `frontend/index.html`, `style.css`, `app.js`
- **HTML** = structure, **CSS** = looks, **JavaScript** = behavior.
- **MapLibre** draws the map, and **OpenFreeMap** provides the street images.
- Search boxes wait until you stop typing for 0.3 s before searching (called "debouncing"), so we don't search on every key.
- `fetch("/api/route", ...)` sends the request. `await` means "wait for the answer before continuing."
- The selected route is drawn in blue and the others in gray. Click either a card or a gray line to switch.
- The elevation chart is a small **SVG** drawing generated from the profile points.
- `lastPreferences` is kept so the next message becomes a **follow-up**.

### `tests/`: automated tests
- 24 tests check decoding, measuring, scoring, explanations, warnings, and stop picking with fake data. They need no internet or AI. Run them with `pytest`.
- `tests/check_ai.py` is different: it sends 18 real requests to Gemini and checks each setting goes the right way (e.g. "avoid bike lanes" must be negative). Run it with `python tests/check_ai.py` after changing the AI instructions.

---

## Key words cheat sheet

| Word | Meaning |
|---|---|
| API / endpoint | A web address that runs code and returns data |
| JSON | Text format for data, looks like a Python dictionary |
| Frontend / backend | The page in the browser / the server code |
| Geocoding | Turning a place name into coordinates |
| Routing engine | Software that finds paths through a road network |
| Edge | One piece of road between two intersections |
| Structured output | Forcing the AI to answer in an exact format |
| Async / await | Waiting for slow things (internet) without freezing everything |
| Haversine | Formula for distance between two points on Earth |
| Environment variable / `.env` | Where secrets like API keys live, outside the code |
