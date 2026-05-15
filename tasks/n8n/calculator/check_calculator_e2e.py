#!/usr/bin/env python3
"""E2E check for the n8n calculator (multiply) task.

Deploys the agent's workflow to a live n8n, hits its webhook with random
numbers, and asserts the response is their product.

Usage:
    python3 check_calculator_e2e.py <workflow.json>

Score (0.0-1.0) is printed to stdout; diagnostics go to stderr.
"""

import json
import random
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from _shared import ActivateError, DeployError, deployed_workflow, fail, post_webhook_until_ready, response_contains


def main() -> None:
    with open(sys.argv[1]) as f:
        workflow = json.load(f)

    try:
        with deployed_workflow(workflow) as webhook_path:
            a, b = random.randint(2, 99), random.randint(2, 99)
            status, body = post_webhook_until_ready(webhook_path, {"a": a, "b": b})
            if status != 200:
                fail(0.6, f"ERROR: webhook never returned 200 ({status}): {body}")

            print(f"Webhook responded: {body}", file=sys.stderr)
            if response_contains(body, a * b):
                print(f"Correct: {a} * {b} = {a * b} (found in response)", file=sys.stderr)
                print(1.0)
                return
            print(f"WRONG: expected {a} * {b} = {a * b}, not found anywhere in body={body}", file=sys.stderr)
            print(0.8)
    except (DeployError, ActivateError) as e:
        fail(e.score, f"ERROR: {e}")


if __name__ == "__main__":
    main()
