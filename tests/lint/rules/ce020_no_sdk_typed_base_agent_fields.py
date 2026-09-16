"""CE020: No ``BaseAgentConfig`` field may be typed against ``claude_agent_sdk``.

``BaseAgentConfig`` is shared by every agent kind, so an ``AnnAssign`` in its class
body whose annotation references a name from ``claude_agent_sdk`` is flagged. Fix
with a local vendor-neutral type (alias / TypedDict mirror), or move the field to
the subclass that needs the SDK type.

Scope: only ``models/agent_config.py``. Module-level SDK uses and SDK-typed fields
on subclasses (``ClaudeCodeAgentConfig`` etc.) are allowed.

Import forms caught:
- ``from claude_agent_sdk import SettingSource`` (and aliased ``... as SS``).
- ``from claude_agent_sdk.types import SettingSource`` (submodule path).
- ``import claude_agent_sdk [as sdk]`` used in attribute form
  (``list[sdk.SettingSource]``).

BLIND SPOT: ``from claude_agent_sdk import *`` hides the names; ruff F403/F405
(``pyproject.toml``) bans star imports.

Add ``# noqa: CE020`` on the offending field for a deliberate exception.

Rationale: .claude/notes/lint-rules.md § CE020
"""

import ast
import re

from tests.lint.rules.base import BaseRule
from tests.lint.violation import Violation


_TARGET_FILE = re.compile(r"[/\\]models[/\\]agent_config\.py$")
_SDK_MODULE = "claude_agent_sdk"
_BASE_CLASS = "BaseAgentConfig"


def _is_sdk_module(module: str | None) -> bool:
    """True for ``claude_agent_sdk`` and any submodule (``claude_agent_sdk.types``).

    Guards the ``.`` boundary so an unrelated ``claude_agent_sdkx`` does not match.
    """
    return module is not None and (module == _SDK_MODULE or module.startswith(f"{_SDK_MODULE}."))


class NoSdkTypedBaseAgentFields(BaseRule):
    id = "CE020"

    def __init__(self, filepath: str) -> None:
        super().__init__(filepath)
        self._in_target = bool(_TARGET_FILE.search(filepath))

    def check(self, tree: ast.AST) -> list[Violation]:
        if not self._in_target:
            return self.violations

        # First pass: collect SDK-bound names. ``sdk_names`` holds names bound by
        # ``from claude_agent_sdk[.sub] import X`` (the asname wins for ``... as Y``);
        # ``sdk_modules`` holds module aliases bound by ``import claude_agent_sdk[ as sdk]``
        # so attribute-form annotations (``sdk.SettingSource``) are also caught.
        sdk_names: set[str] = set()
        sdk_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _is_sdk_module(node.module):
                for alias in node.names:
                    sdk_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_sdk_module(alias.name):
                        sdk_modules.add(alias.asname or alias.name)

        if not sdk_names and not sdk_modules:
            return self.violations

        # Second pass: inspect BaseAgentConfig field annotations only.
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == _BASE_CLASS:
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign):
                        self._check_field(stmt, sdk_names, sdk_modules)

        return self.violations

    def _check_field(self, stmt: ast.AnnAssign, sdk_names: set[str], sdk_modules: set[str]) -> None:
        target = stmt.target.id if isinstance(stmt.target, ast.Name) else "<field>"
        for sub in ast.walk(stmt.annotation):
            ref = None
            if isinstance(sub, ast.Name) and sub.id in sdk_names:
                ref = sub.id
            elif isinstance(sub, ast.Attribute):
                # Bare ``X.attr`` where X is a bound SDK module (``sdk.SettingSource``).
                if isinstance(sub.value, ast.Name) and sub.value.id in sdk_modules:
                    ref = f"{sub.value.id}.{sub.attr}"
                elif sub.attr in sdk_names:
                    ref = sub.attr
            if ref:
                self.violation(
                    stmt,
                    f"BaseAgentConfig field {target!r} is typed against {_SDK_MODULE} ({ref!r}); "
                    "the vendor-neutral base must use a local type — move the field to "
                    "ClaudeCodeAgentConfig or define a local alias.",
                )
                return
