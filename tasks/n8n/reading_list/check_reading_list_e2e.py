#!/usr/bin/env python3
"""E2E check for the n8n reading-list filter/map task.

Posts a fixed mix of books to the agent's workflow and asserts the response
contains exactly the books that match `difficulty > 5 AND pages < 600`,
projected to {title, author}.

Usage:
    python3 check_reading_list_e2e.py <workflow.json>
"""

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))

from _shared import ActivateError, DeployError, deployed_workflow, fail, post_webhook_until_ready


BOOKS = [
    {"title": "Go Pro", "author": "Alice", "difficulty": 7, "pages": 400},  # match
    {"title": "Easy Read", "author": "Bob", "difficulty": 3, "pages": 200},  # fail: difficulty
    {"title": "Tome", "author": "Carol", "difficulty": 9, "pages": 800},  # fail: pages
    {"title": "Sharp", "author": "Dan", "difficulty": 6, "pages": 599},  # match
    {"title": "Edge", "author": "Eve", "difficulty": 6, "pages": 600},  # fail: pages (boundary)
    {"title": "Equal", "author": "Frank", "difficulty": 5, "pages": 100},  # fail: difficulty (boundary)
]
EXPECTED = sorted(
    [{"title": b["title"], "author": b["author"]} for b in BOOKS if b["difficulty"] > 5 and b["pages"] < 600],
    key=lambda b: b["title"],
)


def main() -> None:
    with open(sys.argv[1]) as f:
        workflow = json.load(f)

    try:
        with deployed_workflow(workflow) as webhook_path:
            status, body = post_webhook_until_ready(webhook_path, {"books": BOOKS})
            if status != 200:
                fail(0.6, f"ERROR: webhook never returned 200 ({status}): {body}")

            print(f"Webhook responded: {body}", file=sys.stderr)
            got = body.get("books") if isinstance(body, dict) else None
            if isinstance(got, list):
                got_norm = sorted(
                    [{"title": b.get("title"), "author": b.get("author")} for b in got if isinstance(b, dict)],
                    key=lambda b: str(b.get("title")),
                )
                if got_norm == EXPECTED:
                    print(f"Correct: {len(EXPECTED)} books returned", file=sys.stderr)
                    print(1.0)
                    return
            print(f"WRONG: expected {EXPECTED}, got {got!r} (body={body})", file=sys.stderr)
            print(0.8)
    except (DeployError, ActivateError) as e:
        fail(e.score, f"ERROR: {e}")


if __name__ == "__main__":
    main()
