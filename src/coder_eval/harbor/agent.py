"""``CoderEvalAgent`` — coder-eval as a Harbor agent (C1.2).

The mirror image of the packager: instead of coder-eval grading a Harbor-authored
task, this makes coder-eval Harbor's AGENT. ``harbor run -a
coder_eval.harbor.agent:CoderEvalAgent`` invokes ``coder-eval execute --format
harbor`` inside Harbor's own container against the fixed-path agent-phase task.yaml
the packager bakes in (see ``agent_paths.py``), writing ``task.json`` and a
``trajectory.json`` sibling directly into the container's ``/logs/agent/``, which
Harbor bind-mounts from ``self.logs_dir``.

``BaseAgent.run()`` returns ``None`` and must not return the trajectory itself —
Harbor discovers it by reading ``self.logs_dir / "trajectory.json"`` after the
container syncs back — so this class overrides ``populate_context_post_run`` to parse
that file and fill in the context's token and cost fields, matching the installed
agents' own pattern. ``environment.exec()`` takes a single shell command STRING.

Rationale: .claude/notes/reporting.md § Harbor export
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
        + "a coder-eval runtime dependency. Install it with `pip install 'coder-eval[harbor]'` into the "
        + "same interpreter as coder_eval (the image the packager builds, or a `hb run` host)."
    ) from e

try:
    from coder_eval import __version__
except ImportError:  # pragma: no cover - defensive; coder_eval always defines this
    __version__ = "0.0.0"


# Run-level bookkeeping goes here and is discarded with the container. Anywhere
# outside the WORKDIR would do; /tmp is the one path guaranteed writable in every
# task image. NOT a bare "/tmp": a dedicated subdirectory keeps run.json/run.md/
# experiment.* from littering a directory tasks themselves use.
_THROWAWAY_RUN_DIR = "/tmp/coder-eval-run"


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

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        """Run ``coder-eval execute --format harbor`` inside the environment.

                ``instruction`` is NOT forwarded: the agent-phase task.yaml already carries
                the identical resolved prompt. Token and cost totals are filled in afterward
                by ``populate_context_post_run``.

                ``--workspace-dir "$(pwd)"`` is load-bearing — without it the tempdir sandbox
                writes the agent's workspace somewhere Harbor's verifier never looks.

        ``--run-dir`` must be passed even though ``${run_dir}`` is never substituted
                (both templates below are static), because run-level bookkeeping -- run.json,
                run.md, experiment.* -- still follows it and its DEFAULT is CWD-RELATIVE
                (``runs/<timestamp>``). Omitting it does NOT leave those files harmlessly
                uncollected: cwd is the WORKDIR, so they land INSIDE the agent's own
                workspace, polluting the very directory ``--artifacts-dir`` names and that
                Harbor snapshots. Confirmed live -- ``artifacts/work/runs/<timestamp>/run.json``
                appeared in a collected trial.

                It points at a throwaway tmp path rather than the agent logs dir on purpose:
                Harbor does its own trial-level reporting, so run-level files are of no
                interest to it and would only clutter what it syncs back. The per-task files
                that DO matter -- task.json/task.log/task.html and the promoted
                trajectory.json this class reads back -- all follow ``--logging-dir``.

                The two directory templates are passed as STATIC paths, which is the whole
                reason no copy happens: ``--logging-dir`` puts task.json/task.log flat in
                Harbor's agent logs dir, and ``--artifacts-dir`` names the container's own
                WORKDIR -- the directory the agent already ran in, and the one Harbor itself
                snapshots (task.toml's ``artifacts``) before teardown. Because artifacts are
                no longer a child of the logging dir, nothing lands twice; and because the
                artifacts destination IS the workspace, the capture self-cancels. A static
                template needs no special-casing anywhere -- substituting a string with no
                placeholders is the identity function.

                Rationale: .claude/notes/reporting.md § The non-obvious constraint in the emitted task.yaml
        """
        del instruction, context  # nothing to forward; context is populated post-run
        run_dir = self.environment_logs_dir.as_posix()
        command = (
            f"coder-eval execute {shlex.quote(AGENT_TASK_YAML_PATH)} --format harbor "
            f"--run-dir {_THROWAWAY_RUN_DIR} "
            f'--workspace-dir "$(pwd)" '
            f'--logging-dir {shlex.quote(run_dir)} --artifacts-dir "$(pwd)"'
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
