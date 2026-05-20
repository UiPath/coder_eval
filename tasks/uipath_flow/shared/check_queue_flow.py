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


FLOW_ID = "queue-operations-demo"
TRIGGER_ID = "manualStart"
QUEUE_NAME = "InvoiceProcessingQueue"
QUEUE_FOLDER = "Shared"
QUEUE_KEY = "queue-key-001"
QUEUE_BINDING_NAME = "bQueueName"
QUEUE_BINDING_FOLDER = "bQueueFolder"

CREATE_NODE_ID = "enqueueInvoice"
CREATE_NODE_TYPE = "core.action.queue.create"
CREATE_SERVICE_TYPE = "Orchestrator.CreateQueueItem"
CREATE_LABEL = "Enqueue Invoice"
CREATE_ITEM_DATA = {"invoiceId": "INV-1001", "amount": 123.45, "source": "flow-v2-eval"}
CREATE_PRIORITY = "High"
CREATE_REFERENCE = "INV-1001"
CREATE_DUE_DATE = "2026-06-01T17:00:00Z"

WAIT_NODE_ID = "processInvoiceAndWait"
WAIT_NODE_TYPE = "core.action.queue.create-and-wait"
WAIT_SERVICE_TYPE = "Orchestrator.CreateAndWaitForQueueItem"
WAIT_LABEL = "Process Invoice And Wait"
WAIT_ITEM_DATA = {"invoiceId": "INV-1002", "amount": 987.65, "needsResult": True}
WAIT_PRIORITY = "Normal"
WAIT_REFERENCE = "INV-1002"
WAIT_DUE_DATE = "2026-06-02T17:00:00Z"

EXPECTED_VERSION = "1.0"


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


def expected_queue() -> dict[str, str]:
    return {"name": QUEUE_NAME, "folderPath": QUEUE_FOLDER, "key": QUEUE_KEY}


def decode_item_data(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            fail(f"inputs.itemData is not valid JSON: {exc}")
    return value


def context_by_name(model: dict[str, Any]) -> dict[str, Any]:
    return {entry.get("name"): entry for entry in model.get("context") or [] if isinstance(entry, dict)}


def values_by_name(model: dict[str, Any]) -> dict[str, Any]:
    bindings = model.get("bindings") or {}
    values = bindings.get("values") or []
    require(isinstance(values, list), f"model.bindings.values must be an array: {model}")
    return {entry.get("name"): entry for entry in values if isinstance(entry, dict)}


def check_queue_node(
    node: dict[str, Any],
    *,
    node_id: str,
    node_type: str,
    label: str,
    model_type: str,
    service_type: str,
    item_data: dict[str, Any],
    priority: str,
    reference: str,
    due_date: str,
) -> None:
    require(node.get("id") == node_id, f"expected node id {node_id}, got {node.get('id')}")
    require(node.get("type") == node_type, f"{node_id} has wrong type: {node}")
    require(node.get("typeVersion") == EXPECTED_VERSION, f"{node_id} typeVersion must be {EXPECTED_VERSION}: {node}")
    require(node.get("display", {}).get("label") == label, f"{node_id} has wrong label: {node}")

    inputs = node.get("inputs") or {}
    require(inputs.get("queue") == expected_queue(), f"{node_id} has wrong queue input: {inputs.get('queue')}")
    require(decode_item_data(inputs.get("itemData")) == item_data, f"{node_id} has wrong itemData: {inputs}")
    require(inputs.get("priority") == priority, f"{node_id} has wrong priority: {inputs}")
    require(inputs.get("reference") == reference, f"{node_id} has wrong reference: {inputs}")
    require(inputs.get("deferDate") == "", f"{node_id} has wrong deferDate: {inputs}")
    require(inputs.get("dueDate") == due_date, f"{node_id} has wrong dueDate: {inputs}")

    outputs = node.get("outputs") or {}
    require((outputs.get("output") or {}).get("source") == "=response", f"{node_id} output source is wrong: {outputs}")
    require((outputs.get("error") or {}).get("source") == "=Error", f"{node_id} error source is wrong: {outputs}")

    model = node.get("model")
    require(isinstance(model, dict), f"{node_id} must contain queue resource model metadata")
    require(model.get("type") == model_type, f"{node_id} has wrong model.type: {model}")
    require(model.get("serviceType") == service_type, f"{node_id} has wrong serviceType: {model}")
    require(model.get("version") == "v2", f"{node_id} has wrong model.version: {model}")

    bindings = model.get("bindings") or {}
    require(bindings.get("resource") == "queue", f"{node_id} model.bindings.resource must be queue: {model}")
    require(bindings.get("resourceKey") == QUEUE_KEY, f"{node_id} model.bindings.resourceKey is wrong: {model}")

    values = values_by_name(model)
    require((values.get("name") or {}).get("propertyAttribute") == "name", f"{node_id} missing name binding value")
    require((values.get("name") or {}).get("default") == QUEUE_NAME, f"{node_id} wrong name default: {values}")
    require(
        (values.get("folderPath") or {}).get("propertyAttribute") == "folderPath",
        f"{node_id} missing folderPath binding value",
    )
    require(
        (values.get("folderPath") or {}).get("default") == QUEUE_FOLDER,
        f"{node_id} wrong folderPath default: {values}",
    )

    context = context_by_name(model)
    require(
        (context.get("name") or {}).get("value") == f"=bindings.{QUEUE_BINDING_NAME}",
        f"{node_id} wrong name context",
    )
    require(
        (context.get("folderPath") or {}).get("value") == f"=bindings.{QUEUE_BINDING_FOLDER}",
        f"{node_id} wrong folderPath context",
    )
    require((context.get("_label") or {}).get("value") == label, f"{node_id} wrong _label context")


def check_bindings(flow: dict[str, Any]) -> None:
    bindings = flow.get("bindings") or []
    require(isinstance(bindings, list), "flow bindings must be an array")
    by_id = {binding.get("id"): binding for binding in bindings if isinstance(binding, dict)}

    name = by_id.get(QUEUE_BINDING_NAME)
    folder = by_id.get(QUEUE_BINDING_FOLDER)
    require(name is not None, f"missing queue name binding {QUEUE_BINDING_NAME}")
    require(folder is not None, f"missing queue folder binding {QUEUE_BINDING_FOLDER}")
    require(name.get("resource") == "queue", f"{QUEUE_BINDING_NAME} must use resource queue: {name}")
    require(name.get("resourceKey") == QUEUE_KEY, f"{QUEUE_BINDING_NAME} has wrong resourceKey: {name}")
    require(name.get("default") == QUEUE_NAME, f"{QUEUE_BINDING_NAME} has wrong default: {name}")
    require(name.get("propertyAttribute") == "name", f"{QUEUE_BINDING_NAME} has wrong propertyAttribute: {name}")
    require(folder.get("resource") == "queue", f"{QUEUE_BINDING_FOLDER} must use resource queue: {folder}")
    require(folder.get("resourceKey") == QUEUE_KEY, f"{QUEUE_BINDING_FOLDER} has wrong resourceKey: {folder}")
    require(folder.get("default") == QUEUE_FOLDER, f"{QUEUE_BINDING_FOLDER} has wrong default: {folder}")
    require(
        folder.get("propertyAttribute") == "folderPath",
        f"{QUEUE_BINDING_FOLDER} has wrong propertyAttribute: {folder}",
    )


def check_definition(definitions: list[Any], *, node_type: str, model_type: str, service_type: str) -> None:
    definition = next(
        (
            definition
            for definition in definitions
            if definition.get("nodeType") == node_type and definition.get("version") == EXPECTED_VERSION
        ),
        None,
    )
    require(definition is not None, f"missing {node_type}@{EXPECTED_VERSION} definition")
    require(definition.get("category") == "data-operations", f"wrong category for {node_type}: {definition}")
    require(definition.get("supportsErrorHandling") is True, f"{node_type} must support error handling")
    require(definition.get("display", {}).get("icon") == "list-plus", f"{node_type} has wrong icon: {definition}")
    require(definition.get("model", {}).get("type") == model_type, f"{node_type} has wrong model type: {definition}")
    require(
        definition.get("model", {}).get("serviceType") == service_type,
        f"{node_type} has wrong serviceType: {definition}",
    )
    require(definition.get("model", {}).get("bindings", {}).get("resource") == "queue", f"{node_type} must bind queue")

    input_props = (definition.get("inputDefinition") or {}).get("properties") or {}
    for key in ("queue", "itemData", "priority", "reference", "deferDate", "dueDate"):
        require(key in input_props, f"{node_type} definition missing input property {key}")
    require(input_props.get("itemData", {}).get("type") == "string", f"{node_type} itemData must be string input")

    defaults = definition.get("inputDefaults") or {}
    require(defaults.get("queue") is None, f"{node_type} default queue must be null: {defaults}")
    require(defaults.get("itemData") == "", f"{node_type} default itemData must be empty string: {defaults}")
    require(defaults.get("priority") == "Normal", f"{node_type} default priority must be Normal: {defaults}")

    output = (definition.get("outputDefinition") or {}).get("output") or {}
    error = (definition.get("outputDefinition") or {}).get("error") or {}
    require(output.get("type") == "object", f"{node_type} output must be object: {output}")
    require(output.get("source") == "=response", f"{node_type} output source must be =response: {output}")
    require(error.get("source") == "=Error", f"{node_type} error source must be =Error: {error}")


def check_result_mapping(flow: dict[str, Any]) -> None:
    end_nodes = [node for node in flow.get("nodes") or [] if node.get("type") == "core.control.end"]
    require(end_nodes, "missing End node")
    require(
        any(((node.get("outputs") or {}).get("created") or {}).get("source") == f"=js:$vars.{CREATE_NODE_ID}.output"
            for node in end_nodes),
        f"no End node maps created to $vars.{CREATE_NODE_ID}.output",
    )
    require(
        any(((node.get("outputs") or {}).get("processed") or {}).get("source") == f"=js:$vars.{WAIT_NODE_ID}.output"
            for node in end_nodes),
        f"no End node maps processed to $vars.{WAIT_NODE_ID}.output",
    )


def check_v1_flow(path: Path) -> None:
    flow = load_json(path)
    nodes = flow.get("nodes")
    edges = flow.get("edges")
    definitions = flow.get("definitions")
    require(isinstance(nodes, list), "flow has no nodes array")
    require(isinstance(edges, list), "flow has no edges array")
    require(isinstance(definitions, list), "flow has no definitions array")

    by_id = {node.get("id"): node for node in nodes if isinstance(node, dict)}
    require(TRIGGER_ID in by_id, f"missing trigger {TRIGGER_ID}")
    check_queue_node(
        by_id.get(CREATE_NODE_ID) or {},
        node_id=CREATE_NODE_ID,
        node_type=CREATE_NODE_TYPE,
        label=CREATE_LABEL,
        model_type="bpmn:SendTask",
        service_type=CREATE_SERVICE_TYPE,
        item_data=CREATE_ITEM_DATA,
        priority=CREATE_PRIORITY,
        reference=CREATE_REFERENCE,
        due_date=CREATE_DUE_DATE,
    )
    check_queue_node(
        by_id.get(WAIT_NODE_ID) or {},
        node_id=WAIT_NODE_ID,
        node_type=WAIT_NODE_TYPE,
        label=WAIT_LABEL,
        model_type="bpmn:ServiceTask",
        service_type=WAIT_SERVICE_TYPE,
        item_data=WAIT_ITEM_DATA,
        priority=WAIT_PRIORITY,
        reference=WAIT_REFERENCE,
        due_date=WAIT_DUE_DATE,
    )
    check_bindings(flow)
    check_definition(
        definitions,
        node_type=CREATE_NODE_TYPE,
        model_type="bpmn:SendTask",
        service_type=CREATE_SERVICE_TYPE,
    )
    check_definition(
        definitions,
        node_type=WAIT_NODE_TYPE,
        model_type="bpmn:ServiceTask",
        service_type=WAIT_SERVICE_TYPE,
    )
    check_result_mapping(flow)
    validate_flow(path)
    print(f"v1 Queue flow ok: {len(nodes)} nodes, {len(edges)} edges")


def require_source_term(source: str, term: str) -> None:
    require(term in source, f"required FIL term {term!r} not found")


def check_v2_project(fil_path: Path, converted_flow_path: Path) -> None:
    source = fil_path.read_text(encoding="utf-8")
    require(".manifest.flow" not in source, "FIL source should not reference a manifest sidecar")
    require(re.search(rf"\bflow\s+{re.escape(FLOW_ID)}\s*\{{", source), f"missing flow {FLOW_ID} declaration")
    require(re.search(rf"\btrigger\s+{TRIGGER_ID}\s*:\s*start\s*;", source), f"missing trigger {TRIGGER_ID}: start")
    require(
        re.search(rf"\baction\s+{CREATE_NODE_ID}\s*:\s*{re.escape(CREATE_NODE_TYPE)}@1\.0(?![\d.])\s*\{{", source),
        f"missing {CREATE_NODE_ID}: {CREATE_NODE_TYPE}@1.0 action declaration",
    )
    require(
        re.search(rf"\baction\s+{WAIT_NODE_ID}\s*:\s*{re.escape(WAIT_NODE_TYPE)}@1\.0(?![\d.])\s*\{{", source),
        f"missing {WAIT_NODE_ID}: {WAIT_NODE_TYPE}@1.0 action declaration",
    )
    require(
        re.search(rf"executeNode\(\s*{CREATE_NODE_ID}\s*,", source),
        f"executeNode must call {CREATE_NODE_ID} by action identifier",
    )
    require(
        re.search(rf"executeNode\(\s*{WAIT_NODE_ID}\s*,", source),
        f"executeNode must call {WAIT_NODE_ID} by action identifier",
    )
    require(
        not re.search(rf"executeNode\(\s*['\"]({CREATE_NODE_ID}|{WAIT_NODE_ID})['\"]", source),
        "executeNode should not call Queue actions by string literal",
    )

    for term in [
        'resource: "queue"',
        f'resourceKey: "{QUEUE_KEY}"',
        f'serviceType: "{CREATE_SERVICE_TYPE}"',
        f'serviceType: "{WAIT_SERVICE_TYPE}"',
        f'resourceBindings: {{ name: "{QUEUE_BINDING_NAME}", folderPath: "{QUEUE_BINDING_FOLDER}" }}',
        "rawInputs:",
        f'name: "{QUEUE_NAME}"',
        f'folderPath: "{QUEUE_FOLDER}"',
        f'key: "{QUEUE_KEY}"',
        "itemData:",
        '"INV-1001"',
        '"INV-1002"',
        'priority: "High"',
        'priority: "Normal"',
        'reference: "INV-1001"',
        'reference: "INV-1002"',
        'fixture:',
        "created:",
        "processed:",
    ]:
        require_source_term(source, term)

    forbidden_terms = ["binding:", "folderBinding:", "resourceSubType", "ConnectionId", "FolderKey"]
    for term in forbidden_terms:
        require(term not in source, f"Queue actions should not use connector/process term {term}")

    check_v1_flow(converted_flow_path)
    print("v2 Queue FIL project shape ok")


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
