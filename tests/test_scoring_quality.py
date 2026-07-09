"""Tests for code quality scoring: async function support and self/cls handling."""

import pytest

from coder_eval.scoring.quality import QualityScorer
from coder_eval.scoring.signature_similarity import score_function_signatures


class TestAsyncFunctionDefScoring:
    """Verify scoring modules handle async def functions correctly."""

    def test_signature_similarity_includes_async_functions(self):
        """Signature similarity should detect async def functions."""
        code_with_async = (
            "async def fetch_data(url: str) -> dict:\n    pass\n\nasync def process(data: list) -> None:\n    pass\n"
        )
        result = score_function_signatures(code_with_async, code_with_async)
        assert result == 1.0, f"Signature similarity for identical async code = {result}, expected 1.0"

    def test_signature_similarity_async_vs_sync_same_signature(self):
        """Async and sync functions with same signature should match."""
        async_code = "async def fetch(url: str) -> dict:\n    pass\n"
        sync_code = "def fetch(url: str) -> dict:\n    pass\n"

        result = score_function_signatures(async_code, sync_code)
        assert result == 1.0, f"Signature similarity async vs sync = {result}, expected 1.0"

    def test_quality_type_hints_detects_async_functions(self):
        """Type hint scoring should detect async def functions."""
        scorer = QualityScorer()
        code = "async def fetch(url):\n    pass\n\nasync def process(data):\n    pass\n"

        score = scorer.score_type_hints(code)
        assert score == 0.0, f"Type hint score for unannotated async functions = {score}, expected 0.0"

    def test_quality_docstrings_detects_async_functions(self):
        """Docstring scoring should detect async def functions."""
        scorer = QualityScorer()
        code = "async def fetch(url):\n    pass\n\nasync def process(data):\n    pass\n"

        score = scorer.score_docstrings(code)
        assert score == 0.0, f"Docstring score for undocumented async functions = {score}, expected 0.0"

    def test_quality_error_handling_detects_async_functions(self):
        """Error handling scoring should detect async def functions."""
        scorer = QualityScorer()
        code = "async def fetch(url):\n    pass\n\nasync def process(data):\n    pass\n"

        score = scorer.score_error_handling(code)
        assert score == 0.3, f"Error handling score for async functions without try = {score}, expected 0.3"


class TestSelfClsExclusion:
    """Verify self/cls parameters are excluded from type hint scoring."""

    def test_method_with_self_fully_annotated(self):
        """A method with annotated params (except self) should score 1.0."""
        scorer = QualityScorer()
        code = "class MyClass:\n    def method(self, x: int, y: str) -> bool:\n        pass\n"

        score = scorer.score_type_hints(code)
        assert score == pytest.approx(1.0, abs=0.01), (
            f"Type hint score = {score}, expected 1.0. 'self' is being penalized."
        )

    def test_classmethod_with_cls_fully_annotated(self):
        """A classmethod with annotated params (except cls) should score 1.0."""
        scorer = QualityScorer()
        code = 'class MyClass:\n    @classmethod\n    def create(cls, name: str) -> "MyClass":\n        pass\n'

        score = scorer.score_type_hints(code)
        assert score == pytest.approx(1.0, abs=0.01), (
            f"Type hint score = {score}, expected 1.0. 'cls' is being penalized."
        )
