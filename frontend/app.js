// app.js: the "muscles" of the page. Everything interactive lives here.
//
// The big picture:
//   1. Show a map.
//   2. Let the user search for a start and a destination.
//   3. When they click "Find my route", ask our Python server for a route.
//   4. Draw the route on the map and list the turn-by-turn steps.


// ============ 1. THE MAP ============

const map = new maplibregl.Map({
  container: "map",                                      // the <div id="map">
  style: "https://tiles.openfreemap.org/styles/liberty", // free street map pictures
  center: [-121.8863, 37.3382],                          // [longitude, latitude] of San Jose
  zoom: 12,
});

map.addControl(new maplibregl.NavigationControl(), "top-right");

// A promise that finishes once the map has loaded, so we never draw a route too early.
const mapReady = new Promise((resolve) => map.on("load", resolve));

// The start and end points the user picked. null = not picked yet.
const places = { from: null, to: null };

// What the AI understood last time. Sent back with the next request so
// follow-ups like "ok but shorter" adjust the ride instead of starting over.
let lastPreferences = null;

// The routes from the last search, and which one is shown in blue.
let currentRoutes = [];
let selectedIndex = 0;

// The green and red pins on the map.
const markers = {
  from: new maplibregl.Marker({ color: "#34a853" }),
  to: new maplibregl.Marker({ color: "#ea4335" }),
  stop: new maplibregl.Marker({ color: "#f9ab00" }),  // yellow = stop on the way
};


// ============ 2. SEARCH BOXES ============

// Ask our server for places matching the text, e.g. "santana row".
async function searchPlaces(text) {
  try {
    const response = await fetch("/api/search?q=" + encodeURIComponent(text));
    if (!response.ok) return [];
    return await response.json();
  } catch (error) {
    return []; // offline or server down: just show no suggestions
  }
}

// Save a picked place: remember it, fill the box, drop a pin, move the map.
function pickPlace(which, place) {
  places[which] = place;
  // New trip: keep how the rider likes to ride (kid, hills...), but forget the
  // things that only made sense for the old trip (streets to avoid, the stop).
  if (lastPreferences) {
    lastPreferences = { ...lastPreferences, avoid_streets: [], stop_type: "none" };
  }
  document.getElementById(which).value = place.label;
  markers[which].setLngLat([place.lon, place.lat]).addTo(map);
  map.flyTo({ center: [place.lon, place.lat], zoom: 14 });
}

// Make one search box show suggestions while the user types.
// `which` is "from" or "to" (the id of the <input>).
function setUpSearchBox(which) {
  const input = document.getElementById(which);
  const list = document.getElementById(which + "-suggestions");
  let typingTimer = null;

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
      const results = await searchPlaces(text);
      // If the user kept typing (or left the box) while we waited, these results are old.
      if (input.value.trim() !== text || document.activeElement !== input) return;
      list.innerHTML = "";
      highlighted = -1;
      for (const place of results) {
        const item = document.createElement("li");
        item.textContent = place.label;
        item.setAttribute("role", "option");
        // "mousedown" fires before the box loses focus, so the click always counts.
        item.addEventListener("mousedown", (event) => {
          event.preventDefault();
          pickPlace(which, place);
          list.innerHTML = "";
        });
        item.place = place;
        list.appendChild(item);
      }
      if (results.length === 0) {
        list.appendChild(Object.assign(document.createElement("li"), {
          textContent: "No places found in California", className: "no-results",
        }));
      }
    }, 300);
  });

  // Arrow keys move through suggestions, Enter picks one, Escape closes the list.
  let highlighted = -1;
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
      if (items.length > 0) {
        pickPlace(which, items[Math.max(highlighted, 0)].place);
        list.innerHTML = "";
      }
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
        label: "My location",
        lat: position.coords.latitude,
        lon: position.coords.longitude,
      });
    },
    () => showStatus("Couldn't get your location. Check your browser's permission.", true)
  );
});


// ============ 3. GETTING A ROUTE ============

// If the user typed a place but never clicked a suggestion, use the top result.
async function makeSurePicked(which) {
  if (places[which]) return true;

  const text = document.getElementById(which).value.trim();
  if (!text) return false;

  const results = await searchPlaces(text);
  if (results.length === 0) return false;
  pickPlace(which, results[0]);
  return true;
}

function showStatus(message, isError = false) {
  const status = document.getElementById("status");
  status.textContent = message;
  status.className = isError ? "error" : "";
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

  showStatus("Finding your places...");
  if (!(await makeSurePicked("from")) || !(await makeSurePicked("to"))) {
    showStatus("Please choose both a start and a destination.", true);
    button.disabled = false;
    busy = false;
    return;
  }

  // Show what's happening while the server works (it takes a few seconds).
  const loadingMessages = [
    "🤖 Reading your request...",
    "🗺️ Building route options...",
    "⛰️ Measuring hills and traffic on every street...",
    "⚖️ Scoring routes for you...",
  ];
  let messageNumber = 0;
  showStatus(loadingMessages[0]);
  const loadingTimer = setInterval(() => {
    messageNumber = Math.min(messageNumber + 1, loadingMessages.length - 1);
    showStatus(loadingMessages[messageNumber]);
  }, 1500);

  const sentInstructions = instructionsBox.value;
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

    showStatus("");
    lastPreferences = data.preferences;
    currentRoutes = data.routes;
    // Make sure the map can draw before we add routes to it (but don't wait forever).
    await withTimeout(mapReady, 10000).catch(() => {});
    showResults(data);
    selectRoute(0); // the best route comes first

    // Show the stop on the way (if any) as a yellow pin.
    if (data.stop) {
      markers.stop.setLngLat([data.stop.lon, data.stop.lat])
        .setPopup(new maplibregl.Popup().setText(data.stop.emoji + " " + data.stop.name))
        .addTo(map);
    } else {
      markers.stop.remove();
    }

    // Invite a follow-up, like a conversation (unless they already typed something new).
    if (instructionsBox.value === sentInstructions) instructionsBox.value = "";
    instructionsBox.placeholder = "Want changes? e.g. 'ok but shorter' or 'avoid Monterey Rd too'";
    button.textContent = "Update route";
  } catch (error) {
    if (error.message === "timeout") {
      showStatus("This is taking too long. The free map servers may be busy. Please try again.", true);
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


// ============ 4. SHOWING THE RESULTS ============

// Turn a preference number into a word for the little tags.
// Hills and bike lanes can be negative, meaning the rider wants the opposite.
function level(value) {
  if (value >= 0.7) return "high";
  if (value <= -0.3) return "opposite";
  if (value <= 0.3) return "low";
  return "medium";
}

function hillsTag(value) {
  if (value <= -0.3) return "Hills: bring them on";
  return "Avoid hills: " + level(value);
}

function bikeLanesTag(value) {
  if (value <= -0.3) return "Bike lanes: avoid";
  return "Bike lanes: " + level(value);
}

// Make a small HTML element with a class and text (safer than building HTML strings).
function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function showResults(data) {
  const results = document.getElementById("results");
  results.innerHTML = "";

  // --- What the AI understood (so the rider can check it) ---
  const understood = makeElement("div", "understood");
  understood.appendChild(makeElement("div", "understood-text", "🤖 " + data.understood));

  const p = data.preferences;
  const tags = makeElement("div", "tags");
  const tagTexts = [
    hillsTag(p.avoid_hills),
    "Avoid traffic: " + level(p.avoid_busy_roads),
    bikeLanesTag(p.prefer_bike_lanes),
    "Speed matters: " + level(p.speed_importance),
    p.bicycle_type + " bike · " + p.speed_mph + " mph",
  ];
  for (const street of p.avoid_streets) tagTexts.push("🚫 " + street);
  if (data.stop) tagTexts.push(data.stop.emoji + " Stop: " + data.stop.name);
  for (const text of tagTexts) tags.appendChild(makeElement("span", "tag", text));
  understood.appendChild(tags);
  results.appendChild(understood);

  // --- Why the best route was chosen (built from measured numbers) ---
  const why = makeElement("div", "why");
  why.appendChild(makeElement("strong", "", "Why this route"));
  const reasons = makeElement("ul");
  for (const reason of data.explanation) {
    const isTradeoff = reason.startsWith("Tradeoff: ");
    reasons.appendChild(makeElement("li", isTradeoff ? "tradeoff" : "", reason));
  }
  why.appendChild(reasons);
  results.appendChild(why);

  // --- Honest warnings when the route couldn't do everything asked ---
  if (data.warnings && data.warnings.length > 0) {
    const headsUp = makeElement("div", "heads-up");
    headsUp.appendChild(makeElement("strong", "", "⚠️ Heads up"));
    const list = makeElement("ul");
    for (const warning of data.warnings) list.appendChild(makeElement("li", "", warning));
    headsUp.appendChild(list);
    results.appendChild(headsUp);
  }

  // --- One card per route option ---
  data.routes.forEach((route, index) => {
    const m = route.metrics;
    const card = makeElement("div", "route-card");
    card.dataset.index = index;

    const top = makeElement("div", "card-top");
    top.appendChild(makeElement("span", "card-name", (route.is_best ? "⭐ " : "") + route.name));
    top.appendChild(makeElement("span", "card-time", route.duration_minutes + " min"));
    card.appendChild(top);

    card.appendChild(makeElement("div", "card-description",
      route.description + " · " + route.distance_miles + " mi"));

    const stats = makeElement("div", "card-stats");
    stats.appendChild(makeElement("span", "", "⛰️ " + m.climb_ft + " ft climb"));
    stats.appendChild(makeElement("span", "", "🚗 " + m.busy_road_miles + " mi busy"));
    stats.appendChild(makeElement("span", "", "🚲 " + m.bike_lane_percent + "% bike lanes"));
    card.appendChild(stats);

    // Cards work with a mouse AND with the keyboard (Tab to it, then Enter or Space).
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.setAttribute("aria-label", route.name + " route, " + route.duration_minutes + " minutes");
    card.addEventListener("click", () => selectRoute(index));
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectRoute(index);
      }
    });
    results.appendChild(card);
  });

  // --- Elevation chart for the selected route (drawn by selectRoute) ---
  const chartBox = makeElement("div", "elevation-box");
  chartBox.appendChild(makeElement("strong", "", "Elevation"));
  chartBox.appendChild(makeElement("div", "elevation-chart"));
  results.appendChild(chartBox);

  // --- Turn-by-turn steps for the selected route (filled in by selectRoute) ---
  const stepsBox = makeElement("details", "steps-box");
  stepsBox.appendChild(makeElement("summary", "", "Turn-by-turn directions"));
  stepsBox.appendChild(makeElement("ol", "steps"));
  results.appendChild(stepsBox);
}

// Highlight one route: blue on the map, highlighted card, its steps listed.
function selectRoute(index) {
  selectedIndex = index;
  const route = currentRoutes[index];

  document.querySelectorAll(".route-card").forEach((card) => {
    const isSelected = Number(card.dataset.index) === index;
    card.classList.toggle("selected", isSelected);
    card.setAttribute("aria-pressed", isSelected);
  });

  const steps = document.querySelector(".steps");
  steps.innerHTML = "";
  for (const step of route.steps) {
    const item = makeElement("li", "", step.instruction + " ");
    if (step.distance_miles > 0) {
      item.appendChild(makeElement("span", "step-distance", step.distance_miles + " mi"));
    }
    steps.appendChild(item);
  }

  drawElevationChart(route.metrics.elevation_profile);
  drawRoutes();
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
  const range = Math.max(highest - lowest, 30); // so flat routes don't look like mountains

  // Convert each [miles, feet] into an x, y position inside the picture.
  // (SVG's y goes DOWN, so higher ground needs a smaller y.)
  const xy = profile.map(([mi, ft]) => [
    (mi / maxMiles) * width,
    height - ((ft - lowest) / range) * (height - 6),
  ]);
  const line = xy.map(([x, y]) => x.toFixed(1) + "," + y.toFixed(1)).join(" ");
  const filled = "0," + height + " " + line + " " + width + "," + height;

  chart.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <polygon points="${filled}" class="elevation-fill" />
      <polyline points="${line}" class="elevation-line" />
    </svg>
    <div class="elevation-labels">
      <span>Low ${lowest} ft</span><span>High ${highest} ft</span>
    </div>`;
}

// Draw every route option: the selected one in blue, the others in gray.
function drawRoutes() {
  const allRoutes = {
    type: "FeatureCollection",
    features: currentRoutes.map((route, index) => ({
      type: "Feature",
      properties: { index: index, selected: index === selectedIndex },
      geometry: { type: "LineString", coordinates: route.geometry },
    })),
  };

  if (map.getSource("routes")) {
    map.getSource("routes").setData(allRoutes);
  } else {
    map.addSource("routes", { type: "geojson", data: allRoutes });
    const roundLines = { "line-cap": "round", "line-join": "round" };

    // Gray lines for the other options (click one to select it).
    map.addLayer({
      id: "other-routes",
      type: "line",
      source: "routes",
      filter: ["==", ["get", "selected"], false],
      layout: roundLines,
      paint: { "line-color": "#9aa0a6", "line-width": 6, "line-opacity": 0.8 },
    });
    // White outline + blue line for the selected route, drawn on top.
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
      paint: { "line-color": "#1a73e8", "line-width": 5 },
    });

    map.on("click", "other-routes", (event) => selectRoute(event.features[0].properties.index));
    map.on("mouseenter", "other-routes", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "other-routes", () => (map.getCanvas().style.cursor = ""));
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
