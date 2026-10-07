#!/usr/bin/env bash
# Builds Wayfinder's OWN copy of the Valhalla routing engine for California.
#
# Why: the free public Valhalla server can rate-limit or go down. With local data,
# routing runs on this computer: faster, no limits, works during a live demo.
#
# Needs: the project's .venv with `pip install pyvalhalla`, ~6 GB of disk space,
# and 30-90 minutes (mostly downloading and building tiles).
#
# Run from the Wayfinder folder:   bash scripts/build_valhalla.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
VALHALLA_PKG=$($PY -c "import valhalla, os; print(os.path.dirname(valhalla.__file__))")
DATA="$(pwd)/valhalla_data"
mkdir -p "$DATA"
cd "$DATA"

# 1. California's map data from OpenStreetMap (via Geofabrik), about 1.3 GB.
if [ ! -f california.osm.pbf ]; then
  echo "Downloading California map data..."
  curl -L -o california.osm.pbf.part \
    https://download.geofabrik.de/north-america/us/california-latest.osm.pbf
  mv california.osm.pbf.part california.osm.pbf
fi

# 2. A config file telling Valhalla where everything lives, with roomier limits
#    than the public server (e.g. longer bike trips, more "avoid" points).
$PY "$VALHALLA_PKG/valhalla_build_config.py" \
  --mjolnir-tile-dir "$DATA/tiles" --mjolnir-tile-extract "$DATA/tiles.tar" \
  --mjolnir-admin "$DATA/admins.sqlite" --mjolnir-timezone "$DATA/timezones.sqlite" \
  --additional-data-elevation "$DATA/elevation" --mjolnir-concurrency 12 \
  --service-limits-bicycle-max-distance 250000 --service-limits-trace-max-distance 300000 \
  --service-limits-trace-max-shape 50000 --service-limits-max-exclude-locations 100 \
  > valhalla.json
$PY ../scripts/quiet_valhalla_config.py valhalla.json   # no .tar file, no log spam

# 3. Elevation data for California (so hills and grades can be measured).
echo "Downloading elevation tiles..."
$PY "$VALHALLA_PKG/valhalla_build_elevation.py" -b "-124.5,32.5,-114.1,42.1" -o elevation -p 8

# 4. Build the routing graph ("tiles") from the map + elevation data.
echo "Building routing tiles (this is the slow part)..."
# (Run the bundled program directly: `python -m valhalla ...` only finds it if it's on PATH.)
"$VALHALLA_PKG/bin/valhalla_build_tiles" -c valhalla.json california.osm.pbf

echo "Done! Restart the Wayfinder server and it will use the local engine."
