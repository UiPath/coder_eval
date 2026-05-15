#!/usr/bin/env python3
"""Validate an n8n workflow JSON via n8n-mcp's `validate_workflow` tool.

Runs `npx -y n8n-mcp` over stdio JSON-RPC, calls validate_workflow on the
given workflow file, and exits non-zero if validation reports any errors.
Warnings are surfaced but don't fail the check — they're informational
(deprecation notices, "consider adding error handling", etc.) and the task
prompts already tell the agent not to chase them.

Replaces the per-task inline-Python structural checks (BFS over connections,
substring greps, IF-port heuristics) with one apples-to-apples call that
matches what the agent uses during authoring — same parity story as
`uip maestro flow validate` for the flow side.

Usage:
    python3 validate_workflow_mcp.py <workflow.json>

Exit 0 = valid:true (no errors). Exit 1 = invalid or runtime failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


MCP_CMD = ["n8n-mcp"]
INIT_TIMEOUT_S = 120  # first-time `npx -y` may download the package
CALL_TIMEOUT_S = 30


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_workflow_mcp.py <workflow.json>", file=sys.stderr)
        return 1

    wf_path = Path(sys.argv[1])
    if not wf_path.is_file():
        print(f"ERROR: workflow file not found: {wf_path}", file=sys.stderr)
        return 1

    try:
        workflow = json.loads(wf_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {wf_path} is not valid JSON: {e}", file=sys.stderr)
        return 1

    env = {
        **os.environ,
        "MCP_MODE": "stdio",
        "LOG_LEVEL": "error",
        "DISABLE_CONSOLE_OUTPUT": "true",
    }
    proc = subprocess.Popen(
        MCP_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv() -> dict | None:
        # Skip blank lines and any stray non-JSON log output from the server.
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "coder-eval-validator", "version": "1.0"},
                },
            }
        )
        init = recv()
        if not init or "result" not in init:
            print(f"ERROR: MCP initialize failed: {init}", file=sys.stderr)
            return 1

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "validate_workflow",
                    "arguments": {"workflow": workflow},
                },
            }
        )
        resp = recv()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not resp or "result" not in resp:
        print(f"ERROR: validate_workflow returned no result: {resp}", file=sys.stderr)
        return 1

    structured = resp["result"].get("structuredContent")
    if structured is None:
        # fall back to parsing the text payload
        try:
            text = resp["result"]["content"][0]["text"]
            structured = json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            print(f"ERROR: could not parse validate_workflow response: {e}", file=sys.stderr)
            return 1

    summary = structured.get("summary", {})
    errors = structured.get("errors", []) or []
    warnings = structured.get("warnings", []) or []
    valid = bool(structured.get("valid"))

    # Surface errors to stderr so failures are actionable.
    for err in errors:
        node = err.get("node", "?")
        msg = err.get("message", err)
        print(f"  ERR  [{node}] {msg}", file=sys.stderr)
    for warn in warnings:
        node = warn.get("node", "?")
        msg = warn.get("message", warn)
        print(f"  warn [{node}] {msg}", file=sys.stderr)

    head = (
        f"nodes={summary.get('totalNodes', '?')} "
        f"errors={summary.get('errorCount', len(errors))} "
        f"warnings={summary.get('warningCount', len(warnings))}"
    )

    if valid and not errors:
        print(f"OK: {head}")
        return 0

    print(f"FAIL: {head}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
