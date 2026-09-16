"""CE027 — documented framework env vars must be backed by a real consumer.

``Settings`` sets no ``env_prefix`` and uses ``extra="ignore"``, so a documented env
var that nothing consumes is silently dropped at runtime.

The rule scans ``README.md``, ``action.yml`` and ``docs/**`` for assignments
(``NAME=value``) whose name carries a ``FRAMEWORK_ENV_PREFIXES`` prefix. It flags a
name that is neither a ``Settings`` field or alias nor consumed in ``src/``: an
``os.getenv`` / ``os.environ`` read, a ``"NAME=VALUE"`` child-process literal, or
either of those through a named constant.

Blind spot: only assignments are checked, never bare prose mentions, and third-party
prefixes (``AWS_``, ``ANTHROPIC_``, ...) are not scanned.

Not a ``BaseRule``: it reasons over Markdown/YAML, and is wired as
``tests/test_custom_lint.py::TestCE027DocEnvVarParity``.

Rationale: .claude/notes/lint-rules.md § CE027
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices


# A documented token with one of these prefixes is owned by coder-eval and MUST be consumed by it.
# Third-party namespaces (AWS_, ANTHROPIC_, GEMINI_, GITHUB_, EVALBOARD_, PLUGIN_) are excluded on purpose.
FRAMEWORK_ENV_PREFIXES: tuple[str, ...] = (
    "CODER_EVAL_",
    "API_",
    "BEDROCK_",
    "CODEX_",
    "ANTIGRAVITY_",
    "TELEMETRY_",
)

_PREFIX_ALT = "|".join(FRAMEWORK_ENV_PREFIXES)

# A framework-prefixed ``NAME=`` assignment. The lookbehind rejects a name inside a larger token:
# ``secrets.BEDROCK_TOKEN``, ``X-API_KEY=``, ``dir/API_X=``, ``http://API_Y=``, ``C:\\API_Z=``.
# A single ``=`` with no space before it rejects ``API_KEY = "…"`` regex-pattern examples.
_ENV_ASSIGNMENT = re.compile(r"(?<![\w./:\\-])((?:" + _PREFIX_ALT + r")[A-Z0-9_]*[A-Z0-9])=(?!=)")

# A consumer: an ``os.getenv`` / ``os.environ[...]`` / ``os.environ.get`` read of NAME, or the NAME side of an
# inline ``"NAME=VALUE"`` child-process literal. Stricter than "any uppercase literal", so a constant that
# only spells a var name cannot mask a documented-but-unconsumed assignment.
_SRC_ENV_READ = re.compile(r"""(?:getenv\(\s*|environ(?:\.get\(\s*|\[\s*))['"]([A-Z][A-Z0-9_]{2,})['"]""")
_SRC_ENV_VALUE = re.compile(r"""['"]([A-Z][A-Z0-9_]{2,})=[^'"]*['"]""")

# The same two shapes reached through a named constant (``os.environ.get(IN_CONTAINER_ENV)``). Two-step: a
# constant counts only when some module reads it; an unread ``CONST = "CODER_EVAL_BOGUS"`` stays unbacked
# (pinned by ``test_src_scan_requires_a_real_consumer_not_any_literal``).
_SRC_ENV_CONST_DEF = re.compile(r"""^([A-Z][A-Z0-9_]{2,})\s*(?::[^=\n]+)?=\s*['"]([A-Z][A-Z0-9_]{2,})['"]\s*$""", re.M)
_SRC_ENV_CONST_READ = re.compile(r"""(?:getenv\(\s*|environ(?:\.get\(\s*|\[\s*))([A-Z][A-Z0-9_]{2,})\b""")
_SRC_ENV_CONST_VALUE = re.compile(r"""f['"]\{([A-Z][A-Z0-9_]{2,})\}=[^'"]*['"]""")


def settings_env_names() -> set[str]:
    """Uppercased env names Settings actually reads: field names + AliasChoices."""
    from coder_eval.config import Settings

    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name.upper())
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(str(c).upper() for c in alias.choices if isinstance(c, str))
        elif isinstance(alias, str):
            names.add(alias.upper())
    return names


def src_env_literals(src_root: Path) -> set[str]:
    """Env-var names ``src/`` actually consumes: direct ``os.getenv``/``os.environ``
    reads plus the NAME side of inline ``"NAME=VALUE"`` child-process literals."""
    names: set[str] = set()
    const_values: dict[str, str] = {}
    const_reads: set[str] = set()
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        names.update(_SRC_ENV_READ.findall(text))
        names.update(_SRC_ENV_VALUE.findall(text))
        const_values.update(dict(_SRC_ENV_CONST_DEF.findall(text)))
        const_reads.update(_SRC_ENV_CONST_READ.findall(text))
        const_reads.update(_SRC_ENV_CONST_VALUE.findall(text))
    # Step two: a constant is backed only if it is BOTH defined as an env name and read somewhere.
    names.update(const_values[ident] for ident in const_reads & const_values.keys())
    return names


def scan_doc_env_assignments(text: str) -> set[str]:
    """Framework-prefixed env-var names *assigned* (``NAME=…``) in a doc file."""
    return set(_ENV_ASSIGNMENT.findall(text))


def find_unbacked_env_vars(doc_paths: list[Path], src_root: Path) -> dict[str, list[str]]:
    """Map each doc path to the framework-prefixed env vars it assigns that
    nothing in the framework consumes (would be silently dropped at runtime)."""
    valid = settings_env_names() | src_env_literals(src_root)
    findings: dict[str, list[str]] = {}
    for path in doc_paths:
        if not path.is_file():
            continue
        unbacked = sorted(t for t in scan_doc_env_assignments(path.read_text(encoding="utf-8")) if t not in valid)
        if unbacked:
            findings[str(path)] = unbacked
    return findings


def default_doc_paths(repo_root: Path) -> list[Path]:
    """The doc/config surfaces CE027 scans: README, the published action, docs/**."""
    paths = [repo_root / "README.md", repo_root / "action.yml"]
    docs = repo_root / "docs"
    if docs.is_dir():
        for suffix in ("*.md", "*.yaml", "*.yml"):
            paths.extend(sorted(docs.rglob(suffix)))
    return paths
