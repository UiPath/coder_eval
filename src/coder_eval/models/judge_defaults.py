"""Shared judge default constants.

Split out of ``models/tasks.py`` so that both ``models/tasks.py`` and
``models/criteria.py`` can import ``DEFAULT_JUDGE_MODEL`` without
introducing an import cycle (``tasks.py`` already imports from
``criteria.py``). Keep this a leaf module — it must NOT import from
``criteria.py`` / ``tasks.py``.
"""

DEFAULT_JUDGE_MODEL = "anthropic.claude-sonnet-4-6"
"""Default model used by the LLM judge (``LLMJudgeCriterion.model``)."""

DEFAULT_SYSTEM_ONE_MODEL = "jev-latest"
"""Default System One model (``SystemOneJudgeCriterion.model``)."""

DEFAULT_SYSTEM_ONE_BASE_URL = "https://api.typesafe.ai/v1"
"""Default System One API root. Overridable per criterion for a gateway or proxy."""

DEFAULT_SYSTEM_ONE_API_KEY_ENV = "TYPESAFE_API_KEY"
"""Env var holding the System One bearer token. Only the NAME is ever stored on a criterion."""
