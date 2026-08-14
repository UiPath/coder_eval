"""Custom architectural lint rules for coder_eval.

Each test enforces one rule across the entire src/ tree.  A violation means
the code breaks a project-specific architectural constraint that Ruff cannot
express.  Fix the violation or add `# noqa: CE<id>` to suppress it locally
with a comment explaining why.

Run just these tests:
    uv run pytest tests/test_custom_lint.py -v
    make lint
"""

import re
from pathlib import Path

import pytest

from tests.lint.runner import ALL_RULES, check_paths


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
        import yaml

        from tests.lint.doc_indexes import load_nav

        load_nav(self.REPO_ROOT / "mkdocs.yml")
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load("x: !ENV [A, b]")

    def test_python_object_tags_are_still_rejected(self):
        import yaml

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
RUBRIC_READERS = {"task", "lint-tasks", "init"}

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
    2. **Split counts** as declared, checked through ``expand_dataset(..., split=...)``
       because ``coder-eval plan`` has no ``--split`` flag to check them from a shell.
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


def _skill_frontmatter(path: Path) -> dict:
    """Parse a SKILL.md's YAML frontmatter block."""
    import yaml

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
        import yaml

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

        # The PROCEDURE half, asserted against `SKILL.md` alone. Moving any of these into
        # the method file would leave the step that must act on it silently pointing
        # elsewhere — which is the failure mode the extraction itself could introduce.
        for token, why in (
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
            (
                "weighted_score",
                "the execution preflight measures weighted_score, not f1.yes — an F1 floor prices a "
                "gate that never reads F1 and comes back a meaningless 0.000",
            ),
        ):
            assert token in skill, (
                f"optimize-skill's SKILL.md lost {token!r} — {why}. This is PROCEDURE: it must stay "
                f"in the skill, not move to reference/optimize-method.md"
            )

        # An extracted reference nothing points at is a deleted reference. Mirrors the
        # reference/task-rubric.md pointer sensor.
        assert "${CLAUDE_PLUGIN_ROOT}/reference/optimize-method.md" in skill, (
            "optimize-skill's SKILL.md no longer points at reference/optimize-method.md, so the "
            "cost table, the gate rules and the sign rule are unreachable from the procedure "
            "that has to apply them"
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
        # This file exists to stop three specific failures, and each is invisible in the output:
        # a proposer that paraphrases its last attempt, one that has seen the test split, and one
        # that fixes the row in front of it rather than the category it belongs to.
        text = _normalized(PLUGIN_ROOT / "reference" / "proposal-prompt.md")
        for token, why in (
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
        ):
            assert token in text, f"reference/proposal-prompt.md lost {token!r} — {why}"

    def test_optimize_skill_snippet_names_the_public_gate_api(self):
        # A prose sensor cannot see a snippet drifting from the API it calls: the tokens stay
        # present, the skill stays readable, and the step fails at runtime in the user's terminal
        # after they have paid for three invocations. So assert both halves — the names are in the
        # skill AND they are importable from the module the skill tells the user to import.
        import coder_eval.optimize_gate as gate

        raw = (PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8")
        skill = _normalized(PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md")

        # Derived from the skill's own import lines rather than a list here, so a snippet that
        # starts importing something new is covered without anyone remembering to extend this.
        imported: set[str] = set()
        for block in re.findall(r"from coder_eval\.optimize_gate import \(([^)]*)\)", raw):
            imported |= {n.strip().rstrip(",") for n in block.split() if n.strip().rstrip(",")}
        for line in re.findall(r"from coder_eval\.optimize_gate import ([^(\n]+)", raw):
            imported |= {n.strip() for n in line.split(",") if n.strip()}

        assert imported, (
            "optimize-skill's SKILL.md no longer imports anything from coder_eval.optimize_gate — "
            "either the snippets are gone or the import line changed shape and this sensor is blind"
        )
        for name in sorted(imported):
            assert hasattr(gate, name), (
                f"optimize-skill's SKILL.md tells the user to import {name!r} from "
                f"coder_eval.optimize_gate, which no longer exports it — the snippet would fail at "
                f"runtime, after the user has paid for the runs it was meant to read"
            )

        # The three the procedure must NAME, even if a future snippet stops importing them inline.
        for name in ("activation_gate", "holm_promote", "render_markdown"):
            assert name in skill, f"optimize-skill's SKILL.md no longer names {name!r} in its gate snippet"

    def test_optimize_method_quotes_no_tolerance_numbers(self):
        # The guardrails are bootstrap-derived; the ONE tolerance constant lives in the module.
        # A figure hand-copied into the prose is a second declaration of it — the same shape as the
        # pricing.py <-> pricing.ts mirror this repo had to add a parity test to defend — and it
        # goes stale silently, because nothing about changing a constant makes anyone reread a
        # markdown file. Derived from the constant, so this cannot itself go stale.
        from coder_eval.optimize_gate import MATERIALITY_FLOOR

        method = _normalized(PLUGIN_ROOT / "reference" / "optimize-method.md")
        forbidden = {f"{MATERIALITY_FLOOR:.0%}", f"{MATERIALITY_FLOOR:g}", f"{MATERIALITY_FLOOR:.2f}"}
        present = sorted(f for f in forbidden if f in method)
        assert not present, (
            f"reference/optimize-method.md quotes {present} — the rendered value of MATERIALITY_FLOOR. "
            "The guardrail tolerance is owned by coder_eval.optimize_gate; describe the guardrail as "
            "bootstrap-derived and carry no figure, or the prose drifts the moment the constant moves."
        )

    def test_optimize_surfaces_quote_no_resample_count(self):
        # Same rule as the tolerance above, one constant along. GATE_RESAMPLES is DERIVED from
        # GATE_P_PRECISION / GATE_MAX_FAMILY / DEFAULT_ALPHA, so a figure typed into the prose is a
        # second declaration of a number the module computes — and the derivation is exactly the
        # kind of thing that gets retuned once and then disagrees with two markdown files forever.
        from coder_eval.optimize_gate import GATE_RESAMPLES

        forbidden = {f"{GATE_RESAMPLES:d}", f"{GATE_RESAMPLES:,d}"}
        for name in ("reference/optimize-method.md", "skills/optimize-skill/SKILL.md"):
            surface = _normalized(PLUGIN_ROOT / name)
            present = sorted(f for f in forbidden if f in surface)
            assert not present, (
                f"{name} quotes {present} — the rendered value of GATE_RESAMPLES, which the module "
                "derives from two stated requirements. The rendered verdict block reports the draw "
                "count it actually used; read it from there rather than restating it here."
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
    Enforced over every model reachable from `coder_eval.models`.
    """

    MAX_ANNOTATION_DEPTH = 6

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
        from coder_eval.orchestration.task_loader import _load_dataset_rows, row_split_label

        rows = _load_dataset_rows(task.dataset, task_file_dir)
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


# Fields naming WHERE an artifact goes, not WHAT it must contain. A prompt may say
# "write it to .github/workflows/evals.yml" — that removes filename nondeterminism from
# the measurement without revealing the graded behaviour. `skill_name` is a locator for
# the same reason: it names WHICH skill must engage, while the graded thing is the
# engagement EVENT, which no prompt can supply. The outcome pattern this plugin
# prescribes puts the skill name in every prompt by design.
CE036_LOCATOR_FIELDS = ("path", "agent_file", "file_path", "command", "skill_name")

# Shorter values collide by chance ("ci", "0.7"); a leak worth flagging is a substantive
# string the author put in both places.
CE036_MIN_LEAK_CHARS = 12


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
    `command_executed.command_pattern` of >= CE036_MIN_LEAK_CHARS echoed verbatim in a
    prompt IS flagged. That is correct — a pattern asserting *what ran* is graded
    behaviour, not a locator. The `command` exemption covers `run_command.command`, the
    command the CHECKER runs, which is a different field on a different criterion. No
    in-repo task has the collision, so exempting `command_pattern` would be an unused
    exemption weakening a real check.
    """

    @classmethod
    def _offenders(cls, task, task_file_dir: Path) -> list[str]:
        """Every verbatim leak in the task's expanded rows.

        The rule's whole detection body lives here so the fixtures below and the repo scan
        exercise the SAME code. Split, the repo scan would keep passing identically whether
        or not the rule could still detect anything.
        """
        from coder_eval.orchestration.task_loader import expand_dataset

        offenders: list[str] = []
        for row in expand_dataset(task, task_file_dir):
            prompt = (row.initial_prompt or "").lower()
            if not prompt:
                continue
            for criterion in row.success_criteria:
                # Every string the criterion asserts CONTENT on. `description` is excluded:
                # it is a label, routinely echoes the scenario, and grades nothing.
                dumped = criterion.model_dump()
                dumped.pop("description", None)
                for locator in CE036_LOCATOR_FIELDS:
                    dumped.pop(locator, None)
                for value in _string_leaves(dumped):
                    if len(value) >= CE036_MIN_LEAK_CHARS and value.lower() in prompt:
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
        from coder_eval.models import SkillTriggeredCriterion

        assert len("optimize-skill") >= CE036_MIN_LEAK_CHARS, "fixture no longer exercises the floor"
        task = _dataset_task(
            [{"id": "a"}],
            prompt="Use the optimize-skill skill to improve this description",
            criteria=[SkillTriggeredCriterion(description="d", skill_name="optimize-skill", expected_skill="")],
        )
        assert self._offenders(task, tmp_path) == []

    @pytest.mark.parametrize(("delta", "flagged"), [(-1, False), (0, True)])
    def test_the_length_floor_is_inclusive(self, tmp_path: Path, delta: int, flagged: bool):
        # Both sides of the `>=`, which is the part a refactor actually breaks. The strings
        # are DERIVED from CE036_MIN_LEAK_CHARS rather than spelled out: a hardcoded 11 and
        # 12 would be a second declaration of the same number, which is the drift this
        # fixture exists to prevent. (The literal VALUE of the floor is not pinned here on
        # purpose — it is a tuning knob; what must not move silently is the comparison.)
        from coder_eval.models import FileCheckCriterion

        value = "x" * (CE036_MIN_LEAK_CHARS + delta)
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
        # Pins `_string_leaves`' recursion: a leak in the SECOND entry of a list must be
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
        # CE036_LOCATOR_FIELDS is the single source; CLAUDE.md's CE036 sentence is derived.
        # Both directions, because the list already drifted once: CLAUDE.md named three of
        # the four fields the code exempted, and nothing noticed. The repo automates exactly
        # this class elsewhere (CE028 for the docs indexes, CE033 for the plugin reference).
        import re

        text = _normalized(Path(__file__).parent.parent / "CLAUDE.md")
        sentence = next((s for s in text.split(". ") if "Location fields" in s), None)
        assert sentence is not None, "CLAUDE.md no longer states CE036's exemption list"
        backticked = set(re.findall(r"`([a-z_]+)`", sentence))
        assert set(CE036_LOCATOR_FIELDS) <= backticked, (
            f"CLAUDE.md's CE036 sentence omits {sorted(set(CE036_LOCATOR_FIELDS) - backticked)}"
        )
        assert backticked <= set(CE036_LOCATOR_FIELDS), (
            f"CLAUDE.md's CE036 sentence names {sorted(backticked - set(CE036_LOCATOR_FIELDS))} as exempt, "
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


def _string_leaves(node: object) -> list[str]:
    """Every string in a nested dict/list, flattened."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _string_leaves(v)]
    if isinstance(node, list):
        return [s for v in node for s in _string_leaves(v)]
    return []


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
            "SKILL.md": [],
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

    def test_parse_markdown_tables_skips_fenced_blocks(self):
        from tests.lint.computed_claims import parse_markdown_tables

        text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```\n| x | y |\n| --- | --- |\n| 3 | 4 |\n```\n"
        tables = parse_markdown_tables(text)
        assert [t.header for t in tables] == [["a", "b"]]
