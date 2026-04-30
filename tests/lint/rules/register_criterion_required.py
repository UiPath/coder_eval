"""CE003: BaseCriterion subclasses in criteria/ must use @register_criterion.

The criteria plugin system relies on auto-discovery via @register_criterion.
A class that inherits BaseCriterion but lacks the decorator will be silently
ignored by the registry, causing a runtime KeyError when that criterion type
appears in a task YAML.
"""

import ast
import re

from tests.lint.rules.base import BaseRule


_CRITERIA_FILE = re.compile(r"[/\\]criteria[/\\](?!_|__init__\.py)")
_BASE_CLASSES = {"BaseCriterion"}


class RegisterCriterionRequired(BaseRule):
    id = "CE003"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_criteria = bool(_CRITERIA_FILE.search(filepath))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._in_criteria:
            base_names = {
                b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "")
                for b in node.bases
            }
            if base_names & _BASE_CLASSES:
                decorator_names = {
                    d.id if isinstance(d, ast.Name) else (d.attr if isinstance(d, ast.Attribute) else "")
                    for d in node.decorator_list
                }
                if "register_criterion" not in decorator_names:
                    self.violation(
                        node,
                        f"class '{node.name}' inherits BaseCriterion but is missing @register_criterion",
                    )
        self.generic_visit(node)
