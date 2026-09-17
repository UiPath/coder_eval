#!/usr/bin/env python3
"""Harbor end-to-end smoke test.

Exports a handful of coder-eval tasks to Harbor (`coder-eval export --format
harbor`), runs each with `hb run -a coder_eval.harbor.agent:CoderEvalAgent`
against real Docker, and asserts that:

  - the verifier phase wrote `reward.json` with `reward == 1.0`
  - the agent phase wrote a real ATIF `trajectory.json`
  - both the agent and verifier phases left a `task.json` behind

Invoked by `.github/workflows/harbor-e2e.yml` -- deliberately NOT part of
`make test`: it shells out to a real `hb` CLI, real Docker builds, and (for
the llm_judge scenario) a real model call, none of which belong in the fast
unit-test suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = REPO_ROOT / "tmp" / "harbor_e2e"
AGENT_IMPORT_PATH = "coder_eval.harbor.agent:CoderEvalAgent"
# Where a failing scenario's full export/ + jobs/ tree gets zipped for upload --
# a print-statement diagnostic only shows what this script thought to ask for
# (and dies with the runner). A zip preserves everything: docker/agent logs,
# every task.json/trajectory.json, artifacts/ workspaces -- so a failure can be
# inspected after the fact instead of guessed at from stdout.
FAILURE_ARTIFACTS_DIR = REPO_ROOT / "tmp" / "harbor_e2e_failures"


@dataclass(frozen=True)
class Scenario:
    """One (task.yaml, export flags) pair to round-trip through Harbor."""

    name: str
    task_file: Path


SCENARIOS: list[Scenario] = [
    # "tmpdir" baseline: plain docker driver, default coder-eval-agent image,
    # no dockerfile_path / template_sources / llm_judge -- exercises the bare
    # export -> CoderEvalAgent -> --workspace-dir -> verifier round trip.
    Scenario("baseline", REPO_ROOT / "tests/harbor_e2e/fixtures/docker_baseline.yaml"),
    # llm_judge: real model call inside the VERIFIER phase, not just the agent.
    Scenario("llm_judge", REPO_ROOT / "tests/harbor_e2e/fixtures/llm_judge.yaml"),
    # Custom (BYOD) Docker image via dockerfile_path -- reuses the in-tree
    # byod_smoke_test task/image rather than duplicating it.
    Scenario("docker_custom_image", REPO_ROOT / "tasks/byod_smoke_test.yaml"),
    # template_sources: TemplateDirSource copy-in + rewritten path, plus
    # sandbox.python.env_packages surviving the agent-phase task.yaml merge.
    Scenario("template_sources", REPO_ROOT / "tests/harbor_e2e/fixtures/template_sources.yaml"),
    # command_executed: catches a regression where the verifier phase grades
    # against a directory with no trajectory data -- reward could still land on
    # 1.0 "by luck" from unrelated criteria while this one silently scores 0.0,
    # so `assert_scenario_artifacts` checks its own criterion score directly
    # rather than trusting the aggregate reward alone.
    Scenario("trajectory_criteria", REPO_ROOT / "tests/harbor_e2e/fixtures/trajectory_criteria.yaml"),
]

# Criteria types that can only score correctly if the verifier phase actually
# hydrated the agent phase's trajectory (see portability.py's NEEDS_TRAJECTORY
# class). Checked by name per scenario below rather than globally, since only
# `trajectory_criteria` declares one.
_TRAJECTORY_CRITERION_TYPES = frozenset({"command_executed", "commands_efficiency", "skill_triggered"})


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def export_task(scenario: Scenario, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    cmd = ["coder-eval", "export", str(scenario.task_file), "-o", str(out_dir)]
    result = _run(cmd)
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"[{scenario.name}] `coder-eval export` failed (exit {result.returncode})")


def run_harbor(scenario: Scenario, export_dir: Path, jobs_dir: Path) -> Path:
    if jobs_dir.exists():
        shutil.rmtree(jobs_dir)
    cmd = [
        "hb",
        "run",
        "-p",
        str(export_dir),
        "-a",
        AGENT_IMPORT_PATH,
        "--jobs-dir",
        str(jobs_dir),
        "-n",
        "1",
        "-y",
    ]
    result = _run(cmd)
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"[{scenario.name}] `hb run` failed (exit {result.returncode})")

    # <jobs_dir>/<job_timestamp>/<trial_name>/{agent,verifier}/... -- glob for
    # the one directory two levels down that actually holds a verifier/ output,
    # rather than assuming a fixed trial-name shape Harbor doesn't guarantee.
    trial_dirs = [d for d in jobs_dir.glob("*/*/") if (d / "verifier").is_dir()]
    if len(trial_dirs) != 1:
        raise RuntimeError(
            f"[{scenario.name}] expected exactly one trial directory under {jobs_dir}, found {len(trial_dirs)}"
        )
    return trial_dirs[0]


def assert_scenario_artifacts(scenario: Scenario, trial_dir: Path) -> None:
    reward_path = trial_dir / "verifier" / "reward.json"
    if not reward_path.is_file():
        raise RuntimeError(f"[{scenario.name}] missing {reward_path}")
    reward = json.loads(reward_path.read_text(encoding="utf-8"))

    # Read the per-criterion breakdown BEFORE the reward gate below, so a
    # failing reward's own root cause (which criterion, and why) is always in
    # the failure message -- not only when an unrelated criterion happens to
    # carry the aggregate to 1.0 "by luck" while this one silently failed.
    verifier_task_json = trial_dir / "verifier" / "task.json"
    criteria_detail = None
    if verifier_task_json.is_file():
        verifier_result = json.loads(verifier_task_json.read_text(encoding="utf-8"))
        criteria_detail = [
            {"type": r.get("criterion_type"), "score": r.get("score"), "details": r.get("details")}
            for r in verifier_result.get("success_criteria_results", [])
        ]

    if reward.get("reward") != 1.0:
        # Read the AGENT phase's own task.json directly -- its `iterations[].commands`
        # is the raw telemetry the verifier's `command_executed` etc. are supposed to
        # hydrate from. Dumped unconditionally on failure so a criterion that scored
        # 0.0 because NO commands were ever recorded (a hydration/telemetry bug) is
        # distinguishable at a glance from one that scored 0.0 because the recorded
        # commands just didn't match the pattern (an agent/fixture-wording issue).
        agent_task_jsons = sorted((trial_dir / "agent").glob("**/task.json"))
        agent_commands: list[dict[str, object]] | str = "no agent/task.json found"
        if agent_task_jsons:
            agent_result = json.loads(agent_task_jsons[0].read_text(encoding="utf-8"))
            agent_commands = [
                {"tool_name": c.get("tool_name"), "parameters": c.get("parameters")}
                for it in agent_result.get("iterations", [])
                for c in it.get("commands", [])
            ]
        raise RuntimeError(
            f"[{scenario.name}] expected reward 1.0, got {reward!r} ({reward_path}); "
            + f"criteria: {criteria_detail!r}; agent-phase recorded commands: {agent_commands!r}"
        )

    trajectory_path = trial_dir / "agent" / "trajectory.json"
    if not trajectory_path.is_file():
        raise RuntimeError(f"[{scenario.name}] missing {trajectory_path}")
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    if "schema_version" not in trajectory:
        raise RuntimeError(f"[{scenario.name}] {trajectory_path} is missing 'schema_version'")

    agent_task_jsons = list((trial_dir / "agent").glob("**/task.json"))
    if not agent_task_jsons:
        raise RuntimeError(f"[{scenario.name}] no task.json found under {trial_dir / 'agent'}")
    verifier_task_json = trial_dir / "verifier" / "task.json"
    if not verifier_task_json.is_file():
        raise RuntimeError(f"[{scenario.name}] missing {verifier_task_json}")

    # An overall reward of 1.0 does not prove a trajectory criterion was
    # actually graded -- it could pass "by luck" from unrelated criteria while
    # this one silently scored 0.0 against an ungraded/empty trajectory. Check
    # each trajectory-dependent criterion's OWN score directly.
    verifier_result = json.loads(verifier_task_json.read_text(encoding="utf-8"))
    trajectory_results = [
        r
        for r in verifier_result.get("success_criteria_results", [])
        if r.get("criterion_type") in _TRAJECTORY_CRITERION_TYPES
    ]
    for r in trajectory_results:
        if r.get("score") != 1.0:
            raise RuntimeError(
                f"[{scenario.name}] {r.get('criterion_type')} criterion did not score 1.0 "
                + f"(got {r.get('score')!r}); trajectory hydration likely broken: {r.get('details')!r}"
            )

    print(
        f"[{scenario.name}] OK: reward=1.0, trajectory.json present, "
        + f"{len(agent_task_jsons)} agent task.json + verifier/task.json present"
        + (f", {len(trajectory_results)} trajectory criterion/criteria verified" if trajectory_results else "")
    )


def _zip_scenario_dir(scenario: Scenario) -> Path | None:
    """Zip a failed scenario's whole ``export/`` + ``jobs/`` tree for upload.

    Best-effort: a zip failure must never mask the real scenario failure it was
    trying to preserve evidence for.
    """
    scenario_dir = WORK_DIR / scenario.name
    if not scenario_dir.is_dir():
        return None
    FAILURE_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        archive = shutil.make_archive(str(FAILURE_ARTIFACTS_DIR / scenario.name), "zip", root_dir=str(scenario_dir))
    except OSError as exc:
        print(f"[{scenario.name}] could not zip {scenario_dir} for upload: {exc}", file=sys.stderr)
        return None
    return Path(archive)


def main() -> int:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for scenario in SCENARIOS:
        export_dir = WORK_DIR / scenario.name / "export"
        jobs_dir = WORK_DIR / scenario.name / "jobs"
        print(f"\n=== {scenario.name} ===", flush=True)
        try:
            export_task(scenario, export_dir)
            trial_dir = run_harbor(scenario, export_dir, jobs_dir)
            assert_scenario_artifacts(scenario, trial_dir)
        except Exception as exc:
            print(f"[{scenario.name}] FAILED: {exc}", file=sys.stderr)
            failures.append(scenario.name)
            archive = _zip_scenario_dir(scenario)
            if archive is not None:
                print(f"[{scenario.name}] full export/+jobs/ tree zipped to {archive} for upload", file=sys.stderr)

    print("\n=== Summary ===")
    for scenario in SCENARIOS:
        print(f"  {scenario.name}: {'FAILED' if scenario.name in failures else 'OK'}")

    if failures:
        print(f"\n{len(failures)}/{len(SCENARIOS)} scenario(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
