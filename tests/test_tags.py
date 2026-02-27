"""Tests for task tagging and tag-based filtering."""

from pathlib import Path

import pytest
import yaml

from coder_eval.models import TaskDefinition
from coder_eval.orchestration.batch import filter_tasks_by_tags


def _make_task(task_id: str, tags: list[str]) -> TaskDefinition:
    """Create a minimal TaskDefinition with given tags."""
    return TaskDefinition(
        task_id=task_id,
        description=f"Test task {task_id}",
        initial_prompt="Do something",
        tags=tags,
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "test.py", "description": "File exists"}],
    )


class TestTagValidation:
    """Tests for tag validation on TaskDefinition."""

    def test_valid_tags(self):
        task = _make_task("t1", ["smoke", "golden", "uipath-python"])
        assert task.tags == ["smoke", "golden", "uipath-python"]

    def test_empty_tags_default(self):
        task = _make_task("t1", [])
        assert task.tags == []

    def test_invalid_tag_uppercase(self):
        with pytest.raises(ValueError, match="lowercase kebab-case"):
            _make_task("t1", ["Smoke"])

    def test_invalid_tag_spaces(self):
        with pytest.raises(ValueError, match="lowercase kebab-case"):
            _make_task("t1", ["my tag"])

    def test_invalid_tag_underscores(self):
        with pytest.raises(ValueError, match="lowercase kebab-case"):
            _make_task("t1", ["my_tag"])

    def test_valid_tag_with_numbers(self):
        task = _make_task("t1", ["python3", "v2-test"])
        assert task.tags == ["python3", "v2-test"]


class TestFilterTasksByTags:
    """Tests for filter_tasks_by_tags function."""

    @pytest.fixture()
    def sample_tasks(self) -> list[tuple[Path, TaskDefinition]]:
        return [
            (Path("smoke1.yaml"), _make_task("smoke1", ["smoke", "basic"])),
            (Path("golden1.yaml"), _make_task("golden1", ["golden", "uipath-python"])),
            (Path("example1.yaml"), _make_task("example1", ["example", "basic"])),
            (Path("integration1.yaml"), _make_task("integration1", ["integration", "network"])),
            (Path("untagged.yaml"), _make_task("untagged", [])),
        ]

    def test_no_filters_returns_all(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks)
        assert len(result) == 5

    def test_include_single_tag(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, include_tags={"smoke"})
        assert [t.task_id for _, t in result] == ["smoke1"]

    def test_include_multiple_tags_or_logic(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, include_tags={"smoke", "golden"})
        ids = {t.task_id for _, t in result}
        assert ids == {"smoke1", "golden1"}

    def test_exclude_single_tag(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, exclude_tags={"example"})
        ids = {t.task_id for _, t in result}
        assert "example1" not in ids
        assert len(result) == 4

    def test_include_and_exclude_combined(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, include_tags={"basic"}, exclude_tags={"example"})
        assert [t.task_id for _, t in result] == ["smoke1"]

    def test_include_nonexistent_tag_returns_empty(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, include_tags={"nonexistent"})
        assert result == []

    def test_exclude_nonexistent_tag_returns_all(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, exclude_tags={"nonexistent"})
        assert len(result) == 5

    def test_untagged_tasks_excluded_by_include_filter(self, sample_tasks):
        result = filter_tasks_by_tags(sample_tasks, include_tags={"smoke"})
        ids = {t.task_id for _, t in result}
        assert "untagged" not in ids


class TestYamlTasksHaveTags:
    """Verify all YAML task files have tags defined."""

    def test_all_tasks_have_tags(self):
        tasks_dir = Path("tasks")
        if not tasks_dir.exists():
            pytest.skip("tasks/ directory not found")

        for task_file in sorted(tasks_dir.glob("*.yaml")):
            with open(task_file) as f:
                data = yaml.safe_load(f)
            task = TaskDefinition(**data)
            assert task.tags, f"{task_file.name} should have at least one tag"
