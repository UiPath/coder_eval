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
# stage, three gate invocations and a holdout confirmation, all `coder-eval run`.
SKILLS_REQUIRING_THE_CLI = {"init", "check-skill", "task", "optimize-skill"}

# The skills that read the shared task-quality rubric. A rubric no skill reads is
# dead weight; a reader that stops reading it has silently forked the rubric.
RUBRIC_READERS = {"task", "lint-tasks", "init"}

# Whether each skill must locate a repository's eval tree before it can do anything.
# All six currently must, and each for its own reason: `analyze` needs the run store,
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
SKILL_DOC_SURFACES = ("plugins/coder-eval/README.md", "docs/PLUGIN.md", "README.md", "CLAUDE.md")

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
            "tutorial 08": self.REPO_ROOT / "docs" / "tutorials" / "08-optimizing-a-skill.md",
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
        # the split: a holdout of only positives measures recall and calls it a result, and
        # a tune half with no distractors cannot see a candidate over-claiming. The shipped
        # template is the worked example everyone copies, so the balance it demonstrates has
        # to hold — an edit that moved one row could break it silently.
        import json

        rows = [
            json.loads(line)
            for line in (self.TEMPLATES / "activation-rows.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert all(r.get("split") for r in rows), (
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
                "tune/holdout comparison measures nothing"
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
        text = " ".join((PLUGIN_ROOT / "skills" / "lint-tasks" / "SKILL.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "skills" / "optimize-skill" / "SKILL.md").read_text(encoding="utf-8").split())

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
            ("--split tune", "the tune split drives proposals and the gate"),
            ("--split holdout", "the holdout split is what makes a promotion more than a fit"),
            ("--split holdout --repeats 3", "Stage C's paired comparison needs replicates averaged per row"),
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
        ):
            assert token in text, f"optimize-skill lost {token!r} — {why}"

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

    def test_skill_docs_surfaces_state_the_right_count(self):
        # The companion to the test above, which only checks that each NAME appears. These
        # surfaces also state the count in prose, and adding the sixth skill meant hand-editing
        # seven such sites across four files. Without this, a seventh ships with every count
        # silently wrong — the exact drift that repair was. Derived from disk: no count is
        # written down here.
        #
        # Three phrasings are in use and all three are covered: "<word> skills" / "<word> slash
        # commands" (both READMEs, docs/PLUGIN.md), "x <digit>" (CLAUDE.md's `SKILL.md` x 6),
        # and "The other <word>" (the model-invokable subset, which is the skill count minus
        # the explicit-invocation-only ones).
        words = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
        count = len(PLUGIN_SKILLS)
        assert count in words, f"{count} skills — extend `words` to cover the new count"
        auto = count - sum(1 for v in SKILL_DISABLE_MODEL_INVOCATION.values() if v)
        assert auto in words, f"{auto} model-invokable skills — extend `words`"

        wrong_total = sorted(set(words.values()) - {words[count]})
        wrong_subset = sorted(set(words.values()) - {words[auto]})

        offenders: list[str] = []
        for surface in SKILL_DOC_SURFACES:
            text = (self.REPO_ROOT / surface).read_text(encoding="utf-8")
            offenders += [
                f"{surface}: '{word} {noun}'"
                for word in wrong_total
                for noun in ("skills", "slash commands")
                if f"{word} {noun}" in text
            ]
            offenders += [f"{surface}: 'The other {word}'" for word in wrong_subset if f"The other {word}" in text]
            # The multiplication sign CLAUDE.md writes is given as an escape below, so
            # ruff's ambiguous-character rules do not flag a literal one.
            offenders += [
                f"{surface}: 'SKILL.md` \u00d7 {digit}'"
                for digit in range(2, 9)
                if digit != count and f"SKILL.md` \u00d7 {digit}" in text
            ]
        assert not offenders, (
            f"there are {count} shipped skills, but these surfaces still state another count: "
            f"{offenders}. Update the prose alongside the table."
        )

    def test_analyze_routes_fixes_to_the_right_layer(self):
        # A shallow keyword sensor: it guards against the guidance being DELETED, not
        # against it being badly written. The root-cause token has to survive too, because
        # the routing rule is attached to it — `prompt_gap` naming a missing piece of
        # knowledge is meaningless if nothing says which layer should have supplied it.
        # Whitespace-collapsed so a reflowed paragraph does not fail it — these files are
        # hard-wrapped prose, and a sensor that breaks on rewrapping trains people to
        # distrust it.
        text = " ".join((PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "skills" / "task" / "SKILL.md").read_text(encoding="utf-8").split())
        assert "the repo wins" in text or "the repository wins" in text, (
            "task no longer says repo-local convention beats the bundled rubric — it will "
            "impose the plugin's defaults on a repository that already declared its own"
        )
        assert "conventions you adopted" in text or "conventions adopted" in text, (
            "task does not report WHICH conventions it adopted — an unreported precedence "
            "rule cannot be checked by the person reading the result"
        )
        rubric = " ".join((PLUGIN_ROOT / "reference" / "task-rubric.md").read_text(encoding="utf-8").split())
        assert "the repo wins" in rubric or "the repository wins" in rubric, (
            "reference/task-rubric.md does not carry the same precedence line — the rubric "
            "is read at review time too, and must not contradict the authoring skill"
        )

    def test_ci_skill_covers_experiments_and_pins(self):
        # Two silent-wrong-answer bugs in an emitted workflow. Omitting the experiment
        # drops the `agent:` config it supplies, so the gate measures something other than
        # what the suite measures locally; and a `version:` input that ignores the repo's
        # pin runs the gate on a different CLI than the repo is authored against.
        text = " ".join((PLUGIN_ROOT / "skills" / "ci" / "SKILL.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "skills" / "check-skill" / "SKILL.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "skills" / "init" / "SKILL.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "reference" / "cli-setup.md").read_text(encoding="utf-8").split())
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
        text = " ".join((PLUGIN_ROOT / "skills" / "analyze" / "SKILL.md").read_text(encoding="utf-8").split())
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
