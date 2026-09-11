"""``CoderEvalAgent`` — coder-eval as a Harbor agent (C1.2).

The mirror image of Part C's packager: instead of coder-eval grading a
Harbor-authored task (coder-eval as the verifier), this makes coder-eval
Harbor's AGENT — ``harbor run -a coder_eval.harbor.agent:CoderEvalAgent``
invokes ``coder-eval execute --format harbor`` inside Harbor's own container
against the fixed-path agent-phase task.yaml the packager bakes in (see
``agent_paths.py``), then Harbor picks up the resulting ``trajectory.json``
from ``self.logs_dir`` exactly as it does for its own ``ClaudeCode`` agent.

Design, per ``tmp/harborframework.md``'s "Scoping note — Part A revisited":

- The agent-phase task.yaml is at :data:`AGENT_TASK_YAML_PATH`, criteria-free
  (see ``packager.py``'s ``_write_agent_phase_task_yaml``) — this agent never
  sees ``success_criteria``, only the real ``agent``/prompt/sandbox config.
- ``coder-eval execute --format harbor --run-dir <environment_logs_dir>``
  writes ``task.json`` AND a ``trajectory.json`` (ATIF) sibling directly into
  the container's ``/logs/agent/`` (``environment_logs_dir``), which Harbor
  bind-mounts from ``self.logs_dir`` on the host — the same path Harbor's own
  ``ClaudeCode`` agent writes its trajectory to (verified against the
  installed ``harbor`` package's ``populate_context_post_run``: it writes to
  ``self.logs_dir / "trajectory.json"`` on the HOST side, after the container
  syncs back). This class instead has coder-eval write it directly inside the
  container at the mirrored path, so no separate "workspace_dir" plumbing is
  needed for v1 (Gap 2's accepted workaround).

Verified against a real ``harbor==0.22.0`` install (``tmp/harbor-venv``):
``BaseAgent.name()`` is a ``@staticmethod``; ``run()`` returns ``None`` and
must not return the trajectory itself (Harbor discovers it by reading
``self.logs_dir / "trajectory.json"`` after the container syncs back, the same
way ``ClaudeCode.populate_context_post_run`` does) — so this class overrides
``populate_context_post_run`` to parse that file and fill in
``AgentContext``'s token/cost fields, matching ``ClaudeCode``'s own pattern
exactly. ``environment.exec()`` takes a single shell command STRING (not an
argv list).
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from coder_eval.harbor.agent_paths import AGENT_TASK_YAML_PATH


if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

try:
    from harbor.agents.installed.base import BaseInstalledAgent
except ImportError as e:  # pragma: no cover - exercised only where `harbor` is installed
    raise ImportError(
        "coder_eval.harbor.agent requires the `harbor` package (not installed here). This module is "
        + "meant to run INSIDE a Harbor trial container, where `harbor` is already present -- it is not "
        + "a coder-eval runtime dependency. If you are trying to use this as a Harbor agent, install/pin "
        + "`harbor` in the image the packager builds."
    ) from e

try:
    from coder_eval import __version__
except ImportError:  # pragma: no cover - defensive; coder_eval always defines this
    __version__ = "0.0.0"


class CoderEvalAgent(BaseInstalledAgent):
    """Runs coder-eval's own agent loop as a Harbor agent.

    ``run()`` shells out to ``coder-eval execute --format harbor`` against the
    baked-in agent-phase task.yaml rather than importing coder-eval's
    orchestrator in-process — the whole point is that this class lives inside
    the SAME container image the packager built, so ``coder-eval`` is already
    on PATH there exactly as ``tests/test.sh`` assumes it is for grading.
    """

    SUPPORTS_ATIF: bool = True

    @staticmethod
    def name() -> str:
        return "coder-eval"

    def version(self) -> str:
        return __version__

    async def install(self, environment: BaseEnvironment) -> None:
        """No-op: the packager's exported image is expected to already have `coder-eval` installed.

        Unlike an agent whose CLI is fetched at trial time (npm/pip install
        inside ``install()``), coder-eval is baked into the image at export
        time -- re-installing it here would fight whatever version the image
        pins.
        """
        del environment

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        """Run ``coder-eval execute --format harbor`` inside the environment.

        ``instruction`` (Harbor's resolved ``instruction.md`` text) is NOT
        forwarded — the agent-phase task.yaml at :data:`AGENT_TASK_YAML_PATH`
        already carries the identical resolved prompt (``packager.py`` writes
        both from the same source), so there is nothing to forward. Token/cost
        totals are filled in afterward by ``populate_context_post_run``, not
        here, matching every other installed agent's convention.
        """
        del instruction, context  # nothing to forward; context is populated post-run
        run_dir = self.environment_logs_dir.as_posix()
        command = (
            f"coder-eval execute {shlex.quote(AGENT_TASK_YAML_PATH)} --format harbor --run-dir {shlex.quote(run_dir)}"
        )
        await self._exec(environment, command)

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Parse the ``trajectory.json`` coder-eval wrote and fill in token/cost totals.

        Mirrors ``ClaudeCode.populate_context_post_run`` exactly: by the time
        this runs, Harbor has synced the container's ``environment_logs_dir``
        back to the host at ``self.logs_dir``, so the file coder-eval wrote
        during ``run()`` is now readable here.
        """
        trajectory_path = self.logs_dir / "trajectory.json"
        if not trajectory_path.is_file():
            self.logger.debug(f"No trajectory.json at {trajectory_path}; coder-eval execute may have failed")
            return

        from coder_eval.harbor.atif_models import Trajectory

        try:
            trajectory = Trajectory.model_validate_json(trajectory_path.read_text(encoding="utf-8"))
        except Exception as exc:  # best-effort context enrichment, never fatal to the trial
            self.logger.debug(f"Failed to parse {trajectory_path}: {exc}")
            return

        if trajectory.final_metrics is None:
            return
        metrics = trajectory.final_metrics
        context.cost_usd = metrics.total_cost_usd
        context.n_input_tokens = metrics.total_prompt_tokens or 0
        context.n_cache_tokens = metrics.total_cached_tokens or 0
        context.n_output_tokens = metrics.total_completion_tokens or 0


__all__ = ["CoderEvalAgent"]
