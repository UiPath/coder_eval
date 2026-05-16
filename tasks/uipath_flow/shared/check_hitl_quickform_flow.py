#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


NODE_ID = "reviewExpense"
NODE_TYPE = "uipath.human-in-the-loop"
EXPECTED_VERSION = "1.0"
REQUIRED_OUTCOMES = {"Approve", "Reject"}
REQUIRED_ROUTE_IDS = {"approvedRoute", "rejectedRoute"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        fail(f"could not load JSON {path}: {exc}")


def uip_command() -> str:
    configured = os.environ.get("UIPATH_FLOW_CLI")
    if configured:
        return configured
    bun_uip = Path.home() / ".bun" / "bin" / "uip"
    if bun_uip.exists():
        return str(bun_uip)
    return "uip"


def validate_flow(path: Path) -> None:
    try:
        result = subprocess.run(
            [uip_command(), "maestro", "flow", "validate", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        fail("uip CLI is not available on PATH")
    except subprocess.TimeoutExpired:
        fail(f"uip maestro flow validate timed out for {path}")

    if result.returncode != 0:
        details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        fail(f"flow validation failed for {path}: {details}")


def check_quickform_schema(schema: dict[str, Any]) -> None:
    fields = schema.get("fields")
    outcomes = schema.get("outcomes")
    require(isinstance(fields, list), "HITL schema has no fields array")
    require(isinstance(outcomes, list), "HITL schema has no outcomes array")

    fields_by_id = {field.get("id"): field for field in fields if isinstance(field, dict)}
    approved = fields_by_id.get("approved")
    comments = fields_by_id.get("comments")

    require(isinstance(approved, dict), "schema is missing approved field")
    require(approved.get("type") == "boolean", f"approved field must be boolean, got {approved}")
    require(approved.get("direction") == "output", f"approved field must be an output, got {approved}")

    require(isinstance(comments, dict), "schema is missing comments field")
    require(comments.get("type") == "text", f"comments field must be text, got {comments}")
    require(comments.get("direction") == "output", f"comments field must be an output, got {comments}")

    outcome_names = {outcome.get("name") for outcome in outcomes if isinstance(outcome, dict)}
    require(outcome_names >= REQUIRED_OUTCOMES, f"expected outcomes {sorted(REQUIRED_OUTCOMES)}, got {outcome_names}")


def check_v1_flow(path: Path) -> None:
    flow = load_json(path)
    nodes = flow.get("nodes")
    edges = flow.get("edges")
    definitions = flow.get("definitions")
    require(isinstance(nodes, list), "flow has no nodes array")
    require(isinstance(edges, list), "flow has no edges array")
    require(isinstance(definitions, list), "flow has no definitions array")

    hitl_nodes = [node for node in nodes if node.get("type") == NODE_TYPE]
    require(len(hitl_nodes) == 1, f"expected exactly one {NODE_TYPE} node, got {len(hitl_nodes)}")
    node = hitl_nodes[0]

    require(node.get("id") == NODE_ID, f"expected HITL node id {NODE_ID}, got {node.get('id')}")
    require(node.get("typeVersion") == EXPECTED_VERSION, f"expected typeVersion {EXPECTED_VERSION}, got {node}")

    inputs = node.get("inputs") or {}
    require(inputs.get("type") == "quick", f"expected quickform inputs.type, got {inputs}")
    schema = inputs.get("schema") or {}
    require(isinstance(schema, dict), f"HITL inputs.schema must be an object, got {schema}")
    check_quickform_schema(schema)

    outputs = node.get("outputs") or {}
    output = outputs.get("output") or {}
    status = outputs.get("status") or {}
    require(isinstance(output, dict), f"missing output block: {outputs}")
    require(isinstance(status, dict), f"missing status block: {outputs}")
    require(output.get("source") == "=result", f"output.source must be =result, got {output}")
    require(status.get("source") == "=result.Action", f"status.source must be =result.Action, got {status}")

    require(
        any(edge.get("sourceNodeId") == NODE_ID and edge.get("sourcePort") == "completed" for edge in edges),
        f"expected an edge leaving {NODE_ID} from sourcePort completed",
    )

    hitl_defs = [
        definition
        for definition in definitions
        if definition.get("nodeType") == NODE_TYPE and definition.get("version") == EXPECTED_VERSION
    ]
    require(hitl_defs, f"missing {NODE_TYPE}@{EXPECTED_VERSION} definition")
    require(
        hitl_defs[0].get("model", {}).get("serviceType") == "Actions.HITL",
        f"HITL definition has wrong serviceType: {hitl_defs[0]}",
    )

    node_types = [node.get("type") for node in nodes]
    require("core.trigger.manual" in node_types, "missing manual trigger")
    require("core.logic.decision" in node_types, "missing decision node")
    require(node_types.count("core.logic.mock") >= 2, "expected at least two mock route nodes")

    blob = json.dumps(flow)
    for term in REQUIRED_ROUTE_IDS | {"approved", "comments", "Approve", "Reject"}:
        require(term in blob, f"routing or quickform evidence {term!r} not found in flow")

    validate_flow(path)
    print(f"v1 HITL quickform flow ok: {len(nodes)} nodes, {len(edges)} edges")


def check_v2_project(fil_path: Path, converted_flow_path: Path) -> None:
    source = fil_path.read_text()

    require(".manifest.flow" not in source, "FIL source should not reference a manifest sidecar")
    require(re.search(r"\bflow\s+[a-z][a-z0-9-]*\s*\{", source), "missing top-level flow declaration")
    require(re.search(r"\btrigger\s+\w+\s*:\s*start\s*;", source), "missing manual trigger declaration")

    action_pattern = rf"\baction\s+{NODE_ID}\s*:\s*{re.escape(NODE_TYPE)}@1\.0(?![\d.])\s*\{{"
    require(re.search(action_pattern, source), f"missing {NODE_ID}: {NODE_TYPE}@1.0 action declaration")
    require(
        re.search(rf"executeNode\(\s*{NODE_ID}\s*,", source),
        f"executeNode must call {NODE_ID} by action identifier",
    )
    require(
        not re.search(rf"executeNode\(\s*['\"]{NODE_ID}['\"]", source),
        f"executeNode should not call {NODE_ID} by string literal",
    )

    for term in [
        "rawInputs",
        "schema",
        "recipient",
        "outputs",
        "fixture",
        "approved",
        "comments",
        "Approve",
        "Reject",
        "ActionCenter",
        "approvedRoute",
        "rejectedRoute",
    ]:
        require(term in source, f"required FIL term {term!r} not found")

    require("if" in source and "else" in source, "expected explicit branch over the HITL approval result")

    check_v1_flow(converted_flow_path)
    print("v2 HITL quickform FIL project shape ok")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v1-flow", type=Path)
    parser.add_argument("--fil", type=Path)
    parser.add_argument("--converted-flow", type=Path)
    args = parser.parse_args()

    if args.v1_flow and not args.fil and not args.converted_flow:
        check_v1_flow(args.v1_flow)
    elif args.fil and args.converted_flow and not args.v1_flow:
        check_v2_project(args.fil, args.converted_flow)
    else:
        fail("provide either --v1-flow or both --fil and --converted-flow")


if __name__ == "__main__":
    main()
