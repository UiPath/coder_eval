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


NODE_ID = "categorizeRows"
NODE_TYPE = "uipath.pattern.batch-transform"
EXPECTED_VERSION = "1.0"
TRIGGER_ID = "manualStart"
FILE_VAR = "csvFile"
PROMPT = "Classify each invoice by category and write a one-line summary."
OUTPUT_COLUMNS = [
    {"name": "Category", "description": "One of: Utility, Software, Travel, Other"},
    {"name": "Summary", "description": "Plain-English one-line summary of the invoice"},
]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError:
        fail("uip CLI is not available on PATH")
    except subprocess.TimeoutExpired:
        fail(f"uip maestro flow validate timed out for {path}")

    if result.returncode != 0:
        details = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
        fail(f"flow validation failed for {path}: {details}")


def check_output_columns(value: Any) -> None:
    require(isinstance(value, list), "inputs.outputColumns must be an array")
    require(value == OUTPUT_COLUMNS, f"wrong outputColumns: {value}")
    for index, column in enumerate(value):
        require(isinstance(column, dict), f"outputColumns[{index}] must be an object")
        for key in ("name", "description"):
            require(isinstance(column.get(key), str) and column[key].strip(), f"outputColumns[{index}].{key} is empty")


# The three `check_*` helpers below split `check_v1_flow` for readability.
# The sibling `check_summarize_flow.py` keeps the equivalent logic inline; pick
# one style when adding the next pattern-node checker so the files don't drift.
def check_attachment(flow: dict[str, Any], attachment: Any) -> None:
    expected = f"=js:$vars.{TRIGGER_ID}.output.{FILE_VAR}"
    require(attachment == expected, f"wrong attachment input: {attachment!r}")

    nodes = flow.get("nodes") or []
    trigger = next((node for node in nodes if node.get("id") == TRIGGER_ID), None)
    require(trigger is not None and "trigger" in str(trigger.get("type")), f"missing trigger {TRIGGER_ID}")

    globals_ = (flow.get("variables") or {}).get("globals") or []
    file_var = next((var for var in globals_ if var.get("id") == FILE_VAR), None)
    require(file_var is not None, f"missing flow input variable {FILE_VAR}")
    require(file_var.get("direction") == "in", f"{FILE_VAR} must be an input variable: {file_var}")
    require(file_var.get("type") == "file", f"{FILE_VAR} must have type file: {file_var}")
    require(
        file_var.get("triggerNodeId") == TRIGGER_ID, f"{FILE_VAR} must be bound to trigger {TRIGGER_ID}: {file_var}"
    )


def check_result_mapping(flow: dict[str, Any]) -> None:
    globals_ = (flow.get("variables") or {}).get("globals") or []
    result_var = next((var for var in globals_ if var.get("id") == "result"), None)
    require(result_var is not None, "missing flow output variable result")
    require(result_var.get("direction") == "out", f"result must be an output variable: {result_var}")

    end_nodes = [node for node in flow.get("nodes") or [] if node.get("type") == "core.control.end"]
    require(end_nodes, "missing End node")
    expected = f"=js:$vars.{NODE_ID}.output"
    has_mapping = any(((node.get("outputs") or {}).get("result") or {}).get("source") == expected for node in end_nodes)
    require(has_mapping, f"no End node maps result to {expected}")


def check_definition(definitions: list[Any], *, require_full_error_schema: bool = False) -> None:
    # require_full_error_schema=True is for v2 (the v2-to-v1 converter emits the
    # full code/message/detail/category/status schema). v1 hand-authored prompts
    # only require the =Error source wiring, so the schema fields stay optional.
    definition = next(
        (
            definition
            for definition in definitions
            if definition.get("nodeType") == NODE_TYPE and definition.get("version") == EXPECTED_VERSION
        ),
        None,
    )
    require(definition is not None, f"missing {NODE_TYPE}@{EXPECTED_VERSION} definition")
    require(definition.get("category") == "data-operations", f"wrong category: {definition}")
    require(definition.get("display", {}).get("label") == "Batch Transform", f"wrong display label: {definition}")
    require(definition.get("display", {}).get("icon") == "grid-2x2-plus", f"wrong display icon: {definition}")
    require(definition.get("model", {}).get("serviceType") == "ECS.BatchTransform", f"wrong serviceType: {definition}")

    input_props = (definition.get("inputDefinition") or {}).get("properties") or {}
    for key in ("attachment", "prompt", "enableWebSearchGrounding", "outputColumns"):
        require(key in input_props, f"definition missing input property {key}")

    defaults = definition.get("inputDefaults") or {}
    require(defaults.get("enableWebSearchGrounding") is False, f"wrong enableWebSearchGrounding default: {defaults}")
    if "outputColumns" in defaults:
        require(
            isinstance(defaults.get("outputColumns"), list),
            f"definition outputColumns default must be an array: {defaults}",
        )

    output = (definition.get("outputDefinition") or {}).get("output") or {}
    require(output.get("type") == "file", f"definition output must be file: {output}")
    require(output.get("source") == "=response", f"definition output source must be =response: {output}")

    error_output = (definition.get("outputDefinition") or {}).get("error") or {}
    require(error_output.get("source") == "=Error", f"definition error source must be =Error: {error_output}")
    error_schema = error_output.get("schema")
    if require_full_error_schema or error_schema is not None:
        require(isinstance(error_schema, dict), f"definition error schema must be an object: {error_output}")
        for key in ("code", "message", "detail", "category", "status"):
            require(key in error_schema.get("required", []), f"error schema missing required field {key}")


def check_v1_flow(path: Path, *, require_full_error_schema: bool = False) -> None:
    flow = load_json(path)
    nodes = flow.get("nodes")
    edges = flow.get("edges")
    definitions = flow.get("definitions")
    require(isinstance(nodes, list), "flow has no nodes array")
    require(isinstance(edges, list), "flow has no edges array")
    require(isinstance(definitions, list), "flow has no definitions array")

    matches = [node for node in nodes if node.get("type") == NODE_TYPE]
    require(len(matches) == 1, f"expected exactly one {NODE_TYPE} node, got {len(matches)}")
    node = matches[0]
    require(node.get("id") == NODE_ID, f"expected Batch Transform node id {NODE_ID}, got {node.get('id')}")
    require(node.get("typeVersion") == EXPECTED_VERSION, f"typeVersion must be {EXPECTED_VERSION}, got {node}")
    require("model" not in node, "Batch Transform node instance must not contain a model block")

    inputs = node.get("inputs") or {}
    check_attachment(flow, inputs.get("attachment"))
    require(inputs.get("prompt") == PROMPT, f"wrong prompt: {inputs}")
    require(inputs.get("enableWebSearchGrounding") is False, f"enableWebSearchGrounding must be false: {inputs}")
    check_output_columns(inputs.get("outputColumns"))

    outputs = node.get("outputs") or {}
    output = outputs.get("output") or {}
    error = outputs.get("error") or {}
    require(output.get("type") == "file", f"outputs.output.type must be file, got {outputs}")
    require(output.get("source") == "=response", f"outputs.output.source must be =response, got {outputs}")
    require(error.get("source") == "=Error", f"outputs.error.source must be =Error, got {outputs}")

    check_result_mapping(flow)
    check_definition(definitions, require_full_error_schema=require_full_error_schema)
    validate_flow(path)
    print(f"v1 Batch Transform flow ok: {len(nodes)} nodes, {len(edges)} edges")


def check_v2_project(fil_path: Path, converted_flow_path: Path) -> None:
    source = fil_path.read_text(encoding="utf-8")
    require(".manifest.flow" not in source, "FIL source should not reference a manifest sidecar")
    require(re.search(r"\bflow\s+batch-transform-demo\s*\{", source), "missing flow batch-transform-demo declaration")
    require(re.search(rf"\btrigger\s+{TRIGGER_ID}\s*:\s*start\s*;", source), f"missing trigger {TRIGGER_ID}: start")
    require(
        re.search(rf"\baction\s+{NODE_ID}\s*:\s*{re.escape(NODE_TYPE)}@1\.0(?![\d.])\s*\{{", source),
        f"missing {NODE_ID}: {NODE_TYPE}@1.0 action declaration",
    )
    require(re.search(rf"main\s*\(\s*{FILE_VAR}\s*:\s*file\s*\)", source), "main must take csvFile: file metadata")
    require(
        re.search(rf"executeNode\(\s*{NODE_ID}\s*,", source), f"executeNode must call {NODE_ID} by action identifier"
    )
    require(
        not re.search(rf"executeNode\(\s*['\"]{NODE_ID}['\"]", source),
        f"executeNode should not call {NODE_ID} by string literal",
    )

    # Anchor on individual keys/values rather than whole `{ name, description }`
    # literals so semantically-equivalent FIL (different spacing, single quotes,
    # trailing commas) still passes — `check_v1_flow` already enforces the exact
    # OUTPUT_COLUMNS array on the converted JSON via `value == OUTPUT_COLUMNS`.
    required_terms = [
        "rawInputs:",
        f'attachment: "=js:$vars.{TRIGGER_ID}.output.{FILE_VAR}"',
        PROMPT,
        "enableWebSearchGrounding: false",
        "outputColumns:",
        '"Category"',
        '"Summary"',
        '"One of: Utility, Software, Travel, Other"',
        '"Plain-English one-line summary of the invoice"',
        'output: { type: "file", source: "=response", var: "output" }',
        "fixture:",
        "FullName:",
        "MimeType:",
        "result:",
    ]
    for term in required_terms:
        require(term in source, f"required FIL term {term!r} not found")

    forbidden_terms = ["binding:", "folderBinding:", "resourceBindings", "resourceSubType"]
    for term in forbidden_terms:
        require(term not in source, f"Batch Transform should not use {term}")

    # Forbid dereferencing the csvFile trigger handle. Anchored to the csvFile
    # identifier so unrelated `.Id` / `.FullName` accesses elsewhere in source
    # do not false-positive.
    for attr in ("Id", "FullName"):
        require(
            not re.search(rf"\bcsvFile\.{attr}\b", source),
            f"Batch Transform should not dereference csvFile.{attr}",
        )

    check_v1_flow(converted_flow_path, require_full_error_schema=True)
    print("v2 Batch Transform FIL project shape ok")


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
