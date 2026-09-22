"""Token and USD budget checks, shared by the between-turns gate and the mid-turn stop."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from coder_eval.models import RunLimits, TokenUsage
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.events import AgentStartEvent, StreamEvent, TurnEndEvent, TurnStartEvent


Breach = tuple[str, float, float]


def budget_breach(usages: Sequence[TokenUsage], limits: RunLimits) -> Breach | None:
    """Return ``(budget_name, actual, limit)`` for the first budget ``usages`` exceed, else None.

    ``max_usd`` sums only the usages that carry a cost.
    """
    input_tokens = sum(u.uncached_input_tokens for u in usages)
    if limits.count_cache_creation:
        input_tokens += sum(u.cache_creation_input_tokens for u in usages)
    if limits.count_cached_input:
        input_tokens += sum(u.cache_read_input_tokens for u in usages)
    output_tokens = sum(u.output_tokens for u in usages)
    for name, actual, limit in (
        ("input_tokens", input_tokens, limits.max_input_tokens),
        ("output_tokens", output_tokens, limits.max_output_tokens),
        ("total_tokens", input_tokens + output_tokens, limits.max_total_tokens),
    ):
        if limit is not None and actual > limit:
            return name, actual, limit
    costs = [u.total_cost_usd for u in usages if u.total_cost_usd is not None]
    if limits.max_usd is not None and costs and sum(costs) > limits.max_usd:
        return "usd", sum(costs), limits.max_usd
    return None


class LiveBudget:
    """Stream callback whose ``should_stop`` turns True once the turn in flight crosses a budget.

    Adds each inner turn's ``TurnEndEvent.tokens`` to the task's completed iterations. Tokens
    without a reported cost are priced from the rate card; an unpriced model adds no cost.
    """

    def __init__(self, limits: RunLimits, completed: Callable[[], list[TokenUsage]]) -> None:
        self._limits = limits
        self._completed = completed
        self._model: str | None = None
        self._turns: dict[str, TokenUsage] = {}
        self.breach: Breach | None = None

    @classmethod
    def for_limits(cls, limits: RunLimits | None, completed: Callable[[], list[TokenUsage]]) -> LiveBudget | None:
        if limits is None:
            return None
        caps = (limits.max_input_tokens, limits.max_output_tokens, limits.max_total_tokens, limits.max_usd)
        if all(cap is None for cap in caps):
            return None
        return cls(limits, completed)

    def on_event(self, event: StreamEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self._model = event.model
            self._turns = {}
        elif isinstance(event, TurnStartEvent) and event.model:
            self._model = event.model
        elif isinstance(event, TurnEndEvent) and event.tokens is not None and self.breach is None:
            # Keyed by turn: a turn can end more than once, each time with its running total.
            self._turns[event.turn_id] = self._priced(event.tokens)
            self.breach = budget_breach([*self._completed(), *self._turns.values()], self._limits)

    def should_stop(self) -> bool:
        return self.breach is not None

    def _priced(self, tokens: TokenUsage) -> TokenUsage:
        if tokens.total_cost_usd is not None or self._model is None:
            return tokens
        cost = calculate_cost(
            self._model,
            tokens.uncached_input_tokens,
            tokens.output_tokens,
            tokens.cache_creation_input_tokens,
            tokens.cache_read_input_tokens,
        )
        return tokens.model_copy(update={"total_cost_usd": cost})
