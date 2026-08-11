"""Tests for task tagging and tag-based filtering."""

import re
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
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", ["Smoke"])

    def test_invalid_tag_spaces(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", ["my tag"])

    def test_invalid_tag_underscores(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", ["my_tag"])

    def test_valid_tag_with_numbers(self):
        task = _make_task("t1", ["python3", "v2-test"])
        assert task.tags == ["python3", "v2-test"]

    def test_valid_namespaced_tag(self):
        task = _make_task("t1", ["lifecycle:generate", "shape:multi-node", "connector:google-tasks"])
        assert task.tags == ["lifecycle:generate", "shape:multi-node", "connector:google-tasks"]

    def test_invalid_tag_leading_colon(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", [":generate"])

    def test_invalid_tag_trailing_colon(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", ["lifecycle:"])

    def test_invalid_tag_double_colon(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", ["a:b:c"])

    def test_invalid_tag_uppercase_in_namespace(self):
        with pytest.raises(ValueError, match="kebab-case"):
            _make_task("t1", ["Lifecycle:generate"])


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

        # Recurse so tasks organized into subdirs (e.g. tasks/agents/,
        # tasks/samples/) are validated too, not just the ones left at root.
        for task_file in sorted(tasks_dir.rglob("*.yaml")):
            with open(task_file) as f:
                data = yaml.safe_load(f)
            task = TaskDefinition(**data)
            assert task.tags, f"{task_file.name} should have at least one tag"


class TestCiSmokePassContract:
    """The CI smoke-pass bucket hardcodes a count and a non-recursive glob.

    Both fail SILENTLY OPEN: a `smoke-pass` task added in a subdirectory is not
    matched by `tasks/*.yaml`, the hardcoded expectation still matches what did
    run, and CI stays green while the task never executes.
    """

    WORKFLOW = Path(".github/workflows/pr-checks.yml")

    def _smoke_pass_tasks(self) -> dict[Path, int]:
        """Map each smoke-pass task file to the number of sub-tasks it expands to."""
        found: dict[Path, int] = {}
        for task_file in sorted(Path("tasks").rglob("*.yaml")):
            with open(task_file) as f:
                task = TaskDefinition(**yaml.safe_load(f))
            if "smoke-pass" not in task.tags:
                continue
            rows = len(task.dataset.rows) if (task.dataset and task.dataset.rows) else 1
            found[task_file] = rows
        return found

    def test_expected_count_matches_the_tagged_task_set(self):
        if not self.WORKFLOW.exists():
            pytest.skip("workflow not present")
        text = self.WORKFLOW.read_text(encoding="utf-8")
        expected = int(re.search(r'EXPECTED_SMOKE_PASS_RUN:\s*"(\d+)"', text).group(1))
        succeeded = int(re.search(r'EXPECTED_SMOKE_PASS_SUCCEEDED:\s*"(\d+)"', text).group(1))
        actual = sum(self._smoke_pass_tasks().values())

        assert actual == expected, (
            f"EXPECTED_SMOKE_PASS_RUN is {expected} but {actual} smoke-pass sub-tasks exist. "
            "Update .github/workflows/pr-checks.yml when adding/removing a smoke-pass task."
        )
        assert succeeded == expected

    def test_every_smoke_pass_task_is_matched_by_the_ci_globs(self):
        if not self.WORKFLOW.exists():
            pytest.skip("workflow not present")
        text = self.WORKFLOW.read_text(encoding="utf-8")
        step = text.split("Run smoke-pass bucket", 1)[1].split("- name:", 1)[0]
        globs = re.findall(r"(tasks/[^\s\\]*\.yaml)", step)
        assert globs, "could not find the smoke-pass globs in the workflow"

        for task_file in self._smoke_pass_tasks():
            assert any(task_file.match(g) for g in globs), (
                f"{task_file} is tagged smoke-pass but no CI glob matches it "
                f"(globs: {globs}) — it would silently never run."
            )
