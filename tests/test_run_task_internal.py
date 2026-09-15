"""Tests for the in-container entry point (`coder-eval _run-task-internal`).

CE048: the command itself is a Typer command whose parameter defaults are
`OptionInfo` sentinels, so it must never be invoked in-process. These tests
target the extracted, pure `_scrub_staged_inputs` helper instead.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.cli.run_task_internal_command import _scrub_staged_inputs
from coder_eval.models import IN_CONTAINER_ENV


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    """Write a staged task.yaml + context.json, both carrying the criteria."""
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text("task_id: x\nsuccess_criteria: []\n", encoding="utf-8")
    context_json = tmp_path / "context.json"
    # source_yaml is the raw task text (criteria verbatim) -- the second copy the
    # scrub must remove, at the top level and inside config_lineage.
    context_json.write_text(
        '{"variant_id": "v", "source_yaml": "task_id: x\\nsuccess_criteria: [SECRET]\\n"}',
        encoding="utf-8",
    )
    return task_yaml, context_json


class TestScrubStagedInputs:
    """Anti-cheat: BOTH staged grading-answer-key copies (task.yaml's criteria AND
    context.json's source_yaml) are deleted after load so the in-container agent
    cannot read its own criteria back from either file."""

    def test_deletes_both_when_in_container(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(IN_CONTAINER_ENV, "1")
        task_yaml, context_json = _stage(tmp_path)

        _scrub_staged_inputs(task_yaml, context_json)

        assert not task_yaml.exists()
        assert not context_json.exists(), "context.json (source_yaml has criteria) must be deleted too"

    def test_survives_when_not_in_container(self, tmp_path: Path, monkeypatch):
        # A host/tempdir invocation shares our uid and has no filesystem
        # isolation; the gate keeps the delete docker-only by construction.
        monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
        task_yaml, context_json = _stage(tmp_path)

        _scrub_staged_inputs(task_yaml, context_json)

        assert task_yaml.exists()
        assert context_json.exists()

    def test_env_var_set_but_not_one_leaves_files(self, tmp_path: Path, monkeypatch):
        # Only the exact "1" sentinel arms the delete, mirroring the reference
        # window gate (Sandbox.enforces_permission_windows).
        monkeypatch.setenv(IN_CONTAINER_ENV, "0")
        task_yaml, context_json = _stage(tmp_path)

        _scrub_staged_inputs(task_yaml, context_json)

        assert task_yaml.exists()
        assert context_json.exists()

    def test_missing_files_do_not_raise(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(IN_CONTAINER_ENV, "1")
        task_yaml = tmp_path / "does-not-exist.yaml"
        context_json = tmp_path / "also-missing.json"

        # missing_ok=True: a re-entrant call must not crash on an absent file.
        _scrub_staged_inputs(task_yaml, context_json)

        assert not task_yaml.exists()
        assert not context_json.exists()
