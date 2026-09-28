"""Unit tests for coder_eval.stats — the dependency-free numeric core.

This module imports **only** from ``coder_eval.stats``. That is deliberate and
is itself asserted below: the whole reason the statistics live in their own
module is that they can be reasoned about without dragging in models, timing or
the report layer.
"""

import ast
from pathlib import Path

from coder_eval.stats import mean, stddev


REPO_ROOT = Path(__file__).parent.parent


class TestStatsIsDependencyFree:
    """`stats.py` must import nothing from coder_eval, directly or relatively.

    Cheaper as a unit test than a lint rule: it guards exactly one file. If it
    ever fails, the numeric core has grown a dependency and stopped being the
    thing that can be tested in isolation.
    """

    def test_no_coder_eval_import_of_any_form(self):
        source = (REPO_ROOT / "src" / "coder_eval" / "stats.py").read_text(encoding="utf-8")
        offenders: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import (`from .x import y`).
                if node.level > 0 or (node.module or "").startswith("coder_eval"):
                    offenders.append(f"from {'.' * node.level}{node.module or ''}")
            elif isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.startswith("coder_eval")]
        assert not offenders, f"stats.py must stay dependency-free, found: {offenders}"


class TestDependencyFreeBehaviourIsReal:
    """A smoke check that the extracted module actually computes.

    Deliberately thin: `test_replicate_stats.py` and `test_experiment_reports.py`
    own the numeric coverage, and duplicating it here would mean two places to
    update per formula. What this file exists for is the invariant above.
    """

    def test_the_module_computes_without_any_coder_eval_import(self):
        assert mean([1.0, 2.0, 3.0]) == 2.0
        assert stddev([1.0, 2.0, 3.0]) == 1.0
