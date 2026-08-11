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
        # ``ignore_patterns_and_symlinks([])``.
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
        await this directly without pinning a thread. (``check_all_async``
        currently runs criteria sequentially; running this concurrently with
        other judge-type criteria is deferred to a follow-up PR.) The blocking
        filesystem work (``copytree``/``rmtree``) is pushed to a worker thread
        via ``asyncio.to_thread`` so it doesn't block the loop either.

        Cancellation safety: ``run_async`` is awaited directly on the
        orchestrator's own loop (not under its own ``asyncio.run`` on a worker
        thread), so it is reachable by cancellation (e.g. the ``task_timeout``
        watchdog cancelling the orchestrator task) at any ``await`` — including
        mid-``copytree``. ``asyncio.to_thread`` is NOT itself cancellable (the
        worker thread keeps running after the awaiting coroutine raises
        ``CancelledError``), so every such call here is wrapped in
        ``asyncio.shield`` and tracked in ``self._pending`` — the ``finally``
        below awaits any still-in-flight one BEFORE ``rmtree``, so an orphan
        thread can never recreate files in ``judge_dir`` after cleanup already
        ran. ``judge_dir`` itself is bound with a plain synchronous
        ``tempfile.mkdtemp`` (a single fast syscall — offloading it via
        ``to_thread`` only widens the cancellation window for no benefit, since
        a bare syscall isn't itself an await point cancellation can land on).

        Raises ``TurnTimeoutError`` when the agent exceeds ``turn_timeout``.
        """
        # Narrow via local var — checked in __init__ but pyright doesn't track that.
        src_dir = self._sandbox.sandbox_dir
        assert src_dir is not None, "sandbox not initialized"

        judge_dir = Path(tempfile.mkdtemp(prefix="sub_agent_"))
        pending: list[asyncio.Task[Any]] = []
        try:
            # Copy the sandbox into an isolated temp dir. The sub-agent never touches
            # the original sandbox, so later criteria are unaffected by whatever it
            # does. Symlinks are skipped (vs preserved) so a malicious
            # `creds -> /root/.aws/credentials` plant can't leak host files to a
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
                    await self._shielded_to_thread(pending, shutil.rmtree, ref_dest, ignore_errors=True)
                # Deliberately NO ``dirs_exist_ok=True`` here. The rmtree above
                # is the canonical clear; if any file survives (read-only flag,
                # ENOTEMPTY race, hostile permission bits), we want copytree to
                # FileExistsError loudly rather than silently merge the reference
                # into agent-planted content under the same path. Loud failure on
                # a partial-rmtree edge case is preferred over silently grading
                # against a tampered ``_reference/``.
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
            # Any shielded to_thread call above keeps running on its worker thread even
            # after a cancellation unwinds us into this `finally` — awaiting it here
            # (not itself re-cancelled: asyncio delivers a given cancel() as a single
            # CancelledError at the point it lands, not to every subsequent await in the
            # same coroutine) lets it actually finish BEFORE we rmtree, so an orphan
            # thread can never recreate files in judge_dir after cleanup already ran.
            if pending:
                await asyncio.gather(*(t for t in pending if not t.done()), return_exceptions=True)
            # Deliberately NOT `await asyncio.to_thread(...)` here: a bare await inside
            # `finally` is itself cancellable — cancelling right as this line is reached
            # would skip the cleanup and leak the (now up-to-date) sandbox copy with no
            # reaper. rmtree is a best-effort, bounded filesystem op; call it
            # synchronously so cancellation can't interrupt it mid-cleanup.
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
