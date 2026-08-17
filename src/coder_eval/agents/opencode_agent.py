"""OpenCode agent implementation (the open-source terminal coding agent).

Drives the ``opencode`` CLI in non-interactive mode::

    opencode run --format json -m <provider/model> --dir <cwd> [--auto] [--pure] <prompt>

which streams **newline-delimited JSON events** on stdout. Each line is one
event; this module reduces that stream into the standardized coder_eval event
protocol (``AgentStart`` / ``TurnStart`` / ``ToolStart`` / ``ToolEnd`` /
``TurnEnd`` / ``AgentEnd``) and lets :class:`EventCollector` build the
``TurnRecord`` — so no telemetry is assembled by hand here.

Envelope normalization
----------------------
The CLI emits two envelope shapes on the same stream: the normal form carries
its payload under ``part`` — ``{"type": "tool_use", "sessionID": …,
"part": {…}}`` — while the CLI's own error path emits a flat object with no
``part`` (``{"type": "error", "sessionID": …, "error": {…}}``). :func:`_unwrap`
normalizes both to ``(event_type, payload)`` so the dispatch table is written
once. (The ``session.next.*``/``properties`` envelopes belong to ``opencode
serve``'s HTTP/SSE surface and never appear here — see the note on the event
constants below.)

Session continuity
------------------
The ``sessionID`` observed on the first event is retained and replayed via
``--session`` on the next ``communicate()`` call, which is what makes multi-turn
(dialog-mode) evaluation work against a stateless CLI invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, NoReturn

from coder_eval.agent import Agent
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES
from coder_eval.models import (
    AgentKind,
    AgentState,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    OpenCodeAgentConfig,
    PermissionMode,
    ResultSummary,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.callbacks import StreamCallback, safe_emit
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.utils import expand_env_vars

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing down the CLI subprocess.
# Doubles as the post-EOF exit grace in _settle_turn when no turn deadline is
# configured (a CLI that closed its stream but won't exit gets this long to die
# before the turn is crashed).
_TERM_GRACE_SECONDS = 5.0

# SIGKILL does not exist on Windows (where the process-group sweep is a no-op
# anyway); resolve it dynamically so the module imports and typechecks on every
# platform, falling back to SIGTERM for the direct-pid kill_sync path.
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to keep draining stdout/stderr after the CLI process has been reaped.
# `opencode run` leaves a local server child holding the inherited pipes open, so
# EOF never arrives on its own and every post-exit read must be bounded.
_DRAIN_SECONDS = 2.0

# Event type strings emitted by `opencode run --format json`. These are the CLI's
# OWN compact vocabulary, captured from a live run — NOT the `session.next.*`
# names in the server's OpenAPI schema, which describe the HTTP/SSE surface of
# `opencode serve` instead. The two are not interchangeable.
_STEP_START = "step_start"
_STEP_FINISH = "step_finish"
_TEXT = "text"
_TOOL_USE = "tool_use"
_ERROR = "error"

# The full recognized vocabulary. A zero-exit turn that recognized NOTHING from
# this set captured zero telemetry, and is crashed rather than reported as a
# clean empty success — an earlier version of this harness parsed the wrong
# vocabulary and scored SUCCESS 1.0 with zero turns and zero tokens, which is
# indistinguishable from a real pass in every aggregate. See _settle_turn.
_RECOGNIZED_EVENTS = frozenset({_STEP_START, _STEP_FINISH, _TEXT, _TOOL_USE, _ERROR})

# How many distinct unrecognized event-type strings to retain for the crash
# message when the vocabulary check fails (diagnosis, not an exhaustive list).
_MAX_UNRECOGNIZED_TYPES = 8

# OpenCode's native tool names -> the canonical (Claude) vocabulary that every
# criterion is written against. Mirrors codex_agent's _TOOL_ITEM_NAMES: without
# it a `command_executed` criterion with `tool_name: Bash` matches NOTHING on an
# OpenCode run, and the shell-aware `parameters["command"]` extraction in
# criteria/command_executed.py degrades to raw-JSON matching — so the same task
# scores differently per harness. Unknown tools pass through unchanged.
_TOOL_NAME_MAP: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "multiedit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "list": "LS",
    "webfetch": "WebFetch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Agent",
    # The GPT-family edit tool. OpenCode exposes a provider-specific tool set, so
    # the vocabulary varies by MODEL within this one harness: a live 174-task run
    # showed DeepSeek using write/edit 199 times and apply_patch 0, while GPT-5.6
    # used apply_patch 120 times and write/edit 0. Unmapped, every
    # `tool_name: Write` / `tool_name: Edit` criterion scores 0 on a GPT-family
    # model that edited the file correctly. Maps to `Write` to match codex_agent's
    # `_TOOL_ITEM_NAMES["apply_patch"] = "Write"`, so one criterion reads the same
    # on both harnesses.
    "apply_patch": "Write",
    # OpenCode's native skill loader. Without this entry `skill_triggered` (which
    # keys on the canonical `Skill`) and any `command_executed` written against
    # `tool_name: Skill` read false on every OpenCode run — the engagement happened
    # but no criterion could see it.
    "skill": "Skill",
}

# OpenCode per-tool INPUT-arg key -> canonical (Claude) key. Mirrors
# antigravity_agent's _ANTIGRAVITY_ARG_RENAME and completes what _TOOL_NAME_MAP
# starts: normalizing the tool NAME alone still leaves a `command_executed` with
# a non-Bash `tool_name` matching against a differently-keyed JSON blob (see
# criteria/command_executed.py, which falls back to `json.dumps(parameters)` for
# every tool but Bash), so the same task scores differently per harness. Keyed by
# the canonical tool name (post _TOOL_NAME_MAP); unlisted keys pass through.
#
# `bash` needs no entry: OpenCode already names it `command`, which is why the
# Bash-only shell-aware extraction in command_executed.py was correct as-is.
# `glob`/`grep`/`list` also need none — their `path` already matches Claude's.
#
# BOTH file-path spellings are mapped because the CLI has MOVED: a live capture
# on 2026-08-13 emitted `filePath` (see the fixture in tests/test_opencode_agent.py),
# while the tool schemas registered by the CLI installed at the time of writing
# read `path` (`read`/`write`/`edit` all take `{path, ...}`). Accepting both keeps
# telemetry canonical across the CLI versions a run might use, and neither
# spelling collides with a legitimate parameter of these three tools.
_OPENCODE_ARG_RENAME: dict[str, dict[str, str]] = {
    "Read": {"path": "file_path", "filePath": "file_path"},
    "Write": {"path": "file_path", "filePath": "file_path"},
    "Edit": {
        "path": "file_path",
        "filePath": "file_path",
        "oldString": "old_string",
        "newString": "new_string",
        "replaceAll": "replace_all",
    },
    # The skill loader's argument. With this rename, `skill_triggered` reads the
    # agent-agnostic `parameters["skill"]` on every harness instead of carrying a
    # per-harness alternative list in a criterion that must know nothing about
    # harnesses.
    "Skill": {"name": "skill"},
}

# Config fields the OpenCode CLI has no equivalent knob for. `experiments/default.yaml`
# sets `allowed_tools` on every task, so these are silently dropped by default —
# warn once at start() rather than letting a task believe it constrained the agent.
# `plugins` is NOT here: its skills half is honored via _plugin_skill_dirs below.
_UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "system_prompt_file",
    "allowed_tools",
    "disallowed_tools",
)

# --- skill injection ------------------------------------------------------
#
# A `plugins:` entry is a Claude-plugin root. Claude Code reads its skills from
# the `skills` field of `<root>/.claude-plugin/plugin.json` (conventionally
# `./skills/`). OpenCode has no plugin knob, but it does load skills from
# `skills.paths` in its config — so mapping the plugin root to that directory is
# what makes one `plugins:` line mean the same thing on both harnesses.
#
# The config is handed over through OPENCODE_CONFIG_CONTENT, which OpenCode
# merges as a final local-scope layer. That was chosen over writing
# `<sandbox>/.opencode/skills/` because it (a) writes nothing into the sandbox
# that is later preserved as run artifacts and inspected by file criteria, and
# (b) does not depend on how the CLI resolves a project root from `--dir`.
# Verified orthogonal to `--pure`, which skips external *plugins*, not
# configured skill paths.
#
# Only the skills half of a plugin is honored. A Claude plugin's agents, hooks,
# commands and MCP servers have no OpenCode equivalent and are still dropped.
_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
_PLUGIN_MANIFEST_RELPATH = (".claude-plugin", "plugin.json")
_DEFAULT_PLUGIN_SKILLS_SUBDIR = "skills"
_SKILL_FILE = "SKILL.md"

# ToolEndStatus -> CommandTelemetry.result_status (the persisted tri-state).
_RESULT_STATUS: dict[ToolEndStatus, Literal["success", "error", "unknown"]] = {
    ToolEndStatus.OK: "success",
    ToolEndStatus.ERROR: "error",
    ToolEndStatus.PERMISSION_DENIED: "error",
    ToolEndStatus.UNRESOLVED: "unknown",
}


def _unwrap(obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize an OpenCode CLI event to ``(event_type, payload)``.

    Every line is ``{type, timestamp, sessionID, part: {...}}`` with the payload
    under ``part`` — except the CLI's own error line, which is flat
    (``{type: "error", sessionID, error: {...}}``). Returning the top-level dict
    for the flat case is safe: the accessors read named keys, never iterate.
    """
    event_type = str(obj.get("type") or "")
    part = obj.get("part")
    if isinstance(part, dict):
        return event_type, part
    return event_type, obj


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    """Convert OpenCode's epoch-millisecond timestamps to naive local datetimes.

    Naive-local matches what the rest of the telemetry uses (``datetime.now()``),
    so durations computed against these stay consistent.
    """
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _canonical_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a tool call's argument keys to the canonical cross-agent vocabulary.

    Order is preserved and unlisted keys pass through untouched, so this only ever
    re-labels what ``_OPENCODE_ARG_RENAME`` names for this tool.
    """
    rename = _OPENCODE_ARG_RENAME.get(tool_name)
    if not rename:
        return params
    return {rename.get(key, key): value for key, value in params.items()}


def _manifest_skill_dirs(root: Path) -> list[Path]:
    """Skill directories a Claude-plugin root declares, in manifest order.

    Reads the ``skills`` field of ``<root>/.claude-plugin/plugin.json`` (a string
    or a list of strings, each relative to the root) and falls back to the
    convention default ``<root>/skills`` when the manifest is absent, unreadable,
    or declares none. Honoring the manifest rather than hardcoding ``skills/``
    keeps a plugin that relocates its skills working on both harnesses.
    """
    manifest = root.joinpath(*_PLUGIN_MANIFEST_RELPATH)
    declared: list[str] = []
    if manifest.is_file():
        try:
            data: Any = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            value = data.get("skills")
            if isinstance(value, str):
                declared = [value]
            elif isinstance(value, list):
                declared = [entry for entry in value if isinstance(entry, str)]
    if not declared:
        declared = [_DEFAULT_PLUGIN_SKILLS_SUBDIR]
    return [(root / relative).resolve() for relative in declared]


def _plugin_skill_dirs(
    plugins: Sequence[Mapping[str, Any]] | None,
    log: logging.Logger | logging.LoggerAdapter[Any] = logger,
) -> list[str]:
    """Resolve ``plugins:`` entries to OpenCode ``skills.paths`` directories.

    Every way this can come up empty is logged rather than passed over: a plugin
    whose skills never reach the agent still *looks* like a normal run, which is
    precisely the failure this function exists to close.
    """
    resolved: list[str] = []
    for plugin in plugins or []:
        if not isinstance(plugin, Mapping) or plugin.get("type") != "local":
            log.warning("opencode: ignoring non-local plugin entry %r — only `type: local` maps to skills.", plugin)
            continue
        path_str = plugin.get("path")
        if not path_str:
            continue
        expanded = expand_env_vars(str(path_str))
        root = Path(expanded).resolve()
        if not root.is_dir():
            hint = "env var likely unset" if "$" in expanded else "path does not exist"
            log.warning(
                "opencode: plugin skills path did not resolve: %r -> %r (%s); no skills injected from it",
                path_str,
                expanded,
                hint,
            )
            continue
        candidates = [directory for directory in _manifest_skill_dirs(root) if directory.is_dir()]
        # A path that is ALREADY a bare skills directory (<root>/<name>/SKILL.md)
        # has no `skills/` subdir, so use it as-is. Deliberately not a fallback for
        # a root that HAS one: `skills.paths` is scanned recursively and a repo
        # root can contain self-referential symlinks (UiPath/skills has
        # `plugins/uipath -> ..`), which resolves skills through an arbitrary path
        # and silently drops duplicate names.
        if not candidates:
            candidates = [root]
        for directory in candidates:
            if next(directory.glob(f"*/{_SKILL_FILE}"), None) is None:
                log.warning(
                    "opencode: no <name>/%s directly under %s (from plugin %r) — the CLI still scans it "
                    + "recursively, but check the plugin path points at a skills root",
                    _SKILL_FILE,
                    directory,
                    path_str,
                )
            as_text = str(directory)
            if as_text not in resolved:
                resolved.append(as_text)
    return resolved


class _OpenCodeTurnState:
    """Per-``communicate()`` accumulator: events in, finalization payload out.

    Owns everything the terminal ``AgentEndEvent`` must carry (transcript
    messages, cumulative usage, text output) plus the open-tool bookkeeping
    needed to force-close orphans when a turn dies mid-flight.
    """

    def __init__(self, *, task_id: str, iteration: int, user_input: str, model: str | None) -> None:
        self.task_id = task_id
        self.iteration = iteration
        self.user_input = user_input
        self.model = model

        self.started_at = time.monotonic()
        self.session_id: str | None = None
        self.thread_id: str | None = None

        # Cumulative turn totals (summed across every inner step).
        self.usage = TokenUsage()
        self.cost_usd: float = 0.0
        self.saw_cost = False

        self.messages: list[TranscriptMessage] = []
        self.text_parts: list[str] = []
        self.step_count = 0
        # Steps the CLI reported as FINISHED (`step_finish`), as opposed to
        # `step_count`, which counts the ones it started. `_settle_turn` needs the
        # distinction: a finished step is the CLI's own claim that a generation
        # completed, so one that booked no tokens means the token schema moved.
        self.steps_finished = 0
        self.turn_id: str = ""
        # True between a step's `step_start` and its `step_finish`. `finalize`
        # needs it to close a TurnStartEvent the stream never got to close.
        self.step_open = False
        self.step_started_at: datetime | None = None
        self.step_text_parts: list[str] = []
        self.step_tool_ids: list[str] = []

        # callID -> (telemetry, started_at) for tools awaiting a result.
        self.open_tools: dict[str, CommandTelemetry] = {}
        self.sequence = 0
        self.stop_reason: str | None = None
        self.error_message: str | None = None
        self.max_turns_exhausted = False
        # Guards the one-terminal-event rule; see finalize().
        self.finalized = False
        # Guards _warn_token_shape: one report per turn, not one per step.
        self.warned_token_shape = False
        # Vocabulary drift detection (see _settle_turn): how many events matched
        # _RECOGNIZED_EVENTS, and a bounded sample of the types that did not.
        self.recognized_events = 0
        self.unrecognized_types: set[str] = set()

        self._emit: Callable[[StreamEvent], None] = lambda _e: None

    def bind(self, emit: Callable[[StreamEvent], None]) -> None:
        self._emit = emit

    def emit(self, event: StreamEvent) -> None:
        self._emit(event)

    @property
    def agent_output(self) -> str:
        return "".join(self.text_parts)

    # --- event handlers ----------------------------------------------------

    def on_step_start(self, part: dict[str, Any]) -> None:
        self.step_count += 1
        self.step_open = True
        self.turn_id = str(part.get("messageID") or f"step_{self.step_count}")
        self.step_started_at = datetime.now()
        self.step_text_parts = []
        self.step_tool_ids = []
        self.emit(
            TurnStartEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                model=self.model,
            )
        )

    def on_text(self, part: dict[str, Any]) -> None:
        """``text`` carries a COMPLETE assistant message, not a streaming delta."""
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        self.text_parts.append(text)
        self.step_text_parts.append(text)
        self.emit(TextChunkEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, text=text))

    def on_tool_use(self, part: dict[str, Any]) -> None:
        """A ``tool_use`` event carries the tool's whole state under ``state``.

        In practice the CLI emits one already-``completed`` event per call rather
        than a call/result pair, so the matching ``ToolStart``/``ToolEnd`` are
        both synthesized here. A non-terminal state (``pending``/``running``) is
        still handled: the tool is left open and closed by a later event for the
        same ``callID``, or force-closed as ``unresolved`` if the turn dies first.
        Execution timestamps come from ``state.time``, so ``duration_ms`` reflects
        the tool's real runtime rather than our parse instant.
        """
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        call_id = str(part.get("callID") or f"call_{self.sequence + 1}")
        times = state.get("time") if isinstance(state.get("time"), dict) else {}
        started = _epoch_ms_to_dt(times.get("start"))
        params = state.get("input")
        params = params if isinstance(params, dict) else {}

        telemetry = self.open_tools.get(call_id)
        if telemetry is None:
            self.sequence += 1
            raw_tool = str(part.get("tool") or "unknown")
            tool_name = _TOOL_NAME_MAP.get(raw_tool.lower(), raw_tool)
            telemetry = CommandTelemetry(
                tool_name=tool_name,
                tool_id=call_id,
                assistant_turn_index=self.step_count,
                timestamp=started or datetime.now(),
                execution_started_at=started,
                parameters=_canonical_params(tool_name, params),
                sequence_number=self.sequence,
            )
            self.open_tools[call_id] = telemetry
            self.step_tool_ids.append(call_id)
            self.emit(
                ToolStartEvent(task_id=self.task_id, thread_id=self.thread_id, turn_id=self.turn_id, tool=telemetry)
            )
        else:
            # A SECOND event for a call already open — the pending/running-then-
            # completed lifecycle. The first event routinely carries no `input`
            # (the CLI has not finished assembling the call), so freezing the
            # first event's view would leave `parameters` permanently `{}` and
            # zero every `command_executed` row while the run looked normal.
            # Later evidence wins; absent evidence never clears what we have.
            if params:
                telemetry.parameters = _canonical_params(telemetry.tool_name, params)
            if started is not None:
                telemetry.execution_started_at = started

        status_text = str(state.get("status") or "").lower()
        output = state.get("output")
        error_text = state.get("error")
        if status_text in ("pending", "running"):
            return  # still in flight; a later event (or the orphan sweep) closes it

        if status_text == "error" or error_text:
            message = str(error_text or output or "tool failed")
            denied = "permission" in message.lower() or "denied" in message.lower()
            status = ToolEndStatus.PERMISSION_DENIED if denied else ToolEndStatus.ERROR
        else:
            message = None
            status = ToolEndStatus.OK

        times = state.get("time") if isinstance(state.get("time"), dict) else {}
        self._close_tool(
            call_id,
            status=status,
            summary=output if isinstance(output, str) else None,
            error=message,
            completed_at=_epoch_ms_to_dt(times.get("end")),
        )

    def _close_tool(
        self,
        call_id: str,
        *,
        status: ToolEndStatus,
        summary: str | None,
        error: str | None,
        completed_at: datetime | None = None,
    ) -> None:
        telemetry = self.open_tools.pop(call_id, None)
        if telemetry is None:
            # A result with no matching call (shouldn't happen, but never drop it).
            self.sequence += 1
            telemetry = CommandTelemetry(
                tool_name="unknown",
                tool_id=call_id,
                assistant_turn_index=self.step_count,
                timestamp=datetime.now(),
                sequence_number=self.sequence,
            )
        completed = completed_at or datetime.now()
        telemetry.execution_completed_at = completed
        if telemetry.execution_started_at is not None:
            telemetry.duration_ms = (completed - telemetry.execution_started_at).total_seconds() * 1000
        telemetry.result_status = _RESULT_STATUS[status]
        # Stored untruncated by design (sub-agent returns must survive whole).
        telemetry.result_summary = summary
        telemetry.error_message = error
        self.emit(
            ToolEndEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                tool=telemetry,
                status=status,
            )
        )

    def _rate_card_cost(self) -> float | None:
        """Price the captured buckets from the static rate card.

        ``None`` when the model is unpinned or unpriced, matching "nothing could
        be priced". See :meth:`_resolve_cost` for how this composes with the
        stream's own ``cost`` reporting.
        """
        if not self.model or self.usage.is_empty():
            return None
        return calculate_cost(
            self.model,
            uncached_input_tokens=self.usage.uncached_input_tokens,
            output_tokens=self.usage.output_tokens,
            cache_creation_tokens=self.usage.cache_creation_input_tokens,
            cache_read_tokens=self.usage.cache_read_input_tokens,
        )

    def _resolve_cost(self) -> float | None:
        """Decide the turn's cost: the stream's own accounting vs the rate card.

        A non-zero cost the CLI reported always wins — it is the provider's own
        accounting, and (on OpenRouter) per-request routing makes it strictly
        better than a static headline rate. The rate card fills two gaps that
        would otherwise book tokens with no money and silently understate the
        run-level bill:

        - the stream reported no ``cost`` at all (a provider or auth mode that
          omits it, or a turn that died before its first ``step_finish``);
        - the stream reported ``cost: 0`` for tokens the rate card prices above
          zero. OpenCode reports 0 when its own model registry has no price for
          the model, or under subscription-style auth — neither means the tokens
          were free. A genuinely free model has an all-zero rate entry (or no
          entry), so it still resolves to the stream's 0 here.
        """
        rate = self._rate_card_cost()
        if not self.saw_cost:
            return rate
        if self.cost_usd == 0.0 and rate:
            logger.warning(
                "opencode: the stream reported $0 for a turn the rate card prices at $%.6f "
                + "(model unpriced in OpenCode's registry, or subscription auth); using the rate card "
                + "so the run total is not understated.",
                rate,
            )
            return rate
        return self.cost_usd

    def _warn_token_shape(self, message: str, *args: Any) -> None:
        """Report a token-bucket surprise ONCE per turn (a broken stream repeats it)."""
        if self.warned_token_shape:
            return
        self.warned_token_shape = True
        logger.warning("opencode: unexpected token accounting — " + message, *args)

    def _as_int(self, bucket: str, value: Any) -> int:
        """Coerce one stream-supplied token count, warning instead of raising.

        A bare ``int()`` raises on anything non-numeric (``int("abc")`` ->
        ``ValueError``; ``int({...})``/``int([...])`` -> ``TypeError``), which
        ``communicate``'s ``except Exception`` turns into an ``AgentCrashError``
        — categorized ``AGENT_CRASH`` with ``max_retries=2``, so ONE mistyped
        bucket burns three full attempts and lands the task as ERROR.

        That is the opposite of the policy every neighbouring field follows:
        ``_epoch_ms_to_dt`` type-checks, ``state``/``input``/``cost``/``total`` are
        all ``isinstance``-gated, and ``_fresh_input_slice`` exists specifically to
        warn-once on token-schema drift rather than fail. A changed type in the
        very same ``tokens`` dict is drift too, so it is reported the same way and
        the turn survives on the buckets it could read. It is also what makes
        ``_handle_line``'s advertised "Never raises on bad input" true.
        """
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            if value is not None:
                self._warn_token_shape(
                    "tokens.%s is %r (%s), not a number; counting it as 0 — the CLI's token schema "
                    + "may have changed, so re-check docs/agents/OPENCODE.md before trusting cost",
                    bucket,
                    value,
                    type(value).__name__,
                )
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            self._warn_token_shape(
                "tokens.%s is %r, which is not convertible to a number; counting it as 0 — the CLI's "
                + "token schema may have changed, so re-check docs/agents/OPENCODE.md before trusting cost",
                bucket,
                value,
            )
            return 0

    def _fresh_input_slice(
        self, tokens: dict[str, Any], raw_in: int, raw_out: int, reasoning: int, cw: int, cr: int
    ) -> int:
        """Decide what ``tokens.input`` means on this stream — per step, from evidence.

        coder_eval's ``uncached_input_tokens`` is the fresh slice only (cost bills it
        at the input rate and the cache buckets separately), and two conventions for
        ``input`` exist in the wild:

        - **flat** — ``input`` already IS the fresh slice and
          ``total = input + output + reasoning + cache.read + cache.write``. This is
          what a live capture on the current CLI shows (observed 2026-08-13:
          ``7966 = 6796 + 128 + 18 + 1024`` exactly).
        - **nested** — cached tokens are counted inside ``input`` (the OpenAI
          ``prompt_tokens`` convention), so ``total = input + output + reasoning``
          and the fresh slice subtracts the cache buckets.

        The stream's own ``total`` arbitrates per step, so a CLI upgrade that flips
        the convention re-classifies itself instead of silently mis-booking a bucket.
        With no cache traffic the conventions agree. With no usable ``total`` the
        flat (live-verified) reading is taken — but if cache traffic is present that
        is an UNVERIFIABLE assumption (the original mapping bug was exactly an
        unverified assumption of this kind), so it warns once per turn rather than
        defaulting in silence. A ``total`` matching NEITHER warns loudly — the
        schema moved, and cost should not be trusted blind.
        """
        total = tokens.get("total")
        if not isinstance(total, int):
            if cr or cw:
                self._warn_token_shape(
                    "tokens.total is missing with cache traffic present (cache.read=%d, cache.write=%d); "
                    + "assuming the flat convention (`input` is the fresh slice) but the mapping cannot be "
                    + "verified for this stream — re-check docs/agents/OPENCODE.md before trusting cost",
                    cr,
                    cw,
                )
            return raw_in
        nested = raw_in + raw_out + reasoning
        flat = nested + cr + cw
        # Check flat first: with zero cache traffic the two sums coincide and the
        # conventions agree, so `input` is the fresh slice either way.
        if total == flat:
            return raw_in
        if total == nested:  # implies cache traffic, since flat was checked first
            fresh = raw_in - cr - cw
            if fresh < 0:
                # The stream contradicts itself: `total` says the cache buckets nest
                # inside `input`, but `input` is too small to contain them.
                self._warn_token_shape(
                    "tokens.total says the cache buckets nest inside input, but input(%d) < "
                    + "cache.read(%d) + cache.write(%d); keeping `input` as the fresh slice",
                    raw_in,
                    cr,
                    cw,
                )
                return raw_in
            return fresh
        self._warn_token_shape(
            "tokens.total(%d) matches neither input+output+reasoning(%d) nor that sum plus the cache "
            + "buckets(%d); the bucket mapping may no longer match the CLI — re-check "
            + "docs/agents/OPENCODE.md before trusting cost",
            total,
            nested,
            flat,
        )
        return raw_in

    def on_step_finish(self, part: dict[str, Any]) -> None:
        self.steps_finished += 1
        self.step_open = False
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        raw_in = self._as_int("input", tokens.get("input") or 0)
        raw_out = self._as_int("output", tokens.get("output") or 0)
        step_reasoning = self._as_int("reasoning", tokens.get("reasoning") or 0)
        step_cw = self._as_int("cache.write", cache.get("write") or 0)
        step_cr = self._as_int("cache.read", cache.get("read") or 0)

        step_in = self._fresh_input_slice(tokens, raw_in, raw_out, step_reasoning, step_cw, step_cr)
        # Reasoning tokens are billed at the output rate but reported apart from
        # `output`, so fold them in for the turn total; the per-message record
        # keeps `reasoning_tokens` separately for visibility.
        step_out = raw_out + step_reasoning

        self.usage = TokenUsage(
            uncached_input_tokens=self.usage.uncached_input_tokens + step_in,
            output_tokens=self.usage.output_tokens + step_out,
            cache_creation_input_tokens=self.usage.cache_creation_input_tokens + step_cw,
            cache_read_input_tokens=self.usage.cache_read_input_tokens + step_cr,
        )
        cost = part.get("cost")
        if isinstance(cost, int | float):
            self.cost_usd += float(cost)
            self.saw_cost = True

        finish = part.get("reason")
        if isinstance(finish, str) and finish:
            self.stop_reason = finish

        started = self.step_started_at or datetime.now()
        completed = datetime.now()
        blocks: list[ContentBlock] = []
        step_text = "".join(self.step_text_parts)
        if step_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=step_text))
        for i, tool_id in enumerate(self.step_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        self.messages.append(
            AssistantMessage(
                started_at=started,
                completed_at=completed,
                generation_duration_ms=(completed - started).total_seconds() * 1000,
                content_blocks=blocks,
                tool_use_ids=list(self.step_tool_ids),
                input_tokens=step_in,
                output_tokens=step_out,
                cache_creation_tokens=step_cw,
                cache_read_tokens=step_cr,
                reasoning_tokens=step_reasoning,
                stop_reason=finish if isinstance(finish, str) else None,
                model=self.model,
                message_id=str(part.get("messageID") or "") or None,
            )
        )
        self.emit(
            TurnEndEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                status=TurnEndStatus.COMPLETED,
                tokens=TokenUsage(
                    uncached_input_tokens=step_in,
                    output_tokens=step_out,
                    cache_creation_input_tokens=step_cw,
                    cache_read_input_tokens=step_cr,
                ),
            )
        )

    def on_error(self, part: dict[str, Any]) -> None:
        """Record the CLI's own structured error, which ``_settle_turn`` crashes on.

        The payload is the flat envelope (no ``part``), and its shape varies: a
        nested ``error.data.message`` when the CLI has one, otherwise the error's
        ``name``. Anything else degrades to its string form rather than raising.
        """
        error = part.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            message = (data or {}).get("message") if isinstance(data, dict) else None
            self.error_message = str(message or error.get("name") or "unknown error")
        else:
            self.error_message = str(error or "unknown error")

    def close_open_tools(self) -> None:
        """Force-close every tool still awaiting a result (crash/timeout orphans)."""
        for call_id in list(self.open_tools):
            self._close_tool(call_id, status=ToolEndStatus.UNRESOLVED, summary=None, error="no result observed")

    def finalize(
        self,
        status: AgentEndStatus,
        *,
        crashed: bool = False,
        crash_reason: str | None = None,
    ) -> None:
        """Close orphaned tools and emit the terminal ``AgentEndEvent``.

        Idempotent: the protocol allows EXACTLY ONE ``AgentEndEvent`` per
        ``communicate()``, and the outer ``except Exception`` guard can fire after
        a normal finalize (e.g. a failure while building the record). The first
        call wins so a late crash cannot emit a second terminal event into the
        caller's ``stream_callback``; it still raises, so the failure is not
        swallowed.
        """
        if self.finalized:
            return
        self.finalized = True
        self.close_open_tools()
        usage = self.usage
        cost = self._resolve_cost()
        if cost is not None:
            usage = usage.model_copy(update={"total_cost_usd": cost})
        # A step still open here never received its `step_finish` — the turn
        # died between the two (crash, timeout, cancel) or was cut cleanly
        # (should_stop, max_turns). Either way its TurnStartEvent must be
        # closed, or the protocol's one-pair-per-inner-turn contract
        # (Agent.communicate) is broken and every renderer shows a turn that
        # opens and never ends. Unlike the siblings, the completed steps have
        # already closed themselves in `on_step_finish`, so this fires ONLY for
        # the straggler. TurnEndStatus mirrors AgentEndStatus value-for-value
        # precisely so this conversion is total.
        if self.step_open:
            self.step_open = False
            self.emit(
                TurnEndEvent(
                    task_id=self.task_id,
                    thread_id=self.thread_id,
                    turn_id=self.turn_id,
                    status=TurnEndStatus(status.value),
                    tokens=None,
                )
            )
        self.emit(
            AgentEndEvent(
                task_id=self.task_id,
                thread_id=self.thread_id,
                status=status,
                usage=usage,
                iteration=self.iteration,
                user_input=self.user_input,
                agent_output=self.agent_output,
                model_used=self.model,
                assistant_turn_count=self.step_count,
                messages=list(self.messages),
                num_turns=self.step_count,
                max_turns_exhausted=self.max_turns_exhausted,
                result_summary=ResultSummary(
                    is_error=crashed,
                    subtype=status.value,
                    stop_reason=self.stop_reason,
                    result=crash_reason or self.error_message,
                ),
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - self.started_at,
            )
        )


@AgentRegistry.register(AgentKind.OPENCODE, OpenCodeAgentConfig)
class OpenCodeAgent(Agent[OpenCodeAgentConfig]):
    """Runs the ``opencode`` CLI as a subprocess, one invocation per turn."""

    # `should_stop` is polled at every event boundary — i.e. tool-call
    # granularity — and honored by terminating the CLI subprocess cleanly.
    supports_cooperative_stop: ClassVar[bool] = True

    def __init__(
        self,
        config: OpenCodeAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``create_agent`` calls ``agent_class(config, route=route, **kwargs)`` through
        a ``cast(Any, ...)``, so pyright checks nothing at the call site; a ``**_``
        sink on this side would mean nothing checks it at runtime either. The
        orchestrator depends on that TypeError as a signal — it gates
        ``cost_log_tags`` on ``supports_cost_log_tags`` precisely "otherwise the
        agent-agnostic factory would forward it into ... constructors that don't
        declare it and crash with TypeError" — so a mis-gated kwarg must be loud
        here rather than silently dropped.

        ``route`` is accepted for factory parity and deliberately unused: the CLI
        owns its own provider configuration (see ``docs/agents/OPENCODE.md``), so
        the run's Bedrock/Anthropic routing does not apply to it. ``task_id`` only
        labels the emitted event stream.
        """
        self.config = config
        self.route = route
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._skill_dirs: list[str] = []
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Process-group ids (== the CLI's pid under start_new_session) of every
        # invocation this agent spawned, swept on kill()/kill_sync()/stop() —
        # `opencode run` leaves a server child alive after the CLI exits, and
        # signaling only the CLI pid would orphan it (a slow leak across a batch).
        self._spawned_pgids: list[int] = []
        self._state = AgentState.WORKING

    # --- lifecycle ---------------------------------------------------------

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        if shutil.which("opencode") is None:
            raise RuntimeError(
                "The 'opencode' CLI was not found on PATH."
                + " Install it with `npm install -g opencode-ai` (or see https://opencode.ai/docs/)."
            )
        ignored = [f for f in _UNSUPPORTED_CONFIG_FIELDS if getattr(self.config, f, None)]
        if ignored:
            logger.warning(
                "opencode: %s set but NOT enforced — the CLI has no equivalent knob, so the run is "
                + "unconstrained by them; do not rely on them as a boundary (see docs/agents/OPENCODE.md).",
                ", ".join(ignored),
            )
        self._skill_dirs = _plugin_skill_dirs(self.config.plugins, log=logger)
        if self._skill_dirs:
            logger.info(
                "opencode: injecting %d skill path(s) via %s: %s",
                len(self._skill_dirs),
                _CONFIG_CONTENT_ENV,
                self._skill_dirs,
            )
        elif self.config.plugins:
            # Plugins were declared but produced nothing — the run is about to
            # measure the model without the skills under test. Say so loudly.
            logger.warning(
                "opencode: %d plugin(s) declared but 0 skill path(s) resolved — the agent will run "
                + "WITHOUT them (see docs/agents/OPENCODE.md).",
                len(self.config.plugins),
            )
        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        self._session_id = None
        self._state = AgentState.WORKING

    async def stop(self) -> None:
        await self.kill()
        self._mark_stopped()

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

        Each invocation runs in its own session (``start_new_session``), so its
        pgid is the CLI's pid and the group contains ONLY what that invocation
        spawned — the lingering server child included, a shared daemon we did not
        start excluded. The CLI itself gets SIGTERM-then-SIGKILL first (see
        ``kill``); this reaps whatever survives it. Sessions are persisted on
        disk by OpenCode, so killing a turn's server does not lose ``--session``
        continuity.
        """
        if os.name != "posix":
            return
        for pgid in self._spawned_pgids:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, _SIGKILL)
        self._spawned_pgids.clear()

    def get_environment_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"opencode_model": self.config.model, "opencode_pure": self.config.pure}
        if self._skill_dirs:
            # Recorded per task so a run's report can be checked for whether the
            # skills under test actually reached the agent.
            info["opencode_skill_paths"] = list(self._skill_dirs)
        if self.config.variant:
            info["opencode_variant"] = self.config.variant
        if self._session_id:
            info["opencode_session_id"] = self._session_id
        return info

    # --- command construction ---------------------------------------------

    def _build_argv(self, user_input: str) -> list[str]:
        argv = ["opencode", "run", "--format", "json"]
        if self.config.model:
            argv += ["-m", self.config.model]
        if self.working_directory:
            argv += ["--dir", self.working_directory]
        if self.config.variant:
            argv += ["--variant", self.config.variant]
        if self.config.pure:
            argv.append("--pure")
        # PLAN mode is the one mode that must not auto-approve side effects; every
        # other mode runs unattended, where an approval prompt would simply hang.
        if self.config.permission_mode is not PermissionMode.PLAN:
            argv.append("--auto")
        if self._session_id:
            argv += ["--session", self._session_id]
        argv.append("--")
        argv.append(user_input)
        return argv

    def _build_env(self) -> dict[str, str]:
        """The CLI's full environment: the host's, plus the sandbox's contributions.

        The PATH prepend is the mock-shadowing contract (``Agent.start``): the
        sandbox's mock CLI directories must resolve BEFORE the real binaries, in
        the order given, or a task grading a mocked CLI silently exercises the
        real one. ``PLUGIN_TOOLS_DIR`` is advisory and never overrides an
        inherited value.

        Unlike ``CodexAgent._build_codex_env`` — which hands the SDK a partial
        dict merged over the real environment, and so must resolve the PATH key
        case-insensitively — this returns the WHOLE environment, seeded from
        ``os.environ``, whose keys CPython upper-cases on Windows (``os.py``'s
        ``encodekey``). ``"PATH"`` is therefore the inherited key on every
        platform and cannot duplicate a differently-cased one.
        """
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        self._inject_skill_paths(env)
        return env

    def _inject_skill_paths(self, env: dict[str, str]) -> None:
        """Merge the resolved skill directories into ``OPENCODE_CONFIG_CONTENT``.

        No plugins means the variable is left exactly as inherited, so a run
        without a ``plugins:`` block behaves byte-for-byte as before. An inherited
        value is preserved and appended to rather than clobbered, since the host
        may legitimately configure OpenCode through the same seam.
        """
        if not self._skill_dirs:
            return
        config: dict[str, Any] = {}
        inherited = env.get(_CONFIG_CONTENT_ENV)
        if inherited:
            try:
                parsed = json.loads(inherited)
            except json.JSONDecodeError:
                logger.warning(
                    "opencode: inherited %s is not valid JSON; replacing it with the injected skill paths.",
                    _CONFIG_CONTENT_ENV,
                )
            else:
                if isinstance(parsed, dict):
                    config = parsed
                else:
                    logger.warning(
                        "opencode: inherited %s is not a JSON object; replacing it with the injected skill paths.",
                        _CONFIG_CONTENT_ENV,
                    )
        skills = config.get("skills")
        skills = dict(skills) if isinstance(skills, dict) else {}
        existing = [path for path in skills.get("paths", []) if isinstance(path, str)]
        skills["paths"] = existing + [path for path in self._skill_dirs if path not in existing]
        config["skills"] = skills
        env[_CONFIG_CONTENT_ENV] = json.dumps(config)

    # --- the turn ----------------------------------------------------------

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        if self.working_directory is None:
            raise RuntimeError("OpenCodeAgent.start() must be called before communicate()")

        self._begin_turn()
        collector = EventCollector()

        def emit(event: StreamEvent) -> None:
            collector.on_event(event)
            if stream_callback is not None:
                safe_emit(stream_callback, event)

        state = _OpenCodeTurnState(
            task_id=self.task_id,
            iteration=self._iteration,
            user_input=user_input,
            model=self.config.model,
        )
        state.bind(emit)

        emit(
            AgentStartEvent(
                task_id=self.task_id,
                prompt=user_input,
                iteration=self._iteration,
                model=self.config.model,
            )
        )

        deadline = None if timeout is None else time.monotonic() + timeout
        stopped_early = False
        stderr_drain: asyncio.Future[bytes] | None = None
        # Bound OUTSIDE the try so the teardown in `finally` can tell "never
        # spawned" (a create_subprocess_exec failure) from "spawned and possibly
        # still running".
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_argv(user_input),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=self._build_env(),
                # A single nd-JSON event can carry a whole tool result (a large file
                # read), which blows past StreamReader's default 64 KiB line cap and
                # would raise ValueError mid-stream, killing the read loop.
                limit=STDOUT_LINE_LIMIT_BYTES,
                # Own session/process group, so teardown can killpg the lingering
                # server child without touching anything this invocation didn't
                # spawn. POSIX-only knob; harmless False elsewhere.
                start_new_session=os.name == "posix",
            )
            self._process = proc
            if os.name == "posix":
                self._spawned_pgids.append(proc.pid)
            assert proc.stdout is not None

            # Drain stderr CONCURRENTLY, from the moment the CLI starts. Reading it
            # only after exit (while stdout drives the loop) deadlocks the pair: a
            # child that fills the ~64 KiB stderr pipe blocks on write, stops
            # emitting stdout, and never exits — so the turn hangs to its deadline.
            # docker_runner dodges this by merging stderr into stdout; here that
            # would corrupt the nd-JSON, so it gets its own reader instead.
            if proc.stderr is not None:
                stderr_drain = asyncio.ensure_future(proc.stderr.read())

            # `opencode run` spawns a local server child that INHERITS this stdout
            # pipe, so the pipe is NOT closed when the CLI itself exits — readline()
            # would block until the turn deadline waiting for an EOF that never
            # comes. So race each read against process exit: whichever lands first
            # wins, and once the process is gone a bounded drain collects whatever
            # is still buffered before the loop ends.
            exit_waiter = asyncio.ensure_future(proc.wait())
            read_task: asyncio.Future[bytes] | None = None
            try:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        await self._timeout_turn(state, collector, timeout or 0.0)

                    if read_task is None:
                        read_task = asyncio.ensure_future(proc.stdout.readline())
                    done, _pending = await asyncio.wait(
                        {read_task, exit_waiter},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        await self._timeout_turn(state, collector, timeout or 0.0)
                    if not read_task.done():
                        # The process exited with the read still pending. Give the
                        # buffered tail a bounded window, then stop rather than
                        # waiting on the grandchild's open write end.
                        try:
                            await asyncio.wait_for(asyncio.shield(read_task), _DRAIN_SECONDS)
                        except TimeoutError:
                            break
                    line = read_task.result()
                    read_task = None
                    if not line:
                        break

                    self._handle_line(line, state)

                    if max_turns is not None and state.step_count > max_turns:
                        state.max_turns_exhausted = True
                        await self.kill()
                        break
                    if should_stop is not None and should_stop():
                        stopped_early = True
                        await self.kill()
                        break
            finally:
                if read_task is not None:
                    read_task.cancel()
                exit_waiter.cancel()

            status = await self._settle_turn(
                proc,
                state,
                collector,
                stderr_drain,
                stopped_early=stopped_early,
                deadline=deadline,
                timeout=timeout,
            )
            state.finalize(status)
            # Build BEFORE marking the turn clean: a failure in the reduction is a
            # failed turn, and `_end_turn_ok` would clear the rollback flag that
            # `discard_pending_turn` needs to un-bump `_iteration`.
            record = collector.build_turn_record()
            self._end_turn_ok()
            return record

        except (AgentCrashError, TurnTimeoutError):
            # Already funneled through finalize by _crash_turn / _timeout_turn.
            raise
        except asyncio.CancelledError:
            self._finalize_external_cancel(state.finalize)
            self._capture_partial_turn(collector)
            raise
        except Exception as e:
            # Everything the turn loop does NOT anticipate: a spawn failure
            # (OSError/PermissionError from create_subprocess_exec), a StreamReader
            # ValueError on a line past `limit`, a malformed-payload TypeError in a
            # handler, a pydantic error assembling telemetry. Without this the
            # exception escapes raw and breaks the pending-turn contract three ways:
            # no AgentEndEvent (an unbalanced event tree for every renderer), the
            # captured telemetry dropped instead of parked on `pending_turn`, and
            # `_iteration` left incremented because the orchestrator never reaches
            # `discard_pending_turn`. Same guard, same reasons, as CodexAgent.
            self._crash_turn(state, collector, f"OpenCode turn failed: {e!s}", cause=e)
            raise  # unreachable (_crash_turn is NoReturn) — makes the no-fall-through explicit
        finally:
            if stderr_drain is not None:
                stderr_drain.cancel()
            self._reap_orphaned_cli(proc)
            self._process = None

    def _reap_orphaned_cli(self, proc: asyncio.subprocess.Process | None) -> None:
        """Kill a CLI that is still running as the turn unwinds. No-op otherwise.

        Two exits from :meth:`communicate` reach its ``finally`` with the child
        ALIVE: the ``except Exception`` crash (a ``StreamReader`` ``ValueError``
        on an over-long line, a malformed-payload ``TypeError`` in a handler) and
        an external cancellation — neither passes through the graceful
        ``await self.kill()`` that the intentional cuts and ``_settle_turn`` use.

        Abandoning it is not merely a leak. ``AgentCrashError`` is categorized
        ``AGENT_CRASH`` (``max_retries=2``) and the orchestrator's attempt-failure
        hook only drains ``pending_turn``, so attempt 2 would spawn a SECOND
        ``opencode --dir <sandbox> --session <same id>`` while attempt 1 is still
        editing the files the criteria are about to score — and whichever writer
        won would decide the task's result. ``docker_runner`` kills its container
        from ``finally`` for the same reason.

        Deliberately synchronous. This runs while a ``CancelledError`` is
        propagating, where any await can itself be cut short and leave the child
        alive after all; ``Process.kill()`` and the group sweep deliver their
        signals with no suspension point. Skipping the SIGTERM courtesy is right
        for a turn that is already lost — the graceful escalation in :meth:`kill`
        still owns every path that has something left to flush. ``proc`` is
        ``None`` when the spawn itself failed, i.e. there is nothing to reap.
        """
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()
        self._sweep_process_groups()

    async def _settle_turn(
        self,
        proc: asyncio.subprocess.Process,
        state: _OpenCodeTurnState,
        collector: EventCollector,
        stderr_drain: asyncio.Future[bytes] | None,
        *,
        stopped_early: bool,
        deadline: float | None,
        timeout: float | None,
    ) -> AgentEndStatus:
        """Reap the CLI once the read loop is done and decide the turn's end status.

        Raises ``AgentCrashError`` (via :meth:`_crash_turn`) when the stream carried
        a structured error, when the process died with neither a structured error
        nor an intentional stop, or when a clean exit captured no token telemetry
        (a zero-telemetry turn must not score — see the guard below). Raises
        ``TurnTimeoutError`` when the turn deadline elapses while waiting for the
        exit.
        """
        # Bound the reap: the read loop can end at EOF with the CLI still alive
        # (it closed its stream but never exited), and an unbounded wait here
        # would outlive the turn deadline — the one window where `timeout` was
        # previously unenforced. Give the exit the deadline's remainder, or a
        # short fixed grace when no deadline is configured (post-EOF, a healthy
        # CLI exits almost immediately).
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS if remaining is None else remaining)
        except TimeoutError:
            if remaining is not None:
                await self._timeout_turn(state, collector, timeout or 0.0)
            await self.kill()
            self._crash_turn(
                state,
                collector,
                f"OpenCode closed its event stream but did not exit within {_TERM_GRACE_SECONDS:.0f}s",
            )
        # Collect what the concurrent reader drained. Bounded for the same reason as
        # the read loop: the inherited stderr pipe outlives the CLI, so waiting for
        # the reader's own EOF would block. Shielded so the timeout doesn't kill it
        # before communicate()'s finally can.
        stderr_bytes = b""
        if stderr_drain is not None:
            with contextlib.suppress(TimeoutError):
                stderr_bytes = await asyncio.wait_for(asyncio.shield(stderr_drain), timeout=_DRAIN_SECONDS)

        if state.error_message is not None:
            self._crash_turn(state, collector, f"OpenCode error: {state.error_message}")

        # A non-zero exit with no structured error event still means the turn
        # died — surface stderr rather than reporting a silent empty success.
        if proc.returncode not in (0, None) and not stopped_early and not state.max_turns_exhausted:
            detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
            self._crash_turn(state, collector, f"OpenCode exited non-zero: {detail}")

        # A clean exit that captured NO token telemetry must not score. File-based
        # criteria can still pass on whatever the agent did, producing a SUCCESS
        # that is silently missing from every aggregate — and, worse, one whose
        # `run_limits.max_total_tokens` / `max_usd` gates could never have tripped
        # no matter how much the run actually billed. This already happened once
        # (the harness parsed the `session.next.*` server vocabulary instead of
        # the CLI's), so drift is crashed loudly instead of scored.
        #
        # The condition is the TELEMETRY, not the event vocabulary. Keying on
        # `recognized_events == 0` alone left the identical outcome reachable one
        # layer down: a `step_finish` carrying no `tokens` key (a provider or auth
        # mode that omits usage) recognizes three events, books an all-zero
        # `TokenUsage`, and `EventCollector` then maps that to `token_usage=None`
        # — a COMPLETED turn with no tokens, no cost and no warning.
        #
        # Intentional cuts (should_stop / max_turns) are exempt: both can land
        # before the first event, or between a step's start and its `step_finish`.
        #
        # The second arm keys on a step the CLI reported as FINISHED — its own
        # claim that a generation completed — rather than on `usage.is_empty()`
        # alone, which would also condemn a stream that was cut before any step
        # could finish.
        nothing_recognized = state.recognized_events == 0
        finished_without_tokens = state.steps_finished > 0 and state.usage.is_empty()
        if not stopped_early and not state.max_turns_exhausted and (nothing_recognized or finished_without_tokens):
            if nothing_recognized:
                seen = ", ".join(sorted(state.unrecognized_types)) or "none (stdout carried no JSON events)"
                detail = f"It emitted no recognized events at all. Unrecognized event types seen: {seen}."
            else:
                detail = (
                    f"It reported {state.steps_finished} finished step(s), none of which carried usable "
                    + f"token counts (cost reported: {'yes' if state.saw_cost else 'no'})."
                )
            message = (
                f"OpenCode exited cleanly but the turn captured zero token telemetry. {detail} The CLI's "
                + "event or token schema may have changed — see docs/agents/OPENCODE.md (Telemetry) before "
                + "trusting any run from this CLI version."
            )
            # Escape hatch for a provider/auth mode that reports no usage at all,
            # where crashing every turn would make the harness unusable rather than
            # merely imprecise. Deliberately does NOT cover `nothing_recognized`:
            # that arm is vocabulary drift, which has silently zeroed a whole run
            # before, and no provider quirk can explain it.
            if not self.config.require_token_telemetry and not nothing_recognized:
                logger.warning("opencode: %s Scored anyway — require_token_telemetry is off.", message)
            else:
                self._crash_turn(state, collector, message)

        if stopped_early:
            return AgentEndStatus.STOPPED_EARLY
        if state.max_turns_exhausted:
            return AgentEndStatus.MAX_TURNS_EXHAUSTED
        return AgentEndStatus.COMPLETED

    def _crash_turn(
        self,
        state: _OpenCodeTurnState,
        collector: EventCollector,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        """Park the crashed partial record and raise ``AgentCrashError``.

        ``cause`` preserves the explicit ``__cause__`` link when called from
        inside an ``except ... as e`` block.
        """
        state.close_open_tools()
        try:
            self._finalize_and_raise_crash(state.finalize, message, cause=cause)
        finally:
            self._capture_partial_turn(collector)

    async def _timeout_turn(
        self,
        state: _OpenCodeTurnState,
        collector: EventCollector,
        timeout: float,
    ) -> NoReturn:
        """Kill the CLI, park the crashed partial record, raise ``TurnTimeoutError``.

        ``_finalize_and_raise_timeout`` emits the terminal event via
        ``state.finalize``; the partial record is captured immediately after so
        ``pending_turn`` carries everything observed before the deadline.
        """
        await self.kill()
        state.close_open_tools()
        try:
            self._finalize_and_raise_timeout(state.finalize, timeout)
        finally:
            self._capture_partial_turn(collector)

    def _handle_line(self, line: bytes, state: _OpenCodeTurnState) -> None:
        """Parse one nd-JSON line and dispatch it. Never raises on bad input."""
        raw = line.decode("utf-8", "replace").strip()
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # OpenCode occasionally interleaves non-JSON notices (e.g. the Bun
            # AVX warning) on stdout; a malformed line must not kill the turn.
            logger.debug("opencode: skipping non-JSON stdout line: %s", raw[:200])
            return
        if not isinstance(obj, dict):
            return

        event_type, part = _unwrap(obj)
        if event_type in _RECOGNIZED_EVENTS:
            state.recognized_events += 1
        elif len(state.unrecognized_types) < _MAX_UNRECOGNIZED_TYPES:
            state.unrecognized_types.add(event_type or "<missing type>")

        # sessionID rides on the envelope, not the part.
        session_id = obj.get("sessionID") or part.get("sessionID")
        if isinstance(session_id, str) and session_id:
            if state.session_id is None:
                state.session_id = session_id
                state.thread_id = session_id
            self._session_id = session_id

        if event_type == _STEP_START:
            state.on_step_start(part)
        elif event_type == _TEXT:
            state.on_text(part)
        elif event_type == _TOOL_USE:
            state.on_tool_use(part)
        elif event_type == _STEP_FINISH:
            state.on_step_finish(part)
        elif event_type == _ERROR:
            state.on_error(part)
        else:
            logger.debug("opencode: unhandled event type %r", event_type)
