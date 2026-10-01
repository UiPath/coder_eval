"""Delegate agent — drives UiPath Autopilot's Delegate agent via a Node subprocess.

The reasoning runs in the UiPath backend; tools (shell, file, Office, PDF) execute
locally through the SDK's bundled interop process, so file-based success criteria
work as usual. This agent spawns the host that the public ``@uipath/delegate-stdio``
npm package ships (``dist/delegate_stdio.mjs``). That host runs the Delegate agent
as a subprocess speaking newline-delimited JSON over stdio. ``@uipath/delegate-sdk``
is a dependency of that package, so installing it is the complete install.

Wire protocol (one JSON object per line; the package README is the SSOT)::

    stdin                                   stdout
    {"cmd":"init","options":{...}}      ->  {"type":"init_ok"}
    {"cmd":"send","prompt":..,              {"type":"event","event":{...}}  (zero or more)
            "sessionId":..}                 {"type":"usage","usage":{..}}   (one per model call)
                                        ->  {"type":"result","response":..,"sessionId":..,
                                             "usage":{..},"turnUsages":[..],"model":..}
    {"cmd":"destroy"}                   ->  {"type":"destroyed"}
                                            {"type":"error","message":..}   (any command)

Prerequisites are documented in ``docs/agents/DELEGATE.md`` and enforced with a
clear ``AgentConfigError`` at ``start()`` (Node.js, ``npm install
@uipath/delegate-stdio``, UiPath auth).

Rationale: .claude/notes/agents.md § Delegate agent
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

_HOST_PACKAGE = "@uipath/delegate-stdio"
_HOST_BUNDLE_NAME = "delegate_stdio.mjs"
_HOST_BUNDLE_REL_PATH = Path("node_modules") / "@uipath" / "delegate-stdio" / "dist" / _HOST_BUNDLE_NAME
_AGENT_INSTALL_ROOT = Path(__file__).resolve().parent / "delegate"
"""This agent's own directory; it ships a ``package.json`` that names the host package."""

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

_INIT_CONFIG_ERROR_MARKERS = (
    "auth required",
    "authentication",
    "unauthorized",
    "invalid credentials",
    "invalid token",
    "expired",
    "401",
    "403",
    "requires org/tenant slugs",
    "unknown env",
)
"""Substrings of an init ``error`` message that a retry cannot fix: missing or rejected
auth, or a bad ``DELEGATE_ENV``. Any other init error is retryable."""

_INIT_CONFIG_ERROR_HINT = (
    "coder_eval sends the auth and the org/tenant slugs as the host's `auth` init option, read from "
    + "DELEGATE_AUTH_TOKEN / DELEGATE_TENANT_ID / DELEGATE_ORG_ID / DELEGATE_ORG_SLUG / DELEGATE_TENANT_SLUG "
    + "(or the same names without DELEGATE_), and keeps the host's own AUTH_TOKEN / TENANT_ID / ORG_ID / "
    + "ORG_LOGICAL_NAME / TENANT_NAME out of its environment. A @uipath/delegate-stdio without the `auth` "
    + "init option ignores it. See docs/agents/DELEGATE.md."
)
"""Appended to a config-class init error, whose host message names the host's own variables."""

# Event types (inside the host's `event` frames) that carry model-turn content.
# `session_start` / `done` are informational; anything else is logged and ignored.
_TEXT_EVENT_TYPES = frozenset({"thinking", "message"})
_FAILED_TOOL_STATUSES = frozenset({"failed", "interrupted"})
"""``toolStatus`` values the SDK's ``tool_result`` carries for a tool that did not complete."""


def _tool_id(event: dict[str, Any]) -> str:
    return str(event.get("toolId") or "")


def _orphan_tool_name(event: dict[str, Any], tool_id: str) -> str:
    """The name on a ``tool_result`` with no open call; the SDK falls back to the tool id when it has none."""
    name = event.get("toolName")
    return name if isinstance(name, str) and name and name != tool_id else "unknown"


def _orphan_tool_args(output: Any) -> dict[str, Any]:
    """The call's args, which a failed lookup echoes back in its ``toolResult``."""
    args = output.get("args") if isinstance(output, dict) else None
    return args if isinstance(args, dict) else {}


def _env(bare_name: str) -> str | None:
    """Read a ``DELEGATE_``-namespaced variable, falling back to the bare name.

    The bare spellings (``AUTH_TOKEN``, ``TENANT_ID``, ...) collide with names
    other tooling (npm, Vault, Terraform) commonly exports, so the namespaced
    spelling is checked first.
    """
    return os.environ.get(f"DELEGATE_{bare_name}") or os.environ.get(bare_name)


_AUTH_OPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("accessToken", "AUTH_TOKEN"),
    ("tenantId", "TENANT_ID"),
    ("organizationId", "ORG_ID"),
    ("orgLogicalName", "ORG_SLUG"),
    ("tenantName", "TENANT_SLUG"),
)
"""``(field of the host's auth init option, name read through _env)``."""


def _auth_option() -> dict[str, str]:
    """The host's ``auth`` init option, built from the variables that are set."""
    return {field: value for field, name in _AUTH_OPTION_FIELDS if (value := _env(name))}


_HOST_ENV_REMOVED = (
    "AUTH_TOKEN",
    "TENANT_ID",
    "ORG_ID",
    "ORG_LOGICAL_NAME",
    "TENANT_NAME",
    "BACKEND_URL",
    "DELEGATE_AUTH_TOKEN",
)
"""Removed from the host's environment. The host reads the first six itself, but
coder_eval sends their values as init options instead; and the agent's shell tools
inherit the host env, so no spelling of the token may stay in it."""

_GATEWAY_S2S_ENV_VARS = ("LLMGW_CLIENT_ID", "LLMGW_CLIENT_SECRET", "LLMGW_URL")


def _strip_redundant_gateway_creds(env: dict[str, str]) -> tuple[str, ...]:
    """Remove the ``LLMGW_*`` S2S pair from ``env`` when the host would not use it.

    The agent's shell tools inherit the host env, so a live client secret there is
    exposed to the code under test. The host refreshes its token from that pair
    only when no token file is configured; a token file wins. This mirrors the
    host's own lookup (``DELEGATE_AUTH_TOKEN_FILE``, else ``AUTH_TOKEN_FILE``,
    split on ``os.pathsep``) and keeps the pair when it is the refresh source.
    Returns the names removed.
    """
    raw = env.get("DELEGATE_AUTH_TOKEN_FILE")
    if raw is None:
        raw = env.get("AUTH_TOKEN_FILE", "")
    if not any(entry.strip() for entry in raw.split(os.pathsep)):
        return ()
    return tuple(name for name in _GATEWAY_S2S_ENV_VARS if env.pop(name, None) is not None)


def _candidate_install_roots() -> list[Path]:
    """Search roots, in order: cwd and its ancestors, ``_AGENT_INSTALL_ROOT``, then home.

    The cwd walk mirrors Node's own module resolution. Home is where npm lands a
    package when the cwd has no ``package.json``.
    """
    cwd = Path.cwd().resolve()
    roots: list[Path] = [cwd, *cwd.parents]
    for root in (_AGENT_INSTALL_ROOT, Path.home().resolve()):
        if root not in roots:
            roots.append(root)
    return roots


def _resolve_host_bundle() -> Path:
    """Locate the installed ``@uipath/delegate-stdio``'s ``dist/delegate_stdio.mjs``.

    Resolution order: ``DELEGATE_SDK_PATH`` (explicit file path) ->
    ``DELEGATE_SDK_NODE_MODULES`` (explicit install root, probed exactly) ->
    ``_candidate_install_roots()``, so an ``npm install`` in this agent's own
    ``agents/delegate/`` directory is found from any cwd.

    Raises:
        AgentConfigError: no install found anywhere searched, or ``DELEGATE_SDK_PATH``
            names another file, such as ``@uipath/delegate-sdk``'s ``dist/index.mjs``.
    """
    explicit = os.environ.get("DELEGATE_SDK_PATH")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise AgentConfigError(
                f"DELEGATE_SDK_PATH={path} does not point to a file. Point it at "
                + f"{_HOST_PACKAGE}'s dist/{_HOST_BUNDLE_NAME}."
            )
        if path.name != _HOST_BUNDLE_NAME:
            raise AgentConfigError(
                f"DELEGATE_SDK_PATH={path} must point at {_HOST_PACKAGE}'s dist/{_HOST_BUNDLE_NAME}, "
                + "not at @uipath/delegate-sdk's dist/index.mjs or another file. "
                + f"Run `npm install {_HOST_PACKAGE}`; see docs/agents/DELEGATE.md."
            )
        return path

    root_override = os.environ.get("DELEGATE_SDK_NODE_MODULES")
    if root_override:
        path = (Path(root_override).expanduser().resolve() / _HOST_BUNDLE_REL_PATH).resolve()
        if not path.is_file():
            raise AgentConfigError(
                f"DELEGATE_SDK_NODE_MODULES={root_override}: {_HOST_PACKAGE} not found at {path}. "
                + f"Run `npm install {_HOST_PACKAGE}` there, or set DELEGATE_SDK_PATH directly."
            )
        return path

    searched: list[Path] = []
    for root in _candidate_install_roots():
        candidate = (root / _HOST_BUNDLE_REL_PATH).resolve()
        searched.append(candidate)
        if candidate.is_file():
            return candidate

    searched_block = "\n  ".join(str(p) for p in searched)
    raise AgentConfigError(
        f"{_HOST_PACKAGE} not found. Searched the cwd, its ancestors, this agent's directory, and home:\n"
        + f"  {searched_block}\n"
        + f"Run `npm install {_HOST_PACKAGE}` (plain, public install — no token needed), "
        + f"e.g. in {_AGENT_INSTALL_ROOT}, or set DELEGATE_SDK_NODE_MODULES "
        + f"to the install root, or DELEGATE_SDK_PATH to the dist/{_HOST_BUNDLE_NAME} file directly. "
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


_USAGE_BUCKETS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _parse_usage(raw: Any) -> TokenUsage | None:
    """Parse a host ``usage`` payload (a ``usage`` frame's, or the ``result``'s) into ``TokenUsage``.

    The host follows the Anthropic convention: ``input_tokens`` excludes cache
    reads and writes, which arrive separately as ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``. An absent or invalid bucket reads as 0, and
    an all-zero payload returns ``None``. One with no recognised bucket also
    warns — a renamed field degrades to zero tokens, never a crash.
    """
    if not isinstance(raw, dict):
        return None

    def _int(key: str) -> int:
        value = raw.get(key)
        if isinstance(value, bool):
            return 0
        return value if isinstance(value, int) and value >= 0 else 0

    usage = TokenUsage(
        uncached_input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_creation_input_tokens=_int("cache_creation_input_tokens"),
        cache_read_input_tokens=_int("cache_read_input_tokens"),
    )
    if usage.is_empty():
        if raw and not any(bucket in raw for bucket in _USAGE_BUCKETS):
            logger.warning("delegate: usage payload matched none of the known bucket spellings: %r", sorted(raw))
        return None
    return usage


_WAF_BLOCK_PAGE_MARKERS = ("continue with uipath platform", "not available in your country")
"""Fingerprints of UiPath's Cloudflare block page, served as a 403 when a WAF managed
rule matches shell-like text in the REQUEST BODY. It is not a geo or auth block."""

_SESSION_CONFLICT_MARKER = "already being generated"
"""The backend's 409 for a send into a conversation whose previous generation still runs."""

_SSE_CONNECT_TIMEOUT_MARKER = "sse connect timeout"
"""The SDK's error once its SSE connect watchdog has failed all of its internal retries."""


def _describe_host_error(message: str) -> str | None:
    """A correctly categorized crash reason for a known host failure, or ``None``.

    The raw message would mis-route under ``errors/categorization.py``: a WAF
    block reads as geo/auth but is deterministic per payload (stamped "content
    filter", non-retryable), and an SSE connect failure says "timeout" but is a
    transient backend window (stamped "connection", retryable, "timeout" defanged).
    A session conflict keeps its raw message; the caller handles it.

    Rationale: .claude/notes/agents.md § Delegate agent
    """
    lowered = message.lower()
    if any(marker in lowered for marker in _WAF_BLOCK_PAGE_MARKERS):
        prefix = message.split("<", 1)[0].strip().rstrip(":").strip()
        return (
            "Delegate backend request blocked by the Cloudflare WAF content filter in front of the UiPath "
            + "backend (the generic 'not available in your country' 403 page, not a geo or auth problem): "
            + "shell-like text in the prompt or a tool result matched a managed rule, and the same payload "
            + f"would be blocked again on retry. [{prefix}]"
        )
    if _SSE_CONNECT_TIMEOUT_MARKER in lowered and _SESSION_CONFLICT_MARKER not in lowered:
        original = message[lowered.find(_SSE_CONNECT_TIMEOUT_MARKER) :]
        defanged = re.sub("timeout", "time-out", original, flags=re.IGNORECASE)
        return (
            "Delegate backend connection failure: the turn's request got no response headers within the "
            + "SDK's SSE connect watchdog, on every internal attempt. This is a transient backend "
            + f"availability window, not a task-budget breach. [{defanged}]"
        )
    return None


class _TurnState:
    """Per-``communicate()`` accumulator.

    One ``AssistantMessage`` per turn (see .claude/notes/agents.md § Delegate
    agent), so this is far smaller than the per-round-trip segment machinery a
    richer transcript would need.
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
        # A result arrived while other tools were still open, so the next call is not
        # counted yet. If the model speaks or calls a new tool first, the next call has
        # begun; the open tools stay open, because the SDK can still deliver their results.
        self.results_incomplete = False

        self.model_used: str | None = model
        # The sum of the per-call `usage` frames, until the `result` replaces it with the turn total.
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

    See the module docstring for prerequisites and the wire protocol.
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

        self._host_bundle: Path | None = None
        self._init_options: dict[str, Any] | None = None
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
                + f"then `npm install {_HOST_PACKAGE}`. See docs/agents/DELEGATE.md."
            )
        self._host_bundle = _resolve_host_bundle()

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
        assert self._host_bundle is not None, "start() must resolve the host bundle before spawning"
        env = dict(os.environ)
        if self._env_path_prepend:
            env["PATH"] = os.pathsep.join([*self._env_path_prepend, env.get("PATH", "")])
        if self._plugin_tools_dir and "PLUGIN_TOOLS_DIR" not in env:
            env["PLUGIN_TOOLS_DIR"] = self._plugin_tools_dir
        # Respect an operator's own telemetry choice; only default it off.
        env.setdefault("DELEGATE_TELEMETRY_DISABLED", "1")
        if stripped := _strip_redundant_gateway_creds(env):
            logger.debug("delegate: a token file is configured; removed %s from the host env", ", ".join(stripped))
        for name in _HOST_ENV_REMOVED:
            env.pop(name, None)

        await self._cancel_drain_tasks()
        self._stdout_queue = asyncio.Queue()
        self._stderr_lines.clear()
        self._process = await asyncio.create_subprocess_exec(
            "node",
            str(self._host_bundle),
            cwd=self.working_directory,
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

        self._init_options = self._build_init_options()
        await self._send_command({"cmd": "init", "options": self._init_options})
        try:
            ack = await asyncio.wait_for(self._read_until(("init_ok", "error")), timeout=_INIT_TIMEOUT_SEC)
        except TimeoutError as exc:
            await self._force_kill_host()
            raise AgentCrashError(
                f"Delegate SDK init did not respond within {_INIT_TIMEOUT_SEC:.0f}s "
                + "(hung auth refresh or backend connect?)."
            ) from exc
        if ack.get("type") == "error":
            await self._force_kill_host()
            message = str(ack.get("message", "unknown error"))
            if any(marker in message.lower() for marker in _INIT_CONFIG_ERROR_MARKERS):
                raise AgentConfigError(f"Delegate SDK init failed: {message} {_INIT_CONFIG_ERROR_HINT}")
            raise AgentCrashError(f"Delegate SDK init failed: {message}")

    def _build_init_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "workingDirectory": self.working_directory,
            "enableComputerUse": self.config.enable_computer_use,
        }
        if self.config.model:
            options["model"] = self.config.model
        options.update(self.config.sdk_options)
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
            options["env"] = environment
        if auth := _auth_option():
            options["auth"] = auth
        # list[LocalPluginConfig] is not list[dict[str, Any]] under list invariance.
        skills_path = _resolve_bundled_skills_path(self.config.plugins)  # type: ignore[arg-type]
        options["enableSkills"] = skills_path is not None
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
        """SIGKILL the host and drop its handle, so the next ``communicate()`` respawns.

        The handle is dropped even when the reap times out: a dying host must never
        receive the next ``send``.
        """
        proc = self._process
        self._process = None
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

    def get_sdk_options(self) -> dict[str, Any] | None:
        """The ``init`` options sent to the last host spawned, or ``None`` before ``start()``.

        Credentials are redacted, because the result is persisted with the run:
        ``auth`` becomes the names of its fields, and ``backendUrl`` its host.
        """
        if self._init_options is None:
            return None
        options = dict(self._init_options)
        if "auth" in options:
            options["auth"] = sorted(options["auth"])
        if "backendUrl" in options:
            options["backendUrl"] = urlparse(options["backendUrl"]).hostname
        return options

    def get_environment_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "delegate_model": self.config.model,
        }
        if self.config.enable_computer_use:
            info["delegate_enable_computer_use"] = True
        # The host ranks backendUrl over the env slug (see docs/agents/DELEGATE.md),
        # so recording delegate_env here too would assert a routing decision the SDK
        # never made. Host only (never the full URL, which can carry embedded credentials).
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
            # fresh rather than fail fast.
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
                if mtype == "result":
                    self._handle_result(msg, state)
                    break
                if mtype == "error":
                    await self._crash_on_host_error(state, collector, emit, str(msg.get("message", "unknown error")))
                if mtype == "usage":
                    self._add_call_usage(msg, state)
                    continue
                event = msg.get("event")
                if mtype != "event" or not isinstance(event, dict):
                    logger.debug("delegate: ignoring host message %r", mtype)
                    continue

                self._handle_event(event, state, emit)

                if max_turns is not None and state.api_calls > max_turns:
                    state.max_turns_exhausted = True
                    await self._force_kill_host()
                    break
                if should_stop is not None and should_stop():
                    stopped_early = True
                    await self._force_kill_host()
                    break

            if stopped_early:
                status = AgentEndStatus.STOPPED_EARLY
            elif state.max_turns_exhausted:
                status = AgentEndStatus.MAX_TURNS_EXHAUSTED
            else:
                status = AgentEndStatus.COMPLETED
            self._warn_if_usage_missing(state, status)
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
            self._crash_turn(state, collector, emit, f"Delegate turn failed: {e!s}", cause=e)
            raise  # unreachable — _crash_turn is NoReturn

    def _handle_event(self, event: dict[str, Any], state: _TurnState, emit: Callable[[StreamEvent], None]) -> None:
        """Dispatch one SDK event unwrapped from an ``event`` frame. Never raises on unrecognized shape."""
        event_type = event.get("type")
        session_id = event.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id

        if state.api_calls == 0 and (event_type in _TEXT_EVENT_TYPES or event_type == "tool_call"):
            state.api_calls = 1
        elif state.results_incomplete and (
            event_type in _TEXT_EVENT_TYPES or (event_type == "tool_call" and _tool_id(event) not in state.open_tools)
        ):
            state.api_calls += 1
            state.results_incomplete = False

        if event_type == "message":
            text = event.get("content")
            if isinstance(text, str) and text:
                self._append_message_text(state, text, starts_step=event.get("isStepStart") is not False)
                emit(TextChunkEvent(task_id=self.task_id, turn_id=state.turn_id, text=text))
        elif event_type == "thinking":
            text = event.get("content")
            if isinstance(text, str) and text:
                state.content_blocks.append(
                    ContentBlock(block_type="thinking", sequence=len(state.content_blocks), thinking=text)
                )
        elif event_type == "tool_call":
            self._handle_tool_call(event, state, emit)
        elif event_type == "tool_result":
            self._handle_tool_result(event, state, emit)
            state.results_incomplete = bool(state.open_tools)
            if not state.open_tools:
                state.api_calls += 1
        elif event_type == "error":
            state.error_message = str(event.get("error") or "unknown error")
            logger.warning("delegate: SDK reported an error event: %s", state.error_message)
        elif event_type in ("session_start", "done"):
            pass  # informational; no telemetry to record
        else:
            logger.debug("delegate: unrecognized event type %r", event_type)

    @staticmethod
    def _append_message_text(state: _TurnState, text: str, *, starts_step: bool) -> None:
        """Record assistant text; a streamed delta (``isStepStart: false``) extends the open text block."""
        state.text_parts.append(text)
        last = state.content_blocks[-1] if state.content_blocks else None
        if not starts_step and last is not None and last.block_type == "text":
            last.text = (last.text or "") + text
            return
        state.message_events += 1
        state.content_blocks.append(ContentBlock(block_type="text", sequence=len(state.content_blocks), text=text))

    def _handle_tool_call(self, event: dict[str, Any], state: _TurnState, emit: Callable[[StreamEvent], None]) -> None:
        tool_id = _tool_id(event) or str(uuid.uuid4())
        tool_name = str(event.get("toolName") or "unknown")
        parameters = event.get("toolArgs")
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

    def _handle_tool_result(
        self, event: dict[str, Any], state: _TurnState, emit: Callable[[StreamEvent], None]
    ) -> None:
        tool_id = _tool_id(event)
        telemetry = state.open_tools.pop(tool_id, None)
        output = event.get("toolResult")
        if telemetry is None:
            # The SDK sends no tool_call for a tool name it cannot resolve, only
            # this result, so the row takes its name and args from the result.
            state.sequence += 1
            telemetry = CommandTelemetry(
                tool_name=_orphan_tool_name(event, tool_id),
                tool_id=tool_id or str(uuid.uuid4()),
                assistant_turn_index=state.message_events,
                timestamp=datetime.now(),
                parameters=_orphan_tool_args(output),
                sequence_number=state.sequence,
            )
        if isinstance(output, dict) and isinstance(output.get("content"), str):
            output = output["content"]
        completed = datetime.now()
        telemetry.execution_completed_at = completed
        if telemetry.execution_started_at is not None:
            telemetry.duration_ms = (completed - telemetry.execution_started_at).total_seconds() * 1000
        if isinstance(output, str):
            telemetry.result_summary = output
        elif output is not None:
            # A dict/list result must not be silently dropped -- serialize
            # rather than lose it (CE043: stored whole, never truncated).
            telemetry.result_summary = json.dumps(output)
        else:
            telemetry.result_summary = None
        if event.get("toolStatus") in _FAILED_TOOL_STATUSES:
            status = ToolEndStatus.ERROR
            telemetry.result_status = "error"
            telemetry.error_message = telemetry.result_summary or "tool failed"
        else:
            status = ToolEndStatus.OK
            telemetry.result_status = "success"
        emit(ToolEndEvent(task_id=self.task_id, turn_id=state.turn_id, tool=telemetry, status=status))

    @staticmethod
    def _add_call_usage(msg: dict[str, Any], state: _TurnState) -> None:
        """Add one model call's ``usage`` frame to the turn's running total.

        The host writes a call's frame before any tool result that call caused,
        so a turn cut at ``max_turns`` or by an early stop keeps the usage of
        every call that finished.
        """
        usage = _parse_usage(msg.get("usage"))
        if usage is not None:
            state.usage = usage if state.usage is None else state.usage + usage

    @staticmethod
    def _warn_if_usage_missing(state: _TurnState, status: AgentEndStatus) -> None:
        """Warn when model calls finished but no usage arrived, so the turn books no tokens or cost.

        A cut turn's in-flight call is not finished, so it is not counted.
        """
        if state.usage is not None:
            return
        if status is AgentEndStatus.COMPLETED:
            if state.api_calls > 0:
                logger.warning(
                    "delegate: the host reported no token usage for a turn with %d model call(s); "
                    + "its tokens and cost are unknown",
                    state.api_calls,
                )
        elif state.api_calls > 1:
            logger.warning(
                "delegate: turn ended (%s) with no usage frame from the host; tokens and cost for its %d "
                + "finished model call(s) are unknown (a @uipath/delegate-stdio without per-call usage "
                + "frames reports usage only on its result frame, which a cut turn never gets)",
                status.value,
                state.api_calls - 1,
            )

    def _handle_result(self, msg: dict[str, Any], state: _TurnState) -> None:
        """Fold the host's terminal ``result`` frame into the turn state.

        Its ``usage`` is the turn total, so it replaces the running sum of the
        ``usage`` frames. ``turnUsages`` has one entry per backend round-trip, so
        its length is the authoritative call count and replaces the running
        estimate.
        """
        response = msg.get("response")
        if isinstance(response, str):
            state.final_response = response
        session_id = msg.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
        model = msg.get("model")
        if isinstance(model, str) and model:
            state.model_used = model
        usage = _parse_usage(msg.get("usage"))
        if usage is not None:
            state.usage = usage
        turn_usages = msg.get("turnUsages")
        if isinstance(turn_usages, list) and turn_usages:
            state.api_calls = len(turn_usages)

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
            # Single generation window for the whole turn (see .claude/notes/agents.md
            # § Delegate agent): tiles from the turn's own start, since there is no
            # prior emission to tile from.
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

    def _log_stderr_tail(self, reason: str) -> None:
        """Log the host's stderr tail beside ``reason``.

        Never put the tail in a raised reason: ``errors/categorization.py`` matches
        substrings of the reason, and the tail holds incidental text (the sandbox
        path, which names the task) that can match a non-retryable rule.

        Rationale: .claude/notes/agents.md § Delegate agent
        """
        logger.warning("delegate: %s. stderr tail:\n%s", reason, self._stderr_tail())

    async def _abandon_host_and_crash(
        self,
        state: _TurnState,
        collector: EventCollector,
        emit: Callable[[StreamEvent], None],
        reason: str,
    ) -> NoReturn:
        """Force-kill the current host and crash the turn with ``reason``.

        Shared by every mid-``communicate()`` failure kernel that must not let
        the NEXT ``communicate()`` reuse this host: reuse risks writing a
        ``send`` into a dead pipe, or consuming an ``onEvent`` callback the SDK
        fires after this failure as the retry's own result.
        """
        await self._force_kill_host()
        self._log_stderr_tail(reason)
        self._crash_turn(state, collector, emit, reason)

    async def _crash_on_host_error(
        self,
        state: _TurnState,
        collector: EventCollector,
        emit: Callable[[StreamEvent], None],
        message: str,
    ) -> NoReturn:
        """Crash the turn on a host ``error`` frame; the host is never reused.

        A session conflict also drops the remembered session id, because a retry
        into the same conversation can only conflict again.
        """
        reason = _describe_host_error(message)
        if reason is None:
            if _SESSION_CONFLICT_MARKER in message.lower():
                logger.warning(
                    "delegate: session %s is still generating a reply; the retry starts a new conversation",
                    self._session_id,
                )
                self._session_id = None
            reason = f"Delegate send failed: {message}"
        await self._abandon_host_and_crash(state, collector, emit, reason)

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
                reason = "Delegate host exited before responding"
                self._log_stderr_tail(reason)
                raise AgentCrashError(reason)
            if msg.get("type") in accepted_types:
                return msg
            logger.debug("delegate: ignoring host message %r during init", msg.get("type"))

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
        except asyncio.CancelledError:
            raise
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
