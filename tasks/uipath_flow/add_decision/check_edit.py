#!/usr/bin/env python3
"""Verify that a Decision node was correctly added to the dice-roller flow.

Checks:
1. Flow is valid JSON
2. Decision node (core.logic.decision) exists
3. Two End nodes (core.control.end) exist
4. Decision node has edges from the script node
5. Both End nodes have incoming edges from the decision node
6. Decision definition exists in definitions[]
7. End definition exists in definitions[]
8. variables.nodes is present

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

    # 1. Decision node exists (0.20)
    decision_nodes = [n for n in nodes if n.get("type") == "core.logic.decision"]
    if decision_nodes:
        score += 0.20
        print("OK: Decision node found", file=sys.stderr)
    else:
        print("FAIL: No Decision node (core.logic.decision)", file=sys.stderr)

    # 2. Two End nodes exist (0.15)
    end_nodes = [n for n in nodes if n.get("type") == "core.control.end"]
    if len(end_nodes) >= 2:
        score += 0.15
        print(f"OK: {len(end_nodes)} End nodes found", file=sys.stderr)
    elif len(end_nodes) == 1:
        score += 0.05
        print("PARTIAL: Only 1 End node (expected 2)", file=sys.stderr)
    else:
        print("FAIL: No End nodes", file=sys.stderr)

    # 3. Script → Decision edge exists (0.20)
    script_nodes = [n for n in nodes if n.get("type") == "core.action.script"]
    if decision_nodes and script_nodes:
        script_id = script_nodes[0]["id"]
        decision_id = decision_nodes[0]["id"]
        script_to_decision = [
            e for e in edges if e.get("sourceNodeId") == script_id and e.get("targetNodeId") == decision_id
        ]
        if script_to_decision:
            score += 0.20
            print("OK: Script → Decision edge found", file=sys.stderr)
        else:
            print("FAIL: No edge from Script to Decision", file=sys.stderr)

    # 4. Decision → End edges (true + false branches) (0.20)
    if decision_nodes and end_nodes:
        decision_id = decision_nodes[0]["id"]
        end_ids = {n["id"] for n in end_nodes}
        decision_to_end = [
            e for e in edges if e.get("sourceNodeId") == decision_id and e.get("targetNodeId") in end_ids
        ]
        if len(decision_to_end) >= 2:
            score += 0.20
            print(f"OK: Decision → End edges: {len(decision_to_end)}", file=sys.stderr)
        elif len(decision_to_end) == 1:
            score += 0.10
            print("PARTIAL: Only 1 Decision → End edge (expected 2)", file=sys.stderr)
        else:
            print("FAIL: No Decision → End edges", file=sys.stderr)

    # 5. Decision definition exists (0.10)
    decision_defs = [d for d in definitions if d.get("nodeType") == "core.logic.decision"]
    if decision_defs:
        score += 0.10
        print("OK: Decision definition present", file=sys.stderr)
    else:
        print("FAIL: Missing Decision definition", file=sys.stderr)

    # 6. End definition exists (0.05)
    end_defs = [d for d in definitions if d.get("nodeType") == "core.control.end"]
    if end_defs:
        score += 0.05
        print("OK: End definition present", file=sys.stderr)
    else:
        print("FAIL: Missing End definition", file=sys.stderr)

    # 7. variables.nodes present (0.10)
    node_vars = variables.get("nodes", [])
    if len(node_vars) > 0:
        score += 0.10
        print(f"OK: variables.nodes has {len(node_vars)} entries", file=sys.stderr)
    else:
        print("FAIL: variables.nodes is empty or missing", file=sys.stderr)

    return round(score, 4)


if __name__ == "__main__":
    result = check()
    print(result)
