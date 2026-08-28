"""Tests for reference directory resolution and staging."""

import os
import stat
import sys

import pytest

from coder_eval.models import (
    FileExistsCriterion,
    ReferenceSource,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.evaluation import resolve_reference_dir, stage_reference_dir


def _task(reference=None):
    return TaskDefinition(
        task_id="test",
        description="Test task",
        initial_prompt="Do something",
        agent=parse_agent_config(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="test.py", description="exists")],
        reference=reference,
    )


def test_resolve_missing_directory_raises(tmp_path):
    """A typo'd reference path must fail loudly, not silently grade without it."""
    task_file = tmp_path / "task.yaml"
    task_file.write_text("# task", encoding="utf-8")
    task = _task(ReferenceSource(directory="does_not_exist/"))

    with pytest.raises(FileNotFoundError, match="Reference directory not found"):
        resolve_reference_dir(task, task_file)


def test_resolve_rejects_a_file(tmp_path):
    """`reference.directory` pointing at a FILE is the classic migration mistake."""
    task_file = tmp_path / "task.yaml"
    task_file.write_text("# task", encoding="utf-8")
    (tmp_path / "solution.py").write_text("x = 1", encoding="utf-8")
    task = _task(ReferenceSource(directory="solution.py"))

    with pytest.raises(FileNotFoundError, match="must name a directory"):
        resolve_reference_dir(task, task_file)


def test_resolve_existing_directory(tmp_path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text("# task", encoding="utf-8")
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "solution.py").write_text("x = 1", encoding="utf-8")

    resolved = resolve_reference_dir(_task(ReferenceSource(directory="reference")), task_file)
    assert resolved == ref.resolve()


def test_resolve_no_reference_returns_none(tmp_path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text("# task", encoding="utf-8")
    assert resolve_reference_dir(_task(None), task_file) is None


def test_resolve_without_task_file_raises():
    task = _task(ReferenceSource(directory="reference/"))
    with pytest.raises(ValueError, match="task_file not set"):
        resolve_reference_dir(task, None)


def test_stage_copies_contents(tmp_path):
    source = tmp_path / "reference"
    (source / "nested").mkdir(parents=True)
    (source / "solution.py").write_text("x = 1", encoding="utf-8")
    (source / "nested" / "helper.py").write_text("y = 2", encoding="utf-8")

    staged = stage_reference_dir(source, tmp_path / "staged" / "reference")

    assert (staged / "solution.py").read_text(encoding="utf-8") == "x = 1"
    assert (staged / "nested" / "helper.py").read_text(encoding="utf-8") == "y = 2"


def test_stage_does_not_follow_symlinks(tmp_path):
    """A reference shipping `creds -> ~/.aws/credentials` must not pull host files
    into a directory a judge sub-agent can read."""
    secret = tmp_path / "host_secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    source = tmp_path / "reference"
    source.mkdir()
    (source / "solution.py").write_text("x = 1", encoding="utf-8")
    (source / "creds").symlink_to(secret)

    staged = stage_reference_dir(source, tmp_path / "staged" / "reference")

    assert (staged / "solution.py").exists()
    assert not (staged / "creds").exists()


def test_stage_clears_a_reused_destination(tmp_path):
    """A reused --run-dir must not blend a previous run's reference into this one."""
    source = tmp_path / "reference"
    source.mkdir()
    (source / "new.py").write_text("new", encoding="utf-8")

    destination = tmp_path / "staged" / "reference"
    destination.mkdir(parents=True)
    (destination / "stale.py").write_text("stale", encoding="utf-8")

    staged = stage_reference_dir(source, destination)

    assert (staged / "new.py").exists()
    assert not (staged / "stale.py").exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="host-side POSIX mode semantics; the window runs in a Linux container on every host OS",
)
def test_stage_result_is_writable_so_it_can_be_chmodded(tmp_path):
    """The anti-cheat window chmods the staged copy, so it must not be read-only.

    Under driver: docker the source is a `:ro` bind mount whose mode cannot be
    changed (EROFS) — staging through a writable copy is what makes the mode-000
    window possible at all.
    """
    source = tmp_path / "reference"
    source.mkdir()
    (source / "solution.py").write_text("x = 1", encoding="utf-8")

    staged = stage_reference_dir(source, tmp_path / "staged" / "reference")
    original = stat.S_IMODE(staged.stat().st_mode)
    os.chmod(staged, 0o000)
    try:
        assert stat.S_IMODE(staged.stat().st_mode) == 0o000
    finally:
        os.chmod(staged, original)
