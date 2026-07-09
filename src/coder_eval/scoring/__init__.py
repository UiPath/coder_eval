"""Code scoring utilities for reference implementation comparison.

This package provides three scorer classes for evaluating agent-generated code
against reference implementations using static analysis:

- SimilarityScorer: AST, token, and signature similarity
- ComplexityScorer: Cyclomatic complexity, LOC, function count
- QualityScorer: Type hints, docstrings, error handling

All scorers produce scores in the range [0.0, 1.0] where 1.0 is best.
"""
