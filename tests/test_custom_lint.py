"""Custom architectural lint rules for coder_eval.

Each test enforces one rule across the entire src/ tree.  A violation means
the code breaks a project-specific architectural constraint that Ruff cannot
express.  Fix the violation or add `# noqa: CE<id>` to suppress it locally
with a comment explaining why.

Run just these tests:
    uv run pytest tests/test_custom_lint.py -v
    make lint
"""

import ast
import re
import textwrap
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from tests.lint.cli_flags import help_for, long_flags
from tests.lint.import_resolution import resolved_module
from tests.lint.rules.ce017_models_lazy_agent_imports import ModelsLazyAgentImports
from tests.lint.rules.ce023_no_proxy_shim_import import NoProxyShimImports
from tests.lint.rules.ce041_no_model_dict_splat import NoModelDictSplat
from tests.lint.rules.ce047_no_bare_assert_in_cli import NoBareAssertInCli
from tests.lint.rules.ce048_no_bare_model_copy_update import NoBareModelCopyUpdate
from tests.lint.rules.ce050_escape_untrusted_markup import EscapeUntrustedMarkup
from tests.lint.rules.ce051_importfrom_rules_handle_level import ImportFromRulesHandleLevel
from tests.lint.rules.ce053_run_tree_readers_reconcile import RunTreeReadersReconcile
from tests.lint.rules.ce054_result_status_single_seam import ResultStatusSingleSeam
from tests.lint.rules.ce059_no_sibling_private_imports import NoSiblingPrivateImports
from tests.lint.rules.no_cli_imports_in_core import NoCliImportsInCore
from tests.lint.rules.no_submodule_model_imports import NoSubmoduleModelImports
from tests.lint.runner import ALL_RULES, check_paths
from tests.lint.task_yaml_discovery import all_yaml, task_yamls


SRC = Path(__file__).parent.parent / "src"


@pytest.mark.lint
@pytest.mark.parametrize("rule_class", ALL_RULES, ids=[r.id for r in ALL_RULES])
def test_no_violations(rule_class: type) -> None:
    import sys

    mod_doc = (getattr(sys.modules.get(rule_class.__module__), "__doc__", "") or "").splitlines()[0].strip()
    violations = check_paths([SRC], rules=[rule_class])
    assert not violations, (
        f"\n{len(violations)} violation(s) for {rule_class.id} ({mod_doc}):\n\n"
        + "\n".join(f"  {v}" for v in violations)
        + f"\n\nFix the violation or add `# noqa: {rule_class.id}` to the offending line with a comment explaining why."
    )


@pytest.mark.lint
class TestCE016NoComputedTokenUsageKwargs:
    """CE016 fires on TokenUsage(input_tokens=/total_tokens=) but not elsewhere."""

    @staticmethod
    def _run(src: str):
        import ast

        from tests.lint.rules.ce016_no_computed_tokenusage_kwargs import NoComputedTokenUsageKwargs

        return NoComputedTokenUsageKwargs("<test>").check(ast.parse(src))

    @pytest.mark.parametrize("kw", ["input_tokens", "total_tokens"])
    def test_flags_computed_kwarg_on_tokenusage(self, kw: str):
        assert self._run(f"TokenUsage({kw}=10, output_tokens=5)")

    def test_allows_uncached_input_tokens(self):
        assert not self._run("TokenUsage(uncached_input_tokens=10, output_tokens=5)")

    def test_ignores_input_tokens_on_other_models(self):
        # AssistantMessage / ReconciliationMessage DO have a settable input_tokens.
        assert not self._run("AssistantMessage(input_tokens=10)")
        assert not self._run("ReconciliationMessage(input_tokens=-5)")


@pytest.mark.lint
class TestCE043NoCommandOutputTruncation:
    """CE043 flags truncation of captured command output inside agents/, only."""

    @staticmethod
    def _run(src: str, *, in_agents: bool = True):
        import ast

        from tests.lint.rules.ce043_no_command_output_truncation import NoCommandOutputTruncation

        path = "src/coder_eval/agents/codex_agent.py" if in_agents else "src/coder_eval/reports_html.py"
        return NoCommandOutputTruncation(path).check(ast.parse(src))

    @pytest.mark.parametrize(
        "expr",
        [
            "output[:100]",
            "aggregated_output[:512]",
            "command_item.aggregated_output[:100]",
            "proc_stdout[:80]",
            "result.stderr[:200]",
            'f"Output: {output[:100]}"',
        ],
    )
    def test_flags_output_truncation_in_agents(self, expr: str):
        assert self._run(f"x = {expr}"), f"expected CE043 to flag {expr!r}"

    def test_allows_untruncated_output(self):
        assert not self._run('summary = f"Output: {output}"')
        assert not self._run("summary = aggregated_output")

    def test_ignores_non_output_slices(self):
        # Legit error/orchestration summaries that are NOT captured command output.
        assert not self._run("detail = summary.result[:200]")
        assert not self._run("msg = content_str[:200]")
        assert not self._run("s = '; '.join(messages)[:200]")

    def test_scoped_to_agents_only(self):
        # The same pattern outside agents/ is not this rule's concern (display code).
        assert not self._run("preview = output[:100]", in_agents=False)


@pytest.mark.lint
class TestCE017ModelsLazyAgentImports:
    """CE017 flags only module-level agents/plugins imports inside models/."""

    @staticmethod
    def _run(src: str, *, in_models: bool = True):
        import ast

        from tests.lint.rules.ce017_models_lazy_agent_imports import ModelsLazyAgentImports

        path = "src/coder_eval/models/agent_config.py" if in_models else "src/coder_eval/orchestration/x.py"
        return ModelsLazyAgentImports(path).check(ast.parse(src))

    def test_flags_module_level_agents_import_in_models(self):
        assert self._run("from coder_eval.agents.registry import AgentRegistry")
        assert self._run("import coder_eval.plugins")

    def test_allows_function_local_import_in_models(self):
        src = "def f():\n    from coder_eval.agents.registry import AgentRegistry\n    return AgentRegistry"
        assert not self._run(src)

    def test_allows_type_checking_guarded_import_in_models(self):
        src = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import coder_eval.plugins"
        assert not self._run(src)

    def test_ignores_files_outside_models(self):
        assert not self._run("from coder_eval.agents.registry import AgentRegistry", in_models=False)


@pytest.mark.lint
class TestCE018NoFinalStatusNameDenylist:
    """CE018 fires on string comparisons against FinalStatus member names."""

    @staticmethod
    def _run(src: str):
        import ast

        from tests.lint.rules.ce018_no_final_status_name_denylist import NoFinalStatusNameDenylist

        return NoFinalStatusNameDenylist("<test>").check(ast.parse(src))

    @pytest.mark.parametrize(
        "src",
        [
            's == "SUCCESS"',
            's != "ERROR"',
            '"ERROR" == s',  # reversed
            's in ("FAILURE", "TIMEOUT")',
            's not in ("TOKEN_BUDGET_EXCEEDED", "COST_BUDGET_EXCEEDED")',
            's in ("succeeded", "MAX_TURNS_EXHAUSTED")',  # tuple mixes a member name in
            's in ["SUCCESS", "FAILURE"]',  # list literal
            's in {"ERROR"}',  # set literal
        ],
    )
    def test_flags_member_name_comparisons(self, src: str):
        assert self._run(src), f"expected CE018 to fire on: {src}"

    @pytest.mark.parametrize(
        "src",
        [
            'x == "succeeded"',  # a category VALUE, not a member name
            'x == "failed"',
            'x == "hello"',  # unrelated string
            'x in ("succeeded", "failed")',  # category values, not member names
            'x in ["succeeded", "failed"]',  # list of category values
            "isinstance(x, FinalStatus)",  # not a Compare against a literal
        ],
    )
    def test_ignores_non_member_comparisons(self, src: str):
        assert not self._run(src), f"expected CE018 NOT to fire on: {src}"

    def test_denylist_stays_in_sync_with_enum(self):
        """The rule's hardcoded name set must track FinalStatus (AST rules can't import it)."""
        from coder_eval.models import FinalStatus
        from tests.lint.rules.ce018_no_final_status_name_denylist import _FINAL_STATUS_NAMES

        assert {s.value for s in FinalStatus} == _FINAL_STATUS_NAMES


@pytest.mark.lint
class TestCE019TelemetryNonFatal:
    """CE019 flags unguarded telemetry public functions; allows guarded ones."""

    @staticmethod
    def _run(src: str, *, in_telemetry: bool = True):
        import ast

        from tests.lint.rules.ce019_telemetry_non_fatal import TelemetryNonFatal

        path = "src/coder_eval/telemetry.py" if in_telemetry else "src/coder_eval/orchestrator.py"
        return TelemetryNonFatal(path).check(ast.parse(src))

    def test_flags_unguarded_public_function(self):
        src = "def track_event(name):\n    do_work()\n    more_work()"
        assert self._run(src)

    def test_allows_guarded_function(self):
        src = "def track_event(name):\n    try:\n        do_work()\n    except Exception:\n        pass"
        assert not self._run(src)

    def test_allows_bare_except(self):
        src = "def flush_telemetry():\n    try:\n        do_work()\n    except:\n        pass"
        assert not self._run(src)

    def test_flags_narrow_handler(self):
        # A narrow `except ValueError` is not broad → must be flagged.
        src = "def track_event(name):\n    try:\n        do_work()\n    except ValueError:\n        pass"
        assert self._run(src)

    def test_flags_try_without_except(self):
        # try/finally with no except handler → must be flagged.
        src = "def track_event(name):\n    try:\n        do_work()\n    finally:\n        cleanup()"
        assert self._run(src)

    def test_flags_unguarded_async_function(self):
        # The invariant must hold for async defs too (rule must not be defeatable
        # by converting a function to `async def`).
        src = "async def track_event(name):\n    do_work()"
        assert self._run(src)

    def test_allows_leading_docstring_global_and_guards(self):
        src = (
            "def init_telemetry(v):\n"
            '    """doc."""\n'
            "    global x\n"
            "    if x is None:\n"
            "        return\n"
            "    try:\n"
            "        do_work()\n"
            "    except Exception:\n"
            "        pass"
        )
        assert not self._run(src)

    def test_ignores_private_helpers_and_decorator(self):
        # _coerce_props / track_command are out of scope (not in the allowlist).
        assert not self._run("def _coerce_props(p):\n    return p")
        assert not self._run("def track_command(name):\n    return name")

    def test_ignores_non_telemetry_files(self):
        src = "def track_event(name):\n    do_work()"
        assert not self._run(src, in_telemetry=False)


@pytest.mark.lint
class TestCE020NoSdkTypedBaseAgentFields:
    """CE020 flags claude_agent_sdk-typed fields on BaseAgentConfig; allows them elsewhere."""

    @staticmethod
    def _run(src: str, *, in_target: bool = True):
        import ast

        from tests.lint.rules.ce020_no_sdk_typed_base_agent_fields import NoSdkTypedBaseAgentFields

        path = "src/coder_eval/models/agent_config.py" if in_target else "src/coder_eval/models/other.py"
        return NoSdkTypedBaseAgentFields(path).check(ast.parse(src))

    def test_flags_sdk_typed_field_on_base(self):
        src = (
            "from claude_agent_sdk import SettingSource\n"
            "class BaseAgentConfig:\n"
            "    setting_sources: list[SettingSource] | None = None\n"
        )
        assert self._run(src)

    def test_flags_aliased_sdk_typed_field_on_base(self):
        src = (
            "from claude_agent_sdk import SdkPluginConfig as P\n"
            "class BaseAgentConfig:\n"
            "    plugins: list[P] | None = None\n"
        )
        assert self._run(src)

    def test_allows_local_typed_field_on_base(self):
        src = (
            "from claude_agent_sdk import ClaudeAgentOptions\n"
            "_FIELDS = ClaudeAgentOptions\n"  # module-level use — not flagged
            "class BaseAgentConfig:\n"
            "    plugins: list[LocalPluginConfig] | None = None\n"
        )
        assert not self._run(src)

    def test_flags_submodule_import_field_on_base(self):
        src = (
            "from claude_agent_sdk.types import SettingSource\n"
            "class BaseAgentConfig:\n"
            "    setting_sources: list[SettingSource] | None = None\n"
        )
        assert self._run(src)

    def test_flags_module_attribute_field_on_base(self):
        src = (
            "import claude_agent_sdk as sdk\n"
            "class BaseAgentConfig:\n"
            "    setting_sources: list[sdk.SettingSource] | None = None\n"
        )
        assert self._run(src)

    def test_ignores_unrelated_lookalike_module(self):
        src = (
            "from claude_agent_sdkx import SettingSource\n"  # not the SDK
            "class BaseAgentConfig:\n"
            "    setting_sources: list[SettingSource] | None = None\n"
        )
        assert not self._run(src)

    def test_allows_sdk_typed_field_on_subclass(self):
        src = (
            "from claude_agent_sdk import SettingSource\n"
            "class ClaudeCodeAgentConfig:\n"
            "    setting_sources: list[SettingSource] | None = None\n"
        )
        assert not self._run(src)

    def test_ignores_files_outside_target(self):
        src = (
            "from claude_agent_sdk import SettingSource\n"
            "class BaseAgentConfig:\n"
            "    setting_sources: list[SettingSource] | None = None\n"
        )
        assert not self._run(src, in_target=False)

    def test_no_violations_on_real_agent_config(self):
        """Phase 1 cleanup satisfies CE020: the real module has zero SDK-typed base fields."""
        from tests.lint.rules.ce020_no_sdk_typed_base_agent_fields import NoSdkTypedBaseAgentFields

        agent_config = SRC / "coder_eval" / "models" / "agent_config.py"
        assert not check_paths([agent_config], rules=[NoSdkTypedBaseAgentFields])


@pytest.mark.lint
def test_rule_ids_unique() -> None:
    """No two lint rules may share a CE id (anti-shadow: a noqa keys on the id).

    Mirrors the AgentRegistry / register_pricing anti-shadow invariant. pytest
    silently auto-suffixes duplicate parametrize ids, so without this a duplicate
    CE number (e.g. two in-flight branches claiming the next number) would not
    fail the suite on its own.
    """
    ids = [r.id for r in ALL_RULES]
    assert len(set(ids)) == len(ids), f"duplicate CE rule id(s): {sorted({i for i in ids if ids.count(i) > 1})}"


@pytest.mark.lint
def test_rule_ids_are_unique_across_baserules_and_test_classes() -> None:
    """The uniqueness invariant, extended to the HALF `ALL_RULES` cannot see.

    Roughly a third of the CE rules are not `BaseRule`s at all — CE026-CE031, CE033-CE036,
    CE038-CE039 and CE043-CE046 are `@pytest.mark.lint` classes, because their subject is Markdown,
    YAML, a resolved Typer signature or the whole `src/` tree rather than one `.py` AST. Those ids
    live only in a class NAME, so `runner.py`'s import-time assert (and the test above) cannot see
    them, and a class-wired id could silently collide with a `BaseRule`'s. A `# noqa` keys on the
    id string, so a collision means one suppression quietly disarms two rules.

    The subject is the `TestCE<NNN>` class names in this file, where EVERY rule surfaces — a
    `BaseRule` has one testing it and a class-wired rule IS one. So two rules claiming one number
    show up as two classes claiming it, whichever kind either is. (A single id appearing both in
    `ALL_RULES` and on a test class is the normal shape, not a collision.)

    Boundary: a `BaseRule` with no `TestCE<NNN>` class of its own contributes nothing here and
    could still collide unseen — which is itself a reason to keep the convention.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    class_ids = [
        m.group(1)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and (m := re.match(r"^Test(CE\d+)", node.name))
    ]

    assert len(class_ids) > 20, f"only {len(class_ids)} TestCE classes found — has the convention moved?"
    duplicates = sorted({i for i in class_ids if class_ids.count(i) > 1})
    assert not duplicates, (
        f"{duplicates} are claimed by more than one rule. A `# noqa` keys on the id, so one "
        "suppression would disarm both — renumber the newer one. Note `runner.py`'s import-time "
        "assert cannot see this: it covers ALL_RULES, and roughly a third of the CE rules are "
        "`@pytest.mark.lint` classes rather than BaseRules."
    )


@pytest.mark.lint
class TestCE021GuardedEvaluationResultParse:
    """CE021 flags unguarded EvaluationResult.model_validate_json; allows guarded ones."""

    @staticmethod
    def _run(src: str):
        import ast

        from tests.lint.rules.ce021_guarded_evaluationresult_parse import GuardedEvaluationResultParse

        return GuardedEvaluationResultParse("<test>").check(ast.parse(src))

    def test_flags_unguarded_call(self):
        assert self._run("result = EvaluationResult.model_validate_json(text)")

    def test_allows_guarded_by_value_error(self):
        src = "try:\n    r = EvaluationResult.model_validate_json(text)\nexcept ValueError:\n    pass"
        assert not self._run(src)

    def test_allows_guarded_by_exception(self):
        src = "try:\n    r = EvaluationResult.model_validate_json(text)\nexcept Exception:\n    pass"
        assert not self._run(src)

    def test_allows_guarded_by_bare_except(self):
        src = "try:\n    r = EvaluationResult.model_validate_json(text)\nexcept:\n    pass"
        assert not self._run(src)

    def test_allows_guarded_by_tuple_with_value_error(self):
        # recover_task_results uses `except (OSError, ValueError)` — the tuple member guards.
        src = "try:\n    r = EvaluationResult.model_validate_json(text)\nexcept (OSError, ValueError):\n    pass"
        assert not self._run(src)

    def test_flags_unrelated_handler_only(self):
        # `except OSError` does NOT catch ValidationError/JSONDecodeError → still flagged.
        src = "try:\n    r = EvaluationResult.model_validate_json(text)\nexcept OSError:\n    pass"
        assert self._run(src)

    def test_flags_call_in_finally_body(self):
        # A parse in the finally is not protected by that try's handlers → flagged.
        src = (
            "try:\n    do_work()\nexcept ValueError:\n    pass\n"
            "finally:\n    r = EvaluationResult.model_validate_json(text)"
        )
        assert self._run(src)

    def test_flags_call_in_except_body(self):
        # A parse in the except handler is not protected by that try → flagged.
        src = "try:\n    do_work()\nexcept ValueError:\n    r = EvaluationResult.model_validate_json(text)"
        assert self._run(src)

    def test_flags_call_in_else_body(self):
        # The else clause runs only after the body succeeds; an exception there
        # propagates UNCAUGHT (not protected by this try's except) → flagged.
        src = (
            "try:\n    do_work()\nexcept ValueError:\n    pass\n"
            "else:\n    r = EvaluationResult.model_validate_json(text)"
        )
        assert self._run(src)

    def test_ignores_other_models(self):
        # Narrow to EvaluationResult — other models' parses are out of scope.
        assert not self._run("r = TaskDefinition.model_validate_json(text)")

    def test_nested_try_propagates_guard(self):
        src = (
            "try:\n"
            "    try:\n"
            "        r = EvaluationResult.model_validate_json(text)\n"
            "    except OSError:\n"
            "        pass\n"
            "except ValueError:\n"
            "    pass"
        )
        # Inner body is guarded by the OUTER try's except ValueError.
        assert not self._run(src)


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
class TestCE022SimulationDialogLoopStatementCap:
    """CE022 bounds the regrowth of the noqa'd _simulation_dialog_loop.

    The whole-tree zero-violations guarantee (the live function is at/under the cap)
    is asserted by the parametrized ``test_no_violations`` above; these pin that the
    rule actually fires on regrowth and stays narrow.
    """

    @staticmethod
    def _run(src: str, *, path: str = "src/coder_eval/orchestrator.py"):
        import ast

        from tests.lint.rules.ce022_dialog_loop_statement_cap import SimulationDialogLoopStatementCap

        return SimulationDialogLoopStatementCap(path).check(ast.parse(src))

    @staticmethod
    def _padded_loop(name: str, n_statements: int) -> str:
        body = "\n".join(f"    x{i} = {i}" for i in range(n_statements))
        return f"async def {name}(self, initial_prompt, sandbox_dir):\n{body}"

    def test_flags_oversized_dialog_loop(self):
        """A _simulation_dialog_loop padded well past the cap fires exactly once."""
        violations = self._run(self._padded_loop("_simulation_dialog_loop", 200))
        assert len(violations) == 1
        assert violations[0].rule_id == "CE022"

    def test_allows_small_dialog_loop(self):
        """A short _simulation_dialog_loop body is under the cap → no violation."""
        assert not self._run("async def _simulation_dialog_loop(self, initial_prompt, sandbox_dir):\n    return True")

    def test_ignores_other_function_names(self):
        """The rule is narrow: a different oversized function is ruff's job, not CE022's."""
        assert not self._run(self._padded_loop("some_other_async_method", 200))

    def test_ignores_dialog_loop_outside_orchestrator(self):
        """File-scoped to orchestrator.py — a same-named function elsewhere is ignored."""
        assert not self._run(self._padded_loop("_simulation_dialog_loop", 200), path="src/coder_eval/other.py")

    def test_basename_match_ignores_sibling_suffix_file(self):
        """Exact-basename scoping: a sibling like x_orchestrator.py must NOT match."""
        assert not self._run(self._padded_loop("_simulation_dialog_loop", 200), path="src/coder_eval/x_orchestrator.py")

    def test_flags_oversized_sync_dialog_loop(self):
        """A future async→sync conversion must not silently drop the guard."""
        body = "\n".join(f"    x{i} = {i}" for i in range(200))
        src = f"def _simulation_dialog_loop(self, initial_prompt, sandbox_dir):\n{body}"
        assert len(self._run(src)) == 1


@pytest.mark.lint
class TestCE023NoProxyShimImports:
    """CE023 flags imports of the deprecated coder_eval.proxy.* shim outside it."""

    @staticmethod
    def _run(src: str, *, path: str = "src/coder_eval/agents/antigravity_agent.py"):
        import ast

        from tests.lint.rules.ce023_no_proxy_shim_import import NoProxyShimImports

        return NoProxyShimImports(path).check(ast.parse(src))

    def test_flags_from_proxy_pricing_import(self):
        assert self._run("from coder_eval.proxy.pricing import calculate_cost")

    def test_flags_bare_proxy_import(self):
        assert self._run("import coder_eval.proxy.pricing")

    def test_allows_canonical_pricing_import(self):
        assert not self._run("from coder_eval.pricing import calculate_cost")

    def test_does_not_match_lookalike_module(self):
        # `coder_eval.proxything` is a different package, not the proxy shim.
        assert not self._run("from coder_eval.proxything import x")

    def test_skips_shim_package_itself(self):
        src = "from coder_eval.proxy.pricing import calculate_cost"
        assert not self._run(src, path="src/coder_eval/proxy/__init__.py")


@pytest.mark.lint
class TestCE024DiscriminatedUnions:
    """CE024 flags bare module-level unions of same-file `type: Literal`-tagged models."""

    _TAGGED_CLASSES = (
        "from typing import Annotated, Literal, Union\n"
        "from pydantic import BaseModel, Discriminator, Field\n"
        "class A(BaseModel):\n"
        '    type: Literal["a"] = "a"\n'
        "class B(BaseModel):\n"
        '    type: Literal["b"] = "b"\n'
        "class U(BaseModel):\n"
        "    name: str\n"
        "class V(BaseModel):\n"
        "    name: str\n"
    )

    @staticmethod
    def _run(src: str, *, path: str = "src/coder_eval/models/fake.py"):
        import ast

        from tests.lint.rules.ce024_discriminated_unions import DiscriminatedUnions

        return DiscriminatedUnions(path).check(ast.parse(src))

    def test_flags_bare_union_of_tagged_classes(self):
        assert self._run(self._TAGGED_CLASSES + "X = A | B")

    def test_flags_parenthesized_multiline_union(self):
        assert self._run(self._TAGGED_CLASSES + "X = (\n    A\n    | B\n)")

    def test_flags_union_subscript_form(self):
        assert self._run(self._TAGGED_CLASSES + "X = Union[A, B]")

    def test_flags_annotated_union_without_discriminator(self):
        assert self._run(self._TAGGED_CLASSES + 'X = Annotated[A | B, Field(description="d")]')

    def test_allows_annotated_field_discriminator(self):
        assert not self._run(self._TAGGED_CLASSES + 'X = Annotated[A | B, Field(discriminator="type")]')

    def test_allows_annotated_discriminator_callable(self):
        assert not self._run(self._TAGGED_CLASSES + "X = Annotated[A | B, Discriminator(lambda v: 'a')]")

    def test_allows_union_of_untagged_classes(self):
        assert not self._run(self._TAGGED_CLASSES + "X = U | V")

    def test_allows_union_with_untagged_member(self):
        assert not self._run(self._TAGGED_CLASSES + "X = A | B | U")

    def test_allows_optional_scalar_union(self):
        assert not self._run(self._TAGGED_CLASSES + "X = A | None")

    def test_allows_imported_member_union(self):
        # C is not defined in this file — same-file conservatism.
        assert not self._run(self._TAGGED_CLASSES + "X = A | C")

    def test_ignores_files_outside_models(self):
        assert not self._run(self._TAGGED_CLASSES + "X = A | B", path="src/coder_eval/orchestration/fake.py")

    def test_flags_pep695_type_alias(self):
        # `type X = A | B` is the prevailing alias style in models/ (e.g. agent_config.py).
        assert self._run(self._TAGGED_CLASSES + "type X = A | B")

    def test_allows_pep695_type_alias_with_discriminator(self):
        assert not self._run(self._TAGGED_CLASSES + 'type X = Annotated[A | B, Field(discriminator="type")]')

    def test_flags_annassign_union(self):
        assert self._run(self._TAGGED_CLASSES + "X: object = A | B")


@pytest.mark.lint
class TestCE025LiveVerdictConsistency:
    """CE025: a criterion type's ``LiveSuccessCriterion`` subclassing (models/criteria.py)
    and its checker's ``live_verdict`` override (criteria/) must agree.

    Whole-tree / registry-based (like CE027-31), not a per-file AST rule: the
    invariant spans two separate class hierarchies (the criterion model in
    ``models/criteria.py`` and its checker in ``criteria/``) linked only via the
    shared ``type`` discriminator string through ``CriterionRegistry``, so it
    cannot be checked by looking at one file's AST in isolation.

    A criterion model that is a ``LiveSuccessCriterion`` subclass without a
    ``live_verdict`` override on its checker arms a criterion whose base
    ``live_verdict`` always returns ``"undecided"`` (a silent dead arm); a
    checker overriding ``live_verdict`` whose criterion model is NOT a
    ``LiveSuccessCriterion`` writes decision logic the arming path
    (``validate_early_stop`` gates on ``isinstance(c, LiveSuccessCriterion)``)
    can never reach.
    """

    @staticmethod
    def _type_to_model() -> dict[str, type]:
        from typing import Annotated, get_args, get_origin

        from coder_eval.models import SuccessCriterion

        assert get_origin(SuccessCriterion) is Annotated
        inner, *_ = get_args(SuccessCriterion)
        return {model.model_fields["type"].default: model for model in get_args(inner)}

    @staticmethod
    def _find_violations(pairs: dict[str, tuple[type, type]]) -> list[str]:
        """Shared checker-vs-model pairing logic, driven by an explicit
        ``{criterion_type: (checker_cls, model_cls)}`` mapping so both the real
        registry and synthetic fixtures can exercise it identically."""
        from coder_eval.criteria.base import BaseCriterion
        from coder_eval.models import LiveSuccessCriterion

        violations = []
        for criterion_type, (checker_cls, model) in pairs.items():
            overrides_live_verdict = checker_cls.live_verdict is not BaseCriterion.live_verdict
            is_live_model = issubclass(model, LiveSuccessCriterion)
            if is_live_model and not overrides_live_verdict:
                violations.append(
                    f"{model.__name__} ({criterion_type!r}) is a LiveSuccessCriterion but its checker "
                    f"{checker_cls.__name__} does not override live_verdict — a dead arm."
                )
            elif overrides_live_verdict and not is_live_model:
                violations.append(
                    f"{checker_cls.__name__} ({criterion_type!r}) overrides live_verdict but its criterion "
                    f"model {model.__name__} is not a LiveSuccessCriterion — unreachable decision logic."
                )
        return violations

    def test_real_criteria_tree_is_clean(self) -> None:
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=False)
        pairs = {
            criterion_type: (CriterionRegistry.get_checker(criterion_type), model)
            for criterion_type, model in self._type_to_model().items()
        }
        assert not self._find_violations(pairs)

    def test_detects_live_model_with_no_live_verdict_override(self) -> None:
        # A LiveSuccessCriterion subclass whose checker does NOT override
        # live_verdict — the dead-arm branch.
        from coder_eval.criteria.base import BaseCriterion
        from coder_eval.models import LiveSuccessCriterion

        class _FakeLiveModel(LiveSuccessCriterion):
            def live_decidable_polarities(self):
                return frozenset({"pass"})

        class _FakeCheckerNoOverride(BaseCriterion):
            criterion_type = "fake_live_no_override"

            def _check_impl(self, criterion, sandbox, *, turn_records=None, context=None):
                raise NotImplementedError

        violations = self._find_violations({"fake_live_no_override": (_FakeCheckerNoOverride, _FakeLiveModel)})
        assert violations
        assert "dead arm" in violations[0]

    def test_detects_live_verdict_override_on_non_live_model(self) -> None:
        # A checker overriding live_verdict whose criterion model is a plain
        # BaseSuccessCriterion, not LiveSuccessCriterion — the unreachable
        # decision-logic branch.
        from coder_eval.criteria.base import BaseCriterion, LiveVerdict
        from coder_eval.models import BaseSuccessCriterion

        class _FakeNonLiveModel(BaseSuccessCriterion):
            type: str = "fake_non_live_override"

        class _FakeCheckerOverrides(BaseCriterion):
            criterion_type = "fake_non_live_override"

            def _check_impl(self, criterion, sandbox, *, turn_records=None, context=None):
                raise NotImplementedError

            def live_verdict(self, criterion, turn_records) -> LiveVerdict:
                return "undecided"

        violations = self._find_violations({"fake_non_live_override": (_FakeCheckerOverrides, _FakeNonLiveModel)})
        assert violations
        assert "unreachable" in violations[0]


@pytest.mark.lint
class TestCE009YamlModelsForbidExtras:
    """CE009 fires on non-compliant BaseModel classes in every scoped module."""

    _VIOLATING = "from pydantic import BaseModel\nclass Broken(BaseModel):\n    name: str\n"
    _COMPLIANT = (
        "from pydantic import BaseModel, ConfigDict\n"
        "class Fine(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n'
        "    name: str\n"
    )

    @staticmethod
    def _run(src: str, path: str):
        import ast

        from tests.lint.rules.yaml_models_forbid_extras import YamlModelsForbidExtras

        return YamlModelsForbidExtras(path).check(ast.parse(src))

    def test_scoped_paths_all_activate(self):
        # A typo'd _SCOPED_PATHS entry would silently disable enforcement for
        # that module while the zero-violation sweep stays green — pin each one.
        from tests.lint.rules.yaml_models_forbid_extras import _SCOPED_PATHS

        assert len(_SCOPED_PATHS) == 8
        for path in _SCOPED_PATHS:
            assert self._run(self._VIOLATING, path), f"CE009 did not fire on scoped path {path}"

    def test_compliant_class_not_flagged(self):
        assert not self._run(self._COMPLIANT, "src/coder_eval/models/tasks.py")

    def test_exempt_modules_not_scoped(self):
        for path in ("src/coder_eval/models/results.py", "src/coder_eval/models/telemetry.py"):
            assert not self._run(self._VIOLATING, path)


@pytest.mark.lint
class TestCE027DocEnvVarParity:
    """CE027 — documented framework env-var assignments must be backed by a
    real consumer (a Settings field/alias or an os.getenv read in src/).

    `Settings` sets no `env_prefix` and uses `extra="ignore"`, so a documented
    `NAME=value` whose name matches no field is silently dropped at runtime — the
    `CODER_EVAL_API_BACKEND` (real field: `API_BACKEND`) doc bug. Scans real
    Markdown/YAML surfaces, so it lives here rather than in the AST-only runner.
    """

    REPO_ROOT = Path(__file__).parent.parent

    def test_repo_docs_have_no_unbacked_env_vars(self):
        from tests.lint.doc_env_parity import default_doc_paths, find_unbacked_env_vars

        findings = find_unbacked_env_vars(default_doc_paths(self.REPO_ROOT), self.REPO_ROOT / "src")
        assert not findings, (
            "\nDocumented framework env-var assignment(s) that no Settings field/alias or "
            "src/ reference backs — they are silently dropped at runtime (Settings uses "
            'extra="ignore"). Fix the spelling to a real env name, or add the consumer:\n\n'
            + "\n".join(f"  {path}: {', '.join(names)}" for path, names in sorted(findings.items()))
        )

    def test_catches_the_coder_eval_api_backend_shadow(self):
        # The exact regression: a CODER_EVAL_-prefixed spelling of a real field.
        from tests.lint.doc_env_parity import scan_doc_env_assignments, settings_env_names, src_env_literals

        valid = settings_env_names() | src_env_literals(self.REPO_ROOT / "src")
        found = scan_doc_env_assignments("      CODER_EVAL_API_BACKEND=bedrock\n")
        assert "CODER_EVAL_API_BACKEND" in found
        assert "CODER_EVAL_API_BACKEND" not in valid  # would be flagged

    def test_real_backend_field_is_backed(self):
        from tests.lint.doc_env_parity import settings_env_names

        assert "API_BACKEND" in settings_env_names()

    @pytest.mark.parametrize(
        "line",
        [
            "AWS_BEARER_TOKEN_BEDROCK=${{ secrets.BEDROCK_TOKEN }}",  # secret RHS, not an assignment of BEDROCK_TOKEN
            "[Codex Agent Guide](docs/CODEX_AGENT_GUIDE.md)",  # markdown link, not an assignment
            'pattern: "^API_KEY = \\"\\\\w+\\""',  # regex example with a space before '='
            "the CODEX_MODEL setting selects the model",  # bare prose mention, not an assignment
            "X-API_KEY=v",  # hyphenated token — prefix embedded, not a standalone name
            "dir/API_THING=v",  # path segment — prefix embedded
            "http://API_HOST=1",  # URL segment — prefix embedded
        ],
    )
    def test_non_assignment_shapes_are_not_flagged(self, line: str):
        from tests.lint.doc_env_parity import scan_doc_env_assignments, settings_env_names, src_env_literals

        valid = settings_env_names() | src_env_literals(self.REPO_ROOT / "src")
        unbacked = [t for t in scan_doc_env_assignments(line) if t not in valid]
        assert unbacked == [], f"false positive on non-assignment shape: {unbacked}"

    def test_name_side_of_env_value_literal_counts_as_backed(self):
        # `--env CODER_EVAL_IN_CONTAINER=1` in src makes the doc assignment backed.
        from tests.lint.doc_env_parity import src_env_literals

        names = src_env_literals(self.REPO_ROOT / "src")
        assert "CODER_EVAL_IN_CONTAINER" in names

    def test_src_scan_requires_a_real_consumer_not_any_literal(self, tmp_path: Path):
        # A bare uppercase constant that no code reads must NOT count as "backed",
        # or it could silently mask a documented-but-unconsumed assignment.
        from tests.lint.doc_env_parity import src_env_literals

        (tmp_path / "m.py").write_text(
            'CONST = "CODER_EVAL_BOGUS"\nx = os.getenv("CODER_EVAL_REAL")\n', encoding="utf-8"
        )
        names = src_env_literals(tmp_path)
        assert "CODER_EVAL_REAL" in names  # a genuine os.getenv read is backed
        assert "CODER_EVAL_BOGUS" not in names  # a bare constant is not


@pytest.mark.lint
class TestCE029DocYamlExamples:
    """CE029 — self-contained YAML examples in the docs must validate.

    A published snippet that raises when copy-pasted reads as a broken feature.
    The motivating bug: the `prompt_mutations` recipe used `text:` where the
    field is `content:`, and every mutation model sets `extra="forbid"`. Scans
    real Markdown, so it lives here rather than in the AST-only runner.
    """

    REPO_ROOT = Path(__file__).parent.parent

    @staticmethod
    def _check(text: str) -> str | None:
        from tests.lint.doc_examples import extract_yaml_blocks, validate_block

        blocks = extract_yaml_blocks(Path("doc.md"), text)
        assert len(blocks) == 1, f"expected exactly one yaml block, got {len(blocks)}"
        return validate_block(blocks[0])

    _VALID_TASK = """```yaml
task_id: "demo"
description: "A demo task"
initial_prompt: "Write hello.py"
success_criteria:
  - type: "file_exists"
    path: "hello.py"
    description: "hello.py exists"
```
"""

    def test_repo_doc_examples_validate(self):
        from tests.lint.doc_examples import default_doc_paths, find_invalid_doc_examples

        findings = find_invalid_doc_examples(default_doc_paths(self.REPO_ROOT))
        assert not findings, (
            "\nSelf-contained YAML example(s) in the docs that do not validate against their "
            "model — a reader who copy-pastes this hits a ValidationError. Fix the example, or "
            f"mark the block with `{'<!-- lint-skip: doc-yaml -->'}` if it is intentionally partial:\n\n"
            + "\n".join(f"  {path}:\n    " + "\n    ".join(errs) for path, errs in sorted(findings.items()))
        )

    def test_valid_task_block_passes(self):
        assert self._check(self._VALID_TASK) is None

    def test_catches_the_prompt_mutations_regression(self):
        # The exact historical bug this rule exists for: `text:` where the
        # PromptSuffix field is `content:` (every mutation model forbids extras).
        finding = self._check(
            """```yaml
experiment_id: prompt-phrasing
description: "Terse vs. detailed"
variants:
  - variant_id: terse
  - variant_id: detailed
    prompt_mutations:
      - type: suffix
        text: "Think step by step."
```
"""
        )
        assert finding is not None
        assert "content" in finding

    def test_schematic_placeholder_blocks_are_skipped(self):
        # The task guide's overview block deliberately writes `agent: { ... }`.
        assert (
            self._check(
                """```yaml
task_id: "my_task"
description: "What this task tests"
initial_prompt: "Instructions..."
agent: { ... }
success_criteria: [ ... ]
```
"""
            )
            is None
        )

    def test_fragment_blocks_are_not_validated(self):
        # A bare criteria list is illustrative, not a document. Validating it
        # would flag most blocks in the docs and get the rule deleted.
        assert (
            self._check(
                """```yaml
success_criteria:
  - type: "file_exists"
    path: "app.py"
```
"""
            )
            is None
        )

    def test_lint_skip_marker_is_honored(self):
        from tests.lint.doc_examples import SKIP_MARKER

        broken = self._VALID_TASK.replace('  - type: "file_exists"', "  - type: no_such_criterion")
        assert self._check(broken) is not None, "control: the block must fail without the marker"
        assert self._check(f"{SKIP_MARKER}\n\n{broken}") is None

    def test_non_yaml_fences_are_ignored(self):
        from tests.lint.doc_examples import extract_yaml_blocks

        text = '```json\n{"task_id": 1}\n```\n\n```bash\ncoder-eval run\n```\n'
        assert extract_yaml_blocks(Path("doc.md"), text) == []

    def test_info_string_attributes_are_still_captured(self):
        # mkdocs-material allows ```yaml title="x"; such a block must not be
        # silently skipped, or a broken example there escapes the rule.
        from tests.lint.doc_examples import extract_yaml_blocks

        text = '```yaml title="task.yaml"\ntask_id: 1\n```\n'
        blocks = extract_yaml_blocks(Path("doc.md"), text)
        assert len(blocks) == 1

    def test_valid_experiment_block_passes(self):
        assert (
            self._check(
                """```yaml
experiment_id: demo-experiment
description: "A demo experiment"
variants:
  - variant_id: baseline
  - variant_id: treatment
    agent:
      model: claude-sonnet-4-6
```
"""
            )
            is None
        )


@pytest.mark.lint
class TestCE030DocSchemaParity:
    """CE030 — a registered model's fields must be documented or exempted with a reason.

    Every P0/P1 defect in the docs overhaul was an undocumented user-facing
    field. This gate makes that recurrence impossible for a small, explicit
    registry of models: adding a field forces a doc update or a reasoned
    EXEMPT entry. Scans real Markdown, so it lives here, not in the AST runner.
    """

    REPO_ROOT = Path(__file__).parent.parent

    def test_registered_models_are_fully_documented(self):
        from tests.lint.doc_schema_parity import find_undocumented_fields

        findings = find_undocumented_fields(self.REPO_ROOT)
        assert not findings, (
            "\nRegistered model field(s) documented nowhere in their doc page (and not EXEMPT) — "
            "document them (mention the field name as `inline code`) or add an EXEMPT entry with a "
            "reason in tests/lint/doc_schema_parity.py:\n\n"
            + "\n".join(f"  {model}: {', '.join(fields)}" for model, fields in sorted(findings.items()))
        )

    def test_exempt_fields_carry_a_reason(self):
        from tests.lint.doc_schema_parity import EXEMPT

        for model_name, fields in EXEMPT.items():
            for field_name, reason in fields.items():
                assert reason and reason.strip(), f"EXEMPT[{model_name}][{field_name}] has an empty reason"

    def test_exemptions_reference_real_fields(self):
        # An exemption left behind after a field rename would silently mask a
        # real undocumented field. Pin every exempt name to a real model field.
        from tests.lint.doc_schema_parity import DOCUMENTED_MODELS, EXEMPT

        by_name = {m.__name__: m for m, _ in DOCUMENTED_MODELS}
        for model_name, fields in EXEMPT.items():
            assert model_name in by_name, f"EXEMPT names unknown model {model_name!r}"
            real = set(by_name[model_name].model_fields)
            for field_name in fields:
                assert field_name in real, f"EXEMPT[{model_name}] names non-field {field_name!r}"

    def test_detects_an_undocumented_field(self):
        from pydantic import BaseModel, Field

        from tests.lint.doc_schema_parity import undocumented_fields

        class Synthetic(BaseModel):
            documented: str = Field(default="")
            undocumented: str = Field(default="")

        doc = "The `documented` field is described here."
        assert undocumented_fields(Synthetic, doc, {}) == ["undocumented"]

    def test_inline_code_match_only(self):
        # A field mentioned in prose without backticks does NOT count as documented.
        from pydantic import BaseModel, Field

        from tests.lint.doc_schema_parity import undocumented_fields

        class Synthetic(BaseModel):
            widget: str = Field(default="")

        assert undocumented_fields(Synthetic, "The widget setting is great.", {}) == ["widget"]
        assert undocumented_fields(Synthetic, "The `widget` setting is great.", {}) == []

    def test_exempt_field_is_not_reported(self):
        from pydantic import BaseModel, Field

        from tests.lint.doc_schema_parity import undocumented_fields

        class Synthetic(BaseModel):
            internal: str = Field(default="")

        assert undocumented_fields(Synthetic, "", {"internal": "set by the framework"}) == []

    def test_missing_doc_file_fails_loudly(self):
        # A moved/renamed doc must fail the gate, not vacuously pass. The
        # integration wrapper maps a missing file to "" → every field reported.
        from tests.lint.doc_schema_parity import find_undocumented_fields

        findings = find_undocumented_fields(self.REPO_ROOT / "no_such_dir")
        assert findings, "a missing doc file must surface every field, not return empty"
        assert any("RunLimits" in key for key in findings)


@pytest.mark.lint
class TestCE028DocIndexParity:
    """CE028 — the flat index surfaces (README, docs/index.md, docs/llms.txt) are
    generated from the mkdocs nav + extra.docs_index; disk must match.

    Also enforces the invariants the render depends on: nav<->blurb bijection,
    every published docs/ page is in the nav, and the hand-written tutorials
    table stays in parity with the nav. Reasons over Markdown/YAML, so it lives
    here rather than in the AST runner.
    """

    REPO_ROOT = Path(__file__).parent.parent

    @property
    def _nav(self):
        from tests.lint.doc_indexes import load_nav

        return load_nav(self.REPO_ROOT / "mkdocs.yml")

    @property
    def _blurbs(self):
        from tests.lint.doc_indexes import load_blurbs

        return load_blurbs(self.REPO_ROOT / "mkdocs.yml")

    def test_repo_indexes_match_generated_output(self):
        from tests.lint.doc_indexes import check

        findings = check(self.REPO_ROOT)
        assert not findings, (
            "\nGenerated index surface(s) drifted from the mkdocs nav — run `make docs-indexes` "
            "to regenerate:\n\n" + "\n\n".join(f"{path}:\n{diff}" for path, diff in sorted(findings.items()))
        )

    def test_every_nav_page_has_a_blurb(self):
        from tests.lint.doc_indexes import missing_blurbs

        missing = missing_blurbs(self._nav, self._blurbs)
        assert not missing, f"nav pages without an extra.docs_index blurb (tutorial leaves exempt): {missing}"

    def test_no_orphan_blurbs(self):
        from tests.lint.doc_indexes import orphan_blurbs

        orphans = orphan_blurbs(self._nav, self._blurbs)
        assert not orphans, f"extra.docs_index blurbs with no matching nav page: {orphans}"

    def test_every_published_doc_is_in_the_nav(self):
        from tests.lint.doc_indexes import docs_missing_from_nav

        missing = docs_missing_from_nav(self.REPO_ROOT, self._nav)
        assert not missing, f"published docs/ pages absent from the nav (add to nav or the exclusion set): {missing}"

    def test_tutorials_table_matches_nav(self):
        from tests.lint.doc_indexes import tutorials_table_drift

        drift = tutorials_table_drift(self.REPO_ROOT, self._nav)
        assert drift is None, drift

    def test_tutorials_avoid_mkdocs_only_admonitions(self):
        # Tutorials are read on GitHub as often as on the docs site — from the repo, from a
        # PR diff, from a search result. `!!! note` is mkdocs-material syntax that GitHub
        # does not understand: it renders the marker as literal text and turns the indented
        # body into an accidental code block. Tutorials 01-07 use plain `>` blockquotes,
        # which render correctly in both, so this pins that convention for new ones.
        #
        # Scoped to tutorials on purpose: reference pages under docs/ are site-first and one
        # (DATASETS.md) predates this rule.
        offenders = []
        for path in sorted((self.REPO_ROOT / "docs" / "tutorials").glob("*.md")):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.startswith("!!! ") or line.startswith("??? "):
                    offenders.append(f"{path.relative_to(self.REPO_ROOT)}:{line_no}: {line.strip()}")
        assert not offenders, (
            "mkdocs-material admonition(s) in a tutorial — these render as literal text plus "
            "a stray code block on GitHub. Use a `>` blockquote instead:\n  " + "\n  ".join(offenders)
        )

    def test_real_mkdocs_yaml_parses_despite_env_tag(self):
        # The real mkdocs.yml carries `!ENV [CI, false]`; load_nav must tolerate it.
        nav = self._nav
        assert any(p.doc == "index.md" for p in nav)
        assert any(p.doc == "DIALOG_MODE.md" for p in nav)

    def test_nav_loader_does_not_mutate_global_safeloader(self):

        from tests.lint.doc_indexes import load_nav

        load_nav(self.REPO_ROOT / "mkdocs.yml")
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load("x: !ENV [A, b]")

    def test_python_object_tags_are_still_rejected(self):

        from tests.lint.doc_indexes import load_nav

        # Prove the private loader stays safe_load-equivalent for object construction.
        load_nav(self.REPO_ROOT / "mkdocs.yml")

        class _Probe(yaml.SafeLoader):
            pass

        _Probe.add_multi_constructor("!", lambda loader, suffix, node: None)
        with pytest.raises(yaml.constructor.ConstructorError):
            yaml.load("x: !!python/object/apply:os.system ['echo hi']", Loader=_Probe)

    @pytest.mark.parametrize(
        ("doc", "route"),
        [
            ("index.md", "/docs"),
            ("USER_GUIDE.md", "/docs/user-guide"),
            ("agents/CLAUDE_CODE.md", "/docs/agents/claude-code"),
            ("tutorials/README.md", "/docs/tutorials"),
        ],
    )
    def test_route_for_known_shapes(self, doc: str, route: str):
        from tests.lint.doc_indexes import route_for

        assert route_for(doc) == route

    def test_write_is_idempotent(self, tmp_path: Path):
        import shutil

        from tests.lint.doc_indexes import check, write

        # Copy the pieces write() touches into a scratch tree so the live repo is untouched.
        (tmp_path / "docs").mkdir()
        shutil.copy(self.REPO_ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
        for rel in ("README.md", "docs/index.md", "docs/llms.txt", "docs/tutorials/README.md"):
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.REPO_ROOT / rel, dest)

        write(tmp_path)
        first = (tmp_path / "README.md").read_text(encoding="utf-8")
        write(tmp_path)
        assert (tmp_path / "README.md").read_text(encoding="utf-8") == first
        assert check(tmp_path) == {}

    def test_missing_marker_raises_clear_error(self):
        from tests.lint.doc_indexes import _replace_between

        with pytest.raises(ValueError, match="marker pair"):
            _replace_between("no markers here", "<!-- start -->", "<!-- end -->", "body")

    def test_drift_is_detected(self, tmp_path: Path):
        # A hand-edit between the markers must be reported by check().
        import shutil

        from tests.lint.doc_indexes import check

        (tmp_path / "docs").mkdir()
        shutil.copy(self.REPO_ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
        for rel in ("README.md", "docs/index.md", "docs/llms.txt", "docs/tutorials/README.md"):
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.REPO_ROOT / rel, dest)

        readme = tmp_path / "README.md"
        text = readme.read_text(encoding="utf-8")
        readme.write_text(text.replace("| [User Guide]", "| [Tampered Guide]"), encoding="utf-8")
        findings = check(tmp_path)
        assert str(readme) in findings


# The Claude Code plugin's skills, resolved at collection time so every skill is
# parametrized into the frontmatter / path-containment guards below automatically.
PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "coder-eval"
PLUGIN_SKILLS = sorted(PLUGIN_ROOT.glob("skills/*/SKILL.md"))

# Every text file the plugin ships, not just its skills: a bundled reference is
# copied to the same parentless cache directory and so carries the identical
# runtime-path constraint. Extension-filtered rather than read-and-hope, so a
# future binary asset is skipped instead of crashing the guard.
PLUGIN_TEXT_FILES = sorted(
    p for p in PLUGIN_ROOT.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml", ".yml", ".json", ".jsonl"}
)

# Skills that shell out to the `coder-eval` CLI and must therefore preflight
# `coder-eval --version`. Both READMEs describe this behaviour, so it needs pinning:
# `task` shipped without the check while running `coder-eval plan` AND `coder-eval run`,
# which handed a first-run user a bare `command not found` only after writing N files.
# `analyze` reads a finished run directory, `ci` only emits a workflow, and `lint-tasks`
# has no `Bash` at all (the `coder-eval plan` in its report is a suggestion to the user,
# not a command it runs) — so none of those three needs the CLI.
# `optimize-skill` drives the CLI harder than any other skill here — a baseline, a triage
# stage, three gate invocations and a test confirmation, all `coder-eval run`.
SKILLS_REQUIRING_THE_CLI = {"init", "check-skill", "task", "optimize-skill"}

# The skills that read the shared task-quality rubric. A rubric no skill reads is
# dead weight; a reader that stops reading it has silently forked the rubric.
# `optimize-skill` reads one section rather than the whole file — the grader-fairness
# questions, which it asks of a baseline's per-row detail while `task` asks the same ones
# while writing the grader. It restated them until they were consolidated; being in this
# set is what keeps the pointer from being deleted back into a second copy.
RUBRIC_READERS = {"task", "lint-tasks", "init", "optimize-skill"}

# Whether each skill must locate a repository's eval tree before it can do anything.
# All seven currently must, and each for its own reason: `analyze` needs the run store,
# `init` and `check-skill` must know where tasks already live before writing beside
# them, `lint-tasks` and `task` glob the task tree, and `ci` writes the resolved glob
# into the workflow it emits. Every one of them used to carry its own hardcoded guess
# (`runs/latest`, `tasks/`), which is wrong in any repository that names the tree
# something else or nests it — so the policy is declared once in
# reference/repo-layout.md and a reader that stops pointing at it has forked it.
#
# A mapping rather than a set, mirroring SKILL_DISABLE_MODEL_INVOCATION: a SEVENTH skill
# then has to state whether it needs discovery instead of silently defaulting to "no"
# and quietly reintroducing a hardcoded path. `False` is a legitimate answer — a skill
# that touches no task or run tree — but it has to be written down.
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

# Ways of naming one repository's layout as the DEFAULT. The bare paths `runs/latest`
# and `tasks/` are deliberately absent: `analyze` legitimately keeps a sentence about a
# `latest` symlink resolving, and `ci` has to show *some* glob in its snippet. What may
# not survive is a skill falling back to those paths when it was given nothing.
EVAL_ROOT_DEFAULT_PHRASINGS = (
    "default to `runs/latest`",
    "default to `tasks/`",
    "fell back to the default",
)

# Which skills are explicit-invocation only. Scaffolding a directory (`init`) or
# writing a CI workflow (`ci`) is never something to do unprompted; the rest are
# safe for the agent to reach for on its own.
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

# The surfaces that must name every shipped skill, so a new one cannot ship
# undocumented. Adding a surface is one edit here.
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

# Claude Code loads a listing of every skill's name and description into context.
# The listing's character budget scales at ~1% of the model's context window and is
# SHARED with every other skill the user has installed; when it overflows,
# descriptions are dropped starting with the least-invoked skills. So a plugin that
# grows its descriptions without bound quietly evicts the user's own skills. This
# ceiling makes growth a reviewed decision: raising it is allowed, in a commit that
# says why — which is exactly what a silent drift would not be. Asserted on the SUM,
# not per skill: the longest single description is ~300 against a 1,536 per-entry
# truncation limit, so a per-skill cap would guard nothing.
SKILL_LISTING_BUDGET_CHARS = 1_600

# Tokens that name THIS repository's files. An installed plugin is copied to
# ~/.claude/plugins/cache/ without its parent directories, so any of these in a
# skill body is a path that does not exist at runtime. `tasks/` and
# `.claude/skills/` are deliberately absent: those are user-workspace paths the
# skills legitimately scan and scaffold.
REPO_PATH_TOKENS = ("docs/", "src/", ".claude/shared/", ".claude/commands/", "uv run", "../")


# `_normalized`'s own implementation — the single legitimate use of the raw idiom, named
# here so the guard below can exempt it without depending on where in the file it sits.
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


# The proposal shape's load-bearing instructions. At module level so the self-test can feed the
# same tuple to `_missing_tokens` against mutated text.
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


# The PROCEDURE half of optimize-skill's instructions, asserted against `SKILL.md` alone.
# Moving any of these into the method file would leave the step that must act on it silently
# pointing elsewhere — the failure mode the method/procedure extraction itself could introduce.
#
# At module level, like `_PROPOSAL_TOKENS`, so `_missing_tokens` can be run against MUTATED
# text by a committed self-test. A deletion sensor with no self-test can be reverted to a
# no-op with every test still green.
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
       the selected count since CE043, and ``/coder-eval:task``'s step 6 requires an author to run
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


@pytest.mark.lint
class TestPluginArtifacts:
    """The Claude Code plugin's shipped artifacts must be valid and self-contained.

    `claude plugin validate --strict` (run by the plugin-validate CI job) checks
    the manifests but NOT skill frontmatter — a SKILL.md carrying an unsupported
    `name:` plus an invented key passes it with zero warnings. These tests are
    what stand between a typo'd frontmatter key and a skill that silently never
    triggers, and between a skill body and a repo path that does not exist once
    the plugin is installed.
    """

    REPO_ROOT = Path(__file__).parent.parent
    TEMPLATES = PLUGIN_ROOT / "reference" / "templates"

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

    # --- The shipped grader scaffold -------------------------------------------------------
    #
    # These shell out to `outcome-grader/verify.py` and assert its BEHAVIOUR, because it is the
    # execution track's measuring instrument: a grader that is subtly wrong biases every arm of an
    # A/B equally, so no cross-arm comparison can reveal it. They live in this class rather than a
    # module of their own to sit beside the other shipped-artifact assertions — the scaffold is a
    # plugin artifact, and the question is the same one asked of `outcome.yaml` next door.

    GRADER = PLUGIN_ROOT / "reference" / "templates" / "outcome-grader" / "verify.py"

    @staticmethod
    def _grade(
        tmp_path: Path,
        spec: object,
        artifact: str | None,
        *,
        row: str = "r1",
        artifact_path: str | None = None,
    ) -> tuple[float, str, int]:
        """Run the shipped scaffold over one fabricated row, returning (score, output, exit code).

        Copies the scaffold rather than pointing at it in place: the grader resolves its
        expectations relative to ITSELF, so writing fixtures beside the real one would leave files
        in the plugin tree. The artifact is written under `cwd`, which is what `run_command` sets
        to the sandbox.
        """
        import json
        import shutil
        import subprocess
        import sys

        grader_dir = tmp_path / "grader"
        # `parents=True`: callers pass a SUBdirectory of the fixture when one test grades two
        # artifacts (the discrimination margin), and that parent has not been created.
        grader_dir.mkdir(parents=True)
        shutil.copy(TestPluginArtifacts.GRADER, grader_dir / "verify.py")
        (grader_dir / "expectations").mkdir()
        (grader_dir / "expectations" / f"{row}.json").write_text(json.dumps(spec), encoding="utf-8")

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        if artifact is not None:
            # `artifact_path` for the malformed-spec cases, where the spec cannot supply one.
            relative = artifact_path or (spec["path"] if isinstance(spec, dict) else None)
            assert isinstance(relative, str), "pass artifact_path when the spec carries no usable one"
            target = sandbox / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(artifact, encoding="utf-8")

        completed = subprocess.run(
            [sys.executable, str(grader_dir / "verify.py"), row],
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=60,
        )
        first = completed.stdout.splitlines()[0]
        return float(first), completed.stdout, completed.returncode

    def test_outcome_grader_prints_float_on_first_line(self, tmp_path: Path):
        # The protocol `score_from_stdout` reads: line 1 parses as a float in [0.0, 1.0], every
        # later line is detail. A grader that printed its detail first would score 0.0 with the
        # criterion reporting a parse error, on every row of every arm.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha"]}}}
        score, output, code = self._grade(tmp_path, spec, "# Report\n\nalpha\n")
        assert code == 0
        assert 0.0 <= score <= 1.0
        assert score == 1.0, output
        assert len(output.splitlines()) > 1, "the score line carries no detail — a 0.0 would be unreadable"
        # The other end of the same protocol, asserted here rather than in a third first-line test:
        # rule attribution is the LAST line, so adding it cannot have moved the score off line 1.
        assert not output.splitlines()[0].startswith("RULES "), output
        assert output.splitlines()[-1].startswith("RULES "), output

    def test_outcome_grader_na_drops_from_denominator(self, tmp_path: Path):
        # THE load-bearing rule. A check that does not apply must leave the numerator AND the
        # denominator: scoring it as a failure charges the row for a question nobody asked, and
        # scoring it as a pass inflates every arm equally — which is worse, because it is
        # invisible to every comparison downstream.
        #
        # The fixture is one PASS + one FAIL + one N/A, so the two failure modes are separated
        # NUMERICALLY (0.5 here; 1/3 if the N/A were counted as a failure, 2/3 as a pass) rather
        # than only by a substring in the detail lines.
        artifact = "# Report\n\nalpha\n"
        with_na = {
            "path": "out/report.md",
            "checks": {
                "mentions#hit": {"all_of": ["alpha"]},
                "mentions#miss": {"all_of": ["absent"]},
                "json_field": {},  # declares no field: N/A
            },
        }
        without = {
            "path": "out/report.md",
            "checks": {"mentions#hit": {"all_of": ["alpha"]}, "mentions#miss": {"all_of": ["absent"]}},
        }
        na_score, na_output, _ = self._grade(tmp_path / "a", with_na, artifact)
        plain_score, _, _ = self._grade(tmp_path / "b", without, artifact)
        assert na_score == plain_score == 0.5, na_output
        assert "N/A" in na_output and "1/2 applicable" in na_output, na_output

    def test_outcome_grader_na_never_depends_on_the_artifact(self, tmp_path: Path):
        """Two arms answering one row must be scored over the SAME denominator.

        The defect this pins is the one that inverts an A/B verdict rather than merely biasing it.
        When a check returns N/A because the ARTIFACT is the wrong shape, an arm that ignored the
        requirement entirely drops that check and is scored 1/1, while an arm that complied and got
        one field wrong is scored 1/2 — the worse artifact wins, and nothing in the report says so.

        So the N/A trigger must be a property of the ROW. Here the row asks for JSON; the arm that
        did not produce JSON must FAIL that question, not escape it.
        """
        spec = {
            "path": "out/report.md",
            "checks": {"mentions": {"all_of": ["summary"]}, "json_field": {"field": "status", "equals": "ok"}},
        }
        complied, complied_out, _ = self._grade(tmp_path / "a", spec, '{"summary": "did it", "status": "failed"}')
        ignored, ignored_out, _ = self._grade(tmp_path / "b", spec, "# summary\n\nprose, not JSON\n")
        assert "1/2 applicable" in complied_out, complied_out
        assert "1/2 applicable" in ignored_out, ignored_out
        assert complied == ignored == 0.5, (complied_out, ignored_out)

    def test_outcome_grader_rejects_a_string_where_a_list_is_required(self, tmp_path: Path):
        # `"all_of": "one needle"` — the most natural slip in a hand-written expectations file.
        # Iterating a string yields CHARACTERS, so every check would report "all present" against
        # any artifact of moderate length: a silent 1.0 on every row of every arm, which is exactly
        # the class of defect this instrument exists to avoid.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": "runs a lint step"}}}
        score, output, code = self._grade(tmp_path, spec, "An unrelated summary sentence.\n")
        assert code == 0
        assert score == 0.0, output
        assert "must be a LIST" in output, output

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ({"paths": "out/report.md", "checks": {}}, "string `path`"),
            ({"path": "out/report.md", "checks": [1, 2]}, "`checks` must be an object"),
            ([1, 2], "string `path`"),
        ],
        ids=["path-key-typo", "checks-is-a-list", "spec-is-a-list"],
    )
    def test_outcome_grader_reports_a_malformed_expectations_file(self, tmp_path: Path, spec, expected: str):
        # Author errors in the expectations file must report a score and a reason, never a
        # traceback: coder-eval checks the exit code BEFORE parsing the score line, so a crashed
        # grader is a 0.0 whose cause is not in the report.
        score, output, code = self._grade(tmp_path, spec, "x\n", artifact_path="out/report.md")
        assert code == 0
        assert score == 0.0
        assert expected in output, output

    def test_outcome_grader_keeps_one_check_to_one_detail_line(self, tmp_path: Path):
        # A detail carrying a newline would split into extra lines that read as further check
        # results. Here the forged text arrives through the artifact, quoted back by `mentions`'
        # "missing [...]" detail — so the line count is a fact about the OUTPUT FORMAT, not about
        # what a well-behaved check happens to return.
        forged = "absent\nPASS mentions#ghost: all present"
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": [forged]}}}
        score, output, code = self._grade(tmp_path, spec, "nothing here\n")
        assert code == 0 and score == 0.0
        lines = output.splitlines()
        # score + summary + exactly one detail line + the trailing RULES line
        assert len(lines) == 4, f"one check produced {len(lines) - 3} detail lines: {lines}"
        assert lines[-1].startswith("RULES "), lines
        assert not any(line.startswith("PASS") for line in lines), lines

    def test_outcome_grader_labels_let_one_check_be_declared_twice(self, tmp_path: Path):
        # JSON object keys are unique, so two `mentions` entries would silently collapse to the
        # last one — halving the denominator with no message. The `#label` suffix is what lets a
        # row carry several independent checks of one kind, which is also what makes it continuous.
        spec = {
            "path": "out/report.md",
            "checks": {"mentions#a": {"all_of": ["alpha"]}, "mentions#b": {"all_of": ["absent"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha only\n")
        assert code == 0
        assert score == 0.5, output
        assert "mentions#a" in output and "mentions#b" in output, output

    def test_outcome_grader_score_line_is_read_by_the_real_criterion(self, tmp_path: Path):
        # End to end through `RunCommandChecker._score_from_stdout`, not this file's own
        # `float(stdout.splitlines()[0])`: the tests would otherwise agree with the grader about a
        # protocol neither shares with the code that actually reads it.
        from coder_eval.criteria.run_command import RunCommandChecker
        from coder_eval.models import RunCommandCriterion

        spec = {
            "path": "out/report.md",
            "checks": {"mentions#a": {"all_of": ["alpha"]}, "mentions#b": {"all_of": ["absent"]}},
        }
        _score, stdout, code = self._grade(tmp_path, spec, "alpha only\n")
        criterion = RunCommandCriterion(description="grader", command="python3 verify.py r1", score_from_stdout=True)
        result = RunCommandChecker()._score_from_stdout(criterion, code, stdout, "")
        assert result.error is None, result.error
        assert result.score == 0.5
        assert "mentions#b" in (result.details or ""), "the detail lines did not survive into the criterion result"
        # `rule_row_map` reads the RULES line back out of exactly this field, so the round trip
        # through the real criterion is what proves the attribution is reachable at all.
        assert "RULES " in (result.details or ""), "the RULES line did not survive into the criterion result"

    # The four shipped expectations files, one per row of `outcome-rows.jsonl`. Read once here
    # rather than in each assertion below, and asserted non-empty so a moved directory reports a
    # GAP instead of passing vacuously over nothing.
    @staticmethod
    def _shipped_expectations() -> dict[str, dict]:
        import json

        directory = TestPluginArtifacts.GRADER.parent / "expectations"
        specs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}
        assert specs, f"GAP: no expectations files under {directory} — this whole group would pass vacuously"
        return specs

    @staticmethod
    def _shipped_row_ids() -> list[str]:
        import json

        rows = TestPluginArtifacts.TEMPLATES / "outcome-rows.jsonl"
        ids = [json.loads(line)["id"] for line in rows.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert ids, f"GAP: {rows} carries no rows — the parity assertions below would compare two empty sets"
        return ids

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

    def test_task_skill_documents_row_roles_and_check_floor(self):
        # The two authoring-time rules an author cannot infer from a file that validates. A suite
        # can satisfy every schema in the tree and still be incapable of resolving anything.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        for token, why in (
            (
                "discriminator",
                "a row the incumbent already passes cannot show improvement, only regression — "
                "conflating the two roles is what produces a suite of mostly dead weight",
            ),
            ("guard", "the other half of the role split, and the reason a 1.000 row is not an error"),
            (
                "temptation test",
                "a discriminator whose lazy implementation already satisfies the rule sits at the "
                "ceiling and measures nothing, however many arms are run through it",
            ),
            (
                "Depth over breadth",
                "one row per rule maximises the denominator while minimising per-rule headroom — "
                "the worst possible suite shape, and the one an author writes by default",
            ),
        ):
            assert token in text, f"task/SKILL.md no longer states {token!r} — {why}"

        assert "min / mean / max" in text and "minimum of **4**" in text, (
            "task/SKILL.md's outcome-mode validation lost the applicable-check floor. Below four "
            "checks a row behaves like a binary grader, which manufactures the execution gate's "
            "zero-variance refusal out of a suite that looks fine"
        )

    def test_outcome_grader_missing_artifact_scores_zero_and_exits_zero(self, tmp_path: Path):
        # BOTH halves, because they are separate failures. `_score_from_stdout` checks the exit
        # code BEFORE it parses line 1 and returns early, so a non-zero exit discards whatever the
        # grader computed — the score becomes 0.0 no matter what the first line said.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha"]}}}
        score, output, code = self._grade(tmp_path, spec, None)
        assert score == 0.0
        assert code == 0, "a non-zero exit discards the score line before it is ever parsed"
        assert "artifact not found" in output, output

    def test_outcome_grader_missing_expectations_file_scores_zero_and_exits_zero(self, tmp_path: Path):
        import shutil
        import subprocess
        import sys

        grader_dir = tmp_path / "grader"
        grader_dir.mkdir()
        shutil.copy(self.GRADER, grader_dir / "verify.py")
        (grader_dir / "expectations").mkdir()
        completed = subprocess.run(
            [sys.executable, str(grader_dir / "verify.py"), "no-such-row"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0
        assert float(completed.stdout.splitlines()[0]) == 0.0
        assert "no expectations file" in completed.stdout

    def test_outcome_grader_all_na_does_not_divide_by_zero(self, tmp_path: Path):
        # 0/0 is neither a perfect row nor a failed one — it is a row that measured NOTHING.
        # Printing 1.0 here would read as perfect; raising would take the whole run down. It
        # prints 0.0 and says so, which is what makes `/coder-eval:task`'s discrimination gate
        # catch such a row for free: the known-GOOD artifact scores 0.0 on it.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": []}, "json_field": {}}}
        score, output, code = self._grade(tmp_path, spec, "# Report\n\nanything\n")
        assert code == 0
        assert score == 0.0
        assert "0/0 applicable" in output, output

    def test_outcome_grader_unknown_check_excluded_from_denominator(self, tmp_path: Path):
        # A typo'd check name must not silently inflate the score by counting as a pass, and must
        # not fail a row for a check the author never wrote. It is skipped, loudly.
        spec = {
            "path": "out/report.md",
            "checks": {"mentions": {"all_of": ["alpha"]}, "menshuns": {"all_of": ["never present"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "# Report\n\nalpha\n")
        assert code == 0
        assert score == 1.0
        assert "SKIP unknown check" in output and "1/1 applicable" in output, output

    def test_outcome_grader_a_raising_check_fails_that_check_only(self, tmp_path: Path):
        # One bad check must not crash the grader or zero an otherwise-scored row. `mentions`
        # raises on a non-list `all_of`, which is the cheapest real instance.
        spec = {
            "path": "out/report.md",
            "checks": {"mentions#ok": {"all_of": ["alpha"]}, "mentions#broken": {"all_of": 5}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert score == 0.5, output
        assert "raised" in output, output

    # --- rule attribution: the RULES line `optimize.load.rule_row_map` reads -------------------

    @staticmethod
    def _rules_line(output: str) -> dict[str, str]:
        """The grader's rule attribution, parsed from the LAST `RULES ` line of its stdout.

        Scanned from the END for the prefix rather than taken as `splitlines()[-1]` blindly: that
        is exactly what a consumer does, because `run_command` wraps the grader's stdout in a
        details block that appends a `Stderr:` section after it.
        """
        import json

        line = next(ln for ln in reversed(output.splitlines()) if ln.startswith("RULES "))
        return json.loads(line[len("RULES ") :])

    def test_outcome_grader_emits_rules_line(self, tmp_path: Path):
        # Present, valid JSON, and NOT on line 1 — where `score_from_stdout` reads the score.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#core": "R1", "mentions#other": "R2"},
            "checks": {"mentions#core": {"all_of": ["alpha"]}, "mentions#other": {"all_of": ["absent"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha only\n")
        assert code == 0 and score == 0.5, output
        assert self._rules_line(output) == {"R1": "pass", "R2": "fail"}, output

    def test_outcome_grader_rules_line_absent_attribution_is_empty_object(self, tmp_path: Path):
        # A grader with nothing to attribute emits `RULES {}` — it must never OMIT the line. A
        # consumer has to tell "this row attributed nothing" from "this grader predates the
        # contract", and a missing line is the only signal for the second.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha"]}}}
        _score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert self._rules_line(output) == {}, output

    def test_outcome_grader_rule_fails_if_any_check_fails(self, tmp_path: Path):
        # ANY-FAIL, ALL-NA — and the direction is load-bearing rather than a convention. Any-fail
        # counts the MOST rows as failing a rule, so the headroom estimate built on it is an UPPER
        # bound: a rule that cannot clear the noise floor even under the most generous attribution
        # is definitively unpromotable, which is the only claim the ceiling table makes. A
        # proportional or all-fail rule would make that claim unsound in the dangerous direction.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#a": "R1", "mentions#b": "R1", "mentions#c": "R2", "json_field": "R3"},
            "checks": {
                "mentions#a": {"all_of": ["alpha"]},
                "mentions#b": {"all_of": ["absent"]},
                "mentions#c": {"all_of": ["alpha"]},
                "json_field": {},
            },
        }
        _score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert self._rules_line(output) == {"R1": "fail", "R2": "pass", "R3": "na"}, output

    def test_outcome_grader_a_bare_check_name_attributes_every_label(self, tmp_path: Path):
        # One entry for a check declared three times, which is the shape an author writes by
        # default. Without the bare-name fallback the rule would silently attribute NO rows, and a
        # rule with no failing rows reads as "no headroom" — advice to stop, from a typo.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions": "R1"},
            "checks": {
                "mentions#a": {"all_of": ["alpha"]},
                "mentions#b": {"all_of": ["alpha"]},
                "mentions#c": {"all_of": ["absent"]},
            },
        }
        _score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0
        assert self._rules_line(output) == {"R1": "fail"}, output

    def test_outcome_grader_reports_a_rules_entry_matching_no_check(self, tmp_path: Path):
        # A renamed or mistyped check key leaves its rule looking untouched by this row. Named in
        # the details rather than dropped, and it must not cost the row its score — attribution is
        # an annotation, never a measurement.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#core": "R1", "menshuns#core": "R2"},
            "checks": {"mentions#core": {"all_of": ["alpha"]}},
        }
        score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0 and score == 1.0, output
        assert "matching no declared check" in output, output
        assert self._rules_line(output) == {"R1": "pass"}, output

    def test_outcome_grader_a_malformed_rules_block_costs_no_score(self, tmp_path: Path):
        # `"rules": [...]` — an annotation typo. It reports and moves on: a row that measured
        # correctly must not be zeroed because its bookkeeping was wrong.
        spec = {"path": "out/report.md", "rules": ["R1"], "checks": {"mentions": {"all_of": ["alpha"]}}}
        score, output, code = self._grade(tmp_path, spec, "alpha\n")
        assert code == 0 and score == 1.0, output
        assert "`rules` must be an object" in output, output
        assert self._rules_line(output) == {}, output

    @pytest.mark.parametrize(
        ("argv", "spec_text", "artifact", "why"),
        [
            ([], None, None, "wrong argv"),
            (["r1"], None, None, "no expectations file"),
            (["r1"], "{not json", None, "invalid JSON"),
            (["r1"], "[1, 2]", None, "spec is not an object"),
            (["r1"], '{"path": "out/report.md", "checks": [1]}', None, "checks is not an object"),
            (["r1"], '{"path": "out/report.md", "checks": {}}', None, "artifact missing"),
            (["r1"], '{"path": "out", "checks": {}}', "dir", "artifact is a directory"),
        ],
        ids=["argv", "no-spec", "bad-json", "spec-not-object", "checks-not-object", "no-artifact", "artifact-is-dir"],
    )
    def test_outcome_grader_emits_rules_on_every_early_exit(
        self, tmp_path: Path, argv: list[str], spec_text: str | None, artifact: str | None, why: str
    ):
        # EVERY exit path, including the ones that run no check at all. `_report` owns the line for
        # exactly this reason: a consumer that cannot distinguish a crashed grader from an old one
        # has no way to tell "attribution is unavailable" from "this rule failed nowhere".
        import shutil
        import subprocess
        import sys

        grader_dir = tmp_path / "grader"
        grader_dir.mkdir()
        shutil.copy(self.GRADER, grader_dir / "verify.py")
        (grader_dir / "expectations").mkdir()
        if spec_text is not None:
            (grader_dir / "expectations" / "r1.json").write_text(spec_text, encoding="utf-8")
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        if artifact == "dir":
            (sandbox / "out").mkdir()

        completed = subprocess.run(
            [sys.executable, str(grader_dir / "verify.py"), *argv],
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, (why, completed.stderr)
        assert float(completed.stdout.splitlines()[0]) == 0.0, (why, completed.stdout)
        assert self._rules_line(completed.stdout) == {}, (why, completed.stdout)

    def test_every_line_of_the_shipped_grader_is_reachable(self, tmp_path: Path):
        """No dead code in the one file users COPY — measured, not reviewed.

        **Why this exists rather than a review note.** The scaffold is 460-odd lines of executable
        Python that ships to users, and it sits outside coverage entirely: `pyproject.toml` sets
        ``source = ["src/coder_eval"]``, and every other test here drives it through `subprocess`,
        where nothing is measured. So a branch that can never fire is invisible — and one shipped:
        a `__pycache__` filter guarding a path the file list never yielded, whose own test passed
        with the filter deleted. Reproduced before writing this: an added
        ``if "this-can-never-match" in path.parts: continue`` passed all 1331 tests in this file
        and `test_optimize_gate.py`.

        It drives the scaffold IN-PROCESS rather than through `subprocess`, which is what makes the
        measurement possible at all, and covers every exit path the module docstring declares. That
        overlaps deliberately with the behaviour tests above: those assert what the grader SAYS,
        this asserts that every line of it can still be reached. Neither implies the other — a
        behaviour test passes over dead code, and this passes over wrong answers.

        **Boundary.** Line reachability, not branch reachability: a condition that is always true
        still executes its line. And the `if __name__` block is excluded, since an imported module
        never runs it — the `sys.exit` wrappers there are covered by the subprocess tests instead.
        """
        import contextlib
        import importlib.util
        import io
        import json
        import os
        import shutil

        import coverage

        grader = tmp_path / "outcome-grader"
        shutil.copytree(self.GRADER.parent, grader, ignore=shutil.ignore_patterns("__pycache__"))
        specs = grader / "expectations"
        # Every shape the dispatch loop and the spec reader can meet, so a line that only a
        # malformed input reaches still counts as live.
        (specs / "ok.json").write_text(
            json.dumps(
                {
                    "path": "out/report.md",
                    "rules": {"mentions#a": "R1", "mentions#gone": "R2", "bad": 7},
                    "checks": {
                        "mentions#a": {"all_of": ["alpha"]},
                        "mentions#b": {"all_of": ["absent"]},
                        "mentions#na": {"all_of": []},
                        "mentions#raises": {"all_of": "a string, not a list"},
                        "mentions#type": {"all_of": 5},
                        "json_field": {"field": "status", "equals": "ok"},
                        "json_field#absent": {"field": "nope"},
                        "json_field#none": {},
                        "json_field#bad": {"field": 7},
                        "unknown_check": {},
                        "mentions#params": "not an object",
                        "bad": {"all_of": ["x"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        (specs / "structured.json").write_text(
            json.dumps(
                {
                    "path": "out/data.json",
                    "checks": {
                        "json_field": {"field": "status", "equals": "ok"},
                        "json_field#missing": {"field": "nowhere-in-the-document"},
                        "json_field#present": {"field": "results"},
                        "chatty": {},
                        "weird": {},
                    },
                }
            ),
            encoding="utf-8",
        )
        (specs / "badrules.json").write_text(
            json.dumps({"path": "out/report.md", "rules": ["R1"], "checks": {"mentions": {"all_of": ["alpha"]}}}),
            encoding="utf-8",
        )
        (specs / "badruleid.json").write_text(
            json.dumps({"path": "out/report.md", "rules": {"mentions": 7}, "checks": {"mentions": {"all_of": ["a"]}}}),
            encoding="utf-8",
        )
        (specs / "unreadable.json").write_text(json.dumps({"path": "out/locked.md", "checks": {}}), encoding="utf-8")
        (specs / "notjson.json").write_text("{ not json", encoding="utf-8")
        (specs / "notobject.json").write_text("[1, 2]", encoding="utf-8")
        (specs / "badchecks.json").write_text(json.dumps({"path": "out/report.md", "checks": []}), encoding="utf-8")
        (specs / "nochecks.json").write_text(json.dumps({"path": "out/report.md"}), encoding="utf-8")
        (specs / "isdir.json").write_text(json.dumps({"path": "out", "checks": {}}), encoding="utf-8")

        sandbox = tmp_path / "sandbox"
        (sandbox / "out").mkdir(parents=True)
        (sandbox / "out" / "report.md").write_text("alpha and a chatty line", encoding="utf-8")
        # A LIST at the shallowest level, so the breadth-first walk descends through one.
        (sandbox / "out" / "data.json").write_text(
            json.dumps({"results": [{"status": "ok"}, {"status": "ok"}]}), encoding="utf-8"
        )
        (sandbox / "out" / "locked.md").write_text("unreadable", encoding="utf-8")

        target = str(grader / "verify.py")
        # `config_file=False` and `source=`, not `include=`: this repo's `[tool.coverage.run]` sets
        # `source = ["src/coder_eval"]`, which SILENTLY overrides an include and measures nothing
        # here — the same fail-open shape as the dead filter this test exists to catch.
        measured = coverage.Coverage(data_file=str(tmp_path / ".coverage"), source=[str(grader)], config_file=False)
        measured.start()
        try:
            spec = importlib.util.spec_from_file_location("shipped_grader_under_test", target)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # The dispatch loop's defensive paths are about checks an AUTHOR adds — the file says
            # "REPLACE / EXTEND: the checks your rows actually need" — so exercising them means
            # registering some. With only the two shipped checks they are unreachable, which is a
            # fact about the sample vocabulary, not about the dispatch.
            module.CHECKS["chatty"] = lambda _doc, _params: (print("a check that talks"), (True, "ok"))[1]
            module.CHECKS["weird"] = lambda _doc, _params: (1, "not a bool")

            previous = Path.cwd()
            os.chdir(sandbox)
            try:
                for argv in (
                    ["verify.py"],  # wrong argv
                    ["verify.py", "--fingerprint"],
                    ["verify.py", "ok"],
                    ["verify.py", "structured"],
                    ["verify.py", "missing-row"],
                    ["verify.py", "notjson"],
                    ["verify.py", "notobject"],
                    ["verify.py", "badchecks"],
                    ["verify.py", "nochecks"],
                    ["verify.py", "isdir"],
                    ["verify.py", "badrules"],
                    ["verify.py", "badruleid"],
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        module.main(argv)
                # A scalar artifact: `Artifact.data` stays None where the JSON is not a container.
                (sandbox / "out" / "data.json").write_text("7", encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    module.main(["verify.py", "structured"])

                # An artifact that exists and cannot be READ — the one `OSError` path.
                locked = sandbox / "out" / "locked.md"
                locked.chmod(0o000)
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        module.main(["verify.py", "unreadable"])
                finally:
                    locked.chmod(0o644)
            finally:
                os.chdir(previous)
        finally:
            measured.stop()

        analysis = measured._analyze(target)
        # The `if __name__` block never runs in an imported module; the subprocess tests above are
        # what cover it, and every one of them asserts the exit code it produces.
        source_lines = (grader / "verify.py").read_text(encoding="utf-8").splitlines()
        guard_at = next(i for i, line in enumerate(source_lines, 1) if line.startswith("if __name__"))
        dead = sorted(line for line in analysis.missing if line < guard_at)
        assert not dead, (
            "the shipped grader has lines no input can reach: "
            + ", ".join(f"{line}: {source_lines[line - 1].strip()!r}" for line in dead)
            + ". Dead code in the file users COPY is invisible to every other test here — it runs "
            'through subprocess, and the scaffold is outside `source = ["src/coder_eval"]`. A '
            "`__pycache__` filter guarding a path the file list never yielded shipped exactly this "
            "way, with a test that passed once the filter was deleted."
        )

    def test_outcome_grader_prints_the_protocol_from_exactly_one_place(self):
        # The structural half of "emitted on every exit path", which the parametrized test above
        # can only sample. The top-level `except` is unreachable from a fixture, so what actually
        # guarantees it is that `_report` is the ONE writer of both the score line and the RULES
        # line — a second `print` of either is a new exit path that can drift from the contract.
        source = self.GRADER.read_text(encoding="utf-8")
        assert source.count('print(f"{score:.4f}")') == 1, "the score line is printed from more than one place"
        assert source.count('print("RULES "') == 1, "the RULES line is printed from more than one place"
        assert source.count("def _report(") == 1

    def test_outcome_grader_rules_line_is_last_and_exactly_formatted(self, tmp_path: Path):
        # The exact bytes, because the line is machine-read: sorted keys and compact separators are
        # what let a consumer (and this test) assert a string rather than re-parse to compare.
        spec = {
            "path": "out/report.md",
            "rules": {"mentions#z": "R9", "mentions#a": "R1"},
            "checks": {"mentions#z": {"all_of": ["alpha"]}, "mentions#a": {"all_of": ["alpha"]}},
        }
        _score, output, _code = self._grade(tmp_path, spec, "alpha\n")
        assert output.splitlines()[-1] == 'RULES {"R1":"pass","R9":"pass"}', output

    def test_outcome_grader_discriminates_a_good_artifact_from_a_bad_one(self, tmp_path: Path):
        # The instrument's whole purpose, asserted as a SEPARATION rather than as two scores: a
        # grader that scores a compliant and a violating artifact alike measures nothing, and
        # every number after it is decoration. This is the test-shaped twin of the discrimination
        # gate `/coder-eval:task` step 6.5 requires an author to perform by hand.
        spec = {"path": "out/report.md", "checks": {"mentions": {"all_of": ["alpha", "beta"]}}}
        good, _, _ = self._grade(tmp_path / "good", spec, "# Report\n\nAlpha and BETA.\n")
        bad, _, _ = self._grade(tmp_path / "bad", spec, "# Report\n\nneither one.\n")
        assert good - bad == 1.0, f"separation margin is {good - bad}, not 1.0"

    def test_outcome_grader_is_stdlib_only(self):
        # The sandbox venv installs only what `sandbox.python.env_packages` names, and that list is
        # sized for the AGENT's needs. A third-party import here fails at grading time, on every
        # row, after the whole arm has been paid for.
        import ast
        import sys

        tree = ast.parse(self.GRADER.read_text(encoding="utf-8"))
        imported: set[str] = set()
        unresolved: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                # Through the ONE resolver (CE051) rather than reading `node.module`. Every import
                # in a standalone script is absolute, so the resolver just passes it through — but
                # a scan that is right only for the input it was written against is precisely how
                # `resolved_module` came to exist.
                module = resolved_module(node, str(self.GRADER))
                if module is None:
                    unresolved.append(ast.unparse(node))
                else:
                    imported.add(module.split(".")[0])
        assert imported, "no imports found — the scan is looking at the wrong file"
        assert not unresolved, (
            f"the shipped grader uses a relative import ({unresolved}), which cannot resolve: it is "
            "copied to a path of the user's choosing and run as a standalone script"
        )
        outsiders = sorted(name for name in imported if name not in sys.stdlib_module_names)
        assert not outsiders, f"the shipped grader imports non-stdlib modules: {outsiders}"

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

    def test_outcome_grader_lives_outside_any_mounted_fixture(self):
        """The answer key must not ship inside the exam.

        Everything under a mounted `template_dir` is copied into EVERY sandbox, so a grader's
        expectations placed there hand the agent exactly what it is being marked against — and the
        run still looks completely normal. `outcome.yaml`'s `sandbox:` comment carries the measured
        cost of that accident; this is the assertion that keeps the shipped layout honest.

        **Why this is not a numbered CE rule.** A tree-walking rule would have exactly one
        discoverable subject today, and that subject's mounted fixture is a placeholder directory —
        so the rule would pass whether or not it worked, which is the vacuous-pass failure CE044 and
        CE045 exist to prevent. It is reserved as CE056 in `.claude/harness-candidates.md`, to be
        promoted when a second outcome suite with a `run_command` grader appears. Scoped to
        `outcome.yaml` by construction, deliberately: this is not a repo-wide scan, and
        `tasks/skills/ci-outcome.yaml` (no `run_command` at all) is not a grader suite.
        """
        from coder_eval.models import RepoSource, RunCommandCriterion, StarterFilesSource, TemplateDirSource
        from coder_eval.orchestration.task_loader import load_task

        task, _ = load_task(self.TEMPLATES / "outcome.yaml")
        # EVERY member of the `TemplateSource` union is decided here, and the `else` is why: a
        # fourth source type must FAIL this test rather than fall through, or the mount set
        # silently shrinks on exactly the change that should widen it.
        mounts: list[Path] = []
        for source in task.sandbox.template_sources or []:
            if isinstance(source, TemplateDirSource):
                mounts.append(Path(source.path).resolve())
            elif isinstance(source, StarterFilesSource):
                # Inline content: each `StarterFile.path` is a sandbox DESTINATION, so this source
                # mounts no host directory and nothing of ours can be under it.
                continue
            elif isinstance(source, RepoSource):
                continue  # a remote clone: no local path exists until it is fetched
            else:
                pytest.fail(
                    f"GAP: {type(source).__name__} is a template source this assertion does not "
                    "classify. If it copies a host directory into the sandbox, its path must join "
                    "`mounts`; if it does not, say so — silence here reads as 'checked'"
                )
        assert mounts, (
            "GAP: no mounted directory was discovered on outcome.yaml, so this assertion proves "
            "nothing. Either the fixture mount was removed or `template_sources` was renamed — "
            "either way the layout is no longer being checked"
        )

        graders = [c for c in task.success_criteria if isinstance(c, RunCommandCriterion)]
        assert graders, "the outcome template ships no `run_command` grader to locate"
        # The path the template TELLS an author to use, and the scaffold this repo actually ships.
        # Both matter: the first is what a user copies, the second is what they copy it from.
        # `$TASK_DIR` is the suite YAML's own directory, exported into every `run_command` — the
        # portable spelling, expanded here the way the sandbox's shell would.
        declared = [
            Path(part.replace("$TASK_DIR", str(self.TEMPLATES)))
            for c in graders
            for part in c.command.split()
            if part.endswith(".py")
        ]
        assert declared, f"no .py path found in the grader command {graders[0].command!r}"
        bundled = self.GRADER.resolve()
        for candidate in [*declared, bundled, bundled.parent / "expectations"]:
            resolved = candidate.resolve() if candidate.is_absolute() else (self.TEMPLATES / candidate).resolve()
            for mount in mounts:
                # The PATH RELATIONSHIP, asserted whether or not the mount exists on disk — skipping
                # a missing directory is how this becomes the vacuous pass it exists to avoid.
                assert mount != resolved and mount not in resolved.parents, (
                    f"{resolved} sits under the mounted fixture {mount}, so it is copied into every "
                    "sandbox. A grader's expectations there tell the agent what it is being marked "
                    "against, every arm's score rises, and no cross-arm comparison can reveal it"
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
        # with the runtime's. CE035 exists precisely to have one.
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

    def test_bundled_plugin_root_references_resolve(self):
        # Skills point at their own bundled files with `${CLAUDE_PLUGIN_ROOT}/...`, which is
        # resolved by Claude Code at runtime and by nothing at authoring time. A pointer to a
        # file that does not exist is therefore invisible until a user follows it — and the
        # skills' whole value is handing over an artifact rather than describing one.
        # Caught for real: optimize-skill shipped a pointer at reference/templates/outcome.yaml
        # one commit before that file existed, past 344 green lint tests.
        import re

        broken: list[str] = []
        for doc in (p for p in PLUGIN_TEXT_FILES if p.suffix == ".md"):
            text = doc.read_text(encoding="utf-8")
            for match in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", text):
                target = match.group(1).rstrip(".")
                if not (PLUGIN_ROOT / target).exists():
                    broken.append(f"{doc.relative_to(PLUGIN_ROOT)} -> {target}")
        assert not broken, (
            f"these bundled files point at ${{CLAUDE_PLUGIN_ROOT}} paths that do not exist: "
            f"{sorted(set(broken))}. An installed plugin is copied without its parent directories, "
            "so the reference resolves to nothing and the skill hands the user a dead path."
        )

    def test_reachability_guidance_names_the_plugin_root_layout(self):
        # A local plugin path must be a PLUGIN ROOT — a directory holding `skills/`, so the
        # skill sits at `<path>/skills/<name>/SKILL.md`. A manifest is optional (the
        # namespace then defaults to the directory name). Pointing at a bare directory of
        # skill directories loads NOTHING, silently, and every positive row scores 0 —
        # indistinguishable from a skill that never triggers.
        #
        # The shipped guidance said the opposite (".claude/skills" for
        # ".claude/skills/my-skill/SKILL.md"), so every generated suite reported recall 0.0.
        # Caught only by running one for real. This pins the corrected form in all three
        # places that state it.
        surfaces = {
            "activation template": self.TEMPLATES / "activation.yaml",
            "check-skill": PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md",
            "optimize-skill": PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md",
            "ci": PLUGIN_ROOT / "skills" / "ci" / "SKILL.md",
            "docs/PLUGIN.md": self.REPO_ROOT / "docs" / "PLUGIN.md",
            "tutorial 07": self.REPO_ROOT / "docs" / "tutorials" / "07-plugin-in-claude-code.md",
            "tutorial 08": self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill-description.md",
            "tutorial 09": self.REPO_ROOT / "docs" / "tutorials" / "09-optimizing-a-skill-body.md",
        }
        for name, path in surfaces.items():
            text = path.read_text(encoding="utf-8")
            assert "plugin root" in text.lower(), (
                f"{name} no longer says the plugin `path` must be a PLUGIN ROOT — a bare "
                "directory of skill directories loads nothing and scores recall 0.0"
            )
            # The required layout, spelled out. `skills/` alone would NOT do: the pre-fix
            # (wrong) text contained it too, via `.claude/skills/my-skill/SKILL.md`, so an
            # assertion on the bare token passes on exactly the guidance it must exclude.
            assert "skills/<name>/SKILL.md" in text or "skills/<skill-name>/SKILL.md" in text, (
                f"{name} no longer shows the required `<path>/skills/<name>/SKILL.md` layout"
            )

        # No surface may hand out a SKILL_SOURCE_PATH ending in /skills, nor revive the
        # "directory containing the skill's directory" rule. Both are the pre-fix form, and
        # the ci skill's copy of it shipped straight into users' CI workflows.
        for name, path in surfaces.items():
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if "SKILL_SOURCE_PATH=" in line or "SKILL_SOURCE_PATH=" in line.replace('"', ""):
                    value = line.split("SKILL_SOURCE_PATH=", 1)[1].strip().strip('"').rstrip("`")
                    assert not value.endswith("/skills"), (
                        f"{name}:{line_no} assigns SKILL_SOURCE_PATH a path ending in /skills "
                        f"({value!r}) — that is one level too deep and loads NOTHING; the "
                        "plugin root is its parent"
                    )
            assert "directory *containing* the skill" not in text, (
                f"{name} revived the pre-fix rule 'the directory containing the skill's "
                "directory' — a bare directory of skill directories loads nothing"
            )

        # The user-facing surfaces must name the trap by example, since the wrong form is
        # the intuitive one.
        for name in ("activation template", "check-skill", "ci", "docs/PLUGIN.md", "tutorial 07", "tutorial 08"):
            text = surfaces[name].read_text(encoding="utf-8")
            assert "`.claude/skills`" in text and ".claude" in text, (
                f"{name} no longer contrasts `.claude` against `.claude/skills` — the wrong "
                "path is the intuitive one, so naming it is the whole point"
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
        # with the runtime's. CE035 exists precisely to have one.
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

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_skill_md_frontmatter_is_valid(self, skill: Path):
        # `claude plugin validate --strict` does NOT check skill frontmatter, so this
        # test is the only guard against a typo'd key silently disabling a skill.
        # This set is the PLUGIN's house style, not the specification's limit — the spec
        # defines many more keys (`model`, `effort`, `context`, `agent`, `hooks`, …).
        # Keeping it narrow is deliberate: an unexplained `context: fork` or `model:` on a
        # shipped skill is exactly what should surface for review rather than pass silently.
        supported = {"description", "when_to_use", "disable-model-invocation", "allowed-tools", "disallowed-tools"}
        meta = _skill_frontmatter(skill)

        unknown = set(meta) - supported
        assert not unknown, (
            f"{skill}: frontmatter key(s) {sorted(unknown)} are outside the set this plugin "
            f"deliberately restricts itself to ({sorted(supported)}). If one is genuinely needed, "
            "add it here with a reason rather than working around it."
        )
        assert isinstance(meta.get("description"), str) and meta["description"].strip(), (
            f"{skill}: `description` must be a non-empty string — it is what the model matches on"
        )
        tools = meta.get("allowed-tools")
        if tools is not None:
            assert isinstance(tools, list), f"{skill}: `allowed-tools` must be a YAML array of bare tool names"
            for tool in tools:
                assert isinstance(tool, str) and "(" not in tool, (
                    f"{skill}: `allowed-tools` entry {tool!r} uses the scoped form from .claude/commands; "
                    "the plugin spec takes bare names like 'Bash'"
                )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_cli_driving_skills_preflight_the_version_check(self, skill: Path):
        # Both READMEs tell users that a CLI-driving skill checks `coder-eval --version`
        # and stops with an install hint. Nothing verified that, and `task` did not do it
        # while invoking the CLI twice — so a user without the CLI wrote N task files and
        # then got a bare `command not found`. Declared as a set so a skill that STOPS
        # driving the CLI has to be removed deliberately.
        has_check = "coder-eval --version" in skill.read_text(encoding="utf-8")
        if skill.parent.name in SKILLS_REQUIRING_THE_CLI:
            assert has_check, (
                f"{skill} shells out to the coder-eval CLI but never preflights "
                "`coder-eval --version` — the user learns it is missing only mid-flow"
            )
        else:
            assert not has_check, (
                f"{skill} preflights `coder-eval --version` but is not in "
                "SKILLS_REQUIRING_THE_CLI — add it there, or drop the check"
            )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_model_invocation_flags_match_the_design(self, skill: Path):
        name = skill.parent.name
        assert name in SKILL_DISABLE_MODEL_INVOCATION, (
            f"{name} is a new skill — declare whether it is explicit-invocation only in SKILL_DISABLE_MODEL_INVOCATION"
        )
        meta = _skill_frontmatter(skill)
        expected = SKILL_DISABLE_MODEL_INVOCATION[name]
        assert meta.get("disable-model-invocation", False) is expected, (
            f"{skill}: expected disable-model-invocation {expected}, got {meta.get('disable-model-invocation')!r}"
        )

    def test_every_declared_skill_ships(self):
        # The other side of SKILL_DISABLE_MODEL_INVOCATION: the per-skill tests are
        # parametrized over what is on disk, so without this a deleted skill would
        # pass everything silently.
        on_disk = {p.parent.name for p in PLUGIN_SKILLS}
        missing = sorted(set(SKILL_DISABLE_MODEL_INVOCATION) - on_disk)
        assert not missing, f"declared skill(s) with no skills/<name>/SKILL.md on disk: {missing}"

    def test_bundled_run_layout_matches_the_shared_source(self):
        # The one hand-copied file in the plugin: reference/run-layout.md mirrors
        # .claude/shared/run-layout.md verbatim, minus the pointer comment the
        # original carries on its first line. Generating one hand-written file
        # from another would add machinery without adding a source of truth, so
        # this byte-equality assert is the sensor instead.
        shared = self.REPO_ROOT / ".claude" / "shared" / "run-layout.md"
        bundled = PLUGIN_ROOT / "reference" / "run-layout.md"
        pointer, _, body = shared.read_text(encoding="utf-8").partition("\n")
        assert "plugins/coder-eval/reference/run-layout.md" in pointer, (
            f"{shared} must open with the pointer comment naming its mirror, so an editor of one "
            "file learns about the other"
        )
        assert body == bundled.read_text(encoding="utf-8"), (
            f"{bundled} drifted from {shared} — they are a verbatim mirror; copy the shared file "
            "over the bundled one (keeping the pointer comment only in the shared original)"
        )

    @pytest.mark.parametrize(
        "skill", PLUGIN_TEXT_FILES, ids=[str(p.relative_to(PLUGIN_ROOT)) for p in PLUGIN_TEXT_FILES]
    )
    def test_bundled_files_reference_no_repo_paths(self, skill: Path):
        text = skill.read_text(encoding="utf-8")
        offenders = [token for token in REPO_PATH_TOKENS if token in text]
        assert not offenders, (
            f"{skill} names this repository's path(s) {offenders} — an installed plugin is copied "
            "without its parent directories, so they do not exist at runtime. Bundle what the skill "
            "needs under plugins/coder-eval/ and address it via ${CLAUDE_PLUGIN_ROOT}."
        )

    def test_lint_tasks_skill_is_read_only(self):
        # Assert BOTH keys, because neither alone carries the contract: `allowed-tools` names
        # the tools this skill expects to use, `disallowed-tools` removes the write tools from
        # the pool. Assert only the allowlist and a denylist regression passes; assert only the
        # denylist and a widened allowlist (say `Bash`) passes.
        #
        # Neither key is the real guarantee, which is why the skill body carries a STANDING
        # prohibition too: per the skills spec, `disallowed-tools` "clears when you send your
        # next message", and this skill's step 1 deliberately asks the user one before linting a
        # whole directory. So the frontmatter covers the first turn and the prose covers the
        # rest — `test_lint_tasks_read_only_rule_survives_the_next_turn` guards that half.
        meta = _skill_frontmatter(PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md")

        # `and allowed` first: an ABSENT allowed-tools is the weakest state, not the
        # strongest, and an empty set would satisfy the subset check vacuously.
        allowed = set(meta.get("allowed-tools") or [])
        assert allowed and allowed <= {"Read", "Glob", "Grep"}, (
            f"lint-tasks pre-approves {sorted(allowed - {'Read', 'Glob', 'Grep'})} — an allowlist, "
            "not a denylist, so anything beyond reading breaks the advisory contract"
        )
        assert {"Write", "Edit", "NotebookEdit"} <= set(meta.get("disallowed-tools") or []), (
            "lint-tasks must name every write tool in `disallowed-tools` — that is the half "
            "that actually removes them from the pool"
        )

    def test_lint_tasks_read_only_rule_survives_the_next_turn(self):
        # The frontmatter deny is turn-scoped ("clears when you send your next message"), and
        # step 1 asks the user a question before linting a directory — so for most of a real
        # review the prose rule is the only thing enforcing read-only. Deleting it would leave
        # a skill that advertises "Read-only." in its description with nothing behind it after
        # the first reply.
        text = _normalized(PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md")
        assert "Never modify a file" in text, "lint-tasks lost its standing read-only prohibition"
        assert "standing, not per-turn" in text, (
            "lint-tasks no longer says its read-only rule outlives the frontmatter deny — the "
            "deny clears on the user's next message, which step 1 explicitly solicits"
        )

    def test_lint_tasks_does_not_flag_the_shipped_activation_template(self):
        # A prose sensor, guarding against deletion rather than judging quality — but it
        # covers the one interaction where two shipped skills could contradict each other:
        # the activation suite `check-skill` writes is exactly the shape a naive coverage
        # pass reads as "one criterion, no content check" and flags. The worked example is
        # in the repo (reference/templates/activation.yaml), so the carve-out cannot be
        # written vaguely: it must be structural, since the file may be renamed.

        text = (PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md").read_text(encoding="utf-8")
        for token in ("dataset:", "skill_triggered", "classification_match", "suite_thresholds"):
            assert token in text, (
                f"lint-tasks must name {token!r} in its activation-suite carve-out — without the "
                "structural detection it will flag the suites check-skill generates as broken"
            )
        assert "do not apply" in text, (
            "lint-tasks names the carve-out's conditions but no longer EXEMPTS anything — an "
            "inverted carve-out would keep every token above and still flag activation suites"
        )
        # The conditions must still describe the template check-skill actually copies, or the
        # carve-out has quietly stopped covering the one file it exists for.
        template = yaml.safe_load((self.TEMPLATES / "activation.yaml").read_text(encoding="utf-8"))
        assert template.get("dataset"), "the shipped activation template lost its `dataset:` block"
        types = {c.get("type") for c in template["success_criteria"]}
        assert types & {"skill_triggered", "classification_match"}, (
            f"the shipped activation template's criteria are {sorted(types)} — no longer "
            "classification-style, so lint-tasks' structural carve-out would not match it"
        )
        assert any(c.get("suite_thresholds") for c in template["success_criteria"]), (
            "the shipped activation template lost `suite_thresholds` — the carve-out's third "
            "condition no longer holds, so lint-tasks would flag the suite check-skill writes"
        )

    def test_optimize_skill_keeps_its_load_bearing_instructions(self):
        # optimize-skill's correctness lives entirely in its prose: it drives paid runs and
        # every one of these instructions is something a well-meaning edit would "simplify"
        # away, leaving a skill that still reads plausibly and measures nothing. Same
        # deletion-sensor shape as the lint-tasks read-only guard above.
        # TWO surfaces, and which one a token belongs to is the reviewable part of the
        # split. `SKILL.md` holds the PROCEDURE — which suite, which files, which commands,
        # in what order — so a procedure token asserted anywhere else would mean the step
        # that needs it no longer states it. `reference/optimize-method.md` holds the
        # track-invariant METHOD — the cost table, what each stage bounds, why the two gates
        # differ, the sign rule — which is identical on both tracks and is what has to be
        # right for a verdict to mean anything. Tokens that may legitimately live in either
        # are asserted against the concatenation.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        text = skill + " " + method

        # The single most important invariant. Suite rollups pool replicates, so --repeats
        # writes ONE pooled suite.json and the per-replicate F1 the Stage B gate reads would
        # not exist. Collapsing three invocations into --repeats 3 is the likeliest edit,
        # and it fails silently — the gate would compare a number against itself.
        assert "not** `--repeats 3`" in text or "**not** `--repeats`" in text, (
            "optimize-skill no longer says Stage B must be THREE SEPARATE INVOCATIONS rather "
            "than --repeats 3 — suite rollups pool replicates, so --repeats yields one pooled "
            "suite.json with no per-replicate F1 for the gate to read"
        )
        assert "keyed on `(variant, suite)`" in text, (
            "optimize-skill states the not---repeats rule but no longer says WHY (the rollup "
            "grouping key) — a rule with no reason attached is the first thing an editor drops"
        )

        for token, why in (
            ("--split train", "the train split drives proposals and the gate"),
            ("--split test", "the test split is what makes a promotion more than a fit"),
            ("--split test --repeats 3", "Stage C's paired comparison needs replicates averaged per row"),
            ("stop_early", "an armed suite can pass-stop before a sibling misfire is observable"),
            ("agent.plugins", "reachability wiring — the mechanism, not an invented one"),
            ("recall 0.0", "the silent failure mode of a wrong plugin path"),
            ("disable-model-invocation", "the hard stop on a skill whose description never triggers"),
            ('metrics["f1.yes"]', "F1 is read from the rollup, never recomputed"),
            ("process working directory", "plugin paths resolve against the process cwd, not the task file's dir"),
            # Annexation shows up as a sibling FALSE NEGATIVE, so it moves recall, not
            # precision — on a suite where the sibling never misfires, precision.yes is
            # pinned at 1.0 and a precision gate would be gating on a constant.
            ("**`recall.yes`**", "the sibling-regression gate must read recall, not precision"),
            # Step 7's snapshot must carry the siblings: a variant's plugins block REPLACES
            # the task's, so the round-slug dir is the arm's only skill source. Snapshot one
            # skill and every sibling criterion silently observes `no` in every arm.
            ("copied unchanged", "each arm's snapshot must include the sibling skills, not just the target"),
            # The execution track measures the BODY, and an activation suite cannot grade a
            # body — skill_triggered scores engagement only. Losing this collapses the two
            # tracks onto one instrument that answers the wrong question.
            ("wrong instrument", "the execution track must refuse to reuse an activation suite"),
            # Inverse of the activation rule, and easy to "correct" into a bug: the body
            # track invokes the skill from the prompt to hold activation constant, so what
            # varies is the body alone.
            ("Invoke the skill from the prompt", "the execution track holds activation constant by invoking the skill"),
            # And it must be the SLASH form. Verified live: a `disable-model-invocation`
            # skill is not offered to the model at all, so asking in prose returns "no such
            # skill available" and the row measures nothing — while `/plugin:skill` in the
            # prompt loads it, emits a real Skill tool call, and is detected by
            # skill_triggered. Prose-instead-of-slash is the intuitive edit and it is silent.
            ("Use the slash form", "prose cannot reach a disable-model-invocation skill; only the slash form loads it"),
            # One variable per round. A body edit shipped alongside a description edit is
            # unattributable, and the description change also moves activation.
            ("Never run both tracks in one round", "tracks must not be combined in a single measurement"),
            # A skill that cannot be model-invoked still has an optimizable body — routing
            # to the execution track rather than stopping is the point of the two-track split.
            ("route to the execution track", "disable-model-invocation must route to the body track, not hard-stop"),
            # The whole execution track is predicated on this. suite.json is written only for
            # tasks the dataset expander touched (rollups group on suite_id, which nothing
            # else sets) and --split filters dataset ROWS — so a directory of separate task
            # files gives Stage A no rollup to rank and makes Stage C's --split test silently
            # re-run the train rows. "One task per scenario" is the intuitive shape and it is
            # the broken one.
            (
                "ONE dataset-backed task",
                "the outcome suite must be one dataset-backed task or Stage A has no rollup and Stage C re-runs train",
            ),
            (
                "silently re-runs the train rows",
                "the consequence that makes the dataset requirement non-optional, not a preference",
            ),
            # expand_dataset copies the SAME success_criteria onto every row, substituting
            # ${row.*} into every string leaf. Per-scenario assertions are therefore
            # parameterized by row fields; writing different criteria per scenario is
            # unrepresentable, and discovering that after authoring the rows is expensive.
            (
                "Criteria are copied to every row",
                "per-scenario assertions must be parameterized by row fields, never written per scenario",
            ),
            # Substitution reaches initial_prompt and success_criteria ONLY, never
            # sandbox.template_sources — so a suite has exactly one fixture and scenario
            # variation has to live in the prompt.
            (
                "Every row shares ONE sandbox fixture",
                "row substitution never reaches sandbox:, so rows needing different repo shapes are two suites",
            ),
            (
                "preconditions the skill under test checks",
                "a fixture that fails the skill's own hard stop ties every arm at zero and reads as bad candidates",
            ),
            # There is no --variant flag, so the arm set changes by AUTHORING A NEW FILE.
            # Re-passing the triage file at Stage B/C costs (N+1)/2x the budgeted runs and
            # renders no paired block at all, with nothing in the output announcing it.
            (
                "no `--variant` filter",
                "the arm set can only be changed by authoring a per-stage experiment file",
            ),
            (
                "no `## Paired Comparison` block at all",
                "the cost of re-passing the triage file at Stage B/C — more spend, strictly less evidence",
            ),
            (
                "round<N>-triage.yaml",
                "Stage A's own experiment file — the per-stage naming Steps 9/10 and the ledger depend on",
            ),
            (
                "round<N>-gate.yaml",
                "Stage B's own experiment file, which on the execution track must hold exactly two variants",
            ),
            (
                "round<N>-confirm.yaml",
                "Stage C's own experiment file — reusing the gate or triage file is the documented mistake",
            ),
            (
                "because that block fires only for exactly two variants",
                "the REASON the per-stage files matter: paired_comparison's exactly-two precondition",
            ),
            # The P1-5 mitigation: the skill supplies the artifact rather than relaying
            # requirements to /coder-eval:task, which come back half-applied.
            (
                "reference/templates/outcome.yaml",
                "the execution track must hand over the bundled outcome template, not point vaguely at one",
            ),
            (
                "Hand over the template itself",
                "relayed requirements come back half-applied and the user pays for a second round trip",
            ),
            # Step 5: the common path is that the suite is ALREADY labelled, so the
            # unlabelled branches are the exception rather than the expected work.
            (
                "arrives labelled",
                "a check-skill suite already carries splits — the labelling branches are for hand-authored ones",
            ),
            # A body naming a tool outside the allowlist fails identically in every arm and
            # reads as "the body is bad". The activation track cannot have this confound.
            (
                "tool policy constant across arms",
                "an unpinned tool policy makes a body that names a disallowed tool look like a bad body",
            ),
            # Per-row cost brake. Without it a runaway row eats a whole stage's budget.
            ("run_limits.max_usd", "the per-row cost brake on an outcome suite, where every row is a full task run"),
            # The inverse of the activation guidance. An activation suite caps max_turns at 2
            # deliberately; carried over, an outcome row is truncated and scores as a body
            # failure — a fabricated result, since the body was never allowed to finish.
            # Anchored on the plain-prose consequence rather than the emphasized phrase: a
            # sensor that depends on Markdown bold breaks on a rewrap without the
            # instruction having been touched, which trains readers to distrust it.
            (
                "which is a fabricated result",
                "carrying an activation suite's tight caps to an outcome suite fabricates body failures",
            ),
            # The activation gate's instrument. It replaced `min(candidate F1) > max(incumbent F1)`,
            # which discarded the pairing (both arms ran the SAME rows) and had very little power at
            # 8-12 rows per polarity. Resampling ROWS is the load-bearing word: replicates within a
            # row are the same request asked again, so resampling them individually would understate
            # the interval and manufacture separation.
            ("cluster bootstrap", "the activation gate's instrument — resampling rows, not observations"),
            ("excludes zero", "the promotion condition; a CI containing zero is a non-result"),
            (
                "minimum detectable effect",
                "Stage A must price the smallest resolvable difference before spending, or a stage that "
                "cannot see the effect returns a non-result indistinguishable from a real one",
            ),
            ("Holm", "the Stage A -> Stage B multiplicity correction"),
            (
                "reported diagnostic",
                "range non-overlap is retained as a diagnostic and must never be restored as the gate",
            ),
            # The cost guardrail, named for what it actually compares. A body edit that doubles what
            # a row costs for +0.02 F1 is a trade, not a win.
            ("median cost per row", "the cost guardrail — a body edit that doubles spend must not promote silently"),
            ("median latency per row", "the latency half of the same guardrail"),
            # The refusal rule. Without it the tool reports "not promoted" on a suite where no
            # candidate could ever promote — the same overclaim UNDECIDED exists to prevent, one
            # state further along.
            (
                "REFUSES rather than reporting a negative result",
                "the method file must state that a floor above the Holm threshold is a refusal, "
                "not an ordinary negative result",
            ),
            (
                "add rows",
                "a refusal's remedy is more rows or a smaller family — never another round on the "
                "same suite, which reproduces the floor exactly",
            ),
            # Two preflights, not one. The method file said the execution track gets none; it now
            # gets one on its own metric, from data the control stage already pays for.
            (
                "Two preflights",
                "the method file must describe BOTH tracks' noise floors — it previously said the "
                "execution track gets none, which is the claim this phase made false",
            ),
        ):
            assert token in text, f"optimize-skill lost {token!r} — {why}"

        # The now-false claim, asserted ABSENT. A sensor that only checks the replacement is
        # present would stay green if both sentences shipped side by side, which is worse than
        # either alone: the reader gets two contradictory instructions and no way to pick.
        assert "Not on the execution track" not in method, (
            "reference/optimize-method.md still says the execution track gets no preflight. It gets "
            "one — on weighted_score, after the control arm — and the two claims cannot both stand."
        )

        # Every PROCEDURE token, checked through the same matcher the self-test exercises.
        missing = _missing_tokens(skill, _SKILL_PROCEDURE_TOKENS)
        assert not missing, (
            "optimize-skill's SKILL.md lost:\n  "
            + "\n  ".join(missing)
            + "\n\nThese are PROCEDURE: they must stay in the skill, not move to "
            "reference/optimize-method.md"
        )
        # An extracted reference nothing points at is a deleted reference. Mirrors the
        # reference/task-rubric.md pointer sensor.
        assert "${CLAUDE_PLUGIN_ROOT}/reference/optimize-method.md" in skill, (
            "optimize-skill's SKILL.md no longer points at reference/optimize-method.md, so the "
            "cost table, the gate rules and the sign rule are unreachable from the procedure "
            "that has to apply them"
        )

        # The METHOD half, asserted against the method file alone — what a stage BOUNDS is
        # track-invariant and is the thing that has to be right for a verdict to mean anything.
        for token, why in (
            (
                "search, not a gate",
                "the search loop bounds nothing: its comparison is across invocations, unpaired "
                "and unbootstrapped, so a search win is a hypothesis to gate and never a promotion",
            ),
        ):
            assert token in method, (
                f"reference/optimize-method.md lost {token!r} — {why}. This is METHOD: it must stay "
                f"in the reference, not move to the skill"
            )

        # The paired-diff sign rule has to appear in BOTH gates that read the block —
        # Stage B (execution) and Stage C — because each is a separate decision point and a
        # reader lands on one or the other. Counted rather than `in text`: a presence check
        # stays green when one of the two is deleted, which is exactly the edit to catch.
        # The sign follows VARIANT DECLARATION ORDER (reports_stats builds vid_a/vid_b from
        # variant_ids[0]/[1]), so with `incumbent` declared first a candidate win is NEGATIVE
        # — and a reversed reading promotes the arm that lost, with every later number in the
        # ledger corroborating it.
        # Counted against the METHOD file, because both decision points moved there together
        # (Stage B and Stage C are adjacent sections of it). The reason for TWO is unchanged
        # by the move: each stage is a separate decision point a reader lands on
        # independently, so a cross-reference from one to the other is not good enough.
        for phrase, expected in (("variant declaration order", 2), ("a candidate win reads negative", 2)):
            assert method.count(phrase) >= expected, (
                f"reference/optimize-method.md states {phrase!r} {method.count(phrase)} time(s); both "
                f"the execution Stage B gate and Stage C must carry the paired-diff sign rule, so it "
                f"needs {expected}"
            )

        # The cost table's symbols must match the prose that defines them. It shipped
        # reading `M_tune`/`M_holdout` after the split values were renamed to train/test —
        # invisible to every sensor above, because the rename deleted no instruction, and
        # the table is the surface a reader budgets from.
        for stale in ("M_tune", "M_holdout"):
            assert stale not in text, (
                f"optimize-skill's cost table still uses {stale!r} — the splits are `train`/`test`, "
                "so the table names symbols the surrounding prose never defines"
            )
        for symbol in ("M_train", "M_test"):
            assert symbol in text, f"optimize-skill's cost table lost {symbol!r}"

        # The two gates use DIFFERENT machinery, on purpose, and the reason is subtle enough
        # that a well-meaning edit would unify them. Activation compares F1, which the pooled
        # suite.json cannot report per replicate -> three invocations. Execution compares
        # per-row weighted_score, which `paired_comparison` already computes correctly over
        # replicates -> `--repeats 3` on exactly two variants. Collapsing either into the
        # other silently swaps the instrument for one that cannot see the metric.
        assert "primary instrument" in text, (
            "optimize-skill no longer says the paired comparison is the PRIMARY instrument on "
            "the execution track — it is only corroboration on the activation track, and the "
            "difference is what makes each gate valid for its own metric"
        )
        assert "deliberate departure" in text, (
            "optimize-skill no longer flags that the execution gate departs from the "
            "activation gate on purpose — unlabelled, the two look like an inconsistency to "
            "be tidied away"
        )

        # Its sibling's real name. `/coder-eval:skill-check` is a dangling command, and three
        # steps plus several edge cases hand control back to check-skill.
        assert "/coder-eval:check-skill" in text, "optimize-skill lost its pointer to /coder-eval:check-skill"
        assert "skill-check" not in text.replace("check-skill", ""), (
            "optimize-skill names `skill-check` — that command does not exist; the sibling is `check-skill`"
        )

    def test_optimize_method_documents_successive_halving(self):
        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        text = method + " " + skill

        for token, why in (
            # Capitalized as both surfaces write it — these sensors match the file, not a
            # normalized form, so a rewording is visible rather than silently tolerated.
            ("Successive halving", "the two-pass Stage A that stops paying full price for doomed arms"),
            (
                "dataset.sample_seed",
                "an unpinned seed makes Stage B's three invocations draw three different row sets, "
                "and the gate then pairs almost nothing while reporting an interval anyway",
            ),
            (
                "across invocations",
                "the hazard is ACROSS invocations, not within one — within a single invocation every "
                "arm sees the same rows by construction, so a sensor that lost this would let the "
                "warning be re-described as a within-stage problem and guard the wrong thing",
            ),
        ):
            assert token in text, f"optimize-skill lost {token!r} — {why}"

        # The seed rule is PROCEDURE — it is a thing to check before running a stage — so it must
        # survive in SKILL.md specifically. Asserted against the pair above, deleting the Step 10
        # block would stay green on the method file's copy alone.
        for token in ("dataset.sample_seed", "across invocations"):
            assert token in skill, (
                f"optimize-skill's SKILL.md lost {token!r}. This is PROCEDURE: the agent has to act "
                f"on it before spending a stage, so it cannot live only in the method file."
            )

        # The halving row must be priced in the same symbols as every other row.
        assert "M_train/2" in method, (
            "reference/optimize-method.md's cost table lost the halved Stage A line, so a reader "
            "cannot see what halving actually saves before choosing it"
        )
        assert "by construction" in method, (
            "reference/optimize-method.md no longer says arms share rows BY CONSTRUCTION — without "
            "it a reader guards the seed against a risk that does not exist and misses the real one"
        )

    def test_optimize_skill_documents_the_no_skill_control(self):
        # The control answers the question every later round assumes the answer to. Losing it
        # means rounds of wording changes measured against a body that does nothing here.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")

        # Asserted per SURFACE, not against the concatenation: both files carry the control today,
        # so a pair-wide check would stay green if the whole Step 8 subsection were deleted — and
        # the procedure is the half an agent actually executes.
        for surface, name in ((skill, "SKILL.md"), (method, "reference/optimize-method.md")):
            for token, why in (
                ("control arm", "the arm that establishes the body does measurable work at all"),
                (
                    "body is emptied",
                    "removing the skill instead changes the LISTING, so the control would differ in "
                    "two ways at once and attribute neither — the intuitive simplification is wrong",
                ),
                (
                    "once per suite",
                    "the control is a property of the suite, not the round; re-running it every "
                    "round is pure spend, and the cost table prices it as a one-off",
                ),
            ):
                assert token in surface, f"optimize-skill's {name} lost {token!r} — {why}"

        # `round<N>-control.yaml` is named for the round, unlike every other per-stage file, and
        # that reads as "author one per round" unless the skill says otherwise. Asserted against
        # SKILL.md specifically, because Step 9's file list is where an agent decides what to write.
        assert "authored **once per suite**" in skill or "once per suite** rather than" in skill, (
            "optimize-skill's SKILL.md lists round<N>-control.yaml among the per-stage experiment "
            "files without saying it is authored ONCE PER SUITE. Its round-numbered name reads "
            "like its neighbours, which are per-round, so the distinction has to be explicit."
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

    def test_optimize_skill_points_at_the_proposal_prompt(self):
        # An extracted reference nothing points at is a deleted reference. Mirrors the
        # optimize-method.md and task-rubric.md pointer sensors.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        assert "${CLAUDE_PLUGIN_ROOT}/reference/proposal-prompt.md" in skill, (
            "optimize-skill's SKILL.md no longer points at reference/proposal-prompt.md, so the "
            "shape of the proposal — the trajectories, the anti-repetition history and the test "
            "blinding — is unreachable from the step that has to generate candidates"
        )

    def test_proposal_prompt_keeps_its_load_bearing_instructions(self):
        # This file exists to stop specific failures, each invisible in the output: a proposer that
        # paraphrases its last attempt, one that has seen the test split, one that fixes the row in
        # front of it rather than the category it belongs to, one that never opens the reference
        # solution the suite ships, and one whose edits are exhortations rather than techniques.
        text = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        missing = _missing_tokens(text, _PROPOSAL_TOKENS)
        assert not missing, "reference/proposal-prompt.md lost:\n  " + "\n  ".join(missing)

    @pytest.mark.parametrize(
        ("surface", "tokens"),
        [
            ("reference/proposal-prompt.md", _PROPOSAL_TOKENS),
            ("skills/optimize-skill/SKILL.md", _SKILL_PROCEDURE_TOKENS),
        ],
        ids=["proposal-prompt", "optimize-skill"],
    )
    def test_the_token_matcher_catches_a_removed_sentence(self, surface: str, tokens):
        """The self-test: the REAL matcher, run against text with a guarded sentence removed.

        The committed replacement for "delete it locally and check the test goes red" — which
        proves nothing about the sensor a month later. **Every** token is exercised, in both
        registries, so a pair that can never fail is caught here rather than shipping as
        decoration: that is the failure class this whole file exists to prevent, and a deletion
        sensor is the easiest place in the repo to introduce one.
        """
        text = _normalized(PLUGIN_ROOT / surface)
        assert _missing_tokens(text, tokens) == []

        for token, _why in tokens:
            gutted = text.replace(token, "")
            assert gutted != text, f"{token!r} is not actually present in {surface} — the sensor is decoration"
            missing = _missing_tokens(gutted, tokens)
            assert any(repr(token) in entry for entry in missing), (
                f"removing {token!r} from {surface} did not make the matcher report it"
            )

    def test_optimize_skill_snippet_names_the_public_gate_api(self):
        # A prose sensor cannot see a snippet drifting from the API it calls: the tokens stay
        # present, the skill stays readable, and the step fails at runtime in the user's terminal
        # after they have paid for three invocations. So assert both halves — the names are in the
        # skill AND they are importable from the module the skill tells the user to import.
        import importlib

        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")

        # Derived from the skill's own import lines rather than a list here, so a snippet that
        # starts importing something new is covered without anyone remembering to extend this.
        #
        # EVERY `coder_eval` module, not just `optimize.gate`: the snippets also import
        # `expand_dataset` / `load_task` from `coder_eval.orchestration.task_loader` and models
        # from `coder_eval.models`, and a rename there fails in the user's terminal after the runs
        # are paid for — which is precisely the failure this sensor's own comment describes.
        imported: dict[str, set[str]] = {}
        for module, block in re.findall(r"from (coder_eval[\w.]*) import \(([^)]*)\)", raw):
            imported.setdefault(module, set()).update(n.strip().rstrip(",") for n in block.split() if n.strip(" ,"))
        for module, line in re.findall(r"from (coder_eval[\w.]*) import ([^(\n]+)", raw):
            imported.setdefault(module, set()).update(n.strip() for n in line.split(",") if n.strip())

        # The DECISION layer is six modules since the split, and the snippets import from five of
        # them. Asserting the family rather than one name keeps this from going blind the next time
        # a name moves — which is exactly what it did when `optimize.gate` stopped exporting the
        # gates themselves.
        family = {m for m in imported if m.startswith("coder_eval.optimize.")}
        assert len(family) >= 4, (
            f"optimize-skill's SKILL.md imports from only {sorted(family)} — either the snippets "
            "are gone or the import line changed shape and this sensor is blind"
        )
        for module, names in sorted(imported.items()):
            loaded = importlib.import_module(module)
            for name in sorted(names):
                assert hasattr(loaded, name), (
                    f"optimize-skill's SKILL.md tells the user to import {name!r} from "
                    f"{module}, which no longer exports it — the snippet would fail at "
                    f"runtime, after the user has paid for the runs it was meant to read"
                )

        # The ones the procedure must NAME, even if a future snippet stops importing them inline.
        # This hardcoded list is the REAL guard: the derived half above only asserts that whatever
        # the skill imports exists, so deleting a whole snippet leaves it green on the others.
        for name in ("activation_gate", "holm_promote", "render_markdown", "candidate_leaks", "search_compare"):
            assert name in skill, f"optimize-skill's SKILL.md no longer names {name!r} in its gate snippet"

    def test_optimize_skill_snippets_parse_and_bind(self):
        """The third half: a snippet's CALLS must still bind against the real signatures.

        `test_optimize_skill_snippet_names_the_public_gate_api` asserts the imported names exist.
        A renamed or removed keyword ARGUMENT is invisible to it — the name resolves, the tokens
        are all present, and the snippet raises `TypeError` in the user's terminal after they have
        paid for three invocations.

        **Boundary**, so a green run is not mistaken for a proof:

        - KEYWORD arguments only. A snippet's positional values are placeholders (`run_dir`,
          `dirs`), and `bind_partial` inspects names rather than values, so nothing here executes
          or resolves a snippet local.
        - A callee taking `**kwargs` accepts anything, so the sensor is silent there by
          construction.
        - A name assigned anywhere in the same fence shadows the import, and is skipped — binding
          a local's call against the imported function's signature would be simply wrong.
        """
        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        failures = _snippet_binding_failures(raw)
        assert not failures, "\n  ".join(["optimize-skill's snippets no longer bind:", *failures])

    def test_the_snippet_binder_reads_the_real_snippets(self):
        """Anti-vacuity, on the REAL file: a binder that found no calls would also pass green.

        Mutating one keyword the shipped snippets actually pass must light up several fences.
        """
        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        mutated = raw.replace("suite_id=", "suite_i=")
        assert mutated != raw, "the anchor moved — re-derive it from the skill's snippets"

        failures = _snippet_binding_failures(mutated)
        assert len(failures) >= 5 and all("suite_i" in f for f in failures), failures

    def test_the_snippet_binder_catches_a_bogus_keyword(self):
        # The self-test. Without it the binder could be reverted to a no-op with everything green.
        markdown = (
            "```python\n"
            "from coder_eval.optimize.activation import activation_gate\n\n"
            "activation_gate(suite_id='s', bogus_kwarg=1)\n"
            "```\n"
        )
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "bogus_kwarg" in failures[0], failures

    def test_the_snippet_binder_catches_a_moved_name(self):
        """The hole the call loop leaves, hole 1: a moved name resolves to `None`.

        The call loop skips it via `if not callable(target): continue`, so a snippet importing a
        name that no longer exists reported NOTHING. This is the sensor the module split depends
        on — every one of the skill's imports is about to change module.
        """
        markdown = (
            "```python\n"
            "from coder_eval.optimize.activation import a_name_that_moved\n\n"
            "a_name_that_moved(suite_id='s')\n"
            "```\n"
        )
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1, failures
        assert "a_name_that_moved" in failures[0] and "has no attribute" in failures[0]

    def test_the_snippet_binder_catches_a_moved_name_that_is_never_called(self):
        """The hole the call loop leaves, hole 2, and it is independent of the first.

        The loop only visits names used as `ast.Call` funcs, so a name imported for a type
        annotation or a constant is invisible to it however broken. `CostQualityPoint`,
        `SearchComparison`, `TASK_JSON_GLOB`, `GATE_RESAMPLES` and `MATERIALITY_FLOOR` are all
        imported-not-called in the shipped skill, which is why the check runs over the import map.
        """
        markdown = "```python\nfrom coder_eval.optimize.activation import A_CONSTANT_THAT_MOVED\n```\n"
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "A_CONSTANT_THAT_MOVED" in failures[0], failures

    def test_the_snippet_binder_catches_a_nonexistent_module(self):
        # `import_module` RAISES here; reporting rather than propagating keeps one bad fence from
        # taking every other snippet's check down with it.
        markdown = "```python\nfrom coder_eval.nonexistent import thing\n```\n"
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "does not import" in failures[0], failures

    def test_a_nonexistent_module_whose_name_is_also_called_is_reported_not_raised(self):
        """The crash the call loop used to produce, and the reason this sensor exists at all.

        With a call present, the loop reached its own `import_module` — unguarded — and a mistyped
        module name raised `ModuleNotFoundError` straight out of the sensor instead of reporting a
        failure. Found by simulating the module split against the real `SKILL.md`: every import
        re-pointed at a module that does not exist yet, which is exactly what a half-finished
        Phase 7 looks like.
        """
        markdown = (
            "```python\nfrom coder_eval.optimize_nonexistent import activation_gate\n\n"
            "activation_gate(suite_id='s')\n```\n"
        )
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "does not import" in failures[0], failures

    def test_the_binder_reports_rather_than_crashes_on_a_wholesale_module_rename(self):
        """The Phase 7 rehearsal, on the REAL file: the sensor must survive being wrong."""
        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        mutated = raw.replace("from coder_eval.optimize.", "from coder_eval.nowhere_optimize.")
        assert mutated != raw, "the anchor moved — re-derive it from the skill's snippets"
        failures = _snippet_binding_failures(mutated)
        assert len(failures) >= 15, failures
        assert all("does not import" in f for f in failures), failures

    def test_a_real_non_callable_import_stays_silent(self):
        """`UNRESOLVED_MODEL` and `UNRECORDED_SPLIT` are strings the shipped skill imports.

        The call loop's `not callable` skip must survive: it exists for exactly these, and the new
        existence check must not start reporting them.
        """
        markdown = "```python\nfrom coder_eval.optimize.store import UNRESOLVED_MODEL, UNRECORDED_SPLIT\n```\n"
        assert _snippet_binding_failures(markdown) == []

    @pytest.mark.parametrize(
        "markdown",
        [
            "```python\nfrom coder_eval.optimize.load import (\n    load_arm_rows,\n    gone_missing,\n)\n```\n",
            "```python\nfrom coder_eval.optimize.load import load_arm_rows, gone_missing\n```\n",
        ],
        ids=["parenthesized", "single-line"],
    )
    def test_both_import_forms_are_covered(self, markdown):
        # `origins` is built by two separate regexes; a check that only saw one form would go
        # silent on half the skill's imports.
        failures = _snippet_binding_failures(markdown)
        assert len(failures) == 1 and "gone_missing" in failures[0], failures

    def test_the_snippet_binder_skips_a_shadowed_name(self):
        # A snippet that rebinds an imported name locally must not be bound against the import.
        markdown = (
            "```python\n"
            "from coder_eval.optimize.activation import activation_gate\n\n"
            "activation_gate = lambda **kw: None\n"
            "activation_gate(bogus_kwarg=1)\n"
            "```\n"
        )
        assert _snippet_binding_failures(markdown) == []

    # Claims the code RETIRED, which must not creep back into prose. Each entry is
    # (fragment, why it is false now). The whole sensor family asserts tokens are PRESENT;
    # nothing asserted that a retired claim is ABSENT, and that gap cost this repo twice in one
    # change — the "guardrails gate in the procedure" assertion had to be removed from six
    # surfaces, and a SEVENTH (the activation-track twin, `SKILL.md`) was missed by the plan's own
    # enumeration and by every existing sensor, surviving until a reviewer read the file.
    #
    # Matched against the NORMALIZED text, so a line wrap cannot hide a claim.
    _RETIRED_CLAIMS = (
        (
            "guardrails gate here, in the procedure",
            "the guardrails gate in `holm_promote` / `holm_promote_execution` — in the library, "
            "not in the skill's prose",
        ),
        (
            "stay advisory in the model",
            "a failing guardrail FORCES `promoted = False` on both tracks",
        ),
        (
            "the cost/latency guardrails stay advisory",
            "both tracks fold their guardrails into `promoted`",
        ),
    )

    @pytest.mark.parametrize(
        "surface",
        [
            "skills/optimize-skill/SKILL.md",
            "reference/optimize-method.md",
        ],
    )
    def test_no_surface_revives_a_retired_guardrail_claim(self, surface: str):
        """A retired claim must stay retired — the ABSENCE half this sensor family lacked.

        `promoted` folds both veto lists on both tracks now, so any prose telling a reader that
        the guardrails gate "in the procedure", or that they "stay advisory in the model", sends
        them to check `.passed` by eye on a field that already decided it. That is worse than a
        stale sentence: it is a sentence that instructs.
        """
        text = _normalized(PLUGIN_ROOT / surface)
        revived = [(claim, why) for claim, why in self._RETIRED_CLAIMS if claim in text]
        assert not revived, "\n  ".join(
            [f"{surface} revives a retired claim:", *(f"{claim!r} — but {why}" for claim, why in revived)]
        )

    def test_the_retired_claim_sensor_would_fire(self, tmp_path: Path):
        """Anti-vacuity, through the REAL reader: a fragment that cannot match guards nothing.

        Fed HARD-WRAPPED, because that is how these documents are written and a raw substring
        check would pass on exactly the text it exists to catch — the reason every sensor here
        reads its surface through `_normalized`.
        """
        for claim, _why in self._RETIRED_CLAIMS:
            wrapped = "prose that says\nthe " + claim.replace(" ", "\n") + "\nand should fail"
            surface = tmp_path / "surface.md"
            surface.write_text(wrapped, encoding="utf-8")
            assert claim in _normalized(surface), claim

    def test_optimize_method_quotes_no_tolerance_numbers(self):
        # The guardrails are bootstrap-derived; the ONE tolerance constant lives in the module.
        # A figure hand-copied into the prose is a second declaration of it — the same shape as the
        # pricing.py <-> pricing.ts mirror this repo had to add a parity test to defend — and it
        # goes stale silently, because nothing about changing a constant makes anyone reread a
        # markdown file. Derived from the constant, so this cannot itself go stale.
        from coder_eval.optimize.gate import MATERIALITY_FLOOR

        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        forbidden = {f"{MATERIALITY_FLOOR:.0%}", f"{MATERIALITY_FLOOR:g}", f"{MATERIALITY_FLOOR:.2f}"}
        present = sorted(f for f in forbidden if f in method)
        assert not present, (
            f"reference/optimize-method.md quotes {present} — the rendered value of MATERIALITY_FLOOR. "
            "The guardrail tolerance is owned by coder_eval.optimize.gate; describe the guardrail as "
            "bootstrap-derived and carry no figure, or the prose drifts the moment the constant moves."
        )

    def test_optimize_method_states_a_stage_c_criterion_for_each_track(self):
        """Stage C's acceptance criterion is a DIFFERENT quantity per track, and it said only one.

        The shipped § Stage C text was activation-only — "F1 remains the promotion metric … require
        the F1 direction to reproduce" — which instructs an execution reader to check a number their
        track never produces. On that track the paired `weighted_score` block IS the result, not a
        corroboration of one.

        Asserted by TOKEN rather than by sentence because the prose is pitched at a reader and will be
        reworded; what may not vanish is that both metrics are named as the criterion, and that the
        computed outcomes exist. `f1` alone would be vacuous — the section already mentions F1 nine
        times — so the anchor is the per-track heading plus each track's metric plus the four
        outcomes, which are the model's own `Literal` values and are derived from it here rather than
        retyped.
        """
        import typing

        from coder_eval.models import ConfirmVerdict

        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        stage_c = method[method.index("### Stage C") :]
        assert "acceptance criterion, per track" in stage_c, (
            "reference/optimize-method.md § Stage C no longer states its criterion PER TRACK. "
            "Activation promotes on f1.yes and execution on per-row weighted_score, so one "
            "criterion tells one of the two readers to check a quantity their track never produces."
        )
        for metric in ("f1.yes", "weighted_score"):
            assert metric in stage_c, f"§ Stage C does not name {metric} as an acceptance criterion"
        outcomes = typing.get_args(ConfirmVerdict.model_fields["outcome"].annotation)
        assert len(outcomes) == 4, f"ConfirmVerdict.outcome no longer has four values: {outcomes}"
        for outcome in outcomes:
            assert outcome.upper() in stage_c, (
                f"§ Stage C does not name the {outcome!r} outcome, so a reader cannot tell what the "
                "computed verdict can say"
            )

    def test_the_proposal_prompt_names_scripts_as_edit_targets(self):
        """The gap that produced this: three surfaces, zero mentions, and nobody noticed.

        A skill is a DIRECTORY, and the highest-leverage candidate class — replacing six prose steps
        with a bundled script — was nowhere described as legal. Without a sensor this paragraph is one
        careless edit from vanishing, and its absence is silent: nothing fails, the proposer simply
        stops being told, and the class of candidate stops being written.

        Asserted by TOKEN rather than by sentence, because prose pitched at a proposer will be
        reworded. `scripts/` alone would be too weak — the file mentions `scripts/` in the leak
        paragraph — so the anchor is the edit-target CLAIM plus the hypothesis it names.
        """
        prompt = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        assert "legitimate edit targets" in prompt, (
            "reference/proposal-prompt.md no longer names scripts and reference files as edit "
            "targets. A skill is a directory, and a candidate that moves instruction into a script "
            "is the highest-leverage shape there is — a proposer not told so will not write one."
        )
        for token in ("scripts/", "reference files", "determinism"):
            assert token in prompt, f"the scripts-as-edit-targets paragraph no longer names {token!r}"
        # The three constraints that follow from it, each of which is silent when dropped.
        assert "allowed_tools" in prompt, "the paragraph must say a bundled script needs Bash in allowed_tools"
        assert "skill_text" in prompt, "it must say the leak check reads the whole directory"
        assert "Activation is untouched" in prompt, (
            "it must say a scripts-only candidate cannot move the activation number — a reader will "
            "otherwise look for an effect that cannot be there"
        )

    def test_the_proposal_prompt_supplies_the_passing_rows(self):
        # A proposer shown only failures optimizes for them alone, and an edit that fixes three rows
        # while breaking two is a net loss the aggregate hides. The caveats are part of the claim:
        # the evidence is genuinely mixed, so a sensor that let the honest hedge be deleted would be
        # pinning an overclaim.
        prompt = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        assert "must not regress" in prompt, (
            "reference/proposal-prompt.md no longer supplies the PASSING rows. Only the failing rows "
            "were ever handed over, so nothing told the proposer what it was trading against."
        )
        assert "sample, not the whole passing set" in prompt, "the sampling caveat must stay"
        assert "genuinely mixed" in prompt, (
            "the honest caveat must stay: whether failures-plus-successes beats failures-only flips "
            "across settings, and a sensor pinning the claim without it pins an overclaim"
        )
        assert "regression_check" in prompt, (
            "the proposer must be pointed at the regression corpus BEFORE it writes — the skill only "
            "reads it at Step 10, after the round is paid for"
        )

    def test_the_stop_rule_counts_candidates_not_only_rounds(self):
        """The patience is a budget in HYPOTHESES, and rounds are a poor proxy for them.

        Two rounds of four candidates have tested eight; two rounds of one have tested two and say
        almost nothing. A reader deciding whether a skill is at its ceiling needs the count that
        actually failed.
        """
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        stop_rule = skill[skill.index("## Step 13") :]
        assert "candidates gated across" in stop_rule, (
            "optimize-skill's Step 13 no longer prints a cumulative CANDIDATE count beside the round "
            "count, so 'two rounds promoted nothing' cannot be told from two rounds of one candidate"
        )
        # `_normalized` collapses the file's hard wrapping, so the phrase is matched unwrapped.
        assert "budget in candidates" in stop_rule, (
            "Step 13 must say the patience is a budget in candidates rather than in rounds"
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

    def test_optimize_surfaces_call_the_cost_front_advisory(self):
        # Derived from the constant exactly as the MATERIALITY_FLOOR sensor is, so the claim cannot
        # exist in three files at three vintages. Asserted PER SURFACE rather than against the
        # concatenation: both carry it today, so a pair-wide check would stay green if either
        # section were deleted whole — and the procedure half is what an agent acts on.
        from coder_eval.reports_optimize import COST_FRONT_ADVISORY

        # A distinctive substring of the constant rather than the whole sentence: the prose is
        # hard-wrapped and pitched at a reader, so it paraphrases around this anchor.
        anchor = "advisory"
        assert anchor in COST_FRONT_ADVISORY, "the advisory constant no longer contains its own anchor word"
        for name in ("reference/optimize-method.md", "skills/optimize-skill/SKILL.md"):
            surface = _normalized(PLUGIN_ROOT / name)
            assert anchor in surface, (
                f"{name} no longer describes the cost/quality front as {anchor!r}. It is a REPORTED "
                "view: promotion still requires the primary statistic to separate and every "
                "guardrail to hold, and prose that loses that reads as a licence to promote past "
                "the cost veto."
            )
            # A phrase unique to the NEW paragraphs. `"guardrail"` would have been vacuous —
            # it appears 7x in the method file and 12x in the skill already, so deleting the whole
            # cost-front section would have left the assertion green.
            assert "never a promotion" in surface, (
                f"{name} no longer distinguishes the cost VETO from the cost OBJECTIVE — without "
                "it a reader takes the front for a second gate and promotes past the guardrail"
            )

    def test_optimize_surfaces_quote_no_resample_count(self):
        # Same rule as the tolerance above, two constants along. GATE_RESAMPLES is DERIVED from
        # GATE_P_PRECISION / GATE_MAX_FAMILY / DEFAULT_ALPHA, so a figure typed into the prose is a
        # second declaration of a number the module computes — and the derivation is exactly the
        # kind of thing that gets retuned once and then disagrees with two markdown files forever.
        #
        # DEFAULT_ALPHA rides the same sensor rather than a third near-identical one. The sizing
        # table put it at risk: it needs the Holm threshold in a cell, and the honest way to write
        # that is `alpha/S` bound to the constant by the CE039 claim — not a retyped `0.05/5`.
        from coder_eval.optimize.gate import GATE_RESAMPLES
        from coder_eval.reports_stats import DEFAULT_ALPHA

        forbidden = {
            f"{GATE_RESAMPLES:d}": "GATE_RESAMPLES, which the module derives from two stated requirements",
            f"{GATE_RESAMPLES:,d}": "GATE_RESAMPLES, which the module derives from two stated requirements",
            f"{DEFAULT_ALPHA:g}": "DEFAULT_ALPHA, which reports_stats owns",
        }
        for name in ("reference/optimize-method.md", "skills/optimize-skill/SKILL.md"):
            surface = _normalized(PLUGIN_ROOT / name)
            present = sorted(f for f in forbidden if f in surface)
            assert not present, (
                f"{name} quotes {present} — the rendered value of "
                + "; ".join(forbidden[f] for f in present)
                + ". Write the symbol (`alpha`) and let the block or the CE039 claim bind it; the "
                + "rendered verdict reports the values it actually used."
            )

    def test_skill_listing_budget_is_bounded(self):
        # See SKILL_LISTING_BUDGET_CHARS for why a plugin should self-limit here.
        # Filesystem-derived: no hardcoded skill names, no per-skill numbers.
        per_skill: dict[str, int] = {}
        for path in PLUGIN_SKILLS:
            meta = _skill_frontmatter(path)
            per_skill[path.parent.name] = len(meta.get("description") or "") + len(meta.get("when_to_use") or "")
        total = sum(per_skill.values())
        assert total <= SKILL_LISTING_BUDGET_CHARS, (
            f"the plugin's skill descriptions total {total} characters, over the "
            f"{SKILL_LISTING_BUDGET_CHARS} ceiling (per skill: {sorted(per_skill.items())}). Prefer "
            "trimming an existing description; the listing budget is shared with every skill the "
            "user has installed. Raising the ceiling is allowed in a commit that says why."
        )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_skill_docs_surfaces_list_every_skill(self, skill: Path):
        # What stops the next skill from shipping undocumented. CLAUDE.md was normalized to
        # the slash form when the sixth skill landed, so one form is accepted everywhere.
        name = f"/coder-eval:{skill.parent.name}"
        missing = [
            surface
            for surface in SKILL_DOC_SURFACES
            if name not in (self.REPO_ROOT / surface).read_text(encoding="utf-8")
        ]
        assert not missing, f"{name} is not documented in {missing} — a shipped skill nobody can discover"

    def test_tutorial_index_lists_every_tutorial(self):
        # `docs/tutorials/README.md` is the index a reader lands on, and it is hand-written
        # while the set of tutorials is a directory listing. mkdocs' nav is guarded by CE028,
        # but nothing read this table — so a tutorial added without a row here is invisible
        # to anyone who does not already know its filename.
        tut_dir = self.REPO_ROOT / "docs" / "tutorials"
        index = (tut_dir / "README.md").read_text(encoding="utf-8")
        pages = sorted(p.name for p in tut_dir.glob("*.md") if p.name != "README.md")
        missing = [name for name in pages if f"({name})" not in index]
        assert not missing, (
            f"docs/tutorials/README.md's table does not link {missing}. A tutorial nobody links "
            f"is a tutorial nobody finds."
        )

    def test_tutorial_09_excerpts_match_the_committed_suite(self):
        # Tutorial 09 quotes `tasks/skills/ci-outcome.yaml` and its rows file as excerpts.
        # An excerpt is a derived surface: when the suite gained a second `includes` slot
        # and an `expected_snippet_2` field, the page kept showing the old shape and a
        # reader copying it would build a suite that raises at expansion. Compared against
        # the real files rather than pinned, so the suite stays free to change.
        import json
        import re

        page = (self.REPO_ROOT / "docs" / "tutorials" / "09-optimizing-a-skill-body.md").read_text(encoding="utf-8")
        rows = [
            json.loads(ln)
            for ln in (self.REPO_ROOT / "tasks" / "skills" / "ci-outcome-rows.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        real_fields = set(rows[0])

        # 1. The illustrative JSONL row must carry exactly the fields a real row does.
        shown = [json.loads(m) for m in re.findall(r'^\{"id".*\}$', page, re.M)]
        assert shown, "tutorial 09 no longer shows a sample dataset row; this sensor reads it"
        for row in shown:
            assert set(row) == real_fields, (
                f"tutorial 09's sample row has fields {sorted(set(row))}, but a committed row has "
                f"{sorted(real_fields)}. A reader copies that shape — a missing ${{row.*}} field "
                f"raises at expansion."
            )

        # 2. Every ${row.*} the page's YAML excerpt references must exist on a real row.
        referenced = set(re.findall(r"\$\{row\.([A-Za-z_][A-Za-z0-9_]*)\}", page))
        missing = referenced - real_fields
        assert not missing, f"tutorial 09 references ${{row.{sorted(missing)}}}, which no committed row carries"

        # 3. The excerpt must not UNDER-state the gated criterion: if the suite grades two
        #    snippets, showing one teaches a shape that scores differently from the file.
        suite = (self.REPO_ROOT / "tasks" / "skills" / "ci-outcome.yaml").read_text(encoding="utf-8")
        for field in ("expected_snippet", "expected_snippet_2"):
            if f"${{row.{field}}}" in suite:
                assert f"${{row.{field}}}" in page, (
                    f"the committed suite grades ${{row.{field}}} but tutorial 09's excerpt omits it"
                )

    def test_docs_state_the_right_criterion_type_count(self):
        # Same drift class as the skill count below, one layer down: a hand-maintained
        # number describing a set the registry derives. It had already rotted — three sites
        # said 14 against a registry of 15 — and nothing noticed, because the only guarded
        # surface was CLAUDE.md's heading, which happened to be right.
        #
        # Derived from the registry, never from a literal here: adding a criterion must not
        # require editing this test, only the prose it points at.
        import re

        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=False)
        count = len(CriterionRegistry.list_types())
        # \b on the number, or "5 criterion types" matches inside a correct "15 criterion
        # types" and the sensor reports a failure that is not there.
        patterns = (r"\b(\d+) criterion types", r"\b(\d+) success criteria types", r"Success Criteria \((\d+) types\)")
        offenders = []
        for rel in (
            "docs/TASK_DEFINITION_GUIDE.md",
            "CLAUDE.md",
            "README.md",
            *sorted(str(p.relative_to(self.REPO_ROOT)) for p in (self.REPO_ROOT / "docs" / "tutorials").glob("*.md")),
        ):
            text = _normalized(self.REPO_ROOT / rel)
            for pat in patterns:
                offenders += [f"{rel}: {m.group(0)!r}" for m in re.finditer(pat, text) if int(m.group(1)) != count]
        assert not offenders, (
            f"the registry has {count} criterion types, but these surfaces state another count: "
            f"{offenders}. The count is derived from `CriterionRegistry.list_types()` — update the prose."
        )

    def test_skill_docs_surfaces_state_the_right_count(self):
        # The companion to the test above, which only checks that each NAME appears. These
        # surfaces also state the count in prose, and adding the sixth skill meant hand-editing
        # seven such sites across four files. Without this, a seventh ships with every count
        # silently wrong — the exact drift that repair was. Derived from disk: no count is
        # written down here, and the matcher itself lives in `_wrong_skill_count_offenders`
        # so the wrapped-phrase self-test below can exercise the real thing.
        count = len(PLUGIN_SKILLS)
        auto = count - sum(1 for v in SKILL_DISABLE_MODEL_INVOCATION.values() if v)
        offenders = _wrong_skill_count_offenders(
            {surface: self.REPO_ROOT / surface for surface in SKILL_DOC_SURFACES},
            count=count,
            auto=auto,
        )
        assert not offenders, (
            f"there are {count} shipped skills, but these surfaces still state another count: "
            f"{offenders}. Update the prose alongside the table."
        )

    def test_no_sensor_inlines_the_normalization_idiom(self):
        # `_normalized` exists because a hard wrap silently defeated a sensor and shipped a
        # stale skill count past 91 green tests. The extraction only helps if every sensor
        # actually uses it, and a new one is usually copied from a neighbour — so if the
        # neighbour inlines the idiom, the bug propagates. This forbids the raw form.
        source = Path(__file__).read_text(encoding="utf-8")
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(source.splitlines(), 1)
            # The one legitimate occurrence is `_normalized`'s own body, exempted by exact
            # match rather than by line number so the guard survives edits above it.
            if '" ".join(' in line
            and ".read_text(" in line
            and ".split()" in line
            and line.strip() != _NORMALIZED_IMPL
            # ...and skip this guard's own machinery, which must mention the pattern to test it.
            and "_NORMALIZED_IMPL" not in line
        ]
        assert not offenders, (
            "these lines inline the whitespace-normalization idiom instead of calling "
            f"`_normalized()`: {offenders}. A sensor copied from one of them inherits the "
            "wrapped-phrase blind spot that `_normalized` exists to close."
        )

    def test_count_sensor_catches_a_wrapped_phrase(self, tmp_path: Path):
        # The self-test for the fix above, driven through the REAL matcher rather than
        # through `_normalized` alone. That distinction is the whole value: asserting only
        # that `_normalized` collapses whitespace leaves the sensor free to be reverted to a
        # raw `read_text` with every test still green — a guard that guards nothing.
        #
        # `docs/PLUGIN.md` said "All six\n  skills read it" while seven shipped, and the
        # count sensor passed 91 lint tests: the hard wrap put a newline between the two
        # words the substring check needed adjacent.
        surface = tmp_path / "PLUGIN.md"
        surface.write_text("All six\n  skills read it, which is the point.\n", encoding="utf-8")

        assert "six skills" not in surface.read_text(encoding="utf-8"), (
            "this test's premise is gone: the wrapped form is now literally adjacent, so it "
            "no longer demonstrates what the normalization buys"
        )
        assert _wrong_skill_count_offenders({"wrapped": surface}, count=7, auto=4) == ["wrapped: 'six skills'"], (
            "the count sensor found nothing in a surface reading 'All six\\n  skills' while "
            "seven ship. It has stopped collapsing whitespace, so any wrapped count phrase "
            "slips past it — the exact bug that let the stale count ship green."
        )

        # The converse, so a passing sensor is not simply one that reports everything: the
        # correct count in the same wrapped shape must yield no finding.
        clean = tmp_path / "CLEAN.md"
        clean.write_text("All seven\n  skills read it, which is the point.\n", encoding="utf-8")
        assert _wrong_skill_count_offenders({"clean": clean}, count=7, auto=4) == []

    def test_cli_driving_skills_are_named_in_the_install_prose(self):
        # Both READMEs promise that the CLI-driving skills preflight `coder-eval --version`.
        # SKILLS_REQUIRING_THE_CLI is the declared set, but nothing tied it to the prose, so
        # `optimize-skill` joined the set and both files went on naming three of four — a
        # first-run user of the fourth gets a bare `command not found`. Derived from the set,
        # with no skill names written down here, so a fifth cannot ship silently either.
        #
        # Scoped to the install paragraph, not the whole file: every skill name appears
        # SOMEWHERE in both documents, so a whole-file match would assert nothing at all.
        for surface in ("plugins/coder-eval/README.md", "docs/PLUGIN.md"):
            text = _normalized(self.REPO_ROOT / surface)
            anchor = text.find("coder-eval --version")
            assert anchor != -1, (
                f"{surface} no longer mentions `coder-eval --version` — the install paragraph "
                "this sensor anchors on is gone, so the promise it checks is unverifiable"
            )
            # The sentence naming the skills sits just before the anchor; take a generous
            # window either side rather than guessing at sentence boundaries.
            paragraph = text[max(0, anchor - 400) : anchor + 400]
            missing = sorted(name for name in SKILLS_REQUIRING_THE_CLI if f"`{name}`" not in paragraph)
            assert not missing, (
                f"{surface}'s install paragraph does not name {missing}, which "
                f"SKILLS_REQUIRING_THE_CLI declares must preflight `coder-eval --version`. A "
                "user of an unnamed skill is told nothing about needing the CLI and meets a "
                "bare `command not found` mid-task."
            )

    def test_tutorial_08_row_table_matches_the_committed_jsonl(self):
        # The page's Step-1 table claimed 21 rows against a file holding 28, with the
        # `analyze` row count off by seven — added in Part 2 and never back-propagated. A
        # reader sizing their own suite from that table sizes it from a number that has not
        # been true for two revisions, and nothing in the build noticed.
        #
        # Derived, not duplicated: the JSONL is the source and the table is the surface, so
        # this compares the two rather than pinning either.
        import collections
        import json
        import re

        page = (self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill-description.md").read_text(
            encoding="utf-8"
        )
        rows_file = self.REPO_ROOT / "tasks" / "skills" / "lint-tasks-activation-rows.jsonl"
        rows = [json.loads(ln) for ln in rows_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

        actual = collections.Counter(r["expected_skill"] for r in rows)
        by_split = collections.Counter((r["expected_skill"], r.get("split")) for r in rows)

        # | Kind | `expected_skill` | Total | train | test |
        table = re.findall(
            r"^\|\s*(?:Positive|Distractor|Sibling-owned)\s*\|\s*`([^`]*)`\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
            page,
            re.M,
        )
        assert table, "tutorial 08's Step-1 row table is gone or reshaped; this sensor reads it by column"

        stated_total = 0
        for raw_skill, total, train, test in table:
            skill = "" if raw_skill == '""' else raw_skill
            stated_total += int(total)
            assert (int(total), int(train), int(test)) == (
                actual[skill],
                by_split[(skill, "train")],
                by_split[(skill, "test")],
            ), (
                f"tutorial 08's table says {skill!r} has {total} rows ({train} train / {test} test); "
                f"{rows_file.name} has {actual[skill]} ({by_split[(skill, 'train')]}/{by_split[(skill, 'test')]}). "
                "Recompute the table from the file — a reader sizes their own suite from it."
            )
        assert stated_total == len(rows), (
            f"tutorial 08's table rows sum to {stated_total}; the committed JSONL has {len(rows)} rows"
        )
        assert f"**{len(rows)} rows**" in page, (
            f"tutorial 08 no longer states the committed row count as **{len(rows)} rows** in prose"
        )

    def test_tutorial_08_shows_stage_b_as_three_separate_invocations(self):
        # The single most dangerous edit in this whole area. Suite rollups are keyed on
        # (variant, suite), so `--repeats 3` pools all three replicates into ONE suite.json
        # with one confusion matrix — the per-replicate F1 the activation gate compares
        # would not exist, and the gate would silently compare a number against itself.
        # Now that the page shows real command lines, "simplifying" them is a one-line edit.
        page = self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill-description.md"
        lines = page.read_text(encoding="utf-8").splitlines()

        # Matched on heading TEXT at any level: pinning `###` broke the first time the page
        # was legitimately restructured, which is a sensor failing for a reason that has
        # nothing to do with what it guards.
        def _is_heading(ln: str) -> bool:
            return ln.lstrip("#") != ln and ln.lstrip("#").startswith(" ")

        start = next(i for i, ln in enumerate(lines) if _is_heading(ln) and "Stage B" in ln)
        end = next(i for i, ln in enumerate(lines[start + 1 :], start + 1) if _is_heading(ln))
        block = "\n".join(lines[start:end])

        assert block.count("--run-dir") >= 3, (
            "tutorial 08's Stage B no longer shows THREE separate invocations with distinct "
            f"--run-dir values (found {block.count('--run-dir')}). Three run directories are "
            "what produce three suite.json files; the gate has nothing to read without them"
        )
        # Scoped to the fenced COMMANDS, not the prose: the section's own warning says
        # "**not** `--repeats 3`", and a sensor that fired on the warning would be telling
        # the author to delete the very sentence it exists to protect.
        fenced, inside = [], False
        for line in lines[start:end]:
            if line.strip().startswith("```"):
                inside = not inside
                continue
            if inside:
                fenced.append(line)
        commands = "\n".join(fenced)
        assert "--repeats" not in commands, (
            "tutorial 08's activation Stage B now RUNS `--repeats` — rollups pool replicates "
            "into one suite.json, so the per-replicate F1 this gate compares would not exist. "
            "`--repeats` is correct at Stage C only, where paired_comparison averages per row "
            "BEFORE pairing"
        )
        assert "not** `--repeats 3`" in block, (
            "tutorial 08's Stage B no longer WARNS against --repeats 3. The commands are now "
            "shown, so the warning is what stops the next reader collapsing them"
        )

    def test_tutorial_09_keeps_the_claims_a_reader_most_needs(self):
        # Tutorial 09 reports a NULL result, and the facts that make it useful are the ones
        # an editor tightening a long page would trim first: the suite's shape, the setting
        # that decided the outcome, why the round stopped, and the denominator rule. Same
        # deletion-sensor shape as the optimize-skill guard above.
        text = _normalized(self.REPO_ROOT / "docs" / "tutorials" / "09-optimizing-a-skill-body.md")

        for token, why in (
            (
                "one dataset-backed task",
                "the outcome suite's shape — separate task files produce no rollup to rank and "
                "make --split test silently re-run the train rows",
            ),
            (
                "disallowed_tools",
                "denying sub-agent delegation is the setting that moved engaged rows from 0.333 "
                "to 1.000; an allowlist cannot express it",
            ),
            (
                "disable-model-invocation",
                "the round's actual root cause: the Skill tool REFUSES such a call, so the body "
                "never loads and every criterion scores the model's prior knowledge instead",
            ),
            (
                "read the call's *parameters* and never its *result*",
                "why it stayed hidden — the engagement criterion reported yes for a refused "
                "call, which is what made four arms tie exactly",
            ),
            (
                "completion_rate",
                "an errored row is excluded from the aggregate, so it never shows up as a low "
                "score — only as a denominator that shrank",
            ),
        ):
            assert token in text, f"tutorial 09 lost {token!r} — {why}"

    def test_analyze_routes_fixes_to_the_right_layer(self):
        # A shallow keyword sensor: it guards against the guidance being DELETED, not
        # against it being badly written. The root-cause token has to survive too, because
        # the routing rule is attached to it — `prompt_gap` naming a missing piece of
        # knowledge is meaningless if nothing says which layer should have supplied it.
        # Whitespace-collapsed so a reflowed paragraph does not fail it — these files are
        # hard-wrapped prose, and a sensor that breaks on rewrapping trains people to
        # distrust it.
        text = _normalized(PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md")
        assert "prompt_gap" in text, "analyze lost the `prompt_gap` root-cause token"
        for phrase in ("fix the prompt", "file the tool bug", "which layer"):
            assert phrase in text, (
                f"analyze no longer routes a prompt_gap to the layer that should own it "
                f"(missing {phrase!r}) — patching the prompt instead turns the score green "
                "and changes nothing for users"
            )

    @pytest.mark.parametrize(
        "doc",
        [p for p in PLUGIN_TEXT_FILES if p.suffix == ".md"],
        ids=[str(p.relative_to(PLUGIN_ROOT)) for p in PLUGIN_TEXT_FILES if p.suffix == ".md"],
    )
    def test_bundled_markdown_fences_balance(self, doc: Path):
        # A skill body is an instruction document; an unbalanced fence silently swallows
        # everything after it. `analyze` shipped a ```markdown block containing a ```diff
        # block, and because a closing fence may not carry an info string, the inner
        # opener closed the outer block early and the next bare ``` opened one that never
        # closed — burying 32 lines including the whole Principles section. Nothing caught
        # it, because it is still valid YAML frontmatter and valid-ish Markdown.
        #
        # CommonMark rule applied here: a fence closes only on a run of backticks at least
        # as long as the opener AND carrying no info string. Nesting therefore requires the
        # OUTER fence to be longer (````markdown wrapping ```diff).
        open_len = 0
        for n, raw in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line.startswith("```"):
                continue
            ticks = len(line) - len(line.lstrip("`"))
            info = line[ticks:].strip()
            if open_len == 0:
                open_len = ticks
                opened_at = n
            elif ticks >= open_len and not info:
                open_len = 0
        assert open_len == 0, (
            f"{doc}: code fence opened at line {opened_at} is never closed. A closing fence "
            "may not carry an info string, so a nested block needs a LONGER outer fence "
            "(````markdown around ```diff). Everything after the opener renders as code."
        )

    def test_cli_setup_is_bundled_and_read_by_the_cli_driving_skills(self):
        # Installing the plugin does not install the CLI, so every CLI-driving skill has to
        # handle a missing binary. The POLICY for that (offer, ask, verify, never install
        # unprompted) is declared once in reference/cli-setup.md; each skill keeps only the
        # one-line check locally. Both halves are asserted: the reference ships, and every
        # skill that needs it points at it.
        setup = PLUGIN_ROOT / "reference" / "cli-setup.md"
        assert setup.exists() and setup.read_text(encoding="utf-8").strip(), (
            f"{setup} must exist and be non-empty — the CLI-driving skills read it at runtime"
        )
        pointer = "${CLAUDE_PLUGIN_ROOT}/reference/cli-setup.md"
        for name in sorted(SKILLS_REQUIRING_THE_CLI):
            text = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            assert pointer in text, (
                f"{name} shells out to the CLI but no longer points at {pointer} — it has "
                "forked the install policy, or dropped it"
            )
        # The policy is a shared declaration, so a skill must not restate the install
        # command: two copies drift, and the wrong installer is a real footgun.
        for name in sorted(SKILLS_REQUIRING_THE_CLI):
            text = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            assert "uv tool install" not in text and "pip install" not in text, (
                f"{name} restates the install command that reference/cli-setup.md declares — "
                "point at the reference instead"
            )

    def test_task_skill_declares_repo_convention_precedence(self):
        # The bundled rubric is a floor, not a house style. A repository that already
        # authors tasks has its own conventions, and a skill that quietly overrides them
        # with the plugin's defaults produces work its maintainers have to undo — so the
        # precedence has to be stated, and the conventions adopted have to be reported
        # (otherwise "I followed the repo" is unfalsifiable).
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "the repo wins" in text or "the repository wins" in text, (
            "task no longer says repo-local convention beats the bundled rubric — it will "
            "impose the plugin's defaults on a repository that already declared its own"
        )
        assert "conventions you adopted" in text or "conventions adopted" in text, (
            "task does not report WHICH conventions it adopted — an unreported precedence "
            "rule cannot be checked by the person reading the result"
        )
        rubric = _normalized(PLUGIN_ROOT / "reference" / "task-rubric.md")
        assert "the repo wins" in rubric or "the repository wins" in rubric, (
            "reference/task-rubric.md does not carry the same precedence line — the rubric "
            "is read at review time too, and must not contradict the authoring skill"
        )

    def test_task_skill_documents_outcome_mode(self):
        # `/coder-eval:optimize-skill`'s execution track says "no suite -> stop and point at
        # /coder-eval:task". That instruction was unfollowable: `task` had no notion of a
        # dataset, a row or a split, and its guidance was explicitly one-file-per-task — the
        # precise anti-pattern, since N task files yield no suite.json rollup and `--split test`
        # silently re-runs the train rows. This asserts the mode exists and names what it emits.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "Outcome-suite mode" in text, (
            "task/SKILL.md no longer documents outcome-suite mode — optimize-skill's execution "
            "track hands suite authoring here and has nothing to hand it to"
        )
        assert "dataset-backed" in text and "one ROW per scenario" in text, (
            "task/SKILL.md does not state the dataset-backed, one-row-per-scenario constraint, "
            "which is the whole structural difference between the modes"
        )
        # Step 2.5 is what makes the rows DERIVED from the skill rather than invented beside it.
        assert "Rule inventory" in text and "R1" in text, (
            "task/SKILL.md no longer requires a numbered rule table (Step 2.5) — without it the "
            "rows are whatever the author thought of, and nothing says what the suite covers"
        )
        assert "checked mechanically" in text, (
            "the rule table no longer asks how each rule could be checked MECHANICALLY, which is "
            "what turns an inventory into criteria"
        )
        # All five artifacts, by the names the rest of the plugin uses for them.
        for artifact in ("suite YAML", "rows JSONL", "fixture directory", "grader script", "expectations"):
            assert artifact in text, (
                f"task/SKILL.md's outcome-mode file list no longer names the {artifact!r} — an "
                "artifact nobody is told to write is an artifact that does not get written"
            )
        assert "OUTSIDE the fixture" in text, (
            "task/SKILL.md no longer says the grader and its expectations live OUTSIDE the fixture "
            "directory. Everything under the fixture is copied into every sandbox, so the answer "
            "key ships with the exam — measured at +0.030 mean on a real suite"
        )

    def test_task_skill_supersedes_one_file_per_task(self):
        # A parity sensor, not two independent ones: the ORIGINAL guidance and its outcome-mode
        # OVERRIDE must both be present. Deleting either alone is the failure — the first leaves
        # default mode undocumented, the second leaves outcome mode contradicting the file it is
        # written in, and a reader following the wrong half writes N task files that produce no
        # rollup.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "One file per task" in text, (
            "task/SKILL.md lost the default-mode one-file-per-task guidance that outcome mode "
            "declares itself an exception to"
        )
        assert "several** task files" in text, (
            "task/SKILL.md lost the 'a single request can produce several task files' guidance — "
            "the other passage outcome mode supersedes"
        )
        assert "In outcome mode this is replaced" in text, (
            "Step 4 still says 'One file per task' with no outcome-mode override beside it. The "
            "two passages must be superseded WHERE THEY ARE READ; a rule stated only at the top "
            "of the file is not read by someone who jumped to Step 4"
        )
        assert "supersedes the one-file-per-task guidance in two places" in text, (
            "task/SKILL.md no longer names how many passages outcome mode overrides, so a third "
            "one can appear without anyone noticing it was left un-overridden"
        )

    def test_task_skill_has_discrimination_gate(self):
        # The one error class an A/B cannot detect: a wrong check biases every arm EQUALLY, so the
        # ranking, the paired test and the confirmation all agree with each other and are all wrong
        # together. Proving the grader separates a known-good artifact from a known-bad one is the
        # only place it gets caught, and it has to happen before a stage is paid for.
        text = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        assert "Step 6.5" in text, (
            "task/SKILL.md no longer has a Step 6.5 — the grader discrimination gate. Four shipped "
            "surfaces cite it by that number (see test_shipped_surfaces_cite_a_step_that_exists)"
        )
        assert "known-good" in text and "known-bad" in text, (
            "the discrimination gate no longer names both artifacts, so there is nothing to compare"
        )
        assert "separation margin" in text, (
            "the gate no longer requires the MARGIN to be reported. A gate whose result is not "
            "stated is a step someone can say they did"
        )
        # It cites the rubric rather than restating the fairness questions — one copy, asked by
        # both the skill that writes a grader and the skill that reviews one.
        assert "task-rubric.md" in text and "Grader fairness" in text, (
            "Step 6.5 no longer cites the rubric's grader-fairness section, so either the questions "
            "have been restated here (two copies to drift) or dropped"
        )
        # Step 6's split half: `--split` silently drops unlabelled rows, so every metric would be
        # computed over a smaller suite than the file suggests.
        # Anchored on the requirement, not on the flag names: `--split test` also appears in the
        # "Which mode" section, so asserting the flags alone survives deleting Step 6's paragraph.
        assert "--split train" in text and "Both must exit 0" in text, (
            "task/SKILL.md's outcome-mode validation no longer plans BOTH splits, so a partly "
            "labelled dataset ships silently — `--split` keeps the matching rows and DROPS the "
            "unlabelled ones, and every later metric is computed over the smaller suite"
        )

    def test_shipped_surfaces_cite_a_step_that_exists(self):
        # A prose cross-reference nothing guards. `test_bundled_plugin_root_references_resolve`
        # covers ${CLAUDE_PLUGIN_ROOT} FILE paths in .md files only — it cannot see "step 6.5", and
        # it does not scan .py at all. These four surfaces are what a user copies, and the step they
        # point at is the ONE safeguard against a silently-wrong grader.
        citing = (
            PLUGIN_ROOT / "reference" / "templates" / "outcome-grader" / "verify.py",
            PLUGIN_ROOT / "reference" / "templates" / "outcome-grader" / "expectations" / "core-1.json",
            PLUGIN_ROOT / "reference" / "templates" / "outcome.yaml",
            PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md",
        )
        target = _normalized(PLUGIN_ROOT / "skills" / "task" / "SKILL.md")
        citers = [path for path in citing if "step 6.5" in _normalized(path).casefold()]
        assert citers, (
            "no shipped surface cites the discrimination gate any more — if the step was renumbered, "
            "this test is looking for the wrong string and guards nothing"
        )
        assert "Step 6.5" in target, (
            f"{[p.name for p in citers]} point at `/coder-eval:task` step 6.5, which that skill no "
            "longer has. An author following the pointer finds nothing, and the grader ships ungated"
        )

    def test_task_rubric_carves_out_spec_from_leak_rule(self):
        # Both halves, so an edit cannot delete one and leave the other. Without the prohibition an
        # outcome prompt may state the rule under test, which grades transcription; without the
        # carve-out "follow the spec literally" is ungradeable, because it needs a spec to follow.
        rubric = _normalized(PLUGIN_ROOT / "reference" / "task-rubric.md")
        assert "MAY** state the *spec*" in rubric, (
            "the rubric lost the outcome-suite spec carve-out — an outcome prompt must be allowed to "
            "name the output path and the format, or the rule 'follow the user's spec' cannot be graded"
        )
        # No weaker fallback: a bare "MUST NEVER" is satisfied by any unrelated sentence in the
        # rubric, so the disjunct that was here loosened the sensor to nothing.
        assert "MUST NEVER** state a rule the *body*" in rubric, (
            "the rubric lost the prohibition the carve-out narrows, so the carve-out now reads as "
            "permission to put the graded behaviour in the prompt"
        )
        assert "without the skill" in rubric, (
            "the rubric no longer gives the TEST that separates the two (could an agent without the "
            "skill satisfy the check from the prompt alone?) — the rule is unappliable without it"
        )

    def test_grader_fairness_is_declared_once(self):
        # One declaration, two readers pointing at it from opposite ends: `task` asks these
        # questions when it WRITES a grader, `optimize-skill` when it reviews a baseline one. Both
        # restated them at one point; a second copy agrees on ordinary input and drifts exactly
        # where either was written for.
        rubric = _normalized(PLUGIN_ROOT / "reference" / "task-rubric.md")
        for question in ("penalise a legitimate alternative", "charge one mistake twice", "ever FAIL, and ever PASS"):
            assert question in rubric, f"the rubric's grader-fairness section lost {question!r}"
        assert "property of the ROW, never of the artifact" in rubric, (
            "the rubric no longer states which N/A triggers are legitimate. An N/A keyed on the "
            "ARTIFACT makes the denominator a function of the arm's own output — an arm that "
            "ignored a requirement is scored out of fewer checks than one that attempted it"
        )
        for name in ("task", "optimize-skill"):
            skill = _normalized(PLUGIN_ROOT / "skills" / name / "SKILL.md")
            assert "Grader fairness" in skill, (
                f"{name}/SKILL.md no longer points at the rubric's grader-fairness section"
            )
            assert "penalise a legitimate alternative" not in skill, (
                f"{name}/SKILL.md restates the fairness questions the rubric declares — two copies "
                "to drift, in the checks that exist to catch a grader nothing else can"
            )

    def test_optimize_skill_handoff_names_grader(self):
        # The handoff is the seam. optimize-skill refuses to author a suite (one written by the
        # thing that will judge it is fitted to it) and points at `task` — so it has to say what
        # comes back, or a user hands over an ungated instrument and pays for a stage on it.
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")
        assert "outcome-suite mode" in skill, (
            "optimize-skill's handoff no longer names task's outcome-suite MODE, so the instruction "
            "'point at /coder-eval:task' does not say which branch of it to ask for"
        )
        assert "grader script" in skill and "expectations" in skill, (
            "the handoff no longer names the grader among what `task` produces"
        )
        assert "separation margin" in skill, (
            "the handoff no longer asks for the discrimination gate's margin — the number that says "
            "whether the instrument measures anything at all"
        )

    def test_ci_skill_covers_experiments_and_pins(self):
        # Two silent-wrong-answer bugs in an emitted workflow. Omitting the experiment
        # drops the `agent:` config it supplies, so the gate measures something other than
        # what the suite measures locally; and a `version:` input that ignores the repo's
        # pin runs the gate on a different CLI than the repo is authored against.
        text = _normalized(PLUGIN_ROOT / "skills" / "ci" / "SKILL.md")
        assert "extra-args" in text and "experiment" in text, (
            "the ci skill does not say how to pass an experiment through to the run — a "
            "suite that resolves through one silently measures something else without it"
        )
        assert "pin" in text, (
            "the ci skill no longer conditions the `version:` input on whether the repository pins a coder-eval version"
        )

    def test_user_guide_documents_every_row_selector_on_plan(self):
        """`plan`'s flag table must name every selector the model declares.

        Derived from `ROW_SELECTOR_FLAGS` rather than a hand-typed list, so a fourth selector is
        covered here automatically — the same reason CE043 reads the mapping instead of restating
        it. This is the cheap, targeted half of a general "every long flag has a USER_GUIDE
        section" rule; it deliberately checks the `plan` section only, because that is the surface
        this pairing exists to keep honest. The table already lacked `--split` before the
        selectors were added to `plan` at all, which is how the gap went unnoticed.
        """
        from coder_eval.models import ROW_SELECTOR_FLAGS

        # RAW text, not `_normalized`: that helper collapses newlines, and this assertion is
        # about table ROWS, which only exist while the line structure does.
        guide = (PLUGIN_ROOT.parent.parent / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
        marker = "### `coder-eval plan`"
        assert marker in guide, "the USER_GUIDE no longer has a `coder-eval plan` section"
        section = guide.split(marker, 1)[1].split("\n### ", 1)[0]
        # A table ROW, not merely the token: every selector is also NAMED in its siblings' prose
        # ("applied before --sample / --sample-per-stratum"), so a substring test stays green
        # after the row documenting the flag is deleted. Verified: it did.
        rows = [line for line in section.splitlines() if line.lstrip().startswith("| `")]
        documented = {line.split("`")[1].split()[0].rstrip(",") for line in rows if "`" in line}
        missing = sorted(flag for flag in ROW_SELECTOR_FLAGS.values() if flag not in documented)
        assert not missing, (
            f"docs/USER_GUIDE.md's `plan` flag table does not document {missing}. `plan` is the "
            "pre-spend preview of a run, so a selector it accepts but never documents is one a "
            "user pays to discover."
        )

    def test_ci_skill_teaches_the_split_selector(self):
        # A gate that runs the TRAIN rows scores the skill partly on its own training data and
        # drifts optimistic exactly as the skill improves — the direction that HIDES a regression.
        # The emitted workflow is copied into users' repos, so a skill that never mentions the
        # selector produces a fleet of gates quietly measuring the wrong half of every split suite.
        text = _normalized(PLUGIN_ROOT / "skills" / "ci" / "SKILL.md")
        assert "--split" in text, (
            "the ci skill does not name --split — a suite whose rows are split-labelled will be "
            "gated on whichever half the default selects, which is both halves"
        )
        # Sliced to the SECTION, not the file. `extra-args` appears three times in the
        # pre-existing experiment subsection above, so a whole-file test is unconditionally true
        # and stays green with the snippet deleted — verified.
        section = text.split("The split, if the suite carries one", 1)[-1]
        assert 'extra-args: "--split test"' in section, (
            "the ci skill names --split but never shows it INSIDE extra-args — that input is the "
            "only channel the emitted workflow has for it, and a reader who has to guess the "
            "wiring guesses a `split:` action input that does not exist"
        )
        assert "train" in text, (
            "the ci skill does not say WHY the test split is the one to gate on — without the "
            "reason a reader drops the flag as noise, and a gate on train rows looks green while "
            "the skill is being tuned against the very rows it is scored on"
        )

    def test_ci_skill_does_not_recommend_a_recursive_task_glob(self):
        # `action.yml` expands the `tasks:` input unquoted (`args+=($CE_TASKS)`) with
        # globstar OFF, so `a/**/*.yaml` degrades to `a/*/*.yaml` and silently drops every
        # top-level task — a depth-dependent "measured the wrong set" bug. nullglob is off
        # too, so an unmatched depth pattern reaches the CLI literally and exits 1. Both
        # reproduced by hand. The snippet must therefore show neither `**` in its tasks
        # value nor a fixed ladder of depths.
        skill = PLUGIN_ROOT / "skills" / "ci" / "SKILL.md"
        assert "globstar" in skill.read_text(encoding="utf-8"), (
            "the ci skill emits explicit globs but no longer says WHY — without the reason, "
            "the next reader simplifies them back to `**` and loses the top-level tasks"
        )

        # Scoped to every surface CE026 already scans, not just the skill: the `ci` skill was
        # taught to avoid `**` while five snippets across README.md, docs/CI_GATE.md and the
        # CI tutorial still showed `tasks: tests/tasks/**/*.yaml`, so the plugin contradicted
        # the repo's own onboarding docs — and those are the ones integrators copy.
        from tests.lint.action_docs import default_doc_paths

        offenders = [
            f"{path}:{n}: {line.strip()}"
            for path in default_doc_paths(Path(__file__).parent.parent)
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"^\s*tasks:.*\*\*", line)
        ]
        assert not offenders, (
            "recursive `**` glob in a documented `tasks:` input value — the action word-splits "
            "and pathname-expands that value with globstar off, so it silently drops every task "
            "above the deepest matching level. Emit explicit per-depth globs or a file "
            "list:\n\n" + "\n".join(f"  {o}" for o in offenders)
        )

    def test_check_skill_detects_before_scaffolding(self):
        # Scaffolding a second activation suite beside one that already covers the same
        # skill is worse than doing nothing: two suites drift, and the user pays for both
        # on every run. The existence check therefore has to happen BEFORE row design,
        # which is where the token spend is committed.
        text = _normalized(PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md")
        check = text.find("existing suite")
        assert check != -1 and "skill_triggered" in text, (
            "check-skill names no `existing suite` check — it will scaffold a parallel suite "
            "over one that already covers the skill"
        )
        assert "extend" in text[check : check + 600], (
            "check-skill finds an existing suite but does not offer to EXTEND it — reporting "
            "coverage and then scaffolding anyway is the same duplication with extra steps"
        )
        design = text.find("Design the rows")
        assert design != -1, "check-skill's row-design step was renamed — re-anchor this guard"
        assert check < design, (
            "the existing-suite check must come before row design — that is the step which "
            "commits the token spend this check exists to avoid"
        )

    def test_check_skill_defers_to_an_experiment_supplied_plugins_block(self):
        # A task that redeclares what the experiment layer already provides drifts from it
        # silently — the same reason `task` tells authors to omit `agent:` entirely.
        text = _normalized(PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md")
        assert "experiment" in text and "agent.plugins" in text, (
            "check-skill no longer mentions an experiment-supplied `agent.plugins` block"
        )
        assert "inherit it" in text, (
            "check-skill does not tell the agent to INHERIT an experiment-supplied "
            "`agent.plugins` rather than writing its own — a task that redeclares what the "
            "experiment provides drifts from it with nothing to catch the divergence"
        )

    def test_init_stops_when_the_repo_is_already_configured(self):
        # `init` is a scaffolder pointed at repositories that have nothing. Run against one
        # that already has a suite, it wrote a "first task" beside an existing tree and
        # reported success — so the inventory has to be able to END the skill, not just
        # precede it.
        text = _normalized(PLUGIN_ROOT / "skills" / "init" / "SKILL.md")
        assert "already configured" in text or "already has" in text, (
            "init no longer recognizes an already-configured repository"
        )
        assert "stop" in text, "init's inventory step no longer stops — it only reports"
        for redirect in ("/coder-eval:lint-tasks", "/coder-eval:analyze"):
            assert redirect in text, (
                f"init reports an existing suite but does not point at {redirect} — the user "
                "is told what they have and given nothing to do with it"
            )

    def test_lint_tasks_scale_guidance_stays_read_only(self):
        # The durable half of the scale guard. `lint-tasks` has no `Bash`, so it cannot run
        # `git diff` to find a changed set however sensible that would be — it has to ASK
        # for one. Prose drifts; the read-only contract must not. (The frontmatter tool set
        # itself is already covered by `test_lint_tasks_skill_is_read_only`.)
        text = (PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md").read_text(encoding="utf-8")
        for command in ("git diff", "git status"):
            assert command not in text, (
                f"lint-tasks names `{command}`, which it has no `Bash` to run — at scale it "
                "must ask the user for the changed set instead of proposing a command"
            )

    def test_cli_setup_declares_pin_resolution(self):
        # A repository that already uses coder-eval usually pins the version, and the
        # damage from ignoring that pin is asymmetric: validating with a newer CLI
        # produces schema errors indistinguishable from real ones, and "fixing" them
        # edits the repo's tasks into a shape its own pinned CLI rejects. Asserted on a
        # concrete heading plus the stop rule rather than on the token `pin`, which
        # appeared nowhere in this file before the section landed — so any passing
        # mention would have satisfied a looser test and guarded nothing.
        text = _normalized(PLUGIN_ROOT / "reference" / "cli-setup.md")
        assert "## Version pin" in text, (
            "reference/cli-setup.md lost its `## Version pin` section — the CLI-driving "
            "skills would go back to validating a pinned repository with whatever binary "
            "happens to be on PATH"
        )
        assert "If the resolved version differs from the pin, stop and say so." in text, (
            "cli-setup.md no longer states the stop rule for a pin mismatch — resolving the "
            "pin is only useful if a mismatch halts the skill instead of being reported and "
            "then ignored"
        )

    def test_cli_setup_conditions_the_upgrade_suggestion_on_a_pin(self):
        # "Version skew" used to terminate in "suggest upgrading", full stop. Against a
        # pinned repository that is the single most destructive thing these skills could
        # recommend, so the upgrade advice must now sit BEHIND the pin question.
        section = (
            (PLUGIN_ROOT / "reference" / "cli-setup.md").read_text(encoding="utf-8").partition("## Version skew")[2]
        )
        assert section.strip(), "reference/cli-setup.md lost its `## Version skew` section"
        upgrade = section.find("upgrade coder-eval")
        assert upgrade != -1, "the `Version skew` section no longer names the upgrade command at all"
        assert "pin" in section[:upgrade], (
            "`Version skew` suggests upgrading before it has asked whether the project pins "
            "a version — upgrading past a pin is a repo-breaking action, not a fix"
        )
        assert "match the pin" in " ".join(section.split()), (
            "`Version skew` no longer says what to do when a pin DOES exist — the branch "
            "that makes the upgrade advice conditional rather than merely delayed"
        )

    def test_repo_layout_is_bundled_and_read_by_its_readers(self):
        # Sibling of the cli-setup guard above, for the third shared reference. "Where does
        # this repository keep its tasks and runs?" is a question every shipped skill asks —
        # most to read the trees, `ci` to write the resolved glob into a workflow; inlining
        # the answer once per skill is how the hardcoded `tasks/` / `runs/latest` assumptions
        # got there in the first place. Both halves are asserted: the reference ships, and
        # every skill that needs it points at it. (Counts stay out of this comment on
        # purpose — the mapping below is the source of truth and a seventh skill already
        # made two hardcoded numbers here stale.)
        layout = PLUGIN_ROOT / "reference" / "repo-layout.md"
        assert layout.exists() and layout.read_text(encoding="utf-8").strip(), (
            f"{layout} must exist and be non-empty — the shipped skills read it at runtime "
            "to find the repository's eval tree"
        )
        # Both directions, so neither the mapping nor the tree can drift silently: a
        # declared skill must ship, and a shipped skill must declare its stance.
        on_disk = {p.parent.name for p in PLUGIN_SKILLS}
        declared = set(SKILL_NEEDS_EVAL_ROOT_DISCOVERY)
        assert not declared - on_disk, (
            f"SKILL_NEEDS_EVAL_ROOT_DISCOVERY names skill(s) that do not ship: {sorted(declared - on_disk)}"
        )
        assert not on_disk - declared, (
            f"new skill(s) {sorted(on_disk - declared)} have not declared whether they need eval-root "
            "discovery — add them to SKILL_NEEDS_EVAL_ROOT_DISCOVERY. `False` is fine for a skill that "
            "touches no task or run tree, but defaulting to it silently is how a hardcoded `tasks/` "
            "gets back in."
        )

        pointer = "${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md"
        for name in sorted(n for n, needed in SKILL_NEEDS_EVAL_ROOT_DISCOVERY.items() if needed):
            text = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            assert pointer in text, (
                f"{name} has to locate the repository's eval tree but no longer points at "
                f"{pointer} — it has forked the discovery policy, or gone back to assuming a path"
            )

    @pytest.mark.parametrize("skill", PLUGIN_SKILLS, ids=[p.parent.name for p in PLUGIN_SKILLS])
    def test_skills_do_not_hardcode_an_eval_root(self, skill: Path):
        # The other half of the guard above: pointing at the reference is worthless if the
        # skill still falls back to one repository's layout when handed nothing. Whitespace-
        # normalized because these bodies are hard-wrapped prose — the `analyze` sentence this
        # rule exists for spans two source lines, so a literal match on the raw text would be
        # green today and stay green if the sentence came back.
        text = re.sub(r"\s+", " ", skill.read_text(encoding="utf-8"))
        offenders = [phrase for phrase in EVAL_ROOT_DEFAULT_PHRASINGS if phrase in text]
        assert not offenders, (
            f"{skill} still defaults to this repository's layout ({offenders}) — a repository "
            "that names its eval tree anything else silently gets the wrong path, or none. "
            "Follow reference/repo-layout.md and discover it instead."
        )

    def test_task_rubric_is_bundled_and_read_by_its_readers(self):
        # Both directions of the shared-SSOT decision: the rubric ships, and every skill
        # that is supposed to apply it actually points at it.
        rubric = PLUGIN_ROOT / "reference" / "task-rubric.md"
        assert rubric.exists() and rubric.read_text(encoding="utf-8").strip(), (
            f"{rubric} must exist and be non-empty — `task` and `lint-tasks` read it at runtime"
        )
        pointer = "${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md"
        for name in sorted(RUBRIC_READERS):
            skill = PLUGIN_ROOT / "skills" / name / "SKILL.md"
            assert pointer in skill.read_text(encoding="utf-8"), (
                f"{skill} no longer reads {pointer} — it has silently forked the shared rubric"
            )


@pytest.mark.lint
class TestCE033PluginReferenceParity:
    """CE033 — the plugin's bundled criteria reference is generated from the models.

    An installed plugin is copied to ~/.claude/plugins/cache/ without its parent
    directories, so its skills cannot read docs/TASK_DEFINITION_GUIDE.md at
    runtime — the criterion vocabulary has to ship inside plugins/coder-eval/,
    where a hand-maintained copy would drift on the next criterion change. The
    SuccessCriterion union is the SSOT; `make plugin-reference` writes the copy
    and this class diffs it. Reasons over Markdown + pydantic metadata, so it
    lives here rather than in the AST runner.
    """

    REPO_ROOT = Path(__file__).parent.parent

    def test_generated_reference_matches_disk(self):
        from tests.lint.plugin_reference import check

        findings = check(self.REPO_ROOT)
        assert not findings, (
            "\nThe plugin's bundled criteria reference drifted from the criterion models — run "
            "`make plugin-reference` to regenerate:\n\n"
            + "\n\n".join(f"{path}:\n{diff}" for path, diff in sorted(findings.items()))
        )

    def test_every_criterion_type_appears_in_the_reference(self):
        # Union-driven, so a 15th criterion must appear with zero edits to the generator.
        from tests.lint.plugin_reference import _VARIANTS, _tag, render_criteria

        rendered = render_criteria()
        for cls in _VARIANTS:
            assert f"### `{_tag(cls)}`" in rendered, f"{cls.__name__} is missing from the rendered reference"

    def test_generated_reference_lists_every_accepted_alias(self):
        # The loader accepts legacy `type:` values and rejects removed ones, and until now
        # neither fact reached the bundled reference — so an authoring agent reading a task
        # that uses one had nothing to look it up in, and no way to know the replacement.
        # Driven off the same two maps the validator consumes, so a new alias cannot ship
        # undocumented; the constants are the SSOT and this asserts the render followed.
        from coder_eval.models import NORMALIZED_CRITERION_ALIASES, REMOVED_CRITERION_TYPES
        from tests.lint.plugin_reference import render_criteria

        rendered = render_criteria()
        assert NORMALIZED_CRITERION_ALIASES or REMOVED_CRITERION_TYPES, (
            "both alias maps are empty — this guard is inert; drop it along with the section"
        )
        for alias, overlay in NORMALIZED_CRITERION_ALIASES.items():
            assert f"`{alias}`" in rendered, f"legacy alias {alias} is not documented in the reference"
            assert f"`{overlay['type']}`" in rendered, f"{alias}'s replacement type is not named"
        for name, hint in REMOVED_CRITERION_TYPES.items():
            assert f"`{name}`" in rendered, f"removed type {name} is not documented in the reference"
            assert hint.split(".")[0] in rendered, f"{name}'s migration hint is not rendered"

    def test_common_base_fields_are_not_repeated_per_criterion(self):
        from tests.lint.plugin_reference import render_criteria

        common_section, _, per_criterion = render_criteria().partition("## Criterion types")
        # Match the table-row form the render emits a field NAME in, rather than a bare
        # token, so a criterion whose prose happens to mention "weight" cannot fail this
        # spuriously. A table row is now the ONLY form a field name is emitted in —
        # optional fields are described rows too — so this one assertion covers required
        # and optional alike.
        for field in ("weight", "pass_threshold", "suite_thresholds", "stop_early"):
            assert f"`{field}`" in common_section, f"{field} must be documented once, in Common fields"
            assert f"| `{field}` |" not in per_criterion, (
                f"{field} is inherited and must not be repeated in a per-criterion section"
            )

    def test_every_criterion_field_has_a_description(self):
        # Optional fields now render their description into a table cell, so a field
        # declared without one produces a silently empty cell in the shipped
        # reference. Union-driven: a 15th criterion is covered with zero edits here.
        from coder_eval.models import BaseSuccessCriterion, LiveSuccessCriterion
        from tests.lint.plugin_reference import _DISCRIMINATOR, _VARIANTS

        missing = [
            f"{cls.__name__}.{name}"
            for cls in (*_VARIANTS, BaseSuccessCriterion, LiveSuccessCriterion)
            for name, info in cls.model_fields.items()
            if name != _DISCRIMINATOR and not (info.description or "").strip()
        ]
        assert not missing, (
            f"criterion field(s) with no `description=`: {sorted(set(missing))} — the bundled "
            "reference renders every field's description, so a missing one ships as an empty cell"
        )

    def test_optional_fields_render_with_descriptions(self):
        # Read the expected text off the model rather than hardcoding it, so a
        # reworded description does not fail this test spuriously.
        from coder_eval.models import CommandExecutedCriterion
        from tests.lint.plugin_reference import _cell, render_criteria

        described = _cell(CommandExecutedCriterion.model_fields["require_success"].description or "")
        assert described, "require_success lost its description — the fixture for this test is gone"
        assert f"| `require_success` | {described} |" in render_criteria(), (
            "an optional field must render as a described table row, not a bare name"
        )

    def test_render_is_deterministic(self):
        from tests.lint.plugin_reference import render_criteria

        assert render_criteria() == render_criteria()

    def test_docstringless_criterion_renders(self):
        from typing import Literal

        from coder_eval.models import BaseSuccessCriterion
        from tests.lint.plugin_reference import render_criteria

        class Undocumented(BaseSuccessCriterion):
            type: Literal["undocumented"] = "undocumented"

        Undocumented.__doc__ = None
        rendered = render_criteria([Undocumented])
        assert "### `undocumented`" in rendered
        assert "No fields beyond the common ones." in rendered

    def test_pipe_in_description_is_escaped(self):
        from typing import Literal

        from pydantic import Field

        from coder_eval.models import BaseSuccessCriterion
        from tests.lint.plugin_reference import render_criteria

        class Piped(BaseSuccessCriterion):
            """A criterion whose required field description contains a pipe."""

            type: Literal["piped"] = "piped"
            mode: str = Field(description="one of: a | b | c")

        row = next(line for line in render_criteria([Piped]).splitlines() if line.startswith("| `mode`"))
        # Escaped pipes keep the row at two columns: leading, separator, trailing.
        assert row.count("|") - row.count(r"\|") == 3, row

    def test_write_is_idempotent(self, tmp_path: Path):
        from tests.lint.plugin_reference import check, write

        write(tmp_path)
        first = (tmp_path / "plugins/coder-eval/reference/criteria.md").read_text(encoding="utf-8")
        write(tmp_path)
        assert (tmp_path / "plugins/coder-eval/reference/criteria.md").read_text(encoding="utf-8") == first
        assert check(tmp_path) == {}

    def test_render_carries_no_types_or_defaults(self):
        # The render deliberately emits neither field types nor defaults (see the
        # module docstring); these tokens are what a reintroduced column would leak.
        from tests.lint.plugin_reference import render_criteria

        rendered = render_criteria()
        assert "PydanticUndefined" not in rendered
        assert "typing.Optional" not in rendered

    def test_drift_is_detected(self, tmp_path: Path):
        from tests.lint.plugin_reference import check, write

        write(tmp_path)
        target = tmp_path / "plugins/coder-eval/reference/criteria.md"
        target.write_text(target.read_text(encoding="utf-8").replace("### `file_exists`", "### `tampered`"))
        assert str(target) in check(tmp_path)


@pytest.mark.lint
class TestCE038NestedForbidExtras:
    """CE038: `extra="forbid"` must reach the nested models it appears to protect.

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
        if depth >= TestCE038NestedForbidExtras.MAX_ANNOTATION_DEPTH:
            return []
        return [
            m for arg in typing.get_args(annotation) for m in TestCE038NestedForbidExtras._nested_models(arg, depth + 1)
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
class TestCE031DeadConfigFields:
    """CE031 — a behavior-driving config field must be read somewhere in src/.

    Guards the dead-config class that SimulationConfig.parallel_trials was: a
    field users set in a task YAML that no code reads by name, so it silently
    does nothing. Scans the whole src/ tree for attribute reads, so it lives
    here rather than in the per-file AST runner.
    """

    REPO_ROOT = Path(__file__).parent.parent
    SRC = REPO_ROOT / "src"

    def test_no_dead_config_fields(self):
        from tests.lint.dead_config_fields import find_dead_config_fields

        findings = find_dead_config_fields(self.SRC)
        assert not findings, (
            "\nConfig field(s) that no code in src/ reads by name — dead config a user could set "
            "with no effect. Wire the field to real behavior, remove it, or (if it is consumed only "
            "via serialization) add an EXEMPT entry with a reason in tests/lint/dead_config_fields.py:\n\n"
            + "\n".join(f"  {model}: {', '.join(fields)}" for model, fields in sorted(findings.items()))
        )

    def test_detects_a_dead_field(self):
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            wired: str = Field(default="")
            orphan: str = Field(default="")

        # `wired` is read as an attribute somewhere; `orphan` is not.
        assert dead_config_fields(Synthetic, consumed={"wired"}, exempt={}) == ["orphan"]

    def test_consumed_field_not_flagged(self):
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            live: str = Field(default="")

        assert dead_config_fields(Synthetic, consumed={"live"}, exempt={}) == []

    def test_exempt_field_not_flagged(self):
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            serialized_only: str = Field(default="")

        assert dead_config_fields(Synthetic, consumed=set(), exempt={"serialized_only": "read via model_dump"}) == []

    def test_would_catch_parallel_trials_shape(self):
        # A field named like the removed parallel_trials, absent from the consumed
        # set, must be reported — the exact regression this rule exists for.
        from pydantic import BaseModel, Field

        from tests.lint.dead_config_fields import dead_config_fields

        class Synthetic(BaseModel):
            parallel_trials: bool = Field(default=True)

        assert dead_config_fields(Synthetic, consumed={"n_trials", "max_turns"}, exempt={}) == ["parallel_trials"]

    def test_exemptions_reference_real_fields(self):
        # A stale EXEMPT entry (field renamed/removed) would silently mask a dead field.
        from tests.lint.dead_config_fields import CONSUMED_MODELS, EXEMPT

        by_name = {m.__name__: m for m in CONSUMED_MODELS}
        for model_name, fields in EXEMPT.items():
            assert model_name in by_name, f"EXEMPT names unregistered model {model_name!r}"
            real = set(by_name[model_name].model_fields)
            for field_name, reason in fields.items():
                assert field_name in real, f"EXEMPT[{model_name}] names non-field {field_name!r}"
                assert reason and reason.strip(), f"EXEMPT[{model_name}][{field_name}] has an empty reason"

    def test_registered_fields_are_actually_consumed_on_the_real_tree(self):
        # Belt: prove the attribute scan really finds known-live fields, so a
        # broken scanner (returning everything or nothing) can't pass silently.
        from tests.lint.dead_config_fields import consumed_attr_names

        consumed = consumed_attr_names(self.SRC)
        for name in ("n_trials", "max_usd", "stratify_field"):
            assert name in consumed, f"expected {name!r} to be read as an attribute in src/"


@pytest.mark.lint
class TestCE026ActionDocSurfaces:
    """CE026 — the Action's onboarding surfaces must be truthful and self-sufficient.

    The motivating bug: docs/CI_GATE.md said "there is nothing to install" above a
    copy-pasteable `uses:` step with no agent runtime, while the correcting
    prerequisite note sat 11 lines below and the tutorial's sibling snippet *did*
    show the steps. An integrator who copied it got a run that dies on a missing
    `claude` binary. Reasons over Markdown + YAML, so it lives here rather than in
    the AST-only runner (precedent: CE027-CE031).
    """

    REPO_ROOT = Path(__file__).parent.parent
    ACTION_YML = REPO_ROOT / "action.yml"
    PR_CHECKS = REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"

    def test_primary_action_snippets_show_agent_runtime_prereqs(self):
        from tests.lint.action_docs import default_doc_paths, find_missing_prereqs

        findings = find_missing_prereqs(default_doc_paths(self.REPO_ROOT))
        assert not findings, (
            "\nAction quickstart snippet(s) that a reader can copy but that omit the agent-runtime "
            "prerequisite steps:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_no_unqualified_zero_install_claims_near_action_snippets(self):
        from tests.lint.action_docs import default_doc_paths, find_unscoped_absolute_claims

        findings = find_unscoped_absolute_claims(default_doc_paths(self.REPO_ROOT))
        assert not findings, (
            "\nUnqualified zero-install absolute(s) beside an Action snippet — scope the claim to the "
            "channel it is about:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_marketplace_links_match_the_action_listing_name(self):
        from tests.lint.action_docs import action_listing_name, default_doc_paths, find_slug_mismatches

        listing = action_listing_name(self.ACTION_YML)
        findings = find_slug_mismatches(default_doc_paths(self.REPO_ROOT), listing)
        assert not findings, (
            f"\nMarketplace link/badge(s) inconsistent with action.yml `name: {listing}` — a rename "
            "404s every one of them:\n\n" + "\n".join(f"  {f}" for f in findings)
        )

    def test_plugin_skills_are_covered_by_the_doc_scan(self):
        # The `ci` skill emits an Action snippet users copy verbatim, so it must be
        # held to the same prerequisite standard as the docs pages.
        from tests.lint.action_docs import default_doc_paths

        ci_skill = PLUGIN_ROOT / "skills" / "ci" / "SKILL.md"
        scanned = {p.resolve() for p in default_doc_paths(self.REPO_ROOT)}
        assert ci_skill.resolve() in scanned, (
            f"{ci_skill} is outside default_doc_paths() — its Action snippet would go unchecked"
        )

    def test_ci_skill_snippet_shows_agent_runtime_prereqs(self):
        from tests.lint.action_docs import find_missing_prereqs

        findings = find_missing_prereqs([PLUGIN_ROOT / "skills" / "ci" / "SKILL.md"])
        assert not findings, (
            "\nThe ci skill emits a workflow without the agent-runtime prerequisite steps — a user "
            "who copies it gets a run that dies on a missing `claude` binary:\n\n"
            + "\n".join(f"  {f}" for f in findings)
        )

    def test_action_snippets_pass_only_real_action_inputs(self):
        from tests.lint.action_docs import action_input_names, default_doc_paths, find_unknown_action_inputs

        names = action_input_names(self.ACTION_YML)
        findings = find_unknown_action_inputs(default_doc_paths(self.REPO_ROOT), names)
        assert not findings, (
            "\nAction snippet(s) passing a `with:` key action.yml does not declare — GitHub ignores "
            "unknown inputs, so the copied step silently does less than the snippet promises:\n\n"
            + "\n".join(f"  {f}" for f in findings)
        )

    def test_action_input_names_reads_the_real_action(self):
        # Belt: prove the parser returns real inputs, so a broken reader (empty set)
        # can't make the clause above pass vacuously.
        from tests.lint.action_docs import action_input_names

        names = action_input_names(self.ACTION_YML)
        assert {"tasks", "junit-path", "env"} <= names, names

    def test_catches_an_unknown_action_input(self, tmp_path: Path):
        from tests.lint.action_docs import find_unknown_action_inputs

        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    tasks: t.yaml\n    junit: out.xml\n```\n",
            encoding="utf-8",
        )
        findings = find_unknown_action_inputs([page], {"tasks", "junit-path"})
        assert len(findings) == 1
        assert "`with: junit:`" in findings[0].message

    def test_finds_the_step_at_any_nesting_depth(self, tmp_path: Path):
        # Pages show the step as a whole workflow, a bare step list, or one step alone.
        from tests.lint.action_docs import find_unknown_action_inputs

        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\njobs:\n  eval:\n    steps:\n      - uses: UiPath/coder_eval@v0\n"
            "        with:\n          bogus: 1\n```\n",
            encoding="utf-8",
        )
        assert len(find_unknown_action_inputs([page], {"tasks"})) == 1

    def test_unparseable_or_unrelated_blocks_are_ignored(self, tmp_path: Path):
        from tests.lint.action_docs import find_unknown_action_inputs

        # A deliberate fragment next to a real reference must not raise or fire.
        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    tasks: t.yaml\n```\n\n"
            "```yaml\n  : : not: valid: yaml\n```\n\n"
            "```yaml\n- uses: actions/checkout@v6\n  with:\n    anything: goes\n```\n",
            encoding="utf-8",
        )
        assert find_unknown_action_inputs([page], {"tasks"}) == []

    def test_required_prereqs_match_the_dogfood_job(self):
        # The constant is pinned to the executable reference: the dogfood job proves
        # in CI that these steps are what a fresh runner needs before `uses: ./`.
        from tests.lint.action_docs import DOGFOOD_JOB, REQUIRED_PREREQ_TOKENS, dogfood_prereq_tokens

        tokens = dogfood_prereq_tokens(self.PR_CHECKS)
        for required in REQUIRED_PREREQ_TOKENS:
            assert any(required in token for token in tokens), (
                f"{required!r} is documented as a prerequisite but the {DOGFOOD_JOB} job no longer "
                f"runs it before `uses: ./` (job steps: {sorted(tokens)}). Update "
                "REQUIRED_PREREQ_TOKENS and the doc snippets together."
            )

    def test_catches_a_snippet_missing_prereqs(self, tmp_path: Path):
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text(
            "# Gate\n\n```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    tasks: t.yaml\n```\n",
            encoding="utf-8",
        )
        findings = find_missing_prereqs([page])
        assert len(findings) == 1
        assert "actions/setup-node" in findings[0].message

    def test_only_the_first_action_block_is_checked(self, tmp_path: Path):
        # Later single-input illustrations must stay quiet, or the rule becomes a nuisance.
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text(
            "```yaml\n"
            "- uses: actions/setup-node@v4\n"
            "- run: npm install -g @anthropic-ai/claude-code\n"
            "- uses: UiPath/coder_eval@v0\n"
            "```\n\n"
            "Score floor:\n\n"
            '```yaml\n- uses: UiPath/coder_eval@v0\n  with:\n    minimum-task-score: "0.8"\n```\n',
            encoding="utf-8",
        )
        assert find_missing_prereqs([page]) == []

    def test_prereq_skip_marker_opts_out(self, tmp_path: Path):
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text(
            "<!-- lint-skip: action-prereq: illustrative fragment -->\n```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        assert find_missing_prereqs([page]) == []

    def test_page_without_the_action_is_ignored(self, tmp_path: Path):
        from tests.lint.action_docs import find_missing_prereqs

        page = tmp_path / "page.md"
        page.write_text("```yaml\n- uses: actions/checkout@v6\n```\n", encoding="utf-8")
        assert find_missing_prereqs([page]) == []

    def test_catches_the_nothing_to_install_regression(self, tmp_path: Path):
        # The exact sentence this rule exists for.
        from tests.lint.action_docs import find_unscoped_absolute_claims

        page = tmp_path / "page.md"
        page.write_text(
            "you reference it by repo path — there is nothing to install:\n\n"
            "```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        findings = find_unscoped_absolute_claims([page])
        assert len(findings) == 1
        assert "nothing to install" in findings[0].message

    def test_scoped_claim_is_allowed(self, tmp_path: Path):
        from tests.lint.action_docs import find_unscoped_absolute_claims

        page = tmp_path / "page.md"
        page.write_text(
            "you reference it by repo path — there is no Marketplace install step:\n\n"
            "```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        assert find_unscoped_absolute_claims([page]) == []

    def test_claim_far_from_a_snippet_is_ignored(self, tmp_path: Path):
        from tests.lint.action_docs import find_unscoped_absolute_claims

        page = tmp_path / "page.md"
        page.write_text(
            "there is nothing to install\n" + "\n" * 40 + "```yaml\n- uses: UiPath/coder_eval@v0\n```\n",
            encoding="utf-8",
        )
        assert find_unscoped_absolute_claims([page]) == []

    def test_catches_a_renamed_listing(self, tmp_path: Path):
        from tests.lint.action_docs import find_slug_mismatches

        page = tmp_path / "page.md"
        page.write_text("[coder_eval](https://github.com/marketplace/actions/coder_eval)\n", encoding="utf-8")
        assert find_slug_mismatches([page], "coder_eval") == []
        assert len(find_slug_mismatches([page], "coder eval x")) == 1

    def test_shields_label_must_decode_to_the_listing_name(self, tmp_path: Path):
        from tests.lint.action_docs import decode_shields_label, find_slug_mismatches

        # A single `_` renders as a space, which is why the badge needs the doubled form.
        assert decode_shields_label("coder__eval") == "coder_eval"
        assert decode_shields_label("coder_eval") == "coder eval"

        page = tmp_path / "page.md"
        page.write_text(
            "[![m](https://img.shields.io/badge/marketplace-coder_eval-2ea44f.svg)](https://x)\n",
            encoding="utf-8",
        )
        findings = find_slug_mismatches([page], "coder_eval")
        assert len(findings) == 1
        assert "displays as 'coder eval'" in findings[0].message


@pytest.mark.lint
class TestCE032CriteriaPathSeam:
    """CE032 fires on a criterion checker joining a path onto sandbox_dir."""

    CRITERION_FILE = "/repo/src/coder_eval/criteria/file_check.py"

    @staticmethod
    def _run(src: str, filepath: str):
        import ast

        from tests.lint.rules.ce032_criteria_path_seam import CriteriaPathSeam

        return CriteriaPathSeam(filepath).check(ast.parse(src))

    def test_flags_direct_join(self):
        violations = self._run("agent_path = sandbox.sandbox_dir / criterion.agent_file", self.CRITERION_FILE)
        assert len(violations) == 1
        assert "bypassing the path seam" in violations[0].message

    def test_flags_join_with_literal(self):
        assert self._run("p = self.sandbox.sandbox_dir / 'solution.py'", self.CRITERION_FILE)

    def test_allows_reading_the_root_without_joining(self):
        assert not self._run("if not sandbox.sandbox_dir:\n    return None", self.CRITERION_FILE)

    def test_allows_the_seam(self):
        assert not self._run("content = sandbox.get_file_content(criterion.path)", self.CRITERION_FILE)

    def test_scoped_to_the_criteria_package(self):
        # Sandbox itself, and non-criteria consumers, legitimately join onto the root.
        assert not self._run(
            "target = self.sandbox_dir / path",
            "/repo/src/coder_eval/sandbox.py",
        )
        assert not self._run(
            "target = sandbox.sandbox_dir / path",
            "/repo/src/coder_eval/orchestrator.py",
        )


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

    ROOT = Path(__file__).parent.parent

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
        sorted(p for p in (Path(__file__).parent.parent / "tasks").rglob("*.yaml") if p.name != "metadata.yaml"),
        ids=lambda p: p.relative_to(Path(__file__).parent.parent).as_posix(),
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


# Every surface that teaches an agent to read a run record by hand. Each tells it to
# build a compact per-task summary with `jq`, so each hardcodes a `task.json` field
# vocabulary that nothing otherwise ties to the models. A tuple, not a single path:
# this list held two entries until the repo-local `coder-eval-run-analysis` twin was
# retired in favour of the shipped skill, and a second consumer would drift the same way.
RUN_RECORD_CONSUMERS = ("plugins/coder-eval/skills/analyze/SKILL.md",)

# Words that are jq syntax or builtins rather than record fields.
# fmt: off
_JQ_WORDS = frozenset([
    "and", "or", "not", "if", "then", "elif", "else", "end", "null", "true", "false",
    "length", "all", "any", "select", "map", "keys", "add", "empty", "type", "tostring",
    "tonumber", "join", "split", "sort", "sort_by", "group_by", "unique", "min", "max",
    "reduce", "foreach", "try", "catch", "def", "as", "import", "include", "limit",
    "first", "last", "range", "floor", "ceil", "has", "in", "with_entries", "to_entries",
    "from_entries", "flatten", "contains", "startswith", "endswith", "test", "capture",
])
# fmt: on

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A dot NOT preceded by an identifier char opens a new path, so this captures the HEAD
# of each chain only: `.total_token_usage.input_tokens` yields `total_token_usage`.
_PATH_HEAD = re.compile(r"(?<![A-Za-z0-9_)\]])\.([A-Za-z_][A-Za-z0-9_]*)")

# `analyze` carries a `| Current runs | Older runs | Where |` table, because a run written
# before the rename spells two of these differently. The third cell is load-bearing: only
# a TOP-LEVEL key is a model field with a `validation_alias`, so only those rows can be
# checked against the schema. `max_iterations` is a key inside the free-form `task_config`
# dict and appears in no `AliasChoices` at all — a guard that swept the whole table would
# be unsatisfiable against the very prose it guards, and would get "fixed" by deleting the
# row. Keeping the scoping visible in the shipped table rather than hidden in this file is
# the point: an author adding a row has to say which kind of key it is.
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


@pytest.mark.lint
class TestRunRecordFieldVocabulary:
    """Every task.json field the run-analysis surfaces name must exist on the models.

    `jq` returns `null` for a key that does not exist instead of failing, so a wrong
    field name does not surface as an error — it produces a table of nulls that reads
    like a run with nothing in it. Both surfaces shipped six such names at once
    (`turns`, `total_tokens`, `assistant_turn_count`, `max_turns`, `criteria_count`,
    `all_criteria_perfect`), and the failure is worst exactly where the instruction
    applies: the >20-task path, where the agent is explicitly told NOT to fall back to
    reading whole files.

    Scoped deliberately: only the fenced blocks that mention `success_criteria_results`
    (the summary-extraction programs), and only the HEAD of each dotted path. Deeper
    segments are not checked because `task_config` is a free-form dict, so
    `.task_config.resolved.run_limits.max_turns` is unverifiable from the schema. The
    allowed set unions the run-level and criterion-level models rather than tracking
    which scope each expression sits in — a weakening that still catches every name
    above, since none of them exists on either model.
    """

    @staticmethod
    def _known_fields() -> set[str]:
        from coder_eval.models import ClassificationCriterionResult, CriterionResult, EvaluationResult

        return {
            name
            for model in (EvaluationResult, CriterionResult, ClassificationCriterionResult)
            for name in model.model_json_schema(mode="serialization").get("properties", {})
        }

    @staticmethod
    def _summary_blocks(text: str) -> list[str]:
        blocks, current, inside = [], [], False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                if inside:
                    blocks.append("\n".join(current))
                current, inside = [], not inside
                continue
            if inside:
                current.append(line)
        return [b for b in blocks if "success_criteria_results" in b]

    @pytest.mark.parametrize("relpath", RUN_RECORD_CONSUMERS)
    def test_documented_task_json_fields_exist_on_the_models(self, relpath: str):
        doc = Path(__file__).parent.parent / relpath
        blocks = self._summary_blocks(doc.read_text(encoding="utf-8"))
        assert blocks, (
            f"{relpath}: no fenced block mentioning `success_criteria_results` — either the "
            "summary-extraction program was removed or renamed, and this guard is now inert"
        )
        known = self._known_fields()
        for block in blocks:
            unknown = sorted(_record_fields_referenced(block) - known)
            assert not unknown, (
                f"{relpath}: {unknown} are read off a run record but exist on neither "
                "EvaluationResult nor CriterionResult. jq yields null for a missing key, so this "
                "ships as a summary full of nulls rather than an error. Check the real field name "
                "(turn records are `iterations`; tokens and cost live under `total_token_usage`; "
                "the turn cap under `task_config.resolved.run_limits`; a criterion's type is "
                "`criterion_type` and it passes when `score >= pass_threshold`)."
            )

    def test_legacy_keys_named_by_analyze_are_real_model_aliases(self):
        # The skill now tells the agent that older runs spell two keys differently, which
        # is the opposite failure from the one above: not a name that never existed, but a
        # name that existed and was renamed. A wrong legacy name is just as silent — jq
        # yields null for it too — so every legacy TOP-LEVEL key the skill names must be a
        # name the loader genuinely still accepts. Derived from `AliasChoices` on the model,
        # so a third generation is one model edit and this guard follows it.
        from pydantic import AliasChoices

        from coder_eval.models import EvaluationResult

        accepted = {
            choice
            for info in EvaluationResult.model_fields.values()
            if isinstance(info.validation_alias, AliasChoices)
            for choice in info.validation_alias.choices
            if isinstance(choice, str)
        }
        skill = PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md"
        named = _legacy_top_level_keys(skill.read_text(encoding="utf-8"))
        assert named, (
            f"{skill}: no `| … | … | {_TOP_LEVEL_CELL} |` row — either the two-generation "
            "table was removed or its third cell was reworded, and this guard is now inert"
        )
        unknown = sorted(named - accepted)
        assert not unknown, (
            f"{skill} presents {unknown} as legacy top-level key(s), but the loader accepts "
            f"only {sorted(accepted)} (read off EvaluationResult's validation_alias). An "
            "invented legacy name reads back as null exactly like a current one would."
        )

    def test_analyze_does_not_deny_the_legacy_key_absolutely(self):
        # The skill used to say flatly "There is no top-level `turns`", which is true of
        # current runs and false of anything written before the rename — so an agent
        # reading a real older run was told its correct extraction was wrong. The four
        # OTHER names in that sentence were never top-level in any generation and were
        # denied on purpose; rewriting the sentence must not take them with it.
        text = _normalized(PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md")
        assert "There is no top-level `turns`" not in text, (
            "analyze denies the legacy `turns` key absolutely again — it is what runs "
            "written before the rename actually carry. Make it conditional on the file."
        )
        assert "`iterations`" in text, "analyze no longer names `iterations` as the current key"
        denial = text.partition("There is no top-level")[2].partition(".")[0]
        assert denial, "analyze lost the never-top-level denial sentence entirely"
        for name in ("`total_tokens`", "`total_cost_usd`", "`max_turns`", "`criteria_count`"):
            assert name in denial, (
                f"analyze stopped denying a top-level {name}, which is absent in EVERY "
                "generation — collateral damage from making the `turns` clause conditional"
            )

    def test_catches_the_vocabulary_that_actually_shipped(self):
        # Mutation guard: the exact block both files carried before this was fixed.
        original = """
        {task_id, final_status, weighted_score, duration_seconds, total_cost_usd,
         total_tokens, assistant_turn_count, max_turns, max_turns_exhausted,
         iteration_count, model_used, criteria_count, all_criteria_perfect,
         failed_criteria: [{type, description, score, error_excerpt}]}
        """
        caught = _record_fields_referenced(original) - self._known_fields()
        assert {"total_tokens", "assistant_turn_count", "criteria_count", "all_criteria_perfect"} <= caught


@pytest.mark.lint
class TestGeneratedSurfaceEngine:
    """The write/diff engine both generated-surface checkers (CE028, CE033) route through.

    Extracted from two near-verbatim copies. The copies had diverged in exactly one
    respect — only the plugin-reference one created a missing target (and its parents),
    because it was the only surface whose file might not exist yet. The shared engine
    keeps that more general behaviour, so these tests pin it for both callers.
    """

    def test_write_all_creates_missing_targets_including_parents(self, tmp_path: Path):
        from tests.lint.generated import write_all

        target = tmp_path / "nested" / "deeper" / "out.md"
        assert write_all({target: "body\n"}) == [target]
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_write_all_leaves_an_already_current_file_untouched(self, tmp_path: Path):
        from tests.lint.generated import write_all

        target = tmp_path / "out.md"
        write_all({target: "body\n"})
        before = target.stat().st_mtime_ns
        assert write_all({target: "body\n"}) == [target]
        assert target.stat().st_mtime_ns == before, "an unchanged file must not be rewritten"

    def test_diff_all_is_empty_when_disk_matches(self, tmp_path: Path):
        from tests.lint.generated import diff_all

        target = tmp_path / "out.md"
        target.write_text("body\n", encoding="utf-8")
        assert diff_all({target: "body\n"}) == {}

    def test_diff_all_reports_a_missing_file_as_full_drift(self, tmp_path: Path):
        from tests.lint.generated import diff_all

        # A generated file that was never written is drift, not a crash — the diff has to
        # render rather than raise, or `make lint` fails with a traceback instead of a fix.
        target = tmp_path / "out.md"
        findings = diff_all({target: "body\n"})
        assert list(findings) == [str(target)]
        assert "+body" in findings[str(target)]


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

    REPO_ROOT = Path(__file__).parent.parent

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
class TestCE036LiveVerdictContract:
    """CE036 — every live-observable criterion's `live_verdict` must be deterministic
    and monotonic (GitHub issue #61 item 2).

    `EarlyStopWatcher` latches verdicts, defers the fail-stop, and attributes pass-stop
    flips against the previous round — all correct only while `live_verdict` never
    contradicts an earlier decision and never varies for identical input. That contract
    was documented on `LiveVerdict`/`BaseCriterion.live_verdict` but unenforced: a third
    criterion implementing it non-monotonically would type-check, pass CE025, and
    silently corrupt the stop logic.

    Monotonicity over arbitrary Python is undecidable, so there is no sound static rule
    to write. This replays each criterion against every prefix of a recorded trajectory
    and asserts the property directly. The fixture table lives in
    `tests/lint/live_verdict_contract.py`; the coverage checks below are what stop it
    from decaying into a vacuous always-"undecided" replay.

    Honest limit (documented on the helper module too): this proves the contract on the
    trajectories an author supplied, not in general.
    """

    def test_real_criteria_honor_the_contract(self):
        """Every case for every live criterion type, replayed prefix by prefix."""
        from coder_eval.criteria import CriterionRegistry, init_criteria
        from tests.lint.live_verdict_contract import CASES, contract_violations

        init_criteria(validate=False)
        violations = [
            violation
            for criterion_type, cases in CASES.items()
            for case in cases
            for violation in contract_violations(CriterionRegistry.get_checker(criterion_type)(), case)
        ]
        assert not violations, "live_verdict contract violations:\n" + "\n".join(f"  {v}" for v in violations)

    def test_every_live_criterion_type_has_cases(self):
        """A new LiveSuccessCriterion with no fixtures enforces nothing — fail instead."""
        from tests.lint.live_verdict_contract import missing_case_types

        missing = missing_case_types()
        assert not missing, (
            "live-observable criterion types with no live_verdict contract cases: "
            + ", ".join(missing)
            + "\n\nAdd ContractCase entries to CASES in tests/lint/live_verdict_contract.py demonstrating "
            + "every polarity the type's instances can decide."
        )

    def test_fixtures_exercise_every_decidable_polarity(self):
        """Claiming a polarity is live-decidable but never demonstrating it is a gap."""
        from tests.lint.live_verdict_contract import polarity_gaps

        gaps = polarity_gaps()
        assert not gaps, "untested live_verdict decision paths:\n" + "\n".join(f"  {g}" for g in gaps)

    # --- The harness must actually fire; a green replay proves nothing on its own --- #

    @staticmethod
    def _positive_case(label: str, reaches: str):
        from coder_eval.models import SkillTriggeredCriterion
        from tests.lint.live_verdict_contract import ContractCase, cmd

        return ContractCase(
            label=label,
            criterion=SkillTriggeredCriterion(
                type="skill_triggered",
                description="synthetic",
                skill_name="alpha",
                expected_skill="alpha",
            ),
            commands=(
                cmd("Bash", {"command": "ls"}, sequence_number=0),
                cmd("Bash", {"command": "pwd"}, sequence_number=1),
            ),
            reaches=reaches,
        )

    @staticmethod
    def _checker(live_verdict_impl):
        from coder_eval.criteria.base import BaseCriterion

        class _Synthetic(BaseCriterion):
            criterion_type = "synthetic_live"

            def _check_impl(self, criterion, sandbox, reference_code=None, *, turn_records=None, context=None):
                raise NotImplementedError

            def live_verdict(self, criterion, turn_records):
                return live_verdict_impl(turn_records)

        return _Synthetic()

    def test_detects_a_non_monotonic_live_verdict(self):
        """Decides "pass" on a short prefix, then contradicts itself on a longer one."""
        from tests.lint.live_verdict_contract import contract_violations

        checker = self._checker(lambda records: "pass" if len(records[0].commands) == 1 else "undecided")
        violations = contract_violations(checker, self._positive_case("synthetic", "undecided"))
        assert any("NON-MONOTONIC" in v for v in violations), violations

    def test_detects_a_non_deterministic_live_verdict(self):
        """Same input, different answer — e.g. a wall-clock or RNG read."""
        from tests.lint.live_verdict_contract import contract_violations

        flips = iter(range(1000))
        checker = self._checker(lambda _records: "pass" if next(flips) % 2 else "undecided")
        violations = contract_violations(checker, self._positive_case("synthetic", "undecided"))
        assert any("NON-DETERMINISTIC" in v for v in violations), violations

    def test_detects_a_raising_live_verdict(self):
        """A raise mid-walk becomes ONE labeled violation (case + prefix length) and the
        walk continues — the later prefixes still replay, so the terminal "pass" here is
        judged normally and the raise is the only breach reported."""
        from tests.lint.live_verdict_contract import contract_violations

        def raises_mid_trajectory(records):
            n = len(records[0].commands)
            if n == 1:
                raise ValueError("boom")
            return "pass" if n == 2 else "undecided"

        checker = self._checker(raises_mid_trajectory)
        violations = contract_violations(checker, self._positive_case("synthetic", "pass"))
        assert len(violations) == 1, violations
        assert "RAISED" in violations[0] and "prefix length 1" in violations[0], violations

    def test_a_raise_on_the_final_prefix_does_not_stack_a_phantom_reaches_breach(self):
        """The terminal prefix has no verdict when it raises, so the `reaches` and
        polarity checks are skipped rather than judging the PREVIOUS prefix's stale
        verdict — which would report a second, derived breach on top of the real one."""
        from tests.lint.live_verdict_contract import contract_violations

        def raises_at_the_end(records):
            if len(records[0].commands) == 2:
                raise ValueError("boom")
            return "undecided"

        checker = self._checker(raises_at_the_end)
        # The case declares "pass"; the stale value from prefix 1 is "undecided", so the
        # unguarded comparison would append a phantom "reaches" violation here.
        violations = contract_violations(checker, self._positive_case("synthetic", "pass"))
        assert len(violations) == 1, violations
        assert "RAISED" in violations[0] and "prefix length 2" in violations[0], violations

    def test_detects_a_fixture_that_stopped_exercising_its_decision_path(self):
        """Fixture rot: the case claims a decision the trajectory no longer reaches."""
        from tests.lint.live_verdict_contract import contract_violations

        checker = self._checker(lambda _records: "undecided")
        violations = contract_violations(checker, self._positive_case("synthetic", "pass"))
        assert any("declares 'pass'" in v for v in violations), violations

    def test_detects_a_verdict_outside_the_instance_declared_polarities(self):
        """A positive skill_triggered instance can only live-pass; deciding "fail" means
        the watcher would treat a live trigger as inert."""
        from tests.lint.live_verdict_contract import contract_violations

        checker = self._checker(lambda _records: "fail")
        violations = contract_violations(checker, self._positive_case("synthetic", "fail"))
        assert any("live_decidable_polarities" in v for v in violations), violations

    def test_detects_a_live_type_with_no_cases(self):
        """The completeness check must fail on an empty table, not pass vacuously.

        Compared against the registry, not a hardcoded list: pinning today's type
        names would red THIS test the moment someone adds a live criterion — at
        exactly the moment `test_every_live_criterion_type_has_cases` is already
        failing them with the actionable message, pointing at the wrong file.
        """
        from tests.lint.live_verdict_contract import live_criterion_types, missing_case_types

        expected = sorted(live_criterion_types())
        assert expected, "the union walk found no live criterion types — the check would pass vacuously"
        assert missing_case_types({}) == expected

    def test_detects_an_all_undecided_fixture_set(self):
        """A type whose only case never decides claims coverage it does not have."""
        from tests.lint.live_verdict_contract import polarity_gaps

        gaps = polarity_gaps({"skill_triggered": (self._positive_case("synthetic", "undecided"),)})
        assert len(gaps) == 1
        assert "'pass'" in gaps[0]

    def test_real_criteria_hold_under_permutation(self):
        """Determinism + monotonicity must survive seeded reorderings of every case."""
        from coder_eval.criteria import CriterionRegistry, init_criteria
        from tests.lint.live_verdict_contract import CASES, permuted_violations

        init_criteria(validate=False)
        violations = [
            violation
            for criterion_type, cases in CASES.items()
            for case in cases
            for violation in permuted_violations(CriterionRegistry.get_checker(criterion_type)(), case)
        ]
        assert not violations, "live_verdict permutation violations:\n" + "\n".join(f"  {v}" for v in violations)

    def test_permutation_layer_detects_an_order_sensitive_verdict(self):
        """A recency bug (verdict read off the LATEST command) is monotone on an
        ordering that happens to end with the match — only a reordering exposes it.
        This is the exact bug shape the counterfactual experiment injected."""
        from coder_eval.models import SkillTriggeredCriterion
        from tests.lint.live_verdict_contract import ContractCase, cmd, contract_violations, permuted_violations

        def recency_verdict(records):
            commands = records[0].commands
            if commands and commands[-1].parameters.get("command") == "pwd":
                return "pass"
            return "undecided"

        checker = self._checker(recency_verdict)
        case = ContractCase(
            label="synthetic recency",
            criterion=SkillTriggeredCriterion(
                type="skill_triggered",
                description="synthetic",
                skill_name="alpha",
                expected_skill="alpha",
            ),
            commands=(
                cmd("Bash", {"command": "ls"}, sequence_number=0),
                cmd("Bash", {"command": "cat x"}, sequence_number=1),
                cmd("Bash", {"command": "pwd"}, sequence_number=2),
            ),
            reaches="pass",
        )
        # Clean on the authored ordering (it decides only on the final prefix)...
        assert not [v for v in contract_violations(checker, case) if "NON-MONOTONIC" in v]
        # ...caught under permutation.
        violations = permuted_violations(checker, case)
        assert any("NON-MONOTONIC" in v for v in violations), violations

    def test_permutation_renumbers_so_a_sequence_sorting_checker_is_still_probed(self):
        """The watcher hands `live_verdict` a trajectory sorted by `sequence_number`
        (`EarlyStopWatcher._collect_verdicts`), so a checker may legitimately sort by it
        too. If the shuffle left the original numbers attached, that sort would undo
        every permutation and this layer would silently probe nothing. Renumbering keeps
        the same recency bug detectable through the sort."""
        from coder_eval.models import SkillTriggeredCriterion
        from tests.lint.live_verdict_contract import ContractCase, cmd, permuted_violations

        def sorted_recency_verdict(records):
            commands = sorted(records[0].commands, key=lambda c: c.sequence_number)
            if commands and commands[-1].parameters.get("command") == "pwd":
                return "pass"
            return "undecided"

        checker = self._checker(sorted_recency_verdict)
        case = ContractCase(
            label="synthetic recency behind a sequence sort",
            criterion=SkillTriggeredCriterion(
                type="skill_triggered",
                description="synthetic",
                skill_name="alpha",
                expected_skill="alpha",
            ),
            commands=(
                cmd("Bash", {"command": "ls"}, sequence_number=0),
                cmd("Bash", {"command": "cat x"}, sequence_number=1),
                cmd("Bash", {"command": "pwd"}, sequence_number=2),
            ),
            reaches="pass",
        )
        violations = permuted_violations(checker, case)
        assert any("NON-MONOTONIC" in v for v in violations), violations


@pytest.mark.lint
class TestCE035SplitLabelsAllOrNothing:
    """CE035 — a dataset's split field must be on every row or on none, never on some.

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
        sorted(p for p in (Path(__file__).parent.parent / "tasks").rglob("*.yaml") if p.name != "metadata.yaml"),
        ids=lambda p: p.relative_to(Path(__file__).parent.parent).as_posix(),
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


@pytest.mark.lint
class TestCE036RowPromptsDoNotLeakWhatTheyGrade:
    """CE036 — a dataset row's prompt must not contain the value a criterion grades it on.

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
        sorted(p for p in (Path(__file__).parent.parent / "tasks").rglob("*.yaml") if p.name != "metadata.yaml"),
        ids=lambda p: p.relative_to(Path(__file__).parent.parent).as_posix(),
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
        # for a skill whose name reaches the length floor fails CE036 on its own engagement
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

    def test_ce036_exemption_list_matches_claude_md(self):
        # LEAK_LOCATOR_FIELDS is the single source; CLAUDE.md's CE036 sentence is derived.
        # Both directions, because the list already drifted once: CLAUDE.md named three of
        # the four fields the code exempted, and nothing noticed. The repo automates exactly
        # this class elsewhere (CE028 for the docs indexes, CE033 for the plugin reference).
        import re

        from coder_eval.leak_detection import LEAK_LOCATOR_FIELDS

        text = _normalized(Path(__file__).parent.parent / "CLAUDE.md")
        sentence = next((s for s in text.split(". ") if "Location fields" in s), None)
        assert sentence is not None, "CLAUDE.md no longer states CE036's exemption list"
        backticked = set(re.findall(r"`([a-z_]+)`", sentence))
        assert set(LEAK_LOCATOR_FIELDS) <= backticked, (
            f"CLAUDE.md's CE036 sentence omits {sorted(set(LEAK_LOCATOR_FIELDS) - backticked)}"
        )
        assert backticked <= set(LEAK_LOCATOR_FIELDS), (
            f"CLAUDE.md's CE036 sentence names {sorted(backticked - set(LEAK_LOCATOR_FIELDS))} as exempt, "
            f"which the rule does not exempt"
        )


def _dataset_task(rows: list[dict], *, prompt: str = "${row.id}", criteria=None, split_field: str = "split"):
    """A minimal dataset-backed task over inline rows, for the CE035/CE036 fixtures.

    One builder for both rules: CE035 needs varying rows, CE036 varying prompts AND
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


@pytest.mark.lint
class TestCE039ComputedClaims:
    """CE039: a prose surface's arithmetic must be checked by COMPUTING it, not by matching text.

    Every other prose sensor in this file asserts a string is PRESENT. None asserts that what it
    says is TRUE — and two false claims shipped past all of them in one change: "the statistic
    cannot be computed from one run dir" (it can) and a successive-halving cost "saving" that is
    arithmetically a premium.

    The registry lives in `tests/lint/computed_claims.py`; this class runs it, and adds the
    coverage rule that makes it a sensor CLASS rather than three more bespoke sensors — an
    arithmetic-bearing table no registered claim names is a failure. Like CE026-CE031 and
    CE033-CE038 it is a test class rather than a `BaseRule` in `tests/lint/runner.py`, which is an
    AST walk over `src/**/*.py`.
    """

    def test_registered_claims_hold(self, tmp_path: Path):
        from tests.lint.computed_claims import CLAIMS, evaluate_claims

        failures = evaluate_claims(tmp_path)
        assert not failures, (
            "a registered computed claim about the optimize surfaces no longer holds:\n  "
            + "\n  ".join(failures)
            + "\n\nEach claim is RECOMPUTED from the prose, so this is the prose being wrong rather "
            + "than a token having moved. Why each exists:\n  "
            + "\n  ".join(f"{c.id}: {c.why}" for c in CLAIMS)
        )

    def test_every_arithmetic_table_is_covered(self):
        # The coverage rule. A new table carrying arithmetic must be named by a claim that computes
        # it, or it ships unchecked and nothing says so.
        from tests.lint.computed_claims import COVERED_SURFACES, uncovered_tables

        uncovered = [entry for surface in COVERED_SURFACES for entry in uncovered_tables(surface)]
        assert not uncovered, (
            f"arithmetic-bearing tables no ComputedClaim covers: {uncovered}. Register a claim in "
            "tests/lint/computed_claims.py::CLAIMS whose `covers` names the table's header "
            "signature and whose `check` RECOMPUTES what the table asserts — a table nobody "
            "computes is exactly how a cost 'saving' that was really a premium shipped."
        )

    def test_arithmetic_tables_finds_exactly_the_known_tables(self):
        # Pinned so a widened predicate is a visible diff rather than a quietly noisier gate.
        from tests.lint.computed_claims import METHOD, SKILL, arithmetic_tables, table_signature

        found = {
            surface.name: [table_signature(t) for t in arithmetic_tables(surface.read_text(encoding="utf-8"))]
            for surface in (METHOD, SKILL)
        }
        assert found == {
            "optimize-method.md": ["Spend | Runs", "arms `A` | flat | halved | premium"],
            "SKILL.md": [
                "survivors gated `S` | Holm threshold | discordant rows needed at 8 paired rows | at 20",
                "rule | rows failing | headroom | ceiling | against the 0.0255 floor",
            ],
        }, f"the arithmetic-table predicate now finds {found}"

    def test_every_claim_surface_exists(self):
        # A claim pointing at a deleted file is a claim that silently stops checking.
        from tests.lint.computed_claims import CLAIMS

        missing = sorted(c.id for c in CLAIMS if not c.surface.is_file())
        assert not missing, f"claims registered against a file that no longer exists: {missing}"

    def test_the_matcher_catches_a_wrong_cell(self, tmp_path: Path):
        """The self-test. Runs the REAL halving checker against a hand-built table with a bad cell.

        Mirrors `_wrong_skill_count_offenders`: a sensor with no self-test can be reverted to a
        no-op with every test still green, which is the exact failure class this file exists to
        prevent. Nothing transient, nothing to revert — the wrong table is built here.
        """
        from tests.lint.computed_claims import TIMES, _check_halving_premium

        # TIMES is the surfaces' own multiplication sign, imported rather than typed: ruff's
        # RUF001 flags the literal as ambiguous, and the normalizer under test is what makes it
        # parseable in the first place.
        good = (
            "| arms `A` | flat | halved | premium |\n"
            "| --- | --- | --- | --- |\n"
            f"| 4 | `4 {TIMES} M_train` | `4 {TIMES} M_train` | **none** |\n"
            f"| 5 | `5 {TIMES} M_train` | `5.5 {TIMES} M_train` | `M_train/2` |\n"
        )
        assert _check_halving_premium(good, tmp_path) == []

        # A=5 halving costs half a train split; claiming **none** is the error v1 shipped.
        wrong = good.replace(
            f"| 5 | `5 {TIMES} M_train` | `5.5 {TIMES} M_train` | `M_train/2` |",
            f"| 5 | `5 {TIMES} M_train` | `5.5 {TIMES} M_train` | **none** |",
        )
        failures = _check_halving_premium(wrong, tmp_path)
        assert failures and any("premium" in f and "A=5" in f for f in failures), failures

    def test_the_sizing_matcher_catches_a_wrong_cell(self, tmp_path: Path):
        """The sizing claim's self-test, in the same shape as the halving one above.

        The table a user sizes a suite from is exactly the kind that goes stale plausibly: every
        cell is a small integer, and a wrong one reads like a right one. So the checker is run here
        against a hand-built table with one cell moved.
        """
        from tests.lint.computed_claims import _check_sizing_table

        good = (
            "| survivors gated `S` | Holm threshold | discordant rows needed at 8 paired rows | at 20 |\n"
            "| --- | --- | --- | --- |\n"
            "| 1 | `alpha/1` | 3 | 4 |\n"
            "| 5 | `alpha/5` | 4 | 5 |\n"
        )
        assert _check_sizing_table(good, tmp_path) == []

        # One row understated: a user reads "3 disagreeing rows is enough at S=5" and sizes for it.
        wrong = good.replace("| 5 | `alpha/5` | 4 | 5 |", "| 5 | `alpha/5` | 3 | 5 |")
        failures = _check_sizing_table(wrong, tmp_path)
        assert failures and any("S=5 at 8 rows" in f for f in failures), failures

        # And a threshold column that stops being alpha/S is caught before any count is compared —
        # otherwise a table could pass by having both halves wrong in agreement.
        skewed = good.replace("| 5 | `alpha/5` | 4 | 5 |", "| 5 | `alpha/2` | 4 | 5 |")
        assert any("not alpha/S" in f for f in _check_sizing_table(skewed, tmp_path))

    def test_a_wrong_split_symbol_is_reported_not_raised(self, tmp_path: Path):
        """A cell that PARSES but names an unexpected symbol must fail, not raise.

        Found by mutating the shipped table: renaming Stage C's `M_test` to `M_holdout` — the exact
        drift class this claim exists for — reached the evaluator and raised out of the lint test.
        A rule that crashes on the drift it is for reads as a broken sensor rather than a wrong
        table, so the caller catches it and names the symbol.
        """
        from tests.lint.computed_claims import METHOD, TIMES, _check_cost_table

        text = METHOD.read_text(encoding="utf-8")
        assert _check_cost_table(text, tmp_path) == []

        renamed = text.replace(f"`6 {TIMES} M_test`", f"`6 {TIMES} M_holdout`", 1)
        assert renamed != text, "the anchor moved — re-derive it from the cost table"
        failures = _check_cost_table(renamed, tmp_path)
        assert failures and any("M_holdout" in f for f in failures), failures

    def test_the_search_loop_may_not_be_priced_above_the_smallest_stage_a(self, tmp_path: Path):
        """Invariant 5's self-test, in the shape of the split-symbol one above.

        The search loop exists because it runs ONE arm; the plausible wrong edit is to co-run the
        incumbent "for a fair comparison", which doubles the phase and deletes its reason to exist.
        Priced at `4 x M_train` it would be dearer than a whole two-arm Stage A and the prose would
        still read as a saving, so the invariant is computed rather than matched.
        """
        from tests.lint.computed_claims import METHOD, TIMES, _check_cost_table

        text = METHOD.read_text(encoding="utf-8")
        assert _check_cost_table(text, tmp_path) == []

        overpriced = text.replace(f"`1 {TIMES} M_train`", f"`4 {TIMES} M_train`", 1)
        assert overpriced != text, "the anchor moved — re-derive it from the cost table's search row"
        failures = _check_cost_table(overpriced, tmp_path)
        assert failures and any("search round costs" in f for f in failures), failures

    def test_the_coverage_rule_fails_on_an_unregistered_table(self, tmp_path: Path):
        """The second self-test, and the committed replacement for "edit a shipped file to prove it".

        Three tables, only two of whose signatures a claim covers; the third must be reported by
        header.
        """
        from tests.lint.computed_claims import ComputedClaim, uncovered_tables

        surface = tmp_path / "surface.md"
        surface.write_text(
            "| a | b |\n| --- | --- |\n| `2 * M` | x |\n\n"
            "| c | d |\n| --- | --- |\n| `ceil(N/2)` | y |\n\n"
            "| e | f |\n| --- | --- |\n| `N+1` | z |\n",
            encoding="utf-8",
        )
        claims = [
            ComputedClaim(id="c", surface=surface, why="", covers=("a | b", "c | d"), check=lambda _t, _p: []),
        ]
        uncovered = uncovered_tables(surface, claims)
        assert len(uncovered) == 1 and "`e | f`" in uncovered[0], uncovered

    def test_evaluate_expression_rejects_a_call_it_does_not_whitelist(self):
        from tests.lint.computed_claims import evaluate_expression

        with pytest.raises(ValueError, match="other than ceil"):
            evaluate_expression("open('x')", {})
        with pytest.raises(ValueError, match="disallowed"):
            evaluate_expression("[1, 2]", {})

    def test_evaluate_expression_names_an_unbound_symbol(self):
        # Never a bare NameError from three frames down, where prose drift is indistinguishable
        # from a parser bug.
        from tests.lint.computed_claims import evaluate_expression

        with pytest.raises(ValueError, match="M_holdout"):
            evaluate_expression("2 * M_holdout", {"M_train": 1.0})

    def test_evaluate_expression_handles_ceil_and_unicode_multiply(self):
        from tests.lint.computed_claims import TIMES, evaluate_expression

        expr = f"(N+1) {TIMES} M_train/2 + ceil((N+1)/2) {TIMES} M_train"
        got = evaluate_expression(expr, {"N": 4.0, "M_train": 12.0})
        assert got == 66.0

    def test_the_execution_sign_claim_is_registered(self):
        # Deleting the claim would otherwise reduce coverage silently — `covers=()` means the
        # table-coverage rule cannot notice its absence, because it checks a SENTENCE.
        from tests.lint.computed_claims import CLAIMS

        assert "execution-sign-resolution" in {c.id for c in CLAIMS}

    def test_the_execution_sign_claim_fails_on_a_reworded_sentence(self, tmp_path: Path):
        """The presence half's self-test, in the style of the wrong-cell matchers above.

        A reworded sign sentence must FAIL loudly and be re-pinned deliberately, rather than
        quietly stop being checked.
        """
        from tests.lint.computed_claims import METHOD, _check_execution_sign_resolution

        text = METHOD.read_text(encoding="utf-8")
        assert _check_execution_sign_resolution(text, tmp_path / "real") == []

        # Every occurrence: the file states the rule twice, and leaving one behind would make the
        # sensor look green against a reworded sentence.
        assert text.count("candidate minus incumbent") >= 2, "the anchor moved — re-derive it"
        reworded = text.replace("candidate minus incumbent", "the candidate's score less the incumbent's")
        failures = _check_execution_sign_resolution(reworded, tmp_path / "reworded")
        assert failures and any("candidate minus incumbent" in f for f in failures), failures

    def test_the_execution_sign_claim_fails_on_a_reversed_verdict(self, tmp_path: Path, monkeypatch):
        """The behavioural half's self-test, driving the REAL claim function.

        A claim whose behavioural half was reverted to `return []` would pass every other test in
        this class. So `_exec_gate` — which the claim imports at call time — is swapped for a
        wrapper that negates `mean_diff` on one declaration order only: exactly the shape a gate
        reading `first_declared - second_declared` produces, and exactly the defect the method
        file says promotes the arm that lost.
        """
        import tests.test_optimize_gate as gate_tests
        from tests.lint.computed_claims import METHOD, _check_execution_sign_resolution

        text = METHOD.read_text(encoding="utf-8")
        real_exec_gate = gate_tests._exec_gate

        def _sign_blind(run_dir, **kwargs):
            verdict = real_exec_gate(run_dir, **kwargs)
            # The claim builds each arm's fixture UNDER a named parent, so the marker is in the
            # path rather than in `run_dir.name` (which is the builder's own "round1-gate").
            if "declared-candidate-first" in run_dir.as_posix() and verdict.mean_diff is not None:
                return verdict.model_copy(update={"mean_diff": -verdict.mean_diff})  # CE048 scans src/ only
            return verdict

        monkeypatch.setattr(gate_tests, "_exec_gate", _sign_blind)
        failures = _check_execution_sign_resolution(text, tmp_path / "blind")
        assert failures, "the claim's behavioural half did not notice a sign-blind gate"
        assert any("candidate first" in f for f in failures), failures
        assert any("disagree" in f for f in failures), failures

    def test_parse_markdown_tables_skips_fenced_blocks(self):
        from tests.lint.computed_claims import parse_markdown_tables

        text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```\n| x | y |\n| --- | --- |\n| 3 | 4 |\n```\n"
        tables = parse_markdown_tables(text)
        assert [t.header for t in tables] == [["a", "b"]]


@pytest.mark.lint
class TestCE044EvaluatorDispatch:
    """CE044 — the restricted evaluator's operator whitelist and its dispatch must agree.

    `computed_claims.evaluate_expression` shipped with `case _: return lhs / rhs`, so widening
    `_ALLOWED_OPS` by one line would have made it compute a cost-table cell with DIVISION and
    report the result as true — in the one sensor class whose entire purpose is catching
    arithmetic that lies.

    The scanner lives in `tests/lint/evaluator_dispatch.py`; its boundary is stated there. Wired
    as a test class rather than a `BaseRule` because its subject is a file under `tests/`, and the
    `ALL_RULES` sweep runs over `src/` only.
    """

    CLAIMS_MODULE: ClassVar[Path] = Path(__file__).parent / "lint" / "computed_claims.py"

    def test_the_real_evaluator_has_no_dispatch_gap(self) -> None:
        from tests.lint.evaluator_dispatch import dispatch_gaps

        gaps = dispatch_gaps(self.CLAIMS_MODULE)
        assert not gaps, "the whitelist and the dispatch have drifted:\n  " + "\n  ".join(gaps)

    def test_it_reads_the_real_whitelist(self) -> None:
        # Anti-vacuity: the rule reads `_ALLOWED_OPS` and `evaluate_expression` BY NAME, so a
        # rename would leave it checking an empty set against an empty set.
        from tests.lint.evaluator_dispatch import whitelisted_ops

        allowed = whitelisted_ops(ast.parse(self.CLAIMS_MODULE.read_text(encoding="utf-8")))
        assert "Div" in allowed and len(allowed) >= 4, allowed

    def test_it_reports_a_renamed_whitelist_rather_than_passing(self, tmp_path: Path) -> None:
        # The vacuous-sensor failure mode: a rename must FAIL, never quietly check nothing.
        module = tmp_path / "renamed.py"
        module.write_text(
            textwrap.dedent(
                """
                import ast

                _OPERATORS = (ast.Add,)

                def evaluate_expression(src, env):
                    match src:
                        case ast.Add():
                            return 1.0
                    raise ValueError(src)
                """
            ),
            encoding="utf-8",
        )
        from tests.lint.evaluator_dispatch import dispatch_gaps

        gaps = dispatch_gaps(module)
        assert gaps and any("_ALLOWED_OPS" in g for g in gaps), gaps

    def test_catches_a_whitelisted_operator_with_no_case(self, tmp_path: Path) -> None:
        module = tmp_path / "widened.py"
        module.write_text(
            textwrap.dedent(
                """
                import ast

                _ALLOWED_OPS = (ast.Add, ast.Mod)

                def evaluate_expression(src, env):
                    match src:
                        case ast.Add():
                            return 1.0
                        case _:
                            raise ValueError(src)
                """
            ),
            encoding="utf-8",
        )
        from tests.lint.evaluator_dispatch import dispatch_gaps

        gaps = dispatch_gaps(module)
        assert len(gaps) == 1 and "Mod" in gaps[0], gaps

    def test_catches_a_returning_wildcard(self, tmp_path: Path) -> None:
        # The shipped defect, reproduced: a wildcard that RETURNS silently computes an unhandled
        # operator as division.
        module = tmp_path / "wildcard.py"
        module.write_text(
            textwrap.dedent(
                """
                import ast

                _ALLOWED_OPS = (ast.Add,)

                def evaluate_expression(src, env):
                    match src:
                        case ast.Add():
                            return 1.0
                        case _:
                            return 2.0
                """
            ),
            encoding="utf-8",
        )
        from tests.lint.evaluator_dispatch import dispatch_gaps

        gaps = dispatch_gaps(module)
        assert len(gaps) == 1 and "case _" in gaps[0], gaps

    def test_an_operator_matched_only_by_an_outer_pattern_is_not_a_gap(self) -> None:
        """`USub` is matched by `case ast.UnaryOp(op=ast.USub(), ...)`, never by the inner dispatch.

        A scanner scoped to the operator `match` would report a false gap on it from day one, so
        patterns are collected across every nested `match` in the function.
        """
        from tests.lint.evaluator_dispatch import _evaluator, handled_ops

        fn = _evaluator(ast.parse(self.CLAIMS_MODULE.read_text(encoding="utf-8")))
        assert fn is not None
        assert "USub" in handled_ops(fn)


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

    REPO: ClassVar[Path] = Path(__file__).parent.parent

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

    SRC = Path(__file__).parent.parent / "src" / "coder_eval"
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


def _row_selector_fields() -> set[str]:
    from coder_eval.models import ROW_SELECTOR_FLAGS

    return set(ROW_SELECTOR_FLAGS)


_ROW_SELECTOR_FIELDS = _row_selector_fields()


@pytest.mark.lint
class TestCE043RowSelectorParity:
    """CE043 — `run` and `plan` must declare the same row-selector flags, described identically.

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
class TestEstimatorLedger:
    """The estimator-change protocol's pure predicate — the half that does not touch git.

    A rendered statistic can step for IDENTICAL data when an estimator or resample count changes,
    and nothing in a run artifact distinguishes that from a real change in the measurement. The
    `## Estimator changes` table in `docs/REPORT_SCHEMA.md` is where such a step becomes
    attributable, and the `estimator-protocol` CI job demands a row for a PR that causes one.

    Only `main()` shells out; everything asserted here is a pure function of a
    `path -> diff text` map, so the retro-validation below uses the REAL diff text from the commit
    that motivated the rule rather than checking out history.
    """

    BASE_DOC: ClassVar[str] = (
        "## Estimator changes\n\n| Date | Change | Constant / fixture | Observed step | PR / commit |\n"
        "| --- | --- | --- | --- | --- |\n| 2026-08-13 | a | b | c | d |\n\n## Next section\n"
    )

    @staticmethod
    def _with_extra_row(doc: str) -> str:
        return doc.replace("\n\n## Next section", "\n| 2026-08-16 | e | f | g | h |\n\n## Next section")

    def test_a_snapshot_change_requires_the_ledger(self) -> None:
        from tests.lint.estimator_ledger import check

        failures = check(["tests/_fixtures/report_snapshots/run_full.md"], {}, self.BASE_DOC, self.BASE_DOC)
        assert failures and "run_full.md" in failures[0], failures

    def test_a_constant_change_requires_the_ledger(self) -> None:
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        failures = check(
            [module],
            {module: "-BOOTSTRAP_RESAMPLES = 1000\n+BOOTSTRAP_RESAMPLES = 2000\n"},
            self.BASE_DOC,
            self.BASE_DOC,
        )
        assert failures and any("BOOTSTRAP_RESAMPLES" in f for f in failures), failures

    def test_a_gate_constant_change_requires_the_ledger(self) -> None:
        # The watch set is not `reports_stats`-only: every gate constant steps a rendered gate
        # number exactly the way BOOTSTRAP_RESAMPLES stepped a rendered CI.
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/optimize/gate.py"
        failures = check(
            [module],
            {module: "-MATERIALITY_FLOOR = 0.25\n+MATERIALITY_FLOOR = 0.10\n"},
            self.BASE_DOC,
            self.BASE_DOC,
        )
        assert failures and any("MATERIALITY_FLOOR" in f for f in failures), failures

    def test_a_new_ledger_row_satisfies_it(self) -> None:
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        assert (
            check(
                [module],
                {module: "-BOOTSTRAP_RESAMPLES = 1000\n+BOOTSTRAP_RESAMPLES = 2000\n"},
                self.BASE_DOC,
                self._with_extra_row(self.BASE_DOC),
            )
            == []
        )

    def test_an_unrelated_edit_to_the_page_does_not_satisfy_it(self) -> None:
        # Why the check is a ROW COUNT and not "the file was touched": the ledger lives in a busy
        # page, and a typo fix three sections away would otherwise clear the gate.
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        edited = self.BASE_DOC.replace("## Next section", "## Next section (renamed)")
        assert edited != self.BASE_DOC
        assert check([module], {module: "+BOOTSTRAP_RESAMPLES = 2000\n"}, self.BASE_DOC, edited)

    def test_an_unrelated_change_is_clean(self) -> None:
        from tests.lint.estimator_ledger import check

        assert check(["src/coder_eval/reports.py"], {}, self.BASE_DOC, self.BASE_DOC) == []

    def test_a_comment_only_diff_in_reports_stats_is_clean(self) -> None:
        # `git diff -U0` hunks are context-free changed lines, so matching on the ASSIGNMENT
        # keeps a comment reflow beside the constant from firing.
        from tests.lint.estimator_ledger import check

        module = "src/coder_eval/reports_stats.py"
        hunk = "-# BOOTSTRAP_RESAMPLES is the one resample count.\n+# BOOTSTRAP_RESAMPLES: the one resample count.\n"
        assert check([module], {module: hunk}, self.BASE_DOC, self.BASE_DOC) == []

    def test_every_watched_constant_exists_on_its_module(self) -> None:
        """The anti-rename parity assertion — the single most important test in this phase.

        A renamed constant would make the diff scan match nothing and the job pass **silently**,
        which is worse than no job at all. It lives here rather than at module import because the
        CI job installs nothing and the checker must load without `coder_eval`.
        """
        import importlib

        from tests.lint.estimator_ledger import WATCHED_CONSTANTS

        missing = []
        for path, name in WATCHED_CONSTANTS:
            module_name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
            if not hasattr(importlib.import_module(module_name), name):
                missing.append(f"{module_name}.{name}")
        assert not missing, (
            f"{missing} no longer resolve. A renamed watched constant does not fail the "
            "estimator-protocol job — it makes the job match nothing and pass silently."
        )

    def test_every_watched_constants_real_source_line_still_matches(self) -> None:
        """`hasattr` is not enough: the DIFF SCAN is a regex, and a re-declaration can dodge it.

        `BOOTSTRAP_RESAMPLES: Final[int] = 2000` keeps `hasattr` true while changing the shape the
        scan matches — the same silent pass the parity test above exists to prevent, one layer
        down. So the pattern is run against each constant's real source line.
        """
        from tests.lint.estimator_ledger import WATCHED_CONSTANTS, _assignment_pattern

        repo = Path(__file__).parent.parent
        unmatched = []
        for path, name in WATCHED_CONSTANTS:
            source = (repo / path).read_text(encoding="utf-8")
            declaration = next((line for line in source.splitlines() if line.startswith(name)), None)
            if declaration is None or not _assignment_pattern(name).search(f"+{declaration}"):
                unmatched.append(f"{path}::{name}")
        assert not unmatched, (
            f"{unmatched} are declared in a shape the diff scan does not match — the job would "
            "see the change and say nothing."
        )

    def test_every_snapshot_directory_still_exists_and_holds_fixtures(self) -> None:
        """The fixture half's anti-rename guard, and it is not symmetrical with the constants'.

        `git diff --name-only` reports only a rename's POST-image path, so moving a fixture
        directory disables this half FOREVER while every unit test here — which hardcodes literal
        paths — stays green.
        """
        from tests.lint.estimator_ledger import SNAPSHOT_DIRS, SNAPSHOT_SUFFIXES

        repo = Path(__file__).parent.parent
        empty = [
            d
            for d in SNAPSHOT_DIRS
            if not any(p.suffix in SNAPSHOT_SUFFIXES for p in (repo / d).glob("*") if p.is_file())
        ]
        assert not empty, f"{empty} hold no pinned fixtures — moved, or renamed past the watch list?"

    def test_the_optimize_renders_are_watched(self) -> None:
        # The estimator-FORM blind spot's only backstop: `bootstrap_p_floor`'s value is rendered
        # into these fixtures, so a form change lands there even though no constant moved.
        from tests.lint.estimator_ledger import is_watched_snapshot

        assert is_watched_snapshot("tests/_fixtures/optimize_renders/activation_gate.md")
        assert is_watched_snapshot("tests/_fixtures/optimize_verdicts/activation_gate.json")
        assert not is_watched_snapshot("tests/_fixtures/report_snapshots/_snapshot.py")
        assert not is_watched_snapshot("tests/_fixtures/golden_streams/anything.md")

    def test_estimator_rows_reads_the_real_page(self) -> None:
        # Anti-vacuity: a broken parser returning 0 would make every row-count comparison pass.
        from tests.lint.estimator_ledger import LEDGER_DOC, estimator_rows

        page = (Path(__file__).parent.parent / LEDGER_DOC).read_text(encoding="utf-8")
        assert estimator_rows(page) >= 1

    def test_estimator_rows_finds_the_table_by_signature_not_by_position(self) -> None:
        # A second table added above the ledger inside the section must not retarget the count.
        from tests.lint.estimator_ledger import estimator_rows

        decoy = self.BASE_DOC.replace(
            "## Estimator changes\n\n",
            "## Estimator changes\n\n| x | y |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n\n",
        )
        assert estimator_rows(decoy) == 1

    def test_main_passes_and_fails_on_a_real_repository(self, tmp_path: Path) -> None:
        """`main()` is the merge-blocking half, so it is exercised against real git, not mocked.

        A fixture repo makes the plumbing this rule actually depends on observable: three-dot
        resolution through the merge base, `--diff-filter=M` on the fixture side, and `git show`
        of the base doc.
        """
        import os
        import subprocess

        from tests.lint.estimator_ledger import LEDGER_DOC, main

        def run(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        doc = tmp_path / LEDGER_DOC
        doc.parent.mkdir(parents=True)
        doc.write_text(self.BASE_DOC, encoding="utf-8")
        stats = tmp_path / "src" / "coder_eval" / "reports_stats.py"
        stats.parent.mkdir(parents=True)
        stats.write_text("BOOTSTRAP_RESAMPLES = 1000\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-qm", "base")

        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            run("checkout", "-qb", "feature")
            stats.write_text("BOOTSTRAP_RESAMPLES = 2000\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-qm", "bump")
            assert main("main") == 1, "a watched constant moved with no ledger row and the job passed"

            doc.write_text(self._with_extra_row(self.BASE_DOC), encoding="utf-8")
            run("add", "-A")
            run("commit", "-qm", "ledger")
            assert main("main") == 0, "the ledger gained a row and the job still failed"
        finally:
            os.chdir(cwd)

    def test_the_module_imports_under_a_bare_interpreter(self) -> None:
        """The CI job installs nothing, so the whole import chain must be stdlib-only.

        Asserted by importing it in a subprocess under `-S`, which skips site-packages entirely —
        so `pydantic` and an installed `coder_eval` are both unreachable, exactly as in the job.
        `-c` puts the cwd on `sys.path`, which is how the job's `python -m` finds `tests` too. One
        future `import pydantic` in `tests/__init__.py` would otherwise turn every PR red with a
        ModuleNotFoundError.
        """
        import subprocess
        import sys

        repo = Path(__file__).parent.parent
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import tests.lint.estimator_ledger"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_it_fires_on_the_historical_commit_that_motivated_it(self) -> None:
        """Retro-validation, with `b306a99`'s real inputs rather than a checkout of history.

        That commit set `BOOTSTRAP_RESAMPLES = 2000` and moved a snapshot's two CI upper bounds.
        BOTH signals must fire, or the rule was written against a defect it could not have caught.

        The two inputs are RESOLVED against the tree rather than typed as literals: the module and
        the fixture must still exist at those paths, or this is only a restatement of the two
        trigger tests above with different strings.
        """
        from tests.lint.estimator_ledger import check

        repo = Path(__file__).parent.parent
        module = "src/coder_eval/reports_stats.py"
        snapshot = "tests/_fixtures/report_snapshots/experiment_replicates.md"
        assert (repo / module).is_file() and (repo / snapshot).is_file(), (
            "the commit's own inputs have moved — this test no longer retro-validates anything"
        )
        # And the constant is still declared there, so the diff line below is a real shape.
        assert "BOOTSTRAP_RESAMPLES" in (repo / module).read_text(encoding="utf-8")

        failures = check(
            [module, snapshot],
            {module: "+BOOTSTRAP_RESAMPLES = 2000\n"},
            self.BASE_DOC,
            self.BASE_DOC,
        )
        assert any("BOOTSTRAP_RESAMPLES" in f for f in failures), failures
        assert any("experiment_replicates.md" in f for f in failures), failures

    def test_the_documented_watch_list_matches_the_code(self) -> None:
        """`docs/REPORT_SCHEMA.md` enumerates the watched constants; that list is derived here.

        Two surfaces spelling one set is the drift CE036/`LEAK_LOCATOR_FIELDS` already needed a
        two-way test for. Adding a constant to `WATCHED_CONSTANTS` without documenting it leaves a
        consumer reading a boundary the job no longer has.
        """
        from tests.lint.estimator_ledger import LEDGER_DOC, WATCHED_CONSTANTS

        page = (Path(__file__).parent.parent / LEDGER_DOC).read_text(encoding="utf-8")
        section = page.split("## Estimator changes", 1)[1].split("\n## ", 1)[0]
        undocumented = sorted({name for _module, name in WATCHED_CONSTANTS if f"`{name}`" not in section})
        # This matches the NAME only, and that is a stated limitation rather than an oversight:
        # `FLOOR_RESOLUTION` moved module and this section went on attributing it to the old one with
        # every assertion green. A module check was written and then removed for being unfailable —
        # the section legitimately names an old module inside a ledger row describing the move, and
        # every watched module is named somewhere in the section anyway, so neither a subset rule nor
        # a proximity rule distinguishes the two. Deferred in `.claude/harness-candidates.md`.
        assert not undocumented, (
            f"{undocumented} are watched by the estimator-protocol job but absent from "
            f"{LEDGER_DOC}'s boundary paragraph, which claims to name the watch set."
        )

    def test_the_failure_message_names_the_escape_hatch(self) -> None:
        # A row EDITED rather than added does not raise the count, so a pure-correction PR fails.
        # That is intended, and the message has to say what to do about it.
        from tests.lint.estimator_ledger import check

        failures = check(["tests/_fixtures/report_snapshots/run_full.md"], {}, self.BASE_DOC, self.BASE_DOC)
        assert any("step was zero" in f for f in failures), failures


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
        assert self._check(source, "tests/test_optimize_gate.py") == []

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

    def test_the_scope_moved_to_the_package_rather_than_widening_to_both(self) -> None:
        """The fail-open guard on the re-scope itself.

        A regex that matched the OLD flat filename as well would pass every other test here while
        proving nothing about the move — and CE053's failure mode is silence, so the negative half
        has to be asserted explicitly.
        """
        assert RunTreeReadersReconcile("src/coder_eval/optimize/fronts.py")._in_scope
        assert not RunTreeReadersReconcile("src/coder_eval/optimize_fronts.py")._in_scope

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

    def test_the_live_suppression_set_is_exactly_the_two_documented_ones(self) -> None:
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
        assert sorted(suppressed) == ["cost_quality_points", "load_and_pair"], suppressed
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
class TestCE046CliFlagsAreDocumented:
    """CE046 — every long `--flag` a CLI command declares must appear in `docs/USER_GUIDE.md`.

    The CE030 doc-parity family, one surface over: CE030 gates a model's fields against its guide
    section, and a CLI flag is the same contract with a different declaration site. An undocumented
    flag is a capability the user pays for and cannot find.

    The scan set is DERIVED from `coder_eval.cli.app`, and the only exclusion mechanism is Typer's
    `hidden=True`. There is deliberately no exemption map: "this command is not part of the
    user-facing surface" is already declared once, in `cli/__init__.py`, and a second hand-kept
    list would be that same fact spelled twice.

    **Boundary**, stated so a green run is not mistaken for a proof:

    - it pins DOCUMENTATION, never behaviour — a flag can be documented and do nothing;
    - short flags (`-j`, `-e`) are out of scope by construction, matching CE043;
    - a flag Typer DERIVES from the parameter name carries no `param_decls` and is invisible to
      it — the same blind spot CE043 declares, stated on `tests/lint/cli_flags.py`;
    - "documented" means the name appears anywhere in the guide, including inside a fenced
      example — the failure message names the flag table because that is where a row BELONGS,
      not because the check can tell one from the other.
    """

    GUIDE: ClassVar[Path] = Path(__file__).parent.parent / "docs" / "USER_GUIDE.md"

    def test_every_cli_flag_appears_in_the_user_guide(self) -> None:
        from tests.lint.cli_flags import documented_commands, undocumented_flags

        missing = undocumented_flags(documented_commands(), self.GUIDE.read_text(encoding="utf-8"))
        assert not missing, "\n  ".join(["undocumented CLI flags:", *missing])

    def test_the_scan_sees_the_real_commands(self) -> None:
        # Anti-vacuity: an empty command map or an empty flag set would make the check above pass
        # while reading nothing.
        from tests.lint.cli_flags import documented_commands

        commands = documented_commands()
        assert {"run", "plan"} <= set(commands), sorted(commands)
        assert "--split" in long_flags(commands["run"])

    def test_the_command_set_is_derived_not_hardcoded(self) -> None:
        # Compared against `app` directly. A hardcoded expected list here would reintroduce the
        # checklist the derivation exists to replace — `evaluate` and `aggregate` are easy to miss.
        from coder_eval.cli import app
        from tests.lint.cli_flags import documented_commands

        expected = {c.name or c.callback.__name__ for c in app.registered_commands if c.hidden is not True}
        callback = app.registered_callback
        if callback is not None and callback.callback is not None and callback.hidden is not True:
            expected.add(callback.callback.__name__)
        assert set(documented_commands()) == expected

    def test_a_hidden_command_is_not_scanned(self) -> None:
        # `_run-task-internal`'s `--input` / `--task-dir` are absent from the guide, correctly:
        # the command is `hidden=True`, so it never reaches the check and needs no exemption.
        from tests.lint.cli_flags import documented_commands

        assert "_run-task-internal" not in documented_commands()

    def test_it_catches_an_undocumented_flag(self) -> None:
        import typer

        from tests.lint.cli_flags import undocumented_flags

        def _stub(nowhere: str | None = typer.Option(None, "--nowhere")) -> None: ...

        missing = undocumented_flags({"stub": _stub}, "a guide that mentions no such flag")
        assert len(missing) == 1 and "--nowhere" in missing[0], missing

    def test_a_flag_that_prefixes_another_is_not_auto_satisfied(self) -> None:
        """`--sample` is a prefix of `--sample-per-stratum`, and both are real flags.

        Under a bare substring test this check CANNOT FAIL for `--sample`: deleting every mention
        of it from the guide leaves the longer sibling satisfying the shorter one. Measured on the
        real guide, so this is the sensor's own blind spot pinned rather than a hypothetical.
        """
        from tests.lint.cli_flags import documented_commands, undocumented_flags

        guide = self.GUIDE.read_text(encoding="utf-8")
        without_sample = re.sub(r"--sample(?![\w-])", "--smpl", guide)
        assert "--sample-per-stratum" in without_sample, "the longer sibling must survive the edit"

        missing = undocumented_flags(documented_commands(), without_sample)
        assert missing and all("--sample`" in m or "--sample " in m for m in missing), missing

    def test_a_boolean_pair_documented_with_spaces_still_counts(self) -> None:
        """`--preserve/--no-preserve` arrives as ONE unspaced string; the guide writes it spaced.

        Matching the raw decl would fail on a CORRECTLY documented flag and demand an edit that
        makes the guide worse — so the decl is split on `/` and each bare name matched alone.
        """
        import typer

        from tests.lint.cli_flags import undocumented_flags

        def _stub(preserve: bool = typer.Option(True, "--preserve/--no-preserve")) -> None: ...

        spaced = "the sandbox is kept unless `--preserve / --no-preserve` says otherwise"
        assert undocumented_flags({"stub": _stub}, spaced) == []
        assert undocumented_flags({"stub": _stub}, "only `--preserve` here") != []


@pytest.mark.lint
class TestCE057OutcomePromptsDoNotLeakTheirExpectations:
    """CE057 — an outcome row's prompt must not contain a value its EXPECTATIONS grade it on.

    CE036's blind spot, one indirection over. An outcome suite's marking scheme does not live on
    any criterion: the criterion is a `run_command` naming a script, and every string the row is
    actually graded on sits in `outcome-grader/expectations/<row id>.json`. So the suite whose
    scores an optimization round spends real money on is exactly the one CE036 cannot see into.

    A leak here is worse than an ordinary one, for the reason the whole plan exists: a prompt that
    supplies its own answer scores well whether or not the behaviour under test happened, and in an
    A/B an arm that DELETED that behaviour still passes. It biases every arm equally, so no
    comparison downstream can reveal it.

    **Class-wired, not a `BaseRule`.** Its subject is a JSONL plus a directory of JSON, not one
    `.py` AST, so it follows CE043 / CE045 / CE052 rather than `tests/lint/rules/` — and nothing is
    added to `runner.py`, whose id-uniqueness assert covers `ALL_RULES` alone. The detection body
    lives in `tests/lint/outcome_prompt_leak.py`, a shared reader beside `skip_guards.py` and
    `task_yaml_discovery.py`, so the fixtures below and the repo scan exercise the SAME code.

    **Boundary**, stated so a green run is not mistaken for a proof: **verbatim only**, exactly as
    CE036. A prompt describing the graded behaviour in other words still needs a reviewer. And the
    spec carve-out is the whole difficulty — an outcome prompt legitimately states output paths,
    sheet names and column names, because "follow the user's spec literally" is itself graded.
    `LEAK_LOCATOR_FIELDS` is what draws that line, shared with CE036 rather than redrawn here.
    """

    REPO_ROOT: ClassVar[Path] = Path(__file__).parent.parent

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

        assert not (Path(__file__).parent / "lint" / "rules" / "ce057_outcome_prompt_leak.py").exists()
        assert not any(getattr(rule, "id", None) == "CE057" for rule in ALL_RULES)

    def test_ce056_is_still_reserved_and_unimplemented(self):
        # CE057 was chosen because CE056 is RESERVED with a promotion trigger that has not fired.
        # Renumbering onto it would silently retire that reservation.
        from tests.lint.runner import ALL_RULES

        assert not any(getattr(rule, "id", None) == "CE056" for rule in ALL_RULES)


@pytest.mark.lint
class TestPersistedCriterionResultFieldsAreDocumented:
    """Every `CriterionResult` base field must be named in `docs/REPORT_SCHEMA.md` § CriterionResult.

    CE030's doc-parity family one model over, and NOT a numbered rule: it is one derived assertion
    over one section rather than the configurable model→guide machinery CE030 owns, and adding a
    number would imply a generality it does not have.

    It exists because the gap was real and silent. `CriterionResult` is a PERSISTED consumer contract —
    `task.json` is read by the evalboard and by anything else pointed at a run tree — and CE030's set
    is `TaskDefinition` / `RunLimits` / `Dataset` / `SimulationConfig`, so adding `weight` to the model
    and forgetting the schema doc broke nothing and failed nothing. A reviewer caught it; this is what
    catches the next one.

    Scoped to the BASE model on purpose. The subclasses' own fields are documented in their own
    bullets in that section, and sweeping them in would make the assertion pass or fail on which
    bullet a name happens to appear in rather than on whether it is documented.
    """

    SCHEMA: ClassVar[Path] = Path(__file__).parent.parent / "docs" / "REPORT_SCHEMA.md"
    HEADING: ClassVar[str] = "### CriterionResult"

    def _section(self) -> str:
        text = self.SCHEMA.read_text(encoding="utf-8")
        start = text.index(self.HEADING)
        # To the next same-or-higher heading, so a later section cannot satisfy this one.
        rest = text[start + len(self.HEADING) :]
        end = min((rest.index(m) for m in ("\n### ", "\n## ") if m in rest), default=len(rest))
        return rest[:end]

    def test_every_base_field_is_named(self):
        from coder_eval.models import CriterionResult

        section = self._section()
        assert section.strip(), f"GAP: {self.HEADING} in docs/REPORT_SCHEMA.md is empty"
        fields = set(CriterionResult.model_fields)
        assert fields, "GAP: CriterionResult declares no fields"
        undocumented = sorted(name for name in fields if f"`{name}`" not in section)
        assert not undocumented, (
            f"docs/REPORT_SCHEMA.md's {self.HEADING} section does not name {undocumented}. "
            "task.json is a persisted consumer contract, and CE030's doc-parity family covers "
            "TaskDefinition / RunLimits / Dataset / SimulationConfig only — so a new field here is "
            "documented nowhere and nothing fails."
        )


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

    CHECKER: ClassVar[Path] = Path(__file__).parent.parent / "src" / "coder_eval" / "evaluation" / "checker.py"

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

        assert not (Path(__file__).parent / "lint" / "rules" / "ce058_mirrored_result_fields.py").exists()
        assert not any(getattr(rule, "id", None) == "CE058" for rule in ALL_RULES)
        # Anti-vacuity for the probe itself: it read `rule_id` for three releases, an attribute
        # `BaseRule` does not declare, so all three copies of this assertion could never fail.
        assert any(getattr(rule, "id", None) == "CE059" for rule in ALL_RULES), (
            "the attribute this probe reads is wrong again — it must be able to SEE a wired rule"
        )


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
        """The acceptance test for the 29 renames being COMPLETE, not merely started."""
        assert check_paths([SRC], [NoSiblingPrivateImports]) == []

    def test_the_rule_is_wired_into_all_rules(self) -> None:
        assert NoSiblingPrivateImports in ALL_RULES


@pytest.mark.lint
class TestCE052TemplateTasksLoad:
    """CE052 — every task YAML under `templates/` must load through the real `load_task`.

    A `@pytest.mark.lint` class rather than a `BaseRule`: it reasons over YAML trees and needs the
    loader itself, not one `.py` AST at a time — the same shape as CE035/CE036.

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

    TEMPLATES = Path(__file__).parent.parent / "templates"

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


class TestImportRulesResolveRelativeImports:
    """The parity table: every import rule must fire on BOTH spellings of one violation.

    Five rules matched on `node.module` and none read `node.level`, so `from ..models import X`
    — the way most of `src/` imports models — evaded all of them. The DIRECTION of that failure is
    what let it survive: a broken import rule reports ZERO violations, byte-identical to a clean
    tree, so `make lint` kept printing a clean bill of health while four rules saw nothing and
    CE041 had never fired in its life.

    One parametrized test over every import rule is what would have caught all four at once, and
    it is what stops a fifth joining them.
    """

    # (rule, file being scanned, absolute spelling, relative spelling) — one row per import rule.
    _RULES: ClassVar[list] = [
        (
            NoSubmoduleModelImports,
            "src/coder_eval/orchestration/task_loader.py",
            "from coder_eval.models.tasks import TaskDefinition",
            "from ..models.tasks import TaskDefinition",
        ),
        (
            ModelsLazyAgentImports,
            "src/coder_eval/models/tasks.py",
            "from coder_eval.agents.registry import AgentRegistry",
            "from ..agents.registry import AgentRegistry",
        ),
        (
            NoProxyShimImports,
            "src/coder_eval/orchestration/batch.py",
            "from coder_eval.proxy import thing",
            "from ..proxy import thing",
        ),
        (
            NoCliImportsInCore,
            "src/coder_eval/orchestration/batch.py",
            "from coder_eval.cli.console import console",
            "from ..cli.console import console",
        ),
        (
            NoModelDictSplat,
            "src/coder_eval/orchestration/task_loader.py",
            "from coder_eval.models import TaskDefinition\nTaskDefinition(**d)\n",
            "from ..models import TaskDefinition\nTaskDefinition(**d)\n",
        ),
        (
            # A REAL path under the package, because `resolved_module` stats for `__init__.py` at the
            # root and returns None for a path that does not exist — a synthetic one would make both
            # halves of this row pass for the wrong reason.
            NoSiblingPrivateImports,
            str(SRC / "coder_eval" / "optimize" / "gate.py"),
            "from coder_eval.optimize.load import _PairedRows",
            "from .load import _PairedRows",
        ),
    ]

    @pytest.mark.parametrize(("rule_cls", "path", "absolute", "relative"), _RULES, ids=[r[0].id for r in _RULES])
    def test_it_fires_on_both_spellings(self, rule_cls, path: str, absolute: str, relative: str) -> None:
        for label, source in (("absolute", absolute), ("relative", relative)):
            violations = rule_cls(path).check(ast.parse(source))
            assert len(violations) == 1, f"{rule_cls.id} saw nothing in the {label} spelling: {source!r}"
            assert violations[0].rule_id == rule_cls.id

    def test_ce041_actually_fires_on_a_relative_import_fixture(self) -> None:
        """Anti-vacuity. A rule reporting 0 for the RIGHT reason and one reporting 0 because it is
        broken are indistinguishable without a positive fixture — the whole lesson of this class.
        """
        source = "from ..models import NoiseFloor\nNoiseFloor(**payload)\n"
        violations = NoModelDictSplat("src/coder_eval/orchestration/batch.py").check(ast.parse(source))
        assert len(violations) == 1 and "NoiseFloor" in violations[0].message

    def test_ce041_is_silent_on_literal_keywords_and_on_a_non_models_import(self) -> None:
        for source in (
            "from ..models import NoiseFloor\nNoiseFloor(a=1, b=2)\n",
            "from ..other import Thing\nThing(**payload)\n",
        ):
            assert NoModelDictSplat("src/coder_eval/orchestration/batch.py").check(ast.parse(source)) == []

    def test_ce001_does_not_fire_on_the_type_checking_guarded_form(self) -> None:
        # `evaluation/checker.py` imports `..models.routing` under TYPE_CHECKING and is
        # legitimately exempt — confirmed here rather than assumed.
        source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..models.routing import ApiRoute\n"
        assert NoSubmoduleModelImports("src/coder_eval/evaluation/checker.py").check(ast.parse(source)) == []

    def test_a_path_outside_the_package_degrades_to_no_match(self) -> None:
        # The resolver returns None there, and every caller must read that as "not a match"
        # rather than raising or guessing.
        source = "from ..models import TaskDefinition\nTaskDefinition(**d)\n"
        assert NoModelDictSplat("/tmp/scratch/thing.py").check(ast.parse(source)) == []

    def test_the_whole_package_is_clean_through_the_runner(self) -> None:
        """Every rule above, over real `src/`, with `noqa` honoured. Runs AFTER the positive
        fixtures so a zero here means the rules work, not that they are blind."""
        rules = [row[0] for row in self._RULES]
        offenders = [f"{v.rule_id} {v.file}:{v.line}" for v in check_paths([SRC], rules=rules)]
        assert offenders == [], offenders


class TestResolvedModule:
    """The ONE relative-import resolver. Every import rule's correctness now rests on it.

    Paths are anchored on `SRC` rather than written CWD-relative: `resolved_module` requires the
    package root it picks to really BE a package (an `__init__.py` check, which is what stops it
    fabricating a module path for a file outside `src/coder_eval`), so a fixture path has to point
    into the real tree. A CWD-relative one would resolve to `None` from any other directory and
    quietly turn every case below into a no-op.
    """

    @pytest.mark.parametrize(
        ("source", "relative_path", "expected"),
        [
            ("from coder_eval.models import X", "orchestration/task_loader.py", "coder_eval.models"),
            ("from ..models import X", "orchestration/task_loader.py", "coder_eval.models"),
            ("from ..models.tasks import X", "cli/plan_command.py", "coder_eval.models.tasks"),
            ("from .console import X", "cli/plan_command.py", "coder_eval.cli.console"),
            # `from . import models`: module is None, so it resolves to the PACKAGE itself and the
            # imported NAME is the submodule. Documented on the resolver.
            ("from . import models", "orchestration/task_loader.py", "coder_eval.orchestration"),
            # Walks above the package root -> None, never a half-resolved string.
            ("from ....x import y", "cli/plan_command.py", None),
            ("from ...models import X", "orchestration/task_loader.py", None),
            # A module sitting directly in the package: `..` is already above the root.
            ("from ..models import X", "reports_optimize.py", None),
            # And one level deeper, in a SUBPACKAGE, where the same spelling does resolve.
            ("from ..models import X", "optimize/gate.py", "coder_eval.models"),
            ("from .load import X", "optimize/gate.py", "coder_eval.optimize.load"),
        ],
        ids=[
            "absolute",
            "level-2",
            "level-2-submodule",
            "level-1",
            "from-dot-import",
            "above-root",
            "at-root",
            "top-level-module",
            "subpackage-parent",
            "subpackage-sibling",
        ],
    )
    def test_it_resolves(self, source: str, relative_path: str, expected: str | None) -> None:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.ImportFrom)
        assert resolved_module(node, str(SRC / "coder_eval" / relative_path)) == expected

    def test_a_path_outside_any_package_is_none(self) -> None:
        node = ast.parse("from ..models import X").body[0]
        assert isinstance(node, ast.ImportFrom)
        assert resolved_module(node, "/tmp/somewhere/else.py") is None

    def test_the_innermost_package_root_wins(self) -> None:
        """This repo's own layout is `<checkout>/coder_eval/src/coder_eval/...` for many users.

        Resolving against the OUTER `coder_eval` — the checkout directory — would put the whole
        source tree into the module path. Exercised on the REAL absolute path so the case is the
        one that actually ships, not a hand-written approximation of it.
        """
        real = (SRC / "coder_eval" / "cli" / "plan_command.py").resolve()
        assert real.is_file(), "anchor drifted"
        node = ast.parse("from ..models import X").body[0]
        assert isinstance(node, ast.ImportFrom)
        assert resolved_module(node, str(real)) == "coder_eval.models"

    def test_a_directory_that_merely_shares_the_package_name_is_not_a_package(self) -> None:
        """The fabrication this fails-closed on, reproduced.

        A checkout directory called `coder_eval` is not the `coder_eval` PACKAGE. Before the
        `__init__.py` check, a file anywhere under it resolved against it and got back a confident
        fiction — `coder_eval.tests.lint.models` for a file in `tests/lint/rules/`. A wrong string
        is strictly worse than `None`: it can make a rule fire on innocent code, or stay silent on
        a real violation.
        """
        node = ast.parse("from ..models import X").body[0]
        assert isinstance(node, ast.ImportFrom)
        for fabricated in (
            "/repo/coder_eval/tests/lint/rules/foo.py",
            "/repo/coder_eval/src/coder_eval_uipath/a.py",
            "/repo/coder_eval/evalboard/scripts/f.py",
        ):
            assert resolved_module(node, fabricated) is None, fabricated


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


class TestCE051ImportFromRulesHandleLevel:
    """CE051 — the meta-rule. It polices a class with four CONFIRMED members, not a hypothetical."""

    def _check(self, source: str, path: str = "tests/lint/rules/ce999_example.py"):
        return ImportFromRulesHandleLevel(path).check(ast.parse(textwrap.dedent(source)))

    def test_it_fires_on_a_rule_reading_node_module_without_the_resolver(self) -> None:
        violations = self._check(_CE051_UNRESOLVED)
        assert len(violations) == 1 and violations[0].rule_id == "CE051"

    def test_it_is_silent_once_the_rule_routes_through_the_resolver(self) -> None:
        assert self._check(_CE051_RESOLVED) == []

    def test_a_third_party_matcher_is_exempt_by_construction(self) -> None:
        # A relative import can never resolve to a package outside the importing one, so CE020's
        # `claude_agent_sdk` matcher has no blindness to fix. Exempt by its own constants rather
        # than by a list that would need maintaining.
        source = """
_SDK = "claude_agent_sdk"

class R(BaseRule):
    def visit_ImportFrom(self, node):
        if node.module and node.module.startswith(_SDK):
            self.violation(node, "nope")
"""
        assert self._check(source) == []

    def test_a_docstring_mentioning_coder_eval_does_not_drag_a_rule_into_scope(self) -> None:
        """The branch the "exempt by construction" claim rests on, and it was broken.

        `ast.get_docstring` returns the `cleandoc`-NORMALISED text, which never equals the raw
        `ast.Constant.value` of a multi-line docstring — so a `value not in docstrings` filter
        excluded nothing, and any rule whose PROSE said `coder_eval` was pulled into scope. This
        fixture is CE020's shape with exactly that phrase added to its docstring.
        """
        source = '''
            """A rule about imports, unrelated to coder_eval.models."""

            _SDK = "claude_agent_sdk"

            class R(BaseRule):
                def visit_ImportFrom(self, node):
                    if node.module and node.module.startswith(_SDK):
                        self.violation(node, "nope")
        '''
        assert self._check(source) == []

    def test_a_docstring_alone_cannot_make_a_third_party_rule_report_a_gap(self) -> None:
        # The second symptom of the same bug, and the nastier one: the GAP violation was anchored
        # on the Module node, so it recorded line 0 and no `noqa` comment could ever suppress it.
        source = '''
            """Prose mentioning coder_eval.models, on a rule that walks the tree itself."""

            _SDK = "claude_agent_sdk"

            def _walk(tree):
                for node in tree.body:
                    if node.module and node.module.startswith(_SDK):
                        yield node
        '''
        assert self._check(source) == []

    def test_the_gap_violation_is_anchored_on_a_real_line(self) -> None:
        # An unsuppressible violation is a wall, not a guard: the runner matches a `noqa` comment
        # against the lines a node SPANS, and a Module node reports line 0.
        source = """
            _BANNED = "coder_eval.cli"

            def _walk(tree):
                for node in tree.body:
                    if node.module and node.module.startswith(_BANNED):
                        yield node
        """
        gaps = [v for v in self._check(source) if "cannot verify the resolver is used" in v.message]
        assert gaps and all(v.line > 0 for v in gaps)

    def test_the_resolver_module_itself_is_exempt(self) -> None:
        # It reads `node.module` because it IS the thing everything else routes through, and it
        # cannot call itself. Keyed on the functions it defines, not on its path.
        source = """
            _PACKAGE_ROOT = "coder_eval"

            def _package_chain(filepath):
                return filepath.split("/")

            def resolved_module(node, filepath):
                if node.level == 0:
                    return node.module
                return ".".join(_package_chain(filepath))
        """
        assert self._check(source, "tests/lint/import_resolution.py") == []

    def test_it_reports_a_gap_rather_than_passing_vacuously(self) -> None:
        # A rule restructured so the matching moved out of `visit_ImportFrom` must report a GAP,
        # not silently pass — CE044's lesson about a renamed `_ALLOWED_OPS`.
        source = """
_BANNED = "coder_eval.cli"

def _walk(tree):
    for node in tree.body:
        if node.module and node.module.startswith(_BANNED):
            yield node
"""
        violations = self._check(source)
        assert any("cannot verify the resolver is used" in v.message for v in violations)

    def test_it_is_silent_outside_the_rules_package(self) -> None:
        assert self._check(_CE051_UNRESOLVED, "src/coder_eval/optimize/gate.py") == []

    def test_the_helper_is_reported_not_its_caller(self) -> None:
        # CE041's shape: the matching lives in a module-level helper, so that is where the fix
        # goes and where the violation must point.
        source = """
_MODELS = "coder_eval.models"

def _imports_models(node):
    return node.module == _MODELS

class R(BaseRule):
    def visit_ImportFrom(self, node):
        if _imports_models(node):
            self.violation(node, "nope")
"""
        violations = self._check(source)
        assert len(violations) == 1
        assert violations[0].line == 4, "the violation must point at the helper, not the visitor"

    def test_the_id_is_unique_across_every_wired_rule(self) -> None:
        assert [r.id for r in ALL_RULES].count("CE051") == 1

    def test_the_whole_test_tree_is_clean(self) -> None:
        """CE051's ONLY real enforcement surface, so it must not be able to pass by seeing nothing.

        `ALL_RULES` only ever runs over `src/`, where CE051's own scope guard makes it inert — so
        `make lint` never exercises it. Anchored absolutely rather than CWD-relative: `check_paths`
        silently returns [] for a path that does not exist, which would make the one test that
        matters pass vacuously from any other working directory.
        """
        tests_root = SRC.parent / "tests"
        assert tests_root.is_dir(), "anchor drifted — CE051 would be checking nothing"
        offenders = [f"{v.file}:{v.line}" for v in check_paths([tests_root], rules=[ImportFromRulesHandleLevel])]
        assert offenders == [], offenders


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
