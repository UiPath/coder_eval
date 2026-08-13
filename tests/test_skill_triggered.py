"""Tests for SkillTriggeredCriterion + checker."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from coder_eval.criteria.skill_triggered import SkillTriggeredChecker
from coder_eval.models import (
    ClassificationCriterionResult,
    CriterionResult,
    SkillTriggeredCriterion,
)
from coder_eval.models.results import TurnRecord
from coder_eval.models.telemetry import CommandTelemetry


def _cmd(
    tool_name: str,
    parameters: dict[str, Any] | None = None,
    tool_id: str = "t1",
    result_status: str = "success",
    result_summary: str | None = None,
) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        parameters=parameters or {},
        result_status=result_status,  # type: ignore[arg-type]
        result_summary=result_summary,
    )


def _turn(commands: list[CommandTelemetry]) -> TurnRecord:
    return TurnRecord(iteration=1, user_input="p", agent_output="o", commands=commands)


def _check(*, expected_skill: str, skill_name: str, commands: list[CommandTelemetry]) -> ClassificationCriterionResult:
    criterion = SkillTriggeredCriterion(
        description="did agent invoke a skill?",
        expected_skill=expected_skill,
        skill_name=skill_name,
    )
    checker = SkillTriggeredChecker()
    result = checker.check(criterion, sandbox=None, turn_records=[_turn(commands)])  # type: ignore[arg-type]
    assert isinstance(result, ClassificationCriterionResult)
    return result


class TestSkillTriggeredChecker:
    def test_skill_invoked_tp(self) -> None:
        result = _check(
            expected_skill="uipath-flow", skill_name="uipath-flow", commands=[_cmd("Skill", {"skill": "uipath-flow"})]
        )
        assert result.score == 1.0 and result.observed_label == "yes" and result.expected_label == "yes"

    def test_no_skill_tn(self) -> None:
        result = _check(expected_skill="", skill_name="uipath-flow", commands=[_cmd("Read", {"file_path": "x"})])
        assert result.score == 1.0 and result.observed_label == "no" and result.expected_label == "no"

    def test_errored_skill_call_is_not_engagement(self) -> None:
        # A skill carrying `disable-model-invocation: true` cannot be invoked by the model:
        # the Skill tool refuses it, the body is NEVER loaded, and the agent proceeds on its
        # own prior knowledge — producing plausible output that hides what happened.
        #
        # Counting the attempt reported `yes` for a run the skill took no part in. Observed
        # on 24 of 24 rows of a real outcome suite, where it silently turned an entire A/B
        # round into a measurement of the model's background knowledge: all four arms tied
        # exactly, because none of them ever saw the body they differed in.
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[
                _cmd(
                    "Skill",
                    {"skill": "uipath-flow"},
                    result_status="error",
                    result_summary=(
                        "<tool_use_error>Skill uipath-flow cannot be used with Skill tool "
                        "due to disable-model-invocation</tool_use_error>"
                    ),
                )
            ],
        )
        assert result.observed_label == "no", (
            "an errored Skill call counted as engagement — the body never loaded, so the row "
            "measured the model's prior knowledge while reporting the skill had run"
        )
        assert result.score == 0.0

    def test_errored_skill_call_still_counts_when_the_file_was_read(self) -> None:
        # The two signals are not the same thing. A failed Skill CALL loaded nothing; a path
        # reference means the SKILL.md was actually opened, which is genuine engagement (and
        # is how non-Claude agents engage a skill at all). The result_status gate must not
        # suppress that.
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[
                _cmd("Skill", {"skill": "uipath-flow"}, result_status="error"),
                _cmd("Read", {"file_path": "/x/skills/uipath-flow/SKILL.md"}, tool_id="t2"),
            ],
        )
        assert result.observed_label == "yes" and result.score == 1.0

    def test_skill_invoked_fp(self) -> None:
        result = _check(expected_skill="", skill_name="uipath-flow", commands=[_cmd("Skill", {"skill": "uipath-flow"})])
        assert result.score == 0.0 and result.observed_label == "yes"

    def test_no_skill_fn(self) -> None:
        result = _check(
            expected_skill="uipath-flow", skill_name="uipath-flow", commands=[_cmd("Read", {"file_path": "x"})]
        )
        assert result.score == 0.0 and result.observed_label == "no"

    def test_other_tools_dont_count(self) -> None:
        result = _check(
            expected_skill="",
            skill_name="uipath-flow",
            commands=[_cmd("Bash", {"command": "ls"}), _cmd("Read", {"file_path": "x"}), _cmd("Write", {})],
        )
        assert result.score == 1.0 and result.observed_label == "no"

    def test_wrong_skill_name_not_counted(self) -> None:
        # Agent invoked "uipath-rpa", but criterion filters on "uipath-flow" -> observed="no".
        result = _check(expected_skill="", skill_name="uipath-flow", commands=[_cmd("Skill", {"skill": "uipath-rpa"})])
        assert result.observed_label == "no" and result.score == 1.0

    def test_namespaced_skill_param_matches(self) -> None:
        # Agent emits "uipath:uipath-flow" (plugin:skill prefix); strip before comparing.
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[_cmd("Skill", {"skill": "uipath:uipath-flow"})],
        )
        assert result.observed_label == "yes" and result.score == 1.0

    def test_no_turn_records_returns_base_result_with_error(self) -> None:
        criterion = SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow")
        checker = SkillTriggeredChecker()
        result = checker.check(criterion, sandbox=None, turn_records=None)  # type: ignore[arg-type]
        # No turn records -> base CriterionResult with error, NOT a
        # ClassificationCriterionResult. The aggregator skips these rows,
        # keeping the classification math unpolluted.
        assert not isinstance(result, ClassificationCriterionResult)
        assert result.score == 0.0
        assert result.error is not None


class TestSkillTriggeredAnyEngagement:
    """Any-engagement scoring: a skill counts if it was engaged *at all*, in any
    order. The expected skill passes its criterion (recall) even when a wrong
    skill was touched first; an off-target skill still fails its own criterion
    (precision).
    """

    def _admin_platform(self) -> list[CommandTelemetry]:
        # The agent engages uipath-admin FIRST, then uipath-platform.
        return [
            _cmd("Skill", {"skill": "uipath-admin"}, tool_id="s1"),
            _cmd("Skill", {"skill": "uipath-platform"}, tool_id="s2"),
        ]

    def test_expected_skill_engaged_first_is_true_positive(self) -> None:
        # GT=uipath-admin; admin engaged (first) -> observed=yes, expected=yes.
        result = _check(expected_skill="uipath-admin", skill_name="uipath-admin", commands=self._admin_platform())
        assert result.observed_label == "yes" and result.score == 1.0

    def test_off_target_skill_engaged_is_false_positive(self) -> None:
        # Same run scored for the uipath-platform criterion: platform WAS engaged
        # (second), so on an admin row it is a precision miss -> observed=yes,
        # expected=no -> score 0.0. This is the per-skill precision signal.
        result = _check(expected_skill="uipath-admin", skill_name="uipath-platform", commands=self._admin_platform())
        assert result.observed_label == "yes" and result.score == 0.0

    def test_expected_skill_engaged_after_wrong_still_passes(self) -> None:
        # Item 1: the WRONG skill engages first, the expected one later. The GT
        # criterion must still PASS — an earlier wrong touch (comparison, not
        # commitment) does not fail the row.
        commands = [
            _cmd("Skill", {"skill": "uipath-platform"}, tool_id="s1"),
            _cmd("Skill", {"skill": "uipath-admin"}, tool_id="s2"),
        ]
        result = _check(expected_skill="uipath-admin", skill_name="uipath-admin", commands=commands)
        assert result.observed_label == "yes" and result.score == 1.0

    def test_negative_row_fails_on_any_engagement(self) -> None:
        # Negative row (expected_skill == ""): engaging the target skill at all —
        # even after an unrelated one — is a false positive.
        commands = [
            _cmd("Skill", {"skill": "uipath-platform"}, tool_id="s1"),
            _cmd("Skill", {"skill": "uipath-admin"}, tool_id="s2"),
        ]
        result = _check(expected_skill="", skill_name="uipath-admin", commands=commands)
        assert result.observed_label == "yes" and result.score == 0.0

    def test_stacked_recall_and_precision_on_one_trajectory(self) -> None:
        # How the stacked criteria score a single positive row (GT=uipath-admin)
        # on which the agent engaged BOTH the expected skill and an off-target one.
        # The GT criterion credits recall (pass); the off-target criterion records
        # a precision miss (fail). No precision hole: an extra engagement is never
        # silently absorbed — it lands on its own skill's confusion cell.
        commands = self._admin_platform()
        recall = _check(expected_skill="uipath-admin", skill_name="uipath-admin", commands=commands)
        precision = _check(expected_skill="uipath-admin", skill_name="uipath-platform", commands=commands)
        assert recall.observed_label == "yes" and recall.score == 1.0  # recall: GT engaged
        assert precision.observed_label == "yes" and precision.score == 0.0  # precision: off-target engaged


def _check_multi(
    *, expected_skill: str, skill_name: str, turns: list[list[CommandTelemetry]]
) -> ClassificationCriterionResult:
    criterion = SkillTriggeredCriterion(
        description="did agent invoke a skill?",
        expected_skill=expected_skill,
        skill_name=skill_name,
    )
    checker = SkillTriggeredChecker()
    turn_records = [_turn(cmds) for cmds in turns]
    result = checker.check(criterion, sandbox=None, turn_records=turn_records)  # type: ignore[arg-type]
    assert isinstance(result, ClassificationCriterionResult)
    return result


class TestSkillTriggeredGoldenCorpus:
    """Regression lock: pins observed_label AND score for the canonical
    multi-skill trajectories through ``_check_impl``. Any future change to the
    engagement policy (any- vs first-engagement) breaks these, forcing an
    explicit acknowledgement of the methodology break — and a re-score/backfill
    of historical activation P/R/F1 — before merge.
    """

    @pytest.mark.parametrize(
        ("case", "expected_skill", "skill_name", "commands", "exp_observed", "exp_score"),
        [
            (
                "single-target-recall",
                "uipath-admin",
                "uipath-admin",
                [_cmd("Skill", {"skill": "uipath-admin"}, tool_id="s1")],
                "yes",
                1.0,
            ),
            (
                "target-first-then-competitor-recall",
                "uipath-admin",
                "uipath-admin",
                [
                    _cmd("Skill", {"skill": "uipath-admin"}, tool_id="s1"),
                    _cmd("Skill", {"skill": "uipath-platform"}, tool_id="s2"),
                ],
                "yes",
                1.0,
            ),
            (
                "target-first-then-competitor-precision",
                "uipath-admin",
                "uipath-platform",
                [
                    _cmd("Skill", {"skill": "uipath-admin"}, tool_id="s1"),
                    _cmd("Skill", {"skill": "uipath-platform"}, tool_id="s2"),
                ],
                "yes",
                0.0,
            ),
            (
                "wrong-first-then-target-recall",
                "uipath-admin",
                "uipath-admin",
                [
                    _cmd("Skill", {"skill": "uipath-platform"}, tool_id="s1"),
                    _cmd("Skill", {"skill": "uipath-admin"}, tool_id="s2"),
                ],
                "yes",
                1.0,
            ),
            (
                "negative-target-engaged-false-positive",
                "",
                "uipath-admin",
                [_cmd("Skill", {"skill": "uipath-admin"}, tool_id="s1")],
                "yes",
                0.0,
            ),
            (
                "negative-no-engagement-true-negative",
                "",
                "uipath-admin",
                [_cmd("Read", {"file_path": "notes.txt"})],
                "no",
                1.0,
            ),
            (
                "file-read-target-recall",
                "uipath-admin",
                "uipath-admin",
                [_cmd("Read", {"file_path": "skills/uipath-admin/SKILL.md"})],
                "yes",
                1.0,
            ),
        ],
    )
    def test_golden_corpus_scores_are_pinned(
        self,
        case: str,
        expected_skill: str,
        skill_name: str,
        commands: list[CommandTelemetry],
        exp_observed: str,
        exp_score: float,
    ) -> None:
        result = _check(expected_skill=expected_skill, skill_name=skill_name, commands=commands)
        assert result.observed_label == exp_observed, case
        assert result.score == exp_score, case


class TestSkillTriggeredMultiTurnParity:
    """Any-engagement holds across TurnRecords and for the file-read signal, so
    the agent-agnostic parity CLAUDE.md stresses is asserted for the new branch.
    """

    def test_expected_skill_in_later_turn_still_recalls(self) -> None:
        # Distractor engaged in turn 1, expected skill in turn 2. Recall must
        # credit the GT criterion regardless of which turn the engagement lives in.
        result = _check_multi(
            expected_skill="uipath-admin",
            skill_name="uipath-admin",
            turns=[
                [_cmd("Skill", {"skill": "uipath-platform"}, tool_id="s1")],
                [_cmd("Skill", {"skill": "uipath-admin"}, tool_id="s2")],
            ],
        )
        assert result.observed_label == "yes" and result.score == 1.0

    def test_off_target_in_earlier_turn_is_precision_miss(self) -> None:
        # The competitor engaged in an EARLIER turn than the GT skill still lands
        # as a precision miss on its own criterion (no turn-order dependence).
        turns = [
            [_cmd("Skill", {"skill": "uipath-platform"}, tool_id="s1")],
            [_cmd("Skill", {"skill": "uipath-admin"}, tool_id="s2")],
        ]
        precision = _check_multi(expected_skill="uipath-admin", skill_name="uipath-platform", turns=turns)
        assert precision.observed_label == "yes" and precision.score == 0.0

    def test_file_read_parity_across_turns(self) -> None:
        # Off-Claude parity: the agent READS skill files across turns (platform's
        # in turn 1, admin's in turn 2). Recall/precision score identically to the
        # Skill-tool path, order- and turn-independent.
        turns = [
            [_cmd("Read", {"file_path": "skills/uipath-platform/SKILL.md"})],
            [_cmd("Read", {"file_path": "skills/uipath-admin/reference.md"})],
        ]
        recall = _check_multi(expected_skill="uipath-admin", skill_name="uipath-admin", turns=turns)
        precision = _check_multi(expected_skill="uipath-admin", skill_name="uipath-platform", turns=turns)
        assert recall.observed_label == "yes" and recall.score == 1.0
        assert precision.observed_label == "yes" and precision.score == 0.0


class TestSkillTriggeredCodex:
    """Codex has no ``Skill`` tool — it engages a skill by reading its files via shell.

    The detectable signal is a command whose recorded text references the skill's
    directory, ``skills/<skill_name>/`` (present in both the repo path and the
    ``.agents/skills/`` symlink).
    """

    def _sed(self, path: str) -> CommandTelemetry:
        return _cmd("Bash", {"command": f"/bin/zsh -lc \"sed -n '1,220p' {path}\""})

    def test_codex_reads_skill_md_repo_path_tp(self) -> None:
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[self._sed("/Users/x/uipath/skills/skills/uipath-agents/SKILL.md")],
        )
        assert result.score == 1.0 and result.observed_label == "yes" and result.expected_label == "yes"

    def test_codex_reads_skill_reference_repo_path_tp(self) -> None:
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[self._sed("/Users/x/uipath/skills/skills/uipath-agents/references/context-grounding.md")],
        )
        assert result.score == 1.0 and result.observed_label == "yes"

    def test_codex_reads_skill_md_agents_symlink_tp(self) -> None:
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[self._sed(".agents/skills/uipath-agents/SKILL.md")],
        )
        assert result.score == 1.0 and result.observed_label == "yes"

    def test_codex_reads_skill_md_windows_path_tp(self) -> None:
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[self._sed(r"C:\sandbox\.agents\skills\uipath-agents\SKILL.md")],
        )
        assert result.score == 1.0 and result.observed_label == "yes"

    def test_codex_reads_skill_md_json_escaped_windows_command_tp(self) -> None:
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[self._sed(r"C:\\sandbox\\.agents\\skills\\uipath-agents\\SKILL.md")],
        )
        assert result.score == 1.0 and result.observed_label == "yes"

    def test_codex_reads_wrong_skill_not_counted(self) -> None:
        # Read uipath-rpa's SKILL.md, but criterion filters on uipath-agents -> observed="no".
        result = _check(
            expected_skill="",
            skill_name="uipath-agents",
            commands=[self._sed("/Users/x/uipath/skills/skills/uipath-rpa/SKILL.md")],
        )
        assert result.observed_label == "no" and result.score == 1.0

    def test_codex_prefix_collision_guarded(self) -> None:
        # Reading "uipath-agents-extra" must NOT match skill_name "uipath-agents" (trailing slash).
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[self._sed("/Users/x/uipath/skills/skills/uipath-agents-extra/SKILL.md")],
        )
        assert result.observed_label == "no" and result.score == 0.0

    def test_codex_listing_skills_dir_not_counted(self) -> None:
        # `ls .agents/skills/` references no specific skill -> observed="no".
        result = _check(
            expected_skill="",
            skill_name="uipath-agents",
            commands=[_cmd("Bash", {"command": "ls .agents/skills/"})],
        )
        assert result.observed_label == "no" and result.score == 1.0

    def test_read_tool_with_skill_path_counts(self) -> None:
        # Agent-agnostic over param values: a Read whose file_path is inside the skill dir counts.
        result = _check(
            expected_skill="uipath-agents",
            skill_name="uipath-agents",
            commands=[_cmd("Read", {"file_path": "/x/skills/uipath-agents/SKILL.md"})],
        )
        assert result.observed_label == "yes" and result.score == 1.0


class TestSkillTriggeredCriterionValidation:
    def test_requires_expected_skill_and_skill_name(self) -> None:
        with pytest.raises(ValueError):
            SkillTriggeredCriterion(description="d")  # type: ignore[call-arg]

    def test_valid_construction(self) -> None:
        c = SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow")
        assert c.expected_skill == "uipath-flow"
        assert c.skill_name == "uipath-flow"


class TestSkillTriggeredAggregate:
    def _row(self, observed: str, expected: str) -> ClassificationCriterionResult:
        return ClassificationCriterionResult(
            criterion_type="skill_triggered",
            description="d",
            score=1.0 if observed == expected else 0.0,
            observed_label=observed,
            expected_label=expected,
        )

    def test_aggregate_emits_classification_metrics(self) -> None:
        # 2 TP(yes), 1 TN(no), 1 FN(yes predicted as no), 1 FP(no predicted as yes).
        # yes class: TP=2, FP=1, FN=1 -> precision=2/3, recall=2/3, f1=2/3
        # no class:  TP=1, FP=1, FN=1 -> precision=1/2, recall=1/2, f1=1/2
        per_rows = [
            self._row("yes", "yes"),
            self._row("yes", "yes"),
            self._row("no", "no"),
            self._row("no", "yes"),
            self._row("yes", "no"),
        ]
        checker = SkillTriggeredChecker()
        criterion = SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow")
        agg = checker.aggregate(criterion, per_rows)
        assert agg is not None
        m = agg.metrics
        assert m["accuracy"] == pytest.approx(3 / 5)
        assert m["precision.yes"] == pytest.approx(2 / 3)
        assert m["recall.yes"] == pytest.approx(2 / 3)
        assert m["f1.yes"] == pytest.approx(2 / 3)
        assert m["precision.no"] == pytest.approx(1 / 2)
        assert m["recall.no"] == pytest.approx(1 / 2)
        # Baseline stats inherited
        assert m["count"] == 5.0
        assert m["mean"] == pytest.approx(3 / 5)

    def test_aggregate_empty_returns_none(self) -> None:
        assert (
            SkillTriggeredChecker().aggregate(
                SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow"), []
            )
            is None
        )

    def test_aggregate_non_classification_rows_returns_baseline_only(self) -> None:
        # Rows without labels (e.g., error path) -> baseline stats, no classification.
        per_rows: list[CriterionResult] = [
            CriterionResult(criterion_type="skill_triggered", description="d", score=0.0, error="boom"),
        ]
        checker = SkillTriggeredChecker()
        agg = checker.aggregate(
            SkillTriggeredCriterion(description="d", expected_skill="uipath-flow", skill_name="uipath-flow"), per_rows
        )
        assert agg is not None
        assert "accuracy" not in agg.metrics  # no classification pairs
        assert agg.metrics["count"] == 1.0


class TestRegistry:
    def test_registered(self) -> None:
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=True)
        assert "skill_triggered" in CriterionRegistry.list_types()
