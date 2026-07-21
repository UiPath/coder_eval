"""No-op ("agentless") agent — the Null Object of the Agent hierarchy.

``NoOpAgent`` binds to ``AgentKind.NONE`` (selected via ``agent: {type: none}``)
for system / canary checks that reuse the eval infrastructure (sandbox,
``pre_run``, reports, evalboard, ADX) without running a coding agent. Its
``start`` / ``communicate`` / ``stop`` are no-ops and it makes no model API
call; ``communicate`` emits the standardized event protocol for a single empty
turn and returns the ``EventCollector``'s reduction (an empty
:class:`~coder_eval.models.results.TurnRecord`), so the orchestrator's normal
lifecycle runs unmodified and then checks the success criteria directly against
the sandbox.

See ``docs/TASK_DEFINITION_GUIDE.md`` (No-op / System Tasks) and issue #203.
"""

from __future__ import annotations

from collections.abc import Callable

from coder_eval.agent import Agent, AgentState
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import AgentKind, ApiRoute, NoneAgentConfig, TurnRecord
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)


@AgentRegistry.register(AgentKind.NONE, NoneAgentConfig)
class NoOpAgent(Agent[NoneAgentConfig]):
    """Agent that does nothing — every lifecycle method is a no-op.

    Created and driven by the orchestrator exactly like any other agent, so no
    ``agentless`` branching is needed: the single signal is ``agent.type ==
    AgentKind.NONE``. ``communicate`` is the SOLE emitter of one clean, balanced
    event tree (``AgentStart`` -> ``TurnStart`` -> ``TurnEnd`` -> ``AgentEnd``,
    all ``COMPLETED``) and returns the empty turn the ``EventCollector`` reduces
    from it.
    """

    def __init__(self, config: NoneAgentConfig, route: ApiRoute | None = None) -> None:
        self.config = config
        self.route = route

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        """No-op: there is no agent process to launch."""
        self._state = AgentState.WORKING

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        """Return an empty turn without contacting any model.

        ``should_stop`` is accepted for ``Agent.communicate`` override
        compatibility and ignored — a no-op turn has nothing to interrupt.

        Honors the streaming contract — sole emitter of a balanced event tree
        (``AgentStart`` -> ``TurnStart`` -> ``TurnEnd`` -> ``AgentEnd``) — so the
        task-log handler and renderers see a clean turn boundary. The returned
        ``TurnRecord`` is the ``EventCollector``'s reduction of those events.
        """
        self._begin_turn()

        task_id = str(self.config.type)  # str() so a plugin subclass with a non-enum kind also works
        turn_id = f"none-{self._iteration}"
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])

        emit.on_event(AgentStartEvent(task_id=task_id, prompt=user_input, iteration=self._iteration))
        emit.on_event(TurnStartEvent(task_id=task_id, turn_id=turn_id))
        emit.on_event(TurnEndEvent(task_id=task_id, turn_id=turn_id, status=TurnEndStatus.COMPLETED))
        emit.on_event(
            AgentEndEvent(
                task_id=task_id,
                status=AgentEndStatus.COMPLETED,
                iteration=self._iteration,
                user_input=user_input,
                agent_output="",
                assistant_turn_count=0,
            )
        )

        self._end_turn_ok()
        return collector.build_turn_record()

    async def stop(self) -> None:
        """No-op: nothing to tear down."""
        self._mark_stopped()
