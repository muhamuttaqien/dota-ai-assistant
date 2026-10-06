#!/usr/bin/env python3

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, request

app = Flask(__name__)

OUTPUT_DIR = Path("gsi_inspector_output")
OUTPUT_DIR.mkdir(exist_ok=True)

LATEST_FILE = OUTPUT_DIR / "latest.json"
FIELDS_FILE = OUTPUT_DIR / "observed_fields.json"

observed_fields = set()


def collect_paths(obj, prefix=""):
    """Recursively collect every JSON field path ever observed."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            observed_fields.add(path)
            collect_paths(value, path)

    elif isinstance(obj, list):
        for value in obj:
            collect_paths(value, f"{prefix}[]")


def save_observed_fields():
    FIELDS_FILE.write_text(
        json.dumps(sorted(observed_fields), indent=2),
        encoding="utf-8",
    )


def section_summary(data):
    print("\n" + "=" * 80)
    print(datetime.now().strftime("%H:%M:%S"))
    print("=" * 80)

    print("\nTOP-LEVEL SECTIONS")

    for key, value in data.items():
        if isinstance(value, dict):
            print(f"  {key:<20} dict ({len(value)} keys)")
        elif isinstance(value, list):
            print(f"  {key:<20} list ({len(value)} entries)")
        else:
            print(f"  {key:<20} {type(value).__name__}")

    print("\nIMPORTANT SECTIONS")

    important = [
        "map",
        "player",
        "hero",
        "abilities",
        "items",
        "draft",
        "minimap",
        "buildings",
        "neutralitems",
        "roshan",
        "events",
    ]

    for key in important:
        value = data.get(key)

        if value is None:
            status = "NOT PRESENT"
        elif value == {} or value == []:
            status = "EMPTY"
        elif isinstance(value, dict):
            status = f"PRESENT ({len(value)} keys)"
        elif isinstance(value, list):
            status = f"PRESENT ({len(value)} entries)"
        else:
            status = "PRESENT"

        print(f"  {key:<20} {status}")


@app.route("/", methods=["POST"])
def receive_gsi():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return "invalid JSON", 400

    # Save complete latest payload
    LATEST_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )

    # Accumulate every field ever seen
    collect_paths(data)
    save_observed_fields()

    # Human-readable terminal output
    section_summary(data)

    return "OK", 200


@app.route("/latest", methods=["GET"])
def latest():

    if not LATEST_FILE.exists():
        return {"status": "No GSI payload received yet"}, 404

    return app.response_class(
        LATEST_FILE.read_text(),
        mimetype="application/json",
    )


if __name__ == "__main__":
    print()
    print("DOTA 2 GSI INSPECTOR")
    print("=====================")
    print()
    print("Listening on:")
    print("  http://127.0.0.1:5051")
    print()
    print("Raw latest payload:")
    print(f"  {LATEST_FILE}")
    print()
    print("All observed fields:")
    print(f"  {FIELDS_FILE}")
    print()

    app.run(
        host="127.0.0.1",
        port=5051,
        debug=False,
    )
