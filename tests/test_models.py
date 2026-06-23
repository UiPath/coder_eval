"""Tests for the data models."""

import tempfile
from pathlib import Path

import pytest
import yaml

from coder_eval.models import RepoSource, SandboxConfig, TaskDefinition, TemplateDirSource


def test_load_hello_date_task():
    """Test that the hello_date.yaml task can be loaded."""
    task_file = Path("tasks/hello_date.yaml")
    assert task_file.exists(), "Task file should exist"

    with open(task_file) as f:
        task_data = yaml.safe_load(f)

    # This will raise an error if validation fails
    task = TaskDefinition(**task_data)

    # Basic assertions
    assert task.task_id == "hello_date_smoke_test"
    assert task.agent.type == "claude-code"
    assert task.sandbox.driver == "tempdir"
    assert len(task.success_criteria) == 3


def test_success_criterion_discriminated_union():
    """Test that success criteria are properly discriminated."""
    from coder_eval.models import (
        FileContainsCriterion,
        FileExistsCriterion,
        RunCommandCriterion,
    )

    # Test file_exists
    criterion = FileExistsCriterion(path="test.py", description="Test file")
    assert criterion.type == "file_exists"

    # Test file_contains
    criterion = FileContainsCriterion(path="test.py", includes=["import"], description="Test file")
    assert criterion.type == "file_contains"

    # Test run_command
    criterion = RunCommandCriterion(command="python test.py", description="Run test")
    assert criterion.type == "run_command"


def test_base_type_field_flows_through_aggregate():
    """The declared base `type` field is the source of criterion_type in aggregate()."""
    from coder_eval.criteria import CriterionRegistry, init_criteria
    from coder_eval.models import CriterionResult, FileExistsCriterion, JsonCheckCriterion

    init_criteria()
    for criterion in (
        FileExistsCriterion(path="a.py", description="a"),
        JsonCheckCriterion(path="b.json", description="b"),
    ):
        checker = CriterionRegistry.get_checker(criterion.type)()
        row = CriterionResult(criterion_type=criterion.type, description="row", score=1.0, passed=True)
        agg = checker.aggregate(criterion, [row])
        assert agg is not None
        assert agg.criterion_type == criterion.type


def test_criterion_union_round_trips_via_type_tag():
    """A dict with type='json_check' resolves to JsonCheckCriterion; a bogus tag is rejected."""
    from pydantic import TypeAdapter, ValidationError

    from coder_eval.models import JsonCheckCriterion, SuccessCriterion

    adapter = TypeAdapter(SuccessCriterion)
    parsed = adapter.validate_python({"type": "json_check", "path": "out.json", "description": "check json"})
    assert isinstance(parsed, JsonCheckCriterion)
    assert parsed.type == "json_check"

    # A typo'd/unknown tag must not silently coerce to the first union member.
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "not_a_real_criterion", "description": "x"})


def test_command_not_executed_alias_normalizes_before_union_dispatch():
    """Legacy negative command assertions normalize before union dispatch."""
    from coder_eval.models import CommandExecutedCriterion

    td = TaskDefinition(
        task_id="legacy_command_not_executed",
        description="d",
        initial_prompt="p",
        agent=None,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            {
                "type": "command_not_executed",
                "description": "Do not call the retired command",
                "tool_name": "Bash",
                "command_pattern": r"uip\s+codedagent\s+new\b",
            },
        ],
    )

    legacy = td.success_criteria[0]
    assert isinstance(legacy, CommandExecutedCriterion)
    assert legacy.type == "command_executed"
    assert legacy.min_count == 0
    assert legacy.max_count == 0


class TestLLMJudgeCriterion:
    """Tests for the new llm_judge success criterion model."""

    def test_llm_judge_criterion_defaults(self):
        """Constructing with the minimum required fields yields documented defaults."""
        from coder_eval.models import DEFAULT_GATEWAY_MODEL, LLMJudgeCriterion

        criterion = LLMJudgeCriterion(description="x", prompt="grade this code")
        assert criterion.type == "llm_judge"
        assert criterion.model == DEFAULT_GATEWAY_MODEL
        assert criterion.temperature == 0.0
        # Bumped from 1000 → 2000 when verbose verdict (findings) was added,
        # so output budgets fit the bullet evidence a typical judge emits.
        assert criterion.max_tokens == 2000
        assert criterion.max_file_chars == 20_000
        assert criterion.files == []
        assert criterion.include_reference is True  # opt-out: judge sees reference by default
        assert criterion.include_agent_output is False
        assert criterion.include_tool_calls is False
        assert criterion.capture_transcript is True
        assert criterion.max_transcript_chars == 100_000
        assert criterion.enabled is True
        assert criterion.pass_threshold == 0.7

    def test_llm_judge_criterion_requires_prompt(self):
        """prompt is required — Pydantic raises ValidationError when missing."""
        from pydantic import ValidationError

        from coder_eval.models import LLMJudgeCriterion

        with pytest.raises(ValidationError):
            LLMJudgeCriterion(description="x")  # type: ignore[call-arg]

    def test_llm_judge_criterion_temperature_bounds(self):
        """temperature is bounded to [0.0, 2.0]."""
        from pydantic import ValidationError

        from coder_eval.models import LLMJudgeCriterion

        with pytest.raises(ValidationError):
            LLMJudgeCriterion(description="x", prompt="p", temperature=-0.1)
        with pytest.raises(ValidationError):
            LLMJudgeCriterion(description="x", prompt="p", temperature=2.1)

    def test_llm_judge_criterion_max_tokens_positive(self):
        """max_tokens must be > 0."""
        from pydantic import ValidationError

        from coder_eval.models import LLMJudgeCriterion

        with pytest.raises(ValidationError):
            LLMJudgeCriterion(description="x", prompt="p", max_tokens=0)

    def test_llm_judge_criterion_max_file_chars_positive(self):
        """max_file_chars must be > 0."""
        from pydantic import ValidationError

        from coder_eval.models import LLMJudgeCriterion

        with pytest.raises(ValidationError):
            LLMJudgeCriterion(description="x", prompt="p", max_file_chars=0)

    def test_llm_judge_criterion_in_task_definition(self):
        """A YAML-style dict with type='llm_judge' resolves to LLMJudgeCriterion via the discriminated union."""
        from coder_eval.models import LLMJudgeCriterion

        td = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            agent=None,
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[
                {"type": "llm_judge", "description": "grade output", "prompt": "rubric..."},
            ],
        )
        assert isinstance(td.success_criteria[0], LLMJudgeCriterion)

    def test_default_gateway_model_moved(self):
        """DEFAULT_GATEWAY_MODEL is importable both from its new home and via coder_eval.models."""
        from coder_eval import models as models_pkg
        from coder_eval.models import gateway as gateway_mod

        assert models_pkg.DEFAULT_GATEWAY_MODEL == gateway_mod.DEFAULT_GATEWAY_MODEL


class TestAgentConfig:
    """Tests for AgentConfig fields."""

    def test_invalid_permission_mode_assignment_rejected(self):
        """Test that assigning invalid permission_mode via attribute raises ValidationError."""
        from pydantic import ValidationError

        from coder_eval.models import AgentKind, parse_agent_config

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        with pytest.raises(ValidationError):
            config.permission_mode = "foobar"

    def test_valid_permission_mode_assignment_accepted(self):
        """Test that assigning valid permission_mode via attribute works."""
        from coder_eval.models import AgentKind, parse_agent_config

        config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="default")
        config.permission_mode = "bypassPermissions"
        assert config.permission_mode == "bypassPermissions"


class TestConfigLineageModels:
    """Tests for ConfigLineageEntry and TaskConfigRecord serialization."""

    def test_config_lineage_entry_roundtrip(self):
        from coder_eval.models import ConfigLineageEntry

        entry = ConfigLineageEntry(value="claude-code", source="default")
        data = entry.model_dump()
        restored = ConfigLineageEntry(**data)
        assert restored.value == "claude-code"
        assert restored.source == "default"
        assert restored.source_detail is None

    def test_config_lineage_entry_with_detail(self):
        from coder_eval.models import ConfigLineageEntry

        entry = ConfigLineageEntry(value="opus", source="cli", source_detail="--model")
        data = entry.model_dump()
        assert data["source_detail"] == "--model"
        restored = ConfigLineageEntry(**data)
        assert restored.source_detail == "--model"

    def test_task_config_record_roundtrip(self):
        from coder_eval.models import ConfigLineageEntry, TaskConfigRecord

        record = TaskConfigRecord(
            resolved={"task_id": "test", "agent": {"type": "claude-code"}},
            source_yaml="task_id: test\n",
            source_file="tasks/test.yaml",
            lineage={
                "agent.type": ConfigLineageEntry(value="claude-code", source="default"),
                "agent.model": ConfigLineageEntry(value="opus", source="cli", source_detail="--model"),
            },
        )
        json_str = record.model_dump_json()
        restored = TaskConfigRecord.model_validate_json(json_str)
        assert restored.resolved["task_id"] == "test"
        assert restored.source_yaml == "task_id: test\n"
        assert restored.source_file == "tasks/test.yaml"
        assert restored.lineage["agent.type"].source == "default"
        assert restored.lineage["agent.model"].source_detail == "--model"

    def test_task_config_record_empty_lineage(self):
        from coder_eval.models import TaskConfigRecord

        record = TaskConfigRecord(
            resolved={"task_id": "test"},
            source_yaml="task_id: test\n",
            source_file="tasks/test.yaml",
        )
        assert record.lineage == {}

    def test_evaluation_result_with_task_config(self):
        from datetime import datetime

        from coder_eval.models import ConfigLineageEntry, EvaluationResult, TaskConfigRecord

        record = TaskConfigRecord(
            resolved={"task_id": "test"},
            source_yaml="task_id: test\n",
            source_file="tasks/test.yaml",
            lineage={"agent.type": ConfigLineageEntry(value="claude-code", source="default")},
        )
        result = EvaluationResult(
            task_id="test",
            task_description="Test",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            environment_info={},
            task_config=record,
        )
        data = result.model_dump()
        assert data["task_config"]["resolved"]["task_id"] == "test"
        assert data["task_config"]["lineage"]["agent.type"]["source"] == "default"

    def test_evaluation_result_task_config_default_none(self):
        from datetime import datetime

        from coder_eval.models import EvaluationResult

        result = EvaluationResult(
            task_id="test",
            task_description="Test",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            environment_info={},
        )
        assert result.task_config is None


class TestCriterionResultDiscriminator:
    """JSON round-trip must preserve ClassificationCriterionResult subclass."""

    def test_legacy_task_json_without_result_kind_migrates(self):
        """Historical task.json files (no `result_kind` tag) must still rehydrate as the right subclass."""
        import json
        from datetime import datetime

        from coder_eval.models import ClassificationCriterionResult, CriterionResult, EvaluationResult

        # Simulate a pre-discriminator task.json: a classification result
        # carries label fields but no result_kind, and a plain result has
        # neither. Without the migration validator, both would resolve to
        # base CriterionResult and the labels would be dropped.
        legacy = {
            "task_id": "t",
            "task_description": "d",
            "variant_id": "default",
            "agent_type": "claude-code",
            "started_at": datetime.now().isoformat(),
            "final_status": "SUCCESS",
            "iteration_count": 1,
            "environment_info": {},
            "success_criteria_results": [
                {"criterion_type": "file_exists", "description": "x", "score": 1.0},
                {
                    "criterion_type": "classification_match",
                    "description": "y",
                    "score": 1.0,
                    "observed_label": "POS",
                    "expected_label": "POS",
                },
            ],
        }
        result = EvaluationResult.model_validate_json(json.dumps(legacy))
        kinds = [type(r).__name__ for r in result.success_criteria_results]
        assert kinds == ["CriterionResult", "ClassificationCriterionResult"]
        assert isinstance(result.success_criteria_results[0], CriterionResult)
        cls_result = result.success_criteria_results[1]
        assert isinstance(cls_result, ClassificationCriterionResult)
        assert cls_result.observed_label == "POS"
        assert cls_result.expected_label == "POS"

    def test_mixed_results_roundtrip_preserves_subclass(self):
        from datetime import datetime

        from coder_eval.models import (
            ClassificationCriterionResult,
            CriterionResult,
            EvaluationResult,
        )

        result = EvaluationResult(
            task_id="t",
            task_description="d",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            environment_info={},
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="x", score=1.0),
                ClassificationCriterionResult(
                    criterion_type="classification_match",
                    description="y",
                    score=1.0,
                    observed_label="A",
                    expected_label="A",
                ),
            ],
        )
        roundtripped = EvaluationResult.model_validate_json(result.model_dump_json())
        kinds = [type(r).__name__ for r in roundtripped.success_criteria_results]
        assert kinds == ["CriterionResult", "ClassificationCriterionResult"]
        cls_result = roundtripped.success_criteria_results[1]
        assert isinstance(cls_result, ClassificationCriterionResult)
        assert cls_result.observed_label == "A"
        assert cls_result.expected_label == "A"


class TestSandboxConfigValidation:
    """Tests for SandboxConfig validation logic."""

    def test_multiple_repo_sources_rejected(self):
        """Test that multiple RepoSource entries are rejected."""
        with pytest.raises(ValueError, match="Only one RepoSource is allowed"):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    RepoSource(url="https://github.com/user/repo1.git"),
                    RepoSource(url="https://github.com/user/repo2.git"),
                ],
            )

    def test_multiple_repo_sources_with_other_templates_rejected(self):
        """Test that multiple RepoSource entries are rejected even with other sources."""
        with tempfile.TemporaryDirectory() as tmpdir, pytest.raises(ValueError, match="Only one RepoSource is allowed"):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    RepoSource(url="https://github.com/user/repo1.git"),
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                    RepoSource(url="https://github.com/user/repo2.git"),
                ],
            )

    def test_single_repo_source_first_is_valid(self):
        """Test that a single RepoSource as the first element is valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = SandboxConfig(
                driver="tempdir",
                template_sources=[
                    RepoSource(url="https://github.com/user/repo.git"),
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                ],
            )
            # Should not raise
            assert len(config.template_sources) == 2
            assert isinstance(config.template_sources[0], RepoSource)

    def test_repo_source_not_first_rejected(self):
        """Test that RepoSource must be the first element."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(ValueError, match="RepoSource must be the first element"),
        ):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    TemplateDirSource(path=str(Path(tmpdir) / "template")),
                    RepoSource(url="https://github.com/user/repo.git"),
                ],
            )


class TestTemplateDirSourceMountPoint:
    """Tests for TemplateDirSource.mount_point validation."""

    def test_default_mount_point(self):
        src = TemplateDirSource(path=str(Path(tempfile.gettempdir(), "x")))
        assert src.mount_point == "."

    def test_relative_mount_point_accepted(self):
        src = TemplateDirSource(path=str(Path(tempfile.gettempdir(), "x")), mount_point="a/b")
        assert src.mount_point == "a/b"

    def test_absolute_mount_point_rejected(self):
        with pytest.raises(ValueError, match="must be a relative path"):
            TemplateDirSource(path=str(Path(tempfile.gettempdir(), "x")), mount_point="/abs/path")

    def test_dotdot_mount_point_rejected(self):
        with pytest.raises(ValueError, match=r"must not contain '\.\.'"):
            TemplateDirSource(path=str(Path(tempfile.gettempdir(), "x")), mount_point="../escape")

    def test_empty_mount_point_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            TemplateDirSource(path=str(Path(tempfile.gettempdir(), "x")), mount_point="")
