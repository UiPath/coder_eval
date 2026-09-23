"""Antigravity agent implementation using the official google-antigravity SDK.

Drives Google's Antigravity agent *local harness* (the bundled ``localharness``
binary) via the SDK's :class:`LocalAgentConfig`, authenticating against the
Gemini Developer API with ``GEMINI_API_KEY`` and running entirely on the local
machine — so coder_eval's on-disk success criteria see the agent's writes exactly
as they do for Claude and Codex.

It is the only non-deprecated surface that satisfies headless + GEMINI_API_KEY +
local execution: the branded ``agy`` CLI cannot authenticate headlessly with an
API key, and the Interactions API runs in a REMOTE sandbox whose edits never land
in our dir.

All SDK imports are lazy (inside ``start`` / helpers), mirroring CodexAgent, so
this module imports cleanly when the optional ``[antigravity]`` extra is absent;
a missing SDK surfaces as a clear install hint at ``start()``.
"""

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

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
    AntigravityAgentConfig,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    DirectRoute,
    SystemPromptSemantics,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.pricing import calculate_cost
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
from coder_eval.timing import TurnClock, close_window
from coder_eval.utils import expand_env_vars


logger = logging.getLogger(__name__)

# Fallback when a task pins no ``agent.model`` and neither ``--model`` nor
# ``ANTIGRAVITY_MODEL`` is set: Antigravity 2.0's own default coding model.
_DEFAULT_MODEL = "gemini-3.5-flash"

# How often to re-check for progress once an orphaned (backgrounded) tool call is
# detected. `Conversation.wait_for_wakeup()` is an unimplemented stub on this
# SDK's Local harness, so the agent polls itself. A tuning constant, not a knob.
# Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
_BACKGROUND_POLL_INTERVAL_SECONDS = 5.0

# Retries for a receive_steps() call that hits the SDK's re-entrancy guard; each
# yields one event-loop turn for the prior drain's cleanup to land. Confirmed live
# to clear within 2 turns, so this is a 2.5x margin rather than a tuned budget.
# Rationale: .claude/notes/agents.md § The receive_steps re-entrancy window
_RECEIVE_STEPS_REENTRY_RETRIES = 5

# Fraction of the turn's `timeout` the poll loop may spend waiting on a
# backgrounded tool call before finalizing through its OWN graceful path
# (force-close the orphan, grade normally). A FRACTION, never `timeout` itself: a
# check against the identical value races the ThreadedWatchdog for who fires
# first, while an earlier deadline reliably wins.
# Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
_POLL_DEADLINE_TIMEOUT_FRACTION = 0.8

# Cap on poll *cycles* -- the SOLE bound when a task sets no timeout at all, and
# a backstop against a very large one. 120 * 5s = 10 minutes, ~2x the worst real
# backgrounded-job duration observed (60-300s). Deliberately NOT "break after N
# consecutive empty polls".
# Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
_MAX_BACKGROUND_POLLS = 120

# Antigravity builtin tool name -> the canonical (Claude) vocabulary every
# criterion is written against. Unmapped names pass through unchanged.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
_ANTIGRAVITY_TO_CLAUDE_TOOL_MAP: dict[str, str] = {
    "run_command": "Bash",
    "create_file": "Write",
    "edit_file": "Edit",
    "view_file": "Read",
    "search_directory": "Grep",
    "find_file": "Glob",
    "list_directory": "LS",
    "start_subagent": "Task",
    "search_web": "WebSearch",
    "generate_image": "GenerateImage",
    "ask_question": "AskUser",
    "finish": "Finish",
}

# Tool-call arg keys the harness ADDS at completion (the result payload), not
# model-supplied inputs. The STATIC backstop; ``_params`` also strips any key
# that first appears at DONE. A leaked result would false-positive
# ``skill_triggered``, which substring-searches every parameter value.
_RESULT_ARG_KEYS: frozenset[str] = frozenset(
    {"exit_code", "combined_output", "diff_block", "output", "stdout", "stderr", "result", "results", "summary"}
)

# Antigravity per-tool INPUT-arg key -> canonical (Claude) key, keyed by the
# canonical tool name (post tool-name mapping). Unlisted keys pass through.
_ANTIGRAVITY_ARG_RENAME: dict[str, dict[str, str]] = {
    "Bash": {"command_line": "command"},
    "LS": {"directory_path": "path"},
}

# Step{Status,Type,Source,Target} VALUES we branch on, mirrored as plain strings
# so this module needs no SDK import. Named constants, not bare literals, so a
# StepStatus.ERROR compare is not mistaken for a FinalStatus denylist (CE018).
_STATUS_ACTIVE = "ACTIVE"
_STATUS_DONE = "DONE"
_STATUS_ERROR = "ERROR"
_TYPE_THINKING = "THINKING"
_TYPE_TEXT_RESPONSE = "TEXT_RESPONSE"
_SOURCE_MODEL = "MODEL"
_TARGET_USER = "TARGET_USER"


def _enum_value(x: Any) -> Any:
    """Return a (possibly str-enum) value as its plain ``.value``, else itself."""
    return getattr(x, "value", x)


def _to_token_usage(usage: Any, model: str | None) -> TokenUsage:
    """Map a ``google.antigravity.types.UsageMetadata`` to coder_eval ``TokenUsage``.

    Gemini reports ``prompt`` (with ``cached`` a subset), ``candidates`` and
    ``thoughts``; cache_creation is 0 because Gemini bills no cache-write fee, and
    output folds in thinking because Gemini bills it as output.

    Rationale: .claude/notes/agents.md § Token accounting, per harness
    """
    prompt = getattr(usage, "prompt_token_count", 0) or 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    candidates = getattr(usage, "candidates_token_count", 0) or 0
    thoughts = getattr(usage, "thoughts_token_count", 0) or 0
    uncached_input = max(prompt - cached, 0)
    output = candidates + thoughts
    cost = calculate_cost(model, uncached_input, output, 0, cached) if model else None
    return TokenUsage(
        uncached_input_tokens=uncached_input,
        output_tokens=output,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached,
        total_cost_usd=cost,
    )


@AgentRegistry.register(AgentKind.ANTIGRAVITY, AntigravityAgentConfig)
class AntigravityAgent(Agent[AntigravityAgentConfig]):
    """Implementation of the Agent interface for Google Antigravity (Gemini)."""

    # The step loop has a between-steps guard where `should_stop` runs.
    supports_cooperative_stop: ClassVar[bool] = True

    # TemplatedSystemInstructions wraps system_instructions around the harness's
    # own prompt, and always has — so runs ARE comparable across the marker.
    # Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
    system_prompt_semantics: ClassVar[SystemPromptSemantics] = "append"

    def __init__(
        self,
        config: AntigravityAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "antigravity",
    ):
        """Initialize the Antigravity agent.

        Args:
            config: Agent configuration.
            route: API routing configuration (unused — Antigravity authenticates via
                GEMINI_API_KEY against the Gemini Developer API; kept for parity).
            instance_name: Short label used to prefix this instance's log records.
        """
        self.config = config
        self.route = route or DirectRoute()
        self.working_directory: Path | None = None
        # The live SDK Agent session + its AsyncExitStack. The exit-stack teardown
        # terminates the localharness subprocess, which is what stop()/kill() rely
        # on.
        self._sdk_agent: Any = None
        self._exit_stack: AsyncExitStack | None = None
        # Dirs prepended to PATH so sandbox mock CLIs shadow real ones for the
        # harness's run_command tool (see _harness_env).
        self._env_path_prepend: list[str] = []
        # Turn-lifecycle bookkeeping lives on the Agent base class.
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})

    def _effective_model(self) -> str:
        """Resolve the model: task ``agent.model`` > ``ANTIGRAVITY_MODEL`` > default."""
        return self.config.model or settings.antigravity_model or _DEFAULT_MODEL

    def _resolve_skills_paths(self, plugin_tools_dir: str | None) -> list[str]:
        """Resolve skill search-path roots for the harness's native ``skills_paths``.

        For each ``type: local`` plugin path (env-expanded) plus the runtime
        ``plugin_tools_dir``, hands the harness the directory that DIRECTLY parents
        skill dirs — ``<source>/skills`` or ``<source>`` itself, whichever holds a
        ``<skill>/SKILL.md``. Unlike Codex, Antigravity takes search paths, so no
        symlinking is needed.

        Rationale: .claude/notes/agents.md § Skills, per harness
        """
        sources: list[Path] = []
        for plugin in self.config.plugins or []:
            if not (isinstance(plugin, dict) and plugin.get("type") == "local"):
                continue
            raw = plugin.get("path")
            if not raw:
                continue
            expanded = expand_env_vars(raw)
            path = Path(expanded)
            if path.is_dir():
                sources.append(path)
            else:
                # Loud: an unresolved env var or a missing dir drops the skills
                # silently, so the agent runs blind.
                hint = "env var likely unset" if "$" in expanded else "path does not exist"
                self._log.warning("Plugin skills path did not resolve: %r → %r (%s)", raw, expanded, hint)
        if plugin_tools_dir and Path(plugin_tools_dir).is_dir():
            sources.append(Path(plugin_tools_dir))

        roots: list[str] = []
        seen: set[str] = set()
        for source in sources:
            # Prefer the nested ``skills/`` layout (repo root) over the source itself.
            for candidate in (source / "skills", source):
                if candidate.is_dir() and any(
                    (child / "SKILL.md").exists() for child in candidate.iterdir() if child.is_dir()
                ):
                    resolved = str(candidate.resolve())
                    if resolved not in seen:
                        seen.add(resolved)
                        roots.append(resolved)
                    break  # first matching layout per source wins
        if sources and not roots:
            self._log.warning(
                "0 skills discovered under %s; check the plugin path points at a skills repo root",
                [str(s) for s in sources],
            )
        else:
            self._log.debug("Antigravity skills_paths resolved: %s", roots)
        return roots

    def _resolve_workspaces(self, skills_paths: list[str]) -> list[str]:
        """Workspace roots for the harness's ``workspace_only`` file-tool policy.

        The sandbox working directory (the write target) plus the resolved skill
        roots. ``skills_paths`` drives DISCOVERY only; the file-tool allowlist is
        ``workspaces`` alone, so a skill root missing here is discovered and then
        denied on every read of its ``SKILL.md``.
        """
        return [str(self.working_directory), *skills_paths]

    def _harness_env(self) -> dict[str, str] | None:
        """Per-agent environment for the localharness subprocess (``LocalAgentConfig.env``).

        Returns the mock-CLI PATH prepend as a one-key overlay, or ``None`` when no
        mock dirs are configured. The SDK merges it over ``os.environ`` at spawn,
        so naming only ``PATH`` leaves every other inherited variable untouched.
        The same overlay becomes the harness's ``run_command`` environment.
        """
        if not self._env_path_prepend:
            return None
        # Match the parent's own casing (Windows exports ``Path``) so the merge
        # overrides the inherited entry instead of adding a sibling key.
        path_key = next((k for k in os.environ if k.upper() == "PATH"), "PATH")
        merged = os.pathsep.join([*self._env_path_prepend, os.environ.get(path_key) or ""])
        return {path_key: merged}

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        """Initialize and start the Antigravity agent's local harness session.

        Args:
            working_directory: The sandbox dir, and the primary ``workspace`` so
                writes and run_command operate there — process-cwd-independent, so
                concurrent host-mode tasks don't race.
            env_path_prepend: Dirs prepended to PATH so mock CLIs shadow the real
                ones (the shared mock-shadowing contract), delivered through the
                SDK's per-agent ``env`` seam so concurrent tasks get genuinely
                separate environments rather than a time-sliced global one.
            plugin_tools_dir: A skills/plugin source root, resolved together with
                ``config.plugins`` into the harness's native ``skills_paths``.
        """
        self.working_directory = Path(working_directory)
        self._env_path_prepend = list(env_path_prepend or [])
        self._state = AgentState.WORKING

        try:
            from google.antigravity import Agent as SdkAgent  # pyright: ignore[reportMissingImports]
            from google.antigravity import LocalAgentConfig, types  # pyright: ignore[reportMissingImports]
            from google.antigravity.hooks import policy  # pyright: ignore[reportMissingImports]
        except ImportError as e:
            raise RuntimeError(
                "Antigravity SDK not installed. Install with: pip install 'coder-eval[antigravity]'"
            ) from e

        try:
            # None lets the SDK read GEMINI_API_KEY itself, and raise a clear
            # error if truly unset.
            api_key = os.getenv("GEMINI_API_KEY") or None
            skills_paths = self._resolve_skills_paths(plugin_tools_dir)
            cfg = LocalAgentConfig(
                model=self._effective_model(),
                api_key=api_key,
                # File tools are confined to ``workspaces`` by the auto-prepended
                # workspace_only policy — see _resolve_workspaces for why the skill
                # roots must be in here and not only in ``skills_paths``.
                workspaces=self._resolve_workspaces(skills_paths),
                # Autonomous execution: approve every tool call, which the default
                # policy would deny. ``permission_mode`` is deliberately NOT mapped
                # here — it does not confine this agent, exactly as on Codex, and
                # docs/agents/HARNESS_PARITY.md says so rather than leaving it
                # silent. The isolation boundary is the driver.
                policies=[policy.allow_all()],
                system_instructions=self.config.system_prompt or None,
                # Skill discovery: the search-path roots that parent the skill dirs.
                skills_paths=skills_paths,
                # Mock-CLI PATH shadowing, per agent: two concurrent tasks never
                # see each other's mock dirs.
                env=self._harness_env(),
            )
            # Thinking level onto every resolved model's endpoint. The SDK
            # validates the model list in a model_validator, so options are set on
            # the resolved targets after build.
            level = types.ThinkingLevel(self.config.thinking_level)
            for target in cfg.models or []:
                endpoint = getattr(target, "endpoint", None)
                if isinstance(endpoint, types.GeminiAPIEndpoint):
                    endpoint.options = types.GeminiModelOptions(thinking_level=level)

            # Boots the localharness subprocess + opens the conversation. Held
            # open across communicate() calls, closed in stop().
            self._exit_stack = AsyncExitStack()
            self._sdk_agent = await self._exit_stack.enter_async_context(SdkAgent(cfg))
            self._log.debug("Antigravity local harness started (model=%s)", self._effective_model())
        except Exception as e:
            await self._teardown()
            raise RuntimeError(f"Failed to start Antigravity agent: {e}") from e

    async def _drain(
        self,
        conversation: Any,
        state: "_AntigravityTurnState",
        should_stop: Callable[[], bool] | None,
    ) -> None:
        """Consume one ``receive_steps()`` cycle onto ``state``, honoring a
        cooperative stop mid-stream. Shared by the initial drain and each poll
        cycle's re-drain, so this shape lives in one place.

        A cooperative-stop ``break`` can leave the SDK connection "receiving" for
        a short bounded window, and the NEXT ``receive_steps()`` call raises
        ``RuntimeError`` inside it. The retry below yields an event-loop turn for
        the already-scheduled generator finalizer to run.

        Rationale: .claude/notes/agents.md § The receive_steps re-entrancy window
        """
        for attempt in range(_RECEIVE_STEPS_REENTRY_RETRIES):
            try:
                async with contextlib.aclosing(conversation.receive_steps()) as steps:
                    async for step in steps:
                        state.process_step(step)
                        if should_stop is not None and should_stop():
                            state.stopped_early_hit = True
                            self._log.debug("Cooperative stop requested; ending step loop at this boundary")
                            break
                        # The turn cap shares this boundary: the step that reached
                        # the cap is kept whole, the next is never pulled. After the
                        # cooperative stop, so an armed early-stop wins a tie.
                        if state.max_turns_reached():
                            state.max_turns_hit = True
                            self._log.debug(
                                "max_turns (%s API calls) reached; ending step loop",
                                state.max_turns,
                            )
                            break
                return
            except RuntimeError:
                if attempt == _RECEIVE_STEPS_REENTRY_RETRIES - 1:
                    raise
                self._log.debug(
                    "receive_steps() re-entrancy guard still set from a prior drain; retrying (attempt %d)",
                    attempt + 1,
                )
                await asyncio.sleep(0)

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        """Send a message to the Antigravity agent and receive its response.

        ``should_stop`` is the cooperative early-stop callback, polled after each
        processed step. When it returns True the step loop breaks, the
        conversation is cancelled (best-effort) and the turn finalizes cleanly as
        ``STOPPED_EARLY`` (``crashed=False``).

        ``max_turns`` caps main-thread model API calls, Claude Code's unit,
        enforced in-stream on the same boundary as the cooperative stop.
        See docs/agents/HARNESS_PARITY.md.

        Drives one logical turn: ``conversation.send(prompt)`` then iterate
        ``receive_steps()`` until the turn goes idle.

        Raises:
            RuntimeError: If the agent is not started.
            TurnTimeoutError: Timeout elapsed (partial TurnRecord on pending_turn).
            AgentCrashError: SDK/harness failed mid-turn (same pending_turn contract).
        """
        if not self.working_directory or self._sdk_agent is None:
            raise RuntimeError("Agent not started. Call start() first.")

        assert self.config.type is not None, "AntigravityAgent requires AgentConfig.type before communicate()"

        self._begin_turn()
        # Raw monotonic, deliberately NOT the turn clock: this seeds the poll
        # deadline and `duration_seconds`, neither of which may move when the wall
        # clock steps. `TurnClock` is for the RECORDED stamps.
        turn_start_time = time.monotonic()
        # ONE clock per turn, so the window bounds and the tool intervals
        # subtracted from them share a basis.
        clock = TurnClock()
        task_id = str(self.config.type)
        model = self._effective_model()
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])
        turn_id = f"antigravity-{self._iteration}"

        state = _AntigravityTurnState(
            agent=self,
            emit=emit,
            task_id=task_id,
            turn_id=turn_id,
            collector=collector,
            user_input=user_input,
            iteration=self._iteration,
            model=model,
            turn_start_time=turn_start_time,
            clock=clock,
            max_turns=max_turns,
        )

        try:
            # From the TURN CLOCK, not the model's raw `datetime.now()` default:
            # this bound is subtracted against window bounds the same clock
            # produced, and two bases in one subtraction clamped this harness's
            # -0.017 ms tail to a measured 0.0 (CE058).
            emit.on_event(
                AgentStartEvent(
                    task_id=task_id,
                    prompt=user_input,
                    iteration=self._iteration,
                    model=model,
                    timestamp=clock.now(),
                )
            )

            def _on_turn_timeout() -> None:
                state.timeout_hit = True

            with ThreadedWatchdog(
                timeout_seconds=timeout,
                on_timeout=_on_turn_timeout,
                asyncio_task_to_cancel=asyncio.current_task(),
                label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
            ):
                emit.on_event(TurnStartEvent(task_id=task_id, turn_id=turn_id, model=model))
                conversation = self._sdk_agent.conversation
                poll_count = 0
                # Bound the poll loop's OWN exit earlier than the watchdog's, so
                # its graceful path reliably wins that race. `timeout=None` has
                # nothing to take a fraction of, so the cycle cap is the sole bound.
                poll_deadline = turn_start_time + timeout * _POLL_DEADLINE_TIMEOUT_FRACTION if timeout else None
                try:
                    await conversation.send(user_input)
                    # should_stop runs AFTER process_step (the emission the watcher
                    # latches on) and BEFORE the next step is pulled.
                    await self._drain(conversation, state, should_stop)

                    # The model may background a run_command and go idle, so
                    # receive_steps() exhausts with that call still open. Gated on
                    # the orphaned-tool signal, so a normal turn takes this branch
                    # zero times.
                    # Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
                    while (
                        not state.stopped_early_hit
                        and not state.max_turns_hit
                        and not state.timeout_hit
                        and state.has_orphaned_tool_call()
                        and (
                            poll_count < _MAX_BACKGROUND_POLLS
                            if poll_deadline is None
                            else time.monotonic() < poll_deadline
                        )
                    ):
                        poll_count += 1
                        self._log.debug("Polling for backgrounded work (orphaned tool call); attempt %d", poll_count)
                        await asyncio.sleep(_BACKGROUND_POLL_INTERVAL_SECONDS)
                        if state.timeout_hit or (poll_deadline is not None and time.monotonic() >= poll_deadline):
                            # Skip the re-drain, which could itself await
                            # indefinitely on genuinely non-idle work.
                            break
                        if should_stop is not None and should_stop():
                            state.stopped_early_hit = True
                            break
                        # A re-drain honors the turn cap too (the check lives in
                        # _drain), so a poll cycle can be the one that reaches it.
                        await self._drain(conversation, state, should_stop)

                    if (
                        state.has_orphaned_tool_call()
                        and not state.stopped_early_hit
                        and not state.max_turns_hit
                        and not state.timeout_hit
                    ):
                        # Exited via this loop's OWN bound, not an external
                        # stop/timeout: the call is force-closed as unresolved and
                        # the turn is still graded normally on everything else.
                        bound = (
                            f"poll_deadline ({_POLL_DEADLINE_TIMEOUT_FRACTION:.0%} of {timeout:g}s turn timeout)"
                            if poll_deadline is not None
                            else f"_MAX_BACKGROUND_POLLS ({_MAX_BACKGROUND_POLLS})"
                        )
                        msg = "Poll budget exhausted (%s, poll_count=%d) with a tool call still ACTIVE."
                        self._log.warning(msg, bound, poll_count)

                    if state.stopped_early_hit or state.max_turns_hit:
                        # Best-effort server-side cancel. One check point, so it
                        # fires exactly once whichever drain stopped.
                        with contextlib.suppress(Exception):
                            await conversation.cancel()
                except asyncio.CancelledError:
                    if state.timeout_hit:
                        self._finalize_and_raise_timeout(state.finalize, timeout or 0)
                    raise
                except Exception as e:
                    if state.timeout_hit:
                        self._finalize_and_raise_timeout(state.finalize, timeout or 0, cause=e)
                    if state.ended_cleanly:
                        # Already stopped on purpose — do not escalate.
                        # Rationale: .claude/notes/agents.md § Why a post-stop exception is not a crash
                        self._log.warning("Ignoring post-stop exception; finalizing cleanly: %s", e)
                    else:
                        self._finalize_and_raise_crash(
                            state.finalize, truncate_crash_message(f"Antigravity turn failed: {e!s}"), cause=e
                        )

            if state.timeout_hit:
                # Watchdog fired but the pump finished before the cancel landed.
                assert timeout is not None
                self._finalize_and_raise_timeout(state.finalize, timeout)
        except (AgentCrashError, TurnTimeoutError):
            raise
        except asyncio.CancelledError:
            if not state.finalized:
                self._finalize_external_cancel(state.finalize)
            raise
        except Exception as e:
            if state.ended_cleanly and not state.timeout_hit:
                # Same retry-poisoning guard as the inner handler.
                self._log.warning("Ignoring post-stop exception; finalizing cleanly: %s", e)
            else:
                self._finalize_and_raise_crash(
                    state.finalize, truncate_crash_message(f"Antigravity turn failed: {e!s}"), cause=e
                )

        self._state = AgentState.WORKING
        self._end_turn_ok()
        # Precedence: timeout (raised above) > stopped_early > max_turns > done.
        # Rationale: .claude/notes/agents.md § Shared turn lifecycle
        if state.stopped_early_hit:
            status = AgentEndStatus.STOPPED_EARLY
        elif state.max_turns_hit:
            status = AgentEndStatus.MAX_TURNS_EXHAUSTED
        else:
            status = AgentEndStatus.COMPLETED
        state.finalize(status, crashed=False, crash_reason=None)
        return collector.build_turn_record()

    async def stop(self) -> None:
        """Stop the agent and tear down the local harness session."""
        await self._teardown()
        self._mark_stopped()

    async def kill(self) -> None:
        """Force-terminate: cancel any in-flight turn, then tear down the harness."""
        conversation = self._conversation_or_none()
        if conversation is not None:
            with contextlib.suppress(Exception):
                await conversation.cancel()
        await self.stop()

    def kill_sync(self) -> None:
        """Best-effort synchronous abort for the watchdog thread (cannot await).

        Antigravity's cancel/disconnect are async-only, so the genuine teardown
        happens via the watchdog's asyncio-task cancel and the subsequent
        ``stop()`` exit-stack close. This hook only records intent.
        """
        self._state = AgentState.ERROR

    def get_environment_info(self) -> dict[str, Any]:
        """Record the resolved Gemini model + thinking level for auditability."""
        return {
            **super().get_environment_info(),
            "antigravity_model": self._effective_model(),
            "antigravity_thinking_level": self.config.thinking_level,
        }

    def _conversation_or_none(self) -> Any:
        agent = self._sdk_agent
        if agent is None:
            return None
        with contextlib.suppress(Exception):
            return agent.conversation if agent.is_started else None
        return None

    async def _teardown(self) -> None:
        """Close the SDK Agent context (reaps the localharness subprocess)."""
        stack = self._exit_stack
        self._exit_stack = None
        self._sdk_agent = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()


class _AntigravityTurnState:
    """Per-turn mutable scratch for one ``AntigravityAgent.communicate`` call.

    Maps the Gemini step stream onto the standardized event protocol and
    reconstructs the assistant transcript. The same ``messages`` / ``commands``
    accumulate live, so a mid-turn crash keeps the partial transcript (the
    agent's shared crash kernel builds ``pending_turn`` from ``collector``).

    Step-stream shape this consumes (observed): each ``step_index`` is yielded
    repeatedly through ACTIVE -> DONE transitions; ``usage_metadata`` lands once
    per generation on a DONE/terminal step (summing them == the turn total); a
    tool call carries a stable ``id`` and its result is folded into expanded
    ``args`` at DONE.
    """

    def __init__(
        self,
        *,
        agent: AntigravityAgent,
        emit: CompositeStreamCallback,
        task_id: str,
        turn_id: str,
        collector: EventCollector,
        user_input: str,
        iteration: int,
        model: str,
        turn_start_time: float,
        clock: TurnClock,
        max_turns: int | None = None,
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
        # Injected, not read from a module global, so a test supplies a fake
        # instead of monkeypatching `datetime` out from under the reducer.
        self.clock = clock

        self.max_turns = max_turns
        self.timeout_hit = False
        self.stopped_early_hit = False
        self.max_turns_hit = False
        self.finalized = False

        self.total_usage = TokenUsage()
        self.messages: list[TranscriptMessage] = []
        self.commands: list[CommandTelemetry] = []
        self._output_parts: list[str] = []
        self._assistant_turns = 0
        # Main-thread API calls begun; one spans its first new MODEL step to its usage.
        self.api_calls = 0
        self._in_api_call = False
        self._seen_steps: set[tuple[str, Any]] = set()

        # ToolStart on first sight of an id; ToolEnd at DONE.
        self._next_seq = 0
        self._seen_tools: set[str] = set()
        self._closed_tools: set[str] = set()
        self._open_tools: dict[str, CommandTelemetry] = {}
        # Arg keys present when a tool was first seen (its model-supplied
        # inputs), used at DONE to tell them from harness-appended result fields.
        self._tool_input_keys: dict[str, set[str]] = {}
        # Most recently seen StepStatus per tool id, for has_orphaned_tool_call.
        # Separate from _closed_tools, which tracks only DONE/ERROR.
        self._tool_last_status: dict[str, Any] = {}
        # Content blocks accumulated since the last per-generation flush.
        self._blocks: list[ContentBlock] = []
        # Where the CURRENT generation started, advanced only by a flush that
        # actually emitted a message.
        self._gen_mark_wall: datetime = clock.now()
        # Re-seeded ONCE, at the first observed Step. See
        # `_seed_first_generation_window`.
        self._first_output_seen: bool = False

    @property
    def ended_cleanly(self) -> bool:
        """True once the loop broke on purpose (cooperative stop or the turn cap).

        Both are non-crash terminations, so a stray exception raised while
        unwinding the step generator afterwards must not be escalated.
        """
        return self.stopped_early_hit or self.max_turns_hit

    def max_turns_reached(self) -> bool:
        """True once the model begins API call ``max_turns + 1``, the unit Claude Code's ``--max-turns`` caps.

        The next call opens only after the previous call's tools finish, so every
        call under the cap keeps its tool results.
        """
        return self.max_turns is not None and self.api_calls > self.max_turns

    def _seed_first_generation_window(self, source: Any) -> None:
        """Move the first window's mark to the first observed MODEL output.

        GATED ON ``source``, because ``harness_startup_ms`` is defined as model
        output and the SDK streams Steps that are not: a turn can legitimately open
        with a SYSTEM or USER Step, and seeding on one would put the mark BEFORE
        the model spoke. The same gate guards text streaming below.

        ONCE PER TURN, and that is the whole contract: re-seeding would stop the
        windows tiling. The flag needs no reset — a fresh turn state is built per
        ``communicate()``. A turn that streams no MODEL Step keeps the turn-entry
        mark and clamps to ``0.0``, which is the correct degradation.

        Rationale: .claude/notes/agents.md § First-generation window seeding
        """
        if self._first_output_seen or _enum_value(source) != _SOURCE_MODEL:
            return
        self._first_output_seen = True
        self._gen_mark_wall = self.clock.now()

    def process_step(self, step: Any) -> None:
        """Route one streamed ``Step`` to events + transcript reconstruction."""
        stype = _enum_value(step.type)
        sstatus = _enum_value(step.status)
        ssource = _enum_value(step.source)
        self._seed_first_generation_window(ssource)
        step_key = (getattr(step, "trajectory_id", "") or "", step.step_index)
        if ssource == _SOURCE_MODEL and step_key not in self._seen_steps and not self._in_api_call:
            self._in_api_call = True
            self.api_calls += 1
        self._seen_steps.add(step_key)
        starget = _enum_value(step.target)
        done = sstatus in (_STATUS_DONE, _STATUS_ERROR)

        # Stream visible assistant text deltas.
        if step.content_delta and ssource == _SOURCE_MODEL and starget == _TARGET_USER and stype == _TYPE_TEXT_RESPONSE:
            self.emit.on_event(TextChunkEvent(task_id=self.task_id, turn_id=self.turn_id, text=step.content_delta))

        # Tool calls: ToolStart on first sight, ToolEnd when the owning step is DONE.
        for call_index, call in enumerate(step.tool_calls):
            self._handle_tool_call(call, step, done, sstatus, call_index)

        # Capture content blocks on the terminal transition of a step.
        if done:
            if stype == _TYPE_THINKING and step.thinking:
                self._blocks.append(ContentBlock(block_type="thinking", sequence=0, thinking=step.thinking))
            elif stype == _TYPE_TEXT_RESPONSE and step.content:
                self._output_parts.append(step.content)
                self._blocks.append(ContentBlock(block_type="text", sequence=0, text=step.content))

        # Per-generation usage: fold into the turn total and cut an AssistantMessage.
        if step.usage_metadata is not None:
            if not self._in_api_call:
                self.api_calls += 1
            self._in_api_call = False
            gen = _to_token_usage(step.usage_metadata, self.model)
            self.total_usage = self.total_usage + gen
            self._flush_generation(gen, getattr(step.usage_metadata, "thoughts_token_count", 0) or 0)

    def _handle_tool_call(self, call: Any, step: Any, done: bool, sstatus: Any, call_index: int) -> None:
        raw_name = _enum_value(call.name)
        # call.id is usually present but the SDK types it optional. The fallback
        # mirrors the SDK's own `trajectory_id:step_index` scheme; call_index
        # further disambiguates multiple id-less calls within one step.
        # Rationale: .claude/notes/agents.md § Why the tool-call id falls back the way it does
        trajectory_id = getattr(step, "trajectory_id", "") or ""
        step_key = f"{trajectory_id}:{step.step_index}" if trajectory_id else str(step.step_index)
        cid = call.id or f"{raw_name}_{step_key}_{call_index}"
        self._tool_last_status[cid] = sstatus
        if cid not in self._seen_tools:
            self._seen_tools.add(cid)
            seq = self._next_seq
            self._next_seq += 1
            tool_name = _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP.get(raw_name, str(raw_name))
            self._tool_input_keys[cid] = set(call.args)
            now = self.clock.now()
            tel = CommandTelemetry(
                tool_name=tool_name,
                tool_id=cid,
                timestamp=now,
                parameters=self._params(tool_name, call.args, self._tool_input_keys[cid]),
                sequence_number=seq,
                execution_started_at=now,
            )
            self._open_tools[cid] = tel
            self.emit.on_event(ToolStartEvent(task_id=self.task_id, turn_id=self.turn_id, tool=tel))

        if done and cid in self._open_tools and cid not in self._closed_tools:
            self._closed_tools.add(cid)
            start_tel = self._open_tools[cid]
            exit_code = call.args.get("exit_code")
            errored = (sstatus == _STATUS_ERROR) or (exit_code not in (None, 0))
            result_text = (
                call.args.get("combined_output")
                or call.args.get("diff_block")
                or call.args.get("output")
                or call.args.get("results")
                or call.args.get("summary")
                or step.content
                or None
            )
            completed = self.clock.now()
            started = start_tel.execution_started_at or completed
            tool_ms = max((completed - started).total_seconds() * 1000.0, 0.0)
            end_tel = start_tel.model_copy(
                update={
                    "parameters": self._params(start_tel.tool_name, call.args, self._tool_input_keys.get(cid)),
                    "result_status": "error" if errored else "success",
                    "result_summary": str(result_text) if result_text is not None else None,
                    "error_message": (step.error or "tool failed") if errored else None,
                    "execution_completed_at": completed,
                    "duration_ms": tool_ms,
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
            self._blocks.append(ContentBlock(block_type="tool_use", sequence=0, tool_use_id=cid))

    @staticmethod
    def _params(tool_name: str, args: dict[str, Any], input_keys: set[str] | None) -> dict[str, Any]:
        """Model-supplied inputs only, renamed to canonical cross-agent keys.

        A key is a (dropped) result field when it is in the static
        ``_RESULT_ARG_KEYS`` backstop OR first appeared at DONE, given the
        input-key snapshot taken at tool start. Survivors are renamed to the
        canonical vocabulary.
        """
        rename = _ANTIGRAVITY_ARG_RENAME.get(tool_name, {})
        out: dict[str, Any] = {}
        for k, v in args.items():
            if k in _RESULT_ARG_KEYS:
                continue
            if input_keys is not None and k not in input_keys:
                continue  # appeared only at DONE → harness result payload
            out[rename.get(k, k)] = v
        return out

    def _flush_generation(self, gen: TokenUsage, reasoning_tokens: int) -> None:
        """Cut accumulated blocks into one AssistantMessage carrying this gen's tokens.

        Keeping the per-message buckets summing to the turn total means the
        collector's reconciliation books a zero residual.
        """
        if not self._blocks and gen.is_empty():
            return
        now_wall = self.clock.now()
        # Do NOT "simplify" this to resetting the mark when a tool ends: this
        # harness interleaves a tool INTO a window rather than tiling around it,
        # so the RAW window legitimately contains time that is not model time and
        # the collector clips the tool union out of it. Resetting instead drops
        # the model time around a fast tool.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        _, generation_ms = close_window(mark=self._gen_mark_wall, now=now_wall)
        for i, block in enumerate(self._blocks):
            block.sequence = i
        self.messages.append(
            AssistantMessage(
                started_at=self._gen_mark_wall,
                completed_at=now_wall,
                generation_duration_ms=generation_ms,
                content_blocks=list(self._blocks),
                tool_use_ids=[b.tool_use_id for b in self._blocks if b.block_type == "tool_use" and b.tool_use_id],
                input_tokens=gen.uncached_input_tokens,
                output_tokens=gen.output_tokens,
                cache_creation_tokens=0,
                cache_read_tokens=gen.cache_read_input_tokens,
                reasoning_tokens=reasoning_tokens,
                model=self.model,
                # The Step stream carries no message id, and the evalboard's
                # gap fallback cannot split contiguous windows.
                message_id=f"{self.turn_id}-msg-{self._assistant_turns}",
            )
        )
        self._assistant_turns += 1
        self._blocks = []
        # Advance ONLY after a message was appended: a no-op flush leaves the
        # window open, so a later real generation still measures from its start.
        self._gen_mark_wall = now_wall

    def _agent_output(self) -> str:
        if self._output_parts:
            return "".join(self._output_parts)
        with contextlib.suppress(Exception):
            return self._agent._sdk_agent.conversation.last_response  # type: ignore[union-attr]
        return ""

    def has_orphaned_tool_call(self) -> bool:
        """True if any NOT-YET-CLOSED tool call's most recently seen status is
        ACTIVE — the structural signature of a backgrounded task the model went
        idle on without waiting for. See ``communicate``'s poll loop.

        An ALLOWLIST on ACTIVE, never a denylist on "not yet closed": the SDK also
        has WAITING_FOR_USER, CANCELED and UNKNOWN, none of which the poll loop
        should wait out. The `not in _closed_tools` guard is layered on top as a
        monotonicity backstop, not a substitute.

        Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
        """
        return any(cid not in self._closed_tools and s == _STATUS_ACTIVE for cid, s in self._tool_last_status.items())

    def finalize(self, status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None) -> None:
        """Close orphaned tools, flush leftover blocks, emit TurnEnd + AgentEnd.

        Idempotent. On a crash, also builds the partial ``pending_turn`` from the
        collector (the agent base's shared crash kernel).
        """
        if self.finalized:
            return
        self.finalized = True

        # Force-close any tool that emitted ToolStart but never reached DONE.
        for cid, tel in self._open_tools.items():
            if cid in self._closed_tools:
                continue
            orphan = tel.model_copy(update={"result_status": "unknown", "execution_completed_at": self.clock.now()})
            self.emit.on_event(
                ToolEndEvent(
                    task_id=self.task_id,
                    turn_id=self.turn_id,
                    tool=orphan,
                    status=ToolEndStatus.UNRESOLVED,
                )
            )

        # Flush any trailing blocks not yet attached to a generation (no usage).
        if self._blocks:
            self._flush_generation(TokenUsage(), 0)

        # Parallel by value, so an unmapped future member raises loudly instead
        # of silently bucketing to COMPLETED.
        turn_status = TurnEndStatus(status.value)

        self.emit.on_event(
            TurnEndEvent(
                task_id=self.task_id,
                turn_id=self.turn_id,
                status=turn_status,
                tokens=self.total_usage,
            )
        )
        self.emit.on_event(
            AgentEndEvent(
                task_id=self.task_id,
                status=status,
                usage=self.total_usage,
                iteration=self.iteration,
                user_input=self.user_input,
                agent_output=self._agent_output(),
                model_used=self.model,
                assistant_turn_count=self._assistant_turns,
                messages=self.messages,
                num_turns=self.api_calls,
                crashed=crashed,
                crash_reason=crash_reason,
                max_turns_exhausted=status is AgentEndStatus.MAX_TURNS_EXHAUSTED,
                duration_seconds=time.monotonic() - self.turn_start_time,
                # One basis with the window bounds — see the AgentStartEvent site.
                timestamp=self.clock.now(),
            )
        )

        if crashed:
            self._agent._capture_partial_turn(self.collector)
