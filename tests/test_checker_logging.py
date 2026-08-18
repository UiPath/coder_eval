"""Tests for SuccessChecker pass/fail log split and shared failure-reason helper."""

import logging

import pytest

from coder_eval.evaluation.checker import SuccessChecker, _short_failure_reason
from coder_eval.models import (
    CriterionResult,
    FileExistsCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


# caplog propagation: coder_eval logger sets propagate=False, so pytest's
# root-attached caplog handler won't see records by default. Use
# caplog.set_level(..., logger="coder_eval") to attach caplog to the
# coder_eval logger directly.


@pytest.fixture
def sandbox(tmp_path):
    config = SandboxConfig(driver="tempdir")
    sb = Sandbox(config, task_id="checker_logging_test")
    try:
        sb.setup()
        yield sb
    finally:
        sb.cleanup()


class TestShortFailureReason:
    def test_prefers_error_over_details(self):
        result = CriterionResult(criterion_type="file_exists", description="x", score=0.0, error="X", details="Y")
        assert _short_failure_reason(result) == "X"

    def test_extracts_stderr_line(self):
        result = CriterionResult(
            criterion_type="run_command",
            description="x",
            score=0.0,
            details="stdout: ok\nstderr: boom",
        )
        assert _short_failure_reason(result) == "boom"

    def test_truncation(self):
        payload = "a" * 1000
        result = CriterionResult(criterion_type="x", description="x", score=0.0, details=payload)
        reason = _short_failure_reason(result, max_len=50)
        assert len(reason) <= 50

    def test_newline_collapse(self):
        result = CriterionResult(
            criterion_type="x",
            description="x",
            score=0.0,
            error="line1\nline2\nline3",
        )
        assert _short_failure_reason(result) == "line1 | line2 | line3"

    def test_empty_returns_sentinel(self):
        result = CriterionResult(criterion_type="x", description="x", score=0.0)
        assert _short_failure_reason(result) == "no details"


class TestCheckerLogSplit:
    def test_passing_criterion_logs_at_debug_only(self, sandbox, caplog):
        # Create a file so file_exists passes.
        (sandbox.sandbox_dir / "hello.txt").write_text("hi")  # type: ignore[operator]
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="hello exists", path="hello.txt")

        with caplog.at_level(logging.DEBUG, logger="coder_eval.evaluation.checker"):
            checker.check_all([criterion])

        score_debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "score:" in r.getMessage()]
        failed_infos = [r for r in caplog.records if r.levelno == logging.INFO and "FAILED" in r.getMessage()]
        assert len(score_debugs) == 1
        assert failed_infos == []

    def test_failing_criterion_logs_one_info_with_reason(self, sandbox, caplog):
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="missing", path="nope.txt")

        with caplog.at_level(logging.DEBUG, logger="coder_eval.evaluation.checker"):
            checker.check_all([criterion])

        failed_infos = [r for r in caplog.records if r.levelno == logging.INFO and "FAILED" in r.getMessage()]
        assert len(failed_infos) == 1
        msg = failed_infos[0].getMessage()
        assert "file_exists" in msg
        assert "score=0.00" in msg

    def test_exception_branch_emits_fail_and_exception_log(self, sandbox, caplog, monkeypatch):
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="x", path="x.txt")

        class _BoomChecker:
            def check(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(checker, "_get_checker_instance", lambda _t: _BoomChecker())

        with caplog.at_level(logging.DEBUG, logger="coder_eval.evaluation.checker"):
            results = checker.check_all([criterion])

        assert results[0].score == 0.0
        failed_infos = [r for r in caplog.records if r.levelno == logging.INFO and "FAILED" in r.getMessage()]
        exception_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(failed_infos) == 1
        assert len(exception_records) == 1
        assert "boom" in failed_infos[0].getMessage()


class TestMirroredWeightIsStamped:
    """`CriterionResult.weight` is stamped at the seam, on every one of its three paths.

    The field exists so a run artifact carries the blend its `weighted_score` was computed from —
    the execution gate's primary statistic is a weighted mean over every criterion, and without
    this it is not reconstructible from the file it is read beside. All three paths are covered
    because two of them build a result for a criterion no checker ever ran, so they cannot route
    through `_finalize_result` and are the halves a single happy-path test cannot see.
    """

    def test_the_success_path_stamps_the_declared_weight(self, sandbox):
        (sandbox.sandbox_dir / "hello.txt").write_text("hi")  # type: ignore[operator]
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="hello exists", path="hello.txt", weight=0.05)

        result = checker.check_all([criterion])[0]

        assert result.weight == 0.05

    def test_a_zero_weight_is_recorded_as_zero_not_as_unrecorded(self, sandbox):
        # An informational criterion. `0.0` and `None` are the two states the field exists to tell
        # apart, so no consumer may read this through truthiness.
        (sandbox.sandbox_dir / "hello.txt").write_text("hi")  # type: ignore[operator]
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="informational", path="hello.txt", weight=0.0)

        result = checker.check_all([criterion])[0]

        assert result.weight == 0.0

    def test_the_unregistered_type_path_stamps_the_weight(self, sandbox, monkeypatch):
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="x", path="x.txt", weight=0.25)

        def _missing(_type: str):
            raise KeyError(_type)

        monkeypatch.setattr(checker, "_get_checker_instance", _missing)
        result = checker.check_all([criterion])[0]

        assert result.error is not None and result.weight == 0.25

    async def test_the_async_success_path_stamps_it_too(self, sandbox):
        # Production runs `check_all_async`. `_finalize_result` is shared by both paths, so this is
        # cheap — but the class claims all three paths and a sync-only suite covers half of each.
        (sandbox.sandbox_dir / "hello.txt").write_text("hi")  # type: ignore[operator]
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="hello exists", path="hello.txt", weight=0.05)

        results = await checker.check_all_async([criterion])

        assert results[0].weight == 0.05

    def test_the_checker_exception_path_stamps_the_weight(self, sandbox, monkeypatch):
        checker = SuccessChecker(sandbox)
        criterion = FileExistsCriterion(description="x", path="x.txt", weight=0.25)

        class _BoomChecker:
            def check(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(checker, "_get_checker_instance", lambda _t: _BoomChecker())
        result = checker.check_all([criterion])[0]

        assert result.score == 0.0 and result.weight == 0.25
