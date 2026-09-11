#!/usr/bin/env python3
"""Decompose recorded turns into the four wall-clock buckets, per harness.

Reads `task.json` files, groups their turns by `agent_type`, and prints the
mean generation / tool / startup / teardown against the mean turn duration,
plus the residual as a percentage of wall clock. A healthy harness reconciles
to well under 1%. The tool bucket is the UNION of the command intervals, never
their sum — tool calls overlap, and summing them books the overlap twice.

    uv run python scripts/timing/decompose_run.py runs/<run>/default/*/00/task.json

Not wired into `make`: it needs live runs, not fixtures. NOTE `scripts/` is
outside the Makefile's LINT_PATHS, so this file is neither formatted nor
ruff-checked — keep it small and dependency-free.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from coder_eval.timing import busy_ms


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _tool_ms(turn: dict) -> float:
    """Wall ms this turn spent executing tools — the UNION, not the sum.

    The same rule `coder_eval.timing.busy_ms` applies when a harness subtracts
    tool time out of a generation window, and it has to be the same rule here
    or the identity does not close: Pi resolved a `Write` and a `Bash` that
    overlapped by 18.4 ms in one measured turn, and summing their durations
    booked that overlap twice, which is precisely the 18.3 ms residual that
    found this. A command with no recorded bounds cannot be placed on the
    timeline at all, so it contributes nothing rather than being summed in
    blind — see docs/agents/HARNESS_PARITY.md's Delegate divergence.
    """
    spans = []
    for command in turn.get("commands") or []:
        start = _parse(command.get("execution_started_at"))
        end = _parse(command.get("execution_completed_at"))
        if start is not None and end is not None and end >= start:
            spans.append((start, end))
    if not spans:
        return 0.0
    return busy_ms(spans, min(s for s, _ in spans), max(e for _, e in spans))


def _turn_buckets(turn: dict) -> tuple[float, float, float, float, float] | None:
    """(wall_ms, generation_ms, tool_ms, startup_ms, teardown_ms) for one turn.

    None when the turn was never timed at all — a crash partial with no
    generation. A bucket the harness could not measure counts as 0 toward the
    sums while the turn still contributes its wall clock, so an unmeasured
    bucket shows up as residual rather than silently vanishing.
    """
    duration_seconds = turn.get("duration_seconds")
    if not isinstance(duration_seconds, (int, float)):
        return None
    messages = turn.get("messages") or []
    generation_ms = sum(
        m.get("generation_duration_ms") or 0.0
        for m in messages
        if m.get("role") == "assistant" and isinstance(m.get("generation_duration_ms"), (int, float))
    )
    startup_ms = turn.get("harness_startup_ms")
    teardown_ms = turn.get("harness_teardown_ms")
    return (
        duration_seconds * 1000.0,
        generation_ms,
        _tool_ms(turn),
        startup_ms if isinstance(startup_ms, (int, float)) else 0.0,
        teardown_ms if isinstance(teardown_ms, (int, float)) else 0.0,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_json", nargs="+", type=Path, help="task.json files to decompose")
    args = parser.parse_args(argv)

    by_harness: dict[str, list[tuple[float, float, float, float, float]]] = defaultdict(list)
    for path in args.task_json:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        harness = (record.get("environment_info") or {}).get("agent_type") or record.get("agent_type") or "unknown"
        for turn in record.get("iterations") or []:
            buckets = _turn_buckets(turn)
            if buckets is not None:
                by_harness[harness].append(buckets)

    if not by_harness:
        print("no timed turns found", file=sys.stderr)
        return 1

    header = (
        f"{'harness':<14} {'n':>3} {'wall':>10} {'generation':>11} {'tool':>9} "
        f"{'startup':>9} {'teardown':>9} {'residual':>10} {'%':>7} {'worst turn':>11}"
    )
    print(header)
    print("-" * len(header))
    worst_share = 0.0
    worst_turn = 0.0
    for harness in sorted(by_harness):
        turns = by_harness[harness]
        n = len(turns)
        wall, gen, tool, up, down = (sum(col) / n for col in zip(*turns, strict=True))
        residual = wall - gen - tool - up - down
        share = (residual / wall * 100.0) if wall else 0.0
        # The MEAN residual can hide an outlier by cancellation — the sign
        # flips between harnesses because head/tail are measured between event
        # stamps while duration_seconds is the agent's own monotonic span. So
        # report the worst single turn beside it; that is the real bound.
        per_turn = max(abs(w - g - t - u - d) for w, g, t, u, d in turns)
        worst_share = max(worst_share, abs(share))
        worst_turn = max(worst_turn, per_turn)
        print(
            f"{harness:<14} {n:>3} {wall:>9.1f}ms {gen:>10.1f}ms {tool:>8.1f}ms "
            f"{up:>8.1f}ms {down:>8.1f}ms {residual:>9.3f}ms {share:>6.2f}% {per_turn:>9.3f}ms"
        )
    print(f"\nworst mean |residual| = {worst_share:.2f}% of wall clock")
    print(f"worst single-turn |residual| = {worst_turn:.3f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
