"""Run coder-eval tests."""

import os
import subprocess
from glob import glob
from pathlib import Path

from .config import CODER_EVAL_DIR


def _env_without_claudecode() -> dict[str, str]:
    """Return a copy of os.environ without CLAUDECODE to avoid nested-session guard."""
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def uip_login(
    *,
    authority: str,
    client_id: str,
    client_secret: str,
    tenant: str,
    scope: str = "OR.Default",
) -> None:
    """Authenticate the UiPath CLI before running flow tasks.

    The secret is passed via the UIP_CLIENT_SECRET environment variable
    and referenced with ``--client-secret env.UIP_CLIENT_SECRET`` so the
    CLI reads it from the env (avoids exposing it in process listings).
    """
    env = {**os.environ, "UIP_CLIENT_SECRET": client_secret}
    subprocess.run(
        [
            "uip",
            "login",
            "--authority",
            authority,
            "--client-id",
            client_id,
            "--client-secret",
            "env.UIP_CLIENT_SECRET",
            "--tenant",
            tenant,
            "--scope",
            scope,
        ],
        env=env,
        check=True,
    )


def pull_coder_eval() -> None:
    subprocess.run(["git", "pull"], cwd=CODER_EVAL_DIR, check=True)


def run_tests(
    *,
    model: str = "claude-sonnet-4-6",
    tags: str | None = "smoke",
    task_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    concurrency: int | None = None,
    experiment: str | None = None,
    extra_env: dict[str, str] | None = None,
    verbose: bool = False,
    backend: str | None = None,
) -> Path:
    """Run coder-eval and return the resolved path of the latest run."""
    if task_patterns is None:
        task_patterns = ["tasks/*.yaml"]

    task_files: list[str] = []
    for pattern in task_patterns:
        task_files.extend(sorted(glob(str(CODER_EVAL_DIR / pattern), recursive=True)))

    if exclude_patterns:
        excluded: set[str] = set()
        for pattern in exclude_patterns:
            excluded.update(glob(str(CODER_EVAL_DIR / pattern), recursive=True))
        task_files = [f for f in task_files if f not in excluded]

    if not task_files:
        raise FileNotFoundError(f"No task YAML files found for patterns {task_patterns}")

    cmd = [
        "uv",
        "run",
        "coder-eval",
        "run",
        *task_files,
        "--model",
        model,
    ]
    if tags:
        cmd.extend(["--tags", tags])
    if concurrency is not None:
        cmd.extend(["-j", str(concurrency)])
    if experiment:
        cmd.extend(["--experiment", experiment])
    if verbose:
        cmd.append("--verbose")
    if backend:
        cmd.extend(["--backend", backend])

    env = _env_without_claudecode()
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(cmd, cwd=CODER_EVAL_DIR, env=env)
    if result.returncode != 0:
        print(f"WARNING: coder-eval exited with code {result.returncode} (some tasks may have failed)")

    latest = (CODER_EVAL_DIR / "runs" / "latest").resolve()
    if not latest.exists():
        raise FileNotFoundError(f"Expected run symlink not found: {latest}")
    return latest
