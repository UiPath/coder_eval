#!/usr/bin/env python3
"""Run the DiceRoller flow multiple times and verify it produces valid dice rolls (1-6).

Finds the project directory (containing project.uiproj) automatically and passes
it to `uip flow debug`. Searches the debug output robustly for any integer 1-6.
"""

import glob
import json
import os
import subprocess
import sys

NUM_RUNS = 5


def find_project_dir():
    """Find the project directory containing project.uiproj."""
    candidates = glob.glob("**/project.uiproj", recursive=True)
    if not candidates:
        return None
    # Prefer a path containing "DiceRoller" (case-insensitive)
    for c in candidates:
        if "diceroller" in c.lower():
            return os.path.dirname(c)
    return os.path.dirname(candidates[0])


def find_dice_value(obj):
    """Recursively search JSON output for a dice value (integer 1-6).

    Looks for values that could be dice results. The debug output nests the
    script result in various places:
      - variables.elements[].outputs.response.{key}: <int>
      - variables.globals["rollDice.output"].{key}: <int>
      - or any nested integer 1-6
    """
    if isinstance(obj, int) and 1 <= obj <= 6:
        return obj
    if isinstance(obj, float) and obj == int(obj) and 1 <= int(obj) <= 6:
        return int(obj)
    if isinstance(obj, str):
        # Handle stringified numbers
        try:
            v = int(obj)
            if 1 <= v <= 6:
                return v
        except ValueError:
            pass
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


def parse_json_output(stdout):
    """Parse JSON from CLI output, handling potential non-JSON prefix lines."""
    # Try parsing the whole output first
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    # Try each line as a potential JSON start
    lines = stdout.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue

    return None


def main():
    project_dir = find_project_dir()
    if not project_dir:
        print("FAIL: No project directory found (no project.uiproj)", file=sys.stderr)
        sys.exit(1)

    print(f"Using project directory: {project_dir}")

    values = []
    for i in range(NUM_RUNS):
        r = subprocess.run(
            ["uip", "flow", "debug", project_dir, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            print(
                f"FAIL: Run {i + 1}/{NUM_RUNS} exited with code {r.returncode}",
                file=sys.stderr,
            )
            print(f"stderr: {r.stderr[:500]}", file=sys.stderr)
            print(f"stdout: {r.stdout[:500]}", file=sys.stderr)
            sys.exit(1)

        data = parse_json_output(r.stdout)
        if data is None:
            print(
                f"FAIL: Run {i + 1}/{NUM_RUNS} output is not valid JSON",
                file=sys.stderr,
            )
            print(f"stdout: {r.stdout[:500]}", file=sys.stderr)
            sys.exit(1)

        # Check the flow completed successfully
        final_status = None
        if isinstance(data, dict):
            final_status = (data.get("Data") or {}).get("finalStatus")
        if final_status != "Completed":
            print(
                f"FAIL: Run {i + 1}/{NUM_RUNS} did not complete (status: {final_status})",
                file=sys.stderr,
            )
            print(
                f"output: {json.dumps(data, indent=2)[:500]}", file=sys.stderr
            )
            sys.exit(1)

        dice = find_dice_value(data)
        if dice is None:
            print(
                f"FAIL: Run {i + 1}/{NUM_RUNS} output has no dice value (1-6)",
                file=sys.stderr,
            )
            print(
                f"output: {json.dumps(data, indent=2)[:1000]}", file=sys.stderr
            )
            sys.exit(1)

        values.append(dice)

    if len(set(values)) == 1:
        print(
            f"WARNING: All {NUM_RUNS} runs returned the same value ({values[0]}), may not be random"
        )

    print(f"OK: {NUM_RUNS} runs produced valid dice rolls: {values}")


if __name__ == "__main__":
    main()
