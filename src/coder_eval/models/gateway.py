"""Shared LLM Gateway constants.

Split out of ``models/tasks.py`` so that both ``models/tasks.py`` and
``models/criteria.py`` can import ``DEFAULT_GATEWAY_MODEL`` without
introducing an import cycle (``tasks.py`` already imports from
``criteria.py``).
"""

DEFAULT_GATEWAY_MODEL = "anthropic.claude-sonnet-4-6"
"""Default LLM Gateway model used by PromptRephrase and LLMJudge."""
