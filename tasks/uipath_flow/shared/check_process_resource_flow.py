#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


CONFIGS: dict[str, dict[str, Any]] = {
    "api": {
        "node_id": "nameToAge",
        "node_type": "uipath.core.api-workflow.346b8959-c126-48d3-9c46-942abcf944d7",
        "label": "Name To Age",
        "resource_key": "Shared/test-darwin-apis.API Workflow",
        "resource_subtype": "Api",
        "orchestrator_type": "api",
        "service_type": "Orchestrator.ExecuteApiWorkflowAsync",
        "name_binding": "bApiName",
        "folder_binding": "bApiFolder",
        "name_default": "API Workflow",
        "folder_default": "Shared/test-darwin-apis",
        "input_key": "name",
        "input_value": "tomasz",
        "fixture_terms": ["fixture", "response", "age", "42"],
        "routing_terms": ["age", "18", "adultRoute", "minorRoute"],
    },
    "rpa": {
        "node_id": "runRobot",
        "node_type": "uipath.core.rpa-workflow.7648307a-b180-467c-81f3-06f49a87313b",
        "label": "RPA Workflow12Feb",
        "resource_key": "Shared/sol_with_all_projects.RPA Workflow12Feb",
        "resource_subtype": "Process",
        "orchestrator_type": "process",
        "service_type": "Orchestrator.StartJob",
        "name_binding": "bRpaName",
        "folder_binding": "bRpaFolder",
        "name_default": "RPA Workflow12Feb",
        "folder_default": "Shared/sol_with_all_projects",
        "input_key": "newArgument",
        "input_value": "",
        "fixture_terms": ["fixture", "output", "jobId", "dry-run-job"],
        "routing_terms": ["jobId", "robotStarted", "robotEscalated"],
    },
}


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


def check_bindings(bindings: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    by_id = {entry.get("id"): entry for entry in bindings}
    for binding_id, attr, default in [
        (cfg["name_binding"], "name", cfg["name_default"]),
        (cfg["folder_binding"], "folderPath", cfg["folder_default"]),
    ]:
        entry = by_id.get(binding_id)
        require(entry is not None, f"missing binding {binding_id}")
        require(entry.get("resource") == "process", f"{binding_id}: expected resource process, got {entry}")
        require(
            entry.get("resourceSubType") == cfg["resource_subtype"],
            f"{binding_id}: expected resourceSubType {cfg['resource_subtype']}, got {entry}",
        )
        require(
            entry.get("resourceKey") == cfg["resource_key"],
            f"{binding_id}: expected resourceKey {cfg['resource_key']}, got {entry}",
        )
        require(entry.get("propertyAttribute") == attr, f"{binding_id}: expected propertyAttribute {attr}, got {entry}")
        require(entry.get("default") == default, f"{binding_id}: expected default {default!r}, got {entry}")


def check_v1_flow(path: Path, cfg: dict[str, Any]) -> None:
    flow = load_json(path)
    nodes = flow.get("nodes")
    edges = flow.get("edges")
    definitions = flow.get("definitions")
    bindings = flow.get("bindings")
    require(isinstance(nodes, list), "flow has no nodes array")
    require(isinstance(edges, list), "flow has no edges array")
    require(isinstance(definitions, list), "flow has no definitions array")
    require(isinstance(bindings, list), "flow has no bindings array")

    resource_nodes = [node for node in nodes if node.get("type") == cfg["node_type"]]
    require(resource_nodes, f"no {cfg['node_type']} node found")
    node = resource_nodes[0]
    require(node.get("id") == cfg["node_id"], f"expected node id {cfg['node_id']}, got {node.get('id')}")

    inputs = node.get("inputs") or {}
    require(inputs.get(cfg["input_key"]) == cfg["input_value"], f"wrong process node inputs: {inputs}")

    model = node.get("model") or {}
    require(model.get("serviceType") == cfg["service_type"], f"wrong serviceType: {model}")
    require(model.get("section") == "Published", f"wrong section: {model}")
    model_bindings = model.get("bindings") or {}
    require(model_bindings.get("resource") == "process", f"wrong model.bindings.resource: {model_bindings}")
    require(
        model_bindings.get("resourceSubType") == cfg["resource_subtype"],
        f"wrong model.bindings.resourceSubType: {model_bindings}",
    )
    require(
        model_bindings.get("resourceKey") == cfg["resource_key"],
        f"wrong model.bindings.resourceKey: {model_bindings}",
    )
    require(
        model_bindings.get("orchestratorType") == cfg["orchestrator_type"],
        f"wrong model.bindings.orchestratorType: {model_bindings}",
    )
    values = model_bindings.get("values") or {}
    require(values.get("name") == cfg["name_default"], f"wrong model binding name value: {values}")
    require(values.get("folderPath") == cfg["folder_default"], f"wrong model binding folderPath value: {values}")

    context = {entry.get("name"): entry for entry in model.get("context", [])}
    if context:
        require(
            context.get("name", {}).get("value") == f"=bindings.{cfg['name_binding']}",
            f"name context does not reference {cfg['name_binding']}: {context}",
        )
        require(
            context.get("folderPath", {}).get("value") == f"=bindings.{cfg['folder_binding']}",
            f"folderPath context does not reference {cfg['folder_binding']}: {context}",
        )

    outputs = node.get("outputs") or {}
    error_output = outputs.get("error") or {}
    require(error_output.get("source") in {"=Error", "=result.Error"}, f"missing standard error output: {outputs}")

    check_bindings(bindings, cfg)

    used_types = {node.get("type") for node in nodes}
    defined_types = {definition.get("nodeType") for definition in definitions}
    missing = used_types - defined_types
    require(not missing, f"missing definitions for {sorted(missing)}")
    resource_defs = [definition for definition in definitions if definition.get("nodeType") == cfg["node_type"]]
    require(resource_defs, f"missing definition for {cfg['node_type']}")
    require(
        resource_defs[0].get("model", {}).get("serviceType") == cfg["service_type"],
        f"resource definition has wrong serviceType: {resource_defs[0]}",
    )

    node_types = [node.get("type") for node in nodes]
    require("core.trigger.manual" in node_types, "missing manual trigger")
    require("core.logic.decision" in node_types, "missing decision node")
    require(node_types.count("core.logic.mock") >= 2, "expected at least two mock route nodes")

    blob = json.dumps(flow)
    for term in cfg["routing_terms"]:
        require(term in blob, f"routing evidence {term!r} not found in flow")

    print(f"v1 {cfg['node_id']} process-resource flow ok: {len(nodes)} nodes, {len(edges)} edges")


def check_v2_project(fil_path: Path, bindings_path: Path, cfg: dict[str, Any]) -> None:
    source = fil_path.read_text()
    bindings_doc = load_json(bindings_path)
    bindings = bindings_doc.get("bindings")
    require(isinstance(bindings, list), "bindings.json has no bindings array")

    require(".manifest.flow" not in source, "FIL source should not reference a manifest sidecar")
    require(re.search(r"\bflow\s+[a-z][a-z0-9-]*\s*\{", source), "missing top-level flow declaration")
    require(re.search(r"\btrigger\s+\w+\s*:\s*start\s*;", source), "missing manual trigger declaration")

    action_pattern = rf"\baction\s+{re.escape(cfg['node_id'])}\s*:\s*{re.escape(cfg['node_type'])}@1\.0\.0\s*\{{"
    require(re.search(action_pattern, source), f"missing {cfg['node_id']} action declaration")
    require(
        re.search(rf"executeNode\(\s*{re.escape(cfg['node_id'])}\s*,", source),
        f"executeNode must call {cfg['node_id']} by action identifier",
    )
    require(
        not re.search(rf"executeNode\(\s*['\"]{re.escape(cfg['node_id'])}['\"]", source),
        f"executeNode should not call {cfg['node_id']} by string literal",
    )

    required_terms = [
        cfg["label"],
        cfg["resource_key"],
        f'resourceSubType: "{cfg["resource_subtype"]}"',
        f'orchestratorType: "{cfg["orchestrator_type"]}"',
        f'serviceType: "{cfg["service_type"]}"',
        cfg["name_binding"],
        cfg["folder_binding"],
        cfg["input_key"],
        cfg["input_value"],
    ]
    for term in required_terms + cfg["fixture_terms"] + cfg["routing_terms"]:
        require(term in source, f"required FIL term {term!r} not found")

    require("if" in source and "else" in source, "expected explicit branch over process-resource output")

    check_bindings(bindings, cfg)
    print(f"v2 {cfg['node_id']} FIL project shape ok")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=sorted(CONFIGS), required=True)
    parser.add_argument("--v1-flow", type=Path)
    parser.add_argument("--fil", type=Path)
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args()

    cfg = CONFIGS[args.kind]
    if args.v1_flow:
        check_v1_flow(args.v1_flow, cfg)
    elif args.fil and args.bindings:
        check_v2_project(args.fil, args.bindings, cfg)
    else:
        fail("provide either --v1-flow or both --fil and --bindings")


if __name__ == "__main__":
    main()
