"""Tests for file_contains criterion scoring logic.

Validates that scoring correctly handles includes-only, excludes-only,
and combined includes+excludes cases without score inflation.
"""

from unittest.mock import MagicMock

import pytest

from coder_eval.criteria.file_contains import FileContainsChecker
from coder_eval.models import FileContainsCriterion
from coder_eval.sandbox import Sandbox


class TestFileContainsConditionalScoring:
    """Verify file_contains only averages includes/excludes when both are active."""

    def test_includes_only_score_not_inflated(self):
        """When only includes are specified, score should equal includes_score."""
        sandbox = MagicMock(spec=Sandbox)
        sandbox.file_exists.return_value = True
        sandbox.get_file_content.return_value = "hello world foo"

        criterion = FileContainsCriterion(
            description="Test includes only",
            path="test.txt",
            includes=["hello", "world", "foo", "bar", "baz"],
        )

        checker = FileContainsChecker()
        result = checker._check_impl(criterion, sandbox)

        # 3 of 5 includes found = 0.6
        assert result.score == pytest.approx(0.6, abs=0.01), f"Score {result.score} is inflated. Expected 0.6"

    def test_excludes_only_score_not_inflated(self):
        """When only excludes are specified, score should equal excludes_score."""
        sandbox = MagicMock(spec=Sandbox)
        sandbox.file_exists.return_value = True
        sandbox.get_file_content.return_value = "this has badword1 in it"

        criterion = FileContainsCriterion(
            description="Test excludes only",
            path="test.txt",
            includes=[],
            excludes=["badword1", "badword2"],
        )

        checker = FileContainsChecker()
        result = checker._check_impl(criterion, sandbox)

        # 1 of 2 excludes found = excludes_score = 0.5
        assert result.score == pytest.approx(0.5, abs=0.01), f"Score {result.score} is inflated. Expected 0.5"

    def test_both_includes_and_excludes_averaged(self):
        """When both are specified, they should be averaged."""
        sandbox = MagicMock(spec=Sandbox)
        sandbox.file_exists.return_value = True
        sandbox.get_file_content.return_value = "hello world"

        criterion = FileContainsCriterion(
            description="Test both",
            path="test.txt",
            includes=["hello", "world"],
            excludes=["badword"],
        )

        checker = FileContainsChecker()
        result = checker._check_impl(criterion, sandbox)

        assert result.score == pytest.approx(1.0, abs=0.01)
