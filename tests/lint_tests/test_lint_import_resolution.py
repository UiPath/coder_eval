"""Lint tests: import resolution."""

import ast
import textwrap
from typing import ClassVar

import pytest

from tests.lint.import_resolution import resolved_module
from tests.lint.rules.ce017_models_lazy_agent_imports import ModelsLazyAgentImports
from tests.lint.rules.ce023_no_proxy_shim_import import NoProxyShimImports
from tests.lint.rules.ce041_no_model_dict_splat import NoModelDictSplat
from tests.lint.rules.ce051_importfrom_rules_handle_level import (
    _HAND_ROLLED,
    _HOOK_GAP,
    ImportFromRulesHandleLevel,
)
from tests.lint.rules.ce059_no_sibling_private_imports import NoSiblingPrivateImports
from tests.lint.rules.no_cli_imports_in_core import NoCliImportsInCore
from tests.lint.rules.no_submodule_model_imports import NoSubmoduleModelImports
from tests.lint.runner import ALL_RULES, check_paths
from tests.lint_tests.shared import (
    _CE051_RESOLVED,
    _CE051_UNRESOLVED,
    SRC,
    TESTS_ROOT,
)


@pytest.mark.lint
class TestCE051ImportFromRulesHandleLevel:
    """CE051 — the meta-rule. It polices a class with four CONFIRMED members, not a hypothetical.

    It reports TWO independent findings, so the tests separate them: `_resolver_check` filters to
    the "route through the resolver" half, and `_check` returns everything. A test about one half
    must not be perturbed by the other — every fixture below is a rule that hand-rolls
    `visit_ImportFrom`, which is now itself a finding, so an unfiltered assertion on a resolver
    fixture would be counting the wrong thing.
    """

    def _check(self, source: str, path: str = "tests/lint/rules/ce999_example.py"):
        return ImportFromRulesHandleLevel(path).check(ast.parse(textwrap.dedent(source)))

    def _resolver_check(self, source: str, path: str = "tests/lint/rules/ce999_example.py"):
        return [v for v in self._check(source, path) if v.message not in {_HAND_ROLLED, _HOOK_GAP}]

    def test_it_fires_on_a_rule_reading_node_module_without_the_resolver(self) -> None:
        violations = self._resolver_check(_CE051_UNRESOLVED)
        assert len(violations) == 1 and violations[0].rule_id == "CE051"

    def test_it_is_silent_once_the_rule_routes_through_the_resolver(self) -> None:
        assert self._resolver_check(_CE051_RESOLVED) == []

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
        assert self._resolver_check(source) == []

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
        assert self._resolver_check(source) == []

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
        assert self._resolver_check(source) == []

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
        violations = self._resolver_check(source)
        assert len(violations) == 1
        assert violations[0].line == 4, "the violation must point at the helper, not the visitor"

    def test_a_rule_that_hand_rolls_the_visitor_is_reported(self) -> None:
        """The second check: the hook exists, so opting out of it must not be silent."""
        source = """
_MODELS = "coder_eval.models"

class R(BaseRule):
    def visit_ImportFrom(self, node):
        module = resolved_module(node, self.filepath)
        if module == _MODELS:
            self.violation(node, "nope")
"""
        violations = self._check(source)
        assert [v.message for v in violations] == [_HAND_ROLLED]
        # It routes through the resolver, so the FIRST check is satisfied — the two are independent.
        assert self._resolver_check(source) == []

    def test_a_rule_using_the_hook_is_not_reported(self) -> None:
        source = """
_MODELS = "coder_eval.models"

class R(BaseRule):
    def check_import(self, node, module):
        if module == _MODELS:
            self.violation(node, "nope")
"""
        assert self._check(source) == []

    def test_the_base_class_itself_is_exempt(self) -> None:
        source = """
class BaseRule:
    def visit_ImportFrom(self, node):
        self.check_import(node, resolved_module(node, self.filepath))
        self.generic_visit(node)

    def check_import(self, node, module):
        pass
"""
        assert self._check(source, path="tests/lint/rules/base.py") == []

    @pytest.mark.parametrize("missing", ["visit_ImportFrom", "check_import"])
    def test_the_base_class_reports_a_gap_if_either_half_of_the_hook_goes(self, missing: str) -> None:
        """CE044's lesson: a check whose target was renamed must not pass by matching nothing.

        With `check_import` gone there is nothing to route rules TO, and with `visit_ImportFrom`
        gone nothing resolves the module — either way the second check would then be satisfied by
        every rule file in the tree, silently.
        """
        source = """
class BaseRule:
    def visit_ImportFrom(self, node):
        self.check_import(node, resolved_module(node, self.filepath))

    def check_import(self, node, module):
        pass
"""
        broken = source.replace(f"def {missing}(", "def renamed_away(")
        violations = self._check(broken, path="tests/lint/rules/base.py")
        assert [v.message for v in violations] == [_HOOK_GAP]

    def test_a_rule_outside_the_rules_directory_may_define_the_visitor(self) -> None:
        """The second check is scoped; the first is not.

        `tests/test_optimize_layering.py::_coder_eval_imports` is not a `BaseRule` and can never use
        the hook, which is exactly why CE051 keeps its tests-wide scope for the resolver half.
        """
        source = """
_MODELS = "coder_eval.models"

class NotARule:
    def visit_ImportFrom(self, node):
        module = resolved_module(node, self.filepath)
        if module == _MODELS:
            pass
"""
        assert self._check(source, path="tests/test_something_else.py") == []

    def test_the_rules_directory_defines_the_visitor_exactly_once(self) -> None:
        """The live acceptance criterion, asserted rather than left to the whole-tree scan.

        A zero here would mean the hook was deleted outright; anything above one means a rule
        reintroduced its own visitor. Both are the states this check exists to prevent, and the
        whole-tree scan reports the second but not the first.
        """
        definers = sorted(
            path.name
            for path in (TESTS_ROOT / "lint" / "rules").glob("*.py")
            if any(
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "visit_ImportFrom"
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            )
        )
        assert definers == ["base.py"]

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


@pytest.mark.lint
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


@pytest.mark.lint
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
