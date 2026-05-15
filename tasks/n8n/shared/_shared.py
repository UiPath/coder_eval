"""Reusable n8n API helpers for eval task checkers.

Anything broadly applicable across n8n eval tasks lives here. Per-task
assertion logic belongs in the task's own checker.

Env:
    N8N_API_URL   default http://localhost:5678
    N8N_API_KEY   required

Typical use:

    from _shared import deployed_workflow, post_webhook, fail

    with deployed_workflow(workflow_dict) as webhook_path:
        status, body = post_webhook(webhook_path, {"a": 1, "b": 2})
        # ... task-specific assertions ...
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator


API_URL = os.environ.get("N8N_API_URL", "http://localhost:5678").rstrip("/")
API_KEY = os.environ.get("N8N_API_KEY", "")
TIMEOUT_S = 10


class DeployError(Exception):
    score = 0.1


class ActivateError(Exception):
    score = 0.4


def fail(score: float, msg: str) -> None:
    """Print diagnostic to stderr, score to stdout, exit cleanly.

    Exits 0 because coder_eval's score_from_stdout criterion reads the
    score from stdout regardless of exit code.
    """
    print(msg, file=sys.stderr)
    print(score)
    sys.exit(0)


def _parse(raw: str) -> dict | str:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    """Call the n8n API. Returns (status, parsed_body or raw_text)."""
    # /activate silently no-ops webhook registration without a JSON body+content-type
    if method == "POST" and body is None:
        body = {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_URL}{path}", data=data, method=method)
    req.add_header("X-N8N-API-KEY", API_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, _parse(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read().decode())


def post_webhook(path: str, payload: dict) -> tuple[int, dict | str]:
    """POST to a workflow's production webhook (no auth)."""
    req = urllib.request.Request(
        f"{API_URL}/webhook/{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, _parse(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def post_webhook_until_ready(path: str, payload: dict, timeout_s: int = 10) -> tuple[int, dict | str]:
    """Like post_webhook, but retries until 200 or timeout — the webhook can take
    a moment to register after /activate returns."""
    deadline = time.monotonic() + timeout_s
    while True:
        status, body = post_webhook(path, payload)
        if status == 200 or time.monotonic() >= deadline:
            return status, body
        time.sleep(0.5)


def response_contains(body: object, target: object) -> bool:
    """True if `target` appears anywhere in `body`.

    Number target: matches any numeric leaf (int/float, numeric-string, or
    number embedded in a string) that equals `target`.
    String target: matches any string leaf equal to `target`, case-insensitive
    and trimmed. Exact-value match, not substring — avoids false positives
    where the opposite verdict appears inside a longer message.

    Task prompts don't pin agents to a specific response-key name, so checkers
    use this to accept reasonable shape variation (`{"product": 1452}`,
    `{"result": 1452}`, `{"output": {"value": 1452}}`, etc.) without falling
    over on key drift.
    """
    target_num = _as_float(target) if not isinstance(target, str) else None
    if target_num is not None:
        return _find_number(body, target_num)
    return _find_string(body, str(target).strip().lower())


def response_matches_list(
    body: object,
    expected: list[dict],
    keys: tuple[str, ...] | None = None,
) -> bool:
    """True if some list in `body` matches `expected`, unordered.

    If `keys` is given, dicts are projected to those keys before comparison
    (extra fields ignored). Same key-agnostic intent as `response_contains`
    but for structural list payloads — the agent might key the list under
    `books`, `filtered`, `results`, or `data`.
    """

    def project(d: object) -> object:
        if keys is None or not isinstance(d, dict):
            return d
        return tuple(d.get(k) for k in keys)

    try:
        expected_norm = sorted(project(d) for d in expected)  # type: ignore[type-var]
    except TypeError:
        return False

    def walk(node: object) -> bool:
        if isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
            try:
                got = sorted(project(x) for x in node)  # type: ignore[type-var]
            except TypeError:
                got = None
            if got == expected_norm:
                return True
        if isinstance(node, dict):
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    return walk(body)


def _as_float(x: object) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _find_number(node: object, target: float) -> bool:
    if isinstance(node, dict):
        return any(_find_number(v, target) for v in node.values())
    if isinstance(node, list):
        return any(_find_number(v, target) for v in node)
    if isinstance(node, bool):
        return False
    if isinstance(node, (int, float)):
        return float(node) == target
    if isinstance(node, str):
        for tok in re.findall(r"-?\d+(?:\.\d+)?", node):
            try:
                if float(tok) == target:
                    return True
            except ValueError:
                continue
    return False


def _find_string(node: object, target: str) -> bool:
    if isinstance(node, dict):
        return any(_find_string(v, target) for v in node.values())
    if isinstance(node, list):
        return any(_find_string(v, target) for v in node)
    if isinstance(node, str):
        return node.strip().lower() == target
    return False


@contextlib.contextmanager
def deployed_workflow(workflow: dict) -> Iterator[str]:
    """Deploy + activate a workflow; yield its unique webhook path; clean up on exit.

    Deep-copies the input. Appends a UUID suffix to the workflow name and
    every webhook node's path so concurrent runs don't collide. Injects a
    webhookId on any webhook node missing one — n8n's runtime needs it to
    register the production webhook on activation.

    Raises DeployError (score 0.1) or ActivateError (score 0.4).
    """
    if not API_KEY:
        fail(0.0, "ERROR: N8N_API_KEY not set in environment")

    suffix = uuid.uuid4().hex[:8]
    workflow = copy.deepcopy(workflow)

    webhook_path: str | None = None
    for node in workflow.get("nodes", []):
        if node.get("type") == "n8n-nodes-base.webhook":
            params = node.setdefault("parameters", {})
            webhook_path = f"{params.get('path', 'webhook')}-{suffix}"
            params["path"] = webhook_path
            node.setdefault("webhookId", str(uuid.uuid4()))
    if not webhook_path:
        raise DeployError("no webhook node found in workflow")

    payload = {
        "name": f"{workflow.get('name', 'workflow')} [eval-{suffix}]",
        "nodes": workflow["nodes"],
        "connections": workflow.get("connections", {}),
        "settings": {"executionOrder": "v1"},
    }

    status, body = request("POST", "/api/v1/workflows", payload)
    if status not in (200, 201) or not isinstance(body, dict) or "id" not in body:
        raise DeployError(f"deploy failed ({status}): {body}")
    workflow_id = body["id"]
    print(f"Deployed: id={workflow_id}, path={webhook_path}", file=sys.stderr)

    try:
        status, body = request("POST", f"/api/v1/workflows/{workflow_id}/activate")
        if status not in (200, 201):
            raise ActivateError(f"activate failed ({status}): {body}")
        print("Activated", file=sys.stderr)
        yield webhook_path
    finally:
        request("POST", f"/api/v1/workflows/{workflow_id}/deactivate")
        request("DELETE", f"/api/v1/workflows/{workflow_id}")
        print(f"Cleaned up workflow {workflow_id}", file=sys.stderr)
