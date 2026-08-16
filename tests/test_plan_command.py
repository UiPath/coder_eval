"""Tests for the plan command with experiment awareness."""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from coder_eval.cli.plan_command import plan_command
from coder_eval.models import (
    AgentConfig,
    ExperimentDefinition,
    ExperimentVariant,
    RunLimits,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.models.enums import AgentKind


# Since plan_command uses lazy imports from orchestration.experiment,
# patches must target the source module.
_EXP = "coder_eval.orchestration.experiment"


def _make_task(task_id: str = "test-task", agent: AgentConfig | None = None) -> TaskDefinition:
    """Create a minimal TaskDefinition for testing."""
    return TaskDefinition(
        task_id=task_id,
        description="A test task",
        initial_prompt="Do something",
        agent=agent,
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "description": "check", "path": "out.txt"}],
    )


def _make_experiment(
    experiment_id: str = "test-exp",
    description: str = "Test experiment",
    variants: list[ExperimentVariant] | None = None,
) -> ExperimentDefinition:
    """Create a minimal ExperimentDefinition for testing."""
    if variants is None:
        variants = [
            ExperimentVariant(variant_id="sonnet", agent={"type": "claude-code", "model": "sonnet-4"}),
            ExperimentVariant(variant_id="opus", agent={"type": "claude-code", "model": "opus-4"}),
        ]
    return ExperimentDefinition(
        experiment_id=experiment_id,
        description=description,
        variants=variants,
    )


class TestPlanCommandAgentNone:
    """Tests for handling optional agent field."""

    def test_plan_shows_na_when_agent_is_none(self, tmp_path: Path) -> None:
        """When task.agent is None, plan should show 'N/A' instead of crashing."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        resolved_task = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved_task, {}, 1)),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = [str(call) for call in mock_console.print.call_args_list]
        agent_lines = [p for p in printed if "Agent:" in p]
        assert len(agent_lines) == 1
        assert "N/A (resolved from experiment)" in agent_lines[0]

    def test_plan_shows_agent_type_when_present(self, tmp_path: Path) -> None:
        """When task.agent is set, plan should show the agent type."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        task = _make_task(agent=agent)
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        resolved_task = _make_task(agent=agent)

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved_task, {}, 1)),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = [str(call) for call in mock_console.print.call_args_list]
        agent_lines = [p for p in printed if "Agent:" in p]
        assert len(agent_lines) == 1
        assert "claude-code" in agent_lines[0]

    def test_plan_shows_deferred_when_agent_type_is_none(self, tmp_path: Path) -> None:
        """Phase 3: agent.type may be None on the task; plan must not crash on `.value`."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        # AgentConfig with model only — type deferred to experiment / --type.
        agent = parse_agent_config(model="claude-opus-4-7")
        task = _make_task(agent=agent)
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        resolved_task = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="claude-opus-4-7"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved_task, {}, 1)),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = [str(call) for call in mock_console.print.call_args_list]
        deferred_lines = [p for p in printed if "deferred" in p]
        assert len(deferred_lines) == 1


class TestPlanCommandExperiment:
    """Tests for experiment-aware plan output."""

    def test_plan_with_experiment_flag_shows_experiment_info(self, tmp_path: Path) -> None:
        """When --experiment is provided, plan should show experiment details."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        exp_file = tmp_path / "experiment.yaml"
        exp_file.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment()

        resolved_task = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="sonnet-4"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved_task, {}, 1)),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", exp_file),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file], experiment=exp_file)

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "test-exp" in printed
        assert "Test experiment" in printed
        assert "sonnet" in printed
        assert "opus" in printed

    def test_plan_with_experiment_shows_resolved_agent_per_variant(self, tmp_path: Path) -> None:
        """When experiment is provided, plan should show resolved agent for each task x variant."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        exp_file = tmp_path / "experiment.yaml"
        exp_file.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment()

        resolved_sonnet = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="sonnet-4"))
        resolved_opus = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="opus-4"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", side_effect=[(resolved_sonnet, {}, 1), (resolved_opus, {}, 1)]),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", exp_file),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file], experiment=exp_file)

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "sonnet-4" in printed
        assert "opus-4" in printed

    def test_plan_with_default_experiment(self, tmp_path: Path) -> None:
        """When default experiment file exists, plan auto-loads it."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        default_yaml = tmp_path / "default.yaml"
        default_yaml.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment()
        resolved_task = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, model="sonnet-4"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", default_yaml),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved_task, {}, 1)),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "test-exp" in printed

    def test_plan_warns_when_task_timeout_cannot_extend_single_iteration(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        task = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE))
        resolved_task = task.model_copy(update={"run_limits": RunLimits(task_timeout=1500, turn_timeout=1200)})

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved_task, {}, 1)),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "A larger task_timeout cannot extend the agent's single iteration" in printed
        assert "the agent budget is turn_timeout" in printed

    def test_plan_exits_when_default_experiment_missing(self, tmp_path: Path) -> None:
        """When default experiment file is missing and no --experiment given, plan should exit."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        task = _make_task(agent=agent)

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", tmp_path / "nonexistent.yaml"),
            patch(f"{_EXP}.load_experiment", side_effect=FileNotFoundError("not found")),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            with pytest.raises(typer.Exit) as exc_info:
                plan_command(task_files=[task_file])

            assert exc_info.value.exit_code == 1

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "Failed to load experiment" in printed


class TestPlanCommandValidation:
    """Tests for plan command validation errors."""

    def test_plan_reports_invalid_task(self, tmp_path: Path) -> None:
        """Plan should report errors for invalid task files."""
        task_file = tmp_path / "bad_task.yaml"
        task_file.write_text("placeholder")

        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", side_effect=ValueError("Bad schema")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            with pytest.raises(typer.Exit) as exc_info:
                plan_command(task_files=[task_file])

            assert exc_info.value.exit_code == 1

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "Bad schema" in printed

    def test_plan_exits_on_explicit_experiment_load_failure(self, tmp_path: Path) -> None:
        """When --experiment is explicitly provided and fails to load, plan should exit with code 1."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        exp_file = tmp_path / "bad_experiment.yaml"
        exp_file.write_text("placeholder")

        task = _make_task(agent=None)

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", side_effect=ValueError("Bad experiment")),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", tmp_path / "nonexistent.yaml"),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            with pytest.raises(typer.Exit) as exc_info:
                plan_command(task_files=[task_file], experiment=exp_file)

            assert exc_info.value.exit_code == 1

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "Bad experiment" in printed


def _make_dataset_task(
    rows: list[dict],
    *,
    prompt: str = "Do ${row.id}",
    split_field: str = "split",
) -> TaskDefinition:
    """A dataset-backed task over INLINE rows, so the fixtures never touch disk."""
    from coder_eval.models import Dataset

    return TaskDefinition(
        task_id="dataset-task",
        description="A dataset task",
        initial_prompt=prompt,
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "description": "check", "path": "out.txt"}],
        dataset=Dataset(rows=rows, split_field=split_field),
    )


class TestPlanCommandDatasetPreview:
    """`plan` expands datasets, so the pre-spend surface reports what a run will do.

    Without this, the only way to learn a suite's resolved row count — the number every
    cost estimate and every A/B comparison depends on — was to pay for a run.
    """

    def _run(self, task, task_file: Path, **kwargs):
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        resolved = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE))
        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=(task, "mock yaml")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=(resolved, {}, 1)),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            exit_code = 0
            try:
                plan_command(task_files=[task_file], **kwargs)
            except typer.Exit as e:
                exit_code = e.exit_code
        return " ".join(str(call) for call in mock_console.print.call_args_list), exit_code

    def test_plan_prints_dataset_row_counts(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": f"r{i}"} for i in range(4)])
        printed, exit_code = self._run(task, task_file)
        assert exit_code == 0
        assert "4 rows" in printed and "4 selected" in printed

    def test_plan_split_filters_the_previewed_rows(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task(
            [
                {"id": "a", "split": "train"},
                {"id": "b", "split": "train"},
                {"id": "c", "split": "test"},
                {"id": "d", "split": "test"},
            ]
        )
        printed, exit_code = self._run(task, task_file, split="test")
        assert exit_code == 0
        assert "4 rows" in printed and "2 selected" in printed and "--split test" in printed

    def test_plan_reports_an_unmatched_split_as_an_error_and_exits_1(self, tmp_path: Path) -> None:
        # The pre-spend surface this phase exists to provide: learn the selector is wrong
        # for free, rather than by watching a run abort.
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": "a", "split": "train"}, {"id": "b", "split": "test"}])
        printed, exit_code = self._run(task, task_file, split="typo")
        assert exit_code == 1
        assert "'test', 'train'" in printed

    def test_plan_leaves_a_non_dataset_task_unannotated(self, tmp_path: Path) -> None:
        # A "1 row" line on a task with no dataset: block would be noise, and would imply
        # a concept the task does not have.
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        printed, exit_code = self._run(_make_task(), task_file)
        assert exit_code == 0
        assert "Dataset:" not in printed

    def test_plan_leaves_an_unlabelled_dataset_unfiltered_under_split(self, tmp_path: Path) -> None:
        # --split is global to the invocation, so an unlabelled task in a multi-task run
        # must pass through whole — and the line must not imply the selector did anything.
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        printed, exit_code = self._run(task, task_file, split="test")
        assert exit_code == 0
        assert "3 rows" in printed and "3 selected" in printed
        assert "not labelled, all rows kept" in printed

    def test_plan_catches_a_bad_row_substitution(self, tmp_path: Path) -> None:
        # The value the expansion buys beyond a row count: a ${row.*} naming a field no row
        # carries used to fail at run time, per row, after the sandbox was built.
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": "a"}], prompt="Do ${row.missing}")
        printed, exit_code = self._run(task, task_file)
        assert exit_code == 1
        assert "missing" in printed

    def test_plan_warns_on_a_partly_labelled_dataset(self, tmp_path: Path) -> None:
        # A warning, not an error: the run is legitimate, it is just measuring less than
        # the file suggests. Exit stays 0.
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": "a", "split": "train"}, {"id": "b", "split": "train"}, {"id": "c"}])
        printed, exit_code = self._run(task, task_file, split="train")
        assert exit_code == 0
        assert "⚠" in printed and "1 of 3 rows carry no 'split' label" in printed


class TestPlanCommandRowSelectors:
    """`plan` previews what `run` executes, and names WHICH selector narrowed the set.

    The accounting line used to name whichever selector was *set*, so a reduction caused by a
    task's own `dataset.sample_per_stratum` was reported as `(--split X)`. The suffix now comes
    straight from `RowSelectionOutcome.applied`, which records a cause only when it actually
    removed a row — so the preview cannot claim a narrowing that did not happen, and cannot
    attribute one to the wrong selector.
    """

    _run = TestPlanCommandDatasetPreview._run

    @staticmethod
    def _stratified_task(rows, **dataset_kwargs):
        from coder_eval.models import Dataset

        return TaskDefinition(
            task_id="dataset-task",
            description="A dataset task",
            initial_prompt="Do ${row.id}",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "description": "check", "path": "out.txt"}],
            dataset=Dataset(rows=rows, **dataset_kwargs),
        )

    def test_sample_narrows_and_is_named(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": f"r{i}"} for i in range(4)])
        printed, exit_code = self._run(task, task_file, sample=2)
        assert exit_code == 0
        assert "4 rows" in printed and "2 selected" in printed and "--sample 2" in printed

    def test_sample_at_or_above_the_row_count_is_reported_as_removing_no_rows(self, tmp_path: Path) -> None:
        """`--sample 99` over 4 rows is HONOURED and narrows nothing — and the line must say both.

        Naming it as a CAUSE would claim a subset the run does not take. Saying nothing at all is
        the opposite failure, and was a regression: the line became byte-identical to passing no
        selector, so a user could not confirm the flag had been read, and it contradicted
        `run.md`, which records what was requested.
        """
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": f"r{i}"} for i in range(4)])
        printed, exit_code = self._run(task, task_file, sample=99)
        assert exit_code == 0
        assert "4 rows -> 4 selected" in printed
        assert "requested --sample 99; removed no rows" in printed

    def test_no_selector_at_all_prints_no_parenthetical(self, tmp_path: Path) -> None:
        """The genuinely-silent case, so "removed no rows" cannot creep onto every line."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": f"r{i}"} for i in range(4)])
        printed, exit_code = self._run(task, task_file)
        assert exit_code == 0
        assert "4 rows -> 4 selected" in printed
        assert "requested" not in printed and "--sample" not in printed

    def test_a_honoured_split_that_removes_nothing_is_still_reported(self, tmp_path: Path) -> None:
        """The case that regressed: every row is in the selected split."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_dataset_task([{"id": "a", "split": "test"}, {"id": "b", "split": "test"}])
        printed, exit_code = self._run(task, task_file, split="test")
        assert exit_code == 0
        assert "requested --split test; removed no rows" in printed

    def test_sample_per_stratum_narrows_and_is_named(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta"} for i in range(3)]
        task = self._stratified_task(rows, sample_seed=1)
        printed, exit_code = self._run(task, task_file, sample_per_stratum=1)
        assert exit_code == 0
        assert "6 rows -> 2 selected" in printed and "--sample-per-stratum 1" in printed

    def test_a_yaml_sourced_stratified_count_is_not_attributed_to_split(self, tmp_path: Path) -> None:
        """The B2 regression: a task carrying `dataset.sample_per_stratum` with only --split
        passed used to report the stratified reduction as `(--split train)`."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha", "split": "train"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta", "split": "train"} for i in range(3)]
        task = self._stratified_task(rows, sample_per_stratum=1, sample_seed=1)
        printed, exit_code = self._run(task, task_file, split="train")
        assert exit_code == 0
        assert "dataset.sample_per_stratum: 1" in printed
        # --split kept every row, so it must NOT be named as a cause at all. Asserting on the
        # bare flag rather than "(--split train)": the parenthesised form matches only when
        # --split is the SOLE cause, so a regression naming it alongside the stratified cause
        # would render "(--split train, dataset.sample_per_stratum: 1)" and slip through.
        assert "--split" not in printed

    def test_split_and_sample_per_stratum_are_both_named_split_first(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha", "split": "train"} for i in range(2)]
        rows += [{"id": f"b{i}", "expected_skill": "beta", "split": "train"} for i in range(2)]
        rows += [{"id": f"c{i}", "expected_skill": "alpha", "split": "test"} for i in range(2)]
        task = self._stratified_task(rows, sample_seed=1)
        printed, exit_code = self._run(task, task_file, split="train", sample_per_stratum=1)
        assert exit_code == 0
        assert "(--split train, --sample-per-stratum 1)" in printed

    def test_sample_beats_sample_per_stratum_and_only_it_is_named(self, tmp_path: Path) -> None:
        """`--sample` wins; naming both would claim a stratified narrowing that did not run."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta"} for i in range(3)]
        task = self._stratified_task(rows, sample_seed=1)
        printed, exit_code = self._run(task, task_file, sample=2, sample_per_stratum=1)
        assert exit_code == 0
        assert "--sample 2" in printed
        assert "--sample-per-stratum" not in printed

    def test_strata_line_counts_the_selected_rows(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta"} for i in range(2)]
        task = self._stratified_task(rows)
        printed, exit_code = self._run(task, task_file)
        assert exit_code == 0
        assert "strata (expected_skill): alpha=3, beta=2" in printed
        # And DERIVED, not just literal-equal: the rendered counts must equal what
        # `stratum_key` — the sampler's own rule — produces over the selected rows. A
        # preview that invented its own grouping could still match the literal above.
        from collections import Counter

        from coder_eval.orchestration.task_loader import stratum_key

        expected = Counter(stratum_key(r, task.dataset.stratify_field) for r in rows)
        rendered = ", ".join(f"{k}={n}" for k, n in sorted(expected.items()))
        assert f"strata ({task.dataset.stratify_field}): {rendered}" in printed

    def test_rows_missing_the_stratify_field_are_counted_under_none(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": "a", "expected_skill": "alpha"}, {"id": "b"}, {"id": "c"}]
        task = self._stratified_task(rows)
        printed, exit_code = self._run(task, task_file)
        assert exit_code == 0
        assert "(none)=2" in printed and "alpha=1" in printed

    def test_a_single_stratum_prints_no_strata_line(self, tmp_path: Path) -> None:
        """A one-entry breakdown just restates the row count."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = self._stratified_task([{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)])
        printed, exit_code = self._run(task, task_file)
        assert exit_code == 0
        assert "strata (" not in printed

    def test_an_unseeded_stratified_sample_warns_that_rows_are_redrawn(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta"} for i in range(3)]
        task = self._stratified_task(rows)  # no sample_seed
        printed, exit_code = self._run(task, task_file, sample_per_stratum=1)
        assert exit_code == 0
        assert "re-drawn every invocation" in printed

    def test_a_seeded_stratified_sample_does_not_warn(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        rows = [{"id": f"a{i}", "expected_skill": "alpha"} for i in range(3)]
        rows += [{"id": f"b{i}", "expected_skill": "beta"} for i in range(3)]
        task = self._stratified_task(rows, sample_seed=7)
        printed, exit_code = self._run(task, task_file, sample_per_stratum=1)
        assert exit_code == 0
        assert "re-drawn every invocation" not in printed

    def test_a_non_dataset_task_prints_no_selector_lines(self, tmp_path: Path) -> None:
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        task = _make_task(agent=parse_agent_config(type=AgentKind.CLAUDE_CODE))
        printed, exit_code = self._run(task, task_file, split="test", sample=2)
        assert exit_code == 0
        assert "Dataset:" not in printed and "strata (" not in printed
