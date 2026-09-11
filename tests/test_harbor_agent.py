"""``coder_eval.harbor.agent.CoderEvalAgent`` — the Harbor-agent extension point.

``harbor`` is not a project dependency (it only needs to be present INSIDE a
Harbor trial container, see ``harbor/agent.py``'s module docstring), so this
module cannot simply be imported in a normal test run. Rather than skip it
entirely (leaving `run()`'s command construction and
`populate_context_post_run()`'s trajectory parsing with zero coverage), stub
just enough of the `harbor` package tree to satisfy the one import
(`harbor.agents.installed.base.BaseInstalledAgent`) and import the real module
against that stub.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from coder_eval.harbor.atif_models import AtifAgent, FinalMetrics, Step, Trajectory


class _FakeBaseInstalledAgent:
    """Just enough of Harbor's `BaseInstalledAgent` for `CoderEvalAgent` to subclass."""


@pytest.fixture
def coder_eval_agent_module(monkeypatch: pytest.MonkeyPatch):
    """Import `coder_eval.harbor.agent` against a stubbed `harbor` package tree."""
    harbor_mod = types.ModuleType("harbor")
    agents_mod = types.ModuleType("harbor.agents")
    installed_mod = types.ModuleType("harbor.agents.installed")
    base_mod = types.ModuleType("harbor.agents.installed.base")
    base_mod.BaseInstalledAgent = _FakeBaseInstalledAgent  # type: ignore[attr-defined]

    for name, mod in (
        ("harbor", harbor_mod),
        ("harbor.agents", agents_mod),
        ("harbor.agents.installed", installed_mod),
        ("harbor.agents.installed.base", base_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    # `coder_eval.harbor.agent` may already be cached (unlikely -- nothing else
    # imports it), but force a fresh import against the stub either way.
    monkeypatch.delitem(sys.modules, "coder_eval.harbor.agent", raising=False)
    import importlib

    return importlib.import_module("coder_eval.harbor.agent")


def _make_agent(module, *, logs_dir: Path):
    """Build a `CoderEvalAgent` instance without running Harbor's real `__init__`."""
    agent = object.__new__(module.CoderEvalAgent)
    agent.logs_dir = logs_dir
    agent.environment_logs_dir = logs_dir
    import logging

    agent.logger = logging.getLogger("test-coder-eval-agent")
    return agent


class TestRunCommandConstruction:
    async def test_run_shells_out_to_execute_with_workspace_dir_and_run_dir(self, coder_eval_agent_module, tmp_path):
        """`--workspace-dir "$(pwd)"` is Gap 2's real fix: without it the agent's
        tempdir sandbox writes outside the container's WORKDIR, where Harbor's
        verifier phase looks. `--run-dir` must point at `environment_logs_dir`
        (bind-mounted from Harbor's `self.logs_dir` on the host)."""
        agent = _make_agent(coder_eval_agent_module, logs_dir=tmp_path)

        captured: dict[str, object] = {}

        async def _fake_exec(environment, command):
            captured["environment"] = environment
            captured["command"] = command

        agent._exec = _fake_exec  # type: ignore[method-assign]

        fake_environment = object()
        await agent.run("unused instruction", fake_environment, context=None)

        assert captured["environment"] is fake_environment
        command = captured["command"]
        assert isinstance(command, str)
        assert command.startswith("coder-eval execute ")
        assert "--format harbor" in command
        assert f"--run-dir {tmp_path.as_posix()}" in command
        assert '--workspace-dir "$(pwd)"' in command


class TestPopulateContextPostRun:
    def _write_trajectory(self, path: Path, *, final_metrics: FinalMetrics | None) -> None:
        trajectory = Trajectory(
            agent=AtifAgent(name="coder-eval", version="0.0.0"),
            steps=[Step(step_id=1, source="agent", message="did the thing")],
            final_metrics=final_metrics,
        )
        path.write_text(trajectory.model_dump_json(exclude_none=True), encoding="utf-8")

    def test_fills_cost_and_token_fields_from_trajectory_json(self, coder_eval_agent_module, tmp_path):
        agent = _make_agent(coder_eval_agent_module, logs_dir=tmp_path)
        self._write_trajectory(
            tmp_path / "trajectory.json",
            final_metrics=FinalMetrics(
                total_prompt_tokens=100,
                total_completion_tokens=50,
                total_cached_tokens=10,
                total_cost_usd=0.25,
            ),
        )

        class _Context:
            cost_usd = None
            n_input_tokens = None
            n_cache_tokens = None
            n_output_tokens = None

        context = _Context()
        agent.populate_context_post_run(context)

        assert context.cost_usd == 0.25
        assert context.n_input_tokens == 100
        assert context.n_cache_tokens == 10
        assert context.n_output_tokens == 50

    def test_no_trajectory_json_is_a_silent_noop(self, coder_eval_agent_module, tmp_path):
        """coder-eval execute may have crashed before writing anything -- this
        must not raise, since it runs on every trial regardless."""
        agent = _make_agent(coder_eval_agent_module, logs_dir=tmp_path)

        class _Context:
            cost_usd = "untouched"

        context = _Context()
        agent.populate_context_post_run(context)

        assert context.cost_usd == "untouched"

    def test_trajectory_with_no_final_metrics_is_a_silent_noop(self, coder_eval_agent_module, tmp_path):
        agent = _make_agent(coder_eval_agent_module, logs_dir=tmp_path)
        self._write_trajectory(tmp_path / "trajectory.json", final_metrics=None)

        class _Context:
            cost_usd = "untouched"

        context = _Context()
        agent.populate_context_post_run(context)

        assert context.cost_usd == "untouched"

    def test_malformed_trajectory_json_is_a_silent_noop(self, coder_eval_agent_module, tmp_path):
        """Best-effort context enrichment: a parse failure must never fail the trial."""
        agent = _make_agent(coder_eval_agent_module, logs_dir=tmp_path)
        (tmp_path / "trajectory.json").write_text("not json", encoding="utf-8")

        class _Context:
            cost_usd = "untouched"

        context = _Context()
        agent.populate_context_post_run(context)

        assert context.cost_usd == "untouched"


def test_name_and_version(coder_eval_agent_module):
    from coder_eval import __version__

    assert coder_eval_agent_module.CoderEvalAgent.name() == "coder-eval"
    agent = object.__new__(coder_eval_agent_module.CoderEvalAgent)
    assert agent.version() == __version__
