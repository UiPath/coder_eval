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


@dataclass(frozen=True)
class Scenario:
    """One (task.yaml, export flags) pair to round-trip through Harbor."""

    name: str
    task_file: Path
    allow_credentials: bool = False


SCENARIOS: list[Scenario] = [
    # "tmpdir" baseline: plain docker driver, default coder-eval-agent image,
    # no dockerfile_path / template_sources / llm_judge -- exercises the bare
    # export -> CoderEvalAgent -> --workspace-dir -> verifier round trip.
    Scenario("baseline", REPO_ROOT / "tests/harbor_e2e/fixtures/docker_baseline.yaml"),
    # llm_judge: real model call inside the VERIFIER phase, not just the agent.
    Scenario("llm_judge", REPO_ROOT / "tests/harbor_e2e/fixtures/llm_judge.yaml", allow_credentials=True),
    # Custom (BYOD) Docker image via dockerfile_path -- reuses the in-tree
    # byod_smoke_test task/image rather than duplicating it.
    Scenario("docker_custom_image", REPO_ROOT / "tasks/byod_smoke_test.yaml"),
    # template_sources: TemplateDirSource copy-in + rewritten path, plus
    # sandbox.python.env_packages surviving the agent-phase task.yaml merge.
    Scenario("template_sources", REPO_ROOT / "tests/harbor_e2e/fixtures/template_sources.yaml"),
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def export_task(scenario: Scenario, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    cmd = ["coder-eval", "export", str(scenario.task_file), "-o", str(out_dir)]
    if scenario.allow_credentials:
        cmd.append("--allow-credentials")
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
    if reward.get("reward") != 1.0:
        raise RuntimeError(f"[{scenario.name}] expected reward 1.0, got {reward!r} ({reward_path})")

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

    print(
        f"[{scenario.name}] OK: reward=1.0, trajectory.json present, "
        + f"{len(agent_task_jsons)} agent task.json + verifier/task.json present"
    )


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

    print("\n=== Summary ===")
    for scenario in SCENARIOS:
        print(f"  {scenario.name}: {'FAILED' if scenario.name in failures else 'OK'}")

    if failures:
        print(f"\n{len(failures)}/{len(SCENARIOS)} scenario(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
