// app.js: the "muscles" of the page. Everything interactive lives here.
//
// The big picture:
//   1. Show a map.
//   2. Let the user search for a start and a destination (or click an example).
//   3. When they click "Find my route", ask our Python server for a route.
//   4. Show the route options, WHY the best one won, and the turn-by-turn steps.
//   (3D terrain and the ride preview live in three_d.js.)


// ============ 1. THE MAP ============

const map = new maplibregl.Map({
  container: "map",                                      // the <div id="map">
  style: "https://tiles.openfreemap.org/styles/liberty", // free street map pictures
  center: [-121.8863, 37.3382],                          // [longitude, latitude] of San Jose
  zoom: 12,
  attributionControl: { compact: true },                 // small "ⓘ" so it doesn't cover the panel
});

map.addControl(new maplibregl.NavigationControl(), "top-right");

// A promise that finishes once the map has loaded, so we never draw a route too early.
const mapReady = new Promise((resolve) => map.on("load", resolve));

// The start and end points the user picked. null = not picked yet.
const places = { from: null, to: null };

// What the AI understood last time. Sent back with the next request so
// follow-ups like "ok but shorter" adjust the ride instead of starting over.
let lastPreferences = null;

// Everything the rider has typed for this trip, shown like a chat.
let conversation = [];

// The routes from the last search, and which one is highlighted.
let currentRoutes = [];
let selectedIndex = 0;

// Each kind of route gets its own color, on the map AND on its card.
const ROUTE_COLORS = { best: "#1a73e8", typical: "#5f6368", other: "#9334e6" };

function routeColor(route) {
  if (route.is_best) return ROUTE_COLORS.best;
  if (route.is_typical) return ROUTE_COLORS.typical;
  return ROUTE_COLORS.other;
}

// The green and red pins on the map.
const markers = {
  from: new maplibregl.Marker({ color: "#34a853" }),
  to: new maplibregl.Marker({ color: "#ea4335" }),
  stop: new maplibregl.Marker({ color: "#f9ab00" }),  // yellow = stop on the way
};

// Make a small HTML element with a class and text (safer than building HTML strings).
function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function showStatus(message, isError = false) {
  const status = document.getElementById("status");
  status.textContent = message;
  status.className = isError ? "error" : "";
}


// ============ 2. SEARCH BOXES ============

// Ask our server for places matching the text, preferring places near the map's center.
async function searchPlaces(text) {
  try {
    const center = map.getCenter();
    const url = "/api/search?q=" + encodeURIComponent(text) +
      "&lat=" + center.lat.toFixed(4) + "&lon=" + center.lng.toFixed(4);
    const response = await fetch(url);
    if (!response.ok) return [];
    return await response.json();
  } catch (error) {
    return []; // offline or server down: just show no suggestions
  }
}

// Save a picked place: remember it, fill the box, drop a pin, move the map.
function pickPlace(which, place) {
  places[which] = place;
  document.getElementById(which).value = place.name || place.label;
  document.getElementById(which + "-suggestions").innerHTML = "";
  markers[which].setLngLat([place.lon, place.lat]).addTo(map);
  map.flyTo({ center: [place.lon, place.lat], zoom: 14 });
  startNewTrip();
}

// The start or end changed, so the old results no longer apply.
function startNewTrip() {
  if (currentRoutes.length === 0) return;
  currentRoutes = [];
  conversation = [];
  document.getElementById("results").innerHTML = "";
  document.getElementById("panel").classList.remove("has-results");
  document.getElementById("go").textContent = "Find my route";
  document.getElementById("instructions").placeholder =
    "e.g. avoid big hills, I'm riding with my kid, stay off busy roads";
  markers.stop.remove();
  if (map.getSource("routes")) {
    map.getSource("routes").setData({ type: "FeatureCollection", features: [] });
  }
  if (window.onRouteSelected) window.onRouteSelected(null);

  // Keep HOW the rider likes to ride (kid, hills...), but forget the things that
  // only made sense for the old trip (streets to avoid, the stop).
  if (lastPreferences) {
    lastPreferences = { ...lastPreferences, avoid_streets: [], stop_type: "none" };
    showKeepingChip();
  }
}

// A small removable note: "Keeping your riding style: ... ✕"
function showKeepingChip() {
  const box = document.getElementById("keeping");
  box.innerHTML = "";
  if (!lastPreferences) return;
  box.appendChild(makeElement("span", "", "Keeping your riding style: " + lastPreferences.understood));
  const clear = makeElement("button", "chip-x", "✕");
  clear.type = "button";
  clear.setAttribute("aria-label", "Forget my riding style and start fresh");
  clear.addEventListener("click", () => {
    lastPreferences = null;
    box.innerHTML = "";
  });
  box.appendChild(clear);
}

// Show a list of suggestions under a search box.
function showSuggestions(which, results, message) {
  const list = document.getElementById(which + "-suggestions");
  list.innerHTML = "";
  if (message) {
    list.appendChild(makeElement("li", "no-results", message));
    return;
  }
  for (const place of results) {
    const item = makeElement("li");
    item.setAttribute("role", "option");
    item.appendChild(makeElement("span", "suggestion-name", place.name || place.label));
    if (place.address) item.appendChild(makeElement("span", "suggestion-address", place.address));
    // "mousedown" fires before the box loses focus, so the click always counts.
    item.addEventListener("mousedown", (event) => {
      event.preventDefault();
      pickPlace(which, place);
    });
    item.place = place;
    list.appendChild(item);
  }
  if (results.length === 0) {
    list.appendChild(makeElement("li", "no-results", "No places found in California"));
  }
}

// Make one search box show suggestions while the user types.
// `which` is "from" or "to" (the id of the <input>).
function setUpSearchBox(which) {
  const input = document.getElementById(which);
  const list = document.getElementById(which + "-suggestions");
  let typingTimer = null;
  let highlighted = -1;

  input.addEventListener("input", () => {
    places[which] = null;     // they're typing something new, so forget the old pick
    clearTimeout(typingTimer);

    const text = input.value.trim();
    if (text.length < 3) {
      list.innerHTML = "";
      return;
    }

    // Wait until they stop typing for 0.3 seconds, so we don't search on every key.
    typingTimer = setTimeout(async () => {
      showSuggestions(which, [], "Searching...");
      const results = await searchPlaces(text);
      // If the user kept typing (or left the box) while we waited, these results are old.
      if (input.value.trim() !== text || document.activeElement !== input) return;
      highlighted = -1;
      showSuggestions(which, results);
    }, 300);
  });

  // Arrow keys move through suggestions, Enter picks one, Escape closes the list.
  input.addEventListener("keydown", (event) => {
    const items = [...list.querySelectorAll("li")].filter((li) => li.place);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (items.length === 0) return;
      event.preventDefault();
      highlighted += event.key === "ArrowDown" ? 1 : -1;
      highlighted = (highlighted + items.length) % items.length;
      items.forEach((li, i) => li.classList.toggle("active", i === highlighted));
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (items.length > 0) pickPlace(which, items[Math.max(highlighted, 0)].place);
    } else if (event.key === "Escape") {
      list.innerHTML = "";
    }
  });

  // Hide the suggestions when the user clicks somewhere else.
  input.addEventListener("blur", () => (list.innerHTML = ""));
}

setUpSearchBox("from");
setUpSearchBox("to");

// The 📍 button: use the device's GPS location as the starting point.
document.getElementById("my-location").addEventListener("click", () => {
  if (!navigator.geolocation) {
    showStatus("Your browser can't share its location.", true);
    return;
  }
  showStatus("Finding your location...");
  navigator.geolocation.getCurrentPosition(
    (position) => {
      showStatus("");
      pickPlace("from", {
        name: "My location",
        label: "My location",
        lat: position.coords.latitude,
        lon: position.coords.longitude,
      });
    },
    () => showStatus("Couldn't get your location. Check your browser's permission.", true)
  );
});


// ============ 3. ONE-CLICK EXAMPLES ============
// Real trips that show off what Wayfinder can do. Great for a first visit (and a demo).

const EXAMPLES = [
  {
    button: "🧒 Ride with my kid + coffee",
    from: { name: "San José State University", lat: 37.3351902, lon: -121.881225 },
    to: { name: "Santana Row", lat: 37.3209796, lon: -121.9486002 },
    instructions: "I'm riding with my 8 year old, avoid busy roads and grab coffee on the way",
  },
  {
    button: "🚫 Stay off Capitol Expy",
    from: { name: "Eastridge Center", lat: 37.3266, lon: -121.81 },
    to: { name: "Evergreen Valley College", lat: 37.3005, lon: -121.7633 },
    instructions: "I'm a nervous rider, stay off Capitol Expy please",
  },
  {
    button: "⛰️ Gentlest hills across SF",
    from: { name: "Ferry Building, San Francisco", lat: 37.7955, lon: -122.3937 },
    to: { name: "Golden Gate Park, San Francisco", lat: 37.7695, lon: -122.4537 },
    instructions: "I'm on a heavy cargo bike, flattest route possible",
  },
];

function setUpExamples() {
  const box = document.getElementById("examples");
  for (const example of EXAMPLES) {
    const button = makeElement("button", "example", example.button);
    button.type = "button";
    button.addEventListener("click", () => {
      if (busy) return;
      lastPreferences = null;
      document.getElementById("keeping").innerHTML = "";
      pickPlace("from", { ...example.from, label: example.from.name });
      pickPlace("to", { ...example.to, label: example.to.name });
      document.getElementById("instructions").value = example.instructions;
      findRoute();
    });
    box.appendChild(button);
  }
}

setUpExamples();


// ============ 4. GETTING A ROUTE ============

// Make sure a place was really chosen. If the rider typed text but didn't pick a
// suggestion, only continue if there's exactly one match; otherwise ask them to pick
// (guessing silently once sent people to the wrong "San Jose State University").
async function makeSurePicked(which) {
  if (places[which]) return true;

  const input = document.getElementById(which);
  const text = input.value.trim();
  const name = which === "from" ? "starting point" : "destination";
  if (!text) {
    showStatus("Please choose a " + name + ".", true);
    return false;
  }

  const results = await searchPlaces(text);
  if (results.length === 1) {
    pickPlace(which, results[0]);
    return true;
  }
  input.focus();
  showSuggestions(which, results);
  showStatus(results.length === 0
    ? "Couldn't find that " + name + " in California. Try another name or address."
    : "Please pick your " + name + " from the list.", true);
  return false;
}

let busy = false; // true while a route is being planned, so clicks can't double up

// Wait for a promise, but give up after `ms` milliseconds.
function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_, reject) =>
    setTimeout(() => reject(new Error("timeout")), ms))]);
}

async function findRoute() {
  if (busy) return;
  busy = true;
  const button = document.getElementById("go");
  const instructionsBox = document.getElementById("instructions");
  button.disabled = true;

  const placesOk = (await makeSurePicked("from")) && (await makeSurePicked("to"));
  if (!placesOk) {
    button.disabled = false;
    busy = false;
    return;
  }

  // Show what's happening while the server works, with a timer so it never looks frozen.
  const steps = [
    "🤖 Reading your request",
    "🗺️ Building route options",
    "⛰️ Measuring hills and traffic on every street",
    "⚖️ Scoring each route for you",
  ];
  const startTime = Date.now();
  const updateLoading = () => {
    const seconds = Math.floor((Date.now() - startTime) / 1000);
    const step = steps[Math.min(Math.floor(seconds / 2), steps.length - 1)];
    showStatus(step + "... " + seconds + "s");
  };
  updateLoading();
  const loadingTimer = setInterval(updateLoading, 500);

  const sentInstructions = instructionsBox.value.trim();
  const previous = lastPreferences;
  let response = null;
  try {
    // Send the request to our Python server (the /api/route function in main.py).
    response = await withTimeout(fetch("/api/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start: { lat: places.from.lat, lon: places.from.lon },
        end: { lat: places.to.lat, lon: places.to.lon },
        instructions: sentInstructions,
        previous_preferences: lastPreferences,
      }),
    }), 60000);
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      showStatus(typeof data.detail === "string" ? data.detail
        : "Something went wrong planning this ride. Please try again.", true);
      return;
    }

    if (sentInstructions) conversation.push(sentInstructions);
    lastPreferences = data.preferences;
    document.getElementById("keeping").innerHTML = "";
    currentRoutes = data.routes;
    // Make sure the map can draw before we add routes to it (but don't wait forever).
    await withTimeout(mapReady, 10000).catch(() => {});
    showResults(data, previous);
    selectRoute(0); // the best route comes first

    // Show the stop on the way (if any) as a yellow pin.
    if (data.stop) {
      markers.stop.setLngLat([data.stop.lon, data.stop.lat])
        .setPopup(new maplibregl.Popup().setText(data.stop.emoji + " " + data.stop.name))
        .addTo(map);
    } else {
      markers.stop.remove();
    }

    const best = data.routes[0];
    showStatus("Found " + data.routes.length + " route" + (data.routes.length > 1 ? "s" : "") +
      ". Best match: " + best.duration_minutes + " min, " + best.distance_miles + " mi.");

    // Invite a follow-up, like a conversation (unless they already typed something new).
    if (instructionsBox.value.trim() === sentInstructions) instructionsBox.value = "";
    instructionsBox.placeholder = "Want changes? e.g. 'ok but faster' or 'avoid Monterey Rd too'";
    button.textContent = "Update route";
  } catch (error) {
    if (error.message === "timeout") {
      showStatus("This is taking too long. The map servers may be busy. Please try again.", true);
    } else if (response === null) {
      showStatus("Couldn't reach the Wayfinder server. Check your connection and try again.", true);
    } else {
      console.error(error);
      showStatus("Something went wrong showing this route. Please try again.", true);
    }
  } finally {
    clearInterval(loadingTimer);
    button.disabled = false;
    busy = false;
  }
}

document.getElementById("go").addEventListener("click", findRoute);

// Press Enter in the instructions box to search (Shift+Enter still makes a new line).
document.getElementById("instructions").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    findRoute();
  }
});


// ============ 5. SHOWING THE RESULTS ============

// A "normal" rider. Tags only show settings the AI moved away from these,
// so the rider sees what their words actually changed.
const NORMAL = {
  avoid_hills: 0.5, avoid_busy_roads: 0.5, prefer_bike_lanes: 0.3,
  speed_importance: 0.5, avoid_unpaved: 0.3,
};

function describeSetting(name, value) {
  const high = value >= 0.7;
  const low = value <= 0.3;
  switch (name) {
    case "avoid_hills":
      return value <= -0.3 ? "⛰️ Wants hills" : high ? "⛰️ Avoid hills" : low ? "⛰️ Hills OK" : null;
    case "avoid_busy_roads":
      return high ? "🚗 Avoid traffic" : low ? "🚗 Traffic OK" : null;
    case "prefer_bike_lanes":
      return value <= -0.3 ? "🚲 Avoid bike lanes" : high ? "🚲 Prefer bike lanes" : null;
    case "speed_importance":
      return high ? "⏱️ Speed matters" : low ? "⏱️ No rush" : null;
    case "avoid_unpaved":
      return high ? "🛣️ Paved only" : value <= 0.1 ? "🛣️ Dirt OK" : null;
  }
  return null;
}

// The "AI understood" box: the chat so far, then what the AI filled in.
function makeUnderstoodBox(data, previous) {
  const box = makeElement("div", "understood");

  for (const message of conversation) {
    box.appendChild(makeElement("div", "chat-you", "You: " + message));
  }
  box.appendChild(makeElement("div", "understood-text", "🤖 " + data.understood));

  const p = data.preferences;
  const tags = makeElement("div", "tags");
  const addTag = (text, changed) => {
    const tag = makeElement("span", "tag" + (changed ? " changed" : ""), text);
    if (changed) tag.title = "Changed by your last message";
    tags.appendChild(tag);
  };

  for (const name of Object.keys(NORMAL)) {
    const text = describeSetting(name, p[name]);
    const changed = previous && Math.abs(previous[name] - p[name]) >= 0.15;
    if (text) addTag(text, changed);
    else if (changed) addTag(name.replaceAll("_", " ") + ": back to normal", true);
  }
  const speedChanged = previous && previous.speed_mph !== p.speed_mph;
  addTag("🚴 " + p.bicycle_type + " bike · " + p.speed_mph + " mph", speedChanged);
  for (const street of p.avoid_streets) addTag("🚫 " + street);
  if (data.stop) addTag(data.stop.emoji + " Stop: " + data.stop.name);
  box.appendChild(tags);

  box.appendChild(makeElement("div", "ai-note",
    "Gemini only filled in these settings. Wayfinder's own code built, measured, and scored the routes below."));
  return box;
}

// A stacked bar showing where a route's score comes from (lower = better fit).
function makeScoreBar(route, worstScore) {
  const parts = route.score_parts;
  const box = makeElement("div", "score");

  const pieces = [
    ["time", "ride time", "score-time"],
    ["traffic", "traffic", "score-traffic"],
    ["hills", "hills", "score-hills"],
    ["bike_lanes", "bike lanes", "score-lanes"],
    ["avoided_street", "avoided street", "score-street"],
  ];
  const bar = makeElement("div", "score-bar");
  const words = [];
  for (const [key, label, className] of pieces) {
    const value = parts[key] || 0;
    if (Math.abs(value) < 0.5) continue;
    if (value > 0) {
      const segment = makeElement("span", className);
      segment.style.width = Math.min(100, (value / worstScore) * 100) + "%";
      segment.title = label + ": +" + Math.round(value);
      bar.appendChild(segment);
      words.push(Math.round(value) + " " + label);
    } else {
      words.push("−" + Math.round(-value) + " " + label + " bonus");
    }
  }
  box.appendChild(bar);
  box.appendChild(makeElement("div", "score-words",
    "Score " + Math.round(route.score) + " = " + words.join(" + ").replaceAll("+ −", "− ")));
  return box;
}

// "1.6 fewer miles..." -> "1.6 fewer miles...", "stops at..." -> "Stops at..."
function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function showResults(data, previous) {
  const results = document.getElementById("results");
  results.innerHTML = "";
  document.getElementById("panel").classList.add("has-results"); // hides the examples

  // --- What the rider said, and what the AI understood (so they can check it) ---
  results.appendChild(makeUnderstoodBox(data, previous));

  // --- Honest warnings when the route couldn't do everything asked ---
  if (data.warnings && data.warnings.length > 0) {
    const headsUp = makeElement("div", "heads-up");
    headsUp.appendChild(makeElement("strong", "", "⚠️ Heads up"));
    const list = makeElement("ul");
    for (const warning of data.warnings) list.appendChild(makeElement("li", "", capitalize(warning)));
    headsUp.appendChild(list);
    results.appendChild(headsUp);
  }

  // --- One card per route option (best first) ---
  results.appendChild(makeElement("h2", "section-title", "Route options"));
  const worstScore = Math.max(...data.routes.map((r) =>
    Object.values(r.score_parts).filter((v) => v > 0).reduce((a, b) => a + b, 0)), 1);

  data.routes.forEach((route, index) => {
    const m = route.metrics;
    const card = makeElement("div", "route-card");
    card.dataset.index = index;
    card.style.borderLeftColor = routeColor(route);

    const top = makeElement("div", "card-top");
    top.appendChild(makeElement("span", "card-name", (route.is_best ? "⭐ " : "") + route.label));
    top.appendChild(makeElement("span", "card-time", route.duration_minutes + " min"));
    card.appendChild(top);

    card.appendChild(makeElement("div", "card-description",
      route.description + " · " + route.distance_miles + " mi"));

    const stats = makeElement("div", "card-stats");
    stats.appendChild(makeElement("span", "", "⛰️ " + m.climb_ft + " ft climb"));
    stats.appendChild(makeElement("span", "", "🚗 " + m.busy_road_miles.toFixed(1) + " mi busy"));
    stats.appendChild(makeElement("span", "", "🚲 " + m.bike_lane_percent + "% bike lanes"));
    card.appendChild(stats);

    card.appendChild(makeScoreBar(route, worstScore));

    // The best card explains WHY it won (built from measured numbers, not AI text).
    if (route.is_best) {
      const why = makeElement("div", "why");
      why.appendChild(makeElement("strong", "", "Why this route"));
      const reasons = makeElement("ul");
      for (const reason of data.explanation) {
        const isSide = reason.startsWith("Tradeoff: ") || reason.startsWith("Why not");
        reasons.appendChild(makeElement("li", isSide ? "tradeoff" : "", capitalize(reason)));
      }
      why.appendChild(reasons);
      card.appendChild(why);
    }

    // Cards work with a mouse AND with the keyboard (Tab to it, then Enter or Space).
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.setAttribute("aria-label", route.label + " route, " + route.duration_minutes + " minutes");
    card.addEventListener("click", () => selectRoute(index));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectRoute(index);
      }
    });
    results.appendChild(card);
  });

  // --- How scoring works (for curious riders and judges) ---
  const how = makeElement("details", "how-scored");
  how.appendChild(makeElement("summary", "", "How are routes scored?"));
  how.appendChild(makeElement("p", "",
    "Each route's score is its riding time plus \"penalty minutes\" for things you said you " +
    "dislike: climbing, busy roads, bike lanes (if you'd rather avoid them), or a street you " +
    "banned. Bike lanes earn a bonus if you like them. The AI only sets how much each thing " +
    "matters to you. The measuring and scoring are Wayfinder's own code. Lowest score wins."));
  results.appendChild(how);

  // --- Elevation chart for the selected route (drawn by selectRoute) ---
  const chartBox = makeElement("div", "elevation-box");
  chartBox.appendChild(makeElement("h2", "section-title", "Elevation"));
  chartBox.appendChild(makeElement("div", "elevation-caption"));
  chartBox.appendChild(makeElement("div", "elevation-chart"));
  results.appendChild(chartBox);

  // --- Turn-by-turn steps for the selected route (filled in by selectRoute) ---
  const stepsBox = makeElement("details", "steps-box");
  stepsBox.appendChild(makeElement("summary", "", "Turn-by-turn directions"));
  stepsBox.appendChild(makeElement("ol", "steps"));
  results.appendChild(stepsBox);
}

// Highlight one route: on the map, its card, its chart, and its steps.
function selectRoute(index) {
  selectedIndex = index;
  const route = currentRoutes[index];

  document.querySelectorAll(".route-card").forEach((card) => {
    const isSelected = Number(card.dataset.index) === index;
    card.classList.toggle("selected", isSelected);
    card.setAttribute("aria-pressed", isSelected);
  });

  // Turn-by-turn steps, skipping tiny "steps" that are just a few feet long.
  const steps = document.querySelector(".steps");
  steps.innerHTML = "";
  route.steps.forEach((step, i) => {
    const isLast = i === route.steps.length - 1;
    if (step.distance_miles < 0.02 && i > 0 && !isLast && !step.instruction.includes("stop")) return;
    const item = makeElement("li", "", step.instruction + " ");
    if (step.distance_miles > 0) {
      item.appendChild(makeElement("span", "step-distance", step.distance_miles + " mi"));
    }
    steps.appendChild(item);
  });

  const m = route.metrics;
  document.querySelector(".elevation-caption").textContent =
    m.climb_ft + " ft total climb · steepest hill " + m.steepest_grade + "% · hover the chart to see where";
  drawElevationChart(m.elevation_profile);
  drawRoutes();

  // Let the 3D tools (three_d.js) know which route is shown now.
  if (window.onRouteSelected) window.onRouteSelected(route);
}

// Draw a simple hill-shaped chart of height along the route, as an SVG picture.
// Each point is [miles from start, height in feet].
function drawElevationChart(profile) {
  const chart = document.querySelector(".elevation-chart");
  if (!profile || profile.length < 2) {
    chart.textContent = "No elevation data for this route.";
    return;
  }

  const width = 300;
  const height = 70;
  const miles = profile.map((point) => point[0]);
  const feet = profile.map((point) => point[1]);
  const maxMiles = Math.max(...miles) || 1;
  const lowest = Math.min(...feet);
  const highest = Math.max(...feet);
  // Always show at least 100 ft of height, so gentle routes don't look like mountains.
  const range = Math.max(highest - lowest, 100);

  // Convert each [miles, feet] into an x, y position inside the picture.
  // (SVG's y goes DOWN, so higher ground needs a smaller y.)
  const xy = profile.map(([mi, ft]) => [
    (mi / maxMiles) * width,
    height - ((ft - lowest) / range) * (height - 6),
  ]);
  const line = xy.map(([x, y]) => x.toFixed(1) + "," + y.toFixed(1)).join(" ");
  const filled = "0," + height + " " + line + " " + width + "," + height;

  chart.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"
         aria-label="Elevation from ${lowest} to ${highest} feet over ${maxMiles} miles">
      <polygon points="${filled}" class="elevation-fill" />
      <polyline points="${line}" class="elevation-line" />
    </svg>
    <div class="elevation-labels">
      <span>Start</span><span>${lowest}–${highest} ft</span><span>${maxMiles.toFixed(1)} mi</span>
    </div>`;
}

// Draw every route option in its own color. The selected one is drawn on top, thicker.
// The typical route is dashed, so it's easy to compare against.
function drawRoutes() {
  const allRoutes = {
    type: "FeatureCollection",
    features: currentRoutes.map((route, index) => ({
      type: "Feature",
      properties: {
        index: index,
        selected: index === selectedIndex,
        typical: Boolean(route.is_typical),
        color: routeColor(route),
      },
      geometry: { type: "LineString", coordinates: route.geometry },
    })),
  };

  if (map.getSource("routes")) {
    map.getSource("routes").setData(allRoutes);
  } else {
    map.addSource("routes", { type: "geojson", data: allRoutes });
    const roundLines = { "line-cap": "round", "line-join": "round" };
    const notSelected = ["==", ["get", "selected"], false];

    // Other options (click one to select it): the typical route dashed, others solid.
    map.addLayer({
      id: "other-routes",
      type: "line",
      source: "routes",
      filter: ["all", notSelected, ["==", ["get", "typical"], false]],
      layout: roundLines,
      paint: { "line-color": ["get", "color"], "line-width": 5, "line-opacity": 0.6 },
    });
    map.addLayer({
      id: "typical-route",
      type: "line",
      source: "routes",
      filter: ["all", notSelected, ["==", ["get", "typical"], true]],
      layout: { "line-join": "round" },
      paint: {
        "line-color": ROUTE_COLORS.typical, "line-width": 4, "line-opacity": 0.8,
        "line-dasharray": [2, 1.5],
      },
    });
    // White outline + colored line for the selected route, drawn on top.
    map.addLayer({
      id: "selected-outline",
      type: "line",
      source: "routes",
      filter: ["==", ["get", "selected"], true],
      layout: roundLines,
      paint: { "line-color": "#ffffff", "line-width": 9 },
    });
    map.addLayer({
      id: "selected-route",
      type: "line",
      source: "routes",
      filter: ["==", ["get", "selected"], true],
      layout: roundLines,
      paint: { "line-color": ["get", "color"], "line-width": 5 },
    });

    for (const layer of ["other-routes", "typical-route"]) {
      map.on("click", layer, (event) => selectRoute(event.features[0].properties.index));
      map.on("mouseenter", layer, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", layer, () => (map.getCanvas().style.cursor = ""));
    }
  }

  zoomToRoute(currentRoutes[selectedIndex].geometry);
}

// Zoom the map so the route fits on screen, leaving room for the panel
// (on the left on computers, at the bottom on phones).
function zoomToRoute(coordinates) {
  const bounds = new maplibregl.LngLatBounds();
  for (const point of coordinates) bounds.extend(point);
  const isPhone = window.innerWidth <= 600;
  const padding = isPhone
    ? { top: 40, bottom: window.innerHeight * 0.55 + 20, left: 30, right: 30 }
    : { top: 60, bottom: 60, left: 420, right: 60 };
  map.fitBounds(bounds, { padding: padding });
}
