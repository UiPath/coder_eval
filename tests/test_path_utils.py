"""Tests for path utilities."""

import platform
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.models import ResolvedTask, TaskDefinition
from coder_eval.path_utils import (
    TASK_LOG_FILENAME,
    build_task_run_dir,
    create_latest_symlink,
    format_task_log_id,
    generate_run_id,
    replicate_subdir_name,
    task_log_path,
)
from tests._path_helpers import tmp_subdir


def test_generate_run_id():
    """Test run ID generation format."""
    run_id = generate_run_id()
    # Should match format: YYYY-MM-DD_HH-MM-SS
    assert re.match(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", run_id)


def test_replicate_subdir_name_zero_pads():
    assert replicate_subdir_name(0) == "00"
    assert replicate_subdir_name(5) == "05"
    assert replicate_subdir_name(99) == "99"


def test_build_task_run_dir_default_replicate():
    run_dir = tmp_subdir("runs", "2025-01-01_12-00-00")
    result = build_task_run_dir(run_dir, "default", "hello_world")
    assert result == run_dir / "default" / "hello_world" / "00"


def test_build_task_run_dir_dataset_row_task_id():
    run_dir = tmp_subdir("runs", "2025-01-01_12-00-00")
    result = build_task_run_dir(run_dir, "sonnet", "classify/row-001")
    assert result == run_dir / "sonnet" / "classify" / "row-001" / "00"


def test_build_task_run_dir_custom_replicate_index():
    run_dir = tmp_subdir("runs", "2025-01-01_12-00-00")
    result = build_task_run_dir(run_dir, "v1", "task_a", replicate_index=3)
    assert result == run_dir / "v1" / "task_a" / "03"


def test_task_log_filename_constant():
    assert TASK_LOG_FILENAME == "task.log"


def test_task_log_path_helper(tmp_path: Path):
    assert task_log_path(tmp_path) == tmp_path / TASK_LOG_FILENAME


def test_format_task_log_id_basic():
    assert format_task_log_id("default", "hello_date", 0) == "default/hello_date/00"


def test_format_task_log_id_nonzero_replicate():
    assert format_task_log_id("v1", "task", 7) == "v1/task/07"


def test_format_task_log_id_large_replicate_no_truncation():
    assert format_task_log_id("v", "t", 100) == "v/t/100"


def test_format_task_log_id_matches_build_task_run_dir_relative_path():
    run_dir = tmp_subdir("runs", "X")
    task_dir = build_task_run_dir(run_dir, "v", "t", 3)
    assert format_task_log_id("v", "t", 3) == task_dir.relative_to(run_dir).as_posix()


def test_resolved_task_rejects_negative_replicate_index(tmp_path: Path):
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "out.txt", "description": "f"}],
    )
    with pytest.raises(ValidationError):
        ResolvedTask(
            task=task,
            task_file=tmp_path / "t.yaml",
            run_dir=tmp_path / "run",
            variant_id="v1",
            replicate_index=-1,
        )


def test_create_latest_symlink(tmp_path):
    """Test symlink creation."""
    runs_base = tmp_path / "runs"
    runs_base.mkdir()
    run_dir = runs_base / "2025-01-01_12-00-00"
    run_dir.mkdir()

    create_latest_symlink(runs_base, "2025-01-01_12-00-00")

    latest = runs_base / "latest"
    if platform.system() != "Windows":  # May not work on Windows
        assert latest.is_symlink()
        assert latest.resolve() == run_dir


def test_create_latest_symlink_updates_existing(tmp_path):
    """Test that symlink updates when new run is created."""
    runs_base = tmp_path / "runs"
    runs_base.mkdir()

    # Create first run
    run_dir1 = runs_base / "2025-01-01_12-00-00"
    run_dir1.mkdir()
    create_latest_symlink(runs_base, "2025-01-01_12-00-00")

    # Create second run
    run_dir2 = runs_base / "2025-01-01_13-00-00"
    run_dir2.mkdir()
    create_latest_symlink(runs_base, "2025-01-01_13-00-00")

    latest = runs_base / "latest"
    if platform.system() != "Windows":
        assert latest.is_symlink()
        assert latest.resolve() == run_dir2  # Should point to newer run


class TestDirTemplates:
    """``resolve_dir_template`` — the run layout as data, resolved late.

    Rationale: the logging and artifacts directories are independent, so a caller
    (Harbor) can put them in unrelated parts of the filesystem and no copy is
    needed between them.
    """

    def test_defaults_reproduce_the_historical_layout(self):
        """The whole backward-compatibility claim: an unspecified run must write to
        byte-identical paths, so the defaults are pinned against the literal layout
        rather than against build_task_run_dir (which now calls the resolver)."""
        from coder_eval.path_utils import (
            DEFAULT_ARTIFACTS_DIR_TEMPLATE,
            DEFAULT_LOGGING_DIR_TEMPLATE,
            resolve_dir_template,
        )

        kwargs = {"run_dir": Path("/runs/2026"), "variant_id": "default", "task_id": "greet"}
        assert resolve_dir_template(DEFAULT_LOGGING_DIR_TEMPLATE, **kwargs) == Path("/runs/2026/default/greet/00")
        assert resolve_dir_template(DEFAULT_ARTIFACTS_DIR_TEMPLATE, **kwargs) == Path(
            "/runs/2026/default/greet/00/artifacts/greet"
        )

    def test_build_task_run_dir_agrees_with_the_default_logging_template(self):
        from coder_eval.path_utils import DEFAULT_LOGGING_DIR_TEMPLATE, build_task_run_dir, resolve_dir_template

        for task_id in ("greet", "suite/row-7"):
            for rep in (0, 3):
                assert build_task_run_dir(Path("/r"), "v", task_id, replicate_index=rep) == resolve_dir_template(
                    DEFAULT_LOGGING_DIR_TEMPLATE,
                    run_dir=Path("/r"),
                    variant_id="v",
                    task_id=task_id,
                    replicate_index=rep,
                )

    def test_dataset_task_id_containing_a_separator_nests(self):
        """A dataset-expanded task_id is "<task>/<row>" (task_loader.expand_dataset),
        and those row ids are validated precisely because they become directories."""
        from coder_eval.path_utils import DEFAULT_ARTIFACTS_DIR_TEMPLATE, resolve_dir_template

        assert resolve_dir_template(
            DEFAULT_ARTIFACTS_DIR_TEMPLATE,
            run_dir=Path("/runs/2026"),
            variant_id="default",
            task_id="suite/row-7",
            replicate_index=3,
        ) == Path("/runs/2026/default/suite/row-7/03/artifacts/suite/row-7")

    @pytest.mark.parametrize("static", ["/work", "/work/output", "/logs/agent"])
    def test_a_static_template_is_the_identity_function(self, static):
        """The load-bearing property: an override with no placeholders resolves to
        itself down the SAME code path as the default, so nothing anywhere needs an
        "is this overridden?" branch."""
        from coder_eval.path_utils import resolve_dir_template

        assert resolve_dir_template(
            static, run_dir=Path("/runs/2026"), variant_id="v", task_id="t", replicate_index=5
        ) == Path(static)

    def test_a_windows_run_dir_is_not_mangled_by_backslash_escapes(self):
        """HAZARD: ``re.sub`` interprets backslashes in the REPLACEMENT, which would
        corrupt a Windows run_dir (``C:\\runs\\2026`` -> ``\\r`` etc.). This is why the
        implementation uses ``string.Template``, which inserts values verbatim."""
        from coder_eval.path_utils import resolve_dir_template

        resolved = resolve_dir_template(
            "${run_dir}/${task}", run_dir=Path(r"C:\runs\2026"), variant_id="v", task_id="t"
        )
        assert "runs" in str(resolved) and "2026" in str(resolved)
        assert resolved == Path(r"C:\runs\2026") / "t"

    def test_unknown_placeholder_names_the_valid_ones(self):
        from coder_eval.path_utils import resolve_dir_template

        with pytest.raises(ValueError, match=r"unknown placeholder 'nope'"):
            resolve_dir_template("${nope}/x", run_dir=Path("/r"), variant_id="v", task_id="t")

    def test_malformed_template_is_a_clean_error(self):
        from coder_eval.path_utils import resolve_dir_template

        with pytest.raises(ValueError, match="malformed"):
            resolve_dir_template("${run_dir", run_dir=Path("/r"), variant_id="v", task_id="t")


def _prior_result(sandbox_path: str):
    """A minimal finished-run record, for default_workspace's containment checks."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult

    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="default",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
        sandbox_path=sandbox_path,
    )


class TestDecoupledLayoutConsumers:
    """Regressions from decoupling the artifacts dir from run_dir.

    Each of these silently degraded rather than failing loudly, which is why they
    are pinned here.
    """

    def test_default_workspace_trusts_an_operator_supplied_artifacts_dir(self, tmp_path):
        """`_contained` rejects a recorded sandbox_path outside run_dir -- a real
        security guard, since criteria execute with cwd there. An artifacts dir the
        OPERATOR resolved from their own template is trusted, so widening the roots
        (never relaxing the check) is what makes a decoupled layout regradeable."""
        from coder_eval.orchestration.regrade import default_workspace

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        artifacts = tmp_path / "elsewhere" / "work"
        artifacts.mkdir(parents=True)
        prior = _prior_result(str(artifacts))

        assert default_workspace(run_dir, prior, artifacts_dir=artifacts) == artifacts

    def test_default_workspace_still_refuses_an_untrusted_outside_path(self, tmp_path):
        """The guard must not have been weakened: a recorded sandbox_path outside
        BOTH roots is still refused.

        The artifacts dir deliberately does NOT exist here, so resolution falls
        through to the untrusted sandbox_path -- the path the guard protects. (When
        the artifacts dir DOES exist it is returned directly, since an
        operator-supplied directory outranks anything the record claims.)"""
        from coder_eval.orchestration.regrade import RegradeError, default_workspace

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        artifacts = tmp_path / "elsewhere"  # never created
        rogue = tmp_path / "rogue"
        rogue.mkdir()
        prior = _prior_result(str(rogue))

        with pytest.raises(RegradeError, match="resolves outside"):
            default_workspace(run_dir, prior, artifacts_dir=artifacts)

    def test_aggregate_task_logs_reads_logging_dirs_outside_run_dir(self, tmp_path):
        """The run_dir glob found nothing when the logging dir was elsewhere, writing
        an empty experiment.log with no error."""
        from coder_eval.logging_config import aggregate_task_logs

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        logs = tmp_path / "logs" / "agent"
        logs.mkdir(parents=True)
        (logs / "task.log").write_text("hello from the task\n", encoding="utf-8")

        aggregate_task_logs(run_dir, task_dirs=[logs])

        aggregated = (run_dir / "experiment.log").read_text(encoding="utf-8")
        assert "hello from the task" in aggregated
        assert "No task logs found" not in aggregated

    def test_config_resolvers_are_the_single_chokepoint(self, tmp_path):
        """Orchestrator capture target, --resume clearing, and the regrade lookup all
        go through these, so they cannot disagree about where artifacts live."""
        from coder_eval.orchestration.config import BatchRunConfig

        cfg = BatchRunConfig(run_dir=tmp_path, artifacts_dir_template="/work")
        assert cfg.resolve_artifacts_dir("default", "t") == Path("/work")
        assert cfg.resolve_logging_dir("default", "t") == tmp_path / "default" / "t" / "00"
