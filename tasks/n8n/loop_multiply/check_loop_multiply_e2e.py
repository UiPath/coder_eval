#!/usr/bin/env python3
"""E2E check for the n8n loop-multiply task.

Deploys the agent's workflow, posts a random array of small ints, and asserts
the response equals their product.

Usage:
    python3 check_loop_multiply_e2e.py <workflow.json>
"""

import json
import math
import random
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from _shared import ActivateError, DeployError, deployed_workflow, fail, post_webhook_until_ready


def main() -> None:
    with open(sys.argv[1]) as f:
        workflow = json.load(f)

    nums = [random.randint(2, 9) for _ in range(random.randint(3, 5))]
    expected = math.prod(nums)

    try:
        with deployed_workflow(workflow) as webhook_path:
            status, body = post_webhook_until_ready(webhook_path, {"numbers": nums})
            if status != 200:
                fail(0.6, f"ERROR: webhook never returned 200 ({status}): {body}")

            print(f"Webhook responded: {body} (expected product of {nums} = {expected})", file=sys.stderr)
            product = body.get("product") if isinstance(body, dict) else None
            try:
                if product is not None and float(product) == float(expected):
                    print(f"Correct: prod({nums}) = {product}", file=sys.stderr)
                    print(1.0)
                    return
            except (TypeError, ValueError):
                pass
            print(f"WRONG: expected {expected}, got {product!r} (body={body})", file=sys.stderr)
            print(0.8)
    except (DeployError, ActivateError) as e:
        fail(e.score, f"ERROR: {e}")


if __name__ == "__main__":
    main()
