"""Generate AI analysis of a coder-eval run via Claude Code."""

import subprocess
from pathlib import Path

from .config import CODER_EVAL_DIR
from .run import _env_without_claudecode


def generate_analysis(run_path: Path) -> Path:
    """Invoke /coder-eval-run-analysis on a completed run directory.

    The skill is defined in the coder_eval repo at
    .claude/commands/coder-eval-run-analysis.md and writes analysis.md
    into the target run directory.

    Returns the path to the generated analysis.md file.
    """
    analysis_path = run_path / "analysis.md"

    # The skill expects a path relative to the coder_eval repo root.
    try:
        rel_path = run_path.relative_to(CODER_EVAL_DIR)
    except ValueError:
        rel_path = run_path

    subprocess.run(
        [
            "claude",
            "--print",
            "--permission-mode",
            "bypassPermissions",
            f"/coder-eval-run-analysis {rel_path}",
        ],
        cwd=str(CODER_EVAL_DIR),
        env=_env_without_claudecode(),
        check=True,
        timeout=1800,
    )

    if not analysis_path.exists():
        raise FileNotFoundError(f"Analysis was not generated at {analysis_path}")

    return analysis_path
