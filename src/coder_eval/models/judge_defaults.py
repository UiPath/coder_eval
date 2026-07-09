"""Shared judge default constants.

Split out of ``models/tasks.py`` so that both ``models/tasks.py`` and
``models/criteria.py`` can import ``DEFAULT_JUDGE_MODEL`` without
introducing an import cycle (``tasks.py`` already imports from
``criteria.py``). Keep this a leaf module — it must NOT import from
``criteria.py`` / ``tasks.py``.
"""

DEFAULT_JUDGE_MODEL = "anthropic.claude-sonnet-4-6"
"""Default model used by the LLM judge (``LLMJudgeCriterion.model``)."""
