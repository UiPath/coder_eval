"""Tests for bugs found during code review (2026-04-01).

Each test reproduces a specific bug. The test should FAIL before the fix
and PASS after.
"""

from pathlib import Path

from tests._path_helpers import tmp_subdir


class TestProjectRootResolution:
    """Bug: _PROJECT_ROOT in run_helpers.py uses 3 parent levels instead of 4.

    The file is at src/coder_eval/cli/run_helpers.py so we need 4 parents
    to reach the project root (where tasks/ and experiments/ live).
    run_command.py correctly uses 4 parents, but run_helpers.py only uses 3.
    """

    def test_project_root_resolves_to_actual_project_root(self):
        """DEFAULT_TASKS_DIR should point to a real tasks/ directory."""
        from coder_eval.cli.run_helpers import _PROJECT_ROOT

        # The project root should contain pyproject.toml
        assert (_PROJECT_ROOT / "pyproject.toml").exists(), (
            f"_PROJECT_ROOT resolved to {_PROJECT_ROOT} which doesn't contain pyproject.toml. "
            f"Expected project root with tasks/ and pyproject.toml."
        )

    def test_default_tasks_dir_points_to_tasks(self):
        """DEFAULT_TASKS_DIR should resolve to a directory that exists."""
        from coder_eval.cli.run_helpers import DEFAULT_TASKS_DIR

        assert DEFAULT_TASKS_DIR.exists(), f"DEFAULT_TASKS_DIR resolved to {DEFAULT_TASKS_DIR} which doesn't exist."

    def test_run_helpers_and_run_command_agree_on_project_root(self):
        """run_helpers._PROJECT_ROOT should match run_command's resolution."""
        from coder_eval.cli.run_helpers import _PROJECT_ROOT

        # run_command.py uses 4 parents (the correct count)
        run_command_file = Path(__file__).resolve().parent.parent / "src" / "coder_eval" / "cli" / "run_command.py"
        if run_command_file.exists():
            # Both should resolve to the same project root
            expected_root = run_command_file.resolve().parent.parent.parent.parent
            assert expected_root == _PROJECT_ROOT, (
                f"run_helpers._PROJECT_ROOT={_PROJECT_ROOT} != expected={expected_root}"
            )


class TestPreserveSandboxDefault:
    """BatchRunConfig.preservation_mode and the CLI must share the same default.

    Both default to None (auto / driver-derived), so programmatic and CLI usage
    resolve the mode identically at the batch dispatch seam.
    """

    def test_batch_run_config_default_matches_cli(self):
        """BatchRunConfig.preservation_mode default should match CLI --preservation-mode default (both None)."""
        from coder_eval.orchestration.config import BatchRunConfig

        config = BatchRunConfig(run_dir=tmp_subdir("test"))
        assert config.preservation_mode is None, (
            f"BatchRunConfig.preservation_mode defaults to {config.preservation_mode}, "
            f"but the CLI --preservation-mode default is None (auto)"
        )
