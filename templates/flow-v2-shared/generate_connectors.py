#!/usr/bin/env python3
"""
Generate the canonical Flow v2 connector library from the UiPath Flow registry.

For each curated connector node type, this script writes a single JSON file
under `--output-dir` (default: `./library/`) with the shape below. The output
is language-neutral and round-trips with Flow v1 definitions: every field a
Flow v1 node needs is either in this file or derivable from the per-node
manifest in a Flow v2 file.

    library/
      <connector-key>/
        <action-id>@<version>.json   # e.g. create-issue@1.0.0.json
      ...
      index.json                      # flat list of every entry, for tooling

Per-entry shape:

    {
      "schemaVersion": "1",
      "nodeType": "uipath.connector.uipath-microsoft-github.create-issue",
      "version": "1.0.0",
      "category": "...",
      "tags": ["connector"],
      "connector": { "key": "uipath-microsoft-github" },
      "operation": {
        "name": "Create",
        "objectName": "create_issues",
        "httpMethod": "POST",
        "subType": "standard",
        "supportsStreaming": false
      },
      "display": { "label": "...", "description": "...", "icon": "...",
                   "iconBackground": "...", "iconBackgroundDark": "..." },
      "runtime": {
        "bpmnType": "bpmn:SendTask",
        "serviceType": "Intsvc.ActivityExecution",
        "activityConfigurationVersion": "1.0.0",
        "requiresConnection": true,
        "requiresFolderKey": true
      },
      "inputSchema":  { "fields": [ ... raw registry shape ... ] },
      "outputSchema": { "fields": [ ... ] }
    }

The script keeps the CLI plumbing (registry search, parallel get, on-disk
cache, resume support) from the cs2fil ancestor unchanged — only the
extraction and write phases differ.

Usage:
    python3 generate_connectors.py [options]

Options:
    --output-dir PATH       Output directory (default: ./library)
    --cache-dir PATH        Cache for raw `flow registry get` JSONs
                            (default: ./.registry-cache)
    --is-cache-dir PATH     Cache for raw `is resources describe` JSONs
                            (default: ./.is-cache)
    --skip-enrichment       Skip the second pass that calls
                            `uip is resources describe` per entry
    --keep-temp             Keep cache directories after run
    --concurrency N         Parallel registry get calls (default: 10)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed


SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate the canonical Flow v2 connector library from the UiPath Flow registry"
    )
    p.add_argument("--output-dir", default="./library", help="Output directory (default: ./library)")
    p.add_argument(
        "--cache-dir",
        default="./.registry-cache",
        help="Cache for `flow registry get` JSONs (default: ./.registry-cache)",
    )
    p.add_argument(
        "--is-cache-dir", default="./.is-cache", help="Cache for `is resources describe` JSONs (default: ./.is-cache)"
    )
    p.add_argument(
        "--connectors-cache-dir",
        default="./.is-connectors-cache",
        help="Cache for `is connectors get` JSONs (default: ./.is-connectors-cache)",
    )
    p.add_argument("--skip-enrichment", action="store_true", help="Skip the `is resources describe` enrichment pass")
    p.add_argument(
        "--skip-connector-enrichment", action="store_true", help="Skip the `is connectors get` connector-level pass"
    )
    p.add_argument("--keep-temp", action="store_true", help="Keep cache directories after run")
    p.add_argument("--concurrency", type=int, default=10, help="Parallel fetches (default: 10)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Registry interaction
# ---------------------------------------------------------------------------


def registry_search(cache_dir):
    """Run uip flow registry search and return list of connector node types."""
    search_file = os.path.join(cache_dir, "_search_results.json")
    if not os.path.exists(search_file):
        print("Running registry search...")
        result = subprocess.run(
            ["uip", "flow", "registry", "search", "--filter", "tags:in=connector", "--output", "json"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Registry search failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
        os.makedirs(cache_dir, exist_ok=True)
        with open(search_file, "w") as f:
            f.write(result.stdout)

    with open(search_file) as f:
        data = json.load(f)

    return [entry["NodeType"] for entry in data.get("Data", []) if entry["NodeType"].startswith("uipath.connector.")]


def registry_get(node_type, cache_dir):
    """Fetch a single connector's JSON, using cache if available."""
    safe_name = node_type.replace(".", "_")
    cache_file = os.path.join(cache_dir, f"{safe_name}.json")
    if os.path.exists(cache_file):
        return cache_file

    result = subprocess.run(
        ["uip", "flow", "registry", "get", node_type, "--output", "json"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  WARN: registry get failed for {node_type}: {result.stderr.strip()}", file=sys.stderr)
        return None

    with open(cache_file, "w") as f:
        f.write(result.stdout)
    return cache_file


def fetch_all(node_types, cache_dir, concurrency):
    """Fetch all connector JSONs in parallel."""
    os.makedirs(cache_dir, exist_ok=True)
    results = {}
    already_cached = 0
    to_fetch = []

    for nt in node_types:
        safe_name = nt.replace(".", "_")
        cache_file = os.path.join(cache_dir, f"{safe_name}.json")
        if os.path.exists(cache_file):
            results[nt] = cache_file
            already_cached += 1
        else:
            to_fetch.append(nt)

    if already_cached:
        print(f"  {already_cached} connectors already cached, {len(to_fetch)} to fetch")

    if to_fetch:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(registry_get, nt, cache_dir): nt for nt in to_fetch}
            done = 0
            for future in as_completed(futures):
                nt = futures[future]
                done += 1
                path = future.result()
                if path:
                    results[nt] = path
                if done % 50 == 0 or done == len(to_fetch):
                    print(f"  Fetched {done}/{len(to_fetch)}")

    return results


# ---------------------------------------------------------------------------
# Canonical extraction
# ---------------------------------------------------------------------------
# Curated connector filter — same rules as the cs2fil ancestor:
#   activityType == Curated, isEnabled, targetPlatform == CrossPlatform,
#   isExperimental == false. Anything else is skipped.


def _context_value(context, name):
    """Return the `value` of a context entry by name, or None."""
    for c in context:
        if c.get("name") == name:
            return c.get("value")
    return None


def _has_context(context, name):
    return any(c.get("name") == name for c in context)


def extract_canonical(json_path):
    """Build a canonical-library entry from one raw registry JSON file.

    Returns the entry dict, or None if the connector is filtered out
    (not enabled, not curated, etc.).
    """
    with open(json_path) as f:
        data = json.load(f)

    node = data.get("Data", {}).get("Node")
    if not node:
        return None

    node_type = node.get("nodeType", "")
    model = node.get("model", {}) or {}
    context = model.get("context", []) or []

    # Activity-level metadata moved out of model.context.metadata into the
    # editor form. Search `node.form.sections[*].fields[*].componentProps`
    # for the `connectorDetail` block — it carries isEnabled / targetPlatform
    # / isExperimental / configuration (a JSON string with activityType etc).
    connector_detail = None
    for section in (node.get("form", {}) or {}).get("sections", []) or []:
        for f in section.get("fields", []) or []:
            cp = (f.get("componentProps") or {}).get("connectorDetail")
            if isinstance(cp, dict) and "configuration" in cp:
                connector_detail = cp
                break
        if connector_detail:
            break

    if not connector_detail:
        return None

    if not connector_detail.get("isEnabled", False):
        return None
    if connector_detail.get("targetPlatform") != "CrossPlatform":
        return None
    if connector_detail.get("isExperimental", True):
        return None

    try:
        config = json.loads(connector_detail.get("configuration", "{}"))
    except json.JSONDecodeError:
        return None

    if config.get("activityType") != "Curated":
        return None

    # Operation name — the human-readable verb (Create/Retrieve/etc.) lives
    # on connectorMethodInfo when present; fall back to the model.context entry.
    cmi = node.get("connectorMethodInfo", {}) or {}
    operation_name = cmi.get("operation") or _context_value(context, "operation") or ""

    # API endpoint path. `connectorMethodInfo.path` is in the un-enriched
    # registry response (no connection-id needed) and matches the
    # `inputs.detail.endpoint` field in v1 flow nodes — see uipath-maestro-flow
    # connector plugin docs.
    path = cmi.get("path") or ""

    # Operation/object display name — what the editor uses for "objectDisplayName"
    # in the v1 configuration blob. Lives under `connectorMethodInfo.curated`
    # when present; we fall back to the top-level `display.label`.
    curated = cmi.get("curated") or {}
    object_display_name = curated.get("displayName") or ""

    # Input fields: prefer inputDefinition, fall back to connectorMethodInfo.parameters.
    input_fields = node.get("inputDefinition", {}).get("fields") or []
    if not input_fields:
        for p in cmi.get("parameters", []) or []:
            f = {
                "name": p.get("name", ""),
                "type": p.get("dataType", "string"),
                "required": p.get("required", False),
                "description": p.get("description", ""),
            }
            if "enum" in p:
                f["enum"] = p["enum"]
            input_fields.append(f)

    output_fields = (
        node.get("outputDefinition", {}).get("fields") or node.get("outputResponseDefinition", {}).get("fields") or []
    )

    display = node.get("display", {}) or {}

    entry = {
        "schemaVersion": SCHEMA_VERSION,
        "nodeType": node_type,
        "version": config.get("version", "") or node.get("version", ""),
        "category": node.get("category", ""),
        "tags": node.get("tags", []),
        "connector": {
            "key": config.get("connectorKey", ""),
        },
        "operation": {
            "name": operation_name,
            "objectName": config.get("objectName", ""),
            "objectDisplayName": object_display_name,
            "httpMethod": config.get("httpMethod", ""),
            "path": path,
            "subType": config.get("subType", ""),
            "supportsStreaming": config.get("supportsStreaming", False),
        },
        "display": {
            "label": display.get("label", "") or node.get("displayName", ""),
            "description": display.get("description", "") or node.get("description", ""),
            "icon": display.get("icon", ""),
            "iconBackground": display.get("iconBackground", ""),
            "iconBackgroundDark": display.get("iconBackgroundDark", ""),
        },
        "runtime": {
            "bpmnType": model.get("type", ""),
            "serviceType": model.get("serviceType", ""),
            "activityConfigurationVersion": _context_value(context, "activityConfigurationVersion") or "",
            "requiresConnection": _has_context(context, "connection"),
            "requiresFolderKey": _has_context(context, "folderKey"),
        },
        "inputSchema": {"fields": input_fields},
        "outputSchema": {"fields": output_fields},
    }
    return entry


# ---------------------------------------------------------------------------
# `is connectors get` enrichment (connector-level, not action-level)
# ---------------------------------------------------------------------------
#
# Each (connector_key) → one canonical connector record carrying the human
# display name. The editor stamps this on every node it builds for the
# connector as `inputs.detail.configuration.connectorName`. Exposing it on
# the library entry lets v1→v2 strip the redundant copy and v2→v1 reproduce
# it from the library.


def is_connector_get(connector_key, cache_dir):
    """Cached `uip is connectors get <key>`.

    Returns the parsed first-element dict, or None on any failure.
    """
    safe = _safe_filename(connector_key, "_connector")
    cache_file = os.path.join(cache_dir, f"{safe}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            d = json.load(f)
        data = d.get("Data") or []
        return data[0] if isinstance(data, list) and data else None
    result = subprocess.run(
        ["uip", "is", "connectors", "get", connector_key, "--output", "json"], capture_output=True, text=True
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if payload.get("Result") != "Success":
        with open(cache_file, "w") as f:
            json.dump({"Result": "Failure", "_cached_failure": True}, f)
        return None
    with open(cache_file, "w") as f:
        json.dump(payload, f)
    data = payload.get("Data") or []
    return data[0] if isinstance(data, list) and data else None


def fetch_connector_records(entries, cache_dir, concurrency):
    """Fetch one `is connectors get` per unique connector key in entries.

    Returns a dict of connector_key → connector record (or None).
    """
    os.makedirs(cache_dir, exist_ok=True)
    keys = sorted({e["connector"]["key"] for e in entries if e["connector"]["key"]})
    print(f"  Looking up {len(keys)} connector records...")
    out = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(is_connector_get, k, cache_dir): k for k in keys}
        done = 0
        for fut in as_completed(futures):
            k = futures[fut]
            done += 1
            try:
                out[k] = fut.result()
            except Exception:
                out[k] = None
            if done % 50 == 0 or done == len(keys):
                print(f"  Fetched {done}/{len(keys)} connector records")
    return out


def merge_connector_record(entry, record):
    """Stamp connector-level metadata onto a canonical entry."""
    if not record:
        return entry
    name = record.get("Name")
    if name:
        entry["connector"]["name"] = name
    return entry


# ---------------------------------------------------------------------------
# `is resources describe` enrichment
# ---------------------------------------------------------------------------
#
# `flow registry get` (without --connection-id) returns a sparse view: many
# connectors come back with empty inputDefinition/outputDefinition and no
# connectorMethodInfo. `is resources describe <connector> <object>
# --operation <op>` returns the same data plus path placeholders, parameter
# split (path vs query), and full request/response field schemas — and
# crucially does so without a connection ID for ALL schema-static connectors.
#
# Schema-DYNAMIC connectors (Salesforce SFDC and similar) return empty
# requestFields/responseFields without a connection-id. We detect this and
# tag the entry so downstream tooling knows to consult a per-flow sidecar.


def _safe_filename(*parts):
    """Sanitize parts of a filename so they survive on any FS."""
    s = "__".join(parts)
    return re.sub(r"[^a-zA-Z0-9_.@\-]+", "_", s)


def is_describe_operation(connector_key, object_name, operation, cache_dir):
    """Cached `uip is resources describe <connector> <object> --operation <op>`.

    Returns the parsed Data dict on success, or None on any failure (the
    connector/object/operation combo doesn't exist, network error, etc.).
    """
    safe = _safe_filename(connector_key, object_name, operation)
    cache_file = os.path.join(cache_dir, f"{safe}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f).get("Data")

    result = subprocess.run(
        [
            "uip",
            "is",
            "resources",
            "describe",
            connector_key,
            object_name,
            "--operation",
            operation,
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if payload.get("Result") != "Success":
        # Cache the failure too, with a sentinel, so retries are skipped.
        with open(cache_file, "w") as f:
            json.dump({"Result": "Failure", "_cached_failure": True}, f)
        return None
    with open(cache_file, "w") as f:
        json.dump(payload, f)
    return payload.get("Data")


def is_describe_object(connector_key, object_name, cache_dir):
    """Cached `uip is resources describe <connector> <object>` (no --operation).

    Returns the parsed Data dict on success (with `availableOperations`),
    or None on failure.
    """
    safe = _safe_filename(connector_key, object_name, "_operations")
    cache_file = os.path.join(cache_dir, f"{safe}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f).get("Data")
    result = subprocess.run(
        ["uip", "is", "resources", "describe", connector_key, object_name, "--output", "json"],
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if payload.get("Result") != "Success":
        with open(cache_file, "w") as f:
            json.dump({"Result": "Failure", "_cached_failure": True}, f)
        return None
    with open(cache_file, "w") as f:
        json.dump(payload, f)
    return payload.get("Data")


# Map from registry httpMethod (e.g. "GETBYID") to a guess at the
# `is resources` operation name. The `is resources` API uses verb names like
# "Retrieve" / "List" / "Create"; the registry uses HTTP-method-shaped strings.
HTTP_METHOD_TO_OPERATION = {
    "GET": "List",
    "GETBYID": "Retrieve",
    "POST": "Create",
    "PATCH": "Update",
    "PUT": "Replace",
    "DELETE": "Delete",
}


def discover_operation_name(entry, is_cache_dir):
    """Find the `is resources` operation name for an entry whose
    canonical `operation.name` is empty.

    Strategy: list available operations on the object, then match by the
    httpMethod we already extracted from the registry.
    """
    connector_key = entry["connector"]["key"]
    object_name = entry["operation"]["objectName"]
    http_method = entry["operation"]["httpMethod"]
    if not connector_key or not object_name:
        return None
    obj_data = is_describe_object(connector_key, object_name, is_cache_dir)
    if not obj_data:
        return None
    for op in obj_data.get("availableOperations", []) or []:
        if op.get("method") == http_method:
            return op.get("name")
    return None


def enrich_entry(entry, is_cache_dir):
    """Augment a canonical entry with `is resources describe` data.

    Returns the (mutated) entry. On any failure (object not found, no
    operation match, etc.) returns the entry unchanged so the pipeline
    degrades gracefully.
    """
    connector_key = entry["connector"]["key"]
    object_name = entry["operation"]["objectName"]
    op_name = entry["operation"]["name"]

    # Fallback: no operation name from the registry — discover via httpMethod.
    if not op_name:
        op_name = discover_operation_name(entry, is_cache_dir) or HTTP_METHOD_TO_OPERATION.get(
            entry["operation"]["httpMethod"]
        )
    if not op_name:
        return entry  # nothing to do

    data = is_describe_operation(connector_key, object_name, op_name, is_cache_dir)
    if not data:
        return entry  # connector might be a stub or recently removed

    op_data = data.get("operation", {}) or {}

    # If we'd discovered the name via fallback, persist it now.
    if not entry["operation"]["name"] and op_data.get("name"):
        entry["operation"]["name"] = op_data["name"]

    # Path with placeholders (supersedes the simple `path` we extracted).
    if op_data.get("path"):
        entry["operation"]["pathTemplate"] = op_data["path"]
    # Object/operation display name from `is resources` data.
    if not entry["operation"].get("objectDisplayName"):
        if data.get("displayName"):
            entry["operation"]["objectDisplayName"] = data["displayName"]
    if op_data.get("curated") and not entry["display"].get("operationLabel"):
        entry["display"]["operationLabel"] = op_data["curated"]

    # Parameters (path / query) — keep the raw shape; downstream tooling
    # filters by `type` (path | query).
    params = data.get("parameters") or []
    if params:
        entry["operation"]["parameters"] = params

    # Schema. Empty request+response means schema-dynamic (needs connection-id).
    request_fields = data.get("requestFields") or []
    response_fields = data.get("responseFields") or []
    schema_dynamic = (not request_fields) and (not response_fields)
    entry["runtime"]["requiresConnectionForSchema"] = schema_dynamic

    if request_fields:
        # Replace/augment the registry-derived inputSchema.
        entry["inputSchema"] = {"fields": request_fields}
    if response_fields:
        entry["outputSchema"] = {"fields": response_fields}

    return entry


def enrich_all(entries, is_cache_dir, concurrency):
    """Run enrich_entry on every canonical entry in parallel."""
    os.makedirs(is_cache_dir, exist_ok=True)
    enriched = [None] * len(entries)
    schema_dynamic = 0
    pathTemplate_added = 0
    schema_added = 0

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(enrich_entry, e, is_cache_dir): i for i, e in enumerate(entries)}
        done = 0
        for fut in as_completed(futures):
            i = futures[fut]
            done += 1
            try:
                enriched[i] = fut.result()
            except Exception as exc:
                # Non-fatal — keep the un-enriched entry.
                enriched[i] = entries[i]
                print(f"  WARN: enrichment failed for {entries[i]['nodeType']}: {exc}", file=sys.stderr)
            if done % 100 == 0 or done == len(entries):
                print(f"  Enriched {done}/{len(entries)}")

    for e in enriched:
        if e.get("runtime", {}).get("requiresConnectionForSchema"):
            schema_dynamic += 1
        if e.get("operation", {}).get("pathTemplate"):
            pathTemplate_added += 1
        if e.get("inputSchema", {}).get("fields") or e.get("outputSchema", {}).get("fields"):
            schema_added += 1

    print(f"  pathTemplate added on {pathTemplate_added}/{len(entries)}")
    print(f"  schema added/augmented on {schema_added}/{len(entries)}")
    print(f"  schema-dynamic (need connection-id): {schema_dynamic}/{len(entries)}")
    return enriched


# ---------------------------------------------------------------------------
# Library output
# ---------------------------------------------------------------------------


def _action_id_from_node_type(node_type):
    """Extract the action segment from a uipath.connector.<key>.<action> nodeType."""
    parts = node_type.split(".")
    return parts[-1] if len(parts) >= 4 else node_type.replace(".", "-")


def _build_v1_definition(node_data):
    """Project a registry `Data.Node` into the shape the v1 .flow file
    expects under definitions[]. The registry response is a near-superset:
    drop fields the v1 def doesn't carry (`connectorMethodInfo`,
    `outputResponseDefinition`) and add three standard fields the editor
    stamps on every connector definition (`supportsErrorHandling`,
    `inputDefaults`, `debug`).
    """
    keep = {k: v for k, v in node_data.items() if k not in ("connectorMethodInfo", "outputResponseDefinition")}
    keep.setdefault("supportsErrorHandling", True)
    keep.setdefault("inputDefaults", {})
    keep.setdefault("debug", {"runtime": "bpmnEngine"})
    return keep


def _load_node_data(json_path):
    """Pull `Data.Node` from a cached `flow registry get` response."""
    with open(json_path) as f:
        d = json.load(f)
    return d.get("Data", {}).get("Node")


def write_library(output_dir, entries, v1def_source_paths):
    """Write one JSON file per (connector, action, version) plus a sibling
    `.v1def.json` carrying the v1 definitions[] shape, plus an index.json.

    `v1def_source_paths` maps nodeType → path of the cached registry JSON
    so we can reach back to the raw `Data.Node` at write time.
    """
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    index = []
    written = 0

    for entry in entries:
        connector_key = entry["connector"]["key"] or "unknown"
        action_id = _action_id_from_node_type(entry["nodeType"])
        version = entry["version"] or "0.0.0"

        connector_dir = os.path.join(output_dir, connector_key)
        os.makedirs(connector_dir, exist_ok=True)

        filename = f"{action_id}@{version}.json"
        path = os.path.join(connector_dir, filename)
        with open(path, "w") as f:
            json.dump(entry, f, indent=2)
            f.write("\n")
        written += 1

        # v1 definition sidecar — used by the v2→v1 converter.
        v1def_src = v1def_source_paths.get(entry["nodeType"])
        if v1def_src:
            try:
                node_data = _load_node_data(v1def_src)
                if node_data:
                    v1def = _build_v1_definition(node_data)
                    v1def_path = os.path.join(connector_dir, f"{action_id}@{version}.v1def.json")
                    with open(v1def_path, "w") as f:
                        json.dump(v1def, f, indent=2)
                        f.write("\n")
            except Exception as exc:
                print(f"  WARN: v1def write failed for {entry['nodeType']}: {exc}", file=sys.stderr)

        index.append(
            {
                "nodeType": entry["nodeType"],
                "version": version,
                "connectorKey": connector_key,
                "label": entry["display"]["label"],
                "path": os.path.relpath(path, output_dir),
            }
        )

    # Sort index for stable diffs.
    index.sort(key=lambda e: (e["connectorKey"], e["nodeType"], e["version"]))

    index_path = os.path.join(output_dir, "index.json")
    with open(index_path, "w") as f:
        json.dump({"schemaVersion": SCHEMA_VERSION, "entries": index}, f, indent=2)
        f.write("\n")

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    args = parse_args()

    print(f"Cache directory: {args.cache_dir}")
    print(f"Output directory: {args.output_dir}")

    node_types = registry_search(args.cache_dir)
    print(f"Found {len(node_types)} connector node types")

    print(f"Fetching connector details (concurrency={args.concurrency})...")
    cache_files = fetch_all(node_types, args.cache_dir, args.concurrency)
    print(f"Successfully fetched {len(cache_files)} connectors")

    print("Extracting curated connectors...")
    entries = []
    v1def_source_paths = {}
    for nt, path in sorted(cache_files.items()):
        try:
            entry = extract_canonical(path)
            if entry:
                entries.append(entry)
                v1def_source_paths[entry["nodeType"]] = path
        except Exception as e:
            print(f"  WARN: error processing {nt}: {e}", file=sys.stderr)

    print(f"Found {len(entries)} curated connectors")

    if not args.skip_enrichment:
        print(f"Enriching via `is resources describe` (concurrency={args.concurrency})...")
        entries = enrich_all(entries, args.is_cache_dir, args.concurrency)

    if not args.skip_connector_enrichment:
        print(f"Enriching via `is connectors get` (concurrency={args.concurrency})...")
        connector_records = fetch_connector_records(entries, args.connectors_cache_dir, args.concurrency)
        named = 0
        for e in entries:
            ck = e["connector"]["key"]
            rec = connector_records.get(ck)
            merge_connector_record(e, rec)
            if e["connector"].get("name"):
                named += 1
        print(f"  Named {named}/{len(entries)} entries from connector records")

    print("Writing canonical library...")
    written = write_library(args.output_dir, entries, v1def_source_paths)
    print(f"Wrote {written} library entries + index.json")

    if not args.keep_temp:
        print("Cleaning up caches...")
        shutil.rmtree(args.cache_dir, ignore_errors=True)
        shutil.rmtree(args.is_cache_dir, ignore_errors=True)
        shutil.rmtree(args.connectors_cache_dir, ignore_errors=True)

    print("Done!")


if __name__ == "__main__":
    main()
