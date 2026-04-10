#!/usr/bin/env python3
"""Structural comparison of a generated .flow file against a reference.

Compares topology (node types, edge connectivity, definitions, variables)
while ignoring non-deterministic fields (IDs, positions, timestamps).

Usage:
    python3 check_flow_structure.py <generated.flow> <reference.flow>

Prints a float score (0.0-1.0) to stdout for use with coder-eval's
`score_from_stdout: true` criterion.
"""

import json
import sys
from collections import Counter


def load_flow(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def sorted_node_types(flow: dict) -> list[str]:
    """Extract sorted list of node types from workflow.nodes."""
    return sorted(n["type"] for n in flow.get("nodes", []))


def node_types_used(flow: dict) -> set[str]:
    """Get the set of node types actually used in nodes."""
    return {n["type"] for n in flow.get("nodes", [])}


def global_vars(flow: dict) -> list[tuple[str, str, str]]:
    """Extract sorted list of (id, direction, type) from variables.globals."""
    variables = flow.get("variables", {})
    globals_list = variables.get("globals", [])
    return sorted((v.get("id", ""), v.get("direction", ""), v.get("type", "string")) for v in globals_list)


def node_var_count(flow: dict) -> int:
    """Count entries in variables.nodes."""
    variables = flow.get("variables", {})
    return len(variables.get("nodes", []))


def check_definitions_cover_types(flow: dict) -> bool:
    """Check that every node type used has a matching definition."""
    used = node_types_used(flow)
    defined = {d.get("nodeType", "") for d in flow.get("definitions", [])}
    return used.issubset(defined)


def compare(generated: dict, reference: dict) -> float:
    """Compare generated flow against reference, return score 0.0-1.0."""
    score = 0.0

    # 1. Node types match (0.30)
    gen_types = sorted_node_types(generated)
    ref_types = sorted_node_types(reference)
    if gen_types == ref_types:
        score += 0.30
    else:
        # Partial credit: Jaccard similarity on type multisets (preserves duplicate counts)
        gen_counter = Counter(gen_types)
        ref_counter = Counter(ref_types)
        intersection = sum((gen_counter & ref_counter).values())
        union = sum((gen_counter | ref_counter).values())
        if union:
            score += 0.30 * (intersection / union)

    # 2. Edge count matches (0.20)
    gen_edges = len(generated.get("edges", []))
    ref_edges = len(reference.get("edges", []))
    if gen_edges == ref_edges:
        score += 0.20
    elif ref_edges > 0:
        # Partial credit based on how close the count is
        ratio = min(gen_edges, ref_edges) / max(gen_edges, ref_edges)
        score += 0.20 * ratio

    # 3. Definitions cover all used types (0.20)
    if check_definitions_cover_types(generated):
        score += 0.20
    else:
        # Partial credit: fraction of types covered
        used = node_types_used(generated)
        defined = {d.get("nodeType", "") for d in generated.get("definitions", [])}
        if used:
            coverage = len(used & defined) / len(used)
            score += 0.20 * coverage

    # 4. variables.globals match (0.15)
    gen_globals = global_vars(generated)
    ref_globals = global_vars(reference)
    if gen_globals == ref_globals:
        score += 0.15
    elif ref_globals:
        # Partial credit: fraction of reference globals present in generated
        gen_set = set(gen_globals)
        ref_set = set(ref_globals)
        overlap = len(gen_set & ref_set) / len(ref_set)
        score += 0.15 * overlap

    # 5. variables.nodes present and count reasonable (0.15)
    gen_node_vars = node_var_count(generated)
    ref_node_vars = node_var_count(reference)
    if gen_node_vars == ref_node_vars:
        score += 0.15
    elif gen_node_vars > 0 and ref_node_vars > 0:
        ratio = min(gen_node_vars, ref_node_vars) / max(gen_node_vars, ref_node_vars)
        score += 0.15 * ratio

    return round(score, 4)


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <generated.flow> <reference.flow>", file=sys.stderr)
        sys.exit(1)

    generated_path = sys.argv[1]
    reference_path = sys.argv[2]

    try:
        generated = load_flow(generated_path)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ERROR: Cannot load generated flow: {e}", file=sys.stderr)
        print("0.0")
        sys.exit(0)

    try:
        reference = load_flow(reference_path)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ERROR: Cannot load reference flow: {e}", file=sys.stderr)
        print("0.0")
        sys.exit(0)

    score = compare(generated, reference)

    # Print diagnostic info to stderr, score to stdout
    gen_types = sorted_node_types(generated)
    ref_types = sorted_node_types(reference)
    print(f"Generated: {len(gen_types)} nodes, {len(generated.get('edges', []))} edges", file=sys.stderr)
    print(f"Reference: {len(ref_types)} nodes, {len(reference.get('edges', []))} edges", file=sys.stderr)
    if gen_types != ref_types:
        print(f"Type diff — gen: {gen_types}", file=sys.stderr)
        print(f"Type diff — ref: {ref_types}", file=sys.stderr)
    if not check_definitions_cover_types(generated):
        used = node_types_used(generated)
        defined = {d.get("nodeType", "") for d in generated.get("definitions", [])}
        print(f"Missing definitions: {used - defined}", file=sys.stderr)

    print(score)


if __name__ == "__main__":
    main()
