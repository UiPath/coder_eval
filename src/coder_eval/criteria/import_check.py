"""Import check criterion checker.

Parses Python files with AST to extract all import statements (including those
inside functions and try/except blocks), then validates each import resolves
using importlib in the sandbox environment.

Replaces the previous pattern of:
  - python -m py_compile <file>  (syntax check)
  - python -c 'import <module>'  (dynamic import)

Advantages over dynamic import:
  - Catches bad imports hidden inside try/except or function bodies
  - No false negatives from module-level side effects (e.g. auth errors)
  - Deterministic and clearly interpretable output
"""

import ast
import json
import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, ImportCheckCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def extract_imports(source: str) -> list[str]:
    """Extract all imported module paths from Python source via AST.

    Walks the entire AST including nested scopes (functions, try/except, etc.).

    Args:
        source: Python source code string.

    Returns:
        List of full module paths (deduplicated, order-preserving).
        For ``import foo.bar`` -> ``"foo.bar"``.
        For ``from foo.bar import baz`` -> ``"foo.bar"``.
        All relative imports are skipped (``from . import x``, ``from .bar import baz``,
        ``from ..x import y``, etc.) since ``find_spec`` cannot resolve them without
        knowing the package hierarchy. Only absolute imports are validated.

    Raises:
        SyntaxError: If the source cannot be parsed.
    """
    tree = ast.parse(source)
    seen: set[str] = set()
    modules: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in seen:
                    seen.add(alias.name)
                    modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module not in seen:
            # node.level > 0 means a relative import (from .bar import baz, from ..x import y, etc.)
            # These cannot be resolved with find_spec without knowing the package hierarchy, so skip them.
            seen.add(node.module)
            modules.append(node.module)

    return modules


@register_criterion
class ImportCheckChecker(BaseCriterion[ImportCheckCriterion]):
    """Checker for ImportCheckCriterion.

    Validates that a Python file has valid syntax and all its imports resolve
    in the sandbox environment.
    """

    criterion_type = "import_check"

    def _check_impl(
        self,
        criterion: ImportCheckCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Check that a Python file parses and its imports resolve."""
        # 1. File existence
        if not sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        # 2. Read and parse
        source = sandbox.get_file_content(criterion.path)
        try:
            unique_modules = extract_imports(source)
        except SyntaxError as e:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details=f"Syntax error in '{criterion.path}': {e}",
                error=f"SyntaxError: {e}",
            )

        # 3. No imports to check
        if not unique_modules:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=1.0,
                details="No imports to check",
            )

        # 4. Validate imports via sandbox Python using find_spec (no code execution).
        #    Use python3 for portability (python may not exist on some systems).
        check_script = (
            f"import importlib.util, json; "
            f"modules = {unique_modules!r}; "
            f"results = {{}}; "
            f"exec('for m in modules:\\n"
            f" try:\\n"
            f"  results[m] = importlib.util.find_spec(m) is not None\\n"
            f" except (ModuleNotFoundError, ValueError):\\n"
            f"  results[m] = False'); "
            f"print(json.dumps(results))"
        )

        exit_code, stdout, _stderr = sandbox.run_command(
            f'python3 -c "{check_script}"',
            timeout=criterion.timeout,
        )

        # 5. Parse results
        failed_imports: list[str] = []
        valid_count = 0

        if exit_code == 0 and stdout.strip():
            try:
                results = json.loads(stdout.strip().splitlines()[-1])
                for module in unique_modules:
                    if results.get(module, False):
                        valid_count += 1
                    else:
                        failed_imports.append(module)
            except (json.JSONDecodeError, IndexError):
                failed_imports = list(unique_modules)
        else:
            failed_imports = list(unique_modules)

        total = len(unique_modules)
        score = valid_count / total

        # 6. Build details
        details_parts = [f"Checked: {total}, resolved: {valid_count}/{total}"]
        if failed_imports:
            details_parts.append(f"Failed: {', '.join(failed_imports)}")

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details="; ".join(details_parts),
        )
