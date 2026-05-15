#!/usr/bin/env python3
"""Convert the canonical JSON connector library into a markdown library.

Reads per-op JSON files from `--source <dir>/<key>/<op>@<ver>.json` and
writes a parallel tree of `<output>/<key>/<op>@<ver>.md` plus a sibling
`<output>/index.json` whose `path` fields point to the new `.md` files.

The markdown shape groups input fields by `required` so the agent can read
required first and skip optional unless needed. Output schemas land in
their own section. The source library is read-only — the JSON files and
their sibling `.v1def.json` files are left untouched (those are consumed
by the v1-to-v2 / v2-to-v1 converters).
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


def field_line(f: dict) -> str:
    name = f.get("name", "?")
    type_ = f.get("type", "?")
    desc = (f.get("description") or "").replace("\n", " ").strip()
    enum = f.get("enum")
    suffix = f", enum: {' | '.join(map(str, enum))}" if enum else ""
    if desc:
        return f"- `{name}` ({type_}{suffix}) — {desc}"
    return f"- `{name}` ({type_}{suffix})"


def render_op(j: dict) -> str:
    node_type = j.get("nodeType", "?")
    version = j.get("version", "?")
    conn = j.get("connector", {}) or {}
    op = j.get("operation", {}) or {}
    display = j.get("display", {}) or {}

    out = []
    out.append(f"# {node_type}@{version}")
    out.append("")
    conn_label = f"{conn.get('key', '?')}"
    if conn.get("name"):
        conn_label += f" ({conn['name']})"
    out.append(f"**Connector:** {conn_label}")
    op_label = op.get("objectDisplayName") or display.get("label") or op.get("name") or "?"
    out.append(f"**Operation:** {op_label}")
    desc = (display.get("description") or "").strip()
    if desc:
        out.append(f"**Description:** {desc}")
    if op.get("httpMethod") and op.get("path"):
        out.append(f"**HTTP:** {op['httpMethod']} {op['path']}")
    out.append("")

    fields = (j.get("inputSchema") or {}).get("fields") or []
    required = [f for f in fields if f.get("required")]
    optional = [f for f in fields if not f.get("required")]

    out.append("## Required inputs")
    out.append("")
    if required:
        out.extend(field_line(f) for f in required)
    else:
        out.append("_(none)_")
    out.append("")

    if optional:
        out.append("## Optional inputs")
        out.append("")
        out.extend(field_line(f) for f in optional)
        out.append("")

    out_fields = (j.get("outputSchema") or {}).get("fields") or []
    if out_fields:
        out.append("## Outputs")
        out.append("")
        out.extend(field_line(f) for f in out_fields)
        out.append("")

    return "\n".join(out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, help="Source JSON library dir (must contain index.json)")
    p.add_argument("--output", required=True, help="Destination dir for the markdown library")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    src = Path(args.source).resolve()
    dst = Path(args.output).resolve()

    index_path = src / "index.json"
    if not index_path.is_file():
        print(f"source library missing index.json: {index_path}", file=sys.stderr)
        return 1

    # Wipe and recreate destination so stale entries don't linger.
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    converted = 0
    for connector_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        for op_json in sorted(connector_dir.glob("*@*.json")):
            # Skip v1def sidecars — those are the converter's input, not for the agent.
            if op_json.name.endswith(".v1def.json"):
                continue
            j = json.loads(op_json.read_text())
            md = render_op(j)
            out_path = dst / connector_dir.name / (op_json.stem + ".md")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(md)
            converted += 1

    # Rewrite the index pointing at the new .md paths.
    index = json.loads(index_path.read_text())
    for entry in index.get("entries", []):
        p = entry.get("path", "")
        if p.endswith(".json"):
            entry["path"] = p[:-5] + ".md"
    (dst / "index.json").write_text(json.dumps(index, indent=2))

    print(f"wrote {converted} markdown ops to {dst}")
    print("index.json copied with paths rewritten to .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
