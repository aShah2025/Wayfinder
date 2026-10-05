"""Tidy valhalla.json for Wayfinder: use the tiles folder (no .tar) and log to a file."""
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
config = json.load(open(path))
config["mjolnir"]["tile_extract"] = ""
config["mjolnir"]["traffic_extract"] = ""
# Valhalla's messages go to a log file instead of cluttering the server's terminal.
config["logging"] = {"type": "file", "color": False,
                     "file_name": str(path.parent / "valhalla.log")}
json.dump(config, open(path, "w"), indent=2)
print("valhalla.json tidied")
