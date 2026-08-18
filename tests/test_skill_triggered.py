"""Tests for SkillTriggeredCriterion + checker.

The delivered-body cases below (an errored `Skill` call scores `no`, a delivered one `yes`) are
not only a checker detail: that rule re-baselines every activation suite authored before it
DOWNWARD, because the same traces that used to score `yes` now score `no`. A suite's
`suite_thresholds` therefore cannot be trusted across that boundary without a fresh measurement —
see the provenance comments above each `suite_thresholds:` block in `tasks/skills/`, and the
blast-radius paragraph in `criteria/skill_triggered.py`'s module docstring.

The FILE-READ half of that same rule moved on 2026-08-16 (a crash-force-closed
`Read`/`Glob`/`Grep` stopped counting, matching the `Skill` half's allowlist). It is the same
class of change and the same provenance blocks record it — but measured over the backend that
can produce the pair it re-scores nothing, because the pair never occurs there (the numbers,
and why the file-read denominator rather than the all-commands one is the honest N, are in
`criteria/skill_triggered.py`'s module docstring). `TestEngagementTruthTable` is the contract
that replaced the single remembered branch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from coder_eval.criteria.skill_triggered import _FILE_READ_TOOLS, SkillTriggeredChecker
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
    result_status: str | None = "success",
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

    def test_pending_skill_call_is_not_engagement(self) -> None:
        # `result_status=None` is an IN-FLIGHT call: the early-stop watcher evaluates on
        # ToolStartEvent, before any result exists. For the Skill tool the body IS the tool
        # result, so a call with no result yet has delivered nothing. Counting it made the
        # live verdict pass on a call the frozen check would later score `no`.
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[_cmd("Skill", {"skill": "uipath-flow"}, result_status=None)],
        )
        assert result.observed_label == "no" and result.score == 0.0

    def test_unknown_skill_call_is_not_engagement(self) -> None:
        # A turn that crashes force-closes its open tool calls to "unknown". A Skill call
        # that never returned a result never delivered a body — including the refusal case
        # where the crash beat the error result to the recorder.
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[_cmd("Skill", {"skill": "uipath-flow"}, result_status="unknown")],
        )
        assert result.observed_label == "no" and result.score == 0.0

    def test_excluded_skill_call_is_not_resurrected_by_its_own_parameters(self) -> None:
        # The Skill branch is AUTHORITATIVE for a Skill call. Without that, a call the
        # status gate excluded would fall through to the generic path scan and be counted
        # again the moment any of its own parameters happened to contain a
        # `skills/<name>/`-shaped substring — reintroducing exactly the false `yes` the
        # gate exists to remove, and breaking monotonicity (absent -> present on a call
        # that delivered nothing).
        result = _check(
            expected_skill="uipath-flow",
            skill_name="uipath-flow",
            commands=[_cmd("Skill", {"skill": "x", "hint": "see skills/uipath-flow/SKILL.md"}, result_status="error")],
        )
        assert result.observed_label == "no" and result.score == 0.0

    def test_failed_read_of_a_skill_path_is_not_engagement(self) -> None:
        # A failed Read puts the path in `parameters` while loading nothing. The path
        # reference alone is not evidence the body reached the agent.
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Read", {"file_path": "/x/.agents/skills/my-skill/SKILL.md"}, result_status="error")],
        )
        assert result.observed_label == "no" and result.score == 0.0

    def test_pending_read_of_a_skill_path_is_not_engagement(self) -> None:
        # Same reason as the pending Skill call: counting the in-flight Read would
        # reintroduce the live/frozen divergence one branch over.
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Read", {"file_path": "/x/.agents/skills/my-skill/SKILL.md"}, result_status=None)],
        )
        assert result.observed_label == "no" and result.score == 0.0

    def test_failed_bash_read_still_counts(self) -> None:
        # `Bash` is deliberately NOT gated: `cat SKILL.md | grep foo` exits non-zero AFTER
        # genuinely reading the file. A blanket status gate would drop real off-Claude
        # engagement, which is the whole file-read signal.
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Bash", {"command": "cat skills/my-skill/SKILL.md | grep foo"}, result_status="error")],
        )
        assert result.observed_label == "yes" and result.score == 1.0

    def test_unknown_bash_read_still_counts(self) -> None:
        # The status gate's scope is file-read-only, and `Bash` engagement is inferred from
        # the COMMAND TEXT independently of status. `"unknown"` is genuinely inconclusive
        # here — it cannot show the read happened — but gating on it would make the whole
        # cross-agent file-read signal depend on telemetry that may never report completion,
        # and the same argument that keeps `"error"` counting applies. Pins the truth table
        # below as covering `_FILE_READ_TOOLS` and nothing wider.
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Bash", {"command": "cat skills/my-skill/SKILL.md"}, result_status="unknown")],
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


_GATED_TOOLS: tuple[str, ...] = ("Skill", *sorted(_FILE_READ_TOOLS))
_STATUSES: tuple[str | None, ...] = ("success", "error", None, "unknown")


def _engaging_params(tool_name: str) -> dict[str, Any]:
    """Parameters that make ONE command a would-be engagement of ``my-skill``.

    Derived from the tool name rather than tabulated per case, so adding a tool to
    ``_FILE_READ_TOOLS`` extends the truth table below without editing it.

    The keys match what each tool ACTUALLY records — `Glob` takes a `pattern`, `Grep` a
    `pattern` plus a `path`, only `Read` a `file_path`. The scan reads every string parameter
    so a single `file_path` would work for all three, but a fixture that does not look like
    real telemetry hides the day one of them stops carrying a skill path at all.
    """
    if tool_name == "Skill":
        return {"skill": "my-skill"}
    if tool_name == "Glob":
        return {"pattern": "/x/skills/my-skill/*"}
    if tool_name == "Grep":
        return {"pattern": "foo", "path": "/x/skills/my-skill/"}
    return {"file_path": "/x/skills/my-skill/SKILL.md"}


class TestEngagementTruthTable:
    """The delivered-body rule, pinned across every ``(gated tool, result_status)`` pair.

    Replaces ``test_unknown_read_still_counts``, which asserted the opposite for
    ``Read``/``"unknown"`` on a justification that is false — see
    ``TestPerAgentTelemetryInventory``. The gate used to be written twice (an allowlist on
    the ``Skill`` branch, a denylist on the file-read one); it is one expression now, and
    this is its contract: for the ``Skill`` tool and the three file-read tools alike,
    engagement iff ``result_status == "success"``.
    """

    @pytest.mark.parametrize("result_status", _STATUSES)
    @pytest.mark.parametrize("tool_name", _GATED_TOOLS)
    def test_engagement_iff_result_status_is_success(self, tool_name: str, result_status: str | None) -> None:
        engaged = result_status == "success"
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd(tool_name, _engaging_params(tool_name), result_status=result_status)],
        )
        case = f"{tool_name}/{result_status}"
        assert result.observed_label == ("yes" if engaged else "no"), case
        assert result.score == (1.0 if engaged else 0.0), case

    @pytest.mark.parametrize("result_status", _STATUSES)
    @pytest.mark.parametrize("tool_name", _GATED_TOOLS)
    def test_live_verdict_agrees_with_the_frozen_check(self, tool_name: str, result_status: str | None) -> None:
        # A live/frozen divergence is the failure mode the gate exists to prevent: the
        # watcher passing a run on a signal the frozen check later scores `no`. Asserted
        # on the same 16 cases rather than trusted from the shared helper.
        criterion = SkillTriggeredCriterion(
            description="did agent invoke a skill?", expected_skill="my-skill", skill_name="my-skill"
        )
        turn_records = [_turn([_cmd(tool_name, _engaging_params(tool_name), result_status=result_status)])]
        live = SkillTriggeredChecker().live_verdict(criterion, turn_records)
        frozen = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=turn_records[0].commands,
        )
        case = f"{tool_name}/{result_status}"
        # A positive criterion can only ever live-`pass`; its absence is never decidable
        # mid-run, so the non-engaging cases latch `undecided` rather than `fail`.
        assert live == ("pass" if frozen.observed_label == "yes" else "undecided"), case


class TestLatchMonotonicity:
    """A command's contribution may go absent -> present as it resolves, never the reverse.

    Load-bearing for ``live_verdict``: a latched verdict that could flip would let the
    watcher stop a run on a signal the frozen check then scores the other way. The
    allowlist makes the latch STRICTER, which is the safe direction.
    """

    def _criterion(self) -> SkillTriggeredCriterion:
        return SkillTriggeredCriterion(description="d", expected_skill="my-skill", skill_name="my-skill")

    def _read(self, result_status: str | None) -> CommandTelemetry:
        return _cmd("Read", {"file_path": "/x/skills/my-skill/SKILL.md"}, result_status=result_status)

    def test_in_flight_read_resolving_to_success_goes_undecided_to_pass(self) -> None:
        checker = SkillTriggeredChecker()
        criterion = self._criterion()
        assert checker.live_verdict(criterion, [_turn([self._read(None)])]) == "undecided"
        assert checker.live_verdict(criterion, [_turn([self._read("success")])]) == "pass"

    def test_in_flight_read_force_closed_to_unknown_stays_undecided(self) -> None:
        # The crash force-close path (`claude_code_agent._finalize_commands`,
        # `antigravity_agent`'s orphan close). Nothing was delivered, so the latch must
        # not advance — and the frozen check must agree.
        checker = SkillTriggeredChecker()
        criterion = self._criterion()
        assert checker.live_verdict(criterion, [_turn([self._read(None)])]) == "undecided"
        assert checker.live_verdict(criterion, [_turn([self._read("unknown")])]) == "undecided"
        frozen = _check(expected_skill="my-skill", skill_name="my-skill", commands=[self._read("unknown")])
        assert frozen.observed_label == "no" and frozen.score == 0.0


class TestPerAgentTelemetryInventory:
    """Which agent can emit which ``(tool_name, result_status)`` pair — DERIVED, not restated.

    The deleted ``test_unknown_read_still_counts`` and its comment justified counting
    ``Read``/``"unknown"`` as engagement by asserting that Codex reconstructs real calls
    from the rollout with that status. That claim was a hand-written restatement of the two
    dicts below, and it was false in the half that mattered: Codex emits none of
    ``Read``/``Glob``/``Grep``, so it cannot produce the changed pair at all — whatever
    statuses it sets. (Codex *does* set ``"unknown"``, via ``close_open_tools`` and a command
    with no exit code; that is not the point, and asserting it here would be another
    hand-written restatement. The tool-name disjointness below is the whole load-bearing
    claim, and it is what the asserts check.)

    (The original sentence is deliberately not quoted verbatim here — a grep for it is the
    cheap check that it is gone.) Importing the dicts
    rather than re-listing their values is the whole point — a future comment naming a
    backend as a producer is then checked instead of trusted.
    """

    def test_codex_emits_none_of_the_file_read_tool_names(self) -> None:
        from coder_eval.agents import codex_agent

        item_names = set(codex_agent._TOOL_ITEM_NAMES.values())
        rollout_names = set(codex_agent._ROLLOUT_FN_NAMES.values())
        # Anti-vacuity first: a rename or an emptied dict must report a GAP, not pass
        # silently on an empty set (the CE044/CE045 lesson).
        assert item_names, "codex_agent._TOOL_ITEM_NAMES is empty — renamed or moved?"
        assert rollout_names, "codex_agent._ROLLOUT_FN_NAMES is empty — renamed or moved?"
        assert _FILE_READ_TOOLS.isdisjoint(item_names | rollout_names), (
            "Codex now emits a file-read tool name — the delivered-body rule's rationale "
            "(only the crash force-close paths produce (file-read, 'unknown')) needs re-checking"
        )

    def test_antigravity_does_reach_the_file_read_tool_names(self) -> None:
        # The complement, so the inventory documents WHICH backend can produce the pair
        # rather than claiming none can: antigravity renames view_file/search_directory/
        # find_file to Read/Grep/Glob, and its orphan-close path sets "unknown".
        from coder_eval.agents import antigravity_agent

        mapped = set(antigravity_agent._ANTIGRAVITY_TO_CLAUDE_TOOL_MAP.values())
        assert mapped, "antigravity_agent._ANTIGRAVITY_TO_CLAUDE_TOOL_MAP is empty — renamed or moved?"
        assert mapped >= _FILE_READ_TOOLS, sorted(_FILE_READ_TOOLS - mapped)


class TestSuiteLevelEffectOfTheGate:
    """The gate turns a per-row gap into a suite METRIC — assert the metric, not the label.

    ``recall.yes`` is what ``optimize.activation.activation_gate`` promotes on (via ``f1.yes``),
    so a row whose only engagement is a crash-force-closed read moving from `yes` to `no`
    is a promotion-decision change, not a cosmetic one.
    """

    def _rows(self, third_row_status: str) -> list[CriterionResult]:
        commands_per_row = [
            [_cmd("Skill", {"skill": "my-skill"})],
            [_cmd("Read", {"file_path": "/x/skills/my-skill/SKILL.md"})],
            [_cmd("Read", {"file_path": "/x/skills/my-skill/SKILL.md"}, result_status=third_row_status)],
        ]
        return [_check(expected_skill="my-skill", skill_name="my-skill", commands=c) for c in commands_per_row]

    def _recall_yes(self, rows: list[CriterionResult]) -> float:
        checker = SkillTriggeredChecker()
        criterion = SkillTriggeredCriterion(description="d", expected_skill="my-skill", skill_name="my-skill")
        agg = checker.aggregate(criterion, rows)
        assert agg is not None
        return agg.metrics["recall.yes"]

    def test_a_crash_force_closed_read_costs_the_suite_a_third_of_its_recall(self) -> None:
        assert self._recall_yes(self._rows("success")) == pytest.approx(1.0)
        assert self._recall_yes(self._rows("unknown")) == pytest.approx(2 / 3)


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
            # Appended (not edited) when engagement narrowed to RESOLVED, SUCCESSFUL
            # signals. Historical activation P/R/F1 computed before that change is not
            # directly comparable if any run contained these two shapes — which is what
            # these entries exist to make explicit.
            #
            # The third entry (2026-08-16) was appended when the FILE-READ half of the same
            # rule moved from a denylist to the allowlist the `Skill` half already used, so
            # a crash-force-closed read stopped counting. The corpus did not go red on its
            # own — no `"unknown"` file-read case was pinned — so skipping this append would
            # have made a scoring change invisible here. Measured over the backend that can
            # produce the pair (1,754 claude-code task.json, 8,706 file-read commands: 8,496
            # "success" / 210 "error" / 0 "unknown"), no historical run re-scores; the entry
            # exists so the NEXT change to this policy cannot land silently either.
            (
                "errored-skill-call-with-no-other-signal",
                "uipath-admin",
                "uipath-admin",
                [_cmd("Skill", {"skill": "uipath-admin"}, tool_id="s1", result_status="error")],
                "no",
                0.0,
            ),
            (
                "failed-read-of-skill-path",
                "uipath-admin",
                "uipath-admin",
                [_cmd("Read", {"file_path": "skills/uipath-admin/SKILL.md"}, result_status="error")],
                "no",
                0.0,
            ),
            (
                "unknown-read-of-skill-path",
                "uipath-admin",
                "uipath-admin",
                [_cmd("Read", {"file_path": "skills/uipath-admin/SKILL.md"}, result_status="unknown")],
                "no",
                0.0,
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


class TestSuppressedEngagementDetails:
    """A non-engagement row names WHY it did not engage, when there is a why to name.

    `recall.yes` collapsing because every `Skill` call was refused under
    `disable-model-invocation` and `recall.yes` collapsing because the agent never reached for
    the skill are the same number in the report and completely different problems: the first
    invalidates the round, the second is the measurement working. The note is diagnostic only —
    `score`, `observed_label` and `expected_label` are unaffected.
    """

    def _details(self, *, expected_skill: str, skill_name: str, commands: list[CommandTelemetry]) -> str:
        return _check(expected_skill=expected_skill, skill_name=skill_name, commands=commands).details

    def test_a_refused_skill_call_is_named(self) -> None:
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Skill", {"skill": "my-skill"}, result_status="error")],
        )
        assert result.observed_label == "no" and result.score == 0.0
        assert "Skill/error x1" in result.details, result.details

    def test_a_crash_force_closed_read_is_named(self) -> None:
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Read", {"file_path": "/x/skills/my-skill/SKILL.md"}, result_status="unknown")],
        )
        assert result.observed_label == "no" and result.score == 0.0
        assert "Read/unknown x1" in result.details, result.details

    def test_an_in_flight_call_reads_as_in_flight_not_as_none(self) -> None:
        # `result_status=None` renders as a word rather than the literal `None`, which in a
        # report reads as a missing value rather than as the state it is.
        details = self._details(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Skill", {"skill": "my-skill"}, result_status=None)],
        )
        assert "Skill/in-flight x1" in details, details

    def test_nothing_suppressed_leaves_details_byte_identical(self) -> None:
        # The whole point of appending rather than reformatting: a row with no engagement signal
        # at all must render exactly as it did before this note existed. Asserted against the
        # literal shape, not against a prefix check.
        details = self._details(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Read", {"file_path": "notes.txt"})],
        )
        assert details == "observed='no', expected='yes' (skill_name='my-skill')"

    def test_a_row_that_engaged_after_an_earlier_refusal_carries_no_note(self) -> None:
        # A later success IS engagement. Reporting the earlier refusal on a passing row would
        # read as a failure — the helper must not run on the `triggered is True` path at all.
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[
                _cmd("Skill", {"skill": "my-skill"}, tool_id="s1", result_status="error"),
                _cmd("Skill", {"skill": "my-skill"}, tool_id="s2"),
            ],
        )
        assert result.observed_label == "yes" and result.score == 1.0
        assert result.details == "observed='yes', expected='yes' (skill_name='my-skill')"

    def test_a_suppressed_distractor_still_passes_and_reads_neutrally(self) -> None:
        # The distractor/negative case: suppression here means the row scored CORRECTLY. The
        # note must be an observation, not an accusation — no "failed", "error" or "problem"
        # framing beyond the raw status token the pair carries.
        result = _check(
            expected_skill="",
            skill_name="my-skill",
            commands=[_cmd("Skill", {"skill": "my-skill"}, result_status="error")],
        )
        assert result.observed_label == "no" and result.score == 1.0
        assert "not delivered" in result.details, result.details
        assert "fail" not in result.details.lower(), result.details

    def test_the_note_is_aggregated_and_bounded(self) -> None:
        # Fifty refused calls must not produce a fifty-entry string: `details` is persisted per
        # row in task.json and rendered in reports. Two DIFFERENT mechanisms bound this and it
        # is worth keeping them apart: the COUNT is bounded by the Counter (fifty identical
        # pairs aggregate to one entry) while the CARDINALITY is bounded by
        # `_SUPPRESSED_RENDER_LIMIT`. Only the max-cardinality case below exercises the second.
        commands = [_cmd("Skill", {"skill": "my-skill"}, tool_id=f"s{i}", result_status="error") for i in range(50)]
        commands += [
            _cmd("Read", {"file_path": "/x/skills/my-skill/SKILL.md"}, tool_id="r1", result_status="unknown"),
            _cmd("Glob", {"pattern": "/x/skills/my-skill/*"}, tool_id="g1", result_status="error"),
            _cmd("Grep", {"pattern": "foo", "path": "/x/skills/my-skill/"}, tool_id="p1", result_status=None),
        ]
        details = self._details(expected_skill="my-skill", skill_name="my-skill", commands=commands)
        assert "53 engagement signal(s) not delivered" in details, details
        assert "Skill/error x50" in details, details
        # Four distinct pairs, three rendered, the remainder elided rather than listed.
        assert "+1 more" in details, details

    def test_the_note_is_bounded_at_maximum_cardinality(self) -> None:
        # The genuine worst case, and the only fixture on which a length assertion does work:
        # every gated tool x every non-success status. `_delivered` returns True for all other
        # tools, so 4 x 3 = 12 pairs is the structural ceiling. Measured: ~299 chars unbounded,
        # ~155 bounded — so a raised `_SUPPRESSED_RENDER_LIMIT` fails here rather than passing
        # on a three-pair fixture that never reached the limit.
        commands = [
            _cmd(tool, _engaging_params(tool), tool_id=f"{tool}{i}", result_status=status)
            for i, (tool, status) in enumerate((t, s) for t in _GATED_TOOLS for s in _STATUSES if s != "success")
        ]
        details = self._details(expected_skill="my-skill", skill_name="my-skill", commands=commands)
        assert "12 engagement signal(s) not delivered" in details, details
        assert "+9 more" in details, details
        assert len(details) < 200, details

    def test_the_note_carries_no_markup_brackets(self) -> None:
        # `details` flows into report renderers; a `[...]`-shaped count would be read as
        # markup by any of them that does not escape first.
        details = self._details(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Skill", {"skill": "my-skill"}, result_status="error")],
        )
        # Require the note to EXIST before pinning its shape. Without this the test is a
        # formatting assertion about a string it never demands, so removing the note
        # entirely leaves it green — which is exactly how it read before this line.
        assert "Skill/error x1" in details, details
        assert "[" not in details and "]" not in details, details

    def test_a_suppressed_call_for_another_skill_is_not_reported(self) -> None:
        # The helper is keyed on THIS criterion's skill_name. A refused call for a different
        # skill is not this row's reason for not engaging.
        details = self._details(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd("Skill", {"skill": "other-skill"}, result_status="error")],
        )
        assert details == "observed='no', expected='yes' (skill_name='my-skill')"

    @pytest.mark.parametrize("result_status", _STATUSES)
    @pytest.mark.parametrize("tool_name", _GATED_TOOLS)
    def test_the_truth_table_scores_are_unchanged_by_the_note(self, tool_name: str, result_status: str | None) -> None:
        # Phase 3's acceptance criterion in test form: re-runs the engagement truth table and
        # asserts the diagnostic changed no score and no label, only `details`.
        engaged = result_status == "success"
        result = _check(
            expected_skill="my-skill",
            skill_name="my-skill",
            commands=[_cmd(tool_name, _engaging_params(tool_name), result_status=result_status)],
        )
        case = f"{tool_name}/{result_status}"
        assert result.observed_label == ("yes" if engaged else "no"), case
        assert result.expected_label == "yes", case
        assert result.score == (1.0 if engaged else 0.0), case
