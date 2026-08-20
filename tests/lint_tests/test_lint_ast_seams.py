"""Lint tests: ast seams."""

import ast
import re
import textwrap
from pathlib import Path
from typing import ClassVar

import pytest

from tests.lint.rules.ce047_no_bare_assert_in_cli import NoBareAssertInCli
from tests.lint.rules.ce048_no_bare_model_copy_update import NoBareModelCopyUpdate
from tests.lint.rules.ce050_escape_untrusted_markup import EscapeUntrustedMarkup
from tests.lint.rules.ce053_run_tree_readers_reconcile import _RECONCILE, RunTreeReadersReconcile
from tests.lint.rules.ce054_result_status_single_seam import ResultStatusSingleSeam
from tests.lint.rules.ce059_no_sibling_private_imports import NoSiblingPrivateImports
from tests.lint.runner import ALL_RULES, check_paths
from tests.lint_tests.shared import (
    REPO_ROOT,
    SRC,
)


@pytest.mark.lint
class TestCE037SingleF1Implementation:
    """CE037 flags a second `2 * p * r / (p + r)` anywhere but the canonical module.

    A second F1 implementation agrees with the first on ordinary input and diverges exactly
    where a class has no predictions — so the gate would silently disagree with the numbers
    the run itself reported.
    """

    _F1 = "f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0"

    @staticmethod
    def _run(src: str, filepath: str = "<test>"):
        import ast

        from tests.lint.rules.ce037_single_f1_implementation import SingleF1Implementation

        return SingleF1Implementation(filepath).check(ast.parse(src))

    def test_flags_a_second_f1_implementation(self):
        assert self._run(self._F1)

    def test_flags_an_unguarded_copy(self):
        # A copy that drops the ternary guard is still a second implementation — and the more
        # dangerous one, since it raises instead of applying the 0.0 convention.
        assert self._run("f1 = 2 * p * r / (p + r)")

    def test_flags_a_parenthesized_numerator(self):
        assert self._run("f1 = 2 * (p * r) / (p + r)")

    def test_flags_attribute_and_subscript_operands(self):
        # The same formula spelled off an object or a metrics dict — the shapes a consumer
        # reading `suite.json` would most plausibly write.
        assert self._run("f1 = 2 * self.precision * self.recall / (self.precision + self.recall)")
        assert self._run('f1 = 2 * m["precision"] * m["recall"] / (m["precision"] + m["recall"])')

    def test_does_not_claim_to_catch_algebraic_rewrites(self):
        # Pins the rule's documented boundary: it catches the copy-paste shape, not every way
        # of computing F1. A test asserting otherwise would misrepresent what `make lint` proves.
        assert not self._run("f1 = tp / (tp + 0.5 * (fp + fn))")
        assert not self._run("f1 = 2 / (1 / p + 1 / r)")

    def test_allows_the_canonical_module(self):
        assert not self._run(self._F1, filepath="/repo/src/coder_eval/criteria/_classification_aggregate.py")

    def test_ignores_an_unrelated_division(self):
        assert not self._run("x = 2 * a * b / (c + d)")
        assert not self._run("x = 3 * p * r / (p + r)")
        assert not self._run("x = (p + r) / 2")
        assert not self._run("x = 2 * p * r / (p * r)")


@pytest.mark.lint
class TestCE040BootstrapPFloorSeam:
    """CE040 flags a re-derivation of the bootstrap's p-floor outside `reports_stats.py`.

    The floor is a property of the ESTIMATOR — `1/m` under the naive count, `2/(m+1)` under
    Phipson & Smyth — so a consumer that spells it inline keeps reporting the old value after
    the estimator moves. That is exactly what happened: four sites in `optimize/gate.py` and two
    field descriptions understated it by 2x, in a block a user reads to decide whether to spend.
    """

    @staticmethod
    def _run(src: str, filepath: str = "<test>"):
        import ast

        from tests.lint.rules.ce040_bootstrap_p_floor_seam import BootstrapPFloorSeam

        return BootstrapPFloorSeam(filepath).check(ast.parse(src))

    def test_flags_the_old_naive_floor(self):
        assert self._run("floor = 1.0 / n_resamples")

    def test_flags_an_attribute_spelling(self):
        # The shape the gate actually had: `1.0 / verdict.n_resamples` in a rendered string.
        assert self._run("floor = 1.0 / verdict.n_resamples")
        assert self._run("trigger = 5.0 / verdict.n_resamples")

    def test_flags_a_re_derivation_of_the_current_floor(self):
        # A "helpful" inline copy of the CURRENT estimator's floor is the same defect one
        # estimator later, so it is caught too.
        assert self._run("floor = 2.0 / (n_resamples + 1)")
        assert self._run("floor = 2.0 / (verdict.n_resamples + 1)")

    def test_allows_the_canonical_module(self):
        assert not self._run("return 2.0 / (n_resamples + 1)", filepath="/repo/src/coder_eval/reports_stats.py")

    def test_does_not_claim_to_catch_a_renamed_count(self):
        # Pins the documented boundary: a floor derived from a differently-named local is NOT
        # matched. A test asserting otherwise would misrepresent what `make lint` proves.
        assert not self._run("floor = 2.0 / (m + 1)")
        assert not self._run("floor = 1.0 / draws")

    def test_ignores_unrelated_arithmetic(self):
        assert not self._run("rate = hits / total")
        assert not self._run("x = n_resamples / 2")  # dividing BY 2, not by the count
        assert not self._run("share = n_resamples * 1.0")


@pytest.mark.lint
class TestCE041NoModelDictSplat:
    """CE041 flags a `**<dict>` splat into a model imported from `coder_eval.models`.

    A splat means a mistyped or renamed key lands in no field at all: pydantic ignores extras
    by default, so the model is built with that field at its default and nothing raises. Both
    optimize-gate verdicts were built that way, on models whose job is to say what a promotion
    decision rests on.
    """

    _IMPORT = "from coder_eval.models import ActivationGateVerdict\n"

    @staticmethod
    def _run(src: str, filepath: str = "<test>"):
        import ast

        from tests.lint.rules.ce041_no_model_dict_splat import NoModelDictSplat

        return NoModelDictSplat(filepath).check(ast.parse(src))

    def test_flags_a_bare_name_splat(self):
        # The exact shape `activation_gate` had: one dict, splatted into both returns.
        assert self._run(self._IMPORT + "ActivationGateVerdict(**verdict_kwargs, mean_diff=None)")

    def test_flags_a_merged_dict_display_splat(self):
        # The exact shape `execution_gate`'s `_verdict` had.
        assert self._run(self._IMPORT + "ActivationGateVerdict(**{**base, **overrides})")

    @pytest.mark.parametrize(
        "splat",
        [
            "**other.model_dump()",
            "**self.base",
            '**cfg["v"]',
            "**dict(base, x=1)",
            "**(base | overrides)",
            "**{k: v for k, v in items}",
        ],
    )
    def test_flags_every_operand_shape_not_only_a_name_or_a_dict_display(self, splat: str):
        # `**x.model_dump()` is how this defect usually arrives, and a dict comprehension is not
        # an `ast.Dict`. Narrowing the operand would name a boundary users read as "splats are
        # covered" while leaving the commonest spelling through.
        assert self._run(self._IMPORT + f"ActivationGateVerdict({splat})")

    def test_flags_a_renamed_import(self):
        assert self._run("from coder_eval.models import ActivationGateVerdict as V\nV(**kwargs)")

    def test_flags_a_model_from_a_submodule_import(self):
        assert self._run("from coder_eval.models.optimize import GuardrailCheck\nGuardrailCheck(**d)")

    def test_allows_literal_keywords(self):
        assert not self._run(self._IMPORT + "ActivationGateVerdict(suite_id='s', mean_diff=None)")

    def test_allows_model_validate_on_a_dict(self):
        # The intended escape: `model_validate` VALIDATES the payload rather than splatting it.
        assert not self._run(self._IMPORT + "ActivationGateVerdict.model_validate(payload)")

    def test_ignores_a_splat_into_something_that_is_not_a_model(self):
        assert not self._run(self._IMPORT + "dict(**kwargs)")
        assert not self._run("from elsewhere import Thing\nThing(**kwargs)")

    def test_does_not_claim_to_catch_an_alias_or_a_factory(self):
        # Pins the documented boundary. A test asserting otherwise would misrepresent what
        # `make lint` proves — the callee NAME has to be the imported one.
        assert not self._run(self._IMPORT + "Alias = ActivationGateVerdict\nAlias(**kwargs)")
        assert not self._run(self._IMPORT + "build_verdict(**kwargs)")


@pytest.mark.lint
class TestCE042ReplicatePaddingSeam:
    """CE042 flags a hand-rolled replicate-directory padding outside `path_utils.py`.

    `replicate_subdir_name` owns the two-digit name and its docstring already tells callers so.
    Four sites pinned it anyway; the day the padding widens none of them raises — the globs match
    nothing, both optimize gates load ZERO rows, and the zero-row note blames a path typo.
    """

    _CANONICAL = "/repo/src/coder_eval/path_utils.py"

    @staticmethod
    def _run(src: str, filepath: str = "<test>"):
        import ast

        from tests.lint.rules.ce042_replicate_padding_seam import ReplicatePaddingSeam

        return ReplicatePaddingSeam(filepath).check(ast.parse(src))

    def test_flags_a_replicate_02d_format_spec(self):
        # The shape `reports_junit` had, twice.
        assert self._run('candidate = task_dir / f"{replicate_index:02d}" / "task.json"')

    def test_flags_an_attribute_form(self):
        assert self._run('name = f"{row.replicate_index:02d}"')

    def test_flags_a_padded_glob_literal(self):
        # Both depths: the gate's suite-level glob and the stats loader's task-level one.
        assert self._run('for p in suite_dir.glob("*/[0-9][0-9]/task.json"): pass')
        assert self._run('for p in task_dir.glob("[0-9][0-9]"): pass')

    def test_allows_the_canonical_module(self):
        assert not self._run('return f"{replicate_index:02d}"', filepath=self._CANONICAL)
        assert not self._run('PATTERN = "[0-9][0-9]"', filepath=self._CANONICAL)

    def test_allows_an_unrelated_two_digit_pad(self):
        # CE040's lesson applied: keyed on the NAME, not on the `02d` shape. Otherwise the rule
        # tells a clock formatter to call `replicate_subdir_name`.
        assert not self._run('stamp = f"{minutes:02d}:{seconds:02d}"')
        assert not self._run('label = f"{attempt:02d}"')

    def test_allows_the_helper_call(self):
        assert not self._run("candidate = task_dir / replicate_subdir_name(i) / 'task.json'")
        assert not self._run('for p in task_dir.glob("*/task.json"): pass')

    def test_does_not_claim_to_catch_zfill_or_a_percent_format(self):
        # Pins the documented boundary. A test asserting otherwise would misrepresent what
        # `make lint` proves — and a `??` glob is a legitimate spelling nobody should "unify".
        assert not self._run("name = str(replicate_index).zfill(2)")
        assert not self._run('name = "%02d" % replicate_index')
        assert not self._run('for p in task_dir.glob("??/task.json"): pass')

    def test_the_unconditional_half_fires_on_prose_and_on_an_unrelated_regex(self):
        # The other side of shape 2 being unconditional, pinned rather than discovered: a docstring
        # is a string constant like any other (deliberate — prose pinning the old glob lies the day
        # the padding widens), and so is a genuine non-replicate regex, which is the one false
        # positive the rule's docstring names a suppression for.
        assert self._run('"""Walks `<task_id>/[0-9][0-9]/task.json`."""')
        assert self._run('STAMP = re.compile(r"[0-9][0-9]:[0-9][0-9]")')


@pytest.mark.lint
class TestCE047NoBareAssertInCli:
    """CE047 — no bare `assert` under `src/coder_eval/cli/`.

    `python -O` strips asserts. An internal invariant survives that; argument validation does not,
    and `plan_command.py` shipped a bare `assert` as its only argument validation. The rule's
    boundary — why it is scoped to one directory when `src/` holds 77 asserts — is on the rule
    module.
    """

    def test_it_fires_on_an_assert_in_cli(self) -> None:
        source = textwrap.dedent(
            """
            def plan_command(paths):
                assert paths, "at least one path"
            """
        )
        rule = NoBareAssertInCli("src/coder_eval/cli/plan_command.py")
        violations = rule.check(ast.parse(source))
        assert len(violations) == 1 and violations[0].rule_id == "CE047", violations

    def test_it_is_silent_outside_cli(self) -> None:
        source = textwrap.dedent(
            """
            def _pair(rows):
                assert rows, "internal invariant"
            """
        )
        assert NoBareAssertInCli("src/coder_eval/optimize/gate.py").check(ast.parse(source)) == []

    def test_the_scope_match_survives_windows_separators(self) -> None:
        source = "def f():\n    assert True\n"
        rule = NoBareAssertInCli("src\\coder_eval\\cli\\run_command.py")
        assert len(rule.check(ast.parse(source))) == 1

    def test_the_narrow_scope_is_still_justified(self) -> None:
        """The docstring justifies the narrow scope with a measured number; re-measure the CLAIM.

        Asserted as a BOUND, not as equality. The load-bearing part is "`src/` holds many asserts
        and almost all are legitimate internal invariants, so a repo-wide rule would be noise" —
        an exact-equality anchor would additionally turn every PR that adds or removes an assert
        anywhere in the package red for a rule it never touched, which is friction the claim does
        not need.
        """
        import tests.lint.rules.ce047_no_bare_assert_in_cli as module

        def _asserts(paths) -> int:
            return sum(
                1
                for path in paths
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Assert)
            )

        total = _asserts(sorted(SRC.rglob("*.py")))
        assert total >= 50, (
            f"src/ now holds only {total} assert statements; CE047's boundary justifies a "
            "one-directory scope by there being far too many to gate repo-wide."
        )
        assert re.search(r"\*\*\d+\*\* ``assert`` statements", module.__doc__ or ""), (
            "CE047's docstring no longer states the measured count its narrow scope rests on."
        )
        # And the part that IS exact: the scoped directory is clean, so the rule is a live guard
        # rather than a wish.
        assert _asserts(sorted((SRC / "coder_eval" / "cli").rglob("*.py"))) == 0


@pytest.mark.lint
class TestCE048NoBareModelCopyUpdate:
    """CE048 — `model_copy(update={...})` is replaced by `models.copy_with` everywhere in `src/`.

    `model_copy(update=)` does not check the update's KEYS: a mistyped one lands as a bare instance
    attribute, absent from `model_dump()` entirely, with the intended field left at its default and
    nothing raised. Both optimize-gate verdicts were written that way — on the models that say what
    a promotion decision rests on.

    CE041's shape one verb over: `copy_with` catches it at runtime, literal keywords catch it
    statically, and this rule keeps the static half from coming back. The boundary is on the rule
    module.
    """

    def test_it_fires_on_a_bare_update_call(self) -> None:
        source = "def f(v, notes):\n    return v.model_copy(update={'promoted': False, 'notes': notes})\n"
        violations = NoBareModelCopyUpdate("src/coder_eval/optimize/gate.py").check(ast.parse(source))
        assert len(violations) == 1 and violations[0].rule_id == "CE048", violations

    def test_it_is_silent_on_copy_with(self) -> None:
        source = "def f(v, notes):\n    return copy_with(v, promoted=False, notes=notes)\n"
        assert NoBareModelCopyUpdate("src/coder_eval/optimize/gate.py").check(ast.parse(source)) == []

    def test_it_is_silent_on_a_model_copy_with_no_update(self) -> None:
        # `model_copy(deep=True)` copies without setting anything, so no key can be wrong.
        source = "def f(v):\n    return v.model_copy(deep=True)\n"
        assert NoBareModelCopyUpdate("src/coder_eval/orchestrator.py").check(ast.parse(source)) == []

    def test_the_canonical_module_is_exempt(self) -> None:
        # `copy_with` itself must make the call — it is the thing that checks the keys first.
        source = "def copy_with(model, /, **updates):\n    return model.model_copy(update=updates)\n"
        assert NoBareModelCopyUpdate("src/coder_eval/models/copy_with.py").check(ast.parse(source)) == []
        assert NoBareModelCopyUpdate("src\\coder_eval\\models\\copy_with.py").check(ast.parse(source)) == []

    def test_only_the_canonical_module_remains(self) -> None:
        """Anti-vacuity, and the acceptance criterion in test form.

        The rule is satisfied by suppressions as easily as by conversions, so the number of live
        `model_copy(update=)` calls in `src/` is pinned rather than left to the sweep. There is
        now exactly one, and it is the helper's own — `criteria/agent_judge.py` held the last
        exemption until `AgentJudgeCriterion.agent` narrowed to a single model and its overlay
        became a `model_validate` of a merged dump.
        """
        offenders = [
            path.relative_to(SRC).as_posix()
            for path in sorted(SRC.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "model_copy"
            and any(k.arg == "update" for k in node.keywords)
        ]
        assert offenders == [
            # The canonical module, which is the thing that checks the keys — and nothing else.
            "coder_eval/models/copy_with.py",
        ], offenders

    def test_ce041_no_longer_calls_this_hole_open(self) -> None:
        # CE041's docstring named `model_copy(update=)` as the hole it deliberately left; it now
        # points at the rule that closed it, so the two rules cannot describe the tree differently.
        import tests.lint.rules.ce041_no_model_dict_splat as ce041

        assert "CE048" in (ce041.__doc__ or "")


@pytest.mark.lint
class TestCE050EscapeUntrustedMarkup:
    """CE050 — an interpolated value in a Rich-markup `console.print` must be escaped.

    Rich reads `[...]` in the VALUE as markup too, so a task id, exception message or run.json
    fragment carrying a bracket renders wrong or vanishes — precisely when something has already
    gone wrong and the diagnostic matters most. The rule's boundary (call-site f-strings only, no
    trust analysis) is on the rule module.
    """

    def _check(self, source: str, path: str = "src/coder_eval/cli/plan_command.py"):
        return EscapeUntrustedMarkup(path).check(ast.parse(textwrap.dedent(source)))

    def test_it_fires_on_a_bare_value_beside_a_markup_tag(self) -> None:
        violations = self._check('console.print(f"[red]Error: {e}[/red]")')
        assert len(violations) == 1 and violations[0].rule_id == "CE050", violations

    def test_it_is_silent_when_the_value_is_escaped(self) -> None:
        assert self._check('console.print(f"[red]Error: {escape(str(e))}[/red]")') == []

    def test_it_is_silent_on_an_fstring_with_no_markup(self) -> None:
        # Rich passes a tag-free string through unchanged, so there is nothing to escape.
        assert self._check('console.print(f"Error: {e}")') == []

    def test_it_is_silent_outside_cli(self) -> None:
        assert self._check('console.print(f"[red]{e}[/red]")', "src/coder_eval/reports.py") == []

    def test_it_flags_each_unescaped_value_separately(self) -> None:
        # Two interpolations, one already escaped: only the bare one is reported.
        violations = self._check('console.print(f"[dim]{escape(str(a))} then {b}[/dim]")')
        assert len(violations) == 1

    def test_a_numeric_format_spec_is_exempt(self) -> None:
        # `str.format` raises on a non-numeric value for these specs, so the value cannot be a
        # string and cannot carry a bracket.
        assert self._check('console.print(f"[dim]{cost:.2f} in {n:,} rows[/dim]")') == []

    def test_a_len_call_is_exempt(self) -> None:
        assert self._check('console.print(f"[dim]{len(rows)} rows[/dim]")') == []

    def test_a_bare_name_is_not_assumed_numeric(self) -> None:
        # The asymmetry is deliberate: the rule cannot infer types, and the cost of being wrong
        # this way is one escape() on a number rather than a corrupted diagnostic.
        assert len(self._check('console.print(f"[dim]{count} rows[/dim]")')) == 1

    def test_a_subscript_in_the_text_is_not_a_markup_tag(self) -> None:
        # `[0]` and `[Errno 66]` are prose, not tags — an f-string containing only those is not
        # the shape this rule is about, and treating them as markup would fire on every one.
        assert self._check('console.print(f"row [0] failed: {e}")') == []

    def test_the_scope_match_survives_windows_separators(self) -> None:
        violations = self._check('console.print(f"[red]{e}[/red]")', "src\\coder_eval\\cli\\run_command.py")
        assert len(violations) == 1

    def test_noqa_suppresses_it(self, tmp_path: Path) -> None:
        """Through the runner, which is what honours `# noqa` — the rule itself does not."""
        source = 'console.print(f"[dim]{count} rows[/dim]")  # noqa: CE050\n'
        path = tmp_path / "plan_command.py"
        path.write_text(source, encoding="utf-8")
        assert check_paths([path], rules=[EscapeUntrustedMarkup]) == []

    def test_the_id_is_unique_across_every_wired_rule(self) -> None:
        # `tests/lint/runner.py`'s assert covers ALL_RULES only, so a class-wired id can collide
        # with a BaseRule's without failing anything.
        assert [r.id for r in ALL_RULES].count("CE050") == 1

    def test_it_matches_any_sink_not_just_console_print(self) -> None:
        """Keying on `console.print` fails open the moment output is BUFFERED.

        Measured: the same change that added this rule moved `plan`'s per-file output behind an
        `emit` sink and a `detail.append` list, and five markup-bearing f-strings left the rule's
        view on the very file it was written for. A Rich tag in a `cli/` f-string is markup
        because it reaches a console eventually; the hand-off on the way does not change that.
        """
        for sink in ('emit(f"[red]{e}[/red]")', 'detail.append(f"[red]{e}[/red]")', 'log(f"[red]{e}[/red]")'):
            assert len(self._check(sink)) == 1, sink

    def test_it_sees_through_a_plus_concatenation(self) -> None:
        """Rich parses the JOINED result as one markup string, so the tag and the interpolation
        can sit in different operands — and `+`-joined multi-line strings are this codebase's
        dominant style, since pyright forbids implicit adjacent-string concatenation here.
        Measured: two LIVE unescaped sites (`run_command.py`'s --resume drift warning and
        `aggregate_command.py`'s summary) sat unflagged for exactly this reason.
        """
        source = 'console.print(f"[yellow]warn[/yellow] " + f"path {run_dir} changed")'
        assert len(self._check(source)) == 1

    def test_a_plus_chain_with_no_markup_anywhere_is_silent(self) -> None:
        assert self._check('console.print(f"warn " + f"path {run_dir}")') == []

    def test_arithmetic_over_counts_is_exempt(self) -> None:
        # `{len(a) - len(b)}` is a count; charging a suppression for it would teach the reader
        # that noqa is bookkeeping.
        assert self._check('console.print(f"[dim]{len(labels) - len(labelled)} of {len(labels)}[/dim]")') == []

    def test_the_tag_pattern_matches_what_rich_actually_treats_as_markup(self) -> None:
        # Verified against the installed rich, not inferred: an OPENING tag's first character
        # must be lowercase, `#` or `@`, so `[Errno 66]` prints literally. The earlier pattern
        # accepted it while MISSING `[#ff0000]` and `[@handler]`, which Rich does parse.
        assert self._check('console.print(f"[Errno 66] failed: {e}")') == [], "prose is not markup"
        assert self._check('console.print(f"row [0] failed: {e}")') == [], "a subscript is not markup"
        for real_tag in ("[#ff0000]", "[@handler]", "[bold]"):
            assert len(self._check(f'console.print(f"{real_tag}{{e}}[/]")')) == 1, real_tag
        # A CLOSING tag is `[/` plus anything — and an unmatched one RAISES MarkupError, so an
        # unescaped value carrying one crashes the command rather than merely rendering wrong.
        assert len(self._check('console.print(f"[/BOLD] {e}")')) == 1

    def test_a_local_assigned_from_escape_counts_as_escaped(self) -> None:
        # Hoisting an escape out of a long line is formatting, not a trust decision, and charging
        # a noqa for it teaches the reader that suppressions are bookkeeping.
        source = """
            variant_id = escape(str(variant.variant_id))
            detail.append(f"    [dim]Variant '{variant_id}'[/dim]")
        """
        assert self._check(source) == []

    def test_the_escaped_local_may_be_assigned_after_its_use(self) -> None:
        # The set is a whole-file fact collected before the walk; an assignment can follow the
        # call textually (a helper defined below its caller).
        source = """
            def render():
                detail.append(f"[dim]{name}[/dim]")

            def build():
                name = escape(str(x))
        """
        assert self._check(source) == []

    def test_a_local_assigned_from_something_else_is_not_escaped(self) -> None:
        source = """
            variant_id = str(variant.variant_id)
            detail.append(f"    [dim]Variant '{variant_id}'[/dim]")
        """
        assert len(self._check(source)) == 1

    def test_the_cli_package_is_clean(self) -> None:
        """The remediation held: every flagged site is escaped or carries a reasoned noqa.

        A rule reporting zero because it is broken and one reporting zero for the right reason are
        indistinguishable without the positive fixtures above — which is why they come first.
        """
        offenders = [
            f"{v.file}:{v.line}" for v in check_paths([Path("src/coder_eval/cli")], rules=[EscapeUntrustedMarkup])
        ]
        assert offenders == [], offenders


@pytest.mark.lint
class TestCE053RunTreeReadersReconcile:
    """CE053 — a run-tree reader in the optimize family reconciles, or says why not.

    Written after `measure_noise_floor`, `measure_execution_noise_floor` and `arm_row_scores`
    were measured returning confident numbers over dirs `activation_gate` correctly refused.
    The failure is silent in every case — a contaminated tree loads, parses and returns.
    """

    GATE = "src/coder_eval/optimize/gate.py"

    def _check(self, source: str, path: str | None = None) -> list[object]:
        return list(RunTreeReadersReconcile(path or self.GATE).check(ast.parse(textwrap.dedent(source))))

    def test_it_fires_on_a_reader_that_never_reconciles(self) -> None:
        source = """
            def measure_noise_floor(run_dirs, variant_id, suite_id):
                per_dir = [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]
                return _floor(per_dir)
        """
        violations = self._check(source)
        assert len(violations) == 1, violations
        assert violations[0].rule_id == "CE053"  # type: ignore[attr-defined]

    def test_it_is_silent_when_the_reader_reconciles(self) -> None:
        source = """
            def measure_noise_floor(run_dirs, variant_id, suite_id):
                stale, _unknown = reconcile_arms([(variant_id, run_dirs)], suite_id)
                if stale:
                    return None
                return [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]
        """
        assert self._check(source) == []

    def test_the_primitive_call_also_satisfies_it(self) -> None:
        # `execution_gate`'s grain: one run dir per variant, and it needs the per-dir result.
        source = """
            def execution_gate(run_dir, incumbent_variant, candidate_variant, suite_id):
                for variant in (incumbent_variant, candidate_variant):
                    if reconcile_tree_against_run_json(run_dir, variant, suite_id).unrecorded:
                        return None
                return load_arm_rows([run_dir], incumbent_variant, suite_id)
        """
        assert self._check(source) == []

    def test_one_violation_per_function_however_many_reads(self) -> None:
        # `load_and_pair` reads twice, once per arm. Two noqas for one fault would be noise.
        source = """
            def load_and_pair(incumbent_run_dirs, candidate_run_dirs, a, b, suite_id):
                inc = [load_suite_rows(d, a, suite_id) for d in incumbent_run_dirs]
                cand = [load_suite_rows(d, b, suite_id) for d in candidate_run_dirs]
                return inc, cand
        """
        assert len(self._check(source)) == 1

    def test_the_primitive_itself_is_exempt(self) -> None:
        # `load_arm_rows` composes `load_suite_rows`; reconciling here would run the sweep once
        # per composing gate rather than once per gate.
        source = """
            def load_arm_rows(run_dirs, variant_id, suite_id):
                return pool_replicates([load_suite_rows(d, variant_id, suite_id) for d in run_dirs])
        """
        assert self._check(source) == []

    def test_a_nested_reader_is_folded_into_its_enclosing_function(self) -> None:
        # `execution_gate`'s `_verdict` closure reads nothing, but the shape matters: a nested def
        # must not be judged apart from the body that reconciled for it.
        source = """
            def execution_gate(run_dir, variant, suite_id):
                reconcile_tree_against_run_json(run_dir, variant, suite_id)

                def _inner():
                    return load_arm_rows([run_dir], variant, suite_id)

                return _inner()
        """
        assert self._check(source) == []

    def test_it_is_scoped_to_the_optimize_family(self) -> None:
        source = """
            def somewhere_else(run_dirs, variant_id, suite_id):
                return [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]
        """
        assert self._check(source, "src/coder_eval/reports.py") == []
        assert self._check(source, "tests/test_optimize_load.py") == []

    def test_a_noqa_on_the_read_suppresses_it(self, tmp_path: Path) -> None:
        """The escape hatch, through the real runner rather than the rule in isolation.

        Suppression is `runner._is_suppressed`'s job, and it scans the lines the violation SPANS —
        so this also pins that the rule anchors at the READ rather than at the `def`, where a
        stray noqa anywhere in the function body would silence it.
        """
        # Under a `coder_eval/` segment, because the rule's scope is a PATH regex.
        package = tmp_path / "coder_eval" / "optimize"
        package.mkdir(parents=True)
        module = package / "fronts.py"
        module.write_text(
            "def a_reader(run_dirs, variant_id, suite_id):\n"
            "    # someone else reconciles for this one\n"
            "    return [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]  # noqa: CE053\n",
            encoding="utf-8",
        )
        assert check_paths([module], [RunTreeReadersReconcile]) == []
        # And without it, the same file is a violation — so the assertion above is not vacuous.
        module.write_text(
            "def a_reader(run_dirs, variant_id, suite_id):\n"
            "    return [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]\n",
            encoding="utf-8",
        )
        assert len(check_paths([module], [RunTreeReadersReconcile])) == 1

    @pytest.mark.parametrize(
        "module",
        [
            "optimize/gate",
            "optimize/load",
            "optimize/activation",
            "optimize/execution",
            "optimize/fronts",
            "optimize/search",
            # Not a real module — the DIRECTORY scope is what makes an eighth one covered too.
            # An enumerated scope fails OPEN here: zero violations reads exactly like a clean tree.
            "optimize/something_nobody_has_written_yet",
        ],
    )
    def test_every_module_of_the_optimize_family_is_in_scope(self, module: str) -> None:
        """Scope is the `optimize/` DIRECTORY, so a reader cannot move out of the rule's reach.

        The last of these does not exist. That is the point: a rule scoped to today's file names
        would go silent on exactly the change that redistributes these readers. The directory
        replaced an `optimize_*` filename prefix and the two are incomparable, not ordered — see the
        rule's own docstring.
        """
        source = """
            def a_reader(run_dirs, variant_id, suite_id):
                return [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]
        """
        assert len(self._check(source, f"src/coder_eval/{module}.py")) == 1

    def test_every_optimize_module_on_disk_is_in_scope(self) -> None:
        """Parity, so the scope cannot drift away from the family it is named for."""
        on_disk = sorted(p.name for p in (SRC / "coder_eval" / "optimize").glob("*.py") if p.name != "__init__.py")
        assert on_disk, "no modules under coder_eval/optimize/ — CE053 would be checking nothing"
        out_of_scope = [
            name for name in on_disk if not RunTreeReadersReconcile(f"src/coder_eval/optimize/{name}")._in_scope
        ]
        assert out_of_scope == [], out_of_scope

    @pytest.mark.parametrize(
        ("path", "in_scope"),
        [
            ("src/coder_eval/optimize/fronts.py", True),
            # A nested subpackage, and a module whose name carries a digit. Neither exists today;
            # both were fail-open holes in the first spelling of this regex (`[a-z_]+` directly
            # below the package), and a reader that leaves the rule's reach reports ZERO violations,
            # which is byte-identical to a clean tree.
            ("src/coder_eval/optimize/sub/deep.py", True),
            ("src/coder_eval/optimize/load2.py", True),
            # The OLD flat path: a regex matching both would pass every other test here while
            # proving nothing about the move.
            ("src/coder_eval/optimize_fronts.py", False),
            ("src/coder_eval/reports.py", False),
            ("tests/test_optimize_load.py", False),
        ],
    )
    def test_the_scope_is_the_package_directory_and_only_that(self, path: str, in_scope: bool) -> None:
        """Both directions asserted, because CE053's failure mode is silence rather than noise."""
        assert RunTreeReadersReconcile(path)._in_scope is in_scope

    def test_the_violation_anchors_at_the_earliest_read(self) -> None:
        """The anchor is where the `# noqa: CE053` must sit, so it has to be the FIRST read.

        `ast.walk` is breadth-first, so a read nested inside an `if` is visited after a shallower
        read below it — anchoring on walk order put the violation on the later line.
        """
        source = """
            def a_reader(run_dirs, variant_id, suite_id):
                if run_dirs:
                    first = load_suite_rows(run_dirs[0], variant_id, suite_id)
                second = load_arm_rows(run_dirs, variant_id, suite_id)
                return first, second
        """
        violations = self._check(source)
        assert len(violations) == 1
        # Line 4 of the dedented source (1 = `def`), the nested read — not the flat one on line 5.
        assert violations[0].line == 4  # type: ignore[attr-defined]

    def test_every_accepted_reconciler_really_reconciles(self) -> None:
        """The rule accepts a NAME, so each accepted name must earn it — checked, not assumed.

        `_RECONCILE` holds `reconcile_tree_against_run_json` (the primitive), `reconcile_arms` (the
        whole-arm sweep) and `_refuse_stale_tree` (the execution gate's reconciliation, extracted as
        a named stage). The last two are wrappers, so the rule is satisfied for every reader in the
        package by a call to them — and it would stay satisfied if either quietly stopped calling
        the primitive. That is the trade CE053's docstring records; this is the assertion that keeps
        the trade honest, and it is why adding a name to that set is cheap rather than dangerous.
        """
        primitive = "reconcile_tree_against_run_json"
        wrappers = sorted(_RECONCILE - {primitive})
        assert wrappers, "the set collapsed to the primitive — this test would then check nothing"

        found: dict[str, bool] = {}
        for path in sorted((SRC / "coder_eval" / "optimize").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in wrappers:
                    calls = {
                        call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                        for call in ast.walk(node)
                        if isinstance(call, ast.Call)
                    }
                    found[node.name] = primitive in calls
        assert sorted(found) == wrappers, f"a name CE053 accepts is not defined in the package: {found}"
        assert all(found.values()), f"an accepted reconciler never calls {primitive}: {found}"

    def test_the_live_suppression_set_is_exactly_the_three_documented_ones(self) -> None:
        """Anti-vacuity, and the acceptance criterion in test form.

        The rule is satisfied by suppressions as easily as by reconciles, so the live set is
        pinned by FUNCTION NAME rather than counted. A zero here would mean the path regex
        stopped matching, which is byte-identical to a clean tree.
        """
        suppressed: list[str] = []
        for path in sorted((SRC / "coder_eval" / "optimize").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reported = {v.line for v in RunTreeReadersReconcile(str(path)).check(tree)}
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and reported & set(
                    range(node.lineno, (node.end_lineno or node.lineno) + 1)
                ):
                    suppressed.append(node.name)
        # `resolve_arm_model` is the third, and the only one whose read is not reconciled by a
        # caller: it resolves a MODEL ID, which a stale row cannot change, and every measurement
        # built on that id reconciles the arms it actually reads. One declaration of the idiom is
        # what keeps this at three rather than one suppression per call site.
        assert sorted(suppressed) == ["cost_quality_points", "load_and_pair", "resolve_arm_model"], suppressed
        # And every one of them is genuinely suppressed rather than merely absent from `make lint`.
        assert check_paths([SRC], [RunTreeReadersReconcile]) == []


@pytest.mark.lint
class TestCE054ResultStatusSingleSeam:
    """CE054 — a criterion module compares `result_status` in exactly ONE place.

    Written after `criteria/skill_triggered.py` spelled the delivered-body predicate twice —
    an allowlist on the `Skill` branch, a denylist on the file-read branch — and the two
    drifted, so a crash-force-closed `Read(SKILL.md)` scored as engagement while the identical
    crash on a `Skill` call did not. That is a false positive on `f1.yes`, which
    `optimize.activation.activation_gate` promotes on.

    The FIRST test below is the one that matters: both halves of the real bug lived inside a
    single function, so the per-function spelling of this rule — which is what it was first
    written as — reported zero on the exact file it exists for. Counting comparison SITES is
    the invariant; counting enclosing functions is a rule that passes the bug.
    """

    CRITERION = "src/coder_eval/criteria/skill_triggered.py"

    def _check(self, source: str, path: str | None = None) -> list[object]:
        return list(ResultStatusSingleSeam(path or self.CRITERION).check(ast.parse(textwrap.dedent(source))))

    def test_it_fires_on_the_pre_fix_shape_with_both_sites_in_one_function(self) -> None:
        # Reduced from `_engaged_skill_names` as it stood before the fix. Two predicates, one
        # function, opposite polarities. A per-function seam check returns 0 here.
        source = """
            def _engaged_skill_names(cmd):
                if cmd.tool_name == "Skill":
                    if cmd.result_status != "success":
                        return set()
                    return {cmd.parameters["skill"]}
                if not (cmd.tool_name in _FILE_READ_TOOLS and cmd.result_status in ("error", None)):
                    return scan(cmd)
                return set()
        """
        violations = self._check(source)
        assert len(violations) == 1, violations
        assert violations[0].rule_id == "CE054"  # type: ignore[attr-defined]

    def test_it_is_silent_on_the_single_seam(self) -> None:
        source = """
            def _delivered(cmd):
                if cmd.tool_name == "Skill" or cmd.tool_name in _FILE_READ_TOOLS:
                    return cmd.result_status == "success"
                return True
        """
        assert self._check(source) == []

    def test_it_fires_across_functions_too(self) -> None:
        # The per-function shape IS still caught — counting sites subsumes it.
        source = """
            def _delivered(cmd):
                return cmd.result_status == "success"

            def _other(cmd):
                return cmd.result_status != "error"
        """
        assert len(self._check(source)) == 1

    def test_it_is_scoped_to_the_criteria_package(self) -> None:
        # `analysis.py` buckets counts for a report and `reports_html` picks a CSS class;
        # neither decides a score, and both legitimately read the status more than once.
        source = """
            def a(c):
                return c.result_status == "success"

            def b(c):
                return c.result_status == "error"
        """
        assert self._check(source, "src/coder_eval/analysis.py") == []
        assert self._check(source, "src\\\\coder_eval\\\\analysis.py") == []

    def test_a_non_comparison_read_is_not_matched(self) -> None:
        # Stated on the rule as a boundary, asserted here so it is not mistaken for coverage.
        source = """
            def a(c):
                return f"{c.result_status}" + describe(c.result_status)
        """
        assert self._check(source) == []

    def test_the_tree_has_exactly_one_seam_per_criterion_module(self) -> None:
        """Anti-vacuity, and the acceptance criterion in test form.

        The rule is satisfied by suppressions as easily as by conversions, so the live count
        is pinned rather than left to the sweep. Two criterion modules classify
        `result_status` today — `skill_triggered` (in `_delivered`) and `command_executed` —
        and each does it once. A zero here would mean the scan stopped finding anything.
        """
        criteria_dir = SRC / "coder_eval" / "criteria"
        assert criteria_dir.is_dir(), "criteria/ moved — CE054 would be checking nothing"
        with_seam = sorted(
            path.relative_to(SRC).as_posix()
            for path in criteria_dir.rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Compare)
            and any(isinstance(n, ast.Attribute) and n.attr == "result_status" for n in (node.left, *node.comparators))
        )
        assert with_seam == [
            "coder_eval/criteria/command_executed.py",
            "coder_eval/criteria/skill_triggered.py",
        ], with_seam


@pytest.mark.lint
class TestCE059NoSiblingPrivateImports:
    """CE059 — inside `coder_eval/optimize/`, a name two modules share may not be private.

    One underscore cannot mark two boundaries. Before the package landed, 29 names carried an
    underscore while four modules imported them — the middle tier spelled like the innermost, which
    tells a reader "safe to change this signature" about a helper a change here breaks in three other
    files. The decay is silent: the next author adding a cross-module helper has even odds of
    prefixing it out of habit and nothing fails.
    """

    GATE = str(SRC / "coder_eval" / "optimize" / "gate.py")

    def _check(self, source: str, path: str | None = None) -> list[object]:
        return list(NoSiblingPrivateImports(path or self.GATE).check(ast.parse(textwrap.dedent(source))))

    def test_it_fires_on_a_private_sibling_import(self) -> None:
        violations = self._check("from coder_eval.optimize.load import pool_replicates, _PairedRows")
        assert len(violations) == 1
        assert "_PairedRows" in violations[0].message  # type: ignore[attr-defined]

    def test_the_relative_spelling_fires_too(self) -> None:
        """The CE051 parity shape, and the test that proves the resolver is actually wired.

        Matching `node.module` alone reads `from .load import _pool` as the bare module `"load"`,
        which does not start with `coder_eval.optimize.` — so the rule reports nothing, which is
        byte-identical to a clean tree. That is the fail-OPEN direction CE051 exists for.
        """
        assert len(self._check("from .load import _pool")) == 1
        assert len(self._check("from .load import pool_replicates")) == 0

    def test_a_public_sibling_name_does_not_fire(self) -> None:
        assert self._check("from coder_eval.optimize.load import load_arm_rows, row_score") == []

    def test_a_private_name_from_outside_the_package_does_not_fire(self) -> None:
        # Not this rule's business: the tier argument is about names shared INSIDE the package.
        assert self._check("from coder_eval.reports_stats import _betacf") == []
        assert self._check("from coder_eval.models import _anything") == []

    def test_a_file_outside_the_package_does_not_fire(self) -> None:
        """`reports_optimize.py` is outside by construction, so a widening is a deliberate act."""
        outside = str(SRC / "coder_eval" / "reports_optimize.py")
        assert self._check("from coder_eval.optimize.load import _PairedRows", outside) == []

    def test_it_reports_no_violations_on_the_real_tree(self) -> None:
        """The acceptance test for the 29 renames being COMPLETE, not merely started.

        Guarded by the non-vacuity assert first, on CE053's precedent: `_PACKAGE_PATH` is a hardcoded
        string, so renaming or moving the package leaves this test green while the rule inspects ZERO
        files — indistinguishable from a clean tree, which is this family's standing failure mode.
        """
        in_scope = [
            p for p in (SRC / "coder_eval" / "optimize").glob("*.py") if NoSiblingPrivateImports(str(p))._in_package
        ]
        assert len(in_scope) >= 7, (
            f"CE059 is in scope for only {sorted(p.name for p in in_scope)} — its `_PACKAGE_PATH` no "
            "longer matches the package, so it would be checking nothing"
        )
        assert check_paths([SRC], [NoSiblingPrivateImports]) == []

    def test_the_rule_is_wired_into_all_rules(self) -> None:
        assert NoSiblingPrivateImports in ALL_RULES


@pytest.mark.lint
class TestHolmRejectionsIsConfined:
    """`holm_rejections` may be called only from `optimize.gate`, and only by `holm_family`.

    Holm is a property of a FAMILY. A call site that sees one candidate at a time degenerates to
    an uncorrected `p <= alpha` while still looking like a correction — the failure mode the two
    wrappers exist to prevent, and one no type or test of theirs can catch from the outside.

    CLAUDE.md said `holm_promote` was the ONLY call site until the execution track got its own
    gate, and then "both call sites live in optimize.gate". There is now exactly ONE: the two
    wrappers spelled the same three-line preamble 700 lines apart, and `holm_family` is the single
    declaration they both call. **That is strictly stricter to audit than two were** — the
    membership rule (`p_value is not None`) and the original-index mapping are stated once, so the
    two tracks cannot drift on either, and this assertion now fails on a SECOND call site rather
    than only on a third.

    CALL NODES are counted, not files. A file-set assertion was already true before this existed
    and would have proved nothing; counting calls is what catches another one appearing INSIDE
    `optimize.gate`, which is exactly where it would be written.
    """

    SRC = REPO_ROOT / "src" / "coder_eval"
    ALLOWED: ClassVar[set[str]] = {"holm_family"}

    @staticmethod
    def _call_sites(tree: ast.AST) -> list[tuple[str, int]]:
        """(enclosing function name, line) for every `holm_rejections(...)` call in ``tree``."""
        # INNERMOST wins. `ast.walk` is breadth-first, so a `setdefault` keyed on the outer
        # function claims every descendant first — and a third call written inside a closure (the
        # module already has one, `_verdict` inside `execution_gate`) would then report its
        # enclosing function's allowed name and pass. Overwriting as we descend is what fixes it.
        enclosing: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    enclosing[id(child)] = node.name
        sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "holm_rejections":
                sites.append((enclosing.get(id(node), "<module>"), node.lineno))
        return sites

    def test_exactly_one_call_site_inside_optimize_gate(self):
        found: dict[str, list[tuple[str, int]]] = {}
        for module in sorted(self.SRC.rglob("*.py")):
            sites = self._call_sites(ast.parse(module.read_text(encoding="utf-8")))
            if sites:
                # Keyed on the path relative to `src/coder_eval`, not the bare filename: since the
                # family became a package, `gate.py` alone no longer says WHICH gate.
                found[module.relative_to(self.SRC).as_posix()] = sites

        assert set(found) == {"optimize/gate.py"}, (
            f"holm_rejections is called from {sorted(found)}. Holm corrects a FAMILY, so every call "
            "site has to see the whole family at once — which is why both wrappers route through "
            "one helper in one module. A caller elsewhere is almost certainly correcting one "
            "candidate at a time."
        )
        callers = sorted(name for name, _line in found["optimize/gate.py"])
        assert callers == sorted(self.ALLOWED), (
            f"holm_rejections is called by {callers}, not {sorted(self.ALLOWED)}. A SECOND call — even "
            "inside optimize.gate — is a family being decided somewhere that cannot see all of it, "
            "and a second declaration of the membership rule the two tracks must agree on."
        )

    def test_a_third_call_inside_the_module_is_caught(self):
        # The self-test. A file-set assertion passes this; counting call nodes is what does not.
        source = textwrap.dedent(
            """
            def holm_promote(verdicts, alpha):
                return holm_rejections([v.p for v in verdicts], alpha)

            def holm_promote_execution(verdicts, alpha):
                return holm_rejections([v.p for v in verdicts], alpha)

            def _quietly_uncorrected(verdict, alpha):
                return holm_rejections([verdict.p], alpha)
            """
        )
        callers = sorted(name for name, _line in self._call_sites(ast.parse(source)))
        assert callers != sorted(self.ALLOWED)
        assert "_quietly_uncorrected" in callers

    def test_a_call_hidden_in_a_closure_is_attributed_to_the_closure(self):
        # The natural place a third call would be written: inside a helper nested in an ALLOWED
        # function, where a breadth-first `setdefault` would report the allowed outer name.
        source = textwrap.dedent(
            """
            def holm_promote(verdicts, alpha):
                def _per_candidate(verdict):
                    return holm_rejections([verdict.p], alpha)
                return [_per_candidate(v) for v in verdicts]
            """
        )
        callers = sorted(name for name, _line in self._call_sites(ast.parse(source)))
        assert callers == ["_per_candidate"], callers
        assert callers != sorted(self.ALLOWED)
