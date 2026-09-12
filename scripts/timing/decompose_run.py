#!/usr/bin/env python3
"""Decompose recorded turns into the four wall-clock buckets, per harness.

Reads `task.json` files, groups their turns by `agent_type`, and prints the
mean generation / tool / startup / teardown against the mean turn duration,
plus the residual as a percentage of wall clock. A healthy harness reconciles
to well under 1%. The tool bucket is the UNION of the command intervals, never
their sum — tool calls overlap, and summing them books the overlap twice.

    uv run python scripts/timing/decompose_run.py runs/<run>/default/*/00/task.json

Pass `--max-residual-pct` to turn the report into a GATE: a non-zero exit when
any single turn's |residual| exceeds that share of its own wall clock. The gate
is deliberately TWO-SIDED, because the only other sensor for this identity is
not. `tests/_fixtures/golden_streams/_scrub.py` asserts `overshoot <= ...`,
which catches a bucket that claims MORE time than the turn contains and says
nothing at all about a bucket that claims less — so an unmeasured bucket, the
exact defect this file exists to find, passes every test in the suite. Gating
on `abs(share)` covers both signs.

Not wired into `make`: it needs live runs, not fixtures. NOTE `scripts/` is
outside the Makefile's LINT_PATHS, so this file is neither formatted nor
ruff-checked — keep it small and dependency-free (stdlib plus the shared
timing helpers and `TurnRecord`, so the union rule has a single definition).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from coder_eval.models import TurnRecord
from coder_eval.timing import main_thread_tool_spans, union_ms


# A stored `tool_union_ms` and the union computed here should be the same
# number; JSON round-tripping is the only slack, so the tolerance is absolute
# and tiny.
_UNION_TOLERANCE_MS = 1e-6


def _tool_ms(turn: dict) -> float:
    """Wall ms this turn's MAIN-THREAD tools occupied — the UNION, not the sum.

    Validates the raw dict into a `TurnRecord` and calls the SAME typed
    selector the collector uses, rather than reimplementing the selection rule
    (which commands count, the sub-agent exclusion, the stamp parse, the
    `end >= start` filter) over dicts. Three copies of that rule existed and
    they agreed only because someone kept checking; Pi resolved a `Write` and a
    `Bash` overlapping by 18.4 ms in one measured turn, and a selector that
    disagreed with the collector's would report a residual that is an artifact
    of the disagreement rather than a bucket error. This is the only two-sided
    live sensor for the identity, so that is the worst place for a copy.

    What is shared is the SELECTION and `union_ms`. What is NOT shared is the
    bookkeeping around them — this still builds its own span set and computes
    its own union, so it stays a sensor rather than a restatement of the
    producer's answer. Do not "simplify" it into reading `tool_union_ms`; the
    cross-check below is how that field is verified, not how this is computed.
    """
    record = TurnRecord.model_validate(turn)
    return union_ms(main_thread_tool_spans(record.messages, record.commands))


def _union_breach(turn: dict) -> str | None:
    """The stored `tool_union_ms` disagrees with what we just computed, if so.

    Skips when the field is absent: `decompose_run.py` reads corpora recorded
    before `TurnRecord` carried it, so that is the common case here rather than
    an exotic one. A DISAGREEMENT is its own named breach, distinct from a
    residual breach — a residual says the buckets do not tile the turn, this
    says the producer and an independent recomputation of one bucket do not
    agree about its value, which is a different fault with a different fix.
    """
    stored = turn.get("tool_union_ms")
    if not isinstance(stored, (int, float)):
        return None
    computed = _tool_ms(turn)
    if abs(stored - computed) <= _UNION_TOLERANCE_MS:
        return None
    return (
        f"tool_union_ms={stored:.6f}ms but this turn's main-thread command spans union to "
        f"{computed:.6f}ms (off by {stored - computed:+.6f}ms)"
    )


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
    # MAIN THREAD ONLY. A sub-agent's generations bubble into the same stream
    # tagged with the spawning Agent call's tool_use_id, and that call's own
    # interval already spans the sub-agent's entire run. Counting both books the
    # sub-agent twice — the evalboard's timeline strip filters on exactly this
    # field for exactly this reason (a 120 s Agent call containing 90 s of
    # sub-agent generation drove its residual to -57%).
    messages = turn.get("messages") or []
    generation_ms = sum(
        m.get("generation_duration_ms") or 0.0
        for m in messages
        if m.get("role") == "assistant"
        and m.get("parent_tool_use_id") is None
        and isinstance(m.get("generation_duration_ms"), (int, float))
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


def _residual_ms(buckets: tuple[float, float, float, float, float]) -> float:
    wall, gen, tool, up, down = buckets
    return wall - gen - tool - up - down


def _never_measured(turn: dict) -> bool:
    """True when the collector recorded NO generation window for this turn.

    Both head and tail `None` is how `EventCollector` says a turn had nothing
    measurable — `_scrub.py` asserts exactly that pairing. There is nothing to
    reconcile against, so a 100% residual here is an artifact of the absence,
    not a bucket the harness failed to fill. One of the two set and the other
    `None` is the opposite case and is NOT skipped: that IS a real unmeasured
    bucket, and `_turn_buckets` counts it as 0.0 so it surfaces as residual.
    """
    return turn.get("harness_startup_ms") is None and turn.get("harness_teardown_ms") is None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_json", nargs="+", type=Path, help="task.json files to decompose")
    parser.add_argument(
        "--max-residual-pct",
        type=float,
        default=None,
        help="fail (exit 1) if any gateable turn's |residual| exceeds this share of its own wall clock",
    )
    parser.add_argument(
        "--min-turn-ms",
        type=float,
        default=1000.0,
        help="turns shorter than this are excluded from the share columns and the gate (default 1000)",
    )
    parser.add_argument(
        "--include-crashed",
        action="store_true",
        help="keep crashed and never-measured turns instead of skipping them",
    )
    args = parser.parse_args(argv)

    # (path, turn_index, buckets) rather than bare buckets: a breach that cannot
    # name the file it came from is a gate nobody can act on, and a gate nobody
    # can act on gets muted.
    by_harness: dict[str, list[tuple[Path, int, tuple[float, float, float, float, float]]]] = defaultdict(list)
    skipped_crashed = 0
    skipped_no_window = 0
    # A turn `_turn_buckets` cannot place on the timeline at all (no numeric
    # `duration_seconds`). Counted rather than silently dropped, for the same
    # reason as the two above: an exclusion nobody can see understates how much
    # of the corpus the gate actually looked at.
    skipped_untimed = 0
    # A turn whose STORED tool bucket disagrees with the union recomputed here.
    # Its own list, not folded into the residual breaches: a residual says the
    # buckets do not tile the turn; this says the producer and an independent
    # recomputation of one bucket disagree about its value.
    union_breaches: list[tuple[str, Path, int, str]] = []
    invalid: list[tuple[Path, int, int]] = []
    for path in args.task_json:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        harness = record.get("agent_type") or "unknown"
        for index, turn in enumerate(record.get("iterations") or []):
            # Filter on the TURN, never on the record's `final_status`. The
            # orchestrator preserves a crashed partial TurnRecord across a
            # retry, so a SUCCESS record can hold a crashed turn; and a
            # `coder-eval execute` corpus finalizes EVERY row as NOT_GRADED,
            # which says nothing about timing — a status allowlist would skip
            # all of it and then report a clean gate over nothing measured.
            #
            # The two conditions are counted INDEPENDENTLY and a turn matching
            # both is counted under each. Short-circuiting on the first would
            # leave the second's tally reading 0 on the only corpus that
            # contains it — the measured case: all three excluded turns are
            # crashed and two of them are also never-measured — which reads as
            # a filter that never fires rather than one with no evidence.
            crashed = turn.get("crashed") is True
            no_window = _never_measured(turn)
            if not args.include_crashed and (crashed or no_window):
                skipped_crashed += int(crashed)
                skipped_no_window += int(no_window)
                continue
            try:
                buckets = _turn_buckets(turn)
            except ValidationError as exc:
                # A real record failing TurnRecord validation is a FINDING, not
                # a nuisance: measured across the whole run history on disk,
                # 2466 of 2466 turns validated. Name it and move on rather than
                # falling back to dict access, which is the second selection
                # path this script just removed.
                invalid.append((path, index, exc.error_count()))
                continue
            if buckets is None:
                skipped_untimed += 1
                continue
            breach = _union_breach(turn)
            if breach is not None:
                union_breaches.append((harness, path, index, breach))
            by_harness[harness].append((path, index, buckets))

    if not by_harness:
        print("no timed turns found", file=sys.stderr)
        return 1

    header = (
        f"{'harness':<14} {'n':>3} {'wall':>10} {'generation':>11} {'tool':>9} "
        f"{'startup':>9} {'teardown':>9} {'residual':>10} {'%':>7} {'worst turn':>11} "
        f"{'gated':>6} {'med|%|':>7} {'worst|%|':>9}"
    )
    print(header)
    print("-" * len(header))
    worst_share = 0.0
    worst_turn = 0.0
    skipped_short = 0
    breaches: list[tuple[str, float, float, float, Path, int]] = []
    gateable_total = 0
    for harness in sorted(by_harness):
        rows = by_harness[harness]
        turns = [buckets for _, _, buckets in rows]
        n = len(turns)
        wall, gen, tool, up, down = (sum(col) / n for col in zip(*turns, strict=True))
        residual = wall - gen - tool - up - down
        share = (residual / wall * 100.0) if wall else 0.0
        # The MEAN residual can hide an outlier by cancellation — the sign
        # flips between harnesses because head/tail are measured between event
        # stamps while duration_seconds is the agent's own monotonic span. So
        # report the worst single turn beside it; that is the real bound.
        per_turn = max(abs(_residual_ms(buckets)) for buckets in turns)
        worst_share = max(worst_share, abs(share))
        worst_turn = max(worst_turn, per_turn)

        # Per-turn |residual| as a share of that turn's OWN wall clock. A 30 ms
        # turn with a 5 ms residual is not a 17% defect, so short turns are out
        # of the share columns and out of the gate — but they stay in the means
        # above, where their absolute contribution is honest and tiny.
        # `wall_ms <= 0` is guarded here and not in `_turn_buckets`, which
        # returns a real tuple for a literal 0 duration.
        shares: list[tuple[float, Path, int, tuple[float, float, float, float, float]]] = []
        for path, index, buckets in rows:
            turn_wall = buckets[0]
            if turn_wall <= 0 or turn_wall < args.min_turn_ms:
                skipped_short += 1
                continue
            shares.append((abs(_residual_ms(buckets)) / turn_wall * 100.0, path, index, buckets))
        gateable_total += len(shares)
        if shares:
            median_share = statistics.median(s for s, _, _, _ in shares)
            worst_row = max(shares, key=lambda row: row[0])
            med_col = f"{median_share:>6.3f}%"
            worst_col = f"{worst_row[0]:>8.3f}%"
        else:
            med_col = f"{'—':>7}"
            worst_col = f"{'—':>9}"
        if args.max_residual_pct is not None:
            for turn_share, path, index, buckets in shares:
                if turn_share > args.max_residual_pct:
                    breaches.append((harness, turn_share, _residual_ms(buckets), buckets[0], path, index))

        print(
            f"{harness:<14} {n:>3} {wall:>9.1f}ms {gen:>10.1f}ms {tool:>8.1f}ms "
            f"{up:>8.1f}ms {down:>8.1f}ms {residual:>9.3f}ms {share:>6.2f}% {per_turn:>9.3f}ms "
            f"{len(shares):>6} {med_col} {worst_col}"
        )
    print(f"\nworst mean |residual| = {worst_share:.2f}% of wall clock")
    print(f"worst single-turn |residual| = {worst_turn:.3f}ms")
    print(
        f"skipped: {skipped_crashed} crashed, {skipped_no_window} no-window "
        f"(a turn can be both), {skipped_untimed} untimed, "
        f"{skipped_short} short (< {args.min_turn_ms:.0f}ms)"
    )

    if invalid:
        print(f"\n{len(invalid)} turn(s) failed TurnRecord validation:", file=sys.stderr)
        for path, index, count in invalid:
            print(f"  {path} turn {index}: {count} error(s)", file=sys.stderr)
    if union_breaches:
        print(f"\nSTORED TOOL UNION disagrees on {len(union_breaches)} turn(s):", file=sys.stderr)
        for harness, path, index, detail in union_breaches:
            print(f"  {harness:<14} {path} turn {index}: {detail}", file=sys.stderr)

    if not gateable_total:
        # A gate that passes because it measured nothing is the exact failure
        # this script exists to remove, so it only passes when none was asked for.
        print("no gateable turns", file=sys.stderr)
        return 1 if args.max_residual_pct is not None else 0

    if union_breaches or invalid:
        # Independent of --max-residual-pct: neither is a residual question, and
        # a disagreement about a stored bucket is exactly what a gate is for.
        return 1
    if args.max_residual_pct is None:
        return 0
    if not breaches:
        print(f"\ngate OK: every one of {gateable_total} gateable turns is within {args.max_residual_pct}%")
        return 0
    print(f"\ngate FAILED: {len(breaches)} turn(s) over {args.max_residual_pct}% of wall clock", file=sys.stderr)
    for harness, turn_share, residual_ms, turn_wall, path, index in sorted(breaches, key=lambda b: -b[1]):
        print(
            f"  {harness:<14} {turn_share:>8.3f}%  residual {residual_ms:>10.3f}ms  "
            f"wall {turn_wall:>10.1f}ms  {path} turn {index}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
