"""Translate a graded coder-eval run into Harbor's reward-file contract.

Harbor's verifier reads ``/logs/verifier/reward.json`` (a flat ``dict[str, float]``)
or falls back to ``reward.txt`` (a bare float). It does NOT inspect the verifier
script's exit code — only whether the file exists, is non-empty, and parses. A
missing or malformed one raises inside Harbor's own verify step, which its trial
runner records rather than coalescing to a zero-reward object.

This module's whole job is therefore: **write the file, or don't.** An unmeasured row
must not become ``reward=0.0``, and a grading-time infrastructure failure (which
finalizes as ERROR with a score of ``0.0``, not ``None``) is the same case in
disguise.

Rationale: .claude/notes/reporting.md § Write the reward file, or do not
"""

from __future__ import annotations

import json
from pathlib import Path

from coder_eval.orchestration.regrade import RegradeError, load_prior_result
from coder_eval.path_utils import write_text_atomic


class RewardWriteSkippedError(Exception):
    """The run carries no measured verdict — the caller must write no reward file.

    Raised for an ungraded row (``weighted_score is None``, e.g. ``execute``
    left it ``NOT_GRADED``, or the row crashed before grading). Distinct from
    ``RegradeError`` (also propagated by this module) only in *why* nothing
    can be written — both are infrastructure failures with the identical
    "write nothing" contract; keeping them separate types documents which
    failure mode actually occurred without changing what the caller must do.
    """


def compute_reward(run_dir: Path) -> dict[str, float]:
    """Read a graded run's ``task.json`` and derive Harbor's reward dict.

    Reuses ``load_prior_result`` (the same reader ``evaluate <run_dir>`` /
    `run --resume`` use) rather than re-implementing ``task.json`` loading —
    a second reader is how two copies of "what does this run's outcome mean"
    drift into two different verdicts for the same run.

    Raises:
        RegradeError: ``task.json`` is missing or does not parse as an
            ``EvaluationResult`` (propagated from ``load_prior_result`` — an
            infrastructure failure, same "write nothing" contract as below).
        RewardWriteSkippedError: the row's ``weighted_score`` is ``None`` — an
            unmeasured row, never to be coalesced to ``0.0`` — or its
            ``final_status.category`` is ``"error"``, a grading-time
            infrastructure crash that also carries no real measurement despite
            ``calculate_weighted_score`` writing a literal ``0.0`` for it.
    """
    result = load_prior_result(run_dir)
    if result.weighted_score is None or result.final_status.category == "error":
        raise RewardWriteSkippedError(
            f"{run_dir} carries no measured verdict (final_status={result.final_status.value!r}, "
            + f"weighted_score={result.weighted_score!r}); writing no reward file so Harbor's own "
            + "missing-reward path masks the trial instead of scoring it 0.0"
        )
    return {"reward": result.weighted_score}


def write_reward(run_dir: Path, out_path: Path) -> dict[str, float]:
    """Write Harbor's ``reward.json`` at ``out_path``.

    Must never be called when ``compute_reward`` would raise — the caller
    (the ``coder-eval harbor reward`` CLI command) is expected to let
    ``RewardWriteSkippedError`` / ``RegradeError`` propagate uncaught rather than
    catching them here and writing a file anyway.
    """
    rewards = compute_reward(run_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(out_path, json.dumps(rewards, indent=2) + "\n")
    return rewards


__all__ = ["RegradeError", "RewardWriteSkippedError", "compute_reward", "write_reward"]
