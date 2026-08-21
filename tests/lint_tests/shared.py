"""Constants, fixtures and helpers more than one lint-test module reads.

Extracted verbatim from `tests/test_custom_lint.py` when it was split. Nothing here is a
test; it is the module-level preamble the monolith accumulated, and it lives in one place so
a surface list (`SKILL_DOC_SURFACES`, `PLUGIN_TEXT_FILES`) still has exactly one declaration.
"""

import ast
import re
import textwrap
from pathlib import Path

import yaml


# The repo root, declared ONCE. Every path constant in the split lint suite derives from it.
#
# `parents[2]` from `tests/lint_tests/shared.py`, and it is spelled here rather than in each module
# because that is exactly what broke when the monolith was split: ~25 constants read
# `Path(__file__).parent.parent`, correct in `tests/` and one directory short in `tests/lint_tests/`.
# The symptom was not a red import — it was 178 anti-vacuity assertions firing at once, each
# reporting that the tree it scans had vanished.
REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

SRC = REPO_ROOT / "src"

PLUGIN_ROOT = REPO_ROOT / "plugins" / "coder-eval"

PLUGIN_SKILLS = sorted(PLUGIN_ROOT.glob("skills/*/SKILL.md"))

PLUGIN_TEXT_FILES = sorted(
    p for p in PLUGIN_ROOT.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml", ".yml", ".json", ".jsonl"}
)

SKILLS_REQUIRING_THE_CLI = {"init", "check-skill", "task", "optimize-skill"}

RUBRIC_READERS = {"task", "lint-tasks", "init", "optimize-skill"}

SKILL_NEEDS_EVAL_ROOT_DISCOVERY = {
    "analyze": True,
    "ci": True,
    "init": True,
    "lint-tasks": True,
    "check-skill": True,
    "task": True,
    # Reads both trees: globs the task tree for an activation suite, then reads that
    # run's suite.json rollups.
    "optimize-skill": True,
}

EVAL_ROOT_DEFAULT_PHRASINGS = (
    "default to `runs/latest`",
    "default to `tasks/`",
    "fell back to the default",
)

SKILL_DISABLE_MODEL_INVOCATION = {
    "analyze": False,
    "ci": True,
    "init": True,
    "lint-tasks": False,
    "check-skill": False,
    "task": False,
    # Multi-round and expensive — a baseline plus three A/B stages of full agent runs.
    # Never something to start because a message mentioned a skill's wording.
    "optimize-skill": True,
}

SKILL_DOC_SURFACES = (
    "plugins/coder-eval/README.md",
    "docs/PLUGIN.md",
    "README.md",
    "CLAUDE.md",
    # The tutorial enumerates the commands a reader will see after installing, so a skill
    # missing here is a skill they are told does not exist. Added after it shipped a stale
    # "six commands" list omitting `optimize-skill`: the count sensor below already existed,
    # but this file was not one of the surfaces it read.
    "docs/tutorials/07-plugin-in-claude-code.md",
)

SKILL_LISTING_BUDGET_CHARS = 1_600

REPO_PATH_TOKENS = ("docs/", "src/", ".claude/shared/", ".claude/commands/", "uv run", "../")

_NORMALIZED_IMPL = 'return " ".join(path.read_text(encoding="utf-8").split())'


def _normalized(path: Path) -> str:
    """A surface's text with all whitespace collapsed to single spaces.

    Every prose sensor in this file reads its surface through here, and
    ``test_no_sensor_inlines_the_normalization_idiom`` keeps it that way. These documents are
    hard-wrapped, so a phrase a sensor looks for routinely straddles a newline — and a raw
    substring check then passes on exactly the text it exists to catch.

    That is not hypothetical: `docs/PLUGIN.md` read ``All six\\n  skills read it`` while
    the plugin shipped seven, and the count sensor stayed green through 91 lint tests
    because the newline sat between the two words.
    """
    return " ".join(path.read_text(encoding="utf-8").split())


def _missing_tokens(text: str, tokens: tuple[tuple[str, str], ...]) -> list[str]:
    """Every ``(token, why)`` pair whose token is absent from ``text``.

    A module-level function rather than a loop inside its test, for the same reason
    ``_wrong_skill_count_offenders`` below is one: a self-test can then run the REAL matcher
    against hand-built text with a guarded sentence removed. A deletion sensor with no self-test
    can be reverted to a no-op with every test still green — and "edit a shipped file to prove it"
    is not a test, it is a thing someone did once.

    ``text`` is expected to have come through :func:`_normalized`; these surfaces are hard-wrapped,
    so a phrase routinely straddles a newline.
    """
    return [f"{token!r} — {why}" for token, why in tokens if token not in text]


_PROPOSAL_TOKENS: tuple[tuple[str, str], ...] = (
    (
        "structurally different",
        "without it a proposer converges on a reworded version of a theory that already failed",
    ),
    (
        "blinded",
        "a proposer that has seen the test split turns Stage C into a second train split, "
        "undetectably — the confirmation still renders and no longer means what it says",
    ),
    (
        "categories of user intent",
        "the anti-overfit rule; adding the missed row's wording scores well on that row and nowhere else",
    ),
    # P3. A reference solution answers a question expected-vs-observed cannot: how the row was
    # MEANT to be solved. Without the rule the proposer never opens the one artifact that carries
    # a correct trajectory.
    (
        "gold solution",
        "where a suite ships a reference, the proposer must study HOW the row was meant to be "
        "solved — expected-vs-observed says only that it failed",
    ),
    # And its hazard, which is sharper here than anywhere else in the file: the content is right
    # there and it is known-correct, which is exactly what makes pasting it tempting.
    (
        "never the answer",
        "the gold-solution rule must say extract the PROCEDURE — writing the reference's content "
        "into the candidate is memorization with a known-correct string, the worst kind",
    ),
    # P6. "Generalize to categories" says what to generalize TO; this says what the edit must then
    # CONTAIN. A category with no technique cannot be executed.
    (
        "strategy specific to the failure's CATEGORY",
        "each edit must carry a technique for its failure category, not an exhortation — "
        "'name the symptom vocabulary' is a strategy, 'make it clearer' is a wish",
    ),
)

_SKILL_PROCEDURE_TOKENS: tuple[tuple[str, str], ...] = (
    ("route to the execution track", "Step 2's routing decision"),
    ("Never run both tracks in one round", "Step 3's one-variable-per-round rule"),
    ("ONE dataset-backed task", "Step 4's hard constraint on the suite's shape"),
    ("Invoke the skill from the prompt", "Step 4's activation-held-constant rule"),
    ("Use the slash form", "Step 4's only way to reach a disable-model-invocation skill"),
    ("Hand over the template itself", "Step 4's handover to /coder-eval:task"),
    ("copied unchanged", "Step 8's snapshot must carry the sibling skills"),
    ("/coder-eval:check-skill", "the sibling this skill hands control back to"),
    ("round<N>-triage.yaml", "Step 9's per-stage experiment file"),
    ("round<N>-gate.yaml", "Step 9's per-stage experiment file"),
    ("round<N>-confirm.yaml", "Step 9's per-stage experiment file"),
    # Step 10 drives a library rather than doing arithmetic by hand, so it must name the
    # functions it calls. A paraphrase is a step nobody can execute.
    ("activation_gate", "Step 10 must name the function that computes the verdict"),
    ("holm_promote", "the family correction is a separate call; gating alone never promotes"),
    ("render_markdown", "Step 10 must print the verdict block verbatim rather than paraphrasing it"),
    ("criterion_index", "the gate keys on criterion POSITION; a description key pairs zero rows"),
    # The fourth headline. A refusal means NO candidate could have promoted on this suite,
    # so reporting it as an ordinary negative result is a claim about the candidates the
    # data cannot support — and acting on it (hand back, add rows) is procedure.
    (
        "CANNOT SEPARATE AT THIS SIZE",
        "Step 10 must name the refusal headline and say it is not a negative result",
    ),
    # The execution track's own refusal. A DIFFERENT condition with two DIFFERENT remedies
    # (fix the path; add rows the arms disagree on), so a reader who acts on the activation
    # track's advice here buys rows that cannot help.
    (
        "NOT A RESULT",
        "Step 10 must name the execution track's refusal headline and say it is not a negative result",
    ),
    # The control arm had no invocation at all until the execution preflight needed its
    # output — Step 8 described the snapshot and Step 9's file list never mentioned it.
    # The "+0 runs" claim is false without a run directory to read.
    ("round<N>-control.yaml", "Step 9's per-stage experiment file for the control arm"),
    (
        "--run-dir <runs>/control",
        "Step 8 must give the control arm an actual invocation — the execution preflight "
        "reads that run directory, so a snapshot with no run command buys nothing",
    ),
    (
        "measure_execution_noise_floor",
        "Step 8 must name the function that prices the execution track",
    ),
    # Two fronts, and WHICH one feeds a merge is procedure: the coverage front discards an
    # arm that owns a single row, which is exactly the ingredient a merge is built from.
    (
        "instance-best",
        "Step 10 must name GEPA's front beside the coverage one, and Step 8 must draw a "
        "merge candidate's halves from it",
    ),
    (
        "weighted_score",
        "the execution preflight measures weighted_score, not f1.yes — an F1 floor prices a "
        "gate that never reads F1 and comes back a meaningless 0.000",
    ),
    # The search loop's own experiment file. There is no --variant filter, so a round that
    # runs one arm needs a file holding one arm — and it is the only file in Step 9's list
    # that does, which is exactly why it is the one an agent would try to skip.
    (
        "round<N>-explore.yaml",
        "Step 9 must name the search loop's one-variant experiment file; there is no "
        "--variant filter, so the arm set can only be changed by authoring a file",
    ),
    # A search accept is an UNPAIRED train win. Without this distinction a fresh session
    # reads one as a promotion and recommends text to the user that cleared no gate.
    (
        "lineage head",
        "the search loop's pointer must stay distinct from the incumbent — a search accept "
        "is an unpaired train win, and only Stage B plus Stage C advance what Step 12 diffs",
    ),
    # Step 7's activation taxonomy. The category a proposer cannot name from the counts
    # alone: `details.confusion` says a false positive happened, and only the SIBLING rows
    # say the request belonged to another skill. Losing it collapses two opposite edits
    # (narrow the claim vs. bound the territory) into one "it misfires".
    (
        "stealing a sibling's request",
        "Step 7's activation taxonomy must name the sibling-annexation category — it is "
        "the misfire whose edit differs from an overclaim's, and the gate guards it",
    ),
    # The diagnostic window. `failed_samples[]` is capped, so a reader who does not know
    # to fall through to the per-row task.json sizes the window to whatever the cap left.
    (
        "~15 failing rows",
        "Step 7 must give the diagnostic window a NUMBER; below it the categories are "
        "anecdote, and the capped failed_samples[] silently supplies a smaller one",
    ),
)


def _wrong_skill_count_offenders(surfaces: dict[str, Path], *, count: int, auto: int) -> list[str]:
    """Every place a surface states a skill count other than the real one.

    A module-level function rather than a loop inside its test, so the self-test below can
    run the REAL matcher against a hand-built file. That distinction is the whole point:
    the previous self-test asserted things about ``_normalized`` in isolation, which left
    the sensor itself free to be reverted to a raw ``read_text`` with every test still
    green — a guard that guarded nothing, which is the exact failure class this file exists
    to prevent.

    Three phrasings are in use and all three are covered: ``<word> skills`` /
    ``<word> slash commands`` (both READMEs, ``docs/PLUGIN.md``), ``x <digit>``
    (``CLAUDE.md``'s ``SKILL.md`` x 6), and ``The other <word>`` (the model-invokable
    subset, which is the skill count minus the explicit-invocation-only ones).
    """
    words = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
    assert count in words, f"{count} skills — extend `words` to cover the new count"
    assert auto in words, f"{auto} model-invokable skills — extend `words`"

    wrong_total = sorted(set(words.values()) - {words[count]})
    wrong_subset = sorted(set(words.values()) - {words[auto]})

    offenders: list[str] = []
    for name, path in surfaces.items():
        # Whitespace-collapsed, or a hard-wrapped "All six\n  skills" defeats every
        # substring check below — which is exactly how the stale count shipped green.
        text = _normalized(path)
        offenders += [
            f"{name}: '{word} {noun}'"
            for word in wrong_total
            # "commands" is how the tutorial phrases it, and its absence here is exactly
            # why a stale "six commands" survived: the count was guarded in three
            # phrasings, and the surface used a fourth.
            for noun in ("skills", "slash commands", "commands")
            if f"{word} {noun}" in text
        ]
        offenders += [f"{name}: 'The other {word}'" for word in wrong_subset if f"The other {word}" in text]
        # The multiplication sign CLAUDE.md writes is given as an escape below, so ruff's
        # ambiguous-character rules do not flag a literal one.
        offenders += [
            f"{name}: 'SKILL.md` \u00d7 {digit}'"
            for digit in range(2, 9)
            if digit != count and f"SKILL.md` \u00d7 {digit}" in text
        ]
    return offenders


def _outcome_metric_vocabulary(criterion_type: str = "file_check") -> set[str]:
    """The metric names a `suite_thresholds` on ``criterion_type`` may legitimately gate on.

    Per criterion type, because the vocabularies genuinely differ: `file_check` inherits
    `BaseCriterion.aggregate`'s summary stats, while a classification-style criterion such as
    `skill_triggered` also emits `recall.yes` / `precision.yes` / `f1.yes`. Asserting one
    type's thresholds against another's vocabulary would reject a perfectly valid gate.

    Derived from TWO real sources, because they are genuinely separate and checking only
    the first fails on the very template this repo ships:

    * ``BaseCriterion.aggregate`` emits the summary stats (``count``/``mean``/``median``/
      ``std``/``min``/``max``).
    * ``completion_rate`` is **not** an ``aggregate()`` metric at all — it is injected
      downstream by ``reports._attach_row_accounting`` alongside ``rows_total`` /
      ``rows_excluded``.

    Both halves are obtained by calling the real code, never by writing the names down.
    """
    from coder_eval.criteria import CriterionRegistry, init_criteria
    from coder_eval.models import CriterionResult, FileCheckCriterion
    from coder_eval.reports import _attach_row_accounting

    init_criteria(validate=False)
    checker = CriterionRegistry.get_checker(criterion_type)()
    if criterion_type == "skill_triggered":
        from coder_eval.models import ClassificationCriterionResult, SkillTriggeredCriterion

        criterion = SkillTriggeredCriterion(description="d", skill_name="x", expected_skill="x")
        per_row: list[CriterionResult] = [
            ClassificationCriterionResult(
                criterion_type=criterion_type, description="d", score=1.0, observed_label=label, expected_label=label
            )
            for label in ("yes", "no")
        ]
    else:
        criterion = FileCheckCriterion(description="d", path="x")  # type: ignore[assignment]
        per_row = [
            CriterionResult(criterion_type=criterion_type, description="d", score=score, passed=score >= 0.9)
            for score in (1.0, 0.0)
        ]
    aggregate = checker.aggregate(criterion, per_row)
    assert aggregate is not None
    return set(aggregate.metrics) | set(_attach_row_accounting(aggregate, 2, 2).metrics)


def _assert_outcome_suite_shape(
    path: Path,
    *,
    expected_rows: int,
    expected_split_counts: dict[str, int],
    skill_name: str,
    invocation: str,
) -> None:
    """The structural contract every outcome suite must satisfy.

    Asserted twice — once against the bundled template, once against the checked-in worked
    example — so the two cannot drift into different shapes while both looking fine. Uses
    the real ``load_task`` / ``expand_dataset``, never a re-implementation of either.

    The contract:

    1. **Dataset-backed**, expanding to exactly one task per row. This is the load-bearing
       one: ``suite.json`` is written only for tasks the expander touched (rollups group on
       ``suite_id``), and ``--split`` filters dataset *rows* — so a suite written as
       separate task files gives an optimization round no rollup to rank on and makes
       ``--split test`` silently re-run the train rows.
    2. **Split counts** as declared, checked through ``expand_dataset(..., split=...)``. The same
       question is answerable from a shell — ``coder-eval plan <suite> --split test`` has printed
       the selected count since CE065, and ``/coder-eval:task``'s step 6 requires an author to run
       both splits — but a shell command is not a regression test, so the counts are pinned here
       against the real expander.
    3. **No surviving ``${row.`` anywhere** in the prompt or the expanded criteria —
       substitution has to reach list leaves (``includes: ["${row.x}"]``), not just scalars.
    4. **Every prompt invokes the skill explicitly**, in its opening lines — by slash form,
       by an imperative naming it, or both. Holding activation constant is what makes the
       body the only variable. Note this is deliberately NOT a check for the slash form
       specifically: nothing expands a slash command, so `/plugin:skill` is plain text the
       model may ignore, and the measured-most-reliable form is an explicit imperative
       (6/6 engaged, against 3/6 for the slash form alone).
    5. **An engagement criterion on every row** with a non-empty ``expected_skill`` —
       what separates "the body gave bad instructions" from "the skill never ran".
    """
    import json

    from coder_eval.orchestration.task_loader import expand_dataset, load_task

    task, _source_yaml = load_task(path)
    assert task.dataset is not None, (
        f"{path.name} has no `dataset:` block — an outcome suite MUST be one dataset-backed "
        "task with one row per scenario. Without it no suite.json is written (rollups group "
        "on suite_id, which only the expander sets) and `--split test` silently re-runs the "
        "train rows"
    )
    assert task.dataset.split_field, f"{path.name} has a dataset but no `split_field` — `--split` would filter nothing"

    rows = expand_dataset(task, path.parent)
    assert len(rows) == expected_rows, (
        f"{path.name}: expected one task per dataset row ({expected_rows}), got {len(rows)}"
    )

    for split, count in expected_split_counts.items():
        actual = len(expand_dataset(task, path.parent, split=split))
        assert actual == count, f"{path.name}: `--split {split}` resolves {actual} rows, expected {count}"

    for row in rows:
        # `initial_prompt` is `str | None` (a task may use `initial_prompt_file`), so narrow
        # it — otherwise moving the prompt to a file turns these into a TypeError.
        assert row.initial_prompt, f"{row.task_id} has no initial_prompt"
        assert "${row." not in row.initial_prompt, f"unsubstituted row placeholder in {row.task_id}'s prompt"
        opening = row.initial_prompt.lstrip()[:300]
        assert invocation in opening, (
            f"{row.task_id}'s prompt does not invoke {invocation!r} in its opening lines. An "
            "outcome suite holds activation CONSTANT so the body is the only variable; a prompt "
            "that leaves engagement to chance yields a mixture of two effects and a gate that "
            "can attribute neither"
        )

        rendered = json.dumps([c.model_dump() for c in row.success_criteria], default=str)
        assert "${row." not in rendered, (
            f"unsubstituted row placeholder in {row.task_id}'s criteria — substitution must reach "
            'every string leaf including inside lists (`includes: ["${row.x}"]`)'
        )

        engagement = [
            c
            for c in row.success_criteria
            if c.type == "skill_triggered" and c.skill_name == skill_name  # type: ignore[attr-defined]
        ]
        assert engagement, f"{row.task_id} carries no `skill_triggered` criterion naming {skill_name!r}"
        for criterion in engagement:
            assert criterion.expected_skill, (  # type: ignore[attr-defined]
                f"{row.task_id}'s engagement criterion has an empty `expected_skill`, which asserts "
                f"{skill_name!r} must NOT engage. An outcome suite holds activation CONSTANT — every "
                "row is a positive, and a distractor row here inverts the suite's premise"
            )


_PYTHON_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _snippet_binding_failures(markdown: str) -> list[str]:
    """Every shipped snippet import that no longer RESOLVES, and every call whose KEYWORDS no
    longer bind, as strings.

    A prose sensor sees a snippet's tokens, not its call signatures: a renamed or removed keyword
    argument leaves every token in place and fails in the user's terminal after they have paid for
    the runs the snippet was meant to read. A MOVED name is the same failure one step earlier, and
    it is checked first — over the import map rather than inside the call loop, for the two reasons
    the comment there gives.

    Imports are collected across the WHOLE file first (a snippet may import in one fence and call
    in another), then each fence's calls are bound with `Signature.bind_partial` — keywords only,
    since a snippet's positional values are placeholders. A syntax error in a fence is itself a
    failure: a shipped snippet that does not parse cannot run.

    The boundary is stated on the calling test: keywords only, silent against a `**kwargs` callee,
    and a name assigned anywhere in the same fence is skipped as shadowed.
    """
    import importlib
    import inspect

    origins: dict[str, str] = {}
    for module, block in re.findall(r"from (coder_eval[\w.]*) import \(([^)]*)\)", markdown):
        for name in block.split():
            if cleaned := name.strip(" ,"):
                origins[cleaned] = module
    for module, line in re.findall(r"from (coder_eval[\w.]*) import ([^(\n]+)", markdown):
        for name in line.split(","):
            if cleaned := name.strip():
                origins[cleaned] = module

    failures: list[str] = []
    # EXISTENCE first, over the `origins` map rather than inside the call loop below — and the
    # placement is the whole point, because that loop is blind to a moved name for TWO independent
    # reasons. (1) A name that no longer exists resolves to `None`, which is not callable, so the
    # loop's `if not callable(target): continue` skips it silently. (2) The loop only ever visits
    # names used as `ast.Call` funcs, so a name IMPORTED BUT NEVER CALLED — `CostQualityPoint`,
    # `SearchComparison`, `TASK_JSON_GLOB`, `GATE_RESAMPLES`, `MATERIALITY_FLOOR` — never reaches
    # it at all. Either way a broken import fails in the user's terminal, after they have paid for
    # the runs the snippet was meant to read.
    #
    # It also RESOLVES each name once, into `bound`, which the call loop then reads instead of
    # importing again. That is not tidiness: `import_module` raises `ModuleNotFoundError` on a
    # module that does not exist, and the call loop's copy was unguarded — so a snippet importing
    # from a mistyped module CRASHED this sensor rather than reporting it, which is the precise
    # failure it exists to prevent. Found by simulating the module split against the real file.
    bound: dict[str, object] = {}
    for name, module in sorted(origins.items()):
        try:
            imported = importlib.import_module(module)
        except ImportError as exc:
            failures.append(f"`from {module} import {name}` — the module does not import: {exc}")
            continue
        if not hasattr(imported, name):
            failures.append(f"`from {module} import {name}` — {module} has no attribute {name!r}")
            continue
        bound[name] = getattr(imported, name)

    for index, source in enumerate(_PYTHON_FENCE.findall(markdown), start=1):
        try:
            tree = ast.parse(textwrap.dedent(source))
        except SyntaxError as exc:
            failures.append(f"fence {index} does not parse: {exc}")
            continue

        shadowed = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            name = node.func.id
            if name in shadowed or name not in bound:
                # `not in bound` covers both "never imported here" and "imported but unresolvable",
                # the second of which the existence pass has already reported.
                continue
            target = bound[name]
            # Kept: `UNRESOLVED_MODEL` / `UNRECORDED_SPLIT` are strings the skill imports, and a
            # name that resolves to a non-callable has no signature to bind against.
            if not callable(target):
                continue
            keywords = {kw.arg: None for kw in node.keywords if kw.arg is not None}
            try:
                inspect.signature(target).bind_partial(**keywords)
            except TypeError as exc:
                failures.append(f"fence {index}: {origins[name]}.{name}({', '.join(keywords)}) — {exc}")
    return failures


def _skill_frontmatter(path: Path) -> dict:
    """Parse a SKILL.md's YAML frontmatter block."""

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} does not open with a YAML frontmatter fence"
    end = text.find("\n---\n", 3)
    assert end != -1, f"{path} opens with '---' but never closes the frontmatter fence"
    return yaml.safe_load(text[4:end])


RUN_RECORD_CONSUMERS = ("plugins/coder-eval/skills/analyze/SKILL.md",)

_JQ_WORDS = frozenset(
    [
        "and",
        "or",
        "not",
        "if",
        "then",
        "elif",
        "else",
        "end",
        "null",
        "true",
        "false",
        "length",
        "all",
        "any",
        "select",
        "map",
        "keys",
        "add",
        "empty",
        "type",
        "tostring",
        "tonumber",
        "join",
        "split",
        "sort",
        "sort_by",
        "group_by",
        "unique",
        "min",
        "max",
        "reduce",
        "foreach",
        "try",
        "catch",
        "def",
        "as",
        "import",
        "include",
        "limit",
        "first",
        "last",
        "range",
        "floor",
        "ceil",
        "has",
        "in",
        "with_entries",
        "to_entries",
        "from_entries",
        "flatten",
        "contains",
        "startswith",
        "endswith",
        "test",
        "capture",
    ]
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_PATH_HEAD = re.compile(r"(?<![A-Za-z0-9_)\]])\.([A-Za-z_][A-Za-z0-9_]*)")

_TOP_LEVEL_CELL = "top-level record key"


def _legacy_top_level_keys(text: str) -> set[str]:
    """Legacy TOP-LEVEL record keys the two-generation table names, backticks stripped."""
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) == 3 and cells[2] == _TOP_LEVEL_CELL:
            keys.add(cells[1].strip("`"))
    return keys


def _record_fields_referenced(block: str) -> set[str]:
    """Names the block reads off a run record: bare jq shorthand keys + path heads."""
    names = set()
    for m in _IDENT.finditer(block):
        before, after = block[: m.start()], block[m.end() :].lstrip()
        if before.endswith(".") or before[-1:].isalnum() or before[-1:] == "_":
            continue  # a chain segment, handled by _PATH_HEAD
        if after.startswith((":", "(")) or m.group() in _JQ_WORDS:
            # `name:` is an OUTPUT key being computed (`total_cost_usd: .total_token_usage…`),
            # not a lookup — only bare `{task_id, final_status}` shorthand pulls a field through.
            continue
        names.add(m.group())
    return names | {m.group(1) for m in _PATH_HEAD.finditer(block) if m.group(1) not in _JQ_WORDS}


def _dataset_task(rows: list[dict], *, prompt: str = "${row.id}", criteria=None, split_field: str = "split"):
    """A minimal dataset-backed task over inline rows, for the CE060/CE061 fixtures.

    One builder for both rules: CE060 needs varying rows, CE061 varying prompts AND
    criteria, and a per-class copy would fork the moment either grew a parameter.
    Inline ``rows`` deliberately — no JSONL file, so the fixtures never touch disk.
    """
    from coder_eval.models import Dataset, FileExistsCriterion, TaskDefinition

    return TaskDefinition(
        task_id="t",
        description="dataset-rule fixture",
        initial_prompt=prompt,
        success_criteria=criteria or [FileExistsCriterion(description="d", path="out.txt")],
        dataset=Dataset(rows=rows, split_field=split_field),
    )


def _row_selector_fields() -> set[str]:
    from coder_eval.models import ROW_SELECTOR_FLAGS

    return set(ROW_SELECTOR_FLAGS)


_ROW_SELECTOR_FIELDS = _row_selector_fields()

_CE051_UNRESOLVED = """
_BANNED = "coder_eval.cli"

class R(BaseRule):
    def visit_ImportFrom(self, node):
        if node.module and node.module.startswith(_BANNED):
            self.violation(node, "nope")
"""

_CE051_RESOLVED = """
_BANNED = "coder_eval.cli"

class R(BaseRule):
    def visit_ImportFrom(self, node):
        module = resolved_module(node, self.filepath)
        if module and module.startswith(_BANNED):
            self.violation(node, "nope")
"""
