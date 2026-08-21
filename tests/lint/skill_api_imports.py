"""CE066 — `optimize-skill`'s `SKILL.md` imports from `coder_eval.optimize.api` and nothing else.

**What this converts.** Before the composite migration the skill's fences imported from five of the
optimize family's decision modules and spelled 427 lines of driver logic in markdown: the guards
(``if not arms[0].row_scores: raise SystemExit(…)``), the fallbacks (a suite-level ceiling when rule
attribution is unavailable), the track branches (a commented-out half and a ``TRACK = "activation"``
string the user hand-edited), and one hand-written row primitive. None of it was reachable by a test,
because markdown does not execute — which is why two of those fences computed a reported number from
a run tree they never reconciled, the exact defect CE053 exists to force, invisible to it because the
rule cannot see markdown.

So the library's skill-facing surface was whatever the snippet binder happened to resolve. This rule
is what makes it DECLARED: one module, whose every function has a test.

**What it detects, precisely.** Every ``coder_eval`` module imported by a ``python`` fence in the
given markdown file, and any that is not ``coder_eval.optimize.api``. Fences are ``ast``-parsed, so a
module path mentioned in PROSE is not an import and a fence importing ``pathlib`` or ``subprocess``
is not a finding — only ``coder_eval`` imports are in scope. That is the CE064 lesson: a substring
scan over a heavily-prosed markdown file reports the documentation as the defect.

Not a ``BaseRule`` in ``tests/lint/runner.py``, like CE026-CE033: that runner is AST-only over ``.py``
files, and this reasons over Markdown. It is wired as
``tests/lint_tests/test_lint_plugin_optimize.py::TestCE066SkillImportsOnlyTheApi``.

**The boundary, stated so a green ``make lint`` is not mistaken for a proof.**

- It pins the import MODULE, never that a composite is USED WELL. A fence that imports
  ``optimize.api`` and then reimplements a guard around the call satisfies this rule completely.
- It says nothing about the *names* imported from ``api``, nor about the keywords passed to them.
  ``test_optimize_skill_snippet_names_the_public_gate_api`` and
  ``test_optimize_skill_snippets_parse_and_bind`` are what cover those, and neither subsumes this:
  they check that whatever the skill imports RESOLVES, so a fence reaching back into
  ``optimize.load`` would satisfy both while undoing the migration.
- A fence that reaches the library through ``importlib`` or a string is invisible to it. Nothing in
  the tree does that, and a rule that tried would be guessing.
- One finding per offending import statement, so a fence importing two forbidden modules reports two.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.lint.import_resolution import resolved_module


DECLARED_SURFACE = "coder_eval.optimize.api"

# Fenced python blocks. ```python only — a ```bash block naming a module path is prose as far as this
# rule is concerned, and a fence in another language cannot carry an import statement anyway.
_PYTHON_FENCE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def python_fences(markdown: str) -> list[str]:
    """Every ``python`` fence's source, in document order.

    Returned rather than yielded so a caller can count them: a reader that found ZERO fences would
    report zero offenders, which is byte-identical to a clean file, and the anti-vacuity test in the
    wiring needs the count to rule that out.
    """
    return [match.group(1) for match in _PYTHON_FENCE.finditer(markdown)]


# A relative import spelled inside a markdown fence. A fence is standalone top-level code with no
# parent package, so `from .optimize.api import x` raises `ImportError` before it can reach anything —
# it is not a way past this rule, and reporting it under its own name is more useful than resolving it
# against a path that is not a package.
_RELATIVE = "<relative import — a fence has no parent package>"


def coder_eval_imports(fence: str, source: Path | None = None) -> set[str]:
    """The ``coder_eval`` modules one fence imports, by dotted path.

    ``ast``-parsed, so ``from coder_eval.optimize.api import x`` and ``import coder_eval.optimize.api``
    both resolve to the same module string, and a mention in a comment or a docstring resolves to
    nothing. A fence that does not parse contributes nothing rather than raising — the snippet binder
    is what fails on a syntactically broken fence, and two rules reporting one fault is noise.

    Routed through :func:`~tests.lint.import_resolution.resolved_module` (CE051) rather than reading
    ``node.module`` directly, even though a RELATIVE import cannot occur here in a form that matters:
    a fence has no parent package, so ``from .optimize.api import x`` raises ``ImportError`` before it
    reaches anything. The resolver is called anyway because CE051's subject is the HABIT, not the
    individual case — four rules matched ``node.module`` alone and every one of them failed OPEN, and
    "this file is different" is what each of them could have said. A relative import is reported under
    :data:`_RELATIVE`, so it is never silently clean either.

    ``source`` is the markdown file, passed only so the resolver has a path; it is not a package, so
    the resolver correctly declines to place a relative import and this reports it by shape instead.
    """
    try:
        tree = ast.parse(fence)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(_RELATIVE)
                continue
            module = resolved_module(node, str(source) if source else "")
            if (module or "").startswith("coder_eval"):
                found.add(module or "")
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name.startswith("coder_eval")}
    return found


def find_foreign_imports(path: Path) -> list[str]:
    """Findings: every ``coder_eval`` import in a fence that is not the declared surface.

    Each names the fence by its position in the file, because a skill this long has fifteen of them
    and "SKILL.md imports optimize.load" is not enough to find the one that does.
    """
    fences = python_fences(path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for index, fence in enumerate(fences, start=1):
        for module in sorted(coder_eval_imports(fence, path)):
            if module != DECLARED_SURFACE:
                findings.append(
                    f"{path.name} fence {index} imports {module} — the declared skill-facing surface "
                    f"is {DECLARED_SURFACE} alone"
                )
    return findings
