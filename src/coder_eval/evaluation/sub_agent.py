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


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox


logger = logging.getLogger(__name__)


def _ignore_patterns_and_symlinks(patterns: list[str]):
    """``copytree`` ``ignore`` callable that drops pattern matches AND every symlink.

    Symlinks in the sandbox — whether malicious or accidental — are rejected
    rather than dereferenced into the judge workspace, which would leak host
    files (e.g. a ``creds -> /root/.aws/credentials`` plant) to a Bash-enabled
    judge.
    """
    pattern_ignore = shutil.ignore_patterns(*patterns)

    def _ignore(src: str, names: list[str]) -> set[str]:
        ignored = set(pattern_ignore(src, names))
        src_path = Path(src)
        for name in names:
            if name in ignored:
                continue
            if (src_path / name).is_symlink():
                ignored.add(name)
        return ignored

    return _ignore


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
        # SECURITY: setting_sources=[] is enforced by the caller — it's the caller's
        # responsibility to build the AgentConfig correctly because the field is part of
        # the type contract. We raise rather than mutate so a misconfigured caller fails
        # loudly instead of silently having its config changed. Not `assert` because the
        # check must survive `python -O`.
        if agent_config.setting_sources != []:
            raise ValueError(
                "SubAgentRunner requires agent_config.setting_sources=[] so the SDK does not "
                + "load .claude/settings.json or .mcp.json from the sub-agent's working directory."
            )
        assert sandbox.sandbox_dir is not None, "sandbox not initialized"
        self._sandbox = sandbox
        self._agent_config = agent_config
        self._ignore_patterns = ignore_patterns
        self._route = route
        # When set, copied into ``judge_dir / "_reference"`` after the main sandbox
        # copy. The judge can browse it via Read/Glob. ``None`` skips the copy —
        # callers MUST set this to None when the criterion has ``include_reference=False``
        # so the judge can't see grading material it was opted out of.
        self._reference_dir = reference_dir
        # Separate ignore set for the reference-side copytree. Defaults to ``[]``
        # so we DON'T silently strip a nested ``_reference/`` inside the user's
        # reference dir — the sandbox-side ignore set legitimately contains
        # ``_reference`` (defense-in-depth against agent-planted collisions at
        # the mount point), but reusing it here would silently drop a customer
        # subdir of the same name. Symlinks are stripped unconditionally by
        # ``_ignore_patterns_and_symlinks([])``.
        self._reference_ignore_patterns = reference_ignore_patterns or []
        # Runtime-only in-process MCP server injection (e.g. the judge
        # submit_verdict tool). NOT routed through ``sdk_options`` —
        # ``mcp_servers`` is in ``_FRAMEWORK_OWNED_SDK_FIELDS``.
        self._extra_mcp_servers = extra_mcp_servers or {}
        # Public attribute so the criterion can read it after ``run_async()`` returns.
        # When the caller passes ``capture=None`` the runner doesn't expose one,
        # matching the opt-in-per-construction contract for the verdict channel.
        self.capture = capture

    async def run_async(self, user_msg: str, *, max_turns: int | None, turn_timeout: float) -> TurnRecord:
        """Copy sandbox → start agent → communicate → stop. Kill on any exception.

        Async so a genuine network/subprocess wait yields the event loop instead
        of pinning a thread-pool thread — lets ``SuccessChecker.check_all_async``
        run this concurrently with other judge-type criteria (llm_judge) instead
        of serializing them. The blocking filesystem work (``copytree``/``rmtree``)
        is pushed to a worker thread via ``asyncio.to_thread`` so it doesn't block
        the loop either.

        Raises ``TurnTimeoutError`` when the agent exceeds ``turn_timeout``.
        """
        # Narrow via local var — checked in __init__ but pyright doesn't track that.
        src_dir = self._sandbox.sandbox_dir
        assert src_dir is not None, "sandbox not initialized"

        judge_dir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="sub_agent_"))
        try:
            # Copy the sandbox into an isolated temp dir. The sub-agent never touches
            # the original sandbox, so later criteria are unaffected by whatever it
            # does. Symlinks are skipped (vs preserved) so a malicious
            # `creds -> /root/.aws/credentials` plant can't leak host files to a
            # Bash-enabled sub-agent.
            await asyncio.to_thread(
                shutil.copytree,
                src_dir,
                judge_dir,
                symlinks=True,
                ignore=_ignore_patterns_and_symlinks(self._ignore_patterns),
                dirs_exist_ok=True,  # mkdtemp already created the target; allow merging in
            )

            # Mount the reference solution at ``_reference/`` for the judge to browse.
            # Symlinks are stripped unconditionally by the helper. Pattern-based
            # ignores use a SEPARATE list (``_reference_ignore_patterns``, default
            # ``[]``) so we don't reuse the sandbox-side ``ignore_patterns`` which
            # includes ``_reference`` as defense-in-depth — applying that here would
            # silently strip a nested ``_reference/`` subdir inside the user's
            # reference, dropping grading material the user explicitly chose to
            # include.
            #
            # Defense-in-depth on the sandbox side: ``AgentJudgeCriterion.agent.ignore_patterns``
            # includes ``_reference`` so the first copytree above strips any
            # sandbox-side ``_reference/`` (agent-planted or template-staged). If a
            # caller overrides ignore_patterns and removes ``_reference``, the second
            # copytree would FileExistsError; rmtree is the safety net AND ensures the
            # judge sees grading material exclusively from ``task.reference``.
            if self._reference_dir is not None:
                ref_dest = judge_dir / "_reference"
                if ref_dest.exists():
                    await asyncio.to_thread(shutil.rmtree, ref_dest, ignore_errors=True)
                # Deliberately NO ``dirs_exist_ok=True`` here. The rmtree above
                # is the canonical clear; if any file survives (read-only flag,
                # ENOTEMPTY race, hostile permission bits), we want copytree to
                # FileExistsError loudly rather than silently merge the reference
                # into agent-planted content under the same path. Loud failure on
                # a partial-rmtree edge case is preferred over silently grading
                # against a tampered ``_reference/``.
                await asyncio.to_thread(
                    shutil.copytree,
                    self._reference_dir,
                    ref_dest,
                    symlinks=True,
                    ignore=_ignore_patterns_and_symlinks(self._reference_ignore_patterns),
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
            await asyncio.to_thread(shutil.rmtree, judge_dir, ignore_errors=True)

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
