"""Lint tests: The activation and outcome suites the skills copy into a user's repo, and their fixtures."""

from pathlib import Path

import pytest

from tests.lint_tests.plugin_base import PluginArtifactsBase
from tests.lint_tests.shared import (
    PLUGIN_ROOT,
    _assert_outcome_suite_shape,
    _normalized,
    _outcome_metric_vocabulary,
)


@pytest.mark.lint
class TestTheShippedSuiteTemplates(PluginArtifactsBase):
    """The activation and outcome suites the skills copy into a user's repo, and their fixtures.

    One of five classes carved out of `TestPluginArtifacts`; the shared class attributes and
    grader helpers live on :class:`PluginArtifactsBase`.
    """

    def test_activation_template_expands_to_one_task_per_row(self):
        from coder_eval.orchestration.task_loader import expand_dataset, load_task

        task, _source_yaml = load_task(self.TEMPLATES / "activation.yaml")
        rows = expand_dataset(task, self.TEMPLATES)

        assert len(rows) == 6, f"expected one task per dataset row, got {len(rows)}"
        expected = sorted(c.expected_skill for row in rows for c in row.success_criteria)  # type: ignore[attr-defined]
        assert expected == ["", "", "", "my-skill", "my-skill", "my-skill"]
        for row in rows:
            # `initial_prompt` is `str | None` (a task may use `initial_prompt_file`), so
            # narrow it — otherwise moving the template's prompt to a file turns this
            # assertion into a TypeError instead of a readable failure.
            assert row.initial_prompt and "${row." not in row.initial_prompt, (
                f"unsubstituted or missing row placeholder in {row.task_id}"
            )

    def test_activation_template_thresholds_use_real_metric_keys(self):
        from coder_eval.criteria import CriterionRegistry, init_criteria
        from coder_eval.models import ClassificationCriterionResult, SkillTriggeredCriterion
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "activation.yaml")
        criterion = task.success_criteria[0]
        assert isinstance(criterion, SkillTriggeredCriterion)
        assert criterion.suite_thresholds, "the template must gate the suite on classification metrics"

        # Derive the available metric names by running the real aggregate, never
        # from a hardcoded list (which would re-declare the metric vocabulary).
        init_criteria(validate=False)
        checker = CriterionRegistry.get_checker("skill_triggered")()
        rows = [
            ClassificationCriterionResult(
                criterion_type="skill_triggered",
                description="d",
                score=1.0,
                observed_label=label,
                expected_label=label,
            )
            for label in ("yes", "no")
        ]
        aggregate = checker.aggregate(
            SkillTriggeredCriterion(description="d", skill_name="my-skill", expected_skill="my-skill"),
            rows,
        )
        assert aggregate is not None
        for metric in criterion.suite_thresholds:
            assert metric in aggregate.metrics, (
                f"suite_thresholds names {metric!r}, which the skill_triggered aggregate does not "
                f"emit (available: {sorted(aggregate.metrics)})"
            )

    def test_outcome_template_shape(self):
        # The execution track's instrument. Everything the shared helper asserts is
        # something that fails SILENTLY at full cost when it is wrong — see its docstring.
        _assert_outcome_suite_shape(
            self.TEMPLATES / "outcome.yaml",
            expected_rows=4,
            expected_split_counts={"train": 2, "test": 2},
            skill_name="my-skill",
            invocation="my-plugin:my-skill",
        )

    def test_outcome_template_rows_and_expectations_are_in_parity(self):
        # BOTH directions, because each fails differently and neither is loud. A row with no
        # expectations file scores a hard 0.0000 on every arm — indistinguishable from a
        # catastrophically bad body, and it cost a full 15-row run to find. An orphan expectations
        # file means a row was renamed and something is now silently ungraded.
        #
        # The sensor for the invariant `/coder-eval:task` step 6 states, not a restatement of it:
        # the shipped template is what every author copies, so it must satisfy its own rule.
        rows = set(self._shipped_row_ids())
        specs = set(self._shipped_expectations())
        assert rows == specs, (
            f"the shipped outcome template is out of parity: rows with no expectations file "
            f"{sorted(rows - specs)}, expectations files with no row {sorted(specs - rows)}"
        )

    def test_outcome_template_meets_its_own_check_floor(self):
        # Four DECLARED checks minimum. Below four a row's score takes at most five values and
        # behaves like a binary grader, which is how the execution gate's zero-variance refusal
        # gets manufactured. The template cannot teach a shape the skill forbids.
        for row_id, spec in self._shipped_expectations().items():
            checks = spec.get("checks")
            assert isinstance(checks, dict), f"{row_id}.json declares no `checks` object"
            assert len(checks) >= 4, (
                f"{row_id}.json declares {len(checks)} checks. `/coder-eval:task` step 6 requires "
                "four, so the shipped template would fail the rule it teaches"
            )

    def test_shipped_expectations_declare_real_checks(self):
        # Every shipped expectations file is a shape an author copies, and nothing else in the tree
        # reads them: a JSON syntax error or a check name absent from CHECKS would ship green.
        source = self.GRADER.read_text(encoding="utf-8")
        for row_id, spec in self._shipped_expectations().items():
            assert isinstance(spec.get("path"), str) and isinstance(spec.get("checks"), dict), (
                f"{row_id}.json is not an object with a string `path` and an object `checks`"
            )
            for key in spec["checks"]:
                name = key.split("#", 1)[0]
                assert f'"{name}": check_' in source, (
                    f"{row_id}.json declares check {name!r}, which verify.py's CHECKS table does not "
                    "register — an author copying this file gets a SKIP and a silently smaller denominator"
                )

            # And it must be CONTINUOUS: a row whose applicable checks number one can only score
            # 0.0 or 1.0, which is the zero-variance shape the execution gate refuses to rule on.
            # Weaker than the declared-check floor above on purpose — `json_field: {}` is the
            # documented opt-out, so a file may declare four and apply three.
            # FOUR, matching `task/SKILL.md`'s floor and `core-1.json`'s own `_comment`. At three
            # the sensor would let the template drift below the rule it teaches while staying green.
            applicable = [k for k, params in spec["checks"].items() if params]
            assert len(applicable) >= 4, (
                f"{row_id}.json declares only {len(applicable)} APPLICABLE checks, so it teaches a "
                "near-binary grader — the defect `score_from_stdout` was chosen to avoid"
            )

    def test_outcome_template_grader_slot_is_continuous(self):
        # Through the real loader, not a grep: the point is that the criterion the models BUILD
        # carries `score_from_stdout`, not that the file contains the string. A binary grader over
        # a dozen rows manufactures the execution gate's zero-variance refusal — two arms of
        # different quality score identically and the gate reports it cannot separate them.
        from coder_eval.models import RunCommandCriterion
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "outcome.yaml")
        graders = [c for c in task.success_criteria if isinstance(c, RunCommandCriterion)]
        assert graders, "the outcome template ships no `run_command` grader slot"
        for grader in graders:
            assert grader.score_from_stdout, (
                f"the grader slot {grader.description!r} is BINARY. `score_from_stdout: true` is "
                "what gives the execution gate a continuous per-row score to compare"
            )
            assert "${row.id}" in grader.command, (
                "the grader command does not interpolate ${row.id}, so every row would be graded "
                "against the same expectations file"
            )
            # PORTABLE, not absolute. `$TASK_DIR` is exported into every `run_command` and is
            # mounted symmetrically under `driver: docker`; a hardcoded host path exists on the
            # author's machine only, and its absence scores 0.0 on every row of every arm — which
            # reads exactly like a skill whose body is bad.
            assert "$TASK_DIR/" in grader.command, (
                f"the grader command {grader.command!r} does not address the script through "
                "$TASK_DIR. An absolute host path breaks on a colleague's machine, in CI, and "
                "under driver: docker, and all three failures look like a bad skill body"
            )

    def test_outcome_template_scores_artifacts_not_prose(self):
        # "Score outcomes, not prose" is the execution track's core instruction, and the
        # template is what everyone copies. An LLM judge adds variance to the very number
        # the gate reads, so a template that reached for one would teach the opposite of
        # what optimize-skill says — and the added noise would be invisible in the result.
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "outcome.yaml")
        types = {c.type for c in task.success_criteria}

        artifact_scoring = {"file_check", "json_check", "run_command", "cli_called", "command_executed"}
        assert types & artifact_scoring, (
            f"the outcome template's criteria are {sorted(types)} — none of them scores a real "
            f"artifact or command ({sorted(artifact_scoring)}), which is the whole difference "
            "between an outcome suite and an activation probe"
        )
        assert not types & {"llm_judge", "agent_judge"}, (
            f"the outcome template reaches for a judge ({sorted(types & {'llm_judge', 'agent_judge'})}). "
            "Judges add variance to the number the A/B gate reads; the template is the copied "
            "default and must demonstrate deterministic scoring"
        )

    def test_outcome_template_thresholds_use_real_metric_keys(self):
        # A threshold naming a metric nothing emits is not a loose gate — `_attach_row_accounting`
        # records it with `actual_value=None` and `passed=False`, so the suite fails forever and
        # the cause is a typo. Derived from real calls to BOTH sources; see the helper.
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "outcome.yaml")

        gated = [c for c in task.success_criteria if c.suite_thresholds]
        assert gated, "the outcome template must gate the suite on something, or the round has no verdict"
        for criterion in gated:
            available = _outcome_metric_vocabulary(criterion.type)
            for metric in criterion.suite_thresholds:
                assert metric in available, (
                    f"suite_thresholds names {metric!r}, which no aggregate emits for "
                    f"{criterion.type!r} (available: {sorted(available)})"
                )

    def test_activation_template_caps_turns_and_isolates(self):
        # The template preaches both of these and used to ship neither, so a user who copied
        # it got the opposite of the advice they were reading.
        #
        # The cap is about SIGNAL, not only cost: activation is decided in the first
        # assistant turn, and an uncapped row spends turns exploring a sandbox that
        # deliberately holds no eval files. A row that times out is EXCLUDED from the
        # confusion matrix rather than scored, so it never shows up as a bad number — only
        # as a denominator that quietly shrank.
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "activation.yaml")
        limits = task.run_limits
        assert limits is not None and limits.max_turns == 2, (
            "the activation template must cap max_turns at 2, matching the worked example in "
            "tasks/skills/lint-tasks-activation.yaml — activation is decided in the first "
            "assistant turn, and an uncapped row erodes the confusion-matrix denominator"
        )
        assert limits.turn_timeout == 120 and limits.task_timeout == 300, (
            "the activation template's timeouts must match the worked example key for key"
        )
        assert task.agent is not None and task.agent.setting_sources == [], (
            "the activation template must set `agent.setting_sources: []`. This suite measures "
            "the skill LISTING; inheriting the host project's CLAUDE.md injects a large project "
            "guide into every call — expensive, and a confound on the thing being measured"
        )

    def test_outcome_template_caps_cost(self):
        # An outcome row is a FULL task run, so the template needs a per-row COST brake the
        # activation template does not. Both templates now carry `run_limits:`, but they cap
        # for opposite reasons — activation for signal (see the test above), outcome for
        # spend — so this stays an ABSOLUTE floor rather than a comparison against the other
        # file, which would encode a relationship that does not exist.
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "outcome.yaml")
        limits = task.run_limits
        assert limits is not None, "the outcome template ships no `run_limits:` — every row is a full task run"
        assert limits.max_usd is not None, (
            "the outcome template sets no `max_usd`. It is the per-row cost brake; without it a "
            "single runaway row can consume a whole stage's budget"
        )
        assert limits.max_turns is not None and limits.max_turns >= 15, (
            f"the outcome template caps max_turns at {limits.max_turns}. An activation suite caps "
            "at 2 on purpose (activation is decided in the first assistant turn), but an outcome "
            "row needs a whole task's budget — a row truncated by an activation-sized cap scores "
            "as a body failure that never happened"
        )

    def test_outcome_rows_are_all_positive(self):
        # The INVERSE of the activation template's polarity rule, and the two must not be
        # confused. An activation suite needs distractors on both sides of the split; an
        # outcome suite holds activation CONSTANT, so every row is a positive and a row with
        # `expected_skill: ""` would assert the skill must not engage — inverting the premise.
        import json

        from coder_eval.orchestration.task_loader import row_split_label

        rows = [
            json.loads(line)
            for line in (self.TEMPLATES / "outcome-rows.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows, "the outcome template ships no rows"
        # `row_split_label`, not `r.get("split")`: truthiness would report a legitimate
        # `"split": 0` as unlabelled, which is a SECOND definition of "labelled" competing
        # with the runtime's. CE060 exists precisely to have one.
        assert all(row_split_label(r, "split") is not None for r in rows), (
            "every template row must carry a `split` — a PARTLY labelled dataset is the one bad "
            "state: --split keeps the matching rows and drops the unlabelled ones, shrinking the "
            "suite the metrics are computed over"
        )
        assert len({str(r["split"]) for r in rows}) >= 2, "outcome template rows collapsed to a single split"
        blank = [r["id"] for r in rows if not r.get("expected_skill")]
        assert not blank, (
            f"outcome rows {blank} have an empty `expected_skill`, which asserts the skill must NOT "
            "engage. The execution track holds activation constant — every row is a positive"
        )

    def test_checked_in_outcome_sample_matches_the_shipped_template_shape(self):
        # `tasks/skills/ci-outcome.yaml` is the execution track's worked example, standing
        # to it exactly as lint-tasks-activation.yaml stands to the activation track — and
        # it is the suite the real A/B round runs against. Asserted through the SAME helper
        # as the bundled template, so the shipped shape and the worked example cannot drift
        # into disagreeing about what an outcome suite is.
        #
        # Note this establishes the guard rather than extending one: there is currently no
        # equivalent for the checked-in activation sample either.
        # (`test_lint_tasks_does_not_flag_the_shipped_activation_template` is a different
        # thing — it guards lint-tasks' carve-out against the BUNDLED template and never
        # reads tasks/.)
        sample = self.REPO_ROOT / "tasks" / "skills" / "ci-outcome.yaml"
        _assert_outcome_suite_shape(
            sample,
            expected_rows=10,
            expected_split_counts={"train": 6, "test": 4},
            skill_name="ci",
            invocation="coder-eval:ci",
        )

        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(sample)
        artifact_scoring = {"file_check", "json_check", "run_command", "cli_called", "command_executed"}
        assert {c.type for c in task.success_criteria} & artifact_scoring, (
            "the checked-in outcome sample scores nothing on disk — an outcome suite that "
            "asserts only engagement is an activation suite with a bigger bill"
        )

        # Every row must supply every ${row.*} field the criteria reference. Criteria are
        # copied to EVERY row, so a field only some rows carry raises KeyError mid-expansion
        # — a failure that costs nothing here and a whole stage's setup at run time. This is
        # also what makes a second `includes` slot safe to add: it is only optional-looking.
        import json
        import re

        referenced = set(
            re.findall(
                r"\$\{row\.([A-Za-z_][A-Za-z0-9_]*)\}",
                (self.REPO_ROOT / "tasks" / "skills" / "ci-outcome.yaml").read_text(),
            )
        )
        rows = [
            json.loads(line)
            for line in (sample.parent / "ci-outcome-rows.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for row in rows:
            missing = referenced - set(row)
            assert not missing, (
                f"row {row.get('id')!r} is missing {sorted(missing)}, referenced as ${{row.*}} in the suite"
            )

    def test_checked_in_outcome_fixture_lets_the_skill_act(self):
        # The fixture is load-bearing, and every way it can be wrong is SILENT at full cost.
        # `ci` stops outright on a repo with no `.github/`, so a fixture missing it scores
        # zero on every row of every arm — which ties an A/B round at the floor and reads
        # exactly like three bad candidates. And a fixture workflow that mentions the
        # harness by name flips `ci` into its "one already runs coder-eval, do not add a
        # second" branch, so every row would measure the refusal path instead.
        from coder_eval.orchestration.task_loader import load_task

        sample = self.REPO_ROOT / "tasks" / "skills" / "ci-outcome.yaml"
        task, _ = load_task(sample)
        assert task.sandbox is not None and task.sandbox.template_sources, (
            "ci-outcome.yaml mounts no fixture. Row substitution never reaches `sandbox:`, so "
            "the fixture is the ONLY starting repository all 10 rows get — without one the "
            "agent lands in an empty sandbox and `ci` refuses on every row"
        )
        fixture = Path(task.sandbox.template_sources[0].path)  # type: ignore[attr-defined]
        assert fixture.is_dir(), f"the mounted fixture {fixture} does not exist"

        workflows = sorted((fixture / ".github" / "workflows").glob("*.yml"))
        assert workflows, (
            f"{fixture} has no .github/workflows/*.yml. `ci` says so explicitly: 'If there is "
            "no .github/ directory at all, say that this skill targets GitHub Actions and "
            "stop' — so every row of every arm would score zero on a refusal"
        )
        for workflow in workflows:
            assert "coder_eval" not in workflow.read_text(encoding="utf-8"), (
                f"{workflow.name} names `coder_eval`, which trips `ci`'s 'a workflow already "
                "runs coder-eval — do not add a second one' branch. Every row would then "
                "measure the refusal path rather than the emission path"
            )

        # The eval tree the rows expect the agent to discover: deliberately `evals/` rather
        # than `tasks/` (discovery is exercised, not a lucky guess) and at two depths, so a
        # workflow using a `**` glob — which degrades to one level with globstar off — is
        # detectably wrong rather than indistinguishable from a correct one.
        # TASK depths only. Counting `evals/experiments/` would let this pass with
        # `evals/suite/json-shape.yaml` — the file the assertion is entirely about — deleted.
        depths = {
            p.relative_to(fixture).parent.as_posix()
            for p in (fixture / "evals").rglob("*.yaml")
            if "experiments" not in p.parts
        }
        assert len(depths) >= 2, (
            f"the fixture's eval tree sits at a single depth ({sorted(depths)}). `ci` forbids "
            "`**` in the tasks: input because globstar is off and it silently drops a level — "
            "with one depth, a candidate that ignores that rule scores identically to one that "
            "follows it"
        )

    def test_activation_template_makes_the_skill_reachable(self):
        # The suite runs in a fresh sandbox holding none of the user's files, so without a
        # plugin source the agent is never OFFERED the skill: every positive row scores 0,
        # `recall.yes` trips the template's own suite_thresholds, and Step 7 then reports
        # "the description under-claims" — a confident, entirely fabricated diagnosis of a
        # skill that was simply absent. `test_activation_template_expands_to_one_task_per_row`
        # passes either way, so this is the only thing standing between a scaffolded suite
        # and a guaranteed-meaningless number.
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "activation.yaml")
        assert task.agent is not None and task.agent.plugins, (
            "the activation template must declare `agent.plugins` naming where the skill under "
            "test lives — without it every positive row scores 0 and the suite reports recall 0.0"
        )
        paths = [p.get("path", "") for p in task.agent.plugins]
        assert any("$" in p for p in paths), (
            f"the template's plugin path(s) {paths} should come from an environment variable — "
            "the suite is committed and re-run on other machines, so an absolute path bakes in "
            "one developer's layout"
        )

    def test_activation_rows_have_both_polarities(self):
        import json

        rows = [
            json.loads(line)
            for line in (self.TEMPLATES / "activation-rows.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        labels = {row["expected_skill"] for row in rows}
        assert any(label for label in labels), "no positive rows — recall would be undefined"
        assert "" in labels, "no distractor rows — precision is 1.0 by definition and meaningless"

    def test_activation_rows_split_both_polarities_both_sides(self):
        # `optimize-skill` and the template both require both polarities on BOTH sides of
        # the split: a test of only positives measures recall and calls it a result, and
        # a train half with no distractors cannot see a candidate over-claiming. The shipped
        # template is the worked example everyone copies, so the balance it demonstrates has
        # to hold — an edit that moved one row could break it silently.
        import json

        from coder_eval.orchestration.task_loader import row_split_label

        rows = [
            json.loads(line)
            for line in (self.TEMPLATES / "activation-rows.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # `row_split_label`, not `r.get("split")`: truthiness would report a legitimate
        # `"split": 0` as unlabelled, which is a SECOND definition of "labelled" competing
        # with the runtime's. CE060 exists precisely to have one.
        assert all(row_split_label(r, "split") is not None for r in rows), (
            "every template row must carry a `split` — a PARTLY labelled dataset is the one "
            "bad state: --split keeps the matching rows and drops the unlabelled ones, "
            "shrinking the suite the metrics are computed over"
        )
        by_split: dict[str, set[bool]] = {}
        for r in rows:
            by_split.setdefault(str(r["split"]), set()).add(bool(r["expected_skill"]))
        assert len(by_split) >= 2, f"template rows collapsed to a single split: {sorted(by_split)}"
        for split, polarities in sorted(by_split.items()):
            assert polarities == {True, False}, (
                f"split {split!r} carries only {'positive' if True in polarities else 'distractor'} "
                "rows — both splits need positives AND distractors, or one half of the "
                "train/test comparison measures nothing"
            )

    def test_outcome_template_still_loads_after_the_control_comment(self):
        # The control note is a COMMENT, and this is the inertness sensor: the comment is present
        # AND the template still parses to the same structure through the real loader and expander.
        # Without the first half it would be a copy of test_outcome_template_shape, green whether
        # or not the thing it is named for exists.
        template = PLUGIN_ROOT / "reference" / "templates" / "outcome.yaml"
        raw = template.read_text(encoding="utf-8")
        # Comment markers stripped and whitespace collapsed, so a rewrap cannot defeat the check.
        prose = " ".join(raw.replace("#", " ").split())
        for token in ("CONTROL ARM", "BODY EMPTIED", "ONCE PER SUITE"):
            assert token in prose, (
                f"the bundled outcome template no longer says {token!r}, so a user starting from it "
                "is never told to establish that the body does measurable work before optimizing it"
            )
        # And it stayed a comment: every line mentioning it must be one.
        offenders = [ln for ln in raw.splitlines() if "CONTROL ARM" in ln or "EMPTIED" in ln]
        assert offenders and all(ln.lstrip().startswith("#") for ln in offenders), (
            f"the control note is no longer comment-only ({offenders}) — it must not change the "
            "parsed template, which is what the shape assertion below verifies"
        )
        _assert_outcome_suite_shape(
            PLUGIN_ROOT / "reference" / "templates" / "outcome.yaml",
            expected_rows=4,
            expected_split_counts={"train": 2, "test": 2},
            skill_name="my-skill",
            invocation="my-plugin:my-skill",
        )

    def test_the_execution_track_diffs_a_tree(self):
        # A `SKILL.md`-only diff hides a scripts-only candidate entirely — which is now a legal and
        # encouraged shape, so the presentation step has to render it.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        step_12 = skill[skill.index("## Step 12") : skill.index("## Step 13")]
        assert "skill DIRECTORY" in step_12, (
            "optimize-skill's Step 12 no longer diffs the whole skill directory on the execution "
            "track, so a candidate whose whole hypothesis is a bundled script renders as no change"
        )
        assert "diff -ru" in step_12, "Step 12 should name the diff shape, not only ask for one"
