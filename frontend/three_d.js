// three_d.js: the 3D tools. They help a rider SEE the hills before riding.
//
// The big picture:
//   1. A "3D" button tilts the map and raises real hills out of it.
//   2. A "Preview ride" button flies the camera along the chosen route, like a
//      drone following you, showing your height and how steep the road is.
//   3. Pointing at the elevation chart shows that exact spot on the map.
//
// It uses things app.js made: map, mapReady, currentRoutes, selectedIndex.
// Everything lives inside this one function, so our names can't clash with app.js.

(function () {
  "use strict";

  // If app.js didn't manage to create the map (e.g. no internet), quietly do nothing.
  function mapExists() {
    try {
      return Boolean(map && mapReady);
    } catch (error) {
      return false;
    }
  }
  if (!mapExists()) {
    console.warn("3D tools: the map isn't available, so 3D is turned off.");
    return;
  }


  // ============ 1. SETTINGS ============

  // Free elevation pictures from AWS: each pixel's color encodes a height.
  const TERRAIN_TILES = "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png";
  const HILL_EXAGGERATION = 1.8;  // draw hills 1.8x taller than real, so they're easy to see
  const TILT_3D = 60;             // how far the map leans back in 3D (0 = looking straight down)
  const PREVIEW_TILT = 65;        // the ride preview leans a bit more, like riding behind someone
  const LOOK_AHEAD_METERS = 200;  // during the preview, the camera points at the road this far ahead
  const SECONDS_PER_MILE = 4;     // preview length: 4 seconds per mile...
  const SHORTEST_PREVIEW = 12;    // ...but never shorter than 12 seconds
  const LONGEST_PREVIEW = 35;     // ...or longer than 35 seconds
  const GRADE_WINDOW_MILES = 0.1; // measure steepness over 0.1 mile (about a city block or two)
  const FEET_PER_MILE = 5280;
  const METERS_PER_MILE = 1609.34;

  // Some people turn on "reduce motion" because smooth camera flights make them feel sick.
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // Our own map sources and layers (named "wf-..." so they never clash with the map style's).
  const TERRAIN_SOURCE = "wf-terrain";
  const HILLSHADE_SOURCE = "wf-hillshade"; // MapLibre draws better with a separate copy for shading
  const HILLSHADE_LAYER = "wf-hillshade";


  // ============ 2. SMALL MATH HELPERS ============

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  // Distance in meters between two [lon, lat] points ("haversine" formula for a round Earth).
  function metersBetween(a, b) {
    const toRadians = Math.PI / 180;
    const dLat = (b[1] - a[1]) * toRadians;
    const dLon = (b[0] - a[0]) * toRadians;
    const h = Math.sin(dLat / 2) ** 2 +
      Math.cos(a[1] * toRadians) * Math.cos(b[1] * toRadians) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.sqrt(h));
  }

  // Compass direction from point a to point b, in degrees (0 = north, 90 = east).
  function compassDirection(a, b) {
    const toRadians = Math.PI / 180;
    const lat1 = a[1] * toRadians;
    const lat2 = b[1] * toRadians;
    const dLon = (b[0] - a[0]) * toRadians;
    const y = Math.sin(dLon) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
    return Math.atan2(y, x) / toRadians;
  }

  // Turn smoothly from one direction toward another, always the short way round
  // (from 350° to 10° is a 20° turn, not 340°). `amount` is 0..1 of the way.
  function turnToward(current, target, amount) {
    const difference = ((target - current + 540) % 360) - 180;
    return current + difference * amount;
  }

  // Measure a route once: how far along the road each point is, in meters.
  // Remembered per route, so hovering the chart doesn't redo the math every time.
  const measuredRoutes = new WeakMap();
  function measureRoute(route) {
    if (!measuredRoutes.has(route)) {
      const points = route.geometry;
      const distances = [0];
      for (let i = 1; i < points.length; i++) {
        distances.push(distances[i - 1] + metersBetween(points[i - 1], points[i]));
      }
      measuredRoutes.set(route, { points, distances, total: distances[distances.length - 1] });
    }
    return measuredRoutes.get(route);
  }

  // The [lon, lat] spot that is `meters` along the route.
  function pointAlong(path, meters) {
    const { points, distances } = path;
    if (meters <= 0) return points[0];
    if (meters >= path.total) return points[points.length - 1];

    // Find the piece of road that contains this distance...
    let i = 1;
    while (i < distances.length - 1 && distances[i] < meters) i++;
    // ...then slide the right fraction of the way along that piece.
    const pieceLength = distances[i] - distances[i - 1];
    const fraction = pieceLength > 0 ? (meters - distances[i - 1]) / pieceLength : 0;
    const a = points[i - 1];
    const b = points[i];
    return [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction];
  }

  // The selected route's elevation profile ([miles, feet] pairs), or null if it has none.
  function profileOf(route) {
    const profile = route && route.metrics && route.metrics.elevation_profile;
    return Array.isArray(profile) && profile.length >= 2 ? profile : null;
  }

  // The last mile marker in the profile (the elevation chart's right edge).
  function profileLength(profile) {
    return Math.max(...profile.map((point) => point[0])) || 1;
  }

  // Height in feet at `miles` from the start, reading between the profile's points.
  function elevationAt(profile, miles) {
    if (miles <= profile[0][0]) return profile[0][1];
    for (let i = 1; i < profile.length; i++) {
      const [miles0, feet0] = profile[i - 1];
      const [miles1, feet1] = profile[i];
      if (miles <= miles1) {
        if (miles1 <= miles0) return feet1;
        return feet0 + (feet1 - feet0) * (miles - miles0) / (miles1 - miles0);
      }
    }
    return profile[profile.length - 1][1];
  }

  // Steepness in percent around `miles`: feet gained per 100 feet ridden,
  // measured over about 0.1 mile. Positive = uphill, negative = downhill.
  function gradeAt(profile, miles) {
    const first = profile[0][0];
    const last = profile[profile.length - 1][0];
    const from = Math.max(first, miles - GRADE_WINDOW_MILES / 2);
    const to = Math.min(last, miles + GRADE_WINDOW_MILES / 2);
    if (to - from < 0.02) return 0; // too short a stretch to measure
    const rise = elevationAt(profile, to) - elevationAt(profile, from);
    return (rise / ((to - from) * FEET_PER_MILE)) * 100;
  }

  // Words and a color for a grade: green = easy, orange = moderate, red = steep.
  // (Steep downhills are red too: they need care, especially with kids.)
  function describeGrade(grade) {
    const percent = Math.round(grade) || 0; // "|| 0" turns -0 into 0
    const steepness = Math.abs(grade);
    const level = steepness < 3 ? "easy" : steepness < 6 ? "moderate" : "steep";
    const arrow = percent > 0 ? "↗" : percent < 0 ? "↘" : "→";
    return { percent, level, arrow, className: "wf-grade-" + level };
  }

  // The route that is currently shown in blue, or null if there isn't one yet.
  function selectedRoute() {
    const route = currentRoutes && currentRoutes[selectedIndex];
    return route && Array.isArray(route.geometry) && route.geometry.length >= 2 ? route : null;
  }


  // ============ 3. BUTTONS AND THE PREVIEW CARD ============

  // Make a small HTML element with a class and text.
  function makeEl(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  // The empty <div id="map-tools"> from index.html (made here if it's missing).
  let tools = document.getElementById("map-tools");
  if (!tools) {
    tools = makeEl("div");
    tools.id = "map-tools";
    document.body.appendChild(tools);
  }

  const button3D = makeEl("button", "wf-tool-button");
  button3D.type = "button";
  button3D.setAttribute("aria-label", "3D hills view");
  tools.appendChild(button3D);

  const previewButton = makeEl("button", "wf-tool-button");
  previewButton.type = "button";
  tools.appendChild(previewButton);

  // The little card that shows progress, height and steepness during a preview.
  const card = makeEl("div", "wf-preview-card");
  card.hidden = true;
  card.setAttribute("role", "group");
  card.setAttribute("aria-label", "Ride preview");
  card.appendChild(makeEl("div", "wf-card-title", "🚲 Ride preview"));
  const progressBar = makeEl("div", "wf-progress");
  const progressFill = makeEl("div", "wf-progress-fill");
  progressBar.appendChild(progressFill);
  card.appendChild(progressBar);
  const milesText = makeEl("div", "wf-card-miles");
  card.appendChild(milesText);
  const statsRow = makeEl("div", "wf-card-stats");
  const elevationText = makeEl("span", "wf-card-elevation");
  const gradeText = makeEl("span", "wf-card-grade");
  statsRow.appendChild(elevationText);
  statsRow.appendChild(gradeText);
  card.appendChild(statsRow);
  card.appendChild(makeEl("div", "wf-card-hint", "Press Esc or ■ Stop to end"));
  tools.appendChild(card);

  // Invisible text that screen readers announce ("Ride preview started", etc).
  const announcer = makeEl("span", "wf-sr-only");
  announcer.setAttribute("aria-live", "polite");
  tools.appendChild(announcer);

  function announce(message) {
    announcer.textContent = message;
  }

  // Keep both buttons' words and states in sync with what's happening.
  function updateButtons() {
    button3D.textContent = wants3D ? "🗺️ 2D" : "🏔️ 3D";
    button3D.setAttribute("aria-pressed", wants3D ? "true" : "false");
    button3D.title = wants3D ? "Back to the flat map" : "Show hills in 3D";

    const hasRoute = selectedRoute() !== null;
    previewButton.disabled = !hasRoute && !preview;
    previewButton.classList.toggle("wf-stop", Boolean(preview));
    if (preview) {
      previewButton.textContent = "■ Stop";
      previewButton.setAttribute("aria-label", "Stop the ride preview");
      previewButton.title = "Stop the ride preview (Esc)";
    } else {
      previewButton.textContent = "▶ Preview ride";
      previewButton.setAttribute("aria-label", "Preview the ride in 3D");
      previewButton.title = hasRoute ? "Fly along your route in 3D" : "Find a route first";
    }
  }


  // ============ 4. THE 3D VIEW ============

  let wants3D = false; // what the button says (changes the moment you click)
  let shows3D = false; // what the map is actually showing (changes once the map is ready)

  // Add the hill data and hill shading to the map. Only done once, the first time 3D is used.
  function addHillLayers() {
    // Allow tilting a little further than MapLibre's normal 60°, for the ride preview.
    if (map.getMaxPitch() < 70) map.setMaxPitch(70);

    const elevationTiles = {
      type: "raster-dem",
      tiles: [TERRAIN_TILES],
      encoding: "terrarium",
      tileSize: 256,
      maxzoom: 15,
    };
    if (!map.getSource(TERRAIN_SOURCE)) {
      // The 3D shape stops at zoom 14 (about 10 m detail, plenty for hills), the same
      // as the street map's own tiles; going higher makes MapLibre print a warning.
      map.addSource(TERRAIN_SOURCE, {
        ...elevationTiles,
        maxzoom: 14,
        attribution: "Elevation: Mapzen Terrain Tiles on AWS",
      });
    }
    if (!map.getSource(HILLSHADE_SOURCE)) map.addSource(HILLSHADE_SOURCE, elevationTiles);

    if (!map.getLayer(HILLSHADE_LAYER)) {
      // Soft shadows on the sides of hills facing away from the "sun".
      map.addLayer({
        id: HILLSHADE_LAYER,
        type: "hillshade",
        source: HILLSHADE_SOURCE,
        layout: { visibility: "none" },
        paint: {
          "hillshade-exaggeration": 0.35,
          "hillshade-shadow-color": "rgba(60, 50, 30, 0.6)",
          "hillshade-highlight-color": "rgba(255, 255, 255, 0.3)",
          "hillshade-accent-color": "rgba(60, 50, 30, 0.25)",
        },
      }, hillshadeGoesBefore());
    }

    // A soft sky and haze on the horizon, so the tilted view doesn't end in a blank edge.
    try {
      map.setSky({
        "sky-color": "#9cc7f0",
        "horizon-color": "#e8f1fa",
        "fog-color": "#f2f4f6",
        "sky-horizon-blend": 0.6,
        "horizon-fog-blend": 0.7,
        "fog-ground-blend": 0.85,
      });
    } catch (error) {
      // Older MapLibre without a sky: not important, skip it.
    }
  }

  // Put the shading under the roads (so streets stay crisp), but above parks and water.
  // If this map style has no roads we recognize, fall back to "under the first label".
  function hillshadeGoesBefore() {
    const layers = map.getStyle().layers;
    const firstRoad = layers.find((layer) => /^(tunnel|road|bridge)/.test(layer.id));
    const firstLabel = layers.find((layer) => layer.type === "symbol");
    const firstRoute = layers.find((layer) => layer.id === "other-routes");
    return (firstRoad || firstLabel || firstRoute || {}).id;
  }

  // Turn 3D on or off. Safe to call before the map has loaded: it waits for it.
  // `moveCamera` = false when the ride preview is about to move the camera itself.
  function set3D(turnOn, moveCamera = true) {
    wants3D = turnOn;
    updateButtons();

    return mapReady.then(() => {
      if (wants3D === shows3D) return; // already showing what was asked (or clicked twice)
      const turningOn = wants3D;
      try {
        addHillLayers();
        if (turningOn) {
          map.setTerrain({ source: TERRAIN_SOURCE, exaggeration: HILL_EXAGGERATION });
          map.setLayoutProperty(HILLSHADE_LAYER, "visibility", "visible");
          if (map.getLayer("building-3d")) map.setLayoutProperty("building-3d", "visibility", "visible");
          // Lean back and turn a little, so the hills stand out against each other.
          if (moveCamera) map.easeTo({ pitch: TILT_3D, bearing: map.getBearing() - 20, duration: 1200 });
        } else {
          map.setTerrain(null);
          map.setLayoutProperty(HILLSHADE_LAYER, "visibility", "none");
          if (moveCamera) map.easeTo({ pitch: 0, bearing: 0, duration: 1000 });
        }
        shows3D = turningOn;
      } catch (error) {
        // Something went wrong: put the button back to match what the map really shows.
        console.warn("3D tools: couldn't switch the 3D view.", error);
        wants3D = shows3D;
        updateButtons();
      }
    });
  }

  button3D.addEventListener("click", () => {
    if (wants3D && preview) stopPreview(false); // going flat ends the fly-through
    set3D(!wants3D);
  });

  // If some hill pictures fail to download, the hills there just look flat. Keep going.
  // (Every other map error is still printed, the same way MapLibre normally does.)
  let warnedAboutHills = false;
  map.on("error", (event) => {
    if (event.sourceId === TERRAIN_SOURCE || event.sourceId === HILLSHADE_SOURCE) {
      if (!warnedAboutHills) console.warn("3D tools: some hill data didn't load; those hills will look flat.");
      warnedAboutHills = true;
      return;
    }
    console.error(event.error || event);
  });


  // ============ 5. RIDE PREVIEW (the camera fly-through) ============

  let preview = null; // details about the running preview, or null when nothing is playing

  // The 🚲 marker that rides along the route.
  const riderElement = makeEl("div", "wf-rider", "🚲");
  riderElement.setAttribute("aria-hidden", "true");
  const riderMarker = new maplibregl.Marker({ element: riderElement });

  async function startPreview() {
    const route = selectedRoute();
    if (!route) return;
    stopPreview(false);

    const path = measureRoute(route);
    if (path.total < 1) return; // start and end are the same spot: nothing to fly along

    const miles = route.distance_miles || path.total / METERS_PER_MILE;
    let seconds = clamp(miles * SECONDS_PER_MILE, SHORTEST_PREVIEW, LONGEST_PREVIEW);
    const reduced = reducedMotion.matches;
    if (reduced) seconds = Math.min(seconds, 12); // a few still views, not a long flight

    // Faster flights (long routes) fly a little higher, so it doesn't feel dizzy.
    const metersPerSecond = path.total / seconds;
    const zoom = clamp(16.3 - metersPerSecond / 500, 14.8, 16);

    const profile = profileOf(route);
    const run = {
      route,
      path,
      miles,
      zoom,
      reduced,
      profile,
      profileMiles: profile ? profileLength(profile) : 0,
      duration: seconds * 1000,
      startTime: null,  // set on the first animation frame
      lastTime: null,
      bearing: 0,       // which way the camera faces (smoothed)
      shownT: -1,       // last progress drawn (used for reduced motion)
      frame: null,      // the requestAnimationFrame id, so we can cancel it
      timer: null,      // a setTimeout id, so we can cancel it
    };
    preview = run;

    // Show the rider, the card and the Stop button straight away.
    riderMarker.setLngLat(path.points[0]).addTo(map);
    card.hidden = false;
    document.body.classList.add("wf-previewing"); // on phones, this slides the panel away
    updateCard(run, 0);
    updateButtons();
    announce("Ride preview started. Press Escape to stop.");

    await set3D(true, false); // hills on (waits for the map if it's still loading)
    if (preview !== run) return; // stopped while we were waiting
    fadeBuildings(true);

    // Glide to a spot just behind the start, facing down the road, then begin.
    run.bearing = directionAhead(run, 0, run.bearing);
    const leadIn = reduced ? 0 : 1500;
    map.easeTo({
      center: path.points[0],
      zoom: zoom,
      pitch: PREVIEW_TILT,
      bearing: run.bearing,
      duration: leadIn,
    });
    run.timer = setTimeout(() => {
      if (preview === run) run.frame = requestAnimationFrame(previewFrame);
    }, leadIn + 50);
  }

  // Downtown, tall 3D buildings can hide the route line. During the preview we make
  // them see-through, then put them back the way the map style had them.
  let normalBuildingOpacity = null;
  function fadeBuildings(fade) {
    if (!map.getLayer("building-3d")) return;
    if (fade && normalBuildingOpacity === null) {
      normalBuildingOpacity = map.getPaintProperty("building-3d", "fill-extrusion-opacity") ?? 1;
      map.setPaintProperty("building-3d", "fill-extrusion-opacity", 0.45);
    } else if (!fade && normalBuildingOpacity !== null) {
      map.setPaintProperty("building-3d", "fill-extrusion-opacity", normalBuildingOpacity);
      normalBuildingOpacity = null;
    }
  }

  // Which way the road goes from `meters` along: toward a point a bit further ahead.
  // Looking ahead (not just at the next point) makes the camera start turning
  // BEFORE a corner, the way a real rider looks into a turn.
  function directionAhead(run, meters, fallback) {
    const here = pointAlong(run.path, meters);
    const ahead = pointAlong(run.path, Math.min(meters + LOOK_AHEAD_METERS, run.path.total));
    // Right at the finish there's nothing ahead, so keep facing the same way.
    return metersBetween(here, ahead) > 10 ? compassDirection(here, ahead) : fallback;
  }

  // Draw one frame of the preview. The browser calls this about 60 times a second.
  function previewFrame(now) {
    const run = preview;
    if (!run) return;
    if (run.startTime === null) {
      run.startTime = now;
      run.lastTime = now;
    }
    // Seconds since the last frame (capped, in case the computer hiccuped).
    const secondsPassed = Math.min((now - run.lastTime) / 1000, 0.1);
    run.lastTime = now;

    // How far through the ride we are: 0 = start, 1 = finish.
    let t = Math.min((now - run.startTime) / run.duration, 1);
    if (run.reduced) {
      // Reduced motion: hop between 6 still views instead of gliding.
      t = Math.min(Math.floor(t * 6) / 5, 1);
    }

    if (t !== run.shownT) {
      run.shownT = t;
      const meters = t * run.path.total;
      const here = pointAlong(run.path, meters);
      const target = directionAhead(run, meters, run.bearing);

      if (run.reduced) {
        run.bearing = target;
      } else {
        // Turn only part of the way each frame, so the camera swings smoothly
        // instead of snapping at every bend in the road.
        run.bearing = turnToward(run.bearing, target, 1 - Math.exp(-secondsPassed / 0.6));
      }

      riderMarker.setLngLat(here);
      map.jumpTo({ center: here, zoom: run.zoom, pitch: PREVIEW_TILT, bearing: run.bearing });
      updateCard(run, t);
    }

    if (t < 1) {
      run.frame = requestAnimationFrame(previewFrame);
    } else {
      // Made it! Pause a moment at the finish, then show the whole route again.
      run.timer = setTimeout(() => {
        if (preview === run) stopPreview(true, "Ride preview finished.");
      }, 700);
    }
  }

  // Fill in the card: progress, height and steepness at `t` (0..1) of the way.
  function updateCard(run, t) {
    progressFill.style.transform = "scaleX(" + t + ")";
    milesText.textContent = (t * run.miles).toFixed(1) + " / " + run.miles.toFixed(1) + " mi";

    if (run.profile) {
      const profileMiles = t * run.profileMiles;
      const grade = describeGrade(gradeAt(run.profile, profileMiles));
      elevationText.textContent = "⛰️ " + Math.round(elevationAt(run.profile, profileMiles)) + " ft";
      gradeText.textContent = grade.arrow + " " + grade.percent + "% " + grade.level;
      gradeText.className = "wf-card-grade " + grade.className;
    } else {
      elevationText.textContent = "⛰️ no elevation data";
      gradeText.textContent = "";
    }
    moveChartCursor(t);
  }

  // Stop the preview. `zoomBack` = true to show the whole route again afterwards.
  function stopPreview(zoomBack = true, message = "Ride preview stopped.") {
    const run = preview;
    if (!run) return;
    preview = null;
    cancelAnimationFrame(run.frame);
    clearTimeout(run.timer);

    riderMarker.remove();
    fadeBuildings(false);
    card.hidden = true;
    document.body.classList.remove("wf-previewing");
    moveChartCursor(null);
    updateButtons();
    announce(message);

    if (zoomBack) showWholeRoute(run.route);
  }

  // Ease back out so the whole route fits on screen, still tilted so the hills show.
  // (Same padding as zoomToRoute in app.js: leaves room for the panel.)
  function showWholeRoute(route) {
    const bounds = new maplibregl.LngLatBounds();
    for (const point of route.geometry) bounds.extend(point);
    const isPhone = window.innerWidth <= 600;
    const padding = isPhone
      ? { top: 40, bottom: window.innerHeight * 0.55 + 20, left: 30, right: 30 }
      : { top: 60, bottom: 60, left: 420, right: 60 };
    const camera = map.cameraForBounds(bounds, { padding: padding });
    if (!camera) return; // the window is too small to fit it: leave the camera where it is
    map.easeTo({
      center: camera.center,
      zoom: camera.zoom - (shows3D ? 0.3 : 0), // a tilted view needs a little more room
      bearing: 0,
      pitch: shows3D ? TILT_3D - 15 : 0,
      duration: 1800,
    });
  }

  previewButton.addEventListener("click", () => {
    if (preview) stopPreview(true);
    else startPreview();
  });

  // Escape stops the preview.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && preview) stopPreview(true);
  });

  // Switching tabs stops it too (no point flying when nobody's watching).
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPreview(true);
  });

  // If the rider grabs the map (drag, scroll, pinch), hand the camera back to them.
  // Our own camera moves have no "originalEvent"; a person's mouse or finger does.
  map.on("movestart", (event) => {
    if (preview && event.originalEvent) stopPreview(false);
  });


  // ============ 6. ELEVATION CHART <-> MAP ============
  // Point at the elevation chart (mouse or finger) and a dot shows that spot on the map,
  // with a label like "1.2 mi · 245 ft · 6% grade".

  const hoverDotElement = makeEl("div", "wf-hover-dot");
  hoverDotElement.setAttribute("aria-hidden", "true");
  const hoverDot = new maplibregl.Marker({ element: hoverDotElement });

  const tooltip = makeEl("div", "wf-chart-tip");
  tooltip.setAttribute("aria-hidden", "true");
  tooltip.hidden = true;
  document.body.appendChild(tooltip);

  // The chart SVG that app.js drew for the selected route (or null).
  function findChart(target) {
    return target && target.closest ? target.closest(".elevation-chart svg") : null;
  }

  // A thin vertical line on the chart, marking "you are here".
  // `t` is 0..1 across the chart, or null to hide it.
  function moveChartCursor(t) {
    const svg = document.querySelector(".elevation-chart svg");
    if (!svg) return;
    let line = svg.querySelector(".wf-chart-cursor");
    if (t === null) {
      if (line) line.remove();
      return;
    }
    if (!line) {
      line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", "wf-chart-cursor");
      line.setAttribute("y1", "0");
      line.setAttribute("y2", "70"); // app.js draws the chart 70 units tall
      svg.appendChild(line);
    }
    const x = (t * 300).toFixed(1); // ...and 300 units wide
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
  }

  function showChartSpot(svg, event) {
    const route = selectedRoute();
    const profile = profileOf(route);
    if (!route || !profile) return;

    // How far across the chart the pointer is (0 = left edge, 1 = right edge).
    // app.js draws x = miles / maxMiles * width, so this fraction is also the
    // fraction of the ride's distance.
    const box = svg.getBoundingClientRect();
    if (box.width === 0) return;
    const t = clamp((event.clientX - box.left) / box.width, 0, 1);
    const miles = t * profileLength(profile);
    const feet = Math.round(elevationAt(profile, miles));
    const grade = describeGrade(gradeAt(profile, miles));

    // The dot on the map, the same fraction of the way along the route.
    const path = measureRoute(route);
    hoverDot.setLngLat(pointAlong(path, t * path.total)).addTo(map);
    moveChartCursor(t);

    // The label, just above the chart (so a finger doesn't cover it).
    tooltip.textContent = miles.toFixed(1) + " mi · " + feet + " ft · ";
    tooltip.appendChild(makeEl("span", grade.className, grade.percent + "% grade"));
    tooltip.hidden = false;
    const halfWidth = tooltip.offsetWidth / 2;
    tooltip.style.left = clamp(event.clientX, halfWidth + 4, window.innerWidth - halfWidth - 4) + "px";
    tooltip.style.top = box.top + "px";
  }

  function hideChartSpot() {
    if (tooltip.hidden) return;
    tooltip.hidden = true;
    hoverDot.remove();
    if (!preview) moveChartCursor(null);
  }

  // One set of listeners on the whole page, because app.js redraws the chart
  // every time a route is picked (so listeners on the chart itself would get lost).
  function onPointer(event) {
    if (preview) return; // the preview is using the chart's line right now
    const svg = findChart(event.target);
    if (svg) showChartSpot(svg, event);
    else hideChartSpot();
  }
  document.addEventListener("pointermove", onPointer);
  document.addEventListener("pointerdown", onPointer);

  // Pointer left the chart (or the finger lifted / the panel started scrolling).
  document.addEventListener("pointerout", (event) => {
    const svg = findChart(event.target);
    if (svg && !svg.contains(event.relatedTarget)) hideChartSpot();
  });
  document.addEventListener("pointercancel", hideChartSpot);


  // ============ 7. HOOKING INTO app.js ============

  // app.js calls this every time a route is picked or redrawn.
  const earlierOnRouteSelected = window.onRouteSelected;
  window.onRouteSelected = function (route) {
    if (earlierOnRouteSelected) earlierOnRouteSelected(route);
    stopPreview(false); // app.js is already zooming to the new route
    hideChartSpot();
    updateButtons();
  };

  // Handy for testing from the browser console, e.g. wayfinder3D.startPreview().
  window.wayfinder3D = { set3D, startPreview, stopPreview };

  updateButtons();
})();
