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
from typing import TYPE_CHECKING

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.models import AgentConfig, ProxyRoute


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox


logger = logging.getLogger(__name__)


class UnsupportedRouteError(RuntimeError):
    """Raised by SubAgentRunner when the configured API route is not supported."""


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
    - PROXY fail-fast (the SDK bridging isn't wired through the evaluator proxy yet).
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
        agent_config: AgentConfig,
        ignore_patterns: list[str],
        route: ApiRoute,
    ) -> None:
        # SECURITY: setting_sources=[] is enforced by the caller — it's the caller's
        # responsibility to build the AgentConfig correctly because the field is part of
        # the type contract. We raise rather than mutate so a misconfigured caller fails
        # loudly instead of silently having its config changed. Not `assert` because the
        # check must survive `python -O`.
        if agent_config.setting_sources != []:
            raise ValueError(
                "SubAgentRunner requires agent_config.setting_sources=[] so the SDK does not "
                "load .claude/settings.json or .mcp.json from the sub-agent's working directory."
            )
        assert sandbox.sandbox_dir is not None, "sandbox not initialized"
        self._sandbox = sandbox
        self._agent_config = agent_config
        self._ignore_patterns = ignore_patterns
        self._route = route

    def run(self, user_msg: str, turn_timeout: float) -> TurnRecord:
        """Copy sandbox → start agent → communicate → stop. Kill on any exception.

        Raises ``UnsupportedRouteError`` when the route is ``ProxyRoute`` (fail-fast
        before allocating any resources).
        Raises ``TurnTimeoutError`` when the agent exceeds ``turn_timeout``.
        """
        if isinstance(self._route, ProxyRoute):
            raise UnsupportedRouteError("sub_agent: PROXY backend not supported in MVP")

        # Narrow via local var — checked in __init__ but pyright doesn't track that.
        src_dir = self._sandbox.sandbox_dir
        assert src_dir is not None, "sandbox not initialized"

        judge_dir = Path(tempfile.mkdtemp(prefix="sub_agent_"))
        try:
            # Copy the sandbox into an isolated temp dir. The sub-agent never touches
            # the original sandbox, so later criteria are unaffected by whatever it
            # does. Symlinks are skipped (vs preserved) so a malicious
            # `creds -> /root/.aws/credentials` plant can't leak host files to a
            # Bash-enabled sub-agent.
            shutil.copytree(
                src_dir,
                judge_dir,
                symlinks=True,
                ignore=_ignore_patterns_and_symlinks(self._ignore_patterns),
                dirs_exist_ok=True,  # mkdtemp already created the target; allow merging in
            )

            agent = ClaudeCodeAgent(self._agent_config, route=self._route)
            logger.info(
                "sub_agent: starting (model=%s, max_turns=%d, allowed_tools=%s)",
                self._agent_config.model,
                self._agent_config.max_turns,
                self._agent_config.allowed_tools,
            )
            # Safe because callers run check_all via asyncio.to_thread, so this
            # invocation is on a worker thread with no active event loop. A direct
            # async caller would get RuntimeError — acceptable for the architecture.
            turn = asyncio.run(self._run_agent(agent, judge_dir, user_msg, turn_timeout))
            logger.info(
                "sub_agent: finished (duration=%.1fs, tokens=%s)",
                turn.duration_seconds,
                turn.token_usage,
            )
            return turn
        finally:
            shutil.rmtree(judge_dir, ignore_errors=True)

    @staticmethod
    async def _run_agent(
        agent: ClaudeCodeAgent,
        judge_dir: Path,
        user_msg: str,
        turn_timeout: float,
    ) -> TurnRecord:
        """Run the sub-agent. Hard-kill on any exit path.

        ``stop()`` is cooperative; the SDK's anyio task groups can swallow
        cancellation, so ``kill()`` is required to guarantee the subprocess dies.
        """
        try:
            await agent.start(str(judge_dir))
            return await agent.communicate(user_msg, timeout=turn_timeout)
        except BaseException:
            with contextlib.suppress(Exception):
                await agent.kill()
            raise
        finally:
            with contextlib.suppress(Exception):
                await agent.stop()
