"""Lint tests: task surfaces."""

import ast
from pathlib import Path
from typing import ClassVar

import pytest

from tests.lint.task_yaml_discovery import all_yaml, task_yamls
from tests.lint_tests.shared import REPO_ROOT, TESTS_ROOT, _dataset_task, _normalized


# The repo's own task files, discovered ONCE and through the shared reader.
#
# `task_yamls` parses for a top-level `task_id:` and globs BOTH extensions, which is why it exists:
# these three rules hand-rolled `rglob("*.yaml")`, so a `.yml` task was invisible to every one of
# them — measured, a `tasks/probe.yml` whose prompt leaked a graded value produced zero CE061 cases
# and a green `make lint`. CLAUDE.md calls this reader "the ONE answer to which YAML files under this
# tree are tasks" for exactly that reason.
_REPO_TASKS = sorted(task_yamls(REPO_ROOT / "tasks"))
# A path break must FAIL rather than silently collect nothing — the CE044/CE045 lesson, and the
# reason CE052 one directory over carries the same assert.
assert _REPO_TASKS, "no task YAML found under tasks/ — these three rules would check nothing"


@pytest.mark.lint
class TestCE034ArmedPositiveRequiresSuccess:
    """CE034 — an armed, live-passable `command_executed` must require success.

    `require_success` defaults to False, so a criterion counts an invocation that
    CRASHED. On an unarmed criterion that is merely generous. On an armed one it
    corrupts the run's verdict, because three behaviours compose:

    1. `live_verdict` and `_check_impl` share `_matching_commands`, so a failed
       invocation live-PASSES a positive criterion (`min_count > 0`, no
       `max_count`) the moment it is observed;
    2. `stop_early.on_pass: stop` ends the run on that pass — and
       `decide_within` latches it, so the timeout never fires either;
    3. gating is FIRED-ONLY: a run the watcher cut gates on the ARMED SUBSET
       (`armed_criteria_passed`), so unarmed criteria are never consulted.

    Net effect on `tasks/early_stop_weighted_low_weight_absorbed.yaml` before this
    rule existed: an agent that ran `python app.py` BEFORE creating app.py scored a
    weighted 1.0 over the armed subset and reported SUCCESS — with no app.py and a
    crashed script — because the unarmed `file_exists` was bypassed. Found by
    running the plugin's own `lint-tasks` skill against this repository's tasks.

    Only *pass-capable* instances are constrained, read off the model's own
    `live_decidable_polarities()` rather than re-deriving the shape here. A
    negative assertion (`min_count: 0, max_count: 0`, i.e. "must NOT call curl")
    is fail-only and must NOT set `require_success`: a curl that failed is still a
    curl that was called, and requiring success there would blind the criterion to
    exactly the calls it exists to forbid.
    """

    ROOT = REPO_ROOT

    @staticmethod
    def _offenders(task) -> list[str]:
        """Armed, pass-capable command_executed criteria that don't require success."""
        from coder_eval.models import CommandExecutedCriterion

        return [
            c.description
            for c in task.success_criteria
            if isinstance(c, CommandExecutedCriterion)
            and c.stop_early is not None
            and "pass" in c.live_decidable_polarities()
            and not c.require_success
        ]

    @pytest.mark.parametrize(
        "path",
        _REPO_TASKS,
        ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
    )
    def test_repo_tasks_arm_only_success_requiring_positives(self, path: Path):
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(path)
        offenders = self._offenders(task)
        assert not offenders, (
            f"{path}: armed criteria {offenders} can live-PASS on an invocation that FAILED "
            "(require_success defaults to False). Under FIRED-ONLY armed gating that reports "
            "SUCCESS while bypassing every unarmed criterion. Set `require_success: true`."
        )

    def test_detects_an_armed_positive_without_require_success(self):
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            success_criteria=[
                {
                    "type": "command_executed",
                    "description": "ran the script",
                    "command_pattern": "python app\\.py",
                    "min_count": 1,
                    "stop_early": {"on_pass": "stop"},
                }
            ],
        )
        assert self._offenders(task) == ["ran the script"]

    def test_fail_only_negative_is_not_constrained(self):
        # The distractor shape: fail-only, so it can never live-PASS on a crashed
        # command, and requiring success would hide the forbidden calls it hunts.
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            success_criteria=[
                {
                    "type": "command_executed",
                    "description": "never called curl",
                    "command_pattern": "curl",
                    "min_count": 0,
                    "max_count": 0,
                    "stop_early": {},
                }
            ],
        )
        assert self._offenders(task) == []

    def test_unarmed_positive_is_not_constrained(self):
        # No stop_early block => not armed => a generous default cannot truncate a
        # run or bypass a gate, so this rule deliberately says nothing about it.
        from coder_eval.models import TaskDefinition

        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            success_criteria=[
                {
                    "type": "command_executed",
                    "description": "ran the script",
                    "command_pattern": "python app\\.py",
                    "min_count": 1,
                }
            ],
        )
        assert self._offenders(task) == []


@pytest.mark.lint
class TestCE061RowPromptsDoNotLeakWhatTheyGrade:
    """CE061 — a dataset row's prompt must not contain the value a criterion grades it on.

    This repo's own task rubric (and the `lint-tasks` skill) call a prompt that supplies its
    own answer the most common way a suite scores well while measuring nothing. `lint-tasks`
    applies that rule to a *user's* files; nothing applied it to this repository's, and a
    checked-in worked example shipped with four such rows.

    Scope, stated plainly: this catches the **verbatim** form — the prompt literally contains
    the string a criterion asserts. It cannot catch a *semantic* leak, where the prompt
    describes the graded behaviour in different words ("list the paths explicitly rather than
    with a recursive wildcard" while grading an explicit glob). That form needs a reader, and
    is what `lint-tasks` and code review are for. Guarding the blunt case is still worth it:
    it is the easy mistake, and it is silent.

    One deliberate collision, documented rather than exempted: a literal (non-regex)
    `command_executed.command_pattern` of >= LEAK_MIN_CHARS echoed verbatim in a
    prompt IS flagged. That is correct — a pattern asserting *what ran* is graded
    behaviour, not a locator. The `command` exemption covers `run_command.command`, the
    command the CHECKER runs, which is a different field on a different criterion. No
    in-repo task has the collision, so exempting `command_pattern` would be an unused
    exemption weakening a real check.

    The detection PRIMITIVE lives in `coder_eval.leak_detection`, shared with
    `optimize.search.candidate_leaks`, which asks the same question pointed the other way. The
    containment direction below is all this rule adds to it.
    """

    @classmethod
    def _offenders(cls, task, task_file_dir: Path) -> list[str]:
        """Every verbatim leak in the task's expanded rows.

        The rule's whole detection body lives here so the fixtures below and the repo scan
        exercise the SAME code. Split, the repo scan would keep passing identically whether
        or not the rule could still detect anything.

        `drop_type=False`: unlike a skill body, a row PROMPT containing "skill_triggered" is
        itself worth flagging.
        """
        from coder_eval.leak_detection import graded_strings
        from coder_eval.orchestration.task_loader import expand_dataset

        offenders: list[str] = []
        for row in expand_dataset(task, task_file_dir):
            prompt = (row.initial_prompt or "").lower()
            if not prompt:
                continue
            for criterion in row.success_criteria:
                for value in graded_strings(criterion, drop_type=False):
                    if value.lower() in prompt:
                        offenders.append(f"{row.task_id}: prompt contains {value!r} ({criterion.type})")
        return offenders

    @pytest.mark.parametrize(
        "path",
        _REPO_TASKS,
        ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
    )
    def test_repo_task_prompts_do_not_contain_the_graded_string(self, path: Path):
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(path)
        if task.dataset is None:
            pytest.skip("no dataset: block — nothing is row-substituted")

        offenders = self._offenders(task, path.parent)
        assert not offenders, (
            f"{path}: the prompt hands the agent the exact string a criterion grades it on, so "
            f"the row scores well whether or not the behaviour under test happened — and an "
            f"A/B arm that DELETED that behaviour would still pass. Describe the situation and "
            f"let the skill or the agent supply the method.\n\n" + "\n".join(f"  {o}" for o in offenders)
        )

    def test_detects_a_leaking_row(self, tmp_path: Path):
        from coder_eval.models import FileCheckCriterion

        task = _dataset_task(
            [{"id": "a"}],
            prompt="Write a workflow that sets minimum-task-score to 0.8",
            criteria=[FileCheckCriterion(description="d", path="out.yml", includes=["minimum-task-score"])],
        )
        offenders = self._offenders(task, tmp_path)
        assert len(offenders) == 1 and "minimum-task-score" in offenders[0]

    def test_locator_fields_are_exempt(self, tmp_path: Path):
        # Naming WHERE the artifact goes removes filename nondeterminism from the
        # measurement; it reveals nothing about what the artifact must contain.
        from coder_eval.models import FileCheckCriterion

        task = _dataset_task(
            [{"id": "a"}],
            prompt="Write it to .github/workflows/evals.yml",
            criteria=[FileCheckCriterion(description="d", path=".github/workflows/evals.yml")],
        )
        assert self._offenders(task, tmp_path) == []

    def test_skill_name_is_exempt(self, tmp_path: Path):
        # THE regression this exemption exists for. `skill_name` names WHICH skill must
        # engage; the graded thing is the engagement EVENT, which no prompt can supply —
        # and the outcome-suite pattern this plugin prescribes puts the skill name in every
        # prompt by design. Without the exemption, the first repo-committed outcome suite
        # for a skill whose name reaches the length floor fails CE061 on its own engagement
        # criterion.
        from coder_eval.leak_detection import LEAK_MIN_CHARS
        from coder_eval.models import SkillTriggeredCriterion

        assert len("optimize-skill") >= LEAK_MIN_CHARS, "fixture no longer exercises the floor"
        task = _dataset_task(
            [{"id": "a"}],
            prompt="Use the optimize-skill skill to improve this description",
            criteria=[SkillTriggeredCriterion(description="d", skill_name="optimize-skill", expected_skill="")],
        )
        assert self._offenders(task, tmp_path) == []

    @pytest.mark.parametrize(("delta", "flagged"), [(-1, False), (0, True)])
    def test_the_length_floor_is_inclusive(self, tmp_path: Path, delta: int, flagged: bool):
        # Both sides of the `>=`, which is the part a refactor actually breaks. The strings
        # are DERIVED from LEAK_MIN_CHARS rather than spelled out: a hardcoded 11 and
        # 12 would be a second declaration of the same number, which is the drift this
        # fixture exists to prevent. (The literal VALUE of the floor is not pinned here on
        # purpose — it is a tuning knob; what must not move silently is the comparison.)
        from coder_eval.leak_detection import LEAK_MIN_CHARS
        from coder_eval.models import FileCheckCriterion

        value = "x" * (LEAK_MIN_CHARS + delta)
        task = _dataset_task(
            [{"id": "a"}],
            prompt=f"The answer is {value}",
            criteria=[FileCheckCriterion(description="d", path="out.yml", includes=[value])],
        )
        assert bool(self._offenders(task, tmp_path)) is flagged

    def test_description_is_not_scanned(self, tmp_path: Path):
        # `description` is a label. It routinely echoes the scenario and grades nothing, so
        # scanning it would flag every well-named criterion in the repo.
        from coder_eval.models import FileCheckCriterion

        task = _dataset_task(
            [{"id": "a"}],
            prompt="emit the deployment manifest",
            criteria=[FileCheckCriterion(description="emit the deployment manifest", path="out.yml")],
        )
        assert self._offenders(task, tmp_path) == []

    def test_nested_string_leaves_are_scanned(self, tmp_path: Path):
        # Pins `leak_detection.string_leaves`' recursion: a leak in the SECOND entry of a list must be
        # caught, or a rule that only looked at scalar fields would pass this repo's suites
        # while missing every `includes:` leak — the commonest shape there is.
        from coder_eval.models import FileCheckCriterion

        task = _dataset_task(
            [{"id": "a"}],
            prompt="the file must mention permissions-boundary",
            criteria=[
                FileCheckCriterion(description="d", path="out.yml", includes=["harmless", "permissions-boundary"])
            ],
        )
        offenders = self._offenders(task, tmp_path)
        assert len(offenders) == 1 and "permissions-boundary" in offenders[0]

    def test_ce061_exemption_list_matches_claude_md(self):
        # LEAK_LOCATOR_FIELDS is the single source; CLAUDE.md's CE061 sentence is derived.
        # Both directions, because the list already drifted once: CLAUDE.md named three of
        # the four fields the code exempted, and nothing noticed. The repo automates exactly
        # this class elsewhere (CE028 for the docs indexes, CE033 for the plugin reference).
        import re

        from coder_eval.leak_detection import LEAK_LOCATOR_FIELDS

        text = _normalized(REPO_ROOT / "CLAUDE.md")
        sentence = next((s for s in text.split(". ") if "Location fields" in s), None)
        assert sentence is not None, "CLAUDE.md no longer states CE061's exemption list"
        # The PARENTHESISED list only. Reading every backticked name in the whole sentence let a
        # field the sentence ALSO mentions in its trailing clause — `skill_name` does — be deleted
        # from the exemption list with this still passing (measured).
        listed = re.search(r"Location fields \(([^)]*)\)", sentence)
        assert listed is not None, "CLAUDE.md's CE061 sentence no longer parenthesises the list"
        backticked = set(re.findall(r"`([a-z_]+)`", listed.group(1)))
        assert set(LEAK_LOCATOR_FIELDS) <= backticked, (
            f"CLAUDE.md's CE061 sentence omits {sorted(set(LEAK_LOCATOR_FIELDS) - backticked)}"
        )
        assert backticked <= set(LEAK_LOCATOR_FIELDS), (
            f"CLAUDE.md's CE061 sentence names {sorted(backticked - set(LEAK_LOCATOR_FIELDS))} as exempt, "
            f"which the rule does not exempt"
        )


@pytest.mark.lint
class TestCE052TemplateTasksLoad:
    """CE052 — every task YAML under `templates/` must load through the real `load_task`.

    A `@pytest.mark.lint` class rather than a `BaseRule`: it reasons over YAML trees and needs the
    loader itself, not one `.py` AST at a time — the same shape as CE060/CE061.

    **What it caught.** `templates/ci-outcome-fixture/evals/activation.yaml` declared
    `suite_thresholds: {recall.yes: 0.7}` with no `dataset:` block, which
    `check_suite_thresholds_require_dataset` rejects. Nothing referenced the fixture — `grep -rn
    ci-outcome-fixture tests/ src/ Makefile .github/` returned nothing — so it had never been
    loaded by anything. The damage is not a red build: `resolve_all_tasks` isolates a malformed
    task into `skipped_tasks` and the run prints a yellow warning, so the emitted workflow would
    have run GREEN while silently skipping the one suite that motivates the fixture's
    `SKILL_SOURCE_PATH` passthrough.

    **DISCOVERY, not an enumerated list**, so a future fixture tree is covered on arrival — the
    property that would have covered this one. A file is a task when it carries a top-level
    `task_id:`; everything else (the fixture's `evals/experiments/default.yaml`, which
    `tasks/skills/ci-outcome.yaml` licenses as "not a valid task") is skipped by CONTENT rather
    than by filename, and the skip is asserted rather than silent.

    The discovery itself lives in `tests/lint/task_yaml_discovery.py` — a shared reader on the
    `markdown_tables.py` / `import_resolution.py` precedent — because
    `tests/test_task_yaml_discovery.py` asks the identical question of `tasks/`. Both halves
    (parse rather than regex; glob BOTH extensions) were got wrong independently before they
    were shared, and each failure mode is a task silently vanishing from the set.

    **Complements `TestPluginArtifacts`, does not subsume it.** That class asserts far more about
    `plugins/coder-eval/reference/templates/` — row counts through `expand_dataset`, criterion
    shapes, suite-threshold wiring. This asserts only loadability, over a DISCOVERED set. They
    overlap on two files and neither contains the other; both stay.
    """

    TEMPLATES = REPO_ROOT / "templates"

    def _all_yaml(self) -> list[Path]:
        return all_yaml(self.TEMPLATES)

    def _task_yamls(self) -> list[Path]:
        return task_yamls(self.TEMPLATES)

    def test_the_discovery_set_is_not_empty(self) -> None:
        """Anti-vacuity, and the CE044/CE045 lesson: a moved directory must report a GAP.

        Without this the whole class passes by finding nothing, which is exactly the state a
        renamed `templates/` would produce.
        """
        assert self.TEMPLATES.is_dir(), "templates/ moved — CE052 would be checking nothing"
        discovered = self._task_yamls()
        assert discovered, "no task YAML discovered under templates/ — CE052 is vacuous"

    def test_every_template_task_loads(self) -> None:
        from coder_eval.orchestration.task_loader import load_task

        failures: list[str] = []
        for path in self._task_yamls():
            try:
                load_task(path)
            except Exception as exc:  # the report names the file and the reason
                failures.append(f"{path.relative_to(self.TEMPLATES.parent)}: {exc}")
        assert not failures, "template task YAML failed to load:\n" + "\n".join(failures)

    def test_every_non_task_yaml_is_skipped_by_content_and_named_here(self) -> None:
        """Both known non-tasks are named, so the skip is VISIBLE rather than a silent subtraction.

        Enumerating them is the point: the content check must not be free to start skipping real
        tasks. `tasks/skills/ci-outcome.yaml` licenses the experiment file as "not a valid task";
        the workflow is a GitHub Actions file that happens to live under the fixture, and it only
        became visible here once discovery stopped missing `.yml` entirely.
        """
        non_tasks = {
            self.TEMPLATES / "ci-outcome-fixture" / "evals" / "experiments" / "default.yaml",
            self.TEMPLATES / "ci-outcome-fixture" / ".github" / "workflows" / "lint.yml",
        }
        for path in non_tasks:
            assert path.is_file(), f"fixture moved: {path}"
        assert set(self._task_yamls()) == set(self._all_yaml()) - non_tasks, (
            "an unexpected YAML is being skipped, or a known non-task is now being loaded"
        )

    def test_the_activation_fixture_carries_both_polarities(self) -> None:
        """The defect this phase fixed, pinned as behaviour rather than as "it loads".

        `suite_thresholds: {recall.yes: ...}` is a gate on an across-row metric, so the suite
        needs a dataset — and it needs a DISTRACTOR row too: recall over positives alone is
        satisfiable by a skill that fires on everything.
        """
        from coder_eval.orchestration.task_loader import expand_dataset, load_task

        path = self.TEMPLATES / "ci-outcome-fixture" / "evals" / "activation.yaml"
        task, _source_yaml = load_task(path)
        assert task.dataset is not None, "a suite_thresholds criterion needs a dataset: block"
        rows = expand_dataset(task, path.parent)
        expected = {row.success_criteria[0].expected_skill for row in rows}  # type: ignore[union-attr]
        assert "" in expected, "no distractor row — recall.yes alone cannot detect over-firing"
        assert expected - {""}, "no positive row — recall.yes would have no denominator"

    def test_the_suite_thresholds_validate_against_the_dataset(self) -> None:
        # The validator that rejected the pre-fix file, exercised on the fixed one.
        from coder_eval.models.tasks import TaskDefinition
        from coder_eval.orchestration.task_loader import load_task

        task, _source_yaml = load_task(self.TEMPLATES / "ci-outcome-fixture" / "evals" / "activation.yaml")
        assert isinstance(task, TaskDefinition)
        assert task.success_criteria[0].suite_thresholds == {"recall.yes": 0.7}


@pytest.mark.lint
class TestCE057OutcomePromptsDoNotLeakTheirExpectations:
    """CE057 — an outcome row's prompt must not contain a value its EXPECTATIONS grade it on.

    CE061's blind spot, one indirection over. An outcome suite's marking scheme does not live on
    any criterion: the criterion is a `run_command` naming a script, and every string the row is
    actually graded on sits in `outcome-grader/expectations/<row id>.json`. So the suite whose
    scores an optimization round spends real money on is exactly the one CE061 cannot see into.

    A leak here is worse than an ordinary one, for the reason the whole plan exists: a prompt that
    supplies its own answer scores well whether or not the behaviour under test happened, and in an
    A/B an arm that DELETED that behaviour still passes. It biases every arm equally, so no
    comparison downstream can reveal it.

    **Class-wired, not a `BaseRule`.** Its subject is a JSONL plus a directory of JSON, not one
    `.py` AST, so it follows CE065 / CE045 / CE052 rather than `tests/lint/rules/` — and nothing is
    added to `runner.py`, whose id-uniqueness assert covers `ALL_RULES` alone. The detection body
    lives in `tests/lint/outcome_prompt_leak.py`, a shared reader beside `skip_guards.py` and
    `task_yaml_discovery.py`, so the fixtures below and the repo scan exercise the SAME code.

    **Boundary**, stated so a green run is not mistaken for a proof: **verbatim only**, exactly as
    CE061. A prompt describing the graded behaviour in other words still needs a reviewer. And the
    spec carve-out is the whole difficulty — an outcome prompt legitimately states output paths,
    sheet names and column names, because "follow the user's spec literally" is itself graded.
    `LEAK_LOCATOR_FIELDS` is what draws that line, shared with CE061 rather than redrawn here.
    """

    REPO_ROOT: ClassVar[Path] = REPO_ROOT

    def test_no_shipped_outcome_suite_leaks_its_marking_scheme(self):
        from tests.lint.outcome_prompt_leak import leaks

        found, pairs = leaks(self.REPO_ROOT / "plugins")
        # NON-VACUITY on the PAIR count, never the suite count. Before this plan the only suite in
        # the tree had one expectations file matching zero rows, so a suite-level assert would have
        # passed while comparing nothing — the CE044/CE045 failure this rule cites by name.
        assert pairs, (
            "GAP: no prompt/expectation PAIR was compared. Either no outcome suite was discovered, "
            "or every expectations file matches no row — this rule is inert either way."
        )
        assert not found, (
            "an outcome row's prompt hands the agent a string its expectations grade it on, so the "
            "row scores well whether or not the behaviour happened — and an A/B arm that DELETED "
            "that behaviour would still pass:\n"
            + "\n".join(f"  {leak.row_id}: {leak.value!r} (check {leak.check}) in {leak.suite}" for leak in found)
        )

    @staticmethod
    def _suite(root: Path, *, scenario: str, needle: str, extra_row: dict | None = None) -> Path:
        """One outcome suite on disk: suite YAML + rows JSONL + `outcome-grader/expectations/`.

        The suite YAML is not optional scaffolding — its `initial_prompt` IS the thing compared,
        and building the fixture without it is what let the rule compare row fields instead.
        """
        import json

        (root / "outcome-grader" / "expectations").mkdir(parents=True)
        row = {"id": "core-1", "scenario": scenario, **(extra_row or {})}
        (root / "rows.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        (root / "outcome.yaml").write_text(
            'task_id: "s"\ninitial_prompt: |\n  Use the skill.\n\n  ${row.scenario}\n', encoding="utf-8"
        )
        (root / "outcome-grader" / "expectations" / "core-1.json").write_text(
            json.dumps({"path": "out/report.md", "checks": {"mentions#core": {"all_of": [needle]}}}),
            encoding="utf-8",
        )
        return root

    def test_ce057_ignores_a_row_field_the_prompt_never_interpolates(self, tmp_path: Path):
        """The false positive that would have got this rule `# noqa`'d on first contact.

        `expected_snippet` is a CRITERION parameter — the shipped template feeds it to
        `file_check.includes` — so a suite whose expectations assert the same string is doing
        exactly what the template teaches. The string is in the marking scheme twice and in the
        prompt zero times. Comparing every row field reported that as a leak.
        """
        from tests.lint.outcome_prompt_leak import leaks

        self._suite(
            tmp_path,
            scenario="Summarise the quarterly figures",
            needle="Total Revenue by Region",
            extra_row={"expected_snippet": "Total Revenue by Region"},
        )
        found, pairs = leaks(tmp_path)
        assert pairs == 1 and not found

    def test_ce057_sees_a_leak_in_the_shared_prompt_template(self, tmp_path: Path):
        # The converse, and only reachable by rendering the real template: a graded value hardcoded
        # into `initial_prompt` reaches EVERY row and no row field is involved at all.
        from tests.lint.outcome_prompt_leak import leaks

        root = self._suite(tmp_path, scenario="Summarise the figures", needle="Total Revenue by Region")
        (root / "outcome.yaml").write_text(
            'task_id: "s"\ninitial_prompt: |\n  Always show Total Revenue by Region.\n\n  ${row.scenario}\n',
            encoding="utf-8",
        )
        found, _pairs = leaks(root)
        assert len(found) == 1 and found[0].value == "Total Revenue by Region"

    def test_ce057_skips_a_rows_file_with_no_suite_beside_it(self, tmp_path: Path):
        # No prompt template means nothing to compare against, and GUESSING one is precisely how
        # the false positive above happened. Skipped rather than approximated.
        import json

        from tests.lint.outcome_prompt_leak import leaks

        (tmp_path / "outcome-grader" / "expectations").mkdir(parents=True)
        (tmp_path / "rows.jsonl").write_text(
            json.dumps({"id": "core-1", "scenario": "a graded phrase"}) + "\n", "utf-8"
        )
        (tmp_path / "outcome-grader" / "expectations" / "core-1.json").write_text(
            json.dumps({"path": "o", "checks": {"mentions": {"all_of": ["a graded phrase"]}}}), "utf-8"
        )
        assert leaks(tmp_path) == ([], 0)

    def test_ce057_flags_a_prompt_containing_a_graded_value(self, tmp_path: Path):
        from tests.lint.outcome_prompt_leak import leaks

        self._suite(
            tmp_path, scenario="Write a report that sets minimum-task-score to 0.8", needle="minimum-task-score"
        )
        found, pairs = leaks(tmp_path)
        assert pairs == 1
        assert len(found) == 1 and found[0].value == "minimum-task-score"

    def test_ce057_allows_a_prompt_stating_a_locator(self, tmp_path: Path):
        # THE carve-out. An outcome prompt names the output path by design — that removes filename
        # nondeterminism from the measurement without revealing what the artifact must contain, and
        # the outcome template tells every author to do it.
        from tests.lint.outcome_prompt_leak import leaks

        self._suite(
            tmp_path,
            scenario="Write the summary to reports/quarterly-summary.md",
            needle="a genuinely graded phrase",
            extra_row={"expected_path": "reports/quarterly-summary.md"},
        )
        found, pairs = leaks(tmp_path)
        assert pairs == 1 and not found

    def test_ce057_respects_leak_min_chars(self, tmp_path: Path):
        # Short values collide by chance. The threshold is `leak_detection`'s, not a second one.
        from coder_eval.leak_detection import LEAK_MIN_CHARS
        from tests.lint.outcome_prompt_leak import leaks

        short = "x" * (LEAK_MIN_CHARS - 1)
        self._suite(tmp_path, scenario=f"Mention {short} somewhere", needle=short)
        found, pairs = leaks(tmp_path)
        assert pairs == 0 and not found

    def test_ce057_reports_gap_when_no_suites_discovered(self, tmp_path: Path):
        from tests.lint.outcome_prompt_leak import leaks

        assert leaks(tmp_path) == ([], 0)

    def test_an_expectations_file_matching_no_row_compares_nothing(self, tmp_path: Path):
        # The state the shipped template was in, and why the assert counts PAIRS: the suite is
        # discovered and the file parses, so a suite-level non-empty assert would read as green.
        import json

        from tests.lint.outcome_prompt_leak import leaks

        (tmp_path / "outcome-grader" / "expectations").mkdir(parents=True)
        (tmp_path / "rows.jsonl").write_text(json.dumps({"id": "core-1", "scenario": "anything"}) + "\n", "utf-8")
        (tmp_path / "outcome.yaml").write_text('task_id: "s"\ninitial_prompt: "${row.scenario}"\n', "utf-8")
        (tmp_path / "outcome-grader" / "expectations" / "example-row.json").write_text(
            json.dumps({"path": "out/report.md", "checks": {"mentions": {"all_of": ["a graded phrase"]}}}), "utf-8"
        )
        assert leaks(tmp_path) == ([], 0)

    def test_the_rules_map_and_the_path_are_not_graded_values(self, tmp_path: Path):
        # `rules` maps checks to rule ids and `path` is a locator; neither asserts CONTENT, so
        # neither may fire. A rule id echoed in a scenario is bookkeeping, not an answer.
        from tests.lint.outcome_prompt_leak import graded_values

        spec = {
            "path": "reports/quarterly-summary.md",
            "rules": {"mentions#core": "R1-formulas-in-column"},
            "checks": {"mentions#core": {"all_of": ["a genuinely graded phrase"], "path": "some/other/path.md"}},
        }
        assert graded_values(spec) == [("mentions#core", "a genuinely graded phrase")]

    def test_nothing_was_added_under_the_baserule_directory(self):
        # The rule's own filing decision, asserted rather than described: `tests/lint/rules/` holds
        # `BaseRule` modules and `runner.py`'s id-uniqueness assert covers `ALL_RULES` alone, so a
        # class-wired id living there would be neither loaded nor checked for collisions.
        from tests.lint.runner import ALL_RULES

        assert not (TESTS_ROOT / "lint" / "rules" / "ce057_outcome_prompt_leak.py").exists()
        assert not any(getattr(rule, "id", None) == "CE057" for rule in ALL_RULES)

    @pytest.mark.parametrize(
        ("rule_id", "why"),
        [
            # RESERVED: a promotion trigger that has not fired. CE057 was chosen over it precisely
            # so renumbering could not silently retire the reservation.
            ("CE056", "reserved — the tree-walking fixture check, which today would pass vacuously"),
            # RETIRED: the evaluator's whitelist and its dispatch became one declaration
            # (`_BINARY_OPS` / `_UNARY_OPS`), so there is no parity left for a rule to pin. The
            # rationale lives in `.claude/harness-candidates.md`, beside CE056's reservation.
            ("CE044", "retired — the two halves it pinned are now one dict dispatch"),
        ],
    )
    def test_a_reserved_or_retired_id_is_not_live(self, rule_id: str, why: str) -> None:
        """A number that is not a live rule must not become one by accident.

        Both directions of the register: reusing a RESERVED id drops the reservation, and reusing a
        RETIRED one makes `make lint` report findings under a number whose documented meaning is
        something else. `runner.py`'s uniqueness assert covers `ALL_RULES` only, so neither is
        caught by it — a class-wired rule can claim either id and nothing fails.
        """
        from tests.lint.runner import ALL_RULES

        assert not any(getattr(rule, "id", None) == rule_id for rule in ALL_RULES), why
        # And nothing under `tests/lint/rules/` is named for it either, which is how a `BaseRule`
        # would arrive.
        assert not list((TESTS_ROOT / "lint" / "rules").glob(f"{rule_id.lower()}_*.py")), why
        # And no `TestCE<NNN>` class claims it — the CLASS-WIRED half, which is the case the
        # docstring above names and which neither check can see. `ALL_RULES` never holds a
        # class-wired rule, and the cross-surface uniqueness test fails only on DUPLICATES, so a
        # SINGLE class claiming a reserved or retired number passed every check in the suite
        # (measured: appending `class TestCE044…` to a lint_tests module left it green).
        claimants = sorted(
            f"{path.name}::{node.name}"
            for path in sorted((TESTS_ROOT / "lint_tests").glob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ClassDef) and node.name.startswith(f"Test{rule_id}")
        )
        assert not claimants, f"{rule_id} is {why}, but {claimants} claims it"


@pytest.mark.lint
class TestCE060SplitLabelsAllOrNothing:
    """CE060 — a dataset's split field must be on every row or on none, never on some.

    `optimize-skill` calls a partly-labelled dataset "the dangerous state, because it does
    not look like one", and it is right: ``--split`` keeps the rows whose label matches and
    **silently drops the unlabelled ones**, so the run succeeds, the report renders, and
    every metric is computed over a smaller suite than the file suggests. Nothing in the
    output says how many rows went missing.

    That is mechanically detectable, so per CLAUDE.md's standing instruction it becomes a
    rule rather than a paragraph. Wired as a dedicated ``@pytest.mark.lint`` class rather
    than a ``BaseRule`` because it reasons over YAML + JSONL, not over one ``.py`` AST —
    the same shape as CE034 above.

    Both legal states pass: fully labelled (``--split`` selects) and fully unlabelled
    (``--split`` does not apply to the task at all, via ``expand_dataset``'s ``if labelled:``
    branch). Only the mixture is a finding.
    """

    @staticmethod
    def _split_labels(task, task_file_dir: Path) -> list[str | None]:
        """Each row's split label, using the runtime's own definition of "labelled"."""
        from coder_eval.orchestration.task_loader import load_dataset_rows, row_split_label

        rows = load_dataset_rows(task.dataset, task_file_dir)
        # The CONFIGURED field name, never the literal "split" — a dataset may name it
        # anything, and keying on the default would silently pass every such suite.
        return [row_split_label(row, task.dataset.split_field) for row in rows]

    @classmethod
    def _offenders(cls, task, task_file_dir: Path) -> str | None:
        labels = cls._split_labels(task, task_file_dir)
        labelled = [x for x in labels if x is not None]
        if labelled and len(labelled) != len(labels):
            return f"{len(labelled)} of {len(labels)} rows carry a split label"
        return None

    @pytest.mark.parametrize(
        "path",
        _REPO_TASKS,
        ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
    )
    def test_repo_tasks_are_fully_labelled_or_not_at_all(self, path: Path):
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(path)
        if task.dataset is None:
            pytest.skip("no dataset: block — the rule says nothing about it")

        offender = self._offenders(task, path.parent)
        assert offender is None, (
            f"{path}: {offender}. A PARTLY labelled dataset is the one bad state — "
            f"`--split` keeps the matching rows and silently DROPS the unlabelled ones, so "
            f"every metric is computed over a smaller suite than the file suggests, with "
            f"nothing in the run reporting it. Label the remaining rows (do not exempt)."
        )

    def test_detects_a_partly_labelled_dataset(self, tmp_path: Path):
        task = _dataset_task([{"id": "a", "split": "train"}, {"id": "b", "split": "test"}, {"id": "c"}])
        assert self._offenders(task, tmp_path) == "2 of 3 rows carry a split label"

    def test_fully_labelled_dataset_is_not_flagged(self, tmp_path: Path):
        task = _dataset_task([{"id": "a", "split": "train"}, {"id": "b", "split": "test"}])
        assert self._offenders(task, tmp_path) is None

    def test_fully_unlabelled_dataset_is_not_flagged(self, tmp_path: Path):
        # Legal and safe: `--split` then does not apply to this task at all.
        task = _dataset_task([{"id": "a"}, {"id": "b"}])
        assert self._offenders(task, tmp_path) is None

    def test_zero_counts_as_a_label(self, tmp_path: Path):
        # A falsy 0 is a real label, not a missing value — the split filter compares via
        # str(), so `--split 0` selects it. Treating it as unlabelled would make a fully
        # labelled dataset read as partly labelled.
        task = _dataset_task([{"id": "a", "split": 0}, {"id": "b", "split": 1}])
        assert self._offenders(task, tmp_path) is None

    def test_explicit_null_and_empty_string_count_as_unlabelled(self, tmp_path: Path):
        # Pins the (None, "") convention. A half-labelled JSONL carries explicit nulls, and
        # an empty string is the same "no value here" state.
        task = _dataset_task([{"id": "a", "split": "train"}, {"id": "b", "split": None}, {"id": "c", "split": ""}])
        assert self._offenders(task, tmp_path) == "1 of 3 rows carry a split label"

    def test_rule_keys_on_the_configured_split_field(self, tmp_path: Path):
        # Not the literal "split". A dataset naming its field anything else would otherwise
        # read as fully unlabelled and pass no matter how it was labelled.
        task = _dataset_task([{"id": "a", "fold": "train"}, {"id": "b"}], split_field="fold")
        assert self._offenders(task, tmp_path) == "1 of 2 rows carry a split label"

    def test_expand_dataset_keeps_exactly_the_rows_the_convention_names(self, tmp_path: Path):
        # Asserted against LITERAL expected sets, not against `row_split_label` — the helper
        # is what `expand_dataset` now calls, so deriving the expectation from it would
        # compare the code to itself and pass however wrong both were.
        #
        # These literals encode the convention the extraction had to preserve: `0` is a
        # real label reached by `--split 0` (compared via str()), while explicit `null` and
        # `""` are unlabelled and are reachable by no split at all.
        from coder_eval.orchestration.task_loader import expand_dataset

        rows = [
            {"id": "a", "split": "train"},
            {"id": "b", "split": "test"},
            {"id": "c", "split": 0},
            {"id": "d", "split": None},
            {"id": "e", "split": ""},
        ]
        task = _dataset_task(rows)
        for split, expected in (("train", {"a"}), ("test", {"b"}), ("0", {"c"})):
            kept = {t.task_id.split("/")[-1] for t in expand_dataset(task, tmp_path, split=split)}
            assert kept == expected, f"split={split!r}: expand_dataset kept {kept}, expected {expected}"
