"""Tests for the in-container entry point (`coder-eval _run-task-internal`).

CE048: the command itself is a Typer command whose parameter defaults are
`OptionInfo` sentinels, so it must never be invoked in-process. These tests
target the extracted, pure `_scrub_staged_task_yaml` helper instead.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.cli.run_task_internal_command import _scrub_staged_task_yaml
from coder_eval.models import IN_CONTAINER_ENV


class TestScrubStagedTaskYaml:
    """Anti-cheat: the staged task.yaml (with success_criteria) is deleted after
    load so the in-container agent cannot read its own grading answer key."""

    def test_deletes_when_in_container(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(IN_CONTAINER_ENV, "1")
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text("task_id: x\nsuccess_criteria: []\n", encoding="utf-8")

        _scrub_staged_task_yaml(task_yaml)

        assert not task_yaml.exists()

    def test_survives_when_not_in_container(self, tmp_path: Path, monkeypatch):
        # A host/tempdir invocation shares our uid and has no filesystem
        # isolation; the gate keeps the delete docker-only by construction.
        monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text("task_id: x\n", encoding="utf-8")

        _scrub_staged_task_yaml(task_yaml)

        assert task_yaml.exists()

    def test_env_var_set_but_not_one_leaves_file(self, tmp_path: Path, monkeypatch):
        # Only the exact "1" sentinel arms the delete, mirroring the reference
        # window gate (Sandbox.enforces_permission_windows).
        monkeypatch.setenv(IN_CONTAINER_ENV, "0")
        task_yaml = tmp_path / "task.yaml"
        task_yaml.write_text("task_id: x\n", encoding="utf-8")

        _scrub_staged_task_yaml(task_yaml)

        assert task_yaml.exists()

    def test_missing_file_does_not_raise(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(IN_CONTAINER_ENV, "1")
        task_yaml = tmp_path / "does-not-exist.yaml"

        # missing_ok=True: a re-entrant call must not crash on an absent file.
        _scrub_staged_task_yaml(task_yaml)

        assert not task_yaml.exists()
