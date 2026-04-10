#!/usr/bin/env python3
"""Verify that the script node was removed and flow rewired to End.

Checks:
1. Flow is valid JSON
2. Script node (core.action.script) is gone
3. Trigger node (core.trigger.manual) still exists
4. End node (core.control.end) exists
5. Edge from trigger to End exists
6. No edges reference the removed script node
7. End definition present
8. Script definition removed (or still present is acceptable)
9. variables.nodes regenerated (no script entries)

Prints a float score (0.0-1.0) to stdout.
"""

import json
import sys


RESULT_PATH = "result.flow"


def check() -> float:
    try:
        with open(RESULT_PATH) as f:
            flow = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 0.0

    score = 0.0
    nodes = flow.get("nodes", [])
    edges = flow.get("edges", [])
    definitions = flow.get("definitions", [])
    variables = flow.get("variables", {})

    node_types = {n.get("type") for n in nodes}
    node_ids = {n.get("id") for n in nodes}

    # 1. Script node removed (0.25)
    if "core.action.script" not in node_types:
        score += 0.25
        print("OK: Script node removed", file=sys.stderr)
    else:
        print("FAIL: Script node still present", file=sys.stderr)

    # 2. Trigger still exists (0.15)
    if "core.trigger.manual" in node_types:
        score += 0.15
        print("OK: Trigger node preserved", file=sys.stderr)
    else:
        print("FAIL: Trigger node missing", file=sys.stderr)

    # 3. End node exists (0.15)
    if "core.control.end" in node_types:
        score += 0.15
        print("OK: End node present", file=sys.stderr)
    else:
        print("FAIL: No End node", file=sys.stderr)

    # 4. Edge from trigger to End (0.20)
    trigger_nodes = [n for n in nodes if n.get("type") == "core.trigger.manual"]
    end_nodes = [n for n in nodes if n.get("type") == "core.control.end"]
    if trigger_nodes and end_nodes:
        trigger_id = trigger_nodes[0]["id"]
        end_id = end_nodes[0]["id"]
        trigger_to_end = [e for e in edges if e.get("sourceNodeId") == trigger_id and e.get("targetNodeId") == end_id]
        if trigger_to_end:
            score += 0.20
            print("OK: Trigger → End edge found", file=sys.stderr)
        else:
            print("FAIL: No Trigger → End edge", file=sys.stderr)

    # 5. No dangling edges to removed node (0.10)
    all_edge_nodes = set()
    for e in edges:
        all_edge_nodes.add(e.get("sourceNodeId"))
        all_edge_nodes.add(e.get("targetNodeId"))
    dangling = all_edge_nodes - node_ids
    if not dangling:
        score += 0.10
        print("OK: No dangling edges", file=sys.stderr)
    else:
        print(f"FAIL: Dangling edge references: {dangling}", file=sys.stderr)

    # 6. End definition present (0.05)
    end_defs = [d for d in definitions if d.get("nodeType") == "core.control.end"]
    if end_defs:
        score += 0.05
        print("OK: End definition present", file=sys.stderr)
    else:
        print("FAIL: Missing End definition", file=sys.stderr)

    # 7. variables.nodes updated (no script references) (0.10)
    node_vars = variables.get("nodes", [])
    script_vars = [v for v in node_vars if "rollDice" in v.get("id", "")]
    if not script_vars:
        score += 0.10
        print("OK: No script node variables remain", file=sys.stderr)
    else:
        print(f"FAIL: Script node variables still present: {[v['id'] for v in script_vars]}", file=sys.stderr)

    return round(score, 4)


if __name__ == "__main__":
    result = check()
    print(result)
