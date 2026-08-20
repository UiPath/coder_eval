"""Lint tests: ast rules."""

import pytest

from tests.lint.runner import check_paths
from tests.lint_tests.shared import SRC


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
