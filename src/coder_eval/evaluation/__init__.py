"""Evaluation components for checking success criteria and providing LLM feedback.

This package contains:
- checker: Success criterion validation with continuous scoring (0.0-1.0)
- summaries: summarize_commands helper shared by orchestrator + llm_judge

NO re-exports - use explicit imports:
    from coder_eval.evaluation.checker import SuccessChecker
    from coder_eval.evaluation.summaries import summarize_commands
"""
