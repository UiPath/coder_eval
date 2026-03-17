#!/usr/bin/env python3
"""Run the DiceRoller flow multiple times and verify it produces valid dice rolls (1-6)."""

import json
import subprocess
import sys

FLOW_PATH = "DiceRoller/flow_files/DiceRoller.flow"
NUM_RUNS = 5


def find_dice_value(obj):
    """Recursively search JSON output for an integer value 1-6.

    The flow debug output wraps the script result in a nested JSON structure.
    The Script node returns something like {diceResult: <int>}, which appears
    somewhere inside the debug output's Data field.
    """
    if isinstance(obj, int) and 1 <= obj <= 6:
        return obj
    if isinstance(obj, float) and obj == int(obj) and 1 <= int(obj) <= 6:
        return int(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            result = find_dice_value(v)
            if result is not None:
                return result
    if isinstance(obj, list):
        for v in obj:
            result = find_dice_value(v)
            if result is not None:
                return result
    return None


values = []
for i in range(NUM_RUNS):
    r = subprocess.run(
        ["./node_modules/.bin/uipcli", "flow", "debug", FLOW_PATH],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        print(f"FAIL: Run {i + 1}/{NUM_RUNS} exited with code {r.returncode}", file=sys.stderr)
        print(f"stderr: {r.stderr[:500]}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"FAIL: Run {i + 1}/{NUM_RUNS} output is not valid JSON", file=sys.stderr)
        print(f"stdout: {r.stdout[:500]}", file=sys.stderr)
        sys.exit(1)

    dice = find_dice_value(data)
    if dice is None:
        print(f"FAIL: Run {i + 1}/{NUM_RUNS} output has no dice value (1-6)", file=sys.stderr)
        print(f"output: {json.dumps(data, indent=2)[:500]}", file=sys.stderr)
        sys.exit(1)

    values.append(dice)

if len(set(values)) == 1:
    print(f"WARNING: All {NUM_RUNS} runs returned the same value ({values[0]}), may not be random")

print(f"OK: {NUM_RUNS} runs produced valid dice rolls: {values}")
