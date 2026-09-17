"""OpenCode agent implementation (the open-source terminal coding agent).

Drives the ``opencode`` CLI in non-interactive mode, which streams
newline-delimited JSON events on stdout, and reduces that stream through one
``TurnEmitter`` per turn, on the CLI's own epoch-millisecond stamps.

The CLI emits TWO envelope shapes on the same stream: the normal form carries
its payload under ``part``, while the CLI's own error path emits a flat object
with none. :func:`_unwrap` normalizes both to ``(event_type, payload)`` so the
dispatch table is written once.

The ``sessionID`` observed on the first event is replayed via ``--session`` on
the next ``communicate()``, which is what makes dialog mode work against a
stateless CLI invocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from coder_eval.agents._transport import JsonlDecoder, SubprocessJsonlAgent
from coder_eval.models import (
    READ_ONLY_DENIED_TOOLS,
    AgentKind,
    AgentState,
    ApiRoute,
    ContentBlock,
    Enforcement,
    HarnessContract,
    OpenCodeAgentConfig,
    PermissionMode,
    TimingBasis,
    TokenUsage,
    ToolNameMap,
    UsageGranularity,
)
from coder_eval.pricing import price_turn
from coder_eval.streaming.emitter import Generation, TurnEmitter, TurnOutcome
from coder_eval.streaming.events import (
    AgentEndStatus,
    ToolEndStatus,
    TurnEndStatus,
)
from coder_eval.timing import close_window

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# The CLI's OWN compact vocabulary, captured from a live run — NOT the
# `session.next.*` names in the server's OpenAPI schema, which describe
# `opencode serve`'s HTTP/SSE surface. The two are not interchangeable.
_STEP_START = "step_start"
_STEP_FINISH = "step_finish"
_TEXT = "text"
_TOOL_USE = "tool_use"
_ERROR = "error"

# The full recognized vocabulary. A zero-exit turn that recognized NOTHING from
# it captured zero telemetry and is crashed, not scored.
# Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
_RECOGNIZED_EVENTS = frozenset({_STEP_START, _STEP_FINISH, _TEXT, _TOOL_USE, _ERROR})

# OpenCode's native tool names -> the canonical (Claude) vocabulary that every
# criterion is written against. Unknown tools pass through unchanged.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
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
    "websearch": "WebSearch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Agent",
    # The GPT-family edit tool: OpenCode's tool set varies by MODEL within this
    # one harness. `Write` matches codex_agent's own mapping.
    "apply_patch": "Write",
    # OpenCode's native skill loader; `skill_triggered` keys on the canonical name.
    "skill": "Skill",
}

# OpenCode per-tool INPUT-arg key -> canonical (Claude) key, keyed by the
# canonical tool name (post _TOOL_NAME_MAP). Unlisted keys pass through.
# `bash`/`glob`/`grep`/`list` need no entry — their keys already match Claude's.
# BOTH file-path spellings are mapped because the CLI has MOVED between them.
# Rationale: .claude/notes/agents.md § Tool-name and argument normalization
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
    # So `skill_triggered` reads the agent-agnostic `parameters["skill"]` rather
    # than carrying a per-harness alternative list.
    "Skill": {"name": "skill"},
}

# OpenCode's permission keys are coarser than its tool names: `edit` governs every
# write-shaped tool.
_PERMISSION_KEY_FOR_TOOL: dict[str, str] = {
    "write": "edit",
    "patch": "edit",
    "multiedit": "edit",
    "apply_patch": "edit",
}

# Permissions `"*"` also matches that are not tools. An allowlist re-allows them, so it
# restricts tools only.
_NON_TOOL_PERMISSIONS: tuple[str, ...] = ("external_directory", "doom_loop")

# The canonical tool names OpenCode has no tool for.
_OPENCODE_NO_EQUIVALENT: frozenset[str] = frozenset({"NotebookEdit", "ToolSearch"})

_TOOL_NAMES = ToolNameMap.from_inverse(_TOOL_NAME_MAP, no_equivalent=_OPENCODE_NO_EQUIVALENT)

# Each canonical tool name -> the permission keys that govern its OpenCode tools.
_CLAUDE_TO_OPENCODE_PERMISSION: dict[str, tuple[str, ...]] = {
    canonical: tuple(sorted({_PERMISSION_KEY_FOR_TOOL.get(tool, tool) for tool in natives}))
    for canonical, natives in _TOOL_NAMES.names.items()
}

# Skill paths, the system-prompt `instructions` file and tool `permission` rules are
# merged through this variable, which OpenCode applies as a final local-scope layer.
# Only the SKILLS half of a plugin is honored.
# Rationale: .claude/notes/agents.md § Skills, per harness
_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"

_PROMPT_FILE_NAME = "system_prompt.md"


def _unwrap(obj: dict[str, Any]) -> tuple[str, dict[str, Any], datetime | None]:
    """Normalize an OpenCode CLI event to ``(event_type, payload, envelope stamp)``.

    Every line carries its payload under ``part`` except the CLI's own error
    line, which is flat. Returning the top-level dict for that case is safe: the
    accessors read named keys, never iterate. The envelope ``timestamp`` (epoch
    ms) is the CLI's own stamp for the event; ``None`` when absent.
    """
    event_type = str(obj.get("type") or "")
    stamp = _epoch_ms_to_dt(obj.get("timestamp"))
    part = obj.get("part")
    if isinstance(part, dict):
        return event_type, part, stamp
    return event_type, obj, stamp


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    """Convert OpenCode's epoch-millisecond timestamps to naive local datetimes.

    Naive-local matches the rest of the telemetry, so durations stay consistent.
    """
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _canonical_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a tool call's argument keys to the canonical cross-agent vocabulary.

    Order is preserved and unlisted keys pass through untouched.
    """
    rename = _OPENCODE_ARG_RENAME.get(tool_name)
    if not rename:
        return params
    return {rename.get(key, key): value for key, value in params.items()}


class _OpenCodeDecoder(JsonlDecoder):
    """One turn's reducer: OpenCode's nd-JSON events in, ``TurnEmitter`` calls out.

    Timing is the CLI's own: window bounds come from each event's envelope
    ``timestamp`` and tool spans from ``state.time``. A missing envelope stamp
    falls back to the host clock for a window bound, with one warning per turn.
    The CLI's ``error`` event is final, so it crashes the turn even after a stop.
    """

    error_survives_stop = True

    def __init__(self, emitter: TurnEmitter) -> None:
        super().__init__(emitter)
        self.usage = TokenUsage()
        self.cost_usd: float = 0.0
        self.saw_cost = False
        self.stop_reason: str | None = None
        self.step_count = 0
        # Steps the CLI reported as FINISHED, as opposed to `step_count`, which
        # counts the ones it started. `clean_exit_problem` needs the distinction.
        self.steps_finished = 0
        self.tool_count = 0
        self.step_started_at: datetime | None = None
        # Where the NEXT generation window starts: the previous step's finish.
        # None until the first step finishes — everything before the first
        # `step_start` is CLI process spawn, not model time.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.gen_mark: datetime | None = None
        self.step_text_parts: list[str] = []
        self.step_tool_ids: list[str] = []
        # callID -> the latest canonical parameters of a call still awaiting its result.
        self.open_tools: dict[str, dict[str, Any]] = {}
        self._tool_names: dict[str, str] = {}
        self.warned_token_shape = False
        self.warned_missing_stamp = False

    def __call__(self, event: dict[str, Any]) -> None:
        event_type, part, stamp = _unwrap(event)
        if event_type == _STEP_START:
            self.on_step_start(part, stamp)
        elif event_type == _TEXT:
            self.on_text(part)
        elif event_type == _TOOL_USE:
            self.on_tool_use(part)
        elif event_type == _STEP_FINISH:
            self.on_step_finish(part, stamp)
        elif event_type == _ERROR:
            self.on_error(part)
        else:
            logger.debug("opencode: unhandled event type %r", event_type)

    def end(self, status: AgentEndStatus, *, reason: str | None = None) -> TurnOutcome:
        """End the turn: ``fail`` for CRASHED / TIMEOUT (with ``reason``), else ``finalize``."""
        reported = self.usage.model_copy(update={"total_cost_usd": self.cost_usd if self.saw_cost else None})
        usage = self.usage.model_copy(update={"total_cost_usd": price_turn(reported, (self.emitter.model,))})
        if status is AgentEndStatus.CRASHED or status is AgentEndStatus.TIMEOUT:
            return self.emitter.fail(status, reason or status.value, usage=usage)
        return self.emitter.finalize(status, usage=usage, stop_reason=self.stop_reason)

    def _bound(self, stamp: datetime | None) -> datetime:
        """A window bound: the CLI's envelope stamp, else the host clock (warned once per turn)."""
        if stamp is not None:
            return stamp
        if not self.warned_missing_stamp:
            self.warned_missing_stamp = True
            logger.warning("opencode: an event carried no envelope timestamp; bounding its window on the host clock")
        return self.emitter.now()

    def on_step_start(self, part: dict[str, Any], stamp: datetime | None) -> None:
        if self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(TurnEndStatus.CRASHED)
        self.step_count += 1
        self.emitter.begin_inner_turn(str(part.get("messageID") or f"step_{self.step_count}"))
        self.step_started_at = self._bound(stamp)
        self.step_text_parts = []
        self.step_tool_ids = []

    def on_text(self, part: dict[str, Any]) -> None:
        """``text`` carries a COMPLETE assistant message, not a streaming delta."""
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        self.step_text_parts.append(text)
        self.emitter.text(text)

    def on_tool_use(self, part: dict[str, Any]) -> None:
        """A ``tool_use`` event carries the tool's whole state under ``state``.

        The CLI usually emits one already-``completed`` event per call. A
        non-terminal state leaves the call open, to be closed by a later event for
        the same ``callID`` or swept as ``unresolved``. The span is ``state.time``.
        """
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        call_id = str(part.get("callID") or f"call_{self.tool_count + 1}")
        time_val = state.get("time")
        times = time_val if isinstance(time_val, dict) else {}
        params = state.get("input")
        params = params if isinstance(params, dict) else {}

        if call_id not in self.open_tools:
            self.tool_count += 1
            raw_tool = str(part.get("tool") or "unknown")
            tool_name = _TOOL_NAME_MAP.get(raw_tool.lower(), raw_tool)
            canonical = _canonical_params(tool_name, params)
            self.emitter.open_tool(call_id, tool_name, canonical, started_at=_epoch_ms_to_dt(times.get("start")))
            self.open_tools[call_id] = canonical
            self.step_tool_ids.append(call_id)
            self._tool_names[call_id] = tool_name
        elif params:
            # A SECOND event for an open call: the first routinely carries no
            # `input` yet. Later evidence wins; absent evidence clears nothing.
            self.open_tools[call_id] = _canonical_params(self._tool_names[call_id], params)

        status_text = str(state.get("status") or "").lower()
        if status_text in ("pending", "running"):
            return
        output = state.get("output")
        error_text = state.get("error")
        if status_text == "error" or error_text:
            message = str(error_text or output or "tool failed")
            denied = "permission" in message.lower() or "denied" in message.lower()
            status = ToolEndStatus.PERMISSION_DENIED if denied else ToolEndStatus.ERROR
        else:
            message = None
            status = ToolEndStatus.OK
        self.emitter.close_tool(
            call_id,
            status=status,
            summary=output if isinstance(output, str) else None,
            error=message,
            parameters=self.open_tools.pop(call_id),
            completed_at=_epoch_ms_to_dt(times.get("end")),
            started_at=_epoch_ms_to_dt(times.get("start")),
        )

    def _warn_token_shape(self, message: str, *args: Any) -> None:
        """Report a token-bucket surprise ONCE per turn (a broken stream repeats it)."""
        if self.warned_token_shape:
            return
        self.warned_token_shape = True
        logger.warning("opencode: unexpected token accounting — " + message, *args)

    def _as_int(self, bucket: str, value: Any) -> int:
        """Coerce one stream-supplied token count, warning instead of raising.

        This is what makes ``_handle_line``'s advertised "Never raises on bad
        input" true.

        Rationale: .claude/notes/agents.md § Why token-shape drift warns instead of raising
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

        Two conventions exist in the wild: **flat**, where ``input`` already IS the
        fresh slice and ``total = input + output + reasoning + cache``, and
        **nested**, where the cache buckets are counted inside ``input`` (the
        OpenAI ``prompt_tokens`` convention) and ``total = input + output +
        reasoning``.

        The stream's own ``total`` arbitrates PER STEP. With no cache traffic the
        two agree. With no usable ``total`` the flat reading is taken, but warns
        once if cache traffic is present — that is an unverifiable assumption, and
        the original mapping bug was exactly one of those. A ``total`` matching
        NEITHER warns loudly.

        Rationale: .claude/notes/agents.md § Token accounting, per harness
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
                # The stream contradicts itself: `total` says the cache buckets
                # nest inside `input`, but `input` is too small to hold them.
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

    def on_step_finish(self, part: dict[str, Any], stamp: datetime | None) -> None:
        self.steps_finished += 1
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache_val = tokens.get("cache")
        cache = cache_val if isinstance(cache_val, dict) else {}
        raw_in = self._as_int("input", tokens.get("input") or 0)
        raw_out = self._as_int("output", tokens.get("output") or 0)
        step_reasoning = self._as_int("reasoning", tokens.get("reasoning") or 0)
        step_cw = self._as_int("cache.write", cache.get("write") or 0)
        step_cr = self._as_int("cache.read", cache.get("read") or 0)

        step_in = self._fresh_input_slice(tokens, raw_in, raw_out, step_reasoning, step_cw, step_cr)
        # Reasoning bills at the output rate but is reported apart from `output`.
        step_delta = TokenUsage(
            uncached_input_tokens=step_in,
            output_tokens=raw_out + step_reasoning,
            cache_creation_input_tokens=step_cw,
            cache_read_input_tokens=step_cr,
        )
        self.usage += step_delta
        cost = part.get("cost")
        if isinstance(cost, int | float):
            self.cost_usd += float(cost)
            self.saw_cost = True

        finish = part.get("reason")
        if isinstance(finish, str) and finish:
            self.stop_reason = finish

        completed = self._bound(stamp)
        step_start = self.step_started_at or completed
        blocks: list[ContentBlock] = []
        step_text = "".join(self.step_text_parts)
        if step_text:
            blocks.append(ContentBlock(block_type="text", sequence=0, text=step_text))
        for i, tool_id in enumerate(self.step_tool_ids, start=len(blocks)):
            blocks.append(ContentBlock(block_type="tool_use", sequence=i, tool_use_id=tool_id))

        # Tile from the previous step's finish. The RAW window only.
        self.emitter.add_generation(
            message_id=str(part.get("messageID") or "") or None,
            window=close_window(
                mark=self.gen_mark if self.gen_mark is not None else step_start, now=completed, item_start=step_start
            ),
            parts=[
                Generation(
                    blocks=blocks,
                    tokens=step_delta,
                    reasoning_tokens=step_reasoning,
                    stop_reason=finish if isinstance(finish, str) else None,
                )
            ],
        )
        self.gen_mark = completed
        # SPENT state, cleared HERE and not only in `on_step_start`: a second
        # `step_finish` with no intervening start would otherwise republish this
        # step's whole span as the next one's.
        # Rationale: .claude/notes/agents.md § Per-harness generation marks
        self.step_started_at = None
        if self.emitter.inner_turn_open:
            self.emitter.end_inner_turn(TurnEndStatus.COMPLETED, tokens=step_delta)

    def on_error(self, part: dict[str, Any]) -> None:
        """Record the CLI's own structured error, which the settle crashes on.

        Its shape varies: a nested ``error.data.message`` when the CLI has one,
        otherwise the error's ``name``; anything else degrades to its string form.
        """
        error = part.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            message = (data or {}).get("message") if isinstance(data, dict) else None
            self.error = str(message or error.get("name") or "unknown error")
        else:
            self.error = str(error or "unknown error")


@AgentRegistry.register(AgentKind.OPENCODE, OpenCodeAgentConfig)
class OpenCodeAgent(SubprocessJsonlAgent[OpenCodeAgentConfig]):
    """Runs the ``opencode`` CLI as a subprocess, one invocation per turn."""

    # `should_stop` is polled at every event boundary (tool-call granularity);
    # `system_prompt` joins the CLI's own system messages as an `instructions` file.
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
        timing_basis=TimingBasis.CLI_EPOCH_MS,
        permission_modes=frozenset({PermissionMode.PLAN, PermissionMode.BYPASS_PERMISSIONS}),
    )
    tool_names = _TOOL_NAMES
    cli_name = "OpenCode"
    docs_page = "docs/agents/OPENCODE.md"
    recognized_events = _RECOGNIZED_EVENTS
    decoder = _OpenCodeDecoder

    def __init__(
        self,
        config: OpenCodeAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
        cost_log_tags: dict[str, str] | None = None,
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``route`` is accepted for factory parity and deliberately unused: the CLI
        owns its own provider configuration (``docs/agents/OPENCODE.md``), so the
        run's Bedrock/Anthropic routing does not apply. ``task_id`` only labels
        the event stream.

        Rationale: .claude/notes/agents.md § Why the constructors declare every kwarg
        """
        super().__init__(config, route, task_id=task_id, cost_log_tags=cost_log_tags)
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._skill_dirs: list[str] = []
        # Holds the system prompt as an `instructions` file; created in start(),
        # removed in stop(). Never inside the sandbox.
        self._prompt_dir: str | None = None
        self._session_id: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
        plugin_root: Path | None = None,
    ) -> None:
        if shutil.which("opencode") is None:
            raise RuntimeError(
                "The 'opencode' CLI was not found on PATH."
                + " Install it with `npm install -g opencode-ai` (or see https://opencode.ai/docs/)."
            )
        self._skill_dirs = [str(plugin_root / "skills")] if plugin_root is not None else []
        await asyncio.to_thread(self._write_prompt_file)
        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        self._session_id = None
        self._state = AgentState.WORKING

    async def stop(self) -> None:
        await self.kill()
        self._remove_prompt_dir()
        self._mark_stopped()

    def _write_prompt_file(self) -> None:
        self._remove_prompt_dir()
        if self.config.system_prompt:
            self._prompt_dir = tempfile.mkdtemp(prefix="coder-eval-opencode-")
            Path(self._prompt_dir, _PROMPT_FILE_NAME).write_text(self.config.system_prompt, encoding="utf-8")

    def _remove_prompt_dir(self) -> None:
        if self._prompt_dir is not None:
            shutil.rmtree(self._prompt_dir, ignore_errors=True)
            self._prompt_dir = None

    def get_environment_info(self) -> dict[str, Any]:
        # Base first so the `system_prompt_semantics` run marker is always
        # present (an absent marker reads as a pre-marker run).
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "opencode_model": self.config.model,
            "opencode_pure": self.config.pure,
        }
        if self.config.variant:
            info["opencode_variant"] = self.config.variant
        if self._session_id:
            info["opencode_session_id"] = self._session_id
        return info

    # --- command construction ---------------------------------------------

    def argv(self, prompt: str) -> list[str]:
        argv = ["opencode", "run", "--format", "json"]
        if self.config.model:
            argv += ["-m", self.config.model]
        if self.working_directory:
            argv += ["--dir", self.working_directory]
        if self.config.variant:
            argv += ["--variant", self.config.variant]
        if self.config.pure:
            argv.append("--pure")
        # Auto-approves only what `permission` does not explicitly deny; an
        # approval prompt would hang an unattended run.
        argv.append("--auto")
        if self._session_id:
            argv += ["--session", self._session_id]
        argv.append("--")
        argv.append(prompt)
        return argv

    def env(self) -> dict[str, str]:
        """The CLI's full environment: the host's, plus the sandbox's contributions.

        The PATH prepend is the mock-shadowing contract (``Agent.start``): the
        sandbox's mock CLI directories must resolve BEFORE the real binaries, in
        the order given, or a task grading a mocked CLI silently exercises the
        real one. ``PLUGIN_TOOLS_DIR`` is advisory and never overrides an
        inherited value.

        Returns the WHOLE environment, seeded from ``os.environ``, whose keys
        CPython upper-cases on Windows — so ``"PATH"`` is the inherited key on
        every platform and cannot duplicate a differently-cased one. (Codex hands
        the SDK a PARTIAL dict instead, which is why it resolves the key
        case-insensitively.)
        """
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        self._inject_config_content(env)
        return env

    def _permission_config(self) -> dict[str, str] | None:
        """OpenCode ``permission`` rules for the uniform tool fields; None when none is set.

        An allowlist denies ``*`` and allows the mapped keys. Denies are written
        last, so a disallowed or ``plan``-denied key always wins.
        """
        deny_names = list(self.config.disallowed_tools or [])
        if self.config.permission_mode is PermissionMode.PLAN:
            deny_names += READ_ONLY_DENIED_TOOLS
        permission: dict[str, str] = {}
        if self.config.allowed_tools:
            permission["*"] = "deny"
            permission.update(dict.fromkeys(_NON_TOOL_PERMISSIONS, "allow"))
            for name in self.config.allowed_tools:
                permission.update(dict.fromkeys(_CLAUDE_TO_OPENCODE_PERMISSION[name], "allow"))
        for name in deny_names:
            permission.update(dict.fromkeys(_CLAUDE_TO_OPENCODE_PERMISSION[name], "deny"))
        return permission or None

    def _inject_config_content(self, env: dict[str, str]) -> None:
        """Merge skill paths, the prompt file and permission rules into ``OPENCODE_CONFIG_CONTENT``.

        With none of the three the variable is left exactly as inherited. An
        inherited value is merged, never clobbered: the host may legitimately
        configure OpenCode through the same seam. Our entries win per key.
        """
        permission = self._permission_config()
        if not (self._skill_dirs or self._prompt_dir or permission):
            return
        config: dict[str, Any] = {}
        inherited = env.get(_CONFIG_CONTENT_ENV)
        if inherited:
            try:
                parsed = json.loads(inherited)
            except json.JSONDecodeError:
                logger.warning(
                    "opencode: inherited %s is not valid JSON; replacing it with the injected config.",
                    _CONFIG_CONTENT_ENV,
                )
            else:
                if isinstance(parsed, dict):
                    config = parsed
                else:
                    logger.warning(
                        "opencode: inherited %s is not a JSON object; replacing it with the injected config.",
                        _CONFIG_CONTENT_ENV,
                    )
        if self._skill_dirs:
            skills = config.get("skills")
            skills = dict(skills) if isinstance(skills, dict) else {}
            existing = [path for path in skills.get("paths", []) if isinstance(path, str)]
            skills["paths"] = existing + [path for path in self._skill_dirs if path not in existing]
            config["skills"] = skills
        if self._prompt_dir is not None:
            prompt_file = str(Path(self._prompt_dir, _PROMPT_FILE_NAME))
            inherited_files = config.get("instructions")
            inherited_files = inherited_files if isinstance(inherited_files, list) else []
            config["instructions"] = [f for f in inherited_files if isinstance(f, str) and f != prompt_file] + [
                prompt_file
            ]
        if permission:
            # OpenCode applies the LAST matching rule, so ours go after every inherited one.
            # A host rule for a non-tool key is kept: a tool allowlist must not loosen it.
            inherited_rules = config.get("permission")
            inherited = inherited_rules if isinstance(inherited_rules, dict) else {}
            ours = {k: v for k, v in permission.items() if not (k in _NON_TOOL_PERMISSIONS and k in inherited)}
            config["permission"] = {**{k: v for k, v in inherited.items() if k not in ours}, **ours}
        env[_CONFIG_CONTENT_ENV] = json.dumps(config)

    # --- the turn ----------------------------------------------------------

    def observe(self, event: dict[str, Any]) -> None:
        """Remember the CLI's session id: it rides on the envelope and is replayed via ``--session``."""
        part = event.get("part")
        session_id = event.get("sessionID") or (part.get("sessionID") if isinstance(part, dict) else None)
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id

    def clean_exit_problem(self, decoder: JsonlDecoder) -> str | None:
        """A clean exit whose finished steps carried no token counts must not score.

        ``require_token_telemetry: false`` is the escape hatch for a provider or auth
        mode that reports no usage at all: the turn is scored with a warning.

        Rationale: .claude/notes/agents.md § Why a clean exit can still be a crash
        """
        assert isinstance(decoder, _OpenCodeDecoder)
        if decoder.steps_finished == 0 or not decoder.usage.is_empty():
            return None
        message = (
            "OpenCode exited cleanly but the turn captured zero token telemetry. It reported "
            + f"{decoder.steps_finished} finished step(s), none of which carried usable token counts "
            + f"(cost reported: {'yes' if decoder.saw_cost else 'no'}). The CLI's event or token schema may have "
            + "changed — see docs/agents/OPENCODE.md (Telemetry) before trusting any run from this CLI version."
        )
        if not self.config.require_token_telemetry:
            logger.warning("opencode: %s Scored anyway — require_token_telemetry is off.", message)
            return None
        return message
