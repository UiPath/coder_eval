"""``coder-eval harbor`` — Harbor framework adherence commands.

Distinct from task *definition* (``coder-eval export --format harbor``,
tracked separately): this namespace is for what an outer harness's runtime
contract expects — right now, the verifier's reward file. See
``coder_eval.harbor`` for the shared logic and ``tmp/harborframework.md`` for
the design this implements (C1.1).
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..harbor.reward import RegradeError, RewardWriteSkippedError, write_reward
from .console import console


harbor_app = typer.Typer(
    name="harbor",
    help="Harbor framework adherence commands (reward reporting, log-layout shims).",
    add_completion=False,
)


def reward_command(
    run_dir: Path = typer.Argument(  # noqa: B008
        ...,
        help="A graded coder-eval run directory (holds task.json) — typically the "
        + "--run-dir passed to the preceding `coder-eval evaluate`.",
        exists=True,
        file_okay=False,
    ),
    out: Path = typer.Option(  # noqa: B008
        ...,
        "--out",
        help="Where to write Harbor's reward file, e.g. /logs/verifier/reward.json.",
    ),
) -> None:
    """Translate a graded run's ``task.json`` into Harbor's ``reward.json`` contract.

    Writes NOTHING and exits non-zero when the row carries no measured verdict
    (``weighted_score`` is ``None`` — an ungraded or crashed row) or when
    ``task.json`` itself is missing/unparseable. That is deliberate: Harbor's
    own verifier already treats a missing reward file as an infrastructure
    failure distinct from a measured zero (see ``coder_eval.harbor.reward``),
    so writing ``{"reward": 0.0}`` here would silently convert "never
    measured" into "measured and scored zero" — the exact defect CE049 guards
    against one layer up, in this same shape at the artifact boundary.

    Typical use, from a Harbor task's ``tests/test.sh``:

        coder-eval evaluate tests/task.yaml "$WORKDIR" --in-place --run-dir /logs/verifier
        coder-eval harbor reward /logs/verifier --out /logs/verifier/reward.json
    """
    try:
        rewards = write_reward(run_dir, out)
    except (RewardWriteSkippedError, RegradeError) as e:
        console.print(f"[yellow]⚠[/] {e}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]✓[/] wrote {out}: {rewards}")


__all__ = ["harbor_app", "reward_command"]
