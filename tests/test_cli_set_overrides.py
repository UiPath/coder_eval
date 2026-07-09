"""Tests for the generic ``-D`` / ``--set`` CLI override flag and alias parity."""

from __future__ import annotations

import re

import pytest
import typer
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.cli.run_command import _build_overrides


runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _overrides(**kwargs):
    """Call _build_overrides with all-None defaults, overriding only what's passed."""
    base = dict(
        model=None,
        driver=None,
        set_overrides=[],
    )
    base.update(kwargs)
    return _build_overrides(**base)


class TestCliHelp:
    def test_help_lists_set_and_dash_d(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "--set" in output
        assert "-D" in output


class TestCliValidationErrors:
    def test_typo_suggests_field(self):
        result = runner.invoke(app, ["run", "-D", "agent.modle=x"])
        assert result.exit_code != 0
        assert "did you mean 'model'" in _strip_ansi(result.output)

    def test_unknown_root_rejected(self):
        result = runner.invoke(app, ["run", "-D", "foo.bar=1"])
        assert result.exit_code != 0
        assert "unknown override root" in _strip_ansi(result.output)

    def test_alias_and_dash_d_collision_hard_errors(self):
        result = runner.invoke(app, ["run", "--model", "opus", "-D", "agent.model=sonnet"])
        assert result.exit_code != 0
        output = _strip_ansi(result.output)
        assert "agent.model" in output
        assert "--model" in output

    def test_two_dash_d_collision_hard_errors(self):
        result = runner.invoke(app, ["run", "-D", "run_limits.max_turns=30", "-D", "run_limits.max_turns=40"])
        assert result.exit_code != 0
        assert "more than once" in _strip_ansi(result.output)


class TestSemanticErrorsCleanExit:
    """Layer-5 override errors that surface at task resolution must exit cleanly.

    A bad value (invalid Literal, framework-managed sdk_options key, sdk_options on
    a non-claude agent) raises during resolution; the CLI must wrap it as a clean
    typer.BadParameter (exit 2) rather than letting a raw ValueError/ValidationError
    escape as a traceback.
    """

    def _task_file(self, tmp_path):
        task_file = tmp_path / "t.yaml"
        task_file.write_text(
            "task_id: t\n"
            "description: x\n"
            "initial_prompt: do\n"
            "agent:\n  type: claude-code\n"
            "sandbox:\n  driver: tempdir\n"
            "success_criteria:\n  - type: file_exists\n    path: f.txt\n    description: x\n"
        )
        return task_file

    def _assert_clean_nonzero(self, result):
        assert result.exit_code != 0
        # The original ValueError/ValidationError must NOT escape unhandled.
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"expected a clean CLI error, got unhandled {result.exception!r}"
        )

    def test_invalid_driver_value_exits_cleanly(self, tmp_path):
        task_file = self._task_file(tmp_path)
        result = runner.invoke(
            app,
            [
                "run",
                str(task_file),
                "--run-dir",
                str(tmp_path / "run"),
                "--preservation-mode",
                "NONE",
                "-D",
                "sandbox.driver=bogus",
            ],
        )
        self._assert_clean_nonzero(result)

    def test_sdk_options_framework_key_exits_cleanly(self, tmp_path):
        task_file = self._task_file(tmp_path)
        result = runner.invoke(
            app,
            [
                "run",
                str(task_file),
                "--run-dir",
                str(tmp_path / "run"),
                "--preservation-mode",
                "NONE",
                "-D",
                "agent.sdk_options.hooks={}",
            ],
        )
        self._assert_clean_nonzero(result)


class TestBuildOverridesAliasParity:
    """Only ``--model`` and ``--driver`` survive as aliases; everything else is -D."""

    def test_model_alias_matches_dash_d(self):
        assert _overrides(model="opus") == _overrides(set_overrides=["agent.model=opus"])

    def test_driver_alias_matches_dash_d(self):
        assert _overrides(driver="docker") == _overrides(set_overrides=["sandbox.driver=docker"])

    def test_dash_d_value_coercion(self):
        # YAML-typed values: int stays int, truthy-alias stays string.
        assert _overrides(set_overrides=["run_limits.max_turns=30"])["run_limits.max_turns"] == 30
        assert _overrides(set_overrides=["agent.model=on"])["agent.model"] == "on"

    def test_collision_with_model_alias(self):
        with pytest.raises(typer.BadParameter, match=r"agent\.model"):
            _overrides(model="opus", set_overrides=["agent.model=sonnet"])


class TestResolutionLevel:
    """The override map applied to a resolved task via the engine."""

    def _task(self, sdk_options=None, run_limits=None):
        from coder_eval.models import RunLimits, SandboxConfig, TaskDefinition, parse_agent_config

        return TaskDefinition(
            task_id="t",
            description="x",
            initial_prompt="hi",
            agent=parse_agent_config(type="claude-code", **({"sdk_options": sdk_options} if sdk_options else {})),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.txt", "description": "x"}],
            run_limits=RunLimits(**run_limits) if run_limits else None,
        )

    def test_sdk_option_merges_without_clobbering(self):
        from coder_eval.orchestration.overrides import apply_overrides

        task = self._task(sdk_options={"max_thinking_tokens": 1024})
        apply_overrides(task, _overrides(set_overrides=["agent.sdk_options.effort=high"]))
        assert task.agent.sdk_options == {"max_thinking_tokens": 1024, "effort": "high"}

    def test_max_turns_leaves_task_timeout_intact(self):
        from coder_eval.orchestration.overrides import apply_overrides

        task = self._task(run_limits={"task_timeout": 600})
        apply_overrides(task, _overrides(set_overrides=["run_limits.max_turns=5"]))
        assert task.run_limits is not None
        assert task.run_limits.max_turns == 5
        assert task.run_limits.task_timeout == 600

    def test_docker_working_dir_override(self):
        from coder_eval.orchestration.overrides import apply_overrides

        task = self._task()
        apply_overrides(task, _overrides(set_overrides=["sandbox.docker.working_dir=/root"]))
        assert task.sandbox.docker.working_dir == "/root"
