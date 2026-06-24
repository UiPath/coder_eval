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
