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
import re
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


_TASK_PATH_SKILL_RE = re.compile(r"(?:^|/)tasks/([^/]+)/")


def derive_skill(task: dict) -> str | None:
    """Resolve the skill (primary group) for a task result.

    Mirrors evalboard's deriveSkill: prefer task_path's "tasks/<skill>/"
    segment (persisted starting with the task_path PR); fall back to the
    first "activation" or "uipath-*" tag for older runs. Returns None when
    neither signal yields anything — callers should drop the task from the
    skill breakdown rather than bucketing it under a fake group.
    """
    tp = task.get("task_path")
    if isinstance(tp, str) and tp:
        m = _TASK_PATH_SKILL_RE.search(tp)
        if m:
            return m.group(1)
    for tag in task.get("tags") or []:
        if not isinstance(tag, str):
            continue
        if tag == "activation" or tag.startswith("uipath-"):
            return tag
    return None


def _pct_dot(pct: float) -> str:
    """Traffic-light dot. Anything >= 85% reads green, <50% red, else yellow."""
    if pct < 50:
        return ":red_circle:"
    if pct < 85:
        return ":large_yellow_circle:"
    return ":large_green_circle:"


def skill_breakdown(run: dict, min_tasks: int = 4) -> str:
    """Format a per-skill leaderboard for Slack.

    Lists every skill with at least ``min_tasks`` tasks in this run, sorted
    worst → best so the channel's eye lands on regressions first. Each row is
    prefixed with a traffic-light dot keyed off pass rate. Returns the empty
    string when no skill qualifies, so daily.sh can append unconditionally
    without producing a dangling header.
    """
    by_skill: dict[str, list[dict]] = {}
    for task in run.get("task_results") or []:
        skill = derive_skill(task)
        if skill is None:
            continue
        by_skill.setdefault(skill, []).append(task)

    rows: list[tuple[str, int, int, float]] = []  # (skill, passed, total, pct)
    for skill, tasks in by_skill.items():
        total = len(tasks)
        if total < min_tasks:
            continue
        passed = sum(1 for t in tasks if t.get("status") == "SUCCESS")
        rows.append((skill, passed, total, passed / total * 100))

    if not rows:
        return ""

    rows.sort(key=lambda r: (r[3], -r[2], r[0]))
    lines = [f":dart: Skills (>={min_tasks} tasks, worst → best):"]
    for skill, passed, total, pct in rows:
        lines.append(f"{_pct_dot(pct)} {skill}: {passed}/{total} ({pct:.0f}%)")
    return "\n".join(lines)


def configured_parallelism(run: dict) -> int | None:
    """Configured concurrency cap (BatchRunConfig.max_parallel) recorded in run.json.

    Returns None when the field is absent; the formatter omits the parallelism line in that case.
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
    skipped = cur.get("skipped_tasks") or []
    n_skip = len(skipped)
    pct = (n_pass / n_run * 100) if n_run else 0.0
    duration = fmt_duration(cur.get("total_duration_seconds") or 0)
    cost = total_cost(cur)
    configured = configured_parallelism(cur)
    parallel_str = f" · {configured}x parallel" if configured else ""

    env = cur.get("environment_info") or {}
    coder = (env.get("git_commit") or "?")[:7]
    skills_sha = (env.get("skills_git_commit") or "?")[:7]
    cli = env.get("cli_version") or "?"

    label = " / ".join(filter(None, [model, backend])) or "unknown config"

    fail_total = n_fail + n_err
    skipped_line = f" · :wastebasket: {n_skip} skipped" if n_skip else ""
    lines = [
        f":chart_with_upwards_trend: {suite or 'eval'} suite — {run_id} ({label})",
        f":white_check_mark: {n_pass}/{n_run} passed ({pct:.0f}%) · "
        f":x: {fail_total} failed ({n_fail} fail + {n_err} error){skipped_line}",
        f":moneybag: ${cost:.2f} · :stopwatch: {duration}{parallel_str}",
        f":package: coder_eval @ {coder} · skills @ {skills_sha} · uip @ {cli}",
        f":bar_chart: {DASHBOARD_BASE}/{run_id}",
    ]
    skills_section = skill_breakdown(cur)
    if skills_section:
        lines.append(skills_section)
    return "\n".join(lines)


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
