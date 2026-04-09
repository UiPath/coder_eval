#!/usr/bin/env python3
"""Run the SlackChannelDescription flow via debug and verify the output contains the Bellevue office address."""

import glob
import json
import os
import shutil
import subprocess
import sys

# Address fragments to verify — the channel description should contain these
ADDRESS_FRAGMENTS = [
    "700 Bellevue Way NE",
    "Suite 2000",
    "Bellevue",
    "WA 98004",
]


def find_project_dir():
    """Find the project directory containing project.uiproj."""
    candidates = glob.glob("**/project.uiproj", recursive=True)
    if not candidates:
        return None
    # Prefer a path containing "SlackChannelDescription" (case-insensitive)
    for c in candidates:
        if "slackchanneldescription" in c.lower():
            return os.path.dirname(c)
    return os.path.dirname(candidates[0])


def find_cli():
    """Find the uip binary — try PATH first, then node_modules."""
    if shutil.which("uip"):
        return "uip"
    local = os.path.join("node_modules", ".bin", "uip")
    if os.path.isfile(local):
        return local
    return "uip"


def parse_json_output(stdout):
    """Parse JSON from CLI output, handling non-JSON prefix lines."""
    stdout = stdout.strip()
    # Try full output first
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    # Try from each line starting with '{'
    lines = stdout.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
    return None


def find_any_fragment(obj, fragments):
    """Return which fragments were found anywhere in the JSON output."""
    found = set()
    text = json.dumps(obj).lower()
    for f in fragments:
        if f.lower() in text:
            found.add(f)
    return found


def main():
    project_dir = find_project_dir()
    if project_dir is None:
        print("FAIL: No project directory found (no project.uiproj)", file=sys.stderr)
        sys.exit(1)

    cli = find_cli()
    print(f"Using project directory: {project_dir}")
    print(f"Using CLI: {cli}")

    # Run flow debug — this calls real Slack APIs
    r = subprocess.run(
        [cli, "flow", "debug", project_dir, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=90,
    )

    if r.returncode != 0:
        print(f"FAIL: flow debug exited with code {r.returncode}", file=sys.stderr)
        print(f"stderr: {r.stderr[:1000]}", file=sys.stderr)
        print(f"stdout: {r.stdout[:1000]}", file=sys.stderr)
        sys.exit(1)

    data = parse_json_output(r.stdout)
    if data is None:
        print("FAIL: Could not parse flow debug output as JSON", file=sys.stderr)
        print(f"stdout: {r.stdout[:1000]}", file=sys.stderr)
        sys.exit(1)

    print("Debug output parsed successfully")

    # Check for address fragments in the output
    found = find_any_fragment(data, ADDRESS_FRAGMENTS)
    missing = [f for f in ADDRESS_FRAGMENTS if f not in found]

    if missing:
        print(f"FAIL: Output missing address fragments: {missing}", file=sys.stderr)
        print(f"Found fragments: {list(found)}", file=sys.stderr)
        print(
            f"Output (truncated): {json.dumps(data, indent=2)[:2000]}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("OK: Channel description contains Bellevue office address")
    print(f"  Verified fragments: {ADDRESS_FRAGMENTS}")


if __name__ == "__main__":
    main()
