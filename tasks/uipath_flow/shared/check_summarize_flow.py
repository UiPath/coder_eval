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


NODE_ID = "summarizeContract"
NODE_TYPE = "uipath.pattern.deep-rag"
EXPECTED_VERSION = "1.0"
TRIGGER_ID = "manualStart"
FILE_VAR = "documentFile"
PROMPT = "Write a 5-bullet executive summary covering scope, term, SLAs, penalties, and termination."


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


def check_v1_flow(path: Path) -> None:
    flow = load_json(path)
    nodes = flow.get("nodes")
    edges = flow.get("edges")
    definitions = flow.get("definitions")
    variables = flow.get("variables") or {}
    require(isinstance(nodes, list), "flow has no nodes array")
    require(isinstance(edges, list), "flow has no edges array")
    require(isinstance(definitions, list), "flow has no definitions array")

    matches = [node for node in nodes if node.get("type") == NODE_TYPE]
    require(len(matches) == 1, f"expected exactly one {NODE_TYPE} node, got {len(matches)}")
    node = matches[0]
    require(node.get("id") == NODE_ID, f"expected Summarize node id {NODE_ID}, got {node.get('id')}")
    require(node.get("typeVersion") == EXPECTED_VERSION, f"typeVersion must be {EXPECTED_VERSION}, got {node}")
    require("model" not in node, "Summarize node instance must not contain a model block")

    inputs = node.get("inputs") or {}
    expected_attachment = f"=js:$vars.{TRIGGER_ID}.output.{FILE_VAR}"
    require(inputs.get("attachment") == expected_attachment, f"wrong attachment input: {inputs}")
    require(inputs.get("prompt") == PROMPT, f"wrong prompt: {inputs}")
    require(inputs.get("returnCitations") is True, f"returnCitations must be boolean true: {inputs}")

    outputs = node.get("outputs") or {}
    output = outputs.get("output") or {}
    error = outputs.get("error") or {}
    require(output.get("source") == "=response", f"outputs.output.source must be =response, got {outputs}")
    require(error.get("source") == "=Error", f"outputs.error.source must be =Error, got {outputs}")

    trigger = next((n for n in nodes if n.get("id") == TRIGGER_ID), None)
    require(trigger is not None and "trigger" in str(trigger.get("type")), f"missing trigger {TRIGGER_ID}")

    globals_ = variables.get("globals") or []
    file_var = next((v for v in globals_ if v.get("id") == FILE_VAR), None)
    require(file_var is not None, f"missing flow input variable {FILE_VAR}")
    require(file_var.get("direction") == "in", f"{FILE_VAR} must be an input variable: {file_var}")
    require(file_var.get("type") == "file", f"{FILE_VAR} must have type file: {file_var}")
    require(
        file_var.get("triggerNodeId") == TRIGGER_ID,
        f"{FILE_VAR} must be bound to trigger {TRIGGER_ID}: {file_var}",
    )

    for out_var in ("summary", "citations"):
        require(
            any(v.get("id") == out_var and v.get("direction") == "out" for v in globals_),
            f"missing out variable {out_var}",
        )

    end_nodes = [n for n in nodes if n.get("type") == "core.control.end"]
    require(end_nodes, "missing End node")
    expected_mappings = {
        "summary": f"=js:$vars.{NODE_ID}.output.content.Text",
        "citations": f"=js:$vars.{NODE_ID}.output.content.Citations",
    }
    for output_id, expected_source in expected_mappings.items():
        has_mapping = any(
            ((end.get("outputs") or {}).get(output_id) or {}).get("source") == expected_source
            for end in end_nodes
        )
        require(
            has_mapping,
            f"no End node maps {output_id} to {expected_source}",
        )

    definition = next(
        (
            definition
            for definition in definitions
            if definition.get("nodeType") == NODE_TYPE and definition.get("version") == EXPECTED_VERSION
        ),
        None,
    )
    require(definition is not None, f"missing {NODE_TYPE}@{EXPECTED_VERSION} definition")
    require(
        definition.get("model", {}).get("serviceType") == "ECS.DeepRag",
        f"Summarize definition has wrong model.serviceType: {definition}",
    )
    # Match JSON-quoted forms so bare property names like "Text" cannot be
    # satisfied by an unrelated identifier (e.g. "MyText") elsewhere in the blob.
    definition_blob = json.dumps(definition)
    for term in ("Text", "Citations", "Ordinal", "PageNumber", "Source", "Reference"):
        require(f'"{term}"' in definition_blob, f"definition is missing output schema evidence {term!r}")
    require('"=response"' in definition_blob, "definition is missing =response source")

    validate_flow(path)
    print(f"v1 Summarize flow ok: {len(nodes)} nodes, {len(edges)} edges")


def check_v2_project(fil_path: Path, converted_flow_path: Path) -> None:
    source = fil_path.read_text(encoding="utf-8")
    require(".manifest.flow" not in source, "FIL source should not reference a manifest sidecar")
    require(re.search(r"\bflow\s+summarize-contract\s*\{", source), "missing flow summarize-contract declaration")
    require(re.search(rf"\btrigger\s+{TRIGGER_ID}\s*:\s*start\s*;", source), f"missing trigger {TRIGGER_ID}: start")
    require(
        re.search(rf"\baction\s+{NODE_ID}\s*:\s*{re.escape(NODE_TYPE)}@1\.0(?![\d.])\s*\{{", source),
        f"missing {NODE_ID}: {NODE_TYPE}@1.0 action declaration",
    )
    require(
        re.search(rf"executeNode\(\s*{NODE_ID}\s*,", source),
        f"executeNode must call {NODE_ID} by action identifier",
    )
    require(
        not re.search(rf"executeNode\(\s*['\"]{NODE_ID}['\"]", source),
        f"executeNode should not call {NODE_ID} by string literal",
    )
    require(
        re.search(rf"main\s*\(\s*{FILE_VAR}\s*:\s*file\s*\)", source),
        "main must take opaque trigger-bound documentFile: file metadata",
    )

    # Anchor each required token to its surrounding FIL context (object key
    # followed by `:` or full expression with quotes) so unrelated identifiers
    # like `MyText` or `BaseSource` cannot satisfy the check.
    required_terms = [
        "rawInputs:",
        f'attachment: "=js:$vars.{TRIGGER_ID}.output.{FILE_VAR}"',
        PROMPT,
        "returnCitations: true",
        'source: "=response"',
        "fixture:",
        "Text:",
        "Citations:",
        "Ordinal:",
        "PageNumber:",
        "Source:",
        "Reference:",
        "summary:",
        "citations:",
    ]
    for term in required_terms:
        require(term in source, f"required FIL term {term!r} not found")

    require("binding:" not in source and "folderBinding:" not in source, "Summarize should not use connector bindings")
    require("resourceBindings" not in source, "Summarize should not use process resource bindings")
    require("resourceSubType" not in source, "Summarize should not use process resource metadata")

    check_v1_flow(converted_flow_path)
    print("v2 Summarize FIL project shape ok")


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
