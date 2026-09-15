"""Isolated sandbox-copy + Claude Code SDK subprocess lifecycle for judge-style criteria.

SECURITY: owns the hardening knobs in one place — symlink-stripping copy,
``setting_sources=[]`` enforcement, ignore patterns for ``.claude``/``.mcp.json``.
Any future sub-agent criterion inherits the same posture by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.evaluation.verdict_tool import VerdictCapture
from coder_eval.models import ClaudeCodeAgentConfig
from coder_eval.path_utils import ignore_patterns_and_symlinks


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox


logger = logging.getLogger(__name__)


class SubAgentRunner:
    """Spawn a Claude Code SDK agent in an isolated sandbox copy and return its turn.

    Owns:
    - ``mkdtemp`` + ``copytree`` with symlink filtering + pattern ignores.
    - ``ClaudeCodeAgent`` construction, ``start``/``communicate``/``stop``/``kill`` lifecycle.
    - Temp-dir cleanup on every exit path.

    Does NOT own:
    - Verdict parsing — caller does that on the returned ``TurnRecord``.
    - Error-to-CriterionResult mapping — caller maps exceptions to their own result type.
    """

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        agent_config: ClaudeCodeAgentConfig,
        ignore_patterns: list[str],
        route: ApiRoute,
        reference_dir: Path | None = None,
        reference_ignore_patterns: list[str] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
        capture: VerdictCapture | None = None,
    ) -> None:
        # SECURITY: raise rather than mutate, so a misconfigured caller fails loudly
        # instead of silently having its config changed. Not `assert`: the check
        # must survive `python -O`.
        # Rationale: .claude/notes/contracts.md § The security floor
        if agent_config.setting_sources != []:
            raise ValueError(
                "SubAgentRunner requires agent_config.setting_sources=[] so the SDK does not "
                + "load .claude/settings.json or .mcp.json from the sub-agent's working directory."
            )
        # A sub-agent's system_prompt is its ENTIRE identity, so the coding-agent
        # preset must never prefix it -- and an omitted prompt gets the bare preset,
        # which is the same failure, so neither is accepted.
        # Rationale: .claude/notes/contracts.md § The judge's identity is its system prompt
        if agent_config.system_prompt is None or agent_config.system_prompt_mode != "replace":
            raise ValueError(
                "SubAgentRunner requires agent_config.system_prompt to be set with "
                + "system_prompt_mode='replace': the sub-agent prompt is its entire identity "
                + "and must not be appended to (or replaced by) the claude_code coding-agent preset."
            )
        assert sandbox.sandbox_dir is not None, "sandbox not initialized"
        self._sandbox = sandbox
        self._agent_config = agent_config
        self._ignore_patterns = ignore_patterns
        self._route = route
        # HAZARD: callers MUST set this to None when the criterion has
        # ``include_reference=False``, or the judge sees grading material it was
        # opted out of.
        self._reference_dir = reference_dir
        # SEPARATE from the sandbox-side set, defaulting to ``[]``: that one
        # contains ``_reference`` as defense-in-depth, and reusing it here would
        # drop a user's own nested subdir of the same name.
        # Rationale: .claude/notes/contracts.md § The security floor
        self._reference_ignore_patterns = reference_ignore_patterns or []
        # Runtime-only MCP injection, NOT via ``sdk_options`` -- ``mcp_servers`` is framework-owned.
        self._extra_mcp_servers = extra_mcp_servers or {}
        # Public so the criterion can read it after ``run_async()`` returns; absent
        # when the caller passed ``capture=None``.
        self.capture = capture

    async def run_async(self, user_msg: str, *, max_turns: int | None, turn_timeout: float) -> TurnRecord:
        """Copy sandbox → start agent → communicate → stop. Kill on any exception.

        Async so a genuine network/subprocess wait yields the event loop instead of
        pinning a thread-pool thread; the blocking filesystem work is pushed to a
        worker thread. Every such call is SHIELDED and awaited in the ``finally``
        before cleanup, so an orphan thread cannot recreate files in ``judge_dir``
        after cleanup already ran.

        Raises ``TurnTimeoutError`` when the agent exceeds ``turn_timeout``.

        Rationale: .claude/notes/contracts.md § Cancellation safety
        """
        # Narrow via local var — checked in __init__ but pyright doesn't track that.
        src_dir = self._sandbox.sandbox_dir
        assert src_dir is not None, "sandbox not initialized"

        judge_dir = Path(tempfile.mkdtemp(prefix="sub_agent_"))
        pending: list[asyncio.Task[Any]] = []
        try:
            # HAZARD: symlinks are SKIPPED, not preserved, so a planted
            # `creds -> /root/.aws/credentials` cannot leak host files to a
            # Bash-enabled sub-agent.
            await self._shielded_to_thread(
                pending,
                shutil.copytree,
                src_dir,
                judge_dir,
                symlinks=True,
                ignore=ignore_patterns_and_symlinks(self._ignore_patterns),
                dirs_exist_ok=True,  # mkdtemp already created the target; allow merging in
            )

            # The rmtree is the safety net that keeps the judge's grading material
            # coming exclusively from ``task.reference``: the sandbox-side ignore set
            # strips any agent-planted ``_reference/``, but a caller could override it.
            # Rationale: .claude/notes/contracts.md § The security floor
            if self._reference_dir is not None:
                ref_dest = judge_dir / "_reference"
                if ref_dest.exists():
                    await self._shielded_to_thread(pending, shutil.rmtree, ref_dest, ignore_errors=True)
                # HAZARD: deliberately NO ``dirs_exist_ok=True``. If any file
                # survives the rmtree, fail loudly rather than merge the reference
                # into agent-planted content under the same path.
                await self._shielded_to_thread(
                    pending,
                    shutil.copytree,
                    self._reference_dir,
                    ref_dest,
                    symlinks=True,
                    ignore=ignore_patterns_and_symlinks(self._reference_ignore_patterns),
                )

            agent = ClaudeCodeAgent(
                self._agent_config,
                route=self._route,
                extra_mcp_servers=self._extra_mcp_servers,
            )
            logger.info(
                "sub_agent: starting (model=%s, max_turns=%s, allowed_tools=%s)",
                self._agent_config.model,
                max_turns,
                self._agent_config.allowed_tools,
            )
            turn = await self._run_agent(
                agent,
                judge_dir,
                user_msg,
                max_turns,
                turn_timeout,
                plugin_tools_dir=self._sandbox.plugin_tools_dir,
            )
            logger.info(
                "sub_agent: finished (duration=%.1fs, tokens=%s)",
                turn.duration_seconds,
                turn.token_usage,
            )
            return turn
        finally:
            # A shielded to_thread keeps running after cancellation unwinds us here;
            # awaiting it lets it finish BEFORE the rmtree.
            # Rationale: .claude/notes/contracts.md § Cancellation safety
            if pending:
                await asyncio.gather(*(t for t in pending if not t.done()), return_exceptions=True)
            # HAZARD: deliberately synchronous. A bare await inside `finally` is
            # itself cancellable, and cancelling here would leak the sandbox copy.
            shutil.rmtree(judge_dir, ignore_errors=True)  # noqa: CE002

    @staticmethod
    async def _shielded_to_thread(pending: list[asyncio.Task[Any]], func: Any, *args: Any, **kwargs: Any) -> Any:
        """``await asyncio.to_thread(func, *args, **kwargs)``, but shielded from
        cancellation and tracked in ``pending`` so ``run_async``'s ``finally``
        can wait for it to actually finish before cleaning up ``judge_dir``.

        ``asyncio.shield`` makes the AWAIT here cancellable (a cancellation
        still propagates to the caller immediately, unwinding into `finally`
        as normal) while the underlying worker-thread task keeps running
        independently in the background — ``pending`` is how `finally` finds
        it again to wait for it rather than leaving it to race the cleanup.
        """
        task = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
        pending.append(task)
        return await asyncio.shield(task)

    @staticmethod
    async def _run_agent(
        agent: ClaudeCodeAgent,
        judge_dir: Path,
        user_msg: str,
        max_turns: int | None,
        turn_timeout: float,
        *,
        plugin_tools_dir: str | None = None,
    ) -> TurnRecord:
        """Run the sub-agent. Hard-kill on any exit path.

        ``stop()`` is cooperative; the SDK's anyio task groups can swallow
        cancellation, so ``kill()`` is required to guarantee the subprocess dies.
        """
        try:
            await agent.start(str(judge_dir), plugin_tools_dir=plugin_tools_dir)
            return await agent.communicate(user_msg, timeout=turn_timeout, max_turns=max_turns)
        except BaseException:
            with contextlib.suppress(Exception):
                await agent.kill()
            raise
        finally:
            with contextlib.suppress(Exception):
                await agent.stop()
