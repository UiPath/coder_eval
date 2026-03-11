"""Tests for per-task output file naming."""

from pathlib import Path


class TestTaskFileNames:
    def test_report_path_uses_task_prefix(self):
        """path_utils should return task.json."""
        from coder_eval.path_utils import get_task_report_path

        path = get_task_report_path(Path("/runs/test"), "my-task")
        assert path.name == "task.json"
