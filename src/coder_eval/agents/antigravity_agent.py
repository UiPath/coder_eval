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
from typing import Any

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._logging import PrefixedAdapter
from coder_eval.agents.registry import AgentRegistry
from coder_eval.agents.watchdog import WatchdogFired, run_with_watchdog
from coder_eval.config import settings
from coder_eval.errors.agent import format_timeout_reason
from coder_eval.models import (
    READ_ONLY_DENIED_TOOLS,
    AgentKind,
    AntigravityAgentConfig,
    ApiRoute,
    ContentBlock,
    DirectRoute,
    Enforcement,
    HarnessContract,
    PermissionMode,
    TimingBasis,
    TokenUsage,
    ToolNameMap,
    UsageGranularity,
)
from coder_eval.pricing import price_turn
from coder_eval.streaming.callbacks import StreamCallback
from coder_eval.streaming.emitter import Generation, TurnEmitter, TurnOutcome
from coder_eval.streaming.events import AgentEndStatus, StopReason, ToolEndStatus, TurnEndStatus, end_status_for
from coder_eval.timing import close_window


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
    "start_subagent": "Agent",
    "search_web": "WebSearch",
    "read_url_content": "WebFetch",
    "generate_image": "GenerateImage",
    "ask_question": "AskUser",
    "finish": "Finish",
}

# The canonical tool names Antigravity has no tool for.
_ANTIGRAVITY_NO_EQUIVALENT: frozenset[str] = frozenset({"NotebookEdit", "Skill", "TodoWrite", "ToolSearch"})

_TOOL_NAMES = ToolNameMap.from_inverse(_ANTIGRAVITY_TO_CLAUDE_TOOL_MAP, no_equivalent=_ANTIGRAVITY_NO_EQUIVALENT)

# The harness ends a turn by calling `finish`, so an allowlist never denies it.
_TURN_END_TOOL = "finish"

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
    tokens = TokenUsage(
        uncached_input_tokens=uncached_input,
        output_tokens=output,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached,
    )
    tokens.total_cost_usd = price_turn(tokens, (model,))
    return tokens


class _AntigravityDecoder:
    """One turn's reducer: Antigravity ``Step`` objects in, ``TurnEmitter`` calls out.

    Step-stream shape (observed): each ``step_index`` is yielded repeatedly through
    ACTIVE -> DONE transitions; ``usage_metadata`` lands once per generation on a
    DONE/terminal step (summing them == the turn total); a tool call carries a stable
    ``id`` and its result is folded into expanded ``args`` at DONE.
    """

    def __init__(self, emitter: TurnEmitter, *, turn_id: str = "antigravity-1") -> None:
        self.emitter = emitter
        self.turn_id = turn_id
        self.timeout_hit = False
        self.stop_reason: StopReason | None = None
        self.total_usage = TokenUsage()
        self.output_parts: list[str] = []
        self.generations = 0
        self._seen_tools: set[str] = set()
        self._closed_tools: set[str] = set()
        # Arg keys present when a tool was first seen (its model-supplied inputs),
        # used at DONE to tell them from harness-appended result fields.
        self._tool_input_keys: dict[str, set[str]] = {}
        self._tool_names: dict[str, str] = {}
        # Most recently seen StepStatus per tool id, for has_orphaned_tool_call.
        self._tool_last_status: dict[str, Any] = {}
        # Content blocks accumulated since the last per-generation flush.
        self._blocks: list[ContentBlock] = []
        # Where the CURRENT generation started, advanced only by a flush that
        # actually added a message.
        self._gen_mark: datetime = emitter.now()
        # Re-seeded ONCE, at the first observed MODEL Step.
        self._first_output_seen = False

    @property
    def ended_cleanly(self) -> bool:
        """True once the loop broke on a ``should_stop`` reason: a later exception is not a crash."""
        return self.stop_reason is not None

    def _seed_first_generation_window(self, source: Any) -> None:
        """Move the first window's mark to the first observed MODEL output, once per turn.

        Gated on ``source``: a turn can open with a SYSTEM or USER Step, and seeding
        on one would put the mark before the model spoke.

        Rationale: .claude/notes/agents.md § First-generation window seeding
        """
        if self._first_output_seen or _enum_value(source) != _SOURCE_MODEL:
            return
        self._first_output_seen = True
        self._gen_mark = self.emitter.now()

    @staticmethod
    def _is_reply(stype: Any, ssource: Any, starget: Any) -> bool:
        """The ONE test for "the model talking to the user"; a USER-source prompt echo is not."""
        return stype == _TYPE_TEXT_RESPONSE and ssource == _SOURCE_MODEL and starget == _TARGET_USER

    def __call__(self, step: Any) -> None:
        """Route one streamed ``Step`` to the emitter."""
        stype = _enum_value(step.type)
        sstatus = _enum_value(step.status)
        ssource = _enum_value(step.source)
        self._seed_first_generation_window(ssource)
        starget = _enum_value(step.target)
        done = sstatus in (_STATUS_DONE, _STATUS_ERROR)
        reply = self._is_reply(stype, ssource, starget)

        if step.content_delta and reply:
            self.emitter.text(step.content_delta)

        for call_index, call in enumerate(step.tool_calls):
            self._handle_tool_call(call, step, done, sstatus, call_index)

        if done:
            if stype == _TYPE_THINKING and step.thinking:
                self._blocks.append(ContentBlock(block_type="thinking", sequence=0, thinking=step.thinking))
            elif reply and step.content:
                self.output_parts.append(step.content)
                self._blocks.append(ContentBlock(block_type="text", sequence=0, text=step.content))

        if step.usage_metadata is not None:
            gen = _to_token_usage(step.usage_metadata, self.emitter.model)
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
            tool_name = _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP.get(raw_name, str(raw_name))
            self._tool_names[cid] = tool_name
            self._tool_input_keys[cid] = set(call.args)
            self.emitter.open_tool(cid, tool_name, self._params(tool_name, call.args, self._tool_input_keys[cid]))

        if done and cid not in self._closed_tools:
            self._closed_tools.add(cid)
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
            tool_name = self._tool_names[cid]
            self.emitter.close_tool(
                cid,
                status=ToolEndStatus.ERROR if errored else ToolEndStatus.OK,
                summary=str(result_text) if result_text is not None else None,
                error=(step.error or "tool failed") if errored else None,
                parameters=self._params(tool_name, call.args, self._tool_input_keys.get(cid)),
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
        """Cut the accumulated blocks into one generation carrying this step's tokens."""
        if not self._blocks and gen.is_empty():
            return
        now = self.emitter.now()
        # Do NOT "simplify" this to resetting the mark when a tool ends: this
        # harness interleaves a tool INTO a window rather than tiling around it,
        # so the RAW window legitimately contains time that is not model time.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        for i, block in enumerate(self._blocks):
            block.sequence = i
        self.emitter.add_generation(
            # The Step stream carries no message id; one per generation.
            message_id=f"{self.turn_id}-msg-{self.generations}",
            window=close_window(mark=self._gen_mark, now=now),
            parts=[Generation(blocks=list(self._blocks), tokens=gen, reasoning_tokens=reasoning_tokens)],
        )
        self.generations += 1
        self._blocks = []
        self._gen_mark = now

    def has_orphaned_tool_call(self) -> bool:
        """True if any NOT-YET-CLOSED tool call's most recently seen status is
        ACTIVE — the structural signature of a backgrounded task the model went
        idle on without waiting for. See ``AntigravityAgent._poll_background_work``.

        An ALLOWLIST on ACTIVE, never a denylist on "not yet closed": the SDK also
        has WAITING_FOR_USER, CANCELED and UNKNOWN, none of which the poll loop
        should wait out. The `not in _closed_tools` guard is layered on top as a
        monotonicity backstop, not a substitute.

        Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
        """
        return any(cid not in self._closed_tools and s == _STATUS_ACTIVE for cid, s in self._tool_last_status.items())

    def end(self, status: AgentEndStatus, *, reason: str | None = None, agent_output: str | None = None) -> TurnOutcome:
        """Flush trailing blocks, close the one inner turn with the turn's tokens, and end the turn.

        ``agent_output`` is the fallback when the stream carried no reply text.
        """
        if self._blocks:
            self._flush_generation(TokenUsage(), 0)
        if self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(TurnEndStatus(status.value), tokens=self.total_usage)
        output = "".join(self.output_parts) if self.output_parts else (agent_output or "")
        if status is AgentEndStatus.CRASHED or status is AgentEndStatus.TIMEOUT:
            return self.emitter.fail(
                status,
                reason or status.value,
                usage=self.total_usage,
                agent_output=output,
                assistant_turn_count=self.generations,
                num_turns=self.generations,
            )
        return self.emitter.finalize(
            status,
            usage=self.total_usage,
            agent_output=output,
            assistant_turn_count=self.generations,
            num_turns=self.generations,
        )


@AgentRegistry.register(AgentKind.ANTIGRAVITY, AntigravityAgentConfig)
class AntigravityAgent(Agent[AntigravityAgentConfig]):
    """Implementation of the Agent interface for Google Antigravity (Gemini)."""

    # The step loop has a between-steps guard where `should_stop` runs;
    # TemplatedSystemInstructions wraps system_instructions around the harness's
    # own prompt, and always has — so runs ARE comparable across the marker.
    # Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
    contract = HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.ENFORCED,
        permission_mode=Enforcement.ENFORCED,
        allowed_tools=Enforcement.ENFORCED,
        disallowed_tools=Enforcement.ENFORCED,
        cooperative_stop=True,
        usage_granularity=UsageGranularity.TURN,
        timing_basis=TimingBasis.TURN_CLOCK,
        permission_modes=frozenset({PermissionMode.PLAN, PermissionMode.BYPASS_PERMISSIONS}),
    )
    tool_names = _TOOL_NAMES

    def __init__(
        self,
        config: AntigravityAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "antigravity",
        cost_log_tags: dict[str, str] | None = None,
    ):
        """Initialize the Antigravity agent.

        Args:
            config: Agent configuration.
            route: API routing configuration (unused — Antigravity authenticates via
                GEMINI_API_KEY against the Gemini Developer API; kept for parity).
            instance_name: Short label used to prefix this instance's log records.
            cost_log_tags: LiteLLM correlation headers; accepted for factory parity, unused.
        """
        super().__init__(config, route or DirectRoute(), cost_log_tags=cost_log_tags)
        self.working_directory: Path | None = None
        # The live SDK Agent session + its AsyncExitStack. The exit-stack teardown
        # terminates the localharness subprocess, which is what stop()/kill() rely
        # on.
        self._sdk_agent: Any = None
        self._exit_stack: AsyncExitStack | None = None
        # Dirs prepended to PATH so sandbox mock CLIs shadow real ones for the
        # harness's run_command tool (see _harness_env).
        self._env_path_prepend: list[str] = []
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})

    def _effective_model(self) -> str:
        """Resolve the model: task ``agent.model`` > ``ANTIGRAVITY_MODEL`` > default."""
        return self.config.model or settings.antigravity_model or _DEFAULT_MODEL

    def _resolve_workspaces(self, plugin_root: Path | None) -> list[str]:
        """Workspace roots for the harness's ``workspace_only`` file-tool policy.

        The sandbox working directory (the write target), the staged skills dir, and
        each staged skill's resolved source: the policy canonicalizes a read through
        the stage's symlink, so without the source a discovered skill is unreadable.
        ``skills_paths`` drives DISCOVERY only; the file-tool allowlist is
        ``workspaces`` alone.
        """
        if plugin_root is None:
            return [str(self.working_directory)]
        skills_dir = plugin_root / "skills"
        sources = sorted({str(skill.resolve()) for skill in skills_dir.iterdir()})
        return [str(self.working_directory), str(skills_dir), *sources]

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

    def _policies(self, policy: Any) -> list[Any]:
        """Tool-call policies from the uniform tool fields, built with the SDK's ``policy`` module.

        No allowlist approves every call (autonomous execution; the SDK default
        would deny ``run_command``). A specific deny outranks a specific allow in
        the SDK, so a denied or ``plan``-denied tool stays denied.
        """
        if not self.config.allowed_tools:
            policies = [policy.allow_all()]
        else:
            allowed = {t for name in self.config.allowed_tools for t in _TOOL_NAMES.names[name]}
            policies = [policy.deny_all(), *(policy.allow(t) for t in sorted(allowed | {_TURN_END_TOOL}))]
        deny_names = list(self.config.disallowed_tools or [])
        if self.config.permission_mode is PermissionMode.PLAN:
            deny_names += READ_ONLY_DENIED_TOOLS
        denied = {t for name in deny_names for t in _TOOL_NAMES.names[name]} - {_TURN_END_TOOL}
        return policies + [policy.deny(t) for t in sorted(denied)]

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
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
            plugin_tools_dir: Accepted for the ``Agent.start`` signature; unused here.
            plugin_root: The staged plugin root; its ``skills/`` is the harness's
                native ``skills_paths`` entry.
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
            skills_paths = [str(plugin_root / "skills")] if plugin_root is not None else []
            cfg = LocalAgentConfig(
                model=self._effective_model(),
                api_key=api_key,
                # File tools are confined to ``workspaces`` by the auto-prepended
                # workspace_only policy — see _resolve_workspaces for why the skill
                # roots must be in here and not only in ``skills_paths``.
                workspaces=self._resolve_workspaces(plugin_root),
                policies=self._policies(policy),
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
        decoder: _AntigravityDecoder,
        should_stop: Callable[[], StopReason | None] | None,
    ) -> None:
        """Consume one ``receive_steps()`` cycle into ``decoder``, honoring a
        cooperative stop mid-stream. Shared by the initial drain and each poll
        cycle's re-drain, so this shape lives in one place.

        A cooperative-stop ``break`` can leave the SDK connection "receiving" for
        a short bounded window, and the NEXT ``receive_steps()`` call raises
        ``RuntimeError`` inside it. The retry below yields an event-loop turn for
        the already-scheduled generator finalizer to run. Only an error raised
        before this attempt pulled a step is retried; a later one is a real failure,
        and retrying it would pull and emit the same steps again.

        Rationale: .claude/notes/agents.md § The receive_steps re-entrancy window
        """
        for attempt in range(_RECEIVE_STEPS_REENTRY_RETRIES):
            pulled = False
            try:
                async with contextlib.aclosing(conversation.receive_steps()) as steps:
                    async for step in steps:
                        pulled = True
                        decoder(step)
                        reason = should_stop() if should_stop is not None else None
                        if reason is not None:
                            decoder.stop_reason = reason
                            self._log.debug("Stop requested (%s); ending step loop at this boundary", reason.value)
                            break
                return
            except RuntimeError:
                if pulled or attempt == _RECEIVE_STEPS_REENTRY_RETRIES - 1:
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
        iteration: int,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnOutcome:
        """Send the prompt, drain the Step stream until the turn goes idle, and end the turn.

        ``should_stop`` is polled after each processed Step; on a reason the loop
        breaks, the conversation is cancelled (best-effort) and the turn ends with
        ``end_status_for(reason)``. See ``Agent.communicate``.
        """
        if not self.working_directory or self._sdk_agent is None:
            raise RuntimeError("Agent not started. Call start() first.")
        assert self.config.type is not None, "AntigravityAgent requires AgentConfig.type before communicate()"

        emitter = self._open_emitter(
            prompt=user_input,
            iteration=iteration,
            model=self._effective_model(),
            task_id=str(self.config.type),
            stream_callback=stream_callback,
        )
        emitter.begin()
        turn_id = f"antigravity-{iteration}"
        decoder = _AntigravityDecoder(emitter, turn_id=turn_id)

        def _on_turn_timeout() -> None:
            decoder.timeout_hit = True

        try:
            emitter.begin_inner_turn(turn_id)
            try:
                await run_with_watchdog(
                    self._run_turn(user_input, decoder, timeout, should_stop),
                    timeout_seconds=timeout,
                    on_timeout=_on_turn_timeout,
                    label=f"Turn timeout ({timeout:g}s)" if timeout else "turn_timeout",
                )
            except WatchdogFired:
                return self._fail(decoder, AgentEndStatus.TIMEOUT, format_timeout_reason(timeout or 0))
            except Exception as e:
                if decoder.timeout_hit:
                    return self._fail(decoder, AgentEndStatus.TIMEOUT, format_timeout_reason(timeout or 0))
                if not decoder.ended_cleanly:
                    return self._fail(decoder, AgentEndStatus.CRASHED, f"Antigravity turn failed: {e!s}")
                # Already stopped on purpose — do not escalate.
                # Rationale: .claude/notes/agents.md § Why a post-stop exception is not a crash
                self._log.warning("Ignoring post-stop exception; finalizing cleanly: %s", e)
            if decoder.timeout_hit:
                # The watchdog fired but the body finished before the cancel landed.
                return self._fail(decoder, AgentEndStatus.TIMEOUT, format_timeout_reason(timeout or 0))
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling() == 0:
                # Not a cancel from outside: the SDK raised it inside the turn body.
                return self._fail(decoder, AgentEndStatus.CRASHED, "Antigravity turn failed: the SDK was cancelled")
            self._state = AgentState.ERROR
            decoder.end(AgentEndStatus.CRASHED, reason="turn cancelled", agent_output=self._last_response())
            raise

        self._state = AgentState.WORKING
        # Precedence: timeout (above) > the stop reason > done.
        status = end_status_for(decoder.stop_reason) if decoder.stop_reason is not None else AgentEndStatus.COMPLETED
        return decoder.end(status, agent_output=self._last_response())

    async def _run_turn(
        self,
        user_input: str,
        decoder: _AntigravityDecoder,
        timeout: float | None,
        should_stop: Callable[[], StopReason | None] | None,
    ) -> None:
        """Send the prompt, drain, and poll a backgrounded tool call until the turn goes idle."""
        conversation = self._sdk_agent.conversation
        turn_start = time.monotonic()
        # Bound the poll loop's OWN exit earlier than the watchdog's, so its graceful
        # path reliably wins that race. `timeout=None` leaves the cycle cap as the bound.
        poll_deadline = turn_start + timeout * _POLL_DEADLINE_TIMEOUT_FRACTION if timeout else None
        try:
            await conversation.send(user_input)
            await self._drain(conversation, decoder, should_stop)
            await self._poll_background_work(conversation, decoder, timeout, should_stop, poll_deadline)
        finally:
            if decoder.stop_reason is not None:
                # Best-effort server-side cancel, once, whichever drain stopped.
                with contextlib.suppress(Exception):
                    await conversation.cancel()

    async def _poll_background_work(
        self,
        conversation: Any,
        decoder: _AntigravityDecoder,
        timeout: float | None,
        should_stop: Callable[[], StopReason | None] | None,
        poll_deadline: float | None,
    ) -> None:
        poll_count = 0

        # The model may background a run_command and go idle, so receive_steps()
        # exhausts with that call still open. Gated on the orphaned-tool signal, so a
        # normal turn takes this branch zero times.
        # Rationale: .claude/notes/agents.md § Antigravity Step interleaving and the background poll
        while (
            decoder.stop_reason is None
            and not decoder.timeout_hit
            and decoder.has_orphaned_tool_call()
            and (poll_count < _MAX_BACKGROUND_POLLS if poll_deadline is None else time.monotonic() < poll_deadline)
        ):
            poll_count += 1
            self._log.debug("Polling for backgrounded work (orphaned tool call); attempt %d", poll_count)
            await asyncio.sleep(_BACKGROUND_POLL_INTERVAL_SECONDS)
            if decoder.timeout_hit or (poll_deadline is not None and time.monotonic() >= poll_deadline):
                # Skip the re-drain, which could itself await indefinitely.
                break
            reason = should_stop() if should_stop is not None else None
            if reason is not None:
                decoder.stop_reason = reason
                break
            await self._drain(conversation, decoder, should_stop)

        if decoder.has_orphaned_tool_call() and decoder.stop_reason is None and not decoder.timeout_hit:
            bound = (
                f"poll_deadline ({_POLL_DEADLINE_TIMEOUT_FRACTION:.0%} of {timeout:g}s turn timeout)"
                if poll_deadline is not None
                else f"_MAX_BACKGROUND_POLLS ({_MAX_BACKGROUND_POLLS})"
            )
            self._log.warning(
                "Poll budget exhausted (%s, poll_count=%d) with a tool call still ACTIVE.", bound, poll_count
            )

    def _fail(self, decoder: _AntigravityDecoder, status: AgentEndStatus, reason: str) -> TurnOutcome:
        self._state = AgentState.ERROR
        return decoder.end(status, reason=reason, agent_output=self._last_response())

    def _last_response(self) -> str:
        with contextlib.suppress(Exception):
            return str(self._sdk_agent.conversation.last_response or "")
        return ""

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
        """Close the SDK Agent context (reaps the localharness subprocess).

        Never raises. A failed close is logged: the exit stack has already popped
        its callbacks, so it cannot be retried, and the harness may still be running.
        """
        stack = self._exit_stack
        self._exit_stack = None
        self._sdk_agent = None
        if stack is None:
            return
        try:
            await stack.aclose()
        except Exception:
            self._log.warning(
                "Antigravity harness teardown failed; the harness process may still be running", exc_info=True
            )
