"""Every third-party module imported under ``src/`` must be a DECLARED dependency.

Why this exists, and why the rest of the suite could not catch it: `evaluation/judge_bedrock.py`
imported `httpx` while `pyproject.toml` never declared it. It worked for months because
`anthropic` pulled `httpx` transitively — so `uv.lock` contained it, and every `uv sync --frozen`
job (the whole locked half of CI, plus `make verify`) installed it and passed.

Then `anthropic` 1.0.0 moved to `httpx2`. A *fresh* resolve — which is what
`uv tool install` and `pip install coder-eval` do, because a published wheel carries no
lockfile — stopped installing `httpx`. `criteria/llm_judge.py` imports `judge_bedrock`, so it
raised on import; `criteria/__init__.py` catches that and logs it, and `llm_judge` simply
vanished from the registry. The failure surfaced only later, as a task dying on
"Missing criterion checkers", in the one CI job that installs like a real user.

So the invariant is about the DECLARATION, not the installed set. Checking "can I import it"
passes in every locked environment and is exactly the blind spot that shipped the bug; checking
"is it in `pyproject.toml`" fails fast in the cheap locked job with no network.

What counts as required is DERIVED, not listed: an import wrapped in a `try/except ImportError`
declares its own optionality in code (`google.antigravity`, `openai`), so it is exempt. An
UNGUARDED import is a hard requirement and must be declared. That keeps the exemption set from
drifting out of date — adding a guarded soft dependency needs no edit here, and deleting a guard
turns the import into one this test checks.

Extras count as declared: a lazily imported optional dependency (`uipath`) is legitimate.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path


SRC = Path(__file__).parent.parent / "src"
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

# Import names that are part of this project, not third-party.
FIRST_PARTY = {"coder_eval", "tests"}


def _normalize(name: str) -> str:
    """PEP 503 name normalization, so `python-dotenv` and `python_dotenv` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_distributions() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    specs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)
    # Strip everything after the first version/extras/marker delimiter.
    return {_normalize(re.split(r"[<>=!~\[;\s]", spec, maxsplit=1)[0]) for spec in specs}


def _guards_importerror(handler: ast.ExceptHandler) -> bool:
    """Does this `except` clause catch a missing module?"""
    caught = handler.type
    if caught is None:  # bare `except:` catches it too
        return True
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(n, ast.Name) and n.id in {"ImportError", "ModuleNotFoundError"} for n in names)


def _guarded_import_nodes(tree: ast.AST) -> set[int]:
    """AST node ids of imports sitting inside a `try` whose handler catches a missing module."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_guards_importerror(h) for h in node.handlers):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Import | ast.ImportFrom):
                        guarded.add(id(inner))
    return guarded


def _imported_top_level_modules() -> dict[str, list[str]]:
    """Map each REQUIRED (unguarded) imported top-level module name -> the files importing it."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded = _guarded_import_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative (first-party) import.
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for name in names:
                found.setdefault(name.split(".")[0], []).append(str(path.relative_to(SRC)))
    return found


def test_every_third_party_import_is_a_declared_dependency() -> None:
    declared = _declared_distributions()
    mapping = packages_distributions()
    undeclared: list[str] = []
    unresolvable: list[str] = []

    for module, importers in sorted(_imported_top_level_modules().items()):
        if module in FIRST_PARTY or module in sys.stdlib_module_names:
            continue
        dists = mapping.get(module)
        if not dists:
            unresolvable.append(f"{module} (imported by {importers[0]})")
            continue
        if not any(_normalize(d) in declared for d in dists):
            undeclared.append(f"{module} -> {sorted(dists)} (imported by {', '.join(sorted(set(importers)))})")

    assert not undeclared, (
        "These modules are imported under src/ but their distribution is not declared in "
        f"pyproject.toml: {undeclared}. They are reaching the environment TRANSITIVELY, so "
        "`uv sync --frozen` installs them and this suite passes — until a direct dependency "
        "drops or renames its own dependency, at which point a fresh `pip install coder-eval` "
        "stops installing them and the import fails for real users. Declare each one."
    )
    assert not unresolvable, (
        f"Could not map these imported modules to any installed distribution: {unresolvable}. "
        "Either the module is missing from this environment (install it) or the import is dead."
    )
