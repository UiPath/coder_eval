"""Translate a graded coder-eval run into Harbor's reward-file contract.

Harbor's verifier (``harbor 0.22.0``, verified against source — see
``tmp/harborframework.md`` § C0) reads ``/logs/verifier/reward.json`` (a flat
``dict[str, float]``) or falls back to ``/logs/verifier/reward.txt`` (a bare
float, synthesized into the single key ``"reward"``). It does NOT inspect the
verifier script's exit code — only whether the reward file exists, is
non-empty, and parses. A missing/empty/malformed file raises inside Harbor's
own ``Verifier.verify()`` (``RewardFileNotFoundError`` /
``RewardFileEmptyError`` / ``VerifierOutputParseError``), which Harbor's
``Trial.run()`` catches: it records the exception on ``TrialResult`` and
leaves ``TrialResult.verifier_result`` at its default ``None`` — never
coalesced to a zero-reward object. That is Harbor's own infra-vs-policy split,
already built.

This module's whole job, therefore, is: **write the file, or don't.**

The "don't" case is not a degenerate corner — it is the load-bearing one. An
unmeasured row (``weighted_score is None`` — an ungraded row, or a task.json
that failed to load at all) must not become ``reward=0.0``: that would train
"the agent's behaviour was bad" from a measurement that never happened. Not
writing the file lets Harbor's own missing-reward path mask the trial
instead — the same principle as CE049 (never coalesce a possibly-unmeasured
score to a numeric literal), one level up, at the artifact-writing boundary
rather than the in-process one.

A grading-time INFRASTRUCTURE failure is the same case in disguise:
``EvaluationResult.calculate_weighted_score`` (``models/results.py``)
short-circuits an empty ``success_criteria_results`` list to a hard ``0.0``,
not ``None`` -- so a checker that raises ``JudgeInfrastructureError`` /
``CheckerMisuseError`` / ``ReferenceTamperedError`` (escalating exceptions
that deliberately propagate out of grading rather than being captured into a
scored-0.0 result) finalizes the row ``FinalStatus.ERROR`` with
``weighted_score == 0.0``, not ``None``. That is not a measurement either, so
it gets the same "write nothing" treatment via ``final_status.category ==
"error"``.
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
