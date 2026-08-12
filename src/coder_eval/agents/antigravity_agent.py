"""Antigravity agent implementation using the official google-antigravity SDK.

The backend drives Google's Antigravity agent *local harness* (the bundled
``localharness`` binary shipped in the ``google-antigravity`` wheel) via the
SDK's :class:`LocalAgentConfig`. It authenticates against the Gemini Developer
API with ``GEMINI_API_KEY`` and runs entirely on the local machine, editing
files inside the sandbox working directory — so coder_eval's on-disk success
criteria see the agent's writes exactly as they do for Claude / Codex.

Why this surface (and not the branded ``agy`` CLI or the remote SDK): the
standalone Antigravity CLI cannot authenticate headlessly with a Gemini API key
(only interactive OAuth), and the Interactions API runs in a *remote* cloud
sandbox whose edits never land in our local dir. The local harness is the only
non-deprecated path that satisfies headless + GEMINI_API_KEY + local execution.

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

from coder_eval.agent import Agent, AgentState, ConfigFieldSupport, ConfigSupport
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
from coder_eval.utils import expand_env_vars


logger = logging.getLogger(__name__)

# Recommended Gemini coding model when a task pins no ``agent.model`` and neither
# ``--model`` nor ``ANTIGRAVITY_MODEL`` is set. Gemini 3.5 Flash is Antigravity 2.0's
# default coding model (2026-05) — it outperforms the older Gemini 3.1 Pro on coding /
# agentic benchmarks while running faster; ``medium`` thinking is its daily-driver default.
_DEFAULT_MODEL = "gemini-3.5-flash"

# Antigravity builtin tool name -> canonical Claude-ish tool name, so cross-agent
# success criteria (command_executed / commands_efficiency / skill_triggered) and
# reports key on the SAME tool names the Claude / Codex backends emit. Unmapped
# tool names pass through unchanged.
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
    "read_url_content": "WebFetch",
    "finish": "Finish",
}

# The inverse, for translating ``agent.allowed_tools`` / ``disallowed_tools`` (written
# in Claude names) into the harness's ``CapabilitiesConfig`` tool lists. Built from the
# forward map so the two can never drift; the forward map is 1:1, so the inversion is
# lossless. A Claude tool with no Antigravity counterpart (``Skill`` — Antigravity
# discovers skills through ``skills_paths``, not a tool; ``TodoWrite``; ...) is absent
# here and is dropped with a log line rather than crashing the enum validation.
_CLAUDE_TO_ANTIGRAVITY_TOOL_MAP: dict[str, str] = {v: k for k, v in _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP.items()}

# Tools that keep the harness's control flow working and are therefore never removed
# by an allowlist. ``finish`` is how the agent ends its turn — disabling it strands
# every run at the step-loop until the turn timeout fires.
_ANTIGRAVITY_STRUCTURAL_TOOLS: frozenset[str] = frozenset({"finish"})

# Tool-call arg keys the harness ADDS at completion (the result payload), not
# model-supplied inputs — stripped from CommandTelemetry.parameters and mined for
# the tool result instead. This is the STATIC backstop; the live mapping ALSO
# strips any key that first appears at tool-DONE (see ``_params``), so tool-
# specific result fields (LS ``results``, WebSearch ``summary``) never leak into
# parameters — important because ``skill_triggered`` substring-searches every
# parameter value and a leaked result could otherwise false-positive.
_RESULT_ARG_KEYS: frozenset[str] = frozenset(
    {"exit_code", "combined_output", "diff_block", "output", "stdout", "stderr", "result", "results", "summary"}
)

# Antigravity per-tool INPUT-arg key -> canonical (Claude-ish) key, so cross-agent
# success criteria (command_executed keys on Bash ``parameters["command"]``; LS on
# ``path``) and reports read the SAME parameter names the Claude/Codex backends
# emit. Keyed by the canonical tool name (post tool-name mapping). Unlisted keys
# pass through unchanged.
_ANTIGRAVITY_ARG_RENAME: dict[str, dict[str, str]] = {
    "Bash": {"command_line": "command"},
    "LS": {"directory_path": "path"},
}

# google.antigravity.types.Step{Status,Type,Source,Target} VALUES we branch on,
# mirrored as plain strings so this module needs no SDK import (the SDK is an
# optional extra; only ``start()`` touches it). Named constants — not bare string
# literals — so an antigravity StepStatus.ERROR comparison is not mistaken for a
# coder_eval FinalStatus member-name denylist (lint rule CE018).
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

    Gemini reports ``prompt`` (with ``cached`` as a subset), ``candidates`` (output
    excluding thinking) and ``thoughts`` (reasoning). coder_eval's buckets:
    uncached input = prompt - cached; cache_read = cached; cache_creation = 0
    (Gemini bills no separate cache-write fee); output = candidates + thoughts
    (Gemini bills thinking as output). Cost is rate-carded from the bare model id.
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

    # The step loop has a between-steps guard where the cooperative
    # ``should_stop`` check runs, so this agent supports early-stop-on-criterion.
    supports_cooperative_stop: ClassVar[bool] = True

    # Declared divergence from the shared BaseAgentConfig contract. APPROXIMATED
    # rather than UNHONORED for the same reason as Codex: the run is genuinely
    # unconfined under every mode by design, and the isolation the field implies is
    # provided one layer down by the sandbox driver — so the mode is not ignored so
    # much as satisfied elsewhere. Rejecting it would break every task that sets
    # bypassPermissions to mean "this is a headless eval, do not stop to ask".
    config_support: ClassVar[dict[str, ConfigFieldSupport]] = {
        "permission_mode": ConfigFieldSupport(
            ConfigSupport.APPROXIMATED,
            "every mode runs the harness with policy.allow_all(); coder_eval's isolation "
            + "boundary is the sandbox driver, and a headless eval has no human to approve",
        ),
    }

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
        # The live SDK Agent session + its AsyncExitStack (entered in start(),
        # closed in stop()). The exit-stack teardown terminates the localharness
        # subprocess, so reaping it is what stop()/kill() rely on.
        self._sdk_agent: Any = None
        self._exit_stack: AsyncExitStack | None = None
        # Absolute dirs to prepend to PATH so sandbox mock CLIs shadow real ones
        # for the harness's run_command tool — handed to the SDK's per-agent env
        # seam at start() (see _harness_env).
        self._env_path_prepend: list[str] = []
        # _state / _iteration / _iteration_was_incremented / pending_turn lifecycle
        # bookkeeping lives on the Agent base class (shared defaults + helpers).
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})

    def _effective_model(self) -> str:
        """Resolve the model: task ``agent.model`` > ``ANTIGRAVITY_MODEL`` > default."""
        return self.config.model or settings.antigravity_model or _DEFAULT_MODEL

    def _resolve_skills_paths(self, plugin_tools_dir: str | None) -> list[str]:
        """Resolve skill search-path roots for the harness's native ``skills_paths``.

        Mirrors the source resolution the Codex backend uses: collect ``type: local``
        plugin paths from ``config.plugins`` (env-expanded) plus the runtime
        ``plugin_tools_dir``. For each source, hand the harness the directory that
        *directly* parents skill dirs — either ``<source>/skills`` (a plugin-marketplace
        / repo root) or ``<source>`` itself (already a skills dir) — whichever actually
        contains a ``<skill>/SKILL.md``. The harness auto-discovers skills under those
        roots; no symlinking is needed (unlike Codex, Antigravity takes search paths).
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
                # Loud: an unresolved env var (e.g. unset $SKILLS_REPO_PATH) or a
                # missing dir silently drops the skills, so the agent runs blind.
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
        roots. ``skills_paths`` only drives skill *discovery*; the file-tool
        allowlist is governed solely by ``workspaces``, so the skill roots must
        appear here too — otherwise the agent discovers a skill but every read of
        its ``SKILL.md`` is denied as out-of-workspace. The roots are bind-mounted
        into the sandbox at the same path by the shared docker plugin auto-mount,
        mirroring how Claude reads skills from the mounted plugin path.
        """
        return [str(self.working_directory), *skills_paths]

    def _map_tools(self, tools: list[str], field: str) -> list[str]:
        """Translate Claude-named tools to Antigravity builtin names, dropping unmappables.

        ``CapabilitiesConfig`` validates against the ``BuiltinTools`` enum, so an
        unmapped name would raise instead of being ignored — hence the explicit drop
        plus a log line naming what was dropped and why.
        """
        mapped: list[str] = []
        dropped: list[str] = []
        for tool in tools:
            antigravity_name = _CLAUDE_TO_ANTIGRAVITY_TOOL_MAP.get(tool)
            if antigravity_name is None:
                dropped.append(tool)
            elif antigravity_name not in mapped:
                mapped.append(antigravity_name)
        if dropped:
            self._log.debug(
                "agent.%s entries with no Antigravity builtin were dropped: %s "
                + "(Skill is expected here — Antigravity discovers skills via skills_paths, not a tool)",
                field,
                ", ".join(dropped),
            )
        return mapped

    def _build_capabilities(self, types_mod: Any) -> Any:
        """Build ``CapabilitiesConfig`` from ``allowed_tools`` / ``disallowed_tools``.

        The two SDK fields are mutually exclusive, so an allowlist wins and any
        denylist is subtracted from it rather than passed separately — same resulting
        tool set, no SDK validation error. Returns ``None`` when neither field
        constrains anything, leaving the harness defaults (all tools) in force.

        The structural tools are always re-added: an allowlist that stripped ``finish``
        would leave the agent unable to end its turn. An allowlist that maps to nothing
        usable falls back to the harness defaults with a warning — handing the model a
        single ``finish`` tool produces a zero-scoring run with no diagnosable cause,
        which is the worse failure for an eval harness.
        """
        requested_allow = self.config.allowed_tools or []
        allowed = self._map_tools(requested_allow, "allowed_tools")
        disallowed = self._map_tools(self.config.disallowed_tools or [], "disallowed_tools")

        # Branch on what the TASK asked for, not on what survived mapping: an
        # allowlist whose every entry is unmappable must reach the warning below,
        # not fall through to "no allowlist configured".
        if requested_allow:
            enabled = [t for t in allowed if t not in disallowed]
            enabled += [t for t in sorted(_ANTIGRAVITY_STRUCTURAL_TOOLS) if t not in enabled]
            if set(enabled) <= _ANTIGRAVITY_STRUCTURAL_TOOLS:
                self._log.warning(
                    "agent.allowed_tools (%s) maps to no usable Antigravity tool; "
                    + "falling back to the harness default (all tools enabled).",
                    ", ".join(self.config.allowed_tools or []),
                )
                return None
            self._log.debug("Antigravity enabled_tools: %s", ", ".join(enabled))
            return types_mod.CapabilitiesConfig(enabled_tools=enabled)

        if disallowed:
            disabled = [t for t in disallowed if t not in _ANTIGRAVITY_STRUCTURAL_TOOLS]
            if not disabled:
                return None
            self._log.debug("Antigravity disabled_tools: %s", ", ".join(disabled))
            return types_mod.CapabilitiesConfig(disabled_tools=disabled)

        return None

    def _harness_env(self) -> dict[str, str] | None:
        """Per-agent environment for the localharness subprocess (``LocalAgentConfig.env``).

        Returns the mock-CLI PATH prepend as a one-key overlay, or ``None`` when no
        mock dirs are configured (so the SDK spawns with a plain inherited env). The
        SDK merges this over ``os.environ`` at spawn (``{**os.environ, **env}``), so
        naming only ``PATH`` leaves every other inherited variable untouched. The
        same overlay is handed to the harness as its ``run_command`` environment, so
        mock CLIs shadow the real ones inside the agent's shell too.
        """
        if not self._env_path_prepend:
            return None
        # Match the parent process's own casing (Windows exports ``Path``) so the
        # merge overrides the inherited entry instead of adding a sibling key.
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
            working_directory: Path to the sandbox working directory. The primary
                ``workspace`` so file writes (and run_command) operate there —
                process-cwd-independent, so concurrent host-mode tasks don't race.
                Resolved skill roots are added alongside it so the agent can read
                skill files — see ``_resolve_workspaces``.
            env_path_prepend: Absolute directories to prepend to PATH (typically the
                resolved ``SandboxConfig.mock_path_dirs``) so mock CLIs shadow the real
                ones for the harness's ``run_command`` tool — same mock-shadowing
                contract as the Claude/Codex backends. Delivered through the SDK's
                per-agent ``env`` seam (see ``_harness_env``), so concurrent tasks get
                genuinely separate environments rather than a time-sliced global one.
            plugin_tools_dir: A skills/plugin source root. Resolved (together with
                ``config.plugins``) into the harness's native ``skills_paths`` so the
                agent can discover and engage UiPath skills — see ``_resolve_skills_paths``.
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
            # GEMINI_API_KEY authenticates the harness. None lets the SDK read it
            # from the environment itself (and raise a clear error if truly unset).
            api_key = os.getenv("GEMINI_API_KEY") or None
            skills_paths = self._resolve_skills_paths(plugin_tools_dir)
            cfg = LocalAgentConfig(
                model=self._effective_model(),
                api_key=api_key,
                # File tools are confined to ``workspaces`` by the auto-prepended
                # workspace_only policy. Scope to the sandbox workdir (the write
                # target — process-cwd-independent so concurrent host-mode tasks
                # don't race) PLUS the resolved skill roots, so the agent can READ
                # each SKILL.md. ``skills_paths`` only feeds discovery; the file-tool
                # allowlist is ``workspaces`` alone, so without the roots here every
                # skill read is denied as out-of-workspace. The roots are already
                # bind-mounted into the sandbox at the same path by the shared
                # docker plugin auto-mount (the path Claude reads skills from too).
                workspaces=self._resolve_workspaces(skills_paths),
                # Autonomous execution: approve every tool call (incl. run_command),
                # which the default LocalAgentConfig policy would otherwise deny.
                # ``permission_mode`` is deliberately NOT mapped onto these policies —
                # it does not confine this agent, exactly as on Codex. coder_eval's
                # isolation boundary is the driver (a docker container or an ephemeral
                # per-task tempdir), so an in-agent approval policy is redundant, and
                # the modes below bypassPermissions differ only in what they'd ask a
                # human about — there is no human on a headless eval path. Declared as
                # such in the parity table so it is visible rather than silent.
                policies=[policy.allow_all()],
                # coder_eval's `system_prompt` is text APPENDED to the harness's own
                # default agent prompt (docs/agents/HARNESS_PARITY.md). A plain str
                # here is exactly that: the SDK wraps it as a
                # TemplatedSystemInstructions section on top of Antigravity's
                # defaults. Do NOT switch to types.CustomSystemInstructions — that
                # replaces every default instruction, including the core safety
                # mandates, which a task-level one-liner cannot stand in for.
                system_instructions=self.config.system_prompt or None,
                # Skill discovery: hand the harness the search-path roots that parent
                # the UiPath skill dirs. Unlike Codex (which symlinks into
                # .agents/skills/), Antigravity takes skill search paths natively.
                skills_paths=skills_paths,
                # Mock-CLI PATH shadowing, per agent. The SDK merges this over the
                # inherited os.environ when it spawns the localharness, so two
                # concurrent tasks never see each other's mock dirs.
                env=self._harness_env(),
                # allowed_tools / disallowed_tools → the harness's tool exposure.
                # Stripping a tool from the model's context (rather than denying the
                # call via a policy) matches how Claude Code and Codex read the same
                # fields, and costs no tokens on rejected attempts.
                **({"capabilities": capabilities} if (capabilities := self._build_capabilities(types)) else {}),
            )
            # Attach the configured thinking level (reasoning effort) onto every
            # resolved model's Gemini endpoint. The SDK validates the model list in
            # a model_validator; we set options on the resolved targets after build.
            level = types.ThinkingLevel(self.config.thinking_level)
            for target in cfg.models or []:
                endpoint = getattr(target, "endpoint", None)
                if isinstance(endpoint, types.GeminiAPIEndpoint):
                    endpoint.options = types.GeminiModelOptions(thinking_level=level)

            # Enter the SDK Agent context (boots the localharness subprocess +
            # opens the conversation). Held open across communicate() calls and
            # closed in stop().
            self._exit_stack = AsyncExitStack()
            self._sdk_agent = await self._exit_stack.enter_async_context(SdkAgent(cfg))
            self._log.debug("Antigravity local harness started (model=%s)", self._effective_model())
        except Exception as e:
            await self._teardown()
            raise RuntimeError(f"Failed to start Antigravity agent: {e}") from e

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

        ``max_turns`` caps VISIBLE turns — tool calls, the unit
        ``reports_stats.visible_turn_count`` counts — enforced in-stream on the same
        step-loop boundary as the cooperative stop. Claude Code's native SDK cap
        counts assistant messages instead; one ``communicate()`` here is a single SDK
        turn, so a native counter would cap at 1 and mean nothing. See
        docs/agents/HARNESS_PARITY.md.

        Drives one logical turn: ``conversation.send(prompt)`` then iterate
        ``receive_steps()`` until the turn goes idle, mapping the Gemini step
        stream onto the standardized event protocol.

        Raises:
            RuntimeError: If the agent is not started.
            TurnTimeoutError: Timeout elapsed (partial TurnRecord on pending_turn).
            AgentCrashError: SDK/harness failed mid-turn (same pending_turn contract).
        """
        if not self.working_directory or self._sdk_agent is None:
            raise RuntimeError("Agent not started. Call start() first.")

        assert self.config.type is not None, "AntigravityAgent requires AgentConfig.type before communicate()"

        self._begin_turn()
        turn_start_time = time.monotonic()
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
            max_turns=max_turns,
        )

        try:
            emit.on_event(AgentStartEvent(task_id=task_id, prompt=user_input, iteration=self._iteration, model=model))

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
                try:
                    await conversation.send(user_input)
                    # The cooperative should_stop poll runs AFTER process_step (the
                    # emission that lets the watcher latch on the deciding tool
                    # call) and BEFORE the next step is pulled — the deciding step
                    # is kept, the next is not. No-op when should_stop is None.
                    async for step in conversation.receive_steps():
                        state.process_step(step)
                        if should_stop is not None and should_stop():
                            state.stopped_early_hit = True
                            self._log.debug("Cooperative stop requested; ending step loop at this boundary")
                            break
                        # The turn cap shares this boundary: the step that reached the
                        # cap is kept whole, the next is never pulled. Checked after
                        # the cooperative stop so an armed early-stop still reports as
                        # STOPPED_EARLY when both would fire on the same step.
                        if state.max_turns_reached():
                            state.max_turns_hit = True
                            self._log.debug("max_turns (%s visible turns) reached; ending step loop", max_turns)
                            break
                    if state.stopped_early_hit or state.max_turns_hit:
                        # Best-effort server-side cancel, mirrors kill(); a raising
                        # cancel() lands in the guarded handler below.
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
                        # The turn already stopped cleanly (e.g. the generator's
                        # aclose() raised on the break); escalating to a crash
                        # would trigger the orchestrator's retry with the watcher's
                        # decision still latched → immediate stop-at-turn-0 on the
                        # retry (wasted spend). A cap-break is the same shape: the
                        # retry would burn the budget again and re-hit the cap.
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
                # Same retry-poisoning guard as the inner handler: the turn already
                # ended cleanly (cooperative stop or turn cap), so finalize instead
                # of crashing.
                self._log.warning("Ignoring post-stop exception; finalizing cleanly: %s", e)
            else:
                self._finalize_and_raise_crash(
                    state.finalize, truncate_crash_message(f"Antigravity turn failed: {e!s}"), cause=e
                )

        self._state = AgentState.WORKING
        self._end_turn_ok()
        # Precedence matches Claude: timeout (raised above) > stopped_early >
        # max_turns_exhausted > completed. stopped_early outranks the cap because an
        # armed criterion deciding the outcome is the more specific reason to have
        # cut the run, and the step loop checks it first.
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
        happens via the asyncio-task cancel the watchdog also delivers (which
        unwinds ``receive_steps``) and the subsequent ``stop()`` exit-stack close.
        This hook only records intent.
        """
        self._state = AgentState.ERROR

    def get_environment_info(self) -> dict[str, Any]:
        """Record the resolved Gemini model + thinking level for auditability."""
        return {
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
    ``args`` (``exit_code`` / ``combined_output`` / ``diff_block``) at DONE.
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

        # Tool tracking: emit ToolStart on first sight of an id; ToolEnd at DONE.
        self._next_seq = 0
        self._seen_tools: set[str] = set()
        self._closed_tools: set[str] = set()
        self._open_tools: dict[str, CommandTelemetry] = {}
        # Raw arg keys present when a tool was first seen (its model-supplied
        # inputs). Used at DONE to distinguish inputs from harness-appended
        # result fields, whatever they're named for that tool.
        self._tool_input_keys: dict[str, set[str]] = {}
        # Content blocks accumulated since the last per-generation flush.
        self._blocks: list[ContentBlock] = []

    @property
    def ended_cleanly(self) -> bool:
        """True once the loop broke on purpose (cooperative stop or the turn cap).

        Both are non-crash terminations, so a stray exception raised while unwinding
        the step generator afterwards must not be escalated into a retry.
        """
        return self.stopped_early_hit or self.max_turns_hit

    def max_turns_reached(self) -> bool:
        """True once this turn has produced ``max_turns`` visible turns.

        Delegates the count to the collector (``EventCollector.visible_turn_count``)
        — the single agent-agnostic capture path, so one ``max_turns`` value means
        the same thing here and on Codex. It counts RESOLVED tool calls (the end
        event), which also means the call that reaches the cap keeps its result
        instead of being force-closed as unresolved.
        """
        return self.max_turns is not None and self.collector.visible_turn_count >= self.max_turns

    def process_step(self, step: Any) -> None:
        """Route one streamed ``Step`` to events + transcript reconstruction."""
        stype = _enum_value(step.type)
        sstatus = _enum_value(step.status)
        ssource = _enum_value(step.source)
        starget = _enum_value(step.target)
        done = sstatus in (_STATUS_DONE, _STATUS_ERROR)

        # Stream visible assistant text deltas.
        if step.content_delta and ssource == _SOURCE_MODEL and starget == _TARGET_USER and stype == _TYPE_TEXT_RESPONSE:
            self.emit.on_event(TextChunkEvent(task_id=self.task_id, turn_id=self.turn_id, text=step.content_delta))

        # Tool calls: ToolStart on first sight, ToolEnd when the owning step is DONE.
        for call in step.tool_calls:
            self._handle_tool_call(call, step, done, sstatus)

        # Capture content blocks on the terminal transition of a step.
        if done:
            if stype == _TYPE_THINKING and step.thinking:
                self._blocks.append(ContentBlock(block_type="thinking", sequence=0, thinking=step.thinking))
            elif stype == _TYPE_TEXT_RESPONSE and step.content:
                self._output_parts.append(step.content)
                self._blocks.append(ContentBlock(block_type="text", sequence=0, text=step.content))

        # Per-generation usage: fold into the turn total and cut an AssistantMessage.
        if step.usage_metadata is not None:
            gen = _to_token_usage(step.usage_metadata, self.model)
            self.total_usage = self.total_usage + gen
            self._flush_generation(gen, getattr(step.usage_metadata, "thoughts_token_count", 0) or 0)

    def _handle_tool_call(self, call: Any, step: Any, done: bool, sstatus: Any) -> None:
        raw_name = _enum_value(call.name)
        cid = call.id or f"{raw_name}_{self._next_seq}"
        if cid not in self._seen_tools:
            self._seen_tools.add(cid)
            seq = self._next_seq
            self._next_seq += 1
            tool_name = _ANTIGRAVITY_TO_CLAUDE_TOOL_MAP.get(raw_name, str(raw_name))
            self._tool_input_keys[cid] = set(call.args)
            now = datetime.now()
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
            completed = datetime.now()
            started = start_tel.execution_started_at or completed
            end_tel = start_tel.model_copy(
                update={
                    "parameters": self._params(start_tel.tool_name, call.args, self._tool_input_keys.get(cid)),
                    "result_status": "error" if errored else "success",
                    "result_summary": str(result_text) if result_text is not None else None,
                    "error_message": (step.error or "tool failed") if errored else None,
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
            self._blocks.append(ContentBlock(block_type="tool_use", sequence=0, tool_use_id=cid))

    @staticmethod
    def _params(tool_name: str, args: dict[str, Any], input_keys: set[str] | None) -> dict[str, Any]:
        """Model-supplied inputs only, renamed to canonical cross-agent keys.

        A key is treated as a (dropped) result field when it is in the static
        ``_RESULT_ARG_KEYS`` backstop OR — given the input-key snapshot taken at
        tool start — it first appeared at DONE (harness-appended output, whatever
        the tool names it). Surviving input keys are renamed via
        ``_ANTIGRAVITY_ARG_RENAME`` so ``command_executed`` / reports key on the
        same names (``command`` / ``path``) the Claude/Codex backends emit.
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

        Keeping per-message token buckets summing to the turn total means the
        EventCollector's reconciliation step books a zero residual.
        """
        if not self._blocks and gen.is_empty():
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
                input_tokens=gen.uncached_input_tokens,
                output_tokens=gen.output_tokens,
                cache_creation_tokens=0,
                cache_read_tokens=gen.cache_read_input_tokens,
                reasoning_tokens=reasoning_tokens,
                model=self.model,
            )
        )
        self._assistant_turns += 1
        self._blocks = []

    def _agent_output(self) -> str:
        if self._output_parts:
            return "".join(self._output_parts)
        with contextlib.suppress(Exception):
            return self._agent._sdk_agent.conversation.last_response  # type: ignore[union-attr]
        return ""

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
            orphan = tel.model_copy(update={"result_status": "unknown", "execution_completed_at": datetime.now()})
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

        # AgentEndStatus and TurnEndStatus are parallel by value; convert directly
        # (mirrors the Codex sibling) so an unmapped future member raises loudly
        # instead of silently bucketing to COMPLETED.
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
                num_turns=self._assistant_turns,
                crashed=crashed,
                crash_reason=crash_reason,
                max_turns_exhausted=status is AgentEndStatus.MAX_TURNS_EXHAUSTED,
                duration_seconds=time.monotonic() - self.turn_start_time,
            )
        )

        if crashed:
            self._agent._capture_partial_turn(self.collector)
