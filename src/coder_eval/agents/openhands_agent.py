"""OpenHands agent implementation using the OpenHands Software Agent SDK.

The backend drives the OpenHands ``Conversation`` (``openhands-sdk`` +
``openhands-tools``) against any model reachable through OpenHands' bundled
LiteLLM. The provider is resolved from the ``agent.model`` prefix — LiteLLM reads
the matching key from the environment (``anthropic/…`` → ``ANTHROPIC_API_KEY``,
``openai/…`` → ``OPENAI_API_KEY``, ``openrouter/…`` → ``OPENROUTER_API_KEY``,
``bedrock/…`` → ``AWS_*``, ``litellm_proxy/<alias>`` → ``OPENHANDS_BASE_URL``) with
no per-provider branch in this module. This makes OpenHands the model-agnostic
"universal harness" for isolate-the-model comparisons. ``OPENHANDS_BASE_URL`` is
an OPTIONAL endpoint override, used only for the LiteLLM-proxy path.

NO OpenHands-side sandbox: the SDK's ``LocalWorkspace`` runs tools (terminal /
file_editor) directly on the host. coder_eval's own per-run sandbox — a docker
container (docker driver) or an ephemeral per-task tempdir it creates and discards
(tempdir driver) — is the trust boundary, exactly as it is for Claude / Codex /
Antigravity. Do NOT add a second sandbox; hard isolation of untrusted actions is
the docker driver's job.

Threading model (the one genuinely novel piece): ``Conversation.run()`` is
SYNCHRONOUS and its event callback fires on the calling thread. ``communicate()``
drives it via ``await asyncio.to_thread(self._run_conversation, …)``, so the main
coroutine is *parked* for the run's duration — the callback (on the worker thread)
is then the ONLY writer touching ``emit`` / ``EventCollector`` (single-writer, no
lock, no queue marshaling needed for correctness). Events are emitted DIRECTLY from
that callback; a ``queue.Queue`` fallback would only be needed if Rich garbled
off-thread output, which the single-writer invariant makes unnecessary.
``ThreadedWatchdog`` (a separate OS thread) fires ``conversation.pause()`` on
timeout; ``should_stop()`` is polled inside the callback and also calls ``pause()``.

All SDK imports are lazy (inside ``start`` / ``_run_conversation``), mirroring the
Codex / Antigravity backends, so this module imports cleanly when the optional
``[openhands]`` extra is absent; a missing SDK surfaces as a clear install hint at
``start()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import ThreadedWatchdog
from coder_eval.config import settings
from coder_eval.errors import (
    AgentCrashError,
    TurnTimeoutError,
    truncate_crash_message,
)
from coder_eval.models import (
    AgentKind,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    DirectRoute,
    OpenHandsAgentConfig,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)


logger = logging.getLogger(__name__)

# OpenHands' internal tool registry names (registered by ``import openhands.tools``)
# for the two tools an eval harness needs: a shell (terminal) and file editing.
_TERMINAL_TOOL = "terminal"
_FILE_EDITOR_TOOL = "file_editor"

# Default inner-loop cap when a task pins no max_turns — passed as the
# Conversation's ``max_iteration_per_run`` constructor kwarg (NOT a run() arg).
_DEFAULT_MAX_ITERATIONS = 500

# The ONE terminal ConversationExecutionStatus NAME that is a clean, agent-completed
# turn (the agent called `finish`). Classification is an ALLOWLIST: a turn is clean iff
# the status is FINISHED, or WE caused a PAUSED (timeout / cooperative stop). ANY other
# value — ERROR, STUCK, WAITING_FOR_CONFIRMATION, an unexpected IDLE/RUNNING/DELETING,
# a PAUSED we did not cause, or a future SDK status — is crash-classified, so a
# non-terminal run can never silently score as COMPLETED. A plain string so this module
# needs no SDK import at module load (the SDK is an optional extra; only
# start()/_run_conversation touch it). A named constant — not a bare literal — so the
# comparison is not mistaken for a coder_eval FinalStatus denylist (CE018).
_STATUS_FINISHED = "FINISHED"


@AgentRegistry.register(AgentKind.OPENHANDS, OpenHandsAgentConfig)
class OpenHandsAgent(Agent[OpenHandsAgentConfig]):
    """Implementation of the Agent interface for the OpenHands Software Agent SDK."""

    # The event callback polls the cooperative ``should_stop`` between tool steps
    # and calls ``pause()``, so this agent supports early-stop-on-criterion.
    supports_cooperative_stop: ClassVar[bool] = True

    # ``LLM.extra_headers`` accepts per-request headers (verified against the installed SDK), so
    # the LiteLLM-proxy path can forward the ``x-ce-*`` cost-correlation headers for
    # the per-run actual-cost join. The direct-provider default carries no tags and
    # uses OpenHands' native LiteLLM cost estimate.
    supports_cost_log_tags: ClassVar[bool] = True

    def __init__(
        self,
        config: OpenHandsAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "openhands",
        cost_log_tags: dict[str, str] | None = None,
    ):
        """Initialize the OpenHands agent.

        Args:
            config: Agent configuration.
            route: API routing configuration (unused — OpenHands owns model-reaching
                via the ``agent.model`` provider prefix + optional ``OPENHANDS_BASE_URL``;
                kept for interface compatibility, mirroring Codex/Antigravity).
            instance_name: Short label used to prefix this instance's log records.
            cost_log_tags: Optional ``x-ce-*`` cost-correlation headers forwarded to
                the LiteLLM proxy for the per-run actual-cost join (proxy path only).
        """
        self.config = config
        self.route = route or DirectRoute()
        self.working_directory: Path | None = None
        self._env_path_prepend: list[str] = []
        self._cost_log_tags = dict(cost_log_tags or {})
        # Live handle to the in-flight Conversation, set at the top of communicate()
        # and cleared in its finally. The watchdog / kill paths pause + close it.
        self._active_conversation: Any = None
        # _state / _iteration / _iteration_was_incremented / pending_turn lifecycle
        # bookkeeping lives on the Agent base class (shared defaults + helpers).
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})

    def _effective_model(self) -> str | None:
        """Resolve the model: task/CLI ``agent.model`` wins, else OPENHANDS_MODEL.

        Carries the LiteLLM provider prefix intact (e.g. ``openrouter/z-ai/glm-5.2``,
        ``anthropic/…``, ``litellm_proxy/<alias>``). May be None when nothing is
        configured — surfaced as a clear error at communicate() rather than sending
        model=None to the SDK.
        """
        return self.config.model or settings.openhands_model

    @staticmethod
    def _resolve_base_url() -> str | None:
        """Optional LiteLLM-proxy endpoint override from OPENHANDS_BASE_URL, or None.

        Left unset for direct provider calls (LiteLLM resolves the endpoint from the
        model prefix); set only for the ``litellm_proxy/<alias>`` path.
        """
        return os.getenv("OPENHANDS_BASE_URL") or None

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        """Initialize and start the OpenHands agent.

        The heavy SDK objects (``LLM`` / ``Agent`` / ``Conversation``) are built
        per-turn in ``communicate()`` (mirroring Claude's per-communicate client),
        so ``start()`` only records the working directory + PATH prepend and
        verifies the optional extra is importable, surfacing a clear install hint
        if it is not.

        Args:
            working_directory: The sandbox working directory. Passed to the
                Conversation as its ``workspace`` so file edits land where
                coder_eval's on-disk success criteria look for them.
            env_path_prepend: Absolute dirs to prepend to PATH so sandbox mock CLIs
                shadow the real ones for the terminal tool's shell.
            plugin_tools_dir: Unused (OpenHands has no Claude-style skill discovery
                surface in v1; ``skill_triggered`` is not implemented for it).
        """
        self.working_directory = Path(working_directory)
        self._env_path_prepend = list(env_path_prepend or [])
        # Keep the SDK's startup banner out of the task log.
        os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
        self._state = AgentState.WORKING

        try:
            import openhands.sdk
            import openhands.tools  # noqa: F401
        except ImportError as e:
            raise RuntimeError("OpenHands SDK not installed. Install with: pip install 'coder-eval[openhands]'") from e

        # OpenHands has no Claude-style skill surface in v1: skill plugins are not
        # wired and `skill_triggered` is not implemented for it. Warn loudly rather
        # than silently ignoring a configured `plugins:` block (a task that armed a
        # skill_triggered criterion against this agent would otherwise score it a
        # silent always-negative — see docs/agents/OPENHANDS.md "Known limitations").
        if self.config.plugins:
            self._log.warning(
                "OpenHandsAgent ignores agent.plugins (%d configured): "
                + "skill discovery / skill_triggered are not implemented for this agent.",
                len(self.config.plugins),
            )

        self._log.debug("OpenHands agent ready (model=%s)", self._effective_model())

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        """Send a message to the OpenHands agent and receive its response.

        Builds a fresh ``LLM`` / ``Agent`` / ``Conversation`` for this turn, sends
        the prompt, then drives the SYNCHRONOUS ``conversation.run()`` off-thread
        (``asyncio.to_thread``) so the event loop stays free and the watchdog's
        ``pause()`` can land. The per-turn ``_OpenHandsTurnState`` maps the SDK's
        event stream onto the standardized event protocol.

        ``should_stop`` is the cooperative early-stop callback, polled inside the
        event callback; when it returns True the run is paused and the turn
        finalizes cleanly as ``STOPPED_EARLY`` (``crashed=False``).

        Raises:
            RuntimeError: If the agent is not started (or no model is configured).
            TurnTimeoutError: Timeout elapsed (partial TurnRecord on pending_turn).
            AgentCrashError: SDK failed mid-turn (same pending_turn contract).
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")

        assert self.config.type is not None, "OpenHandsAgent requires AgentConfig.type before communicate()"

        model = self._effective_model()
        if not model:
            raise RuntimeError(
                "No model configured for the OpenHands agent. "
                + "Set agent.model (e.g. 'openrouter/z-ai/glm-5.2'), --model, or OPENHANDS_MODEL."
            )

        self._begin_turn()
        turn_start_time = time.monotonic()
        task_id = str(self.config.type)
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])
        turn_id = f"openhands-{self._iteration}"

        state = _OpenHandsTurnState(
            agent=self,
            emit=emit,
            task_id=task_id,
            turn_id=turn_id,
            collector=collector,
            user_input=user_input,
            iteration=self._iteration,
            model=model,
            turn_start_time=turn_start_time,
            should_stop=should_stop,
        )

        try:
            emit.on_event(AgentStartEvent(task_id=task_id, prompt=user_input, iteration=self._iteration, model=model))

            def _on_turn_timeout() -> None:
                state.timeout_hit = True
                self._pause_active_conversation()

            with ThreadedWatchdog(
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                asyncio_task_to_cancel=asyncio.current_task(),
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            ):
                emit.on_event(TurnStartEvent(task_id=task_id, turn_id=turn_id, model=model))
                # run() is SYNCHRONOUS → drive it off-thread; the event callback emits
                # events as they fire (single-writer, main coroutine parked).
                #
                # The worker runs as its own Task so that on a watchdog/external cancel we
                # can AWAIT it to completion before touching the turn state. asyncio.to_thread
                # cancels only the awaiting coroutine — the OS thread keeps running — so
                # abandoning it would let the main coroutine resume and read/build from
                # state.messages/_open_tools/_blocks while the worker is STILL mutating them
                # (a data race). Instead the timeout path fires pause() first (in
                # _on_turn_timeout), which makes run() return promptly (verified against the SDK),
                # then we drain the worker so the state is fully settled by a single writer
                # before finalize() reads it — and so state.usage / final_status are committed
                # even on a timed-out turn.
                worker = asyncio.ensure_future(
                    asyncio.to_thread(self._run_conversation, state, user_input, model, max_turns)
                )
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # The awaiting frame was cancelled but the OS thread keeps running.
                    # Drain it to completion (pause() already asked run() to stop, so it
                    # returns promptly) so the worker is the sole writer that settles the
                    # state before finalize() reads it. Re-shield in a loop because the
                    # pending cancel makes each await re-raise until the future is done.
                    await self._drain_worker(worker)
                    if state.timeout_hit:
                        self._finalize_and_raise_timeout(state.finalize, timeout or 0)
                    raise
                except Exception as e:
                    if state.timeout_hit:
                        self._finalize_and_raise_timeout(state.finalize, timeout or 0, cause=e)
                    if state.stopped_early_hit:
                        # The turn already stopped cleanly; escalating to a crash would
                        # trigger the orchestrator's retry with the watcher's decision
                        # still latched → immediate stop-at-turn-0 on the retry (wasted
                        # spend). Fall through to the clean tail.
                        self._log.warning("Ignoring post-stop exception; finalizing as STOPPED_EARLY: %s", e)
                    else:
                        self._finalize_and_raise_crash(
                            state.finalize, truncate_crash_message(f"OpenHands turn failed: {e!s}"), cause=e
                        )

            if state.timeout_hit:
                # Watchdog fired but run() finished before the cancel landed.
                assert timeout is not None
                self._finalize_and_raise_timeout(state.finalize, timeout)
        except (AgentCrashError, TurnTimeoutError):
            raise
        except asyncio.CancelledError:
            if not state.finalized:
                self._finalize_external_cancel(state.finalize)
            raise
        except Exception as e:
            if state.stopped_early_hit and not state.timeout_hit:
                self._log.warning("Ignoring post-stop exception; finalizing as STOPPED_EARLY: %s", e)
            else:
                self._finalize_and_raise_crash(
                    state.finalize, truncate_crash_message(f"OpenHands turn failed: {e!s}"), cause=e
                )
        finally:
            self._close_conversation()

        self._state = AgentState.WORKING
        self._end_turn_ok()
        # Precedence matches the other backends: timeout (raised above) > stopped_early > completed.
        status = AgentEndStatus.STOPPED_EARLY if state.stopped_early_hit else AgentEndStatus.COMPLETED
        state.finalize(status, crashed=False, crash_reason=None)
        return collector.build_turn_record()

    @staticmethod
    async def _drain_worker(worker: asyncio.Future[Any]) -> None:
        """Await the off-thread worker to completion despite a pending cancel.

        The enclosing task is being cancelled, so each ``await`` on the worker
        re-raises ``CancelledError`` immediately rather than waiting. We re-shield
        in a loop until the worker future is actually ``done()`` — the OS thread
        cannot be cancelled, and ``pause()`` (already fired) makes ``run()`` return
        promptly, so this settles quickly. Guarded so a truly stuck native call
        can't spin forever (the watchdog's ``kill_sync``/``close`` is the backstop).
        """
        for _ in range(3000):  # ~30s ceiling at 10ms/iteration
            if worker.done():
                return
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(worker), timeout=0.01)

    def _run_conversation(self, state: _OpenHandsTurnState, user_input: str, model: str, max_turns: int | None) -> None:
        """Build the SDK objects, send the prompt, and drive the synchronous run().

        Runs on the ``asyncio.to_thread`` worker. The SDK imports are lazy here so
        the module loads without the optional extra. After ``run()`` returns, the
        terminal ``ConversationExecutionStatus`` decides clean-vs-crash and the
        accumulated ``Metrics`` are mapped onto the turn total.
        """
        from openhands.sdk import LLM, Agent, Conversation, Tool

        base_url = self._resolve_base_url()
        # Forward cost-correlation headers ONLY on the proxy path (tags present).
        extra_headers = dict(self._cost_log_tags) if self._cost_log_tags else None
        llm = LLM(
            model=model,
            base_url=base_url,
            extra_headers=extra_headers,
            usage_id=state.turn_id,
        )
        sdk_agent = Agent(llm=llm, tools=[Tool(name=_TERMINAL_TOOL), Tool(name=_FILE_EDITOR_TOOL)])
        conversation = Conversation(
            agent=sdk_agent,
            workspace=str(self.working_directory),
            callbacks=[state.dispatch],
            max_iteration_per_run=max_turns or _DEFAULT_MAX_ITERATIONS,
            # delete_on_close defaults True, which would delete coder_eval's sandbox
            # working dir on close(); coder_eval owns sandbox teardown.
            delete_on_close=False,
        )
        self._active_conversation = conversation
        state.conversation = conversation
        conversation.send_message(user_input)
        conversation.run()
        state.record_final_status(conversation)
        state.usage = self._token_usage_from_openhands(conversation)
        # ALLOWLIST classification: a turn is clean iff the terminal status is FINISHED,
        # or WE caused a PAUSED (timeout / cooperative stop). When we paused, the outer
        # communicate() handlers own the outcome (timeout raise / clean STOPPED_EARLY),
        # so we don't raise here. Otherwise ANY status other than FINISHED — ERROR,
        # STUCK, WAITING_FOR_CONFIRMATION, or an unexpected IDLE/RUNNING/DELETING (or a
        # future SDK status), including a PAUSED we did NOT cause — is a crash, so a
        # non-terminal run can never silently score as COMPLETED (denylist → allowlist).
        we_paused = state.timeout_hit or state.stopped_early_hit
        if not we_paused and state.final_status != _STATUS_FINISHED:
            raise RuntimeError(f"OpenHands run ended in status {state.final_status or '(unknown)'}")
        # NOTE: teardown is deliberately NOT done here. close() is driven from the
        # main-thread `finally` in communicate() (_close_conversation); the drain there
        # guarantees this worker has fully returned before that close runs, so it never
        # closes mid-run from this thread. The task-level watchdog may ALSO call close()
        # via kill_sync() from its own OS thread — that's safe because _close_conversation
        # is idempotent (null-and-suppress) with delete_on_close=False, so a concurrent
        # close is at worst a redundant no-op, never data loss.

    def _token_usage_from_openhands(self, conversation: Any) -> TokenUsage | None:
        """Map OpenHands' accumulated ``Metrics`` onto coder_eval's ``TokenUsage``.

        Single conversion site. OpenHands' ``TokenUsage.prompt_tokens`` ALREADY
        INCLUDES the cache buckets (verified empirically against the SDK), so the fresh/uncached slice
        is ``prompt_tokens - cache_read - cache_write`` (guarded with ``max(0, …)``).
        coder_eval's three input buckets are mutually exclusive and SUM to the full
        prompt; cost bills the uncached slice at the input rate. The turn cost is
        OpenHands' native LiteLLM estimate (``accumulated_cost``); when the model is
        unpriced in our rate card we still keep that estimate (never zero it).
        """
        try:
            metrics = conversation.conversation_stats.get_combined_metrics()
        except Exception as e:
            self._log.debug("Could not read OpenHands metrics: %s", e)
            return None
        if metrics is None:
            return None
        usage = getattr(metrics, "accumulated_token_usage", None)
        cost = self._finite_cost(getattr(metrics, "accumulated_cost", None))
        if usage is None:
            return TokenUsage(total_cost_usd=cost) if cost else None

        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_tokens", 0) or 0
        cache_write = getattr(usage, "cache_write_tokens", 0) or 0
        # Preserve the invariant that the three input buckets SUM to the full prompt:
        # clamp the cache buckets so cache_read + cache_write never exceeds prompt (a
        # malformed provider report would otherwise over-count input tokens/cost, since
        # uncached is floored at 0). cache_read is trusted first, cache_write fills the
        # remainder.
        cache_read = min(cache_read, prompt)
        cache_write = min(cache_write, prompt - cache_read)
        uncached = prompt - cache_read - cache_write
        # NOTE: reasoning tokens are billed as output by LiteLLM, so they are already
        # inside completion_tokens. TokenUsage carries no reasoning bucket (that lives
        # on AssistantMessage), so we do NOT pass it here — mirrors Antigravity.
        return TokenUsage(
            uncached_input_tokens=uncached,
            output_tokens=completion,
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
            # OpenHands' native LiteLLM cost estimate is authoritative for this
            # backend (no proxy actual-cost join in v1). Keep it even when the model
            # is unpriced in our rate card.
            total_cost_usd=cost,
        )

    @staticmethod
    def _finite_cost(cost: Any) -> float | None:
        """Coerce the SDK's ``accumulated_cost`` to a finite float, else None.

        Guards against a nan/inf estimate ever propagating into budget / report math.
        """
        if cost is None:
            return None
        with contextlib.suppress(TypeError, ValueError):
            value = float(cost)
            if math.isfinite(value):
                return value
        return None

    async def stop(self) -> None:
        """Stop the agent and tear down any live Conversation."""
        self._close_conversation()
        self._mark_stopped()

    async def kill(self) -> None:
        """Force-terminate: pause any in-flight run, then tear down."""
        self._pause_active_conversation()
        await self.stop()

    def kill_sync(self) -> None:
        """Synchronous abort for the watchdog thread (cannot await).

        Pause the in-flight run so the blocked ``run()`` unwinds, then close the
        conversation. Safe to call at any time and idempotent.
        """
        self._pause_active_conversation()
        self._close_conversation()

    def _pause_active_conversation(self) -> None:
        """Pause the in-flight Conversation, if any (best-effort, idempotent).

        ``pause()`` is thread-safe (verified: callable from another thread
        while ``run()`` blocks). Used by the timeout watchdog and the cooperative
        early-stop poll to break the loop.
        """
        conversation = self._active_conversation
        if conversation is None:
            return
        with contextlib.suppress(Exception):
            conversation.pause()

    def _close_conversation(self) -> None:
        """Close the live Conversation (best-effort, idempotent).

        ``delete_on_close=False`` is set at construction so this does NOT delete
        coder_eval's sandbox working dir.
        """
        conversation = self._active_conversation
        self._active_conversation = None
        if conversation is None:
            return
        with contextlib.suppress(Exception):
            conversation.close()

    def get_environment_info(self) -> dict[str, Any]:
        """Record the resolved OpenHands routing so runs are auditable/comparable.

        The model (with its provider prefix) is always recorded; the proxy host is
        added only when ``OPENHANDS_BASE_URL`` is set (host only, not the full URL,
        to avoid leaking any embedded credentials).
        """
        info: dict[str, Any] = {"openhands_model": self._effective_model() or ""}
        base_url = self._resolve_base_url()
        if base_url:
            info["openhands_base_url_host"] = urlparse(base_url).hostname or ""
        return info


class _OpenHandsTurnState:
    """Per-turn mutable scratch for one ``OpenHandsAgent.communicate`` call.

    Maps the OpenHands SDK event stream onto the standardized event protocol and
    reconstructs the assistant transcript. The same ``messages`` / ``commands``
    accumulate live, so a mid-turn crash keeps the partial transcript (the agent's
    shared crash kernel builds ``pending_turn`` from ``collector``).

    Event shape this consumes (verified against the SDK, live order):
    ``SystemPromptEvent -> MessageEvent -> (ActionEvent -> ObservationEvent) x N``
    (the last ActionEvent's ``tool_name == "finish"``). Tools join start↔end on
    ``ActionEvent.tool_call_id`` == ``ObservationEvent.action_id``; a tool FAILURE
    arrives as a separate ``AgentErrorEvent`` (same ``tool_call_id``).

    Single-writer: the callback runs on the ``asyncio.to_thread`` worker while the
    main coroutine is parked, so no lock is needed. Token buckets are NOT cut
    per-generation (OpenHands surfaces usage only as a post-run aggregate); the
    turn total is set on ``usage`` after ``run()`` and the EventCollector books the
    reconciliation residual so the transcript sums to the total.
    """

    def __init__(
        self,
        *,
        agent: OpenHandsAgent,
        emit: CompositeStreamCallback,
        task_id: str,
        turn_id: str,
        collector: EventCollector,
        user_input: str,
        iteration: int,
        model: str,
        turn_start_time: float,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        self._agent = agent
        self.emit = emit
        self.task_id = task_id
        self.turn_id = turn_id
        self.collector = collector
        self.user_input = user_input
        self.iteration = iteration
        self.model = model
        self.turn_start_time = turn_start_time
        self._should_stop = should_stop

        self.timeout_hit = False
        self.stopped_early_hit = False
        self.finalized = False
        self.conversation: Any = None

        # Turn total, committed by the run helper after run() returns (None on crash
        # → EventCollector treats it as "no usage reported").
        self.usage: TokenUsage | None = None
        self._final_status: str | None = None

        self.messages: list[TranscriptMessage] = []
        self.commands: list[CommandTelemetry] = []
        self._output_parts: list[str] = []
        self._blocks: list[ContentBlock] = []
        self._assistant_turns = 0

        self._next_seq = 0
        # tool_call_id → the ToolStart telemetry, awaiting its Observation/error.
        self._open_tools: dict[str, CommandTelemetry] = {}
        self._closed_tools: set[str] = set()

    def dispatch(self, event: Any) -> None:
        """Route one SDK event to its handler, then poll cooperative stop.

        The ``should_stop`` poll runs AFTER the handler (the emission that lets the
        watcher latch on the deciding tool call) — the deciding event is kept, the
        run is then paused so no further work is pulled. No-op when should_stop is
        None. Errors in a handler are swallowed so a mapping hiccup never aborts the
        run mid-stream (the frozen trajectory is authoritative for grading).
        """
        try:
            self._route(event)
        except Exception as e:  # pragma: no cover - defensive
            self._agent._log.debug("OpenHands event mapping error (%s): %s", type(event).__name__, e)

        if self._should_stop is not None and not self.stopped_early_hit and self._should_stop():
            self.stopped_early_hit = True
            self._agent._log.debug("Cooperative stop requested; pausing OpenHands run at this boundary")
            self._agent._pause_active_conversation()

    def _route(self, event: Any) -> None:
        from openhands.sdk.event import ActionEvent, AgentErrorEvent, MessageEvent, ObservationEvent

        if isinstance(event, ActionEvent):
            self._on_action(event)
        elif isinstance(event, ObservationEvent):
            self._on_observation(event)
        elif isinstance(event, AgentErrorEvent):
            self._on_agent_error(event)
        elif isinstance(event, MessageEvent):
            self._on_message(event)

    def _on_action(self, event: Any) -> None:
        """Emit ToolStart + record the tool_use block for one tool call."""
        tool_call_id = getattr(event, "tool_call_id", None) or f"tool_{self._next_seq}"
        if tool_call_id in self._open_tools:
            return  # defensive: duplicate action for the same id
        seq = self._next_seq
        self._next_seq += 1
        now = datetime.now()
        tel = CommandTelemetry(
            tool_name=getattr(event, "tool_name", "") or "Tool",
            tool_id=tool_call_id,
            timestamp=now,
            parameters=self._action_parameters(event),
            sequence_number=seq,
            execution_started_at=now,
        )
        self._open_tools[tool_call_id] = tel
        self.emit.on_event(ToolStartEvent(task_id=self.task_id, turn_id=self.turn_id, tool=tel))
        self._blocks.append(ContentBlock(block_type="tool_use", sequence=0, tool_use_id=tool_call_id))

    def _on_observation(self, event: Any) -> None:
        """Resolve the pending tool for a successful ObservationEvent."""
        action_id = getattr(event, "action_id", None)
        self._close_tool(action_id, errored=False, result=self._observation_text(event), error_message=None)

    def _on_agent_error(self, event: Any) -> None:
        """A tool FAILURE arrives as a separate AgentErrorEvent — close it as ERROR."""
        tool_call_id = getattr(event, "tool_call_id", None)
        error = getattr(event, "error", None)
        self._close_tool(tool_call_id, errored=True, result=None, error_message=str(error) if error else "tool failed")

    def _close_tool(self, tool_id: str | None, *, errored: bool, result: str | None, error_message: str | None) -> None:
        if not tool_id or tool_id not in self._open_tools or tool_id in self._closed_tools:
            return
        self._closed_tools.add(tool_id)
        start_tel = self._open_tools[tool_id]
        completed = datetime.now()
        started = start_tel.execution_started_at or completed
        end_tel = start_tel.model_copy(
            update={
                "result_status": "error" if errored else "success",
                "result_summary": result,
                "error_message": error_message,
                "execution_completed_at": completed,
                "duration_ms": max((completed - started).total_seconds() * 1000.0, 0.0),
            }
        )
        self.commands.append(end_tel)
        self.emit.on_event(
            ToolEndEvent(
                task_id=self.task_id,
                turn_id=self.turn_id,
                tool=end_tel,
                status=ToolEndStatus.ERROR if errored else ToolEndStatus.OK,
            )
        )

    def _on_message(self, event: Any) -> None:
        """Assistant text (source == 'agent') → TextChunk + a text block."""
        if getattr(event, "source", None) != "agent":
            return
        text = self._message_text(event)
        if not text:
            return
        self._output_parts.append(text)
        self._blocks.append(ContentBlock(block_type="text", sequence=0, text=text))
        self.emit.on_event(TextChunkEvent(task_id=self.task_id, turn_id=self.turn_id, text=text))

    @staticmethod
    def _message_text(event: Any) -> str:
        from openhands.sdk.llm import content_to_str

        llm_message = getattr(event, "llm_message", None)
        content = getattr(llm_message, "content", None)
        if content is None:
            return ""
        with contextlib.suppress(Exception):
            return "".join(content_to_str(content))
        return ""

    @staticmethod
    def _observation_text(event: Any) -> str | None:
        observation = getattr(event, "observation", None)
        if observation is None:
            return None
        for attr in ("agent_observation", "content", "output", "text"):
            value = getattr(observation, attr, None)
            if isinstance(value, str) and value:
                return value
        with contextlib.suppress(Exception):
            return str(observation)
        return None

    @staticmethod
    def _action_parameters(event: Any) -> dict[str, Any]:
        """Best-effort model-supplied inputs for a tool call (the typed action)."""
        action = getattr(event, "action", None)
        if action is None:
            return {}
        with contextlib.suppress(Exception):
            dumped = action.model_dump(mode="json")
            if isinstance(dumped, dict):
                return dumped
        with contextlib.suppress(Exception):
            return dict(vars(action))
        return {}

    @property
    def final_status(self) -> str | None:
        """The terminal ConversationExecutionStatus value (e.g. 'FINISHED'), or None."""
        return self._final_status

    def record_final_status(self, conversation: Any) -> None:
        """Capture the terminal ConversationExecutionStatus after run() returns."""
        with contextlib.suppress(Exception):
            status = conversation.state.execution_status
            self._final_status = getattr(status, "name", None) or getattr(status, "value", None) or str(status)

    def _agent_output(self) -> str:
        return "".join(self._output_parts)

    def _flush_blocks(self) -> None:
        """Cut accumulated blocks into a single AssistantMessage.

        OpenHands surfaces usage only as a post-run aggregate (no per-generation
        token stream in events), so the per-message token buckets are left at 0 and
        the EventCollector books the whole turn total as the reconciliation residual
        (the token-sum invariant still holds). One flush per turn keeps the
        transcript non-empty for the evalboard.
        """
        if not self._blocks:
            return
        now = datetime.now()
        for i, block in enumerate(self._blocks):
            block.sequence = i
        self.messages.append(
            AssistantMessage(
                started_at=now,
                completed_at=now,
                generation_duration_ms=0.0,
                content_blocks=list(self._blocks),
                tool_use_ids=[b.tool_use_id for b in self._blocks if b.block_type == "tool_use" and b.tool_use_id],
                model=self.model,
            )
        )
        self._assistant_turns += 1
        self._blocks = []

    def finalize(self, status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None) -> None:
        """Close orphaned tools, flush leftover blocks, emit TurnEnd + AgentEnd.

        Idempotent. On a crash, also builds the partial ``pending_turn`` from the
        collector (the agent base's shared crash kernel).
        """
        if self.finalized:
            return
        self.finalized = True

        # Force-close any tool that emitted ToolStart but never resolved (an orphan).
        for tool_id, tel in self._open_tools.items():
            if tool_id in self._closed_tools:
                continue
            orphan = tel.model_copy(update={"result_status": "unknown", "execution_completed_at": datetime.now()})
            self.emit.on_event(
                ToolEndEvent(
                    task_id=self.task_id,
                    turn_id=self.turn_id,
                    tool=orphan,
                    status=ToolEndStatus.UNRESOLVED,
                )
            )

        self._flush_blocks()

        # AgentEndStatus and TurnEndStatus are parallel by value; convert directly
        # so an unmapped future member raises loudly instead of bucketing to COMPLETED.
        turn_status = TurnEndStatus(status.value)
        self.emit.on_event(
            TurnEndEvent(
                task_id=self.task_id,
                turn_id=self.turn_id,
                status=turn_status,
                tokens=self.usage,
            )
        )
        self.emit.on_event(
            AgentEndEvent(
                task_id=self.task_id,
                status=status,
                usage=self.usage or TokenUsage(),
                iteration=self.iteration,
                user_input=self.user_input,
                agent_output=self._agent_output(),
                model_used=self.model,
                assistant_turn_count=self._assistant_turns,
                messages=self.messages,
                num_turns=self._assistant_turns,
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - self.turn_start_time,
            )
        )

        if crashed:
            self._agent._capture_partial_turn(self.collector)
