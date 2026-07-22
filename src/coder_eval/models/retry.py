"""Per-run retry policy overrides (top-level ``retry:`` block)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetryPolicy(BaseModel):
    """Per-run overrides for the built-in retry policy.

    Lives at the task's top level (``retry:``), not under ``run_limits``: a
    retry policy is not a cap that aborts a run, it is how hard the run tries
    to survive a transient error. It is its own ``-D`` root — ``-D
    retry.max_retries=0``.

    The baseline policy lives in ``errors/categories.py::RETRY_CONFIG`` — a
    per-error-category table (how many times an API error / rate limit / sandbox
    setup failure is worth retrying, and with what backoff). Which categories
    are retryable at all is a property of the error, not of the run, so it stays
    in that table. What varies per run is *appetite*: a local debug loop wants
    ``max_retries: 0`` to fail fast, a flaky-network nightly wants more attempts
    and a longer initial delay than the defaults.

    Every field is optional and applies uniformly to the categories that are
    already retryable; ``None`` means "keep the built-in value". A category with
    ``max_retries=0`` in the table (non-retryable, e.g. auth errors) is never
    made retryable by an override here — overriding a semantic classification
    from a run config would turn a deterministic failure into a slow one.

    The overrides are applied in ``errors/retry.py::resolve_retry_config`` (the
    single seam every retry decision resolves through); this model stays a pure
    data holder so ``models`` keeps no dependency on ``errors``.
    """

    model_config = ConfigDict(extra="forbid")

    max_retries: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Override the retry count for every retryable error category. 0 disables retries "
            "entirely (fail on first error). None = per-category built-in defaults."
        ),
    )
    initial_delay: float | None = Field(
        default=None,
        gt=0.0,
        description="Override the first backoff delay in seconds for every retryable category. None = built-in.",
    )
    backoff_multiplier: float | None = Field(
        default=None,
        gt=0.0,
        description="Override the exponential backoff multiplier for every retryable category. None = built-in.",
    )

    def overrides(self) -> dict[str, float | int]:
        """The non-None overrides as a ``RetryConfig.model_copy(update=...)`` dict."""
        return {
            name: value
            for name, value in (
                ("max_retries", self.max_retries),
                ("initial_delay", self.initial_delay),
                ("backoff_multiplier", self.backoff_multiplier),
            )
            if value is not None
        }
