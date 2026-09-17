"""No-op ("agentless") agent — the Null Object of the Agent hierarchy.

``NoOpAgent`` binds to ``AgentKind.NONE`` (selected via ``agent: {type: none}``)
for system / canary checks that reuse the eval infrastructure (sandbox,
``pre_run``, reports, evalboard, ADX) without running a coding agent. Its
``start`` / ``communicate`` / ``stop`` are no-ops and it makes no model API
call; ``communicate`` writes a single empty turn through its ``TurnEmitter`` and
returns its outcome (an empty
:class:`~coder_eval.models.results.TurnRecord`), so the orchestrator's normal
lifecycle runs unmodified and then checks the success criteria directly against
the sandbox.

See ``docs/TASK_DEFINITION_GUIDE.md`` (No-op / System Tasks) and issue #203.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from coder_eval.agent import Agent, AgentState
from coder_eval.agents.registry import SPI_VERSION, AgentRegistry
from coder_eval.models import (
    AgentKind,
    ApiRoute,
    Enforcement,
    HarnessContract,
    NoneAgentConfig,
    TimingBasis,
    UsageGranularity,
)
from coder_eval.streaming.callbacks import StreamCallback
from coder_eval.streaming.emitter import TurnOutcome
from coder_eval.streaming.events import AgentEndStatus, StopReason


@AgentRegistry.register(AgentKind.NONE, NoneAgentConfig, spi_version=SPI_VERSION)
class NoOpAgent(Agent[NoneAgentConfig]):
    """Agent that does nothing — every lifecycle method is a no-op.

    Created and driven by the orchestrator exactly like any other agent, so no
    ``agentless`` branching is needed: the single signal is ``agent.type ==
    AgentKind.NONE``. ``communicate`` writes one clean, balanced event tree
    (``AgentStart`` -> ``TurnStart`` -> ``TurnEnd`` -> ``AgentEnd``, all
    ``COMPLETED``) and returns its outcome.
    """

    contract = HarnessContract(
        system_prompt=Enforcement.UNSUPPORTED,
        plugin_skills=Enforcement.UNSUPPORTED,
        permission_mode=Enforcement.UNSUPPORTED,
        allowed_tools=Enforcement.UNSUPPORTED,
        disallowed_tools=Enforcement.UNSUPPORTED,
        cooperative_stop=False,
        reports_cost=True,
        usage_granularity=UsageGranularity.TURN,
        timing_basis=TimingBasis.TURN_CLOCK,
    )

    def __init__(
        self, config: NoneAgentConfig, route: ApiRoute | None = None, *, cost_log_tags: dict[str, str] | None = None
    ) -> None:
        super().__init__(config, route, cost_log_tags=cost_log_tags)

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
    ) -> None:
        """No-op: there is no agent process to launch."""
        self._state = AgentState.WORKING

    async def communicate(
        self,
        user_input: str,
        *,
        iteration: int,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnOutcome:
        """Return one empty, completed turn without contacting any model.

        ``timeout`` and ``should_stop`` are accepted and ignored: a no-op turn has
        nothing to interrupt.
        """
        emitter = self._open_emitter(
            prompt=user_input,
            iteration=iteration,
            model=None,
            task_id=str(self.config.type),  # str() so a plugin subclass with a non-enum kind also works
            stream_callback=stream_callback,
        )
        emitter.begin()
        emitter.begin_inner_turn(f"none-{iteration}")
        emitter.end_inner_turn()
        return emitter.finalize(AgentEndStatus.COMPLETED, assistant_turn_count=0, num_turns=None, result_summary=None)

    async def stop(self) -> None:
        """No-op: nothing to tear down."""
        self._mark_stopped()
