#!/usr/bin/env python3
"""Build the Slack JSON payload for the latest dashboard run.

Reads ``runs/latest/run.json`` to compute mechanical metrics: pass / fail
/ error counts, total cost, wall duration, configured parallelism, repo
SHAs. No LLM — pure data-from-disk.

Prints a JSON object {"text": "..."} on stdout, ready to feed into the
Slack Workflow Builder webhook. On missing run.json the message degrades
gracefully to a one-liner so daily.sh can still notify on hard failures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("CODER_EVAL_REPO", "/home/azureuser/uipath/coder_eval"))
RUNS = REPO / "runs"
DASHBOARD_BASE = "https://coder-evalboard.uipath-dev.com/runs"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def load_run(run_dir: Path) -> dict | None:
    try:
        return json.loads((run_dir / "run.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def total_cost(run: dict) -> float:
    return sum(float(t.get("total_cost_usd") or 0) for t in run.get("task_results", []))


def configured_parallelism(run: dict) -> int | None:
    """Configured concurrency cap (BatchRunConfig.max_parallel) recorded in run.json.

    Returns None for older runs that pre-date the field; the formatter omits the
    parallelism line in that case.
    """
    val = run.get("max_parallel")
    if isinstance(val, int) and val > 0:
        return val
    return None


def build_metrics(cur: dict, suite: str, model: str, backend: str) -> str:
    run_id = cur["run_id"]
    n_run = cur.get("tasks_run", 0)
    n_pass = cur.get("tasks_succeeded", 0)
    n_fail = cur.get("tasks_failed", 0)
    n_err = cur.get("tasks_error", 0)
    pct = (n_pass / n_run * 100) if n_run else 0.0
    duration = fmt_duration(cur.get("total_duration_seconds") or 0)
    cost = total_cost(cur)
    configured = configured_parallelism(cur)
    parallel_str = f" · {configured}x parallel" if configured else ""

    env = cur.get("environment_info") or {}
    coder = (env.get("git_commit") or "?")[:7]
    skills = (env.get("skills_git_commit") or "?")[:7]
    cli = (env.get("cli_git_commit") or "?")[:7]

    label = " / ".join(filter(None, [model, backend])) or "unknown config"

    fail_total = n_fail + n_err
    return "\n".join(
        [
            f":chart_with_upwards_trend: {suite or 'eval'} suite — {run_id} ({label})",
            f":white_check_mark: {n_pass}/{n_run} passed ({pct:.0f}%) · "
            f":x: {fail_total} failed ({n_fail} fail + {n_err} error)",
            f":moneybag: ${cost:.2f} · :stopwatch: {duration}{parallel_str}",
            f":package: coder_eval @ {coder} · skills @ {skills} · cli @ {cli}",
            f":bar_chart: {DASHBOARD_BASE}/{run_id}",
        ]
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rc", type=int, required=True, help="dashboard exit code")
    p.add_argument("--log", default="", help="path to wrapper log file")
    p.add_argument("--suite", default=os.environ.get("SUITE", ""))
    p.add_argument("--model", default=os.environ.get("MODEL", ""))
    p.add_argument("--backend", default=os.environ.get("BACKEND", ""))
    args = p.parse_args()

    latest_dir = RUNS / "latest"
    if latest_dir.is_symlink() or latest_dir.exists():
        latest_dir = latest_dir.resolve()
    cur = load_run(latest_dir) if latest_dir.exists() else None

    if args.rc == 75:
        text = f":warning: skipped (lock held — concurrent uip on the box) · suite={args.suite}"
    elif args.rc != 0 and not cur:
        text = f":x: run failed before producing run.json (exit {args.rc}) · suite={args.suite} · log: {args.log}"
    elif cur:
        text = build_metrics(cur, args.suite, args.model, args.backend)
        if args.rc != 0:
            text = (
                f":warning: dashboard exited {args.rc} (some tasks may have failed) — "
                f"metrics from latest run.json:\n" + text
            )
    else:
        text = f":question: unknown state (exit {args.rc}, no run.json) · log: {args.log}"

    print(json.dumps({"text": text}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
