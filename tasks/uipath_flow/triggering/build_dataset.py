#!/usr/bin/env python3
"""Regenerate triggering.jsonl from Triggering.csv at the repo root.

Reads the repo-root Triggering.csv, keeps only rows whose ``should_trigger``
is exactly ``yes`` or ``no`` (drops ``unsure``, blanks, typos), strips the
prompt, and writes one JSON object per line to triggering.jsonl next to
this script. Assigns stable ``rNNN`` ids so expanded task_ids are stable
across runs.

Usage:
    uv run python tasks/uipath_flow/triggering/build_dataset.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[2]  # tasks/uipath_flow/triggering -> tasks -> repo root

CSV_PATH = _REPO_ROOT / "Triggering.csv"
OUT_PATH = _SCRIPT_DIR / "triggering.jsonl"

_ALLOWED_LABELS = {"yes", "no"}


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found: {CSV_PATH}")

    kept: list[dict[str, str]] = []
    dropped = 0
    with CSV_PATH.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            label = (raw.get("should_trigger") or "").strip().lower()
            prompt = (raw.get("prompt") or "").strip()
            if label not in _ALLOWED_LABELS or not prompt:
                dropped += 1
                continue
            kept.append(
                {
                    "id": f"r{len(kept) + 1:03d}",
                    "prompt": prompt,
                    "should_trigger": label,
                }
            )

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    yes_count = sum(1 for r in kept if r["should_trigger"] == "yes")
    no_count = len(kept) - yes_count
    print(f"Wrote {len(kept)} rows to {OUT_PATH} (yes={yes_count}, no={no_count}; dropped {dropped})")


if __name__ == "__main__":
    main()
