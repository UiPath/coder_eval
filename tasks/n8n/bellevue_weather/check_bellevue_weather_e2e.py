#!/usr/bin/env python3
"""E2E check for the n8n weather-decision task.

Independently fetches open-meteo to compute the expected verdict, deploys
the agent's workflow, hits its webhook with the same coordinates, and
asserts the verdict matches.

Usage:
    python3 check_weather_e2e.py <workflow.json>
"""

import json
import sys
import urllib.request
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from _shared import ActivateError, DeployError, deployed_workflow, fail, post_webhook_until_ready, response_contains


LAT, LON = 47.6101, -122.2015  # Bellevue, WA


def fetch_temp_f() -> float:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current=temperature_2m"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data["current"]["temperature_2m"] * 9 / 5 + 32


def main() -> None:
    with open(sys.argv[1]) as f:
        workflow = json.load(f)

    temp_f = fetch_temp_f()
    expected = "warm" if temp_f > 60 else "cool"

    try:
        with deployed_workflow(workflow) as webhook_path:
            status, body = post_webhook_until_ready(webhook_path, {"latitude": LAT, "longitude": LON})
            if status != 200:
                fail(0.6, f"ERROR: webhook never returned 200 ({status}): {body}")

            print(f"Webhook responded: {body} (ground truth: {temp_f:.1f}°F → {expected})", file=sys.stderr)
            if response_contains(body, expected):
                print(f"Correct: {expected} (found in response)", file=sys.stderr)
                print(1.0)
                return
            print(f"WRONG: expected {expected!r}, not found anywhere in body={body}", file=sys.stderr)
            print(0.8)
    except (DeployError, ActivateError) as e:
        fail(e.score, f"ERROR: {e}")


if __name__ == "__main__":
    main()
