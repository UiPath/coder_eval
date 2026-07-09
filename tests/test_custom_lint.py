"""Custom architectural lint rules for coder_eval.

Each test enforces one rule across the entire src/ tree.  A violation means
the code breaks a project-specific architectural constraint that Ruff cannot
express.  Fix the violation or add `# noqa: CE<id>` to suppress it locally
with a comment explaining why.

Run just these tests:
    uv run pytest tests/test_custom_lint.py -v
    make lint
"""

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
    """CE025 flags criteria whose `live_stop_polarities` and `live_verdict` disagree."""

    _POLARITIES_NONEMPTY = '    live_stop_polarities: ClassVar[frozenset[str]] = frozenset({"pass", "fail"})\n'
    _POLARITIES_EMPTY = "    live_stop_polarities: ClassVar[frozenset[str]] = frozenset()\n"
    _POLARITIES_PLAIN = '    live_stop_polarities = frozenset({"pass"})\n'
    _POLARITIES_NONLITERAL = "    live_stop_polarities: ClassVar[frozenset[str]] = frozenset(_SOME_SET)\n"
    _LIVE_VERDICT = '    def live_verdict(self, criterion, turn_records):\n        return "undecided"\n'

    @staticmethod
    def _cls(*members: str) -> str:
        body = "".join(members) if members else "    pass\n"
        return "from typing import ClassVar\nclass FakeChecker:\n" + body

    @staticmethod
    def _run(src: str, *, path: str = "src/coder_eval/criteria/fake_checker.py"):
        import ast

        from tests.lint.rules.ce025_live_verdict_consistency import LiveVerdictConsistency

        return LiveVerdictConsistency(path).check(ast.parse(src))

    def test_flags_polarities_without_live_verdict(self):
        assert self._run(self._cls(self._POLARITIES_NONEMPTY))

    def test_flags_live_verdict_without_polarities(self):
        assert self._run(self._cls(self._LIVE_VERDICT))

    def test_flags_live_verdict_with_empty_polarities(self):
        assert self._run(self._cls(self._POLARITIES_EMPTY, self._LIVE_VERDICT))

    def test_allows_polarities_with_live_verdict(self):
        assert not self._run(self._cls(self._POLARITIES_NONEMPTY, self._LIVE_VERDICT))

    def test_allows_neither_declared(self):
        # A plain checker (e.g. file_exists) that arms nothing declares neither member.
        assert not self._run(self._cls("    path: str\n"))

    def test_allows_empty_polarities_without_live_verdict(self):
        assert not self._run(self._cls(self._POLARITIES_EMPTY))

    def test_detects_plain_assign_polarities(self):
        # Non-empty `live_stop_polarities` via a plain (non-annotated) assignment still arms.
        assert self._run(self._cls(self._POLARITIES_PLAIN))

    def test_non_literal_arg_treated_nonempty(self):
        # A computed frozenset() argument is conservatively non-empty → live_verdict required.
        assert self._run(self._cls(self._POLARITIES_NONLITERAL))
        assert not self._run(self._cls(self._POLARITIES_NONLITERAL, self._LIVE_VERDICT))

    def test_scope_exemptions(self):
        # base.py legitimately pairs the empty default with the default live_verdict; and
        # files outside criteria/ are out of scope entirely.
        offending = self._cls(self._LIVE_VERDICT)
        assert not self._run(offending, path="src/coder_eval/criteria/base.py")
        assert not self._run(offending, path="src/coder_eval/orchestration/x.py")

    def test_real_criteria_tree_is_clean(self):
        from tests.lint.rules.ce025_live_verdict_consistency import LiveVerdictConsistency

        criteria_dir = SRC / "coder_eval" / "criteria"
        assert not check_paths([criteria_dir], rules=[LiveVerdictConsistency])


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
