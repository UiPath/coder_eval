"""Unit tests for `coder_eval.optimize.search` — the accept/revert decision and the leak preflight.

`search_compare` is emphatically NOT a gate (unpaired, unreplicated, uncorrected), so a `True` here
is a hypothesis to gate. `candidate_leaks` stays pure while `skill_text` beside it does the IO.
"""

import inspect
from pathlib import Path
from typing import ClassVar

import pytest

from coder_eval.leak_detection import LEAK_MIN_CHARS
from coder_eval.models import (
    ArmRowScores,
    FileCheckCriterion,
    OptimizeMeasurements,
    RegressionRow,
    RoundScores,
    SkillTriggeredCriterion,
    TaskDefinition,
)
from coder_eval.optimize.search import (
    SearchComparison,
    candidate_leaks,
    lineage_head_scores,
    regression_check,
    search_compare,
    skill_text,
)
from tests.optimize_fixtures import (
    SEARCH_HEAD_SCORES,
    SUITE,
    arm_row_scores_for,
)


class TestRegressionCheck:
    """The corpus finally has a reader, and a hole is not a pass."""

    _CORPUS: ClassVar[list[RegressionRow]] = [
        RegressionRow(row_id="pos-1", promoted_in_round=1, reason="oblique phrasing"),
        RegressionRow(row_id="pos-2", promoted_in_round=1, reason="symptom vocabulary"),
        RegressionRow(row_id="pos-3", promoted_in_round=2, reason="negated request"),
    ]

    def test_names_the_lost_row_and_the_hole_but_not_the_kept_one(self) -> None:
        arm = ArmRowScores(variant_id="cand", row_scores={"pos-1": 1.0, "pos-2": 0.5})
        found = regression_check(self._CORPUS, arm)
        assert [(row.row_id, score) for row, score in found] == [("pos-2", 0.5), ("pos-3", None)]

    def test_an_empty_corpus_is_an_empty_result(self) -> None:
        assert regression_check([], ArmRowScores(variant_id="cand", row_scores={"pos-1": 1.0})) == []

    def test_an_arm_that_scored_nothing_reports_every_row_as_a_hole(self) -> None:
        found = regression_check(self._CORPUS, ArmRowScores(variant_id="cand"))
        assert [score for _row, score in found] == [None, None, None]

    def test_the_threshold_reclassifies_a_partial_score(self) -> None:
        # 2 of 3 replicates reads 0.667: a loss at the binary default, a pass on a fractional suite.
        arm = ArmRowScores(variant_id="cand", row_scores={r.row_id: 2 / 3 for r in self._CORPUS})
        assert len(regression_check(self._CORPUS, arm)) == 3
        assert regression_check(self._CORPUS, arm, threshold=0.6) == []

    def test_results_come_back_in_corpus_order(self) -> None:
        arm = ArmRowScores(variant_id="cand", row_scores={"pos-2": 0.0, "pos-1": 0.0, "pos-3": 0.0})
        assert [row.row_id for row, _score in regression_check(self._CORPUS, arm)] == ["pos-1", "pos-2", "pos-3"]


def _leak_row(row_id: str, criteria: list) -> TaskDefinition:
    """One expanded train row — what `expand_dataset` hands `candidate_leaks`."""
    return TaskDefinition(
        task_id=f"{SUITE}/{row_id}",
        description="leak fixture",
        initial_prompt="do the thing",
        success_criteria=criteria,
    )


# The real shape: a criterion asserting a substantive string on a file the row expects.
_GRADED = "minimum-task-score"
_ROWS = [_leak_row("r1", [FileCheckCriterion(description="d", path="out.yml", includes=[_GRADED])])]


class TestSkillText:
    """The preflight reads a TREE, because a one-file read returns clean on a real leak.

    A candidate may edit `scripts/` and reference files, so a graded string bundled into one of those
    was invisible to `(arm / "SKILL.md").read_text()` — and the result came back **clean**,
    byte-identical to a genuinely clean candidate, which is the worst shape a preflight can have.
    """

    @staticmethod
    def _skill(root: Path, *, body: str = "# My skill\n", script: str | None = None) -> Path:
        skill = root / "skills" / "my-skill"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(body, encoding="utf-8")
        if script is not None:
            (skill / "scripts" / "helper.py").write_text(script, encoding="utf-8")
        return skill

    def test_a_graded_string_bundled_in_scripts_is_flagged(self, tmp_path: Path) -> None:
        """And the same tree read one-file-only is NOT — both halves, so the test states the defect."""
        candidate = self._skill(tmp_path / "cand", script=f"THRESHOLD = {_GRADED!r}\n")
        baseline = self._skill(tmp_path / "base")

        assert candidate_leaks(skill_text(candidate), skill_text(baseline), _ROWS)
        one_file = (candidate / "SKILL.md").read_text(encoding="utf-8")
        assert candidate_leaks(one_file, skill_text(baseline), _ROWS) == [], (
            "the one-file read must come back clean here — that IS the defect this replaces"
        )

    def test_a_string_already_in_the_baselines_reference_file_is_not_flagged(self, tmp_path: Path) -> None:
        # Widening the scan cannot re-introduce the wolf-crying the DIFF was built to prevent: a span
        # the baseline already carries stays invisible however many files are read.
        script = f"THRESHOLD = {_GRADED!r}\n"
        candidate = self._skill(tmp_path / "cand", script=script)
        baseline = self._skill(tmp_path / "base", script=script)
        assert candidate_leaks(skill_text(candidate), skill_text(baseline), _ROWS) == []

    def test_a_binary_file_is_skipped_rather_than_raising(self, tmp_path: Path) -> None:
        skill = self._skill(tmp_path / "cand")
        (skill / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01")
        text = skill_text(skill)
        assert "My skill" in text and "PNG" not in text

    def test_a_symlink_out_of_the_skill_directory_is_not_followed(self, tmp_path: Path) -> None:
        """Both forms: a linked FILE and a linked DIRECTORY. Either would scan arbitrary files.

        The two are stopped by different mechanisms — the explicit `is_symlink` check and `rglob` not
        recursing through a symlinked directory — so a test covering one proves nothing about the
        other.
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("a secret from outside the skill", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "also-secret.txt").write_text("another secret entirely", encoding="utf-8")

        skill = self._skill(tmp_path / "cand")
        (skill / "linked.txt").symlink_to(outside)
        (skill / "linkdir").symlink_to(elsewhere, target_is_directory=True)

        text = skill_text(skill)
        assert "outside the skill" not in text and "another secret" not in text

    def test_an_unreadable_file_does_not_abort_the_scan(self, tmp_path: Path) -> None:
        # A preflight must not fail a round over one stray unreadable file.
        import os

        skill = self._skill(tmp_path / "cand")
        locked = skill / "locked.md"
        locked.write_text("unreachable", encoding="utf-8")
        os.chmod(locked, 0o000)
        try:
            assert "My skill" in skill_text(skill)
        finally:
            os.chmod(locked, 0o644)

    def test_each_file_is_preceded_by_its_relative_path(self, tmp_path: Path) -> None:
        # So a finding stays locatable, and so two files cannot concatenate into a phantom match
        # across the boundary between them.
        skill = self._skill(tmp_path / "cand", script="print(1)\n")
        text = skill_text(skill)
        assert "SKILL.md" in text and "scripts/helper.py" in text

    def test_the_order_is_deterministic(self, tmp_path: Path) -> None:
        skill = self._skill(tmp_path / "cand", script="print(1)\n")
        (skill / "reference.md").write_text("some reference prose\n", encoding="utf-8")
        assert skill_text(skill) == skill_text(skill)
        # Sorted by relative path, so a filesystem's directory order cannot move it.
        text = skill_text(skill)
        assert text.index("SKILL.md") < text.index("reference.md") < text.index("scripts/helper.py")

    def test_an_empty_or_missing_directory_is_an_empty_string(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        assert skill_text(tmp_path / "empty") == ""
        assert skill_text(tmp_path / "nope") == ""


class TestCandidateLeaks:
    """The anti-memorization preflight. It is a DIFF, and every test here is about that."""

    def test_flags_a_span_the_candidate_adds(self) -> None:
        findings = candidate_leaks(f"set {_GRADED} in the workflow", "an incumbent that says nothing", _ROWS)
        assert len(findings) == 1
        assert _GRADED in findings[0] and "r1" in findings[0] and "file_check" in findings[0]

    def test_returns_nothing_when_the_baseline_already_says_it(self) -> None:
        """THE false-positive regression, and the reason this function takes a baseline.

        Measured against this repo's own `tasks/skills/ci-outcome.yaml`, an absolute scan flags the
        shipped `ci` skill on five strings that are simply the output contract its suite grades. A
        checker that fires on the shipped skill on its first run is one users learn to ignore.

        Built as candidate = baseline + unrelated prose rather than as the degenerate
        candidate == baseline: that is the real shape (a candidate is an EDIT of its baseline), and
        it proves the containment is per-span rather than a whole-text equality.
        """
        baseline = f"The workflow must set {_GRADED} to the project's floor."
        assert candidate_leaks(baseline, baseline, _ROWS) == []
        assert candidate_leaks(baseline + "\n\nAlso prefer the smallest edit that could work.", baseline, _ROWS) == []

    def test_a_clean_candidate_is_clean(self) -> None:
        assert candidate_leaks("nothing graded here at all", "", _ROWS) == []

    def test_an_empty_candidate_leaks_nothing(self) -> None:
        # Degenerate input returns empty rather than raising. (Not the emptied-body control, which
        # keeps its frontmatter and is skipped by the skill's loop anyway.)
        assert candidate_leaks("", "", _ROWS) == []

    def test_empty_rows_return_empty_rather_than_raising(self) -> None:
        assert candidate_leaks(f"a body mentioning {_GRADED}", "", []) == []

    def test_an_empty_baseline_flags_everything_present(self) -> None:
        # Correct rather than noisy: a baseline with no body cannot have contributed anything.
        assert len(candidate_leaks(f"we set {_GRADED} here", "", _ROWS)) == 1

    def test_identical_findings_are_de_duplicated(self) -> None:
        # `tasks/skills/ci-outcome.yaml` really carries `includes: [x, x]` on one row, and before
        # this the real train split produced 14 lines for 5 distinct spans — noise in a check whose
        # design rationale is not firing more than it has to.
        rows = [_leak_row("r1", [FileCheckCriterion(description="d", path="o.yml", includes=[_GRADED, _GRADED])])]
        assert len(candidate_leaks(f"we set {_GRADED} here", "", rows)) == 1

    def test_matching_is_case_insensitive(self) -> None:
        # Both consumers lower-case both sides; disagreeing about that would make "a leak" mean
        # two different things in the two rules that share this primitive.
        assert len(candidate_leaks(f"we set {_GRADED.upper()} here", "", _ROWS)) == 1

    def test_a_locator_value_in_the_candidate_does_not_flag(self) -> None:
        # Naming WHERE an artifact goes removes filename nondeterminism from the measurement
        # without revealing what is graded, so a body that names the path is not memorizing.
        rows = [_leak_row("r1", [FileCheckCriterion(description="d", path=".github/workflows/evals.yml")])]
        assert candidate_leaks("write it to .github/workflows/evals.yml", "", rows) == []

    def test_the_skills_own_name_does_not_flag(self) -> None:
        # CE036's `skill_name` exemption, pointed the other way: an outcome suite names the skill
        # in every row by design, and the skill's own body names itself constantly.
        #
        # The floor guard is the whole test. A name shorter than LEAK_MIN_CHARS returns [] whether
        # or not `skill_name` is exempt, so without it this passes on a rule that exempts nothing.
        # CE036's twin carries the identical assertion for the identical reason.
        assert len("optimize-skill") >= LEAK_MIN_CHARS, "fixture no longer exercises the floor"
        rows = [
            _leak_row("r1", [SkillTriggeredCriterion(description="d", skill_name="optimize-skill", expected_skill="")])
        ]
        assert candidate_leaks("This is the optimize-skill skill and it does things", "", rows) == []

    def test_the_criterion_type_does_not_flag(self) -> None:
        """`drop_type=True`, and it was found by measurement rather than by reasoning.

        Without it, `optimize-skill`'s own body — which discusses eval criteria at length — flags
        on `skill_triggered`, the criterion's DISCRIMINATOR rather than any graded content. A pure
        false positive on any skill whose body discusses evaluation.
        """
        rows = [_leak_row("r1", [SkillTriggeredCriterion(description="d", skill_name="s", expected_skill="")])]
        assert candidate_leaks("stack a skill_triggered criterion on every row", "", rows) == []

    def test_a_leak_nested_in_a_list_is_caught(self) -> None:
        rows = [_leak_row("r1", [FileCheckCriterion(description="d", path="o.yml", includes=["harmless-xx", _GRADED])])]
        assert len(candidate_leaks(f"body mentioning {_GRADED}", "", rows)) == 1

    def test_it_takes_no_min_chars_parameter(self) -> None:
        # One value, read from the constant. A parameter would be a second declaration of a
        # number CE036 and this function must agree on.
        assert "min_chars" not in inspect.signature(candidate_leaks).parameters


class TestLineageHeadScores:
    """The head lookup, which the skill's snippet used to do with a bare `next()`/`max()`."""

    def _measurements(self, *rounds: RoundScores) -> OptimizeMeasurements:
        return OptimizeMeasurements(skill="my-skill", round_scores=list(rounds))

    def test_none_when_no_round_recorded_a_head(self) -> None:
        rounds = RoundScores(round=1, arm_row_scores=[arm_row_scores_for("a", {"r1": 1.0})])
        assert lineage_head_scores(self._measurements(rounds)) is None

    def test_none_on_an_empty_sidecar(self) -> None:
        assert lineage_head_scores(self._measurements()) is None

    def test_takes_the_highest_round_that_named_one(self) -> None:
        # Highest ROUND, not last-in-list: the sidecar replaces per round, so ordering is a
        # write-order artefact while `round` is the real sequence.
        early = RoundScores(round=3, arm_row_scores=[arm_row_scores_for("a", {"r1": 1.0})], lineage_head="a")
        late = RoundScores(round=7, arm_row_scores=[arm_row_scores_for("b", {"r1": 0.5})], lineage_head="b")
        head = lineage_head_scores(self._measurements(late, early))
        assert head is not None and head.variant_id == "b"

    def test_skips_a_later_round_that_accepted_nothing(self) -> None:
        # A round with no accept leaves the head where it was; it must not blank the lineage.
        kept = RoundScores(round=2, arm_row_scores=[arm_row_scores_for("a", {"r1": 1.0})], lineage_head="a")
        quiet = RoundScores(round=3, arm_row_scores=[arm_row_scores_for("b", {"r1": 0.5})])
        head = lineage_head_scores(self._measurements(kept, quiet))
        assert head is not None and head.variant_id == "a"


class TestSearchCompare:
    """The search loop's accept/revert decision, which used to be arithmetic in a markdown block.

    Every guard here was previously a line an agent had to copy faithfully.
    """

    def test_a_better_candidate_is_accepted(self) -> None:
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", {"r1": 1.0, "r2": 1.0, "r3": 1.0, "r4": 0.0}),
        )
        assert result.beats and result.accepted
        assert result.head_score == 0.5 and result.candidate_score == 0.75
        assert result.shared_rows == ("r1", "r2", "r3", "r4")
        assert result.blocker is None

    def test_a_worse_candidate_is_not_accepted(self) -> None:
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", dict.fromkeys(SEARCH_HEAD_SCORES, 0.0)),
        )
        assert not result.beats and not result.accepted
        assert result.blocker is None, "losing is an ordinary result, not a blocked one"

    def test_a_tie_is_not_a_win(self) -> None:
        # Strictly greater. A tie that advanced the head would move the bar on an accident, and
        # the next round would then have to beat a number nothing earned.
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES), arm_row_scores_for("cand", dict(SEARCH_HEAD_SCORES))
        )
        assert result.head_score == result.candidate_score
        assert not result.beats and not result.accepted

    def test_no_shared_rows_is_a_wiring_blocker_not_a_hole(self) -> None:
        # Checked BEFORE holes: no overlap at all is a wiring fault, and reporting it as a hole
        # sends the reader looking for a flaky row instead of an unpinned sample seed.
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES), arm_row_scores_for("cand", {"other": 1.0})
        )
        assert not result.accepted and result.head_score is None and result.candidate_score is None
        assert result.blocker is not None and "sample_seed" in result.blocker

    def test_a_hole_refuses_rather_than_averaging_around_it(self) -> None:
        # The candidate errored on r2 — its mean over the survivors would be 1.0 and would "win".
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", {"r1": 1.0, "r3": 1.0, "r4": 1.0}),
        )
        assert not result.accepted and result.holes == ("r2",)
        assert result.head_score is None, "a refused comparison must report no number to misread"
        assert result.blocker is not None and "r2" in result.blocker

    def test_a_row_only_the_candidate_scored_is_not_a_hole(self) -> None:
        # Holes are asymmetric on purpose: an extra row the head never measured cannot make the
        # candidate look better, because the comparison runs over the intersection either way.
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", {**SEARCH_HEAD_SCORES, "r5": 1.0}),
        )
        assert result.holes == () and result.blocker is None
        assert result.shared_rows == ("r1", "r2", "r3", "r4")

    def test_a_corpus_regression_blocks_an_otherwise_winning_candidate(self) -> None:
        # THE reason the corpus is read here rather than at the next Stage A: an accept advances
        # the lineage, so a re-lost row rides forward until a multi-arm round notices.
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", {"r1": 0.0, "r2": 1.0, "r3": 1.0, "r4": 1.0}),
            corpus=corpus,
        )
        assert result.beats, "the aggregate really does improve — that is what makes this dangerous"
        assert not result.accepted
        assert [row.row_id for row, _ in result.regressions] == ["r1"]
        assert result.blocker is not None and "oblique phrasing" in result.blocker

    def test_a_corpus_row_the_candidate_holds_does_not_block(self) -> None:
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", {"r1": 1.0, "r2": 1.0, "r3": 1.0, "r4": 0.0}),
            corpus=corpus,
        )
        assert result.accepted and result.regressions == () and result.blocker is None

    def test_the_corpus_threshold_is_forwarded(self) -> None:
        # A fractional execution suite needs a bar other than 1.0; the parameter exists for it.
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="partial credit is fine")]
        candidate = arm_row_scores_for("cand", {"r1": 0.8, "r2": 1.0, "r3": 1.0, "r4": 1.0})
        assert not search_compare(arm_row_scores_for("head", SEARCH_HEAD_SCORES), candidate, corpus=corpus).accepted
        assert search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES), candidate, corpus=corpus, threshold=0.5
        ).accepted

    def test_a_losing_candidate_is_not_blocked_by_the_corpus(self) -> None:
        # It already failed on the score; adding a corpus blocker would misreport WHY.
        corpus = [RegressionRow(row_id="r1", promoted_in_round=1, reason="oblique phrasing")]
        result = search_compare(
            arm_row_scores_for("head", SEARCH_HEAD_SCORES),
            arm_row_scores_for("cand", dict.fromkeys(SEARCH_HEAD_SCORES, 0.0)),
            corpus=corpus,
        )
        assert not result.beats and not result.accepted and result.blocker is None

    def test_an_empty_head_is_a_blocker_not_a_crash(self) -> None:
        # `RoundScores`' validator makes this unreachable through the sidecar, but the function
        # is public and must not divide by zero on a hand-built arm.
        result = search_compare(arm_row_scores_for("head", {}), arm_row_scores_for("cand", {"r1": 1.0}))
        assert not result.accepted and result.blocker is not None


class TestAcceptedIsDerived:
    """`accepted` was a field every construction site set to `beats and blocker is None`.

    Two spellings of one rule, settable inconsistently by any caller, with nothing to notice.
    """

    def _comparison(self, *, beats: bool, blocker: str | None) -> SearchComparison:
        return SearchComparison(
            beats=beats,
            head_score=0.5,
            candidate_score=0.75,
            shared_rows=("r1",),
            holes=(),
            regressions=(),
            blocker=blocker,
        )

    def test_accepted_is_not_stored(self) -> None:
        assert "accepted" not in SearchComparison._fields
        assert len(SearchComparison._fields) == 7

    def test_a_winning_unblocked_candidate_is_accepted(self) -> None:
        assert self._comparison(beats=True, blocker=None).accepted is True

    def test_a_blocker_defeats_a_win(self) -> None:
        assert self._comparison(beats=True, blocker="a corpus regression").accepted is False

    def test_accepted_cannot_be_set_inconsistently(self) -> None:
        # The state a caller could previously have stored as `accepted=True`: it did not win.
        assert self._comparison(beats=False, blocker=None).accepted is False

    def test_replace_on_the_dropped_field_now_raises(self) -> None:
        # The failure mode the keyword-form construction sites exist to make loud rather than
        # silent — a positional build would have shifted every later argument instead.
        # `TypeError` on 3.13, not the `ValueError` older CPython raised here.
        with pytest.raises(TypeError, match="accepted"):
            self._comparison(beats=True, blocker=None)._replace(accepted=False)
