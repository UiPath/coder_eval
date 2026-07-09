"""CE020: No ``BaseAgentConfig`` field may be typed against ``claude_agent_sdk``.

``BaseAgentConfig`` is the vendor-neutral Pydantic base shared by every agent kind
(Claude Code, Codex, NoOp, and third-party BYOA configs). A field on it whose
annotation references a ``claude_agent_sdk`` type leaks a Claude-Code-specific type
onto agents that have nothing to do with the Claude SDK — the leaky-abstraction this
refactor removed (``plugins: list[SdkPluginConfig]`` → local ``LocalPluginConfig``;
``setting_sources`` moved down to ``ClaudeCodeAgentConfig``).

The boundary is mechanically detectable, so this rule guards it: any ``AnnAssign``
field inside the ``BaseAgentConfig`` class body whose annotation references a name
imported from ``claude_agent_sdk`` is flagged. Fix by defining a local vendor-neutral
type (alias / TypedDict mirror) or moving the field down to the concrete subclass that
actually needs the SDK type.

Scope: only ``models/agent_config.py`` is inspected. The rule is deliberately narrow:
- A module-level use of an SDK type (e.g. ``dataclasses.fields(ClaudeAgentOptions)``)
  is NOT flagged — only ``AnnAssign`` annotations inside ``BaseAgentConfig`` are.
- SDK-typed fields on subclasses (``ClaudeCodeAgentConfig`` etc.) are allowed.

Import forms caught (so the leak can't sneak back via a different import style):
- ``from claude_agent_sdk import SettingSource`` (and aliased ``... as SS``).
- ``from claude_agent_sdk.types import SettingSource`` (submodule path).
- ``import claude_agent_sdk`` / ``import claude_agent_sdk as sdk`` used in attribute
  form (``list[sdk.SettingSource]``).

Not caught: ``from claude_agent_sdk import *`` (a wildcard hides the names) — but
ruff F403/F405 already bans star imports, so that hole is covered upstream.

Add ``# noqa: CE020`` on the offending field for a deliberate exception.
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
