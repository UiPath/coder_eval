"""Lint tests: harness meta."""

import ast
import textwrap
from pathlib import Path
from typing import ClassVar

import pytest

from tests.lint.cli_flags import help_for, long_flags
from tests.lint.import_resolution import resolved_module
from tests.lint_tests.shared import (
    _ROW_SELECTOR_FIELDS,
    REPO_ROOT,
    TESTS_ROOT,
)


@pytest.mark.lint
class TestCE035WorkflowOutputParity:
    """CE035 — a `steps.<id>.outputs.<key>` / `needs.<job>.outputs.<key>` reference must
    resolve to a key its writer actually produces.

    The motivating bug: `verify-published-action.yml` read
    `steps.parity.outputs.version` twice, but that step writes `pin`/`newest`/`lagging`
    (the shell *variable* was `VERSION`, the output *key* was `newest`). GitHub expands an
    unwritten output to '', so `TAG_REF: v${{ … }}` became the bare `v`, `git show
    "v:action.yml"` exited 128 under `set -euo pipefail`, and the preflight job was red on
    100% of triggers — taking the paid e2e tier (`needs: preflight`) with it. Invisible to
    ruff/pyright/pytest, and actionlint models `steps.*.outputs` as an open string map.
    Reasons over workflow YAML + embedded shell, so it lives here, not in the AST runner.
    """

    REPO_ROOT = REPO_ROOT

    def test_all_workflow_output_refs_resolve(self):
        from tests.lint.workflow_outputs import find_unresolved_output_refs, workflow_paths

        findings = find_unresolved_output_refs(workflow_paths(self.REPO_ROOT), self.REPO_ROOT)
        assert not findings, "unresolvable workflow output references:\n" + "\n".join(f"  {f}" for f in findings)

    def test_catches_an_unwritten_step_output(self, tmp_path: Path):
        """The exact shape of the shipped bug: reading a key the writer never echoes."""
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "w.yml").write_text(
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - id: parity\n"
            "        run: |\n"
            "          {\n"
            '            echo "pin=$PIN"\n'
            '            echo "newest=$VERSION"\n'
            '          } >> "$GITHUB_OUTPUT"\n'
            "      - env:\n"
            "          TAG_REF: v${{ steps.parity.outputs.version }}\n"
            "          PIN_REF: ${{ steps.parity.outputs.pin }}\n"
            "        run: echo hi\n",
            encoding="utf-8",
        )
        findings = find_unresolved_output_refs([wf / "w.yml"], tmp_path)
        assert len(findings) == 1, [str(f) for f in findings]
        assert "outputs.version` is never written" in findings[0].message
        assert "['newest', 'pin']" in findings[0].message

    def test_catches_a_missing_step_id_and_an_undeclared_needs_output(self, tmp_path: Path):
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"
            "  a:\n"
            "    outputs:\n"
            "      version: ${{ steps.ver.outputs.version }}\n"
            "    steps:\n"
            "      - id: ver\n"
            '        run: echo "version=1.2.3" >> "$GITHUB_OUTPUT"\n'
            "  b:\n"
            "    if: needs.a.outputs.released_version != ''\n"
            "    steps:\n"
            "      - run: echo ${{ steps.nope.outputs.x }}\n",
            encoding="utf-8",
        )
        messages = [f.message for f in find_unresolved_output_refs([wf], tmp_path)]
        assert any("does not exist in this job" in m for m in messages), messages
        assert any("needs.a.outputs.released_version` is not declared" in m for m in messages), messages

    def test_skips_third_party_actions_and_unreadable_writers(self, tmp_path: Path):
        """Boundaries that keep the rule sound: a pinned action's outputs are not on disk,
        and a body that writes outputs from an embedded interpreter is not guessed at."""
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - id: app-token\n"
            "        uses: actions/create-github-app-token@abc123\n"
            "      - id: py\n"
            "        run: |\n"
            '          python3 -c \'import os; open(os.environ["GITHUB_OUTPUT"], "a")\'\n'
            "      - run: echo ${{ steps.app-token.outputs.token }} ${{ steps.py.outputs.whatever }}\n",
            encoding="utf-8",
        )
        assert find_unresolved_output_refs([wf], tmp_path) == []

    def test_catches_an_undeclared_local_composite_output(self, tmp_path: Path):
        """The `uses: ./` arm: outputs ARE on disk, so a typo against them is enumerable.
        Without this, deleting the composite branch leaves the suite green."""
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        (tmp_path / "action.yml").write_text("name: coder_eval\noutputs:\n  run-dir:\n    value: x\n", encoding="utf-8")
        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - id: gate\n"
            "        uses: ./\n"
            "      - run: echo ${{ steps.gate.outputs.rundir }} ${{ steps.gate.outputs.run-dir }}\n",
            encoding="utf-8",
        )
        findings = find_unresolved_output_refs([wf], tmp_path)
        assert len(findings) == 1, [str(f) for f in findings]
        assert "outputs.rundir`" in findings[0].message
        assert "['run-dir']" in findings[0].message, "the message must name the real outputs"

    def test_catches_a_reference_to_a_step_that_writes_no_outputs(self, tmp_path: Path):
        """A `run:` step that never touches $GITHUB_OUTPUT writes nothing — distinct from
        the 'unreadable writers' skip, and the branch that reports it (`writes no outputs`)
        was otherwise unexercised."""
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - id: quiet\n"
            "        run: echo 'this step sets nothing'\n"
            "      - run: echo ${{ steps.quiet.outputs.anything }}\n",
            encoding="utf-8",
        )
        findings = find_unresolved_output_refs([wf], tmp_path)
        assert len(findings) == 1, [str(f) for f in findings]
        assert "writes no outputs" in findings[0].message

    def test_catches_a_needs_reference_to_a_nonexistent_job(self, tmp_path: Path):
        """A typo'd job name fails identically to a typo'd key — it expands to '' — and is
        just as enumerable, so it is a finding rather than a deferral to actionlint (which
        this repo documents as advisory, not a gate)."""
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"
            "  release:\n"
            "    outputs:\n"
            "      version: ${{ steps.v.outputs.version }}\n"
            "    steps:\n"
            "      - id: v\n"
            '        run: echo "version=1.0.0" >> "$GITHUB_OUTPUT"\n'
            "  promote:\n"
            "    needs: [release]\n"
            "    if: needs.relase.outputs.version != ''\n"
            "    steps:\n"
            "      - run: echo hi\n",
            encoding="utf-8",
        )
        findings = find_unresolved_output_refs([wf], tmp_path)
        assert len(findings) == 1, [str(f) for f in findings]
        assert "names job 'relase', which does not exist" in findings[0].message
        assert "['promote', 'release']" in findings[0].message

    def test_a_dynamic_printf_writer_is_unreadable_not_a_bogus_key(self, tmp_path: Path):
        """Regression: the writer scan used to capture the conversion letter out of a
        format string (`printf "%s=%s\\n"` -> the key `s`). That non-empty-but-wrong set
        defeats the "no readable key => skip" contract and false-FAILS a correct workflow,
        which is the one direction the docstring promises the rule can never take."""
        from tests.lint.workflow_outputs import _written_keys, find_unresolved_output_refs

        assert _written_keys({"run": 'printf "%s=%s\\n" "$K" "$V" >> "$GITHUB_OUTPUT"'}) is None
        assert _written_keys({"run": 'echo "pin=$PIN" >> "$GITHUB_OUTPUT"'}) == {"pin"}

        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - id: dyn\n"
            '        run: printf "%s=%s\\n" "$K" "$V" >> "$GITHUB_OUTPUT"\n'
            "      - run: echo ${{ steps.dyn.outputs.pin }}\n",
            encoding="utf-8",
        )
        assert find_unresolved_output_refs([wf], tmp_path) == []

    def test_finding_reports_the_line_of_the_offending_reference(self, tmp_path: Path):
        """`Finding.line` is what sends a developer to the right place; nothing asserted
        it, so it could regress to a constant 1 with a green suite."""
        from tests.lint.workflow_outputs import find_unresolved_output_refs

        wf = tmp_path / "w.yml"
        wf.write_text(
            "jobs:\n"  # 1
            "  j:\n"  # 2
            "    steps:\n"  # 3
            "      - id: s\n"  # 4
            '        run: echo "a=1" >> "$GITHUB_OUTPUT"\n'  # 5
            "      - run: echo hi\n"  # 6
            "      - run: echo ${{ steps.s.outputs.b }}\n",  # 7
            encoding="utf-8",
        )
        findings = find_unresolved_output_refs([wf], tmp_path)
        assert len(findings) == 1
        assert findings[0].line == 7, f"expected line 7, got {findings[0].line}"
        assert str(findings[0]).startswith(f"{wf}:7 — ")


@pytest.mark.lint
class TestCE063NestedForbidExtras:
    """CE063: `extra="forbid"` must reach the nested models it appears to protect.

    Pydantic does NOT propagate `model_config` into nested models. A container that declares
    `extra="forbid"` therefore rejects a typo at its own top level and silently DROPS one inside
    any nested model that did not declare it for itself — while every reader of the container's
    config reasonably believes otherwise.

    That is not hypothetical: `OptimizeMeasurements` shipped with `extra="forbid"` and a docstring
    justifying it as "a typo must not become a permanent cache miss, and the regression corpus is
    not reconstructible" — and both `NoiseFloor` and `RegressionRow`, where the corpus actually
    lives, took the lenient default. The guarantee did not hold precisely where it was claimed.

    Runtime introspection rather than an AST walk, because the question is about resolved field
    TYPES (through `list[...]`, `X | None`, unions and aliases), which the AST does not know.
    **Roots.** Every model reachable from `coder_eval.models`, PLUS the extra roots below.
    `coder_eval.models` alone is not the whole surface: `BatchRunConfig` lives in
    `orchestration/` and declares `extra="forbid"`, so a lenient model nested under it was
    exactly this rule's shape while sitting outside its reach. Found while adding `RowSelection`
    — the rule stayed green on the very pattern it exists to catch, because of WHERE the
    container happens to live rather than because of anything about the container.

    **Exemptions carry a reason, not just a name.** A container may legitimately nest a lenient
    model; what it must not do is nest one silently.
    """

    MAX_ANNOTATION_DEPTH = 6

    # Strict containers outside `coder_eval.models` that this rule must still reach.
    EXTRA_ROOTS = ("coder_eval.orchestration.config:BatchRunConfig",)

    # `Container.field -> Nested` pairs that are deliberate, each with WHY.
    EXEMPT: ClassVar[dict[str, str]] = {
        "BatchRunConfig.row_selection -> RowSelection": (
            "RowSelection is also nested under RunSummary, which is deliberately lenient so "
            "run.json stays readable when written by a NEWER coder-eval — a forbid here would "
            "turn a future fourth selector into a hard parse failure of the whole report rather "
            "than an ignored key. The config-side typo risk is covered statically instead: "
            "RowSelection(splt=...) is a pyright error, and the model is constructed at exactly "
            "one production site with literal keywords."
        ),
    }

    @staticmethod
    def _nested_models(annotation: object, depth: int = 0) -> list[type]:
        """Every BaseModel reachable through an annotation's type arguments."""
        import typing

        from pydantic import BaseModel

        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return [annotation]
        if depth >= TestCE063NestedForbidExtras.MAX_ANNOTATION_DEPTH:
            return []
        return [
            m for arg in typing.get_args(annotation) for m in TestCE063NestedForbidExtras._nested_models(arg, depth + 1)
        ]

    def _offenders(self) -> list[str]:
        return sorted({o for o in self._raw_offenders() if o not in self.EXEMPT})

    def _raw_offenders(self) -> list[str]:
        from importlib import import_module

        from pydantic import BaseModel

        import coder_eval.models as models_pkg

        offenders: list[str] = []
        seen: set[type] = set()

        def visit(cls: type) -> None:
            if cls in seen:
                return
            seen.add(cls)
            strict = cls.model_config.get("extra") == "forbid"  # type: ignore[attr-defined]
            for name, field in cls.model_fields.items():  # type: ignore[attr-defined]
                for nested in self._nested_models(field.annotation):
                    if strict and nested.model_config.get("extra") != "forbid":
                        offenders.append(f"{cls.__name__}.{name} -> {nested.__name__}")
                    visit(nested)

        for name in dir(models_pkg):
            obj = getattr(models_pkg, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel):
                visit(obj)
        for root in self.EXTRA_ROOTS:
            module_path, _, attr = root.partition(":")
            visit(getattr(import_module(module_path), attr))
        return sorted(set(offenders))

    def test_no_violations(self):
        offenders = self._offenders()
        assert not offenders, (
            "a model declaring extra='forbid' has nested model field(s) that do NOT:\n  "
            + "\n  ".join(offenders)
            + "\n\npydantic does not propagate model_config, so a typo inside those nested models is "
            "silently dropped while the container's config says it would be rejected. Declare "
            "`model_config = ConfigDict(extra='forbid')` on the nested model too, or relax the "
            "container if leniency is genuinely intended there."
        )

    def test_every_extra_root_resolves_and_is_strict(self):
        """A root that stopped existing — or stopped forbidding extras — silently checks nothing."""
        from importlib import import_module

        for root in self.EXTRA_ROOTS:
            module_path, _, attr = root.partition(":")
            cls = getattr(import_module(module_path), attr)
            assert cls.model_config.get("extra") == "forbid", (
                f"{root} is listed as a strict root but no longer declares extra='forbid' — it is "
                "checking nothing, and removing it from EXTRA_ROOTS is the honest fix"
            )

    def test_every_exemption_is_still_a_real_violation(self):
        """An exemption for a pair that no longer violates is a stale licence.

        Without this, tightening `RowSelection` later would leave a permanent excuse behind — and
        the next lenient model added to that field would inherit it silently.
        """
        stale = sorted(set(self.EXEMPT) - set(self._raw_offenders()))
        assert not stale, f"EXEMPT lists pair(s) that are no longer violations, so the entry is dead: {stale}"

    def test_the_check_actually_fires(self):
        # The rule is only worth having if it can fail. A lenient nested model under a strict
        # container is exactly the shape that shipped, so build one and prove it is caught.
        from pydantic import BaseModel, ConfigDict

        class Lenient(BaseModel):
            value: int = 0

        class Strict(BaseModel):
            model_config = ConfigDict(extra="forbid")
            nested: list[Lenient] = []

        assert self._nested_models(Strict.model_fields["nested"].annotation) == [Lenient]
        assert Strict.model_config.get("extra") == "forbid"
        assert Lenient.model_config.get("extra") != "forbid"
        # And the consequence it guards, demonstrated rather than asserted about:
        assert Strict.model_validate({"nested": [{"value": 1, "typo": 2}]}).nested[0].value == 1


@pytest.mark.lint
class TestCE065RowSelectorParity:
    """CE065 — `run` and `plan` must declare the same row-selector flags, described identically.

    `plan` is sold as the pre-spend preview of what `run` will execute. A selector `run` has
    and `plan` lacks makes the exact invocation the user is about to pay for un-previewable —
    which is precisely the state this rule was written after: `run` shipped `--split`,
    `--sample` and `--sample-per-stratum` while `plan` had only `--split`, so the one command
    whose whole job is to cost a run could not preview two thirds of the ways a run is scoped.

    Three assertions, because the drift has three shapes:

    1. the flag MAP covers exactly the model's fields — a fourth selector on `RowSelection`
       forces a flag decision instead of silently existing on neither command;
    2. both commands DECLARE every flag in the map;
    3. both use the SAME help-string object, so the two surfaces cannot describe one flag two
       different ways.

    Wired as a dedicated ``@pytest.mark.lint`` class rather than a ``BaseRule`` because it
    reasons over resolved Typer signatures, not over one ``.py`` AST.

    **Boundary**, stated so a green run is not mistaken for a proof:

    - it reads ``OptionInfo.param_decls``, so a flag Typer DERIVES from the parameter name
      (``sample: int | None = typer.Option(None)``) is invisible to it and would be reported
      as missing;
    - it pins DECLARATION and HELP TEXT, never behaviour. ``plan`` could accept ``--sample``
      and ignore it entirely and this rule would stay green — the preview-parity test in
      ``tests/test_cli_row_selectors.py`` is what covers that.
    """

    def test_the_flag_map_covers_exactly_the_selector_model(self) -> None:
        from coder_eval.models import ROW_SELECTOR_FLAGS, RowSelection

        assert set(ROW_SELECTOR_FLAGS) == set(RowSelection.model_fields), (
            "ROW_SELECTOR_FLAGS and RowSelection have drifted — a selector field with no flag "
            "renders as nothing on every surface that prints selectors, and a flag with no "
            "field is recorded nowhere in run.json"
        )

    @pytest.mark.parametrize("command_name", ["run_command", "plan_command"])
    def test_both_commands_declare_every_row_selector(self, command_name: str) -> None:
        # Import the FUNCTIONS from their defining modules: `coder_eval.cli` re-exports both
        # names, so `from coder_eval.cli import run_command` would bind the function and make
        # a module-attribute lookup fail confusingly.
        from coder_eval.cli.plan_command import plan_command
        from coder_eval.cli.run_command import run_command
        from coder_eval.models import ROW_SELECTOR_FLAGS

        fn = run_command if command_name == "run_command" else plan_command
        declared = long_flags(fn)
        missing = sorted(set(ROW_SELECTOR_FLAGS.values()) - declared)
        assert not missing, (
            f"{command_name} does not declare {missing}. `plan` is the pre-spend preview of what "
            "`run` executes, so a selector on one command only makes the exact invocation the "
            "user is about to pay for un-previewable — or, the other way round, previewable but "
            "not runnable. Add the flag to both, sharing the help text in cli/row_selectors.py."
        )

    @pytest.mark.parametrize("field", sorted(_ROW_SELECTOR_FIELDS))
    def test_both_commands_share_one_help_string_per_flag(self, field: str) -> None:
        from coder_eval.cli import row_selectors
        from coder_eval.cli.plan_command import plan_command
        from coder_eval.cli.run_command import run_command
        from coder_eval.models import ROW_SELECTOR_FLAGS

        # Parametrized over the MODEL's fields and resolved to a flag through the one map, so
        # a fourth selector is covered here automatically rather than needing this list edited.
        flag = ROW_SELECTOR_FLAGS[field]
        expected = {
            "split": row_selectors.SPLIT_HELP,
            "max_rows": row_selectors.SAMPLE_HELP,
            "sample_per_stratum": row_selectors.SAMPLE_PER_STRATUM_HELP,
        }[field]
        run_help = help_for(run_command, flag)
        plan_help = help_for(plan_command, flag)
        assert run_help is plan_help, (
            f"{flag} is described differently by `run` and `plan`. One flag, two descriptions, is "
            "how the preview and the run drift apart in a reader's head."
        )
        # Identity, not equality: an inlined copy of the same sentence would compare equal
        # while being a second declaration free to drift on the next edit.
        assert run_help is expected, (
            f"{flag}'s help is not the shared constant from cli/row_selectors.py — it is a copy, "
            "and a copy is what lets the two surfaces describe one flag two ways."
        )

    def test_the_rule_fires_on_a_command_missing_a_selector(self) -> None:
        """Prove the checker BITES rather than merely passing — a stub with two of three flags."""
        import typer

        from coder_eval.models import ROW_SELECTOR_FLAGS

        def _stub(
            split: str | None = typer.Option(None, "--split"),
            sample: int | None = typer.Option(None, "--sample"),
        ) -> None: ...

        missing = sorted(set(ROW_SELECTOR_FLAGS.values()) - long_flags(_stub))
        assert missing == ["--sample-per-stratum"]


@pytest.mark.lint
class TestCE045SkipOnIgnoredPath:
    """CE045 — a test may not skip itself on a path this repository does not track.

    `test_existing_history_json_is_left_alone` guarded on `.optimize-skill/ci/history.json`, which
    is gitignored: the file exists in the author's working tree and in no clone, so the test passed
    locally and skipped in CI every time. What it took with it was a whole-package AST scan that
    had therefore never run there.

    The scanner lives in `tests/lint/skip_guards.py`; its boundary is stated there. Wired as a test
    class rather than a `BaseRule` because its subject is `tests/`, and the `ALL_RULES` sweep runs
    over `src/` only.
    """

    REPO: ClassVar[Path] = REPO_ROOT

    @staticmethod
    def _prefixes(gitignore: Path):
        from tests.lint.skip_guards import gitignored_prefixes

        return gitignored_prefixes(gitignore)

    def test_the_tree_has_no_unsuppressed_ignored_skip_guard(self) -> None:
        from tests.lint.skip_guards import find_ignored_skip_guards

        hits = find_ignored_skip_guards(
            sorted((self.REPO / "tests").rglob("*.py")), self._prefixes(self.REPO / ".gitignore")
        )
        assert not hits, "\n  ".join(["a test skips itself on a path no clone has:", *hits])

    def test_it_finds_the_real_subject_once_the_noqa_is_removed(self, tmp_path: Path) -> None:
        """The anti-vacuity test, run against the REAL file in its real shape.

        The guard it targets is `not history.exists()` and carries zero string constants, so a
        scanner reading only the `if` test would report nothing and this whole class would pass
        vacuously. Stripping the suppression from a copy of the real source proves two things at
        once: the scanner resolves the path through the local assignment, and the `# noqa` is what
        silences it rather than blindness.
        """
        from tests.lint.skip_guards import find_ignored_skip_guards

        real = self.REPO / "tests" / "test_optimize_measurements.py"
        source = real.read_text(encoding="utf-8")
        stripped = source.replace("  # noqa: CE045", "")
        assert stripped != source, "the noqa marker moved — re-derive this anchor from the real file"

        copy = tmp_path / "test_optimize_measurements.py"
        copy.write_text(stripped, encoding="utf-8")
        hits = find_ignored_skip_guards([copy], self._prefixes(self.REPO / ".gitignore"))
        assert len(hits) == 1 and ".optimize-skill" in hits[0], hits

    def test_catches_a_skip_on_a_gitignored_path_via_a_local_assignment(self, tmp_path: Path) -> None:
        # The REAL shape: the literal is on the assignment line, not inside the `if`. A fixture
        # written the other way round passes on a blind scanner.
        from tests.lint.skip_guards import find_ignored_skip_guards

        (tmp_path / ".gitignore").write_text(".optimize-skill/\n", encoding="utf-8")
        module = tmp_path / "test_thing.py"
        module.write_text(
            textwrap.dedent(
                """
                import pytest

                ROOT = __import__("pathlib").Path(".")

                def test_thing():
                    p = ROOT / ".optimize-skill" / "x.json"
                    if not p.exists():
                        pytest.skip("absent")
                    assert p.read_text()
                """
            ),
            encoding="utf-8",
        )
        hits = find_ignored_skip_guards([module], self._prefixes(tmp_path / ".gitignore"))
        assert len(hits) == 1 and "test_thing" in hits[0], hits

    def test_a_skip_on_a_tracked_path_is_clean(self, tmp_path: Path) -> None:
        from tests.lint.skip_guards import find_ignored_skip_guards

        (tmp_path / ".gitignore").write_text(".optimize-skill/\n", encoding="utf-8")
        module = tmp_path / "test_thing.py"
        module.write_text(
            textwrap.dedent(
                """
                import pytest

                ROOT = __import__("pathlib").Path(".")

                def test_thing():
                    p = ROOT / "docs" / "USER_GUIDE.md"
                    if not p.exists():
                        pytest.skip("absent")
                """
            ),
            encoding="utf-8",
        )
        assert find_ignored_skip_guards([module], self._prefixes(tmp_path / ".gitignore")) == []

    def test_a_noqa_suppresses_it(self, tmp_path: Path) -> None:
        from tests.lint.skip_guards import find_ignored_skip_guards

        (tmp_path / ".gitignore").write_text(".optimize-skill/\n", encoding="utf-8")
        module = tmp_path / "test_thing.py"
        module.write_text(
            textwrap.dedent(
                """
                import pytest

                ROOT = __import__("pathlib").Path(".")

                def test_thing():
                    p = ROOT / ".optimize-skill" / "x.json"
                    if not p.exists():  # noqa: CE045
                        pytest.skip("absent")
                """
            ),
            encoding="utf-8",
        )
        assert find_ignored_skip_guards([module], self._prefixes(tmp_path / ".gitignore")) == []

    def test_the_skip_message_alone_never_fires_it(self, tmp_path: Path) -> None:
        # Matched on the reconstructed path SEGMENT, never with `in` against the guard's text —
        # otherwise the rule fires on the skip's own message.
        from tests.lint.skip_guards import find_ignored_skip_guards

        (tmp_path / ".gitignore").write_text(".optimize-skill/\n", encoding="utf-8")
        module = tmp_path / "test_thing.py"
        module.write_text(
            textwrap.dedent(
                """
                import pytest

                def test_thing(flag):
                    if not flag:
                        pytest.skip("no .optimize-skill working tree here")
                """
            ),
            encoding="utf-8",
        )
        assert find_ignored_skip_guards([module], self._prefixes(tmp_path / ".gitignore")) == []

    def test_gitignored_prefixes_ignores_globs_and_negations(self, tmp_path: Path) -> None:
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# a comment\n\n*.log\n!keep/\nnested/path/\n.optimize-skill/\nruns/\n", encoding="utf-8")
        assert self._prefixes(gitignore) == {".optimize-skill", "runs"}


@pytest.mark.lint
class TestCE058MirroredResultFieldsAreStamped:
    """CE058 — a mirrored `CriterionResult` field must be assigned at the checker's one seam.

    `CriterionResult` mirrors three fields from the criterion that produced it: `pass_threshold`,
    `gating` and `weight`. All three are stamped in `evaluation/checker.py`. A fourth added to the
    model and not stamped there is SILENT — every result carries the field's default, nothing
    raises, no type checker complains, and every consumer believes the value is real. `weight` is
    what makes it a recurring class rather than a one-off, and the execution gate's dead-weight
    reading treats an unrecorded weight as "not recorded" on a run that recorded everything else.

    **Class-wired, not a `BaseRule`, and that is structural rather than stylistic.**
    `BaseRule.__init__` takes one `filepath` and visits that one file; this predicate spans TWO — a
    field description in `models/results.py` and an assignment in `evaluation/checker.py`. So it
    follows CE057 exactly: detection body in `tests/lint/mirrored_result_fields.py`, nothing under
    `tests/lint/rules/`, `runner.py` untouched.

    **All three functions are required, not any one of them.** A union over the three is satisfied
    by a stamp in `_finalize_result` alone — which is exactly the defect: the two error paths build a
    result for a criterion no checker ran, so the field defaults there with nothing raised.

    **Boundary:** it matches the description TEXT, so a mirrored field that does not say
    `mirrors ` is invisible (hence the non-vacuity assert below); it checks an assignment EXISTS,
    never that the assigned value is right; and it is scoped to `CriterionResult` itself. That last
    one is a decision about the future rather than a live suppression — no subclass field says
    `mirrors ` today, so an unscoped rule would currently detect nothing extra. What makes the
    scoping right anyway is that a subclass field is produced by the criterion that computed it and
    no seam could stamp one.
    """

    CHECKER: ClassVar[Path] = REPO_ROOT / "src" / "coder_eval" / "evaluation" / "checker.py"

    def test_every_mirrored_field_is_stamped_in_the_real_tree(self):
        from tests.lint.mirrored_result_fields import gaps

        found, fields = gaps(self.CHECKER)
        # NON-VACUITY, the CE044/CE045 lesson: a renamed description convention would empty
        # `found` for the wrong reason and report a clean tree.
        assert set(fields) >= {"pass_threshold", "gating", "weight"}, (
            f"GAP: the mirrored-field detector sees {fields} — it must see all three of "
            "pass_threshold, gating and weight, or it is reporting a clean tree because it can no "
            "longer find its subject."
        )
        assert not found, (
            "a CriterionResult field says it mirrors its criterion but evaluation/checker.py does "
            "not assign it on every path, so those results carry the default and nothing raises:\n"
            + "\n".join(f"  {gap.field} — missing from {', '.join(gap.missing_from)}" for gap in found)
        )

    def test_ce058_fires_when_the_seam_stops_stamping_a_field(self, tmp_path: Path):
        # The synthetic violation: a checker source that stamps the older two and forgets `weight`.
        from tests.lint.mirrored_result_fields import gaps

        source = tmp_path / "checker.py"
        source.write_text(
            "class SuccessChecker:\n"
            "    def _finalize_result(self, criterion, result):\n"
            "        result.pass_threshold = criterion.pass_threshold\n"
            "        result.gating = criterion.is_gating\n"
            "        return result\n"
            "\n"
            "    def _missing_checker_result(self, criterion):\n"
            "        return CriterionResult(pass_threshold=criterion.pass_threshold, gating=True)\n"
            "\n"
            "    def _error_result(self, criterion, exc):\n"
            "        return CriterionResult(pass_threshold=criterion.pass_threshold, gating=True)\n",
            encoding="utf-8",
        )
        found, _fields = gaps(source)
        assert [gap.field for gap in found] == ["weight"]
        assert found[0].missing_from == ("_finalize_result", "_missing_checker_result", "_error_result")

    def test_ce058_fires_when_only_the_success_path_stamps_a_field(self, tmp_path: Path):
        """The any-of hole, and the reason the three functions are required EACH.

        A field stamped in `_finalize_result` alone defaults silently on both error paths — which
        build a result for a criterion no checker ran and therefore cannot route through the seam.
        A rule that unioned the three would report this tree as clean.
        """
        from tests.lint.mirrored_result_fields import gaps

        source = tmp_path / "checker.py"
        source.write_text(
            "class SuccessChecker:\n"
            "    def _finalize_result(self, criterion, result):\n"
            "        result.pass_threshold = criterion.pass_threshold\n"
            "        result.gating = criterion.is_gating\n"
            "        result.weight = criterion.weight\n"
            "        return result\n"
            "\n"
            "    def _missing_checker_result(self, criterion):\n"
            "        return CriterionResult(pass_threshold=criterion.pass_threshold, gating=True)\n"
            "\n"
            "    def _error_result(self, criterion, exc):\n"
            "        return CriterionResult(pass_threshold=criterion.pass_threshold, gating=True)\n",
            encoding="utf-8",
        )
        found, _fields = gaps(source)
        assert [gap.field for gap in found] == ["weight"]
        assert found[0].missing_from == ("_missing_checker_result", "_error_result")

    def test_a_stamping_function_missing_from_the_file_reports_every_field(self, tmp_path: Path):
        # A renamed or deleted error path must not pass over silently: with no matching function
        # there is nothing to union with, so every mirrored field is missing from it.
        from tests.lint.mirrored_result_fields import gaps

        source = tmp_path / "checker.py"
        source.write_text("def unrelated():\n    return None\n", encoding="utf-8")
        found, fields = gaps(source)
        assert {gap.field for gap in found} == set(fields)

    def test_a_stamp_outside_the_named_functions_does_not_count(self, tmp_path: Path):
        # The seam is the point. A weight assigned in some other helper leaves the two error paths
        # unstamped, which is the half of the defect a whole-file scan would miss.
        from tests.lint.mirrored_result_fields import gaps

        source = tmp_path / "checker.py"
        source.write_text(
            "def somewhere_else(criterion, result):\n    result.weight = criterion.weight\n",
            encoding="utf-8",
        )
        found, _fields = gaps(source)
        assert "weight" in [gap.field for gap in found]

    def test_ce058_reads_the_base_model_only(self):
        """The scoping, asserted on a subclass field that DOES claim to mirror something.

        Written this way because the obvious version cannot fail: no shipped subclass field says
        `mirrors `, so comparing `mirrored_fields()` against the real subclasses' own fields
        compares two sets that are disjoint by construction — the vacuous pass the module docstring
        cites CE044/CE045 for. A subclass declaring the marker is the only input that distinguishes
        "reads the base model" from "walks every subclass", and it must not be detected: no seam
        could stamp a field the criterion itself produces.
        """
        from pydantic import Field

        from coder_eval.models import ClassificationCriterionResult, CriterionResult
        from tests.lint.mirrored_result_fields import MIRROR_MARKER, mirrored_fields

        class _SubclassClaimingToMirror(CriterionResult):
            observed_thing: str = Field(default="", description=f"{MIRROR_MARKER}BaseSuccessCriterion.nothing")

        assert MIRROR_MARKER in (_SubclassClaimingToMirror.model_fields["observed_thing"].description or "")
        assert "observed_thing" not in mirrored_fields()
        # And the real subclasses' own fields are absent for the ordinary reason too.
        subclass_only = set(ClassificationCriterionResult.model_fields) - set(CriterionResult.model_fields)
        assert subclass_only, "GAP: the subclass declares no field of its own"
        assert not (set(mirrored_fields()) & subclass_only)

    def test_nothing_was_added_under_the_baserule_directory(self):
        # CE057's filing decision, asserted the same way: `tests/lint/rules/` holds `BaseRule`
        # modules and `runner.py`'s id-uniqueness assert covers `ALL_RULES` alone.
        from tests.lint.runner import ALL_RULES

        assert not (TESTS_ROOT / "lint" / "rules" / "ce058_mirrored_result_fields.py").exists()
        assert not any(getattr(rule, "id", None) == "CE058" for rule in ALL_RULES)
        # Anti-vacuity for the probe itself: it read `rule_id` for three releases, an attribute
        # `BaseRule` does not declare, so all three copies of this assertion could never fail.
        assert any(getattr(rule, "id", None) == "CE059" for rule in ALL_RULES), (
            "the attribute this probe reads is wrong again — it must be able to SEE a wired rule"
        )


@pytest.mark.lint
class TestTheLayeringCoverageAssertRunsAtCollection:
    """`tests/test_optimize_layering.py` must check the rank ladder's COVERAGE at module scope.

    Written because it was silently lost. When the optimize test monolith was split, the splitter
    moved every top-level statement that defines a NAME — and a bare `assert` defines none, so two
    module-scope coverage asserts vanished. Nothing went red: an assert is not a test, and the
    node-id parity check that guarded the split (803 ids, identical modulo the file component) cannot
    see a statement that collects nothing.

    Module scope is the point, not a stylistic preference. A new module in `coder_eval/optimize/` with
    no rank must stop that file COLLECTING, rather than failing whichever single test happens to
    notice — because the failure it guards is "a layering test silently iterates over an incomplete
    set", and a test that never ran cannot report that.

    The boundary: it asserts the assert EXISTS at module scope and mentions the coverage predicate.
    It does not check what the predicate returns — the file's own tests do that.
    """

    LAYERING = TESTS_ROOT / "test_optimize_layering.py"

    def test_a_module_scope_assert_calls_the_coverage_predicate(self) -> None:
        tree = ast.parse(self.LAYERING.read_text(encoding="utf-8"))
        called = {
            node.func.id
            for stmt in tree.body
            if isinstance(stmt, ast.Assert)
            for node in ast.walk(stmt)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_rank_coverage_gap" in called, (
            "no module-scope assert calls `_rank_coverage_gap` in tests/test_optimize_layering.py. A "
            "new module under coder_eval/optimize/ would then join the family unranked and be "
            "silently skipped by whichever layering test iterates the rank map — which is what "
            "happened once already, when a file split dropped this assert because it defines no name"
        )

    def test_the_predicate_the_assert_calls_is_defined_in_that_file(self) -> None:
        """Anti-vacuity: a renamed predicate must fail loudly rather than leave a string meaning nothing.

        By AST rather than by importing the module: importing it would couple two test files and, worse,
        run its module-scope asserts inside THIS file's collection, so an unranked module would report
        the failure here instead of where it belongs.
        """
        tree = ast.parse(self.LAYERING.read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert "_rank_coverage_gap" in defined, (
            "`_rank_coverage_gap` is not defined in tests/test_optimize_layering.py — the assertion "
            "above is matching a name that no longer exists"
        )


@pytest.mark.lint
class TestTheSharedFixtureModuleIsNotInverted:
    """`tests/optimize_fixtures.py` may not import from the test tree.

    A lint-layer module (`tests/lint/computed_claims.py`) was importing private helpers out of
    `tests/test_optimize_gate.py`: the lint layer reasons ABOUT the tree, so reaching into a test
    file for fixtures inverts the dependency, and it is what blocked splitting that file. Extracting
    the builders fixes it only while the new module stays a leaf — if it ever imports a `test_*`
    module or `tests.lint`, the inversion is back one hop over.

    A test rather than a CE rule: one file, one predicate, and a rule would need a scope no other
    module in the tree shares. Two boundaries: it reads IMPORT statements only, so a runtime
    `importlib.import_module("tests.…")` is invisible to it; and it routes every module string
    through `resolved_module` (CE051) rather than reading `node.module`, treating an unresolvable
    RELATIVE import as an offender — inside `tests/`, which is a package, a relative import can only
    point back into the test tree.
    """

    FIXTURES = TESTS_ROOT / "optimize_fixtures.py"

    @staticmethod
    def _test_tree_imports(path: Path) -> list[str]:
        """Import statements in ``path`` that reach into the test tree."""
        offenders: list[str] = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = resolved_module(node, str(path))
                if module is None:
                    # `resolved_module` answers None outside a `coder_eval/` package root, which is
                    # every path here. A level-0 import is absolute and resolves straight through;
                    # anything left is relative, and relative from inside `tests/` is the test tree.
                    if node.level:
                        offenders.append(f"{path.name}:{node.lineno} -> a relative import")
                elif module.split(".")[0] == "tests":
                    # `from tests import test_optimize_gate` puts the offending name in `node.names`
                    # and leaves `module` as the bare package — the case `resolved_module`'s own
                    # docstring warns about, so the imported NAMES are read here too.
                    for alias in node.names:
                        offenders.append(f"{path.name}:{node.lineno} -> {module}.{alias.name}")
            elif isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{node.lineno} -> {a.name}" for a in node.names if a.name.split(".")[0] == "tests"
                ]
        return offenders

    def test_the_fixture_module_imports_nothing_from_the_test_tree(self) -> None:
        offenders = self._test_tree_imports(self.FIXTURES)
        assert offenders == [], (
            f"tests/optimize_fixtures.py imports from the test tree: {offenders}. It is a leaf on "
            "`tests/lint/import_resolution.py`'s precedent — `coder_eval` and stdlib only, or the "
            "inverted dependency it was extracted to remove is back one hop over"
        )

    def test_the_scan_can_see_both_import_shapes(self, tmp_path: Path) -> None:
        """Anti-vacuity: a scan that resolves nothing reports [] on a file full of violations.

        The real subject is clean, so the detector is proven on a synthetic module — the CE044/CE045
        lesson, where a sensor passed because it was looking at nothing.
        """
        probe = tmp_path / "probe.py"
        probe.write_text(
            "from tests.optimize_fixtures import write_row\n"
            "import tests.test_optimize_layering\n"
            "from tests import test_optimize_layering as layering_tests\n"
            "from .sibling import thing\n",
            encoding="utf-8",
        )
        found = self._test_tree_imports(probe)
        assert len(found) == 4, f"the scan misses one of the four import shapes: {found}"
        # The `node.level` branch specifically — the one added because CE051 fired on reading
        # `node.module`, and therefore the one most likely to be wrong and never exercised.
        assert any("a relative import" in entry for entry in found)
        # And the `from tests import <module>` shape, whose offending name is in `node.names`.
        assert any(entry.endswith("tests.test_optimize_layering") for entry in found)

    def test_it_is_not_collected_as_a_test_module(self) -> None:
        # Named `optimize_fixtures.py`, not `test_optimize_fixtures.py`: pytest would collect it, and
        # a builder taking a `variant` argument would be reported as a test with a missing fixture.
        assert not self.FIXTURES.name.startswith("test_")
        assert not any(
            node.name.startswith("test_")
            for node in ast.parse(self.FIXTURES.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef | ast.ClassDef)
        ), "a name starting with `test_` in a non-test module is a test nothing runs"

    def test_no_lint_module_reaches_into_a_test_file(self) -> None:
        """The original defect, asserted where it happened rather than only where it was fixed."""
        lint_root = TESTS_ROOT / "lint"
        assert lint_root.is_dir(), "anchor drifted — this would be checking nothing"
        offenders = [
            entry
            for path in sorted(lint_root.rglob("*.py"))
            for entry in self._test_tree_imports(path)
            if "test_" in entry.split("->")[-1]
        ]
        assert offenders == [], (
            f"a lint-layer module imports from a test module: {offenders}. Shared builders belong in "
            "`tests/optimize_fixtures.py`, which both layers may import"
        )
