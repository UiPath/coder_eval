"""Pi agent implementation (the ``pi`` Node coding agent — https://pi.dev/).

Drives the ``pi`` CLI in JSON print mode, which streams newline-delimited JSON
events on stdout, and reduces that stream through one ``TurnEmitter`` per turn.

Three grammar facts that are not obvious from the event names (``pi`` 0.84.4):

- ``agent_start`` can appear MORE THAN ONCE per invocation — Pi auto-retries a
  transient provider error internally — and ``agent_end`` is therefore NOT
  terminal. ``agent_settled`` (or EOF) is; the turn ends there.
- ``turn_start`` is one per agent-loop step (``num_turns`` on the record).
- ``message_end`` is ignored for token accounting: ``turn_end`` echoes the same
  assistant usage once per step, so reading both would double-count.

A per-agent ``--session-dir`` + stable ``--session-id``, replayed on every
``communicate()``, are what make dialog mode work across CLI invocations.

Rationale: .claude/notes/agents.md § Pi
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from coder_eval.agent import Agent
from coder_eval.errors.agent import format_timeout_reason
from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES
from coder_eval.models import (
    READ_ONLY_DENIED_TOOLS,
    AgentKind,
    AgentState,
    ApiRoute,
    ContentBlock,
    Enforcement,
    HarnessContract,
    PermissionMode,
    PiAgentConfig,
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

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing down the CLI subprocess.
# Doubles as the post-EOF exit grace in _settle_turn when no turn deadline is set.
# Re-declared at OpenCode's value rather than shared — see the notes.
# Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
_TERM_GRACE_SECONDS = 5.0

# SIGKILL does not exist on Windows (where the process-group sweep is a no-op
# anyway); resolve it dynamically so the module imports and typechecks on every
# platform, falling back to SIGTERM for the direct-pid kill_sync path.
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to keep draining stdout/stderr after the CLI has been reaped: a
# print-mode CLI may leave an inherited pipe open, so every post-exit read is
# bounded.
_DRAIN_SECONDS = 2.0

# How many distinct unrecognized event-type strings to retain for the crash
# message when the vocabulary check fails (diagnosis, not an exhaustive list).
_MAX_UNRECOGNIZED_TYPES = 8

# pi's native tool names -> the canonical (Claude) vocabulary every criterion is
# written against. Unknown tools pass through unchanged.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
_TOOL_NAME_MAP: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "multiedit": "Edit",
    # Pi's search tool is `find` (glob-by-pattern); there is no `glob` in its set.
    "find": "Glob",
    "grep": "Grep",
    "list": "LS",
    "ls": "LS",
    "webfetch": "WebFetch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Agent",
}

# pi per-tool INPUT-arg key -> canonical (Claude) key, keyed by the canonical
# tool name (post _TOOL_NAME_MAP). The search tools keep `path`, which is already
# Claude's key. Unlisted keys pass through.
_PI_ARG_RENAME: dict[str, dict[str, str]] = {
    "Read": {"path": "file_path"},
    "Write": {"path": "file_path"},
    "Edit": {
        "path": "file_path",
        "oldString": "old_string",
        "newString": "new_string",
        "replaceAll": "replace_all",
    },
}

# The canonical tool names Pi has no tool for.
_PI_NO_EQUIVALENT: frozenset[str] = frozenset({"NotebookEdit", "Skill", "ToolSearch", "WebSearch"})

_TOOL_NAMES = ToolNameMap.from_inverse(_TOOL_NAME_MAP, no_equivalent=_PI_NO_EQUIVALENT)

# The full recognized Pi vocabulary (from `pi` 0.84.4). A clean exit that
# recognized NOTHING from this set is vocabulary drift and is crashed, not scored.
# Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
_RECOGNIZED_EVENTS = frozenset(
    {
        "session",
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
        "agent_end",
        "agent_settled",
    }
)


def _canonical_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a tool call's argument keys to the canonical cross-agent vocabulary.

    Order is preserved and unlisted keys pass through untouched.
    """
    rename = _PI_ARG_RENAME.get(tool_name)
    if not rename:
        return params
    return {rename.get(key, key): value for key, value in params.items()}


def _result_text(result: Any) -> str | None:
    """Best-effort flatten of a Pi ``tool_execution_end.result`` to text.

    Pi results are ``{"content": [{"type": "text", "text": "..."}, ...]}`` (spike
    verified). Fall back to the string form for any other shape so a summary is
    never silently dropped.
    """
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
            joined = "".join(parts)
            if joined:
                return joined
    if result is None:
        return None
    return str(result)


class _PiDecoder:
    """One turn's reducer: Pi's nd-JSON events in, ``TurnEmitter`` calls out.

    Holds only what the emitter cannot know: where the next generation window
    opens, the current step's text and tool ids, the usage and reported-cost sums,
    the last stop reason, and a terminal provider ``error``.
    """

    def __init__(self, emitter: TurnEmitter) -> None:
        self.emitter = emitter
        self.usage = TokenUsage()
        self.cost_usd: float = 0.0
        self.saw_cost = False
        self.stop_reason: str | None = None
        self.error: str | None = None
        self.turn_count = 0
        self.tool_count = 0
        self.open_tool_ids: set[str] = set()
        self.turn_started_at: datetime | None = None
        self.turn_text_parts: list[str] = []
        self.turn_tool_ids: list[str] = []
        # Where the NEXT generation window starts: the previous turn's end.
        # None until the first turn finishes, and deliberately so — everything
        # before the first `turn_start` is CLI process spawn, not model time.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.gen_mark: datetime | None = None
        # Rationale: .claude/notes/agents.md § Why token-shape drift warns instead of raising
        self.warned_token_shape = False

    def __call__(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "turn_start":
            self.on_turn_start()
        elif event_type == "message_update":
            self.on_message_update(event)
        elif event_type == "tool_execution_start":
            self.on_tool_execution_start(event)
        elif event_type == "tool_execution_end":
            self.on_tool_execution_end(event)
        elif event_type == "turn_end":
            self.on_turn_end(event)
        else:
            logger.debug("pi: unhandled event type %r", event_type)

    def end(self, status: AgentEndStatus, *, reason: str | None = None) -> TurnOutcome:
        """End the turn: ``fail`` for CRASHED / TIMEOUT (with ``reason``), else ``finalize``."""
        reported = self.usage.model_copy(update={"total_cost_usd": self.cost_usd if self.saw_cost else None})
        usage = self.usage.model_copy(update={"total_cost_usd": price_turn(reported, (self.emitter.model,))})
        if status is AgentEndStatus.CRASHED or status is AgentEndStatus.TIMEOUT:
            return self.emitter.fail(status, reason or status.value, usage=usage)
        return self.emitter.finalize(status, usage=usage, stop_reason=self.stop_reason)

    def on_turn_start(self) -> None:
        # A prior step's `turn_start` with no `turn_end` — a generation aborted
        # mid-turn (the willRetry case). Close its dangling inner turn first.
        if self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(TurnEndStatus.CRASHED)
        self.turn_count += 1
        self.emitter.begin_inner_turn(f"turn_{self.turn_count}")
        self.turn_started_at = self.emitter.now()
        self.turn_text_parts = []
        self.turn_tool_ids = []

    def on_message_update(self, event: dict[str, Any]) -> None:
        """Stream a ``text_delta`` (thinking/toolcall ignored)."""
        update = event.get("assistantMessageEvent")
        if not isinstance(update, dict) or update.get("type") != "text_delta":
            return
        delta = update.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        self.turn_text_parts.append(delta)
        self.emitter.text(delta)

    def on_tool_execution_start(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("toolCallId") or f"call_{self.tool_count + 1}")
        if call_id in self.open_tool_ids:
            return
        self.tool_count += 1
        self.open_tool_ids.add(call_id)
        raw_tool = str(event.get("toolName") or "unknown")
        tool_name = _TOOL_NAME_MAP.get(raw_tool.lower(), raw_tool)
        args = event.get("args")
        self.emitter.open_tool(call_id, tool_name, _canonical_params(tool_name, args if isinstance(args, dict) else {}))
        self.turn_tool_ids.append(call_id)

    def on_tool_execution_end(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("toolCallId") or "")
        summary = _result_text(event.get("result"))
        if event.get("isError"):
            message = summary or "tool failed"
            # Best-effort: Pi does not tag permission denials, so infer from the
            # text. The persisted tri-state folds both to "error".
            denied = "permission" in message.lower() or "denied" in message.lower()
            status = ToolEndStatus.PERMISSION_DENIED if denied else ToolEndStatus.ERROR
        else:
            message = None
            status = ToolEndStatus.OK
        self.open_tool_ids.discard(call_id)
        self.emitter.close_tool(call_id, status=status, summary=summary, error=message)

    def _warn_token_shape(self, message: str, *args: Any) -> None:
        """Log a token-accounting anomaly at most once per turn (not once per bucket/step)."""
        if self.warned_token_shape:
            return
        self.warned_token_shape = True
        logger.warning("pi: unexpected token accounting — " + message, *args)

    def _as_int(self, value: Any) -> int:
        """Coerce one stream-supplied token count; count a non-number as 0.

        A bool is never a token count (``int(True) == 1``). ``None`` is a
        legitimately-absent bucket (silent); any OTHER unparseable value warns once.

        Rationale: .claude/notes/agents.md § Why token-shape drift warns instead of raising
        """
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            self._warn_token_shape("token count had unexpected type %s (%r); counted as 0", type(value).__name__, value)
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            self._warn_token_shape("token count %r was not parseable as an int; counted as 0", value)
            return 0

    def on_turn_end(self, event: dict[str, Any]) -> None:
        """Book this step's usage and its generation.

        Usage is read from ``turn_end`` ONCE per step (not from every
        ``message_end``, which echoes the same numbers).
        """
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
        raw_usage = message.get("usage")
        if not isinstance(raw_usage, dict) or not raw_usage:
            self._warn_token_shape("turn_end carried no usage object; this step's tokens/cost counted as 0")
        usage = raw_usage if isinstance(raw_usage, dict) else {}

        step_in = self._as_int(usage.get("input"))
        raw_out = self._as_int(usage.get("output"))
        step_reasoning = self._as_int(usage.get("reasoning"))
        step_cw = self._as_int(usage.get("cacheWrite"))
        step_cr = self._as_int(usage.get("cacheRead"))
        # Reasoning bills at the output rate but is reported apart from `output`.
        step_out = raw_out + step_reasoning

        if raw_usage and step_in == raw_out == step_reasoning == step_cw == step_cr == 0:
            self._warn_token_shape("turn_end usage object had all-zero token buckets; this step booked 0 tokens/cost")

        tokens = TokenUsage(
            uncached_input_tokens=step_in,
            output_tokens=step_out,
            cache_creation_input_tokens=step_cw,
            cache_read_input_tokens=step_cr,
        )
        self.usage += tokens
        # Pi's invariant is totalTokens == input + output + cacheRead + cacheWrite;
        # reasoning is EXCLUDED from it, so compare against raw_out.
        reported_total = usage.get("totalTokens")
        if isinstance(reported_total, int | float) and not isinstance(reported_total, bool):
            expected_total = step_in + raw_out + step_cw + step_cr
            if reported_total != expected_total:
                self._warn_token_shape(
                    "turn_end totalTokens=%d does not reconcile with input+output+cacheRead+cacheWrite=%d — "
                    + "a bucket may have been renamed or its meaning moved; re-check docs/agents/PI.md before "
                    + "trusting cost",
                    reported_total,
                    expected_total,
                )
        cost = usage.get("cost")
        if isinstance(cost, dict):
            total = cost.get("total")
            if isinstance(total, int | float) and not isinstance(total, bool):
                self.cost_usd += float(total)
                self.saw_cost = True

        finish = message.get("stopReason")
        if isinstance(finish, str) and finish:
            self.stop_reason = finish
        # A terminal provider error: `pi -p` exits 0 after exhausting retries.
        # Reset on a non-error turn so a recovered retry error never leaks.
        if finish == "error":
            err = message.get("errorMessage")
            self.error = err if isinstance(err, str) and err else "pi reported stopReason=error"
        else:
            self.error = None

        completed = self.emitter.now()
        blocks: list[ContentBlock] = []
        turn_text = "".join(self.turn_text_parts)
        if turn_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=turn_text))
        for i, tool_id in enumerate(self.turn_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        # Tile from the previous turn's end. The RAW window only.
        turn_start = self.turn_started_at if self.turn_started_at is not None else completed
        self.emitter.add_generation(
            message_id=str(message.get("responseId") or "") or None,
            window=close_window(
                mark=self.gen_mark if self.gen_mark is not None else turn_start, now=completed, item_start=turn_start
            ),
            parts=[
                Generation(
                    blocks=blocks,
                    tokens=tokens,
                    reasoning_tokens=step_reasoning,
                    stop_reason=finish if isinstance(finish, str) else None,
                )
            ],
        )
        self.gen_mark = completed
        # SPENT state, reset HERE and not only in `on_turn_start`: a duplicate
        # `turn_end` with no intervening start would otherwise republish this
        # turn's span, text and tool ids as the next turn's. It still books its
        # own generation and tokens, but closes no inner turn.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.turn_started_at = None
        self.turn_text_parts = []
        self.turn_tool_ids = []
        if self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(TurnEndStatus.COMPLETED, tokens=tokens)


@AgentRegistry.register(AgentKind.PI, PiAgentConfig)
class PiAgent(Agent[PiAgentConfig]):
    """Runs the ``pi`` CLI as a subprocess, one invocation per turn."""

    # `should_stop` is polled at every event boundary (tool-call granularity);
    # `--append-system-prompt` appends to, never replaces, the CLI's own prompt.
    # Rationale: .claude/notes/agents.md § The system_prompt_semantics marker
    contract = HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.ENFORCED,
        permission_mode=Enforcement.ENFORCED,
        allowed_tools=Enforcement.ENFORCED,
        disallowed_tools=Enforcement.ENFORCED,
        cooperative_stop=True,
        usage_granularity=UsageGranularity.STEP,
        timing_basis=TimingBasis.TURN_CLOCK,
        permission_modes=frozenset({PermissionMode.PLAN, PermissionMode.BYPASS_PERMISSIONS}),
    )
    tool_names = _TOOL_NAMES

    def __init__(
        self,
        config: PiAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
        cost_log_tags: dict[str, str] | None = None,
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``route`` is accepted for factory parity and deliberately unused: the CLI
        owns its own provider configuration. ``task_id`` only labels the event
        stream.

        Rationale: .claude/notes/agents.md § Why the constructors declare every kwarg
        """
        super().__init__(config, route, cost_log_tags=cost_log_tags)
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        # The staged root's skills dir, passed to `pi --skill`. Assigned in start().
        self._skill_dirs: list[str] = []
        # Per-agent session, reused across communicate() calls for multi-turn
        # continuity. Removed in stop(), deliberately NOT in kill().
        # Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
        self._session_id: str | None = None
        self._session_dir: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Process-group ids of every invocation this agent spawned, swept on
        # kill()/kill_sync()/stop().
        self._spawned_pgids: list[int] = []
        self._state = AgentState.WORKING

    # --- lifecycle ---------------------------------------------------------

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
    ) -> None:
        if shutil.which("pi") is None:
            raise RuntimeError(
                "The 'pi' CLI was not found on PATH."
                + " Install it with `npm install -g @earendil-works/pi-coding-agent` (see https://pi.dev/)."
            )
        self._skill_dirs = [str(plugin_root / "skills")] if plugin_root is not None else []
        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        # A stable pre-assigned id (create-if-missing on turn 1, resume after).
        # The tempdir lives OUTSIDE the sandbox and staged reference dir, so it
        # never pollutes graded files. Drop a prior start()'s dir so re-starting
        # the same instance cannot leak one.
        self._cleanup_session_dir()
        # Sanitize task_id before it reaches pi's `--session-id`: a dataset row's
        # path-shaped id would resolve to a non-existent subdir under
        # `--session-dir` and fail the row before any work.
        safe_task_id = re.sub(r"[^A-Za-z0-9._-]", "_", self.task_id)
        self._session_id = f"coder-eval-{safe_task_id}-{uuid4().hex[:8]}"
        self._session_dir = tempfile.mkdtemp(prefix="pi-session-")
        self._state = AgentState.WORKING

    async def stop(self) -> None:
        await self.kill()
        self._cleanup_session_dir()
        self._mark_stopped()

    def _cleanup_session_dir(self) -> None:
        if self._session_dir is not None:
            shutil.rmtree(self._session_dir, ignore_errors=True)
            self._session_dir = None

    async def kill(self) -> None:
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        self._sweep_process_groups()

    def kill_sync(self) -> None:
        """SIGKILL the in-flight CLI and its process group (watchdog thread; must not await)."""
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, _SIGKILL)
        self._sweep_process_groups()

    def _sweep_process_groups(self) -> None:
        """SIGKILL every process group this agent spawned (POSIX only).

        Each invocation runs in its own session, so its pgid is the CLI's pid and
        the group holds ONLY what that invocation spawned.
        """
        if os.name != "posix":
            return
        for pgid in self._spawned_pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, _SIGKILL)
        self._spawned_pgids.clear()

    def get_environment_info(self) -> dict[str, Any]:
        # Spread the base first so the `system_prompt_semantics` run marker is
        # always present (CE046).
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "pi_model": self.config.model,
            "pi_thinking_level": self.config.thinking_level,
        }
        if self._session_id:
            info["pi_session_id"] = self._session_id
        return info

    # --- command construction ---------------------------------------------

    def _build_argv(self, user_input: str) -> list[str]:
        # -p exits after the run; --no-context-files + --no-approve isolate the
        # sandbox from host AGENTS.md/CLAUDE.md and project-local trust.
        # --session-dir + --session-id give cross-communicate() continuity — NOT
        # --no-session, which would defeat it. No --dir: the working dir is `cwd`.
        assert self._session_dir is not None and self._session_id is not None
        argv = [
            "pi",
            "-p",
            "--mode",
            "json",
            "--no-context-files",
            "--no-approve",
            "--session-dir",
            self._session_dir,
            "--session-id",
            self._session_id,
        ]
        if self.config.model:
            argv += ["--model", self.config.model]  # provider-prefixed form
        if self.config.thinking_level:
            argv += ["--thinking", self.config.thinking_level]
        for skill_dir in self._skill_dirs:
            # Additive skill load from the staged root: Pi lists each skill's
            # name+description in the system prompt and reads it on demand.
            argv += ["--skill", skill_dir]
        argv += self._tool_flags()
        if self.config.system_prompt:
            argv += ["--append-system-prompt", self.config.system_prompt]
        # user_input is a distinct argv element after `--` (never shell-interpolated).
        argv += ["--", user_input]
        return argv

    def _tool_flags(self) -> list[str]:
        """``--tools`` / ``--no-tools`` / ``--exclude-tools`` from the uniform tool fields.

        A deny always wins: denied names are subtracted from the allowlist, and
        ``permission_mode: plan`` denies the Write, Edit and Bash equivalents.
        """
        deny_names = list(self.config.disallowed_tools or [])
        if self.config.permission_mode is PermissionMode.PLAN:
            deny_names += READ_ONLY_DENIED_TOOLS
        deny = {pi for name in deny_names for pi in _TOOL_NAMES.names[name]}
        if self.config.allowed_tools:
            allow = {pi for name in self.config.allowed_tools for pi in _TOOL_NAMES.names[name]} - deny
            return ["--tools", ",".join(sorted(allow))] if allow else ["--no-tools"]
        return ["--exclude-tools", ",".join(sorted(deny))] if deny else []

    def _build_env(self) -> dict[str, str]:
        """The CLI's full environment: the host's, plus the sandbox's contributions.

        The PATH prepend is the mock-shadowing contract (``Agent.start``): the
        sandbox's mock CLI directories must resolve BEFORE the real binaries.
        ``PLUGIN_TOOLS_DIR`` is advisory and never overrides an inherited value.
        Returns the WHOLE environment so the CLI keeps the host's provider
        credentials.
        """
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        return env

    # --- the turn ----------------------------------------------------------

    async def communicate(
        self,
        user_input: str,
        *,
        iteration: int,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        should_stop: Callable[[], StopReason | None] | None = None,
    ) -> TurnOutcome:
        """Run one ``pi -p`` invocation as one turn; see ``Agent.communicate``."""
        if self.working_directory is None:
            raise RuntimeError("PiAgent.start() must be called before communicate()")

        emitter = self._open_emitter(
            prompt=user_input,
            iteration=iteration,
            model=self.config.model,
            task_id=self.task_id,
            stream_callback=stream_callback,
        )
        emitter.begin()
        decoder = _PiDecoder(emitter)
        vocabulary = _Vocabulary()

        # Deadlines stay on `time.monotonic()`, deliberately NOT the turn clock:
        # a deadline must not move when the wall clock steps.
        deadline = None if timeout is None else time.monotonic() + timeout
        requested_stop: StopReason | None = None
        stderr_drain: asyncio.Future[bytes] | None = None
        # Bound OUTSIDE the try so `finally` can tell "never spawned" from
        # "spawned and possibly still running".
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_argv(user_input),
                # Pi reads a non-TTY stdin to EOF before it emits anything; an
                # inherited, still-open stdin stalls the turn to its deadline.
                # Rationale: .claude/notes/agents.md § Why a CLI never inherits stdin
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=self._build_env(),
                # One nd-JSON event can carry a whole tool result, past
                # StreamReader's default 64 KiB cap.
                limit=STDOUT_LINE_LIMIT_BYTES,
                # Own session/process group, so teardown can killpg a lingering
                # child without touching anything this invocation didn't spawn.
                start_new_session=os.name == "posix",
            )
            self._process = proc
            if os.name == "posix":
                self._spawned_pgids.append(proc.pid)
            assert proc.stdout is not None

            # Drain stderr CONCURRENTLY, or a child that fills the pipe blocks on
            # write and hangs the turn to its deadline.
            # Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
            if proc.stderr is not None:
                stderr_drain = asyncio.ensure_future(proc.stderr.read())

            # An inherited pipe may never reach EOF, so race each read against
            # process exit; a bounded drain then collects the tail.
            exit_waiter = asyncio.ensure_future(proc.wait())
            read_task: asyncio.Future[bytes] | None = None
            try:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return await self._time_out(decoder, timeout or 0.0)

                    if read_task is None:
                        read_task = asyncio.ensure_future(proc.stdout.readline())
                    done, _pending = await asyncio.wait(
                        {read_task, exit_waiter},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        return await self._time_out(decoder, timeout or 0.0)
                    if not read_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(read_task), _DRAIN_SECONDS)
                        except TimeoutError:
                            break
                    line = read_task.result()
                    read_task = None
                    if not line:
                        break

                    self._handle_line(line, decoder, vocabulary)

                    requested_stop = should_stop() if should_stop is not None else None
                    if requested_stop is not None:
                        await self.kill()
                        break
            finally:
                if read_task is not None:
                    read_task.cancel()
                exit_waiter.cancel()

            return await self._settle_turn(
                proc,
                decoder,
                vocabulary,
                stderr_drain,
                requested_stop=requested_stop,
                deadline=deadline,
                timeout=timeout,
            )

        except asyncio.CancelledError:
            self._state = AgentState.ERROR
            decoder.end(AgentEndStatus.CRASHED, reason="turn cancelled")
            raise
        except Exception as e:
            # A spawn failure, a StreamReader ValueError past `limit`, a malformed payload.
            logger.warning("pi: turn failed", exc_info=True)
            return self._crash(decoder, f"Pi turn failed: {e!s}")
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._reap_orphaned_cli(proc)
            self._process = None

    def _reap_orphaned_cli(self, proc: asyncio.subprocess.Process | None) -> None:
        """Kill a CLI still running as the turn unwinds. No-op otherwise.

        Synchronous (no await) so it survives a ``CancelledError`` in flight.
        ``proc`` is ``None`` when the spawn failed.

        Rationale: .claude/notes/agents.md § Reaping the CLI harnesses
        """
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()
        self._sweep_process_groups()

    async def _settle_turn(
        self,
        proc: asyncio.subprocess.Process,
        decoder: _PiDecoder,
        vocabulary: _Vocabulary,
        stderr_drain: asyncio.Future[bytes] | None,
        *,
        requested_stop: StopReason | None,
        deadline: float | None,
        timeout: float | None,
    ) -> TurnOutcome:
        """Reap the CLI once the read loop is done and end the turn.

        CRASHED when the process died with neither an intentional stop nor a
        recognized event stream, TIMEOUT when the deadline elapses while waiting
        for the exit.
        """
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS if remaining is None else remaining)
        except TimeoutError:
            if remaining is not None:
                return await self._time_out(decoder, timeout or 0.0)
            await self.kill()
            return self._crash(
                decoder, f"Pi closed its event stream but did not exit within {_TERM_GRACE_SECONDS:.0f}s"
            )
        stderr_bytes = b""
        if stderr_drain is not None:
            with contextlib.suppress(TimeoutError):
                stderr_bytes = await asyncio.wait_for(asyncio.shield(stderr_drain), timeout=_DRAIN_SECONDS)

        # A terminal provider error is infrastructure failure, not an agent
        # failure, and `pi -p` exits 0 after exhausting retries. GATED on
        # intentional cuts: a cut can fire before the clearing `turn_end` arrives.
        # Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
        if decoder.error is not None and requested_stop is None:
            return self._crash(decoder, f"Pi error: {decoder.error}")

        if proc.returncode not in (0, None) and requested_stop is None:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            return self._crash(decoder, f"Pi exited non-zero: {detail}")

        # A clean exit that recognized NO events is vocabulary drift. Intentional
        # cuts are exempt: a stop can land before the first event.
        if requested_stop is None and vocabulary.recognized == 0:
            seen = ", ".join(sorted(vocabulary.unrecognized)) or "none (stdout carried no JSON events)"
            return self._crash(
                decoder,
                "Pi exited cleanly but the turn captured no recognized events. Unrecognized event types seen: "
                + f"{seen}. The CLI's event schema may have changed — see docs/agents/PI.md before trusting any "
                + "run from this CLI version.",
            )

        return decoder.end(end_status_for(requested_stop) if requested_stop is not None else AgentEndStatus.COMPLETED)

    def _crash(self, decoder: _PiDecoder, message: str) -> TurnOutcome:
        self._state = AgentState.ERROR
        return decoder.end(AgentEndStatus.CRASHED, reason=message)

    async def _time_out(self, decoder: _PiDecoder, timeout: float) -> TurnOutcome:
        """Kill the CLI and end the turn as a timeout."""
        await self.kill()
        self._state = AgentState.ERROR
        return decoder.end(AgentEndStatus.TIMEOUT, reason=format_timeout_reason(timeout))

    def _handle_line(self, line: bytes, decoder: _PiDecoder, vocabulary: _Vocabulary) -> None:
        """Parse one nd-JSON line and hand it to the decoder. Never raises on bad input.

        ``agent_end`` is NOT terminal — only ``agent_settled`` / stdout EOF is — so
        it is recognized, ignored, and the read loop keeps going.
        """
        raw = line.decode("utf-8", "replace").strip()
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("pi: skipping non-JSON stdout line: %s", raw[:200])
            return
        if not isinstance(obj, dict):
            return
        event_type = str(obj.get("type") or "")
        if event_type in _RECOGNIZED_EVENTS:
            vocabulary.recognized += 1
        elif len(vocabulary.unrecognized) < _MAX_UNRECOGNIZED_TYPES:
            vocabulary.unrecognized.add(event_type or "<missing type>")
        decoder(obj)


@dataclass
class _Vocabulary:
    """The drift check's evidence: how many events matched Pi's vocabulary, and a sample of what did not."""

    recognized: int = 0
    unrecognized: set[str] = field(default_factory=set)
