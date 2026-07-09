"""Tests for optional sandbox configuration with tempdir default.

When a TaskDefinition is created without a sandbox block, the validator
automatically sets sandbox to a default SandboxConfig with driver='tempdir'.
"""

from __future__ import annotations

from coder_eval.models import (
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)


def test_sandbox_optional_defaults_to_tempdir() -> None:
    """When sandbox is omitted, it defaults to tempdir driver."""
    task = TaskDefinition(
        task_id="test_task",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    assert task.sandbox is not None
    assert task.sandbox.driver == "tempdir"


def test_sandbox_explicit_tempdir() -> None:
    """When sandbox is explicitly set to tempdir, it is preserved."""
    task = TaskDefinition(
        task_id="test_task",
        description="test",
        initial_prompt="do something",
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    assert task.sandbox is not None
    assert task.sandbox.driver == "tempdir"


def test_sandbox_explicit_docker() -> None:
    """When sandbox is explicitly set to docker, it is preserved."""
    task = TaskDefinition(
        task_id="test_task",
        description="test",
        initial_prompt="do something",
        sandbox=SandboxConfig(driver="docker"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    assert task.sandbox is not None
    assert task.sandbox.driver == "docker"


def test_sandbox_default_factory_creates_instance() -> None:
    """Sandbox field uses default_factory to create a new instance per task."""
    task1 = TaskDefinition(
        task_id="test_task_1",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    task2 = TaskDefinition(
        task_id="test_task_2",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    # Both should have sandbox set and they should be different instances
    assert task1.sandbox is not None
    assert task2.sandbox is not None
    assert task1.sandbox is not task2.sandbox


def test_sandbox_default_has_python_env() -> None:
    """The default sandbox includes standard Python environment."""
    task = TaskDefinition(
        task_id="test_task",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    assert task.sandbox is not None
    assert task.sandbox.python is not None


def test_sandbox_default_respects_config() -> None:
    """The default sandbox has default resource limits."""
    task = TaskDefinition(
        task_id="test_task",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    assert task.sandbox is not None
    assert task.sandbox.limits is not None
    assert task.sandbox.limits.timeout == 300  # default timeout


def test_sandbox_custom_config_preserved() -> None:
    """When sandbox is explicitly configured, custom settings are preserved."""
    custom_sandbox = SandboxConfig(
        driver="docker",
    )
    task = TaskDefinition(
        task_id="test_task",
        description="test",
        initial_prompt="do something",
        sandbox=custom_sandbox,
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    assert task.sandbox is not None
    assert task.sandbox.driver == "docker"


def test_multiple_tasks_get_independent_defaults() -> None:
    """Each task gets its own default sandbox instance."""
    task1 = TaskDefinition(
        task_id="task1",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    task2 = TaskDefinition(
        task_id="task2",
        description="test",
        initial_prompt="do something",
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )
    # Both should have sandbox set, but they should be different instances
    assert task1.sandbox is not None
    assert task2.sandbox is not None
    assert task1.sandbox is not task2.sandbox
