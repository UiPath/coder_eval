#!/usr/bin/env python3
"""E2E check for the n8n quarter→season switch task.

Deploys the agent's workflow once, then probes all four quarters and asserts
each maps to its expected season.

Usage:
    python3 check_season_e2e.py <workflow.json>
"""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from _shared import (
    ActivateError,
    DeployError,
    deployed_workflow,
    fail,
    post_webhook,
    post_webhook_until_ready,
    response_contains,
)


SEASONS = {1: "Spring", 2: "Summer", 3: "Fall", 4: "Winter"}


def main() -> None:
    with open(sys.argv[1]) as f:
        workflow = json.load(f)

    try:
        with deployed_workflow(workflow) as webhook_path:
            # First call may need the registration retry; subsequent calls won't.
            status, body = post_webhook_until_ready(webhook_path, {"quarter": 1})
            if status != 200:
                fail(0.6, f"ERROR: webhook never returned 200 ({status}): {body}")

            wrong: list[str] = []
            for q, expected in SEASONS.items():
                if q == 1:
                    got_status, got_body = status, body
                else:
                    got_status, got_body = post_webhook(webhook_path, {"quarter": q})
                print(f"  q={q}: status={got_status} body={got_body}", file=sys.stderr)
                if got_status != 200 or not response_contains(got_body, expected):
                    wrong.append(f"q={q} expected {expected!r}, body={got_body!r}")

            if not wrong:
                print("Correct: all 4 quarters mapped", file=sys.stderr)
                print(1.0)
                return
            print(f"WRONG: {'; '.join(wrong)}", file=sys.stderr)
            print(0.8)
    except (DeployError, ActivateError) as e:
        fail(e.score, f"ERROR: {e}")


if __name__ == "__main__":
    main()
