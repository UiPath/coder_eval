"""Delegate agent — drives UiPath Autopilot's Delegate agent via a Node subprocess.

The reasoning runs in the UiPath backend; tools (shell, file, Office, PDF) execute
locally through the SDK's bundled interop process, so file-based success criteria
work as usual. This agent spawns a first-party Node host this framework ships,
``agents/delegate/delegate_host.mjs``, wrapping the public ``@uipath/delegate-sdk``
package's ``DelegateAgent`` class in a newline-JSON stdio protocol — see that
file's header for the exact wire format.

Prerequisites are documented in ``docs/agents/DELEGATE.md`` and enforced with a
clear ``AgentConfigError`` at ``start()`` (Node.js, ``npm install
@uipath/delegate-sdk``, UiPath auth). Deliberate scope reductions versus the
UiPath-internal sibling agent's more hardened adapter (no multi-generation
transcript splitting, no WAF/SSE/session-conflict/stall-resend recovery) and
every remaining ``# UNVERIFIED`` spot's rationale live in one place, not
scattered:

Rationale: .claude/notes/agents.md § Delegate agent
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
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, NoReturn
from urllib.parse import urlparse

from coder_eval.agent import Agent
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES
from coder_eval.models import (
    AgentKind,
    AgentState,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    DelegateAgentConfig,
    ResultSummary,
    SystemPromptSemantics,
    TokenUsage,
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
from coder_eval.timing import close_window
from coder_eval.utils import process_plugins

from .registry import AgentRegistry


logger = logging.getLogger(__name__)

# --- Host resolution ---------------------------------------------------------

_HOST_SCRIPT = Path(__file__).parent / "delegate" / "delegate_host.mjs"
_SDK_ENTRY_REL_PATH = Path("node_modules") / "@uipath" / "delegate-sdk" / "dist" / "index.mjs"

_UNSUPPORTED_CONFIG_FIELDS: tuple[str, ...] = (
    "allowed_tools",
    "disallowed_tools",
    "system_prompt",
    "system_prompt_file",
)
"""Config fields with no Delegate SDK equivalent. ``permission_mode`` is
deliberately absent: the SDK has no permission-prompt concept, and the field
always carries a truthy default, so warning on it would be noise every run."""

_STOP_TIMEOUT_SEC = 30.0
_TERM_GRACE_SECONDS = 5.0
_INIT_TIMEOUT_SEC = 60.0
"""Deadline for the init handshake -- otherwise a host hung inside
``agent.initialize()`` (auth refresh, backend connect) blocks ``start()``
indefinitely, unlike every turn-scoped read, which is deadline-bounded."""
_SIGKILL: signal.Signals = getattr(signal, "SIGKILL", signal.SIGTERM)

# The SDK event types this host forwards verbatim that carry model-turn content.
# `session_start` / `step` / `done` are recognized-but-informational; anything
# else is logged and ignored rather than silently dropped.
_TEXT_EVENT_TYPES = frozenset({"thinking", "message"})


def _env(bare_name: str) -> str | None:
    """Read a ``DELEGATE_``-namespaced auth var, falling back to the bare name.

    coder_eval controls these names (it forwards the values into the SDK's
    ``auth`` object; the SDK never reads process.env itself), so the bare
    spellings (``AUTH_TOKEN``, ``TENANT_ID``, ...) collide with names other
    tooling (npm, Vault, Terraform) commonly exports. The namespaced spelling
    is checked first; the bare one stays for delegate-cli compatibility.
    """
    return os.environ.get(f"DELEGATE_{bare_name}") or os.environ.get(bare_name)


def _candidate_install_roots() -> list[Path]:
    """Ancestor-walk search roots: cwd, its ancestors, and home.

    Mirrors Node's own module resolution so an ``npm install`` run in the
    launch directory, any ancestor, or (where npm lands a package when the cwd
    has no ``package.json``) home, is found with zero configuration.
    """
    cwd = Path.cwd().resolve()
    roots: list[Path] = [cwd, *cwd.parents]
    home = Path.home().resolve()
    if home not in roots:
        roots.append(home)
    return roots


def _resolve_sdk_entry() -> Path:
    """Locate the installed ``@uipath/delegate-sdk``'s ``dist/index.mjs``.

    Resolution order: ``DELEGATE_SDK_PATH`` (explicit file path) ->
    ``DELEGATE_SDK_NODE_MODULES`` (explicit install root, probed exactly) ->
    ancestor walk from cwd (plus home), so an ``npm install`` anywhere in that
    chain — including this module's own ``agents/delegate/`` directory, which
    ships a ``package.json`` naming the dependency — is found automatically.

    Raises:
        AgentConfigError: no install found anywhere searched.
    """
    explicit = os.environ.get("DELEGATE_SDK_PATH")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise AgentConfigError(
                f"DELEGATE_SDK_PATH={path} does not point to a file. Point it at "
                + "@uipath/delegate-sdk's dist/index.mjs."
            )
        return path

    root_override = os.environ.get("DELEGATE_SDK_NODE_MODULES")
    if root_override:
        path = (Path(root_override).expanduser().resolve() / _SDK_ENTRY_REL_PATH).resolve()
        if not path.is_file():
            raise AgentConfigError(
                f"DELEGATE_SDK_NODE_MODULES={root_override}: @uipath/delegate-sdk not found at {path}. "
                + "Run `npm install @uipath/delegate-sdk` there, or set DELEGATE_SDK_PATH directly."
            )
        return path

    searched: list[Path] = []
    for root in _candidate_install_roots():
        candidate = (root / _SDK_ENTRY_REL_PATH).resolve()
        searched.append(candidate)
        if candidate.is_file():
            return candidate

    searched_block = "\n  ".join(str(p) for p in searched)
    raise AgentConfigError(
        "@uipath/delegate-sdk not found. Searched the cwd, its ancestors, and home:\n"
        + f"  {searched_block}\n"
        + "Run `npm install @uipath/delegate-sdk` (plain, public install — no token needed), "
        + "e.g. in this framework's own agents/delegate/ directory, or set DELEGATE_SDK_NODE_MODULES "
        + "to the install root, or DELEGATE_SDK_PATH to the dist/index.mjs file directly. "
        + "See docs/agents/DELEGATE.md."
    )


def _resolve_bundled_skills_path(plugins: list[dict[str, Any]] | None) -> str | None:
    """Translate the first ``local`` plugin's ``path`` to the SDK's ``bundledSkillsPath``.

    The SDK expects ``bundledSkillsPath`` to point at ONE directory whose direct
    children are skill folders — a different shape than ``agents/_skills.py``'s
    shared resolver, which enumerates individual skill directories for CLIs that
    take a repeated ``--skill <dir>`` list (OpenCode/Pi). That resolver does not
    fit here, so this mirrors the coder_eval_uipath sibling's own mapping
    instead: ``<plugin.path>/skills``, first plugin wins.
    """
    expanded = process_plugins(plugins or [], log=logger)
    if not expanded:
        return None
    first = expanded[0]
    if "path" not in first:
        logger.warning("delegate: plugin missing 'path' field — skipping: %r", first)
        return None
    if len(expanded) > 1:
        others = [p.get("path", repr(p)) for p in expanded[1:]]
        logger.warning("delegate: only one plugin is supported; using %s and ignoring: %s", first["path"], others)
    return str(Path(first["path"]) / "skills")


def _parse_usage(raw: Any) -> TokenUsage | None:
    """Parse the SDK's per-turn usage payload (from ``getLastTurnUsage()``) into ``TokenUsage``.

    CONFIRMED (reading the installed ``@uipath/delegate-sdk@0.1.12``'s bundled
    ``dist/index.mjs``): no event this host forwards ever carries a ``usage``
    field -- the SDK's per-turn token accounting lives only in its internal
    store, reachable through ``DelegateAgent.getLastTurnUsage()``, which
    ``delegate_host.mjs`` calls after ``sendMessage()`` resolves and attaches
    to the ``send_ok`` message as ``usage``. That getter's shape, from the
    SDK's own ``setUsage`` store action: ``{promptTokens, completionTokens,
    promptTokensCached, cacheCreationTokens, turnTokenUnits,
    contextBreakdown}``. ``promptTokens`` is the TOTAL input token count
    (cached + uncached, OpenAI-style); ``promptTokensCached`` is the
    cache-READ subset of it, so ``uncached = promptTokens - promptTokensCached``.
    Falls back to 0 for anything absent (e.g. before the backend's first
    internal usage report), mirroring the project's "warn on drift, never
    raise" contract -- a future SDK release renaming one of these fields
    degrades to zero tokens for that bucket, not a crash.
    """
    if not isinstance(raw, dict):
        return None

    def _int(key: str) -> int:
        value = raw.get(key)
        if isinstance(value, bool):
            return 0
        return value if isinstance(value, int) and value >= 0 else 0

    prompt_total = _int("promptTokens")
    prompt_cached = _int("promptTokensCached")
    output_tokens = _int("completionTokens")
    cache_creation = _int("cacheCreationTokens")
    if prompt_total == 0 and output_tokens == 0 and prompt_cached == 0 and cache_creation == 0:
        if raw:
            logger.warning("delegate: usage payload matched none of the known bucket spellings: %r", sorted(raw))
        return None
    return TokenUsage(
        uncached_input_tokens=max(prompt_total - prompt_cached, 0),
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=prompt_cached,
    )


class _TurnState:
    """Per-``communicate()`` accumulator.

    One ``AssistantMessage`` per turn (see module docstring's "No
    multi-generation transcript splitting"), so this is far smaller than the
    per-round-trip segment machinery a richer transcript would need.
    """

    def __init__(self, *, iteration: int, user_input: str, model: str | None) -> None:
        self.iteration = iteration
        self.user_input = user_input
        self.turn_id = f"delegate-{iteration}"
        self.started_at = time.monotonic()
        self.started_dt = datetime.now()

        self.content_blocks: list[ContentBlock] = []
        self.text_parts: list[str] = []
        self.tool_use_ids: list[str] = []
        self.open_tools: dict[str, CommandTelemetry] = {}
        self.sequence = 0
        self.message_events = 0
        # Backend round-trips begun. A tool-only reply streams no text, so each call
        # after the first opens once the previous call's tools have all returned.
        self.api_calls = 0

        self.model_used: str | None = model
        self.usage: TokenUsage | None = None
        self.final_response: str | None = None
        self.error_message: str | None = None
        self.max_turns_exhausted = False
        self.finalized = False

    @property
    def agent_output(self) -> str:
        return self.final_response if self.final_response is not None else "".join(self.text_parts)


@AgentRegistry.register(AgentKind.DELEGATE, DelegateAgentConfig)
class DelegateAgent(Agent[DelegateAgentConfig]):
    """Drives UiPath Autopilot's Delegate agent through a persistent Node host subprocess.

    See module docstring for prerequisites and the deliberate scope reductions
    versus the UiPath-only sibling plugin's more hardened adapter.
    """

    # The host streams one event per model/tool step, checked after each.
    supports_cooperative_stop: ClassVar[bool] = True

    # The Delegate SDK has no CLI-style system-prompt knob to append/replace onto.
    system_prompt_semantics: ClassVar[SystemPromptSemantics] = "unknown"

    def __init__(
        self,
        config: DelegateAgentConfig,
        route: ApiRoute | None = None,
        *,
        task_id: str = "unknown",
    ) -> None:
        """Every parameter the agent factory can pass is DECLARED, not absorbed.

        ``route`` is accepted for factory parity and deliberately unused — the
        Delegate SDK has its own UiPath auth path, not Anthropic-style routing.
        """
        self.config = config
        self.route = route
        self.task_id = task_id
        self.working_directory: str | None = None
        self._env_path_prepend: list[str] = []
        self._plugin_tools_dir: str | None = None
        self._session_id: str | None = None
        self._state = AgentState.WORKING

        self._sdk_entry: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._pgid: int | None = None

    # --- lifecycle -----------------------------------------------------------

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        if shutil.which("node") is None:
            raise AgentConfigError(
                "Node.js was not found on PATH. Install it (https://nodejs.org/), "
                + "then `npm install @uipath/delegate-sdk`. See docs/agents/DELEGATE.md."
            )
        self._sdk_entry = _resolve_sdk_entry()

        ignored = [f for f in _UNSUPPORTED_CONFIG_FIELDS if getattr(self.config, f, None)]
        if ignored:
            logger.warning(
                "delegate: %s set but has no Delegate SDK equivalent — NOT enforced; do not rely on "
                + "them as a boundary (see docs/agents/DELEGATE.md).",
                ", ".join(ignored),
            )

        self.working_directory = working_directory
        self._env_path_prepend = list(env_path_prepend or [])
        self._plugin_tools_dir = plugin_tools_dir
        self._session_id = self.config.session_id
        self._state = AgentState.WORKING

        await self._spawn_and_init()

    async def _spawn_and_init(self) -> None:
        assert self._sdk_entry is not None, "start() must resolve the SDK entry before spawning"
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        # Respect an operator's own telemetry choice; only default it off.
        env.setdefault("DELEGATE_TELEMETRY_DISABLED", "1")

        await self._cancel_drain_tasks()
        self._stdout_queue = asyncio.Queue()
        self._stderr_lines.clear()
        self._process = await asyncio.create_subprocess_exec(
            "node",
            str(_HOST_SCRIPT),
            str(self._sdk_entry),
            cwd=str(_HOST_SCRIPT.parent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=STDOUT_LINE_LIMIT_BYTES,
            # Own session/process group, so a force-kill can killpg the SDK's
            # bundled interop child too. POSIX-only knob; harmless False elsewhere.
            start_new_session=os.name == "posix",
        )
        self._pgid = self._process.pid if os.name == "posix" else None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stdout_task = asyncio.create_task(self._drain_stdout(self._process.stdout))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))

        init_options = self._build_init_options()
        await self._send_command({"cmd": "init", "options": init_options})
        try:
            ack = await asyncio.wait_for(self._read_until(("init_ok", "init_error")), timeout=_INIT_TIMEOUT_SEC)
        except TimeoutError as exc:
            await self._force_kill_host()
            self._process = None
            raise AgentConfigError(
                f"Delegate SDK init did not respond within {_INIT_TIMEOUT_SEC:.0f}s "
                + "(hung auth refresh or backend connect?)."
            ) from exc
        if ack.get("type") == "init_error":
            raise AgentConfigError(f"Delegate SDK init failed: {ack.get('message', 'unknown error')}")

    def _build_init_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "workingDirectory": self.working_directory,
            "enableComputerUse": self.config.enable_computer_use,
        }
        if self.config.model:
            options["model"] = self.config.model
        if self.config.effort:
            options["effort"] = self.config.effort
        if self.config.project_id:
            options["projectId"] = self.config.project_id
        if self.config.session_id:
            options["sessionId"] = self.config.session_id
        if self._env_path_prepend:
            options["shellPathPrepend"] = list(self._env_path_prepend)
        backend_url = os.environ.get("DELEGATE_BACKEND_URL")
        if backend_url:
            options["backendUrl"] = backend_url
        environment = os.environ.get("DELEGATE_ENV")
        if environment:
            options["environment"] = environment
        auth_token = _env("AUTH_TOKEN")
        if auth_token:
            auth: dict[str, Any] = {
                "accessToken": auth_token,
                "tenantId": _env("TENANT_ID"),
                "organizationId": _env("ORG_ID"),
            }
            # CONFIRMED LIVE: when `environment` (rather than `backendUrl`) is set,
            # the SDK resolves the backend URL from THIS auth object's
            # organizationName/tenantName fields, not from ORG_SLUG/TENANT_SLUG
            # process.env directly (that pairing is documented only in the SDK's
            # own error message, aimed at delegate-cli's env-var-driven wrapper —
            # the SDK class we drive here never reads those two vars itself).
            org_slug = _env("ORG_SLUG")
            tenant_slug = _env("TENANT_SLUG")
            if org_slug:
                auth["organizationName"] = org_slug
            if tenant_slug:
                auth["tenantName"] = tenant_slug
            options["auth"] = auth
        # list[LocalPluginConfig] is not list[dict[str, Any]] under list invariance.
        skills_path = _resolve_bundled_skills_path(self.config.plugins)  # type: ignore[arg-type]
        if skills_path:
            options["bundledSkillsPath"] = skills_path
        return options

    async def stop(self) -> None:
        await self._teardown_host()
        self._mark_stopped()

    async def kill(self) -> None:
        await self._force_kill_host()

    def kill_sync(self) -> None:
        """SIGKILL the host (watchdog thread; must not await).

        Drops the handle immediately rather than waiting for a reap this
        synchronous path cannot await: the next ``communicate()`` must
        respawn, not write a ``send`` into a pipe whose far end is dead or
        dying.
        """
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, _SIGKILL)
        self._process = None
        self._sweep_pgid()

    async def _force_kill_host(self) -> None:
        proc = self._process
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
        self._sweep_pgid()

    def _sweep_pgid(self) -> None:
        """SIGKILL the host's process group (POSIX only), reaping any orphaned child.

        The host itself is already killed by ``proc.kill()``/``os.kill`` above;
        this additionally reaps the SDK's bundled interop process
        (``UiPath.Aria.ComputerUse.Api``) if it outlived the host, mirroring
        ``opencode_agent.py``'s ``_sweep_process_groups``.
        """
        if os.name == "posix" and self._pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(self._pgid, _SIGKILL)
        self._pgid = None

    def _stderr_tail(self) -> str:
        return "\n".join(list(self._stderr_lines)[-20:]) or "(no stderr captured)"

    async def _cancel_drain_tasks(self) -> None:
        """Cancel and await the stdout/stderr drain tasks of the CURRENT host, if any.

        Must run before ``_spawn_and_init`` reassigns ``_stdout_task`` /
        ``_stderr_task`` for a respawn, or the previous host's tasks are
        dropped uncancelled rather than torn down (they self-terminate on
        their pipe's EOF either way, but dropping a live task silently is the
        pattern this project's teardown paths avoid elsewhere).
        """
        # Narrowed to cancellation alone (mirrors isolation/docker_runner.py's
        # same cancel-then-await teardown) so a genuine KeyboardInterrupt /
        # SystemExit -- or a CancelledError delivered to the CALLER during
        # this same await -- still propagates instead of being swallowed.
        # _drain_stdout/_drain_stderr already log and handle their own
        # non-cancellation failures internally.
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._stdout_task = None
        self._stderr_task = None

    async def _teardown_host(self) -> None:
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(Exception):
                await self._send_command({"cmd": "destroy"})
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=_STOP_TIMEOUT_SEC)
        await self._force_kill_host()
        await self._cancel_drain_tasks()
        self._process = None

    def get_environment_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "delegate_model": self.config.model,
        }
        if self.config.effort:
            info["delegate_effort"] = self.config.effort
        if self.config.enable_computer_use:
            info["delegate_enable_computer_use"] = True
        # DELEGATE_BACKEND_URL wins over DELEGATE_ENV when both are set (see
        # _build_init_options/docs/agents/DELEGATE.md), so recording delegate_env
        # here too would assert a routing decision the SDK never made. Host only
        # (never the full URL, which can carry embedded credentials).
        backend_url = os.environ.get("DELEGATE_BACKEND_URL")
        environment = os.environ.get("DELEGATE_ENV")
        if backend_url:
            info["delegate_backend_url_host"] = urlparse(backend_url).hostname
        elif environment:
            info["delegate_env"] = environment
        if self._session_id:
            info["delegate_session_id"] = self._session_id
        return info

    # --- the turn --------------------------------------------------------

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
            raise RuntimeError("DelegateAgent.start() must be called before communicate()")

        if self._process is None or self._process.returncode is not None:
            # A prior cooperative stop or crash left no live host: respawn
            # fresh rather than fail fast. Session-conflict-specific recovery
            # is deferred (see module docstring); this simpler policy covers
            # both "the previous turn stopped cleanly" and "it crashed".
            try:
                await self._spawn_and_init()
            except AgentConfigError as exc:
                # Node/the SDK install were already validated once in start();
                # a failure respawning mid-run is a transient backend/auth
                # hiccup, not a missing prerequisite -- make it retryable
                # instead of ending the task outright.
                raise AgentCrashError(str(exc)) from exc

        self._begin_turn()
        collector = EventCollector()

        def emit(event: StreamEvent) -> None:
            collector.on_event(event)
            if stream_callback is not None:
                safe_emit(stream_callback, event)

        state = _TurnState(
            iteration=self._iteration,
            user_input=user_input,
            model=self.config.model,
        )

        emit(
            AgentStartEvent(task_id=self.task_id, prompt=user_input, iteration=self._iteration, model=self.config.model)
        )
        emit(TurnStartEvent(task_id=self.task_id, turn_id=state.turn_id, model=self.config.model))

        deadline = None if timeout is None else time.monotonic() + timeout
        stopped_early = False
        try:
            await self._send_command({"cmd": "send", "prompt": user_input, "sessionId": self._session_id})

            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    await self._timeout_turn(state, collector, emit, timeout or 0.0)

                try:
                    msg = await self._read_next_message(deadline)
                except TimeoutError:
                    # The deadline elapsed WHILE blocked reading, not just at
                    # the top-of-loop check above -- route through the same
                    # timeout kernel (kills the host, clears self._process) so
                    # this is never misreported as a generic AgentCrashError,
                    # which would leave a "send" in flight on a host the next
                    # communicate() would wrongly reuse.
                    await self._timeout_turn(state, collector, emit, timeout or 0.0)
                if msg is None:
                    # The stream is closed. Not always because the process
                    # already exited: _drain_stdout also signals this on a
                    # buffer-limit overrun or its own unexpected exception,
                    # where the host can still be alive -- force-kill before
                    # dropping the handle so the NEXT communicate() respawns
                    # cleanly instead of orphaning it.
                    await self._abandon_host_and_crash(
                        state, collector, emit, "Delegate host closed its output stream unexpectedly"
                    )

                mtype = msg.get("type")
                if mtype == "send_ok":
                    self._handle_send_ok(msg, state)
                    break
                if mtype == "send_error":
                    # Reuse of a live host after a recoverable `send_error` is
                    # not attempted: force-kill so the next communicate() always
                    # respawns onto a fresh queue rather than risk consuming a
                    # stale onEvent callback the SDK fires after this rejection.
                    await self._abandon_host_and_crash(
                        state, collector, emit, f"Delegate send failed: {msg.get('message', 'unknown error')}"
                    )
                if mtype == "fatal":
                    # The host exits right after writing this line (every
                    # `fatal` site calls process.exit) -- force-kill is then a
                    # no-op, but stays here to match the EOF branch exactly
                    # rather than trust that invariant from this side too.
                    await self._abandon_host_and_crash(
                        state, collector, emit, f"Delegate host crashed: {msg.get('message', 'unknown error')}"
                    )
                if mtype in ("protocol_error", "destroy_error"):
                    logger.warning("delegate: host reported %s: %s", mtype, msg.get("message"))
                    continue

                self._handle_event(msg, state, emit)

                if max_turns is not None and state.api_calls > max_turns:
                    state.max_turns_exhausted = True
                    await self._abandon_host_after_loop_exit()
                    break
                if should_stop is not None and should_stop():
                    stopped_early = True
                    await self._abandon_host_after_loop_exit()
                    break

            if stopped_early:
                status = AgentEndStatus.STOPPED_EARLY
            elif state.max_turns_exhausted:
                status = AgentEndStatus.MAX_TURNS_EXHAUSTED
            else:
                status = AgentEndStatus.COMPLETED
            self._finalize_turn(state, status, emit)
            record = collector.build_turn_record()
            self._end_turn_ok()
            return record

        except (AgentCrashError, TurnTimeoutError):
            raise
        except asyncio.CancelledError:

            def finalize_on_cancel(
                status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None
            ) -> None:
                self._finalize_turn(state, status, emit, crashed=crashed, crash_reason=crash_reason)

            self._finalize_external_cancel(finalize_on_cancel)
            self._capture_partial_turn(collector)
            raise
        except Exception as e:
            # The host may or may not still be alive (e.g. a BrokenPipeError
            # from a dead stdin) -- force-kill so a retry always respawns
            # rather than write into, or read stale events from, this host.
            await self._force_kill_host()
            self._process = None
            self._crash_turn(state, collector, emit, f"Delegate turn failed: {e!s}", cause=e)
            raise  # unreachable — _crash_turn is NoReturn

    def _handle_event(self, msg: dict[str, Any], state: _TurnState, emit: Callable[[StreamEvent], None]) -> None:
        """Dispatch one forwarded SDK event. Never raises on unrecognized shape."""
        event_type = msg.get("type")
        session_id = msg.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
        model = msg.get("model")
        if isinstance(model, str) and model:
            state.model_used = model
        usage = _parse_usage(msg.get("usage"))
        if usage is not None:
            state.usage = usage

        if state.api_calls == 0 and (event_type in _TEXT_EVENT_TYPES or event_type == "tool_call"):
            state.api_calls = 1

        if event_type in _TEXT_EVENT_TYPES:
            text = msg.get("content")
            if isinstance(text, str) and text:
                state.text_parts.append(text)
                if event_type == "message":
                    state.message_events += 1
                    state.content_blocks.append(
                        ContentBlock(block_type="text", sequence=len(state.content_blocks), text=text)
                    )
                    emit(TextChunkEvent(task_id=self.task_id, turn_id=state.turn_id, text=text))
                else:
                    state.content_blocks.append(
                        ContentBlock(block_type="thinking", sequence=len(state.content_blocks), thinking=text)
                    )
        elif event_type == "tool_call":
            self._handle_tool_call(msg, state, emit)
        elif event_type == "tool_result":
            self._handle_tool_result(msg, state, emit)
            if not state.open_tools:
                state.api_calls += 1
        elif event_type == "error":
            message = msg.get("message") or msg.get("content") or "unknown error"
            state.error_message = str(message)
            logger.warning("delegate: SDK reported an error event: %s", state.error_message)
        elif event_type in ("session_start", "step", "done"):
            pass  # informational; no telemetry to record
        else:
            logger.debug("delegate: unrecognized event type %r", event_type)

    def _handle_tool_call(self, msg: dict[str, Any], state: _TurnState, emit: Callable[[StreamEvent], None]) -> None:
        # UNVERIFIED: exact id-field spelling.
        tool_id = str(msg.get("toolId") or msg.get("id") or msg.get("callId") or uuid.uuid4())
        tool_name = str(msg.get("toolName") or msg.get("tool") or "unknown")
        parameters = msg.get("input")
        parameters = parameters if isinstance(parameters, dict) else {}
        state.sequence += 1
        telemetry = CommandTelemetry(
            tool_name=tool_name,
            tool_id=tool_id,
            assistant_turn_index=state.message_events,
            timestamp=datetime.now(),
            execution_started_at=datetime.now(),
            parameters=parameters,
            sequence_number=state.sequence,
        )
        state.open_tools[tool_id] = telemetry
        state.tool_use_ids.append(tool_id)
        state.content_blocks.append(
            ContentBlock(block_type="tool_use", sequence=len(state.content_blocks), tool_use_id=tool_id)
        )
        emit(ToolStartEvent(task_id=self.task_id, turn_id=state.turn_id, tool=telemetry))

    def _handle_tool_result(self, msg: dict[str, Any], state: _TurnState, emit: Callable[[StreamEvent], None]) -> None:
        tool_id = str(msg.get("toolId") or msg.get("id") or msg.get("callId") or "")
        telemetry = state.open_tools.pop(tool_id, None)
        if telemetry is None:
            # A result with no matching open call (id mismatch or unknown shape).
            # Never drop it: synthesize a telemetry row rather than lose the
            # event.
            state.sequence += 1
            telemetry = CommandTelemetry(
                tool_name="unknown",
                tool_id=tool_id or str(uuid.uuid4()),
                assistant_turn_index=state.message_events,
                timestamp=datetime.now(),
                sequence_number=state.sequence,
            )
        error_text = msg.get("error")
        output = msg.get("output") if "output" in msg else msg.get("content")
        completed = datetime.now()
        telemetry.execution_completed_at = completed
        if telemetry.execution_started_at is not None:
            telemetry.duration_ms = (completed - telemetry.execution_started_at).total_seconds() * 1000
        if error_text:
            status = ToolEndStatus.ERROR
            telemetry.result_status = "error"
            telemetry.error_message = str(error_text)
        else:
            status = ToolEndStatus.OK
            telemetry.result_status = "success"
        if isinstance(output, str):
            telemetry.result_summary = output
        elif output is not None:
            # A dict/list result must not be silently dropped -- serialize
            # rather than lose it (CE043: stored whole, never truncated).
            telemetry.result_summary = json.dumps(output)
        else:
            telemetry.result_summary = None
        emit(ToolEndEvent(task_id=self.task_id, turn_id=state.turn_id, tool=telemetry, status=status))

    def _handle_send_ok(self, msg: dict[str, Any], state: _TurnState) -> None:
        # CONFIRMED (reading the installed SDK's bundled source):
        # sendMessage()'s resolved value is always a plain string, never an
        # object -- `usage`/`sessionId` are NOT nested under it. delegate_host.mjs
        # instead reads them off `getLastTurnUsage()`/`getSessionId()` after
        # sendMessage() resolves and attaches them to this message's own
        # top level (see delegate_host.mjs's wire-protocol header comment).
        result = msg.get("result")
        if isinstance(result, str):
            state.final_response = result
        session_id = msg.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
        usage = _parse_usage(msg.get("usage"))
        if usage is not None:
            state.usage = usage

    def _close_open_tools(self, state: _TurnState, emit: Callable[[StreamEvent], None]) -> None:
        for tool_id, telemetry in list(state.open_tools.items()):
            telemetry.result_status = "unknown"
            emit(
                ToolEndEvent(
                    task_id=self.task_id, turn_id=state.turn_id, tool=telemetry, status=ToolEndStatus.UNRESOLVED
                )
            )
            del state.open_tools[tool_id]

    def _finalize_turn(
        self,
        state: _TurnState,
        status: AgentEndStatus,
        emit: Callable[[StreamEvent], None],
        *,
        crashed: bool = False,
        crash_reason: str | None = None,
    ) -> None:
        if state.finalized:
            return
        state.finalized = True
        self._close_open_tools(state, emit)

        usage = state.usage or TokenUsage()
        if not usage.is_empty():
            cost = calculate_cost(
                state.model_used or self.config.model or "",
                uncached_input_tokens=usage.uncached_input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_input_tokens,
                cache_read_tokens=usage.cache_read_input_tokens,
            )
            if cost is not None:
                usage = usage.model_copy(update={"total_cost_usd": cost})

        messages = []
        if state.content_blocks:
            completed = datetime.now()
            # Single generation window for the whole turn (see module docstring's
            # "No multi-generation transcript splitting"): tiles from the turn's
            # own start, since there is no prior emission to tile from.
            started, generation_ms = close_window(mark=state.started_dt, now=completed)
            messages.append(
                AssistantMessage(
                    started_at=started,
                    completed_at=completed,
                    generation_duration_ms=generation_ms,
                    content_blocks=state.content_blocks,
                    tool_use_ids=list(state.tool_use_ids),
                    input_tokens=usage.uncached_input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_tokens=usage.cache_creation_input_tokens,
                    cache_read_tokens=usage.cache_read_input_tokens,
                    model=state.model_used,
                    message_id=f"{state.turn_id}-msg-0",
                )
            )

        emit(
            TurnEndEvent(
                task_id=self.task_id,
                turn_id=state.turn_id,
                status=TurnEndStatus(status.value),
                tokens=usage if not usage.is_empty() else None,
            )
        )
        emit(
            AgentEndEvent(
                task_id=self.task_id,
                status=status,
                usage=usage,
                iteration=state.iteration,
                user_input=state.user_input,
                agent_output=state.agent_output,
                model_used=state.model_used,
                assistant_turn_count=max(state.message_events, 1) if not crashed else state.message_events,
                messages=messages,
                num_turns=None if crashed else max(state.api_calls, 1),
                max_turns_exhausted=state.max_turns_exhausted,
                result_summary=ResultSummary(
                    is_error=crashed,
                    subtype=status.value,
                    stop_reason=None,
                    result=crash_reason or state.error_message,
                ),
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - state.started_at,
            )
        )

    async def _abandon_host_after_loop_exit(self) -> None:
        """Force-kill after ``max_turns``/cooperative-stop; next turn respawns."""
        await self._force_kill_host()
        self._process = None

    async def _abandon_host_and_crash(
        self,
        state: _TurnState,
        collector: EventCollector,
        emit: Callable[[StreamEvent], None],
        message: str,
    ) -> NoReturn:
        """Force-kill the current host, drop the handle, and crash the turn.

        Shared by every mid-``communicate()`` failure kernel that must not let
        the NEXT ``communicate()`` reuse this host: reuse risks writing a
        ``send`` into a dead pipe, or consuming an ``onEvent`` callback the SDK
        fires after this failure as the retry's own result.
        """
        await self._force_kill_host()
        self._process = None
        self._crash_turn(state, collector, emit, f"{message}. stderr tail:\n{self._stderr_tail()}")

    def _crash_turn(
        self,
        state: _TurnState,
        collector: EventCollector,
        emit: Callable[[StreamEvent], None],
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        def finalize(status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None) -> None:
            self._finalize_turn(state, status, emit, crashed=crashed, crash_reason=crash_reason)

        try:
            self._finalize_and_raise_crash(finalize, message, cause=cause)
        finally:
            self._capture_partial_turn(collector)

    async def _timeout_turn(
        self, state: _TurnState, collector: EventCollector, emit: Callable[[StreamEvent], None], timeout: float
    ) -> NoReturn:
        await self._force_kill_host()
        # Drop the handle so the NEXT communicate() respawns rather than reuse
        # a killed process (or, worse, a "send" still nominally in flight).
        self._process = None

        def finalize(status: AgentEndStatus, *, crashed: bool = False, crash_reason: str | None = None) -> None:
            self._finalize_turn(state, status, emit, crashed=crashed, crash_reason=crash_reason)

        try:
            self._finalize_and_raise_timeout(finalize, timeout)
        finally:
            self._capture_partial_turn(collector)

    # --- wire I/O --------------------------------------------------------

    async def _send_command(self, command: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Delegate host is not running")
        self._process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_next_message(self, deadline: float | None) -> dict[str, Any] | None:
        """Read the next queued host message, or raise ``TimeoutError`` past ``deadline``."""
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        return await asyncio.wait_for(self._stdout_queue.get(), timeout=timeout)

    async def _read_until(self, accepted_types: tuple[str, ...]) -> dict[str, Any]:
        while True:
            msg = await self._stdout_queue.get()
            if msg is None:
                # As in communicate()'s loop: the host may still be alive
                # (a buffer overrun or drain exception, not necessarily exit).
                await self._force_kill_host()
                self._process = None
                tail = self._stderr_tail()
                raise AgentCrashError(f"Delegate host exited before responding. stderr tail:\n{tail}")
            if msg.get("type") in accepted_types:
                return msg
            if msg.get("type") in ("protocol_error", "fatal"):
                logger.warning("delegate: host reported %s during init: %s", msg.get("type"), msg.get("message"))

    async def _drain_stdout(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                try:
                    line = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    logger.error("delegate: a stdout line exceeded the buffer limit: %s", exc)
                    self._stderr_lines.append(f"[host] stdout line exceeded buffer limit: {exc}")
                    await self._stdout_queue.put(None)
                    return
                if not line:
                    await self._stdout_queue.put(None)
                    return
                raw = line.decode("utf-8", "replace").strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    # The SDK itself can write a non-JSON diagnostic line to
                    # stdout at import time (confirmed live) — skip, don't crash.
                    logger.debug("delegate: skipping non-JSON stdout line: %s", raw[:200])
                    continue
                if isinstance(obj, dict):
                    await self._stdout_queue.put(obj)
        except BaseException:
            logger.exception("delegate: stdout drain failed")
            await self._stdout_queue.put(None)
            raise

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                try:
                    line = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    continue
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
                    logger.debug("delegate[stderr]: %s", text)
        except Exception:
            logger.exception("delegate: stderr drain failed")
