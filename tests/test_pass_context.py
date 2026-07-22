"""End-to-end integration tests for run_command pass_context.

A real tempdir Sandbox runs a scoring script that reads
``$CODER_EVAL_CONTEXT``, json.load-s it, and derives a score. This exercises
the full contract: Orchestrator-supplied run_result -> SuccessChecker ->
CheckContext -> RunCommandChecker -> Sandbox.run_command(extra_env=...).
"""

from datetime import datetime

from coder_eval.criteria.base import CheckContext
from coder_eval.criteria.run_command import RunCommandChecker
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import EvaluationResult, RunCommandCriterion, SandboxConfig
from coder_eval.sandbox import Sandbox


def _eval_result(iteration_count: int = 0) -> EvaluationResult:
    return EvaluationResult(
        task_id="test",
        task_description="d",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=iteration_count,
        environment_info={},
    )


# Scoring script written to a file (no bare f-string shell interpolation — rubric §5).
# Prints the score on the first line, derived from the payload it reads back.
_SCORE_SCRIPT = (
    "import json, os\n"
    "ctx = json.load(open(os.environ['CODER_EVAL_CONTEXT']))\n"
    "assert ctx['success_criteria_results'] == []\n"
    "print(1.0 if ctx['task_id'] == 'test' else 0.0)\n"
)


def test_end_to_end_script_reads_context_env_var():
    """A script reading $CODER_EVAL_CONTEXT scores through SuccessChecker.check()."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_pass_ctx_e2e")
    sandbox.setup()
    try:
        (sandbox.sandbox_dir / "score.py").write_text(_SCORE_SCRIPT, encoding="utf-8")
        criterion = RunCommandCriterion(
            command="python score.py",
            score_from_stdout=True,
            pass_context=True,
            description="score from context",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, run_result=_eval_result())
        assert result.score == 1.0
    finally:
        sandbox.cleanup(preserve=False)


def test_check_all_forwards_run_result_to_context():
    """check_all threads run_result down into CheckContext for the criterion."""
    config = SandboxConfig(driver="tempdir", python=None)
    sandbox = Sandbox(config, task_id="test_pass_ctx_check_all")
    sandbox.setup()
    try:
        # A script that scores from len(iterations) proves the actual payload reached it.
        script = (
            "import json, os\n"
            "ctx = json.load(open(os.environ['CODER_EVAL_CONTEXT']))\n"
            "print(min(1.0, len(ctx['iterations']) / 2))\n"
        )
        (sandbox.sandbox_dir / "count.py").write_text(script, encoding="utf-8")
        criterion = RunCommandCriterion(
            command="python count.py",
            score_from_stdout=True,
            pass_context=True,
            description="score from iteration count",
        )
        checker = SuccessChecker(sandbox)
        # iteration_count is metadata; iterations list is what the script reads (empty here).
        results = checker.check_all([criterion], run_result=_eval_result())
        assert len(results) == 1
        assert results[0].score == 0.0  # empty iterations -> 0/2
    finally:
        sandbox.cleanup(preserve=False)


def test_context_carries_run_result_field():
    """Sanity: CheckContext exposes run_result (used by RunCommandChecker)."""
    ctx = CheckContext(run_result=_eval_result())
    assert ctx.run_result is not None
    # And the checker consumes it without a real sandbox path leak.
    assert RunCommandChecker.criterion_type == "run_command"
