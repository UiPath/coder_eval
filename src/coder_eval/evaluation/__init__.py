"""Evaluation components for checking success criteria and providing LLM feedback.

This package contains:
- checker: Success criterion validation with continuous scoring (0.0-1.0)
- reviewer: Optional LLM-based qualitative feedback

NO re-exports - use explicit imports:
    from coder_eval.evaluation.checker import SuccessChecker
    from coder_eval.evaluation.reviewer import LLMReviewer
"""
