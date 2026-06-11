#!/usr/bin/env python3
"""Build the Slack JSON payload for the latest dashboard run.

Reads ``runs/latest/run.json`` to compute mechanical metrics: pass / fail
/ error counts, wall duration, configured parallelism, repo SHAs. No LLM —
pure data-from-disk.

Prints a JSON object {"text": "..."} on stdout, ready to feed into the
Slack Workflow Builder webhook. Suppresses output (empty stdout) when the
run shouldn't ping the channel — wrapper failure, missing run.json, or
fewer than ``MIN_TASKS_FOR_PING`` tasks recorded (likely a test run /
broken task discovery). The webhook goes to a 300-person channel, so the
bias is heavily toward "stay silent unless we're confident".
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("CODER_EVAL_REPO", "/home/azureuser/uipath/coder_eval"))
RUNS = REPO / "runs"
DASHBOARD_BASE = "https://coder-evalboard.uipath-dev.com/runs"

# A version-shaped token (mirrors coder_eval.utils.looks_like_version, kept
# local since this script is stdlib-only). The aggregator already drops junk,
# but a stale / un-resummarised run.json could still hold a non-version
# cli_version (e.g. an `{"Result": "Success"}` envelope from an older capture);
# guard here so we never re-emit that to the channel. Drift is " | "-joined, so
# accept that shape by validating each piece.
_VERSION_TOKEN = re.compile(r"^v?\d+\.\d+\.\d+\S*$")


def _clean_cli_version(value: object) -> str:
    """Return a version-shaped ``cli_version`` (or its drift-joined pieces), else '?'."""
    if not isinstance(value, str) or not value:
        return "?"
    pieces = [p.strip() for p in value.split("|")]
    good = [p for p in pieces if _VERSION_TOKEN.match(p)]
    return " | ".join(good) if good else "?"


# Minimum tasks_run before we'll post to the channel. Production suite is ~300;
# anything significantly lower likely means broken task discovery, a partial
# run, or an ad-hoc smoke test — none of which the channel needs to see.
MIN_TASKS_FOR_PING = 100

# Pass-rate cutoffs for the traffic-light dot. >= GREEN_PCT reads green;
# < RED_PCT reads red; in between is yellow. "Not green" (< GREEN_PCT) is also
# the cutoff at which we tag the skill's owner in the breakdown — keep the two
# uses keyed off the same constant so they never drift apart.
GREEN_PCT = 85.0
RED_PCT = 50.0

# Per-skill owner mapping, shipped alongside this script and read at runtime.
OWNERS_CSV = Path(__file__).resolve().parent / "skill-test-owners.csv"


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
    """Traffic-light dot. >= GREEN_PCT reads green, < RED_PCT red, else yellow."""
    if pct < RED_PCT:
        return ":red_circle:"
    if pct < GREEN_PCT:
        return ":large_yellow_circle:"
    return ":large_green_circle:"


def load_skill_owners() -> dict[str, str]:
    """Map skill name → owner real name from skill-test-owners.csv.

    The CSV is two columns, ``SkillName,Owner`` — one point-of-contact per
    skill. Returns an empty dict when the file is missing or unreadable so the
    breakdown degrades to no tags rather than failing the ping (the file ships
    with the repo but may lag a fresh checkout). Skills absent from the CSV
    simply get no owner.
    """
    try:
        text = OWNERS_CSV.read_text()
    except OSError:
        return {}
    owners: dict[str, str] = {}
    for row in csv.DictReader(text.splitlines()):
        skill = (row.get("SkillName") or "").strip()
        owner = (row.get("Owner") or "").strip()
        if skill and owner:
            owners[skill] = owner
    return owners


def activation_summary(run_dir: Path | None) -> str:
    """One-line activation score for Slack — average recall across the whole skill
    catalog (a skill with fewer than ``min_prompts`` prompts, or none, counts 0).

    The activation suite is a nested sub-run, so its ``activation`` rollup lives in
    ``<run_dir>/activation/run.json`` (NOT the skills run.json). Returns the empty
    string when activation didn't run, so the caller appends it conditionally.
    """
    if run_dir is None:
        return ""
    try:
        data = json.loads((run_dir / "activation" / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    act = data.get("activation")
    if not act or act.get("score") is None:
        return ""
    pct = act["score"] * 100
    return f"Activation: {pct:.0f}% avg recall across {act.get('denominator')} skills"


def skill_breakdown(run: dict, min_tasks: int = 4) -> str:
    """Format a per-skill leaderboard for Slack.

    Lists every skill with at least ``min_tasks`` tasks in this run, sorted
    worst → best so the channel's eye lands on regressions first. Each row is
    prefixed with a traffic-light dot keyed off pass rate. Non-green skills
    (< GREEN_PCT) are tagged with their owner's real name in plaintext (no
    Slack @mention — we surface the point-of-contact without paging a
    300-person channel). Returns the empty string when no skill qualifies, so
    daily.sh can append unconditionally without producing a dangling header.
    """
    owners = load_skill_owners()
    by_skill: dict[str, list[dict]] = {}
    for task in run.get("task_results") or []:
        skill = derive_skill(task)
        # Activation rows are a classification benchmark, not skill build/operate
        # tasks — they're summarized by the dedicated activation line, not lumped
        # into the per-skill leaderboard as one giant "activation" bucket.
        if skill is None or skill == "activation":
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
        owner = owners.get(skill) if pct < GREEN_PCT else None
        owner_tag = f" — {owner}" if owner else ""
        lines.append(f"{_pct_dot(pct)} {skill}: {passed}/{total} ({pct:.0f}%){owner_tag}")
    return "\n".join(lines)


def configured_parallelism(run: dict) -> int | None:
    """Configured concurrency cap (BatchRunConfig.max_parallel) recorded in run.json.

    Returns None when the field is absent; the formatter omits the parallelism line in that case.
    """
    val = run.get("max_parallel")
    if isinstance(val, int) and val > 0:
        return val
    return None


def build_metrics(cur: dict, suite: str, model: str, backend: str, run_dir: Path | None = None) -> str:
    run_id = cur["run_id"]
    n_run = cur.get("tasks_run", 0)
    n_pass = cur.get("tasks_succeeded", 0)
    n_fail = cur.get("tasks_failed", 0)
    n_err = cur.get("tasks_error", 0)
    skipped = cur.get("skipped_tasks") or []
    n_skip = len(skipped)
    pct = (n_pass / n_run * 100) if n_run else 0.0
    duration = fmt_duration(cur.get("total_duration_seconds") or 0)
    configured = configured_parallelism(cur)
    parallel_str = f" · {configured}x parallel" if configured else ""

    env = cur.get("environment_info") or {}
    coder = (env.get("git_commit") or "?")[:7]
    skills_sha = (env.get("skills_git_commit") or "?")[:7]
    cli = _clean_cli_version(env.get("cli_version"))

    label = " / ".join(filter(None, [model, backend])) or "unknown config"

    fail_total = n_fail + n_err
    skipped_line = f" · :wastebasket: {n_skip} skipped" if n_skip else ""
    lines = [
        f":chart_with_upwards_trend: {suite or 'eval'} suite — {run_id} ({label})",
        f":white_check_mark: {n_pass}/{n_run} passed ({pct:.0f}%) · "
        f":x: {fail_total} failed ({n_fail} fail + {n_err} error){skipped_line}"
        f" · :stopwatch: {duration}{parallel_str}",
        f":package: coder_eval @ {coder} · skills @ {skills_sha} · uip @ {cli}",
        f":bar_chart: {DASHBOARD_BASE}/{run_id}",
    ]
    activation_line = activation_summary(run_dir)
    if activation_line:
        lines.append(activation_line)
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

    # Suppression policy — print nothing (daily.sh skips the curl on empty stdout)
    # when the run shouldn't ping a 300-person channel. Gate on whether the task
    # phase produced usable run.json data, NOT on the wrapper's exit code: once
    # tasks have run and run.json is written, post-analysis flakes (review
    # timeout, upload glitch) should not silence the channel — they happen after
    # the task data is durable on disk. RC != 0 from a pre-task-phase failure
    # (lock conflict, dep install failure, missing creds) leaves no run.json, so
    # the load_run check below still catches those.
    latest_dir = RUNS / "latest"
    if latest_dir.is_symlink() or latest_dir.exists():
        latest_dir = latest_dir.resolve()
    cur = load_run(latest_dir) if latest_dir.exists() else None

    if not cur:
        # No run.json (e.g., TASK_PATTERN single-task smoke that bypasses the
        # dashboard wrapper, or a pre-task wrapper crash). Nothing to summarize.
        return 0

    if (cur.get("tasks_run") or 0) < MIN_TASKS_FOR_PING:
        # Likely a test run or broken task discovery — don't spam the channel.
        return 0

    print(json.dumps({"text": build_metrics(cur, args.suite, args.model, args.backend, run_dir=latest_dir)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
