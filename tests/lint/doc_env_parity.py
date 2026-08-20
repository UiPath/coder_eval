"""CE027 — documented framework env vars must be backed by a real consumer.

``coder_eval.config.Settings`` sets no ``env_prefix`` and uses ``extra="ignore"``,
so a documented env var whose name does not match a ``Settings`` field (or one of
its ``AliasChoices``) is **silently dropped** at runtime with zero signal — the
exact failure mode behind the ``CODER_EVAL_API_BACKEND`` doc bug (the real field
is ``API_BACKEND``, so the ``CODER_EVAL_``-prefixed spelling selected no backend
and the run fell back to Direct Anthropic).

This rule scans the doc/config surfaces (``README.md``, ``action.yml``,
``docs/**``) for env-var **assignments** (``NAME=value`` — the copy-pasteable,
dangerous form) carrying a **framework-owned prefix** and flags any whose name is
neither a ``Settings`` env name/alias nor referenced anywhere in ``src/`` — the
framework also reads a handful of vars directly via ``os.getenv`` (e.g.
``CODER_EVAL_SKILLS_DIR``, ``CODEX_BASE_URL``, ``CODER_EVAL_IN_CONTAINER``), and
those are legitimately documentable.

Scope note: only *assignments* are checked, not bare prose mentions. Prose
scanning is too false-positive-prone (markdown links like
``CODEX_AGENT_GUIDE.md``, secret RHS references like ``secrets.BEDROCK_TOKEN``,
regex-pattern examples like ``API_KEY = "…"``), and the assignment form is the
one users actually copy into a workflow, so it carries the real risk.

It is intentionally NOT a ``BaseRule`` registered in ``tests/lint/runner.py``:
that runner is AST-only and walks ``.py`` files, whereas this rule reasons over
Markdown/YAML doc surfaces. It is wired as a dedicated test in
``tests/lint_tests/test_lint_doc_surfaces.py::TestCE027DocEnvVarParity``.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices


# Framework-owned env-var name prefixes. A documented token starting with one of
# these is owned by coder-eval and MUST be consumed by it. Deliberately EXCLUDES
# broad third-party namespaces (AWS_, ANTHROPIC_, GEMINI_, GITHUB_, EVALBOARD_,
# PLUGIN_) whose vars are consumed by SDKs / CI, not necessarily via Settings, so
# scanning them would produce false positives on legitimately-external names.
FRAMEWORK_ENV_PREFIXES: tuple[str, ...] = (
    "CODER_EVAL_",
    "API_",
    "BEDROCK_",
    "CODEX_",
    "ANTIGRAVITY_",
    "TELEMETRY_",
)

_PREFIX_ALT = "|".join(FRAMEWORK_ENV_PREFIXES)

# A framework-prefixed env-var ASSIGNMENT in doc/config text: ``NAME=`` where NAME
# carries a framework prefix. The negative lookbehind rejects a name embedded in
# a larger token — attribute access (``secrets.BEDROCK_TOKEN``), a hyphenated
# token (``X-API_KEY=``), a path/URL segment (``dir/API_X=``, ``http://API_Y=``),
# or a Windows path (``C:\\API_Z=``). The single ``=`` (not ``==``) with no space
# before it also rejects ``API_KEY = "…"`` regex-pattern examples.
_ENV_ASSIGNMENT = re.compile(r"(?<![\w./:\\-])((?:" + _PREFIX_ALT + r")[A-Z0-9_]*[A-Z0-9])=(?!=)")

# How the framework actually *consumes* an env var, so a documented assignment is
# only "backed" if some module reads it: a direct ``os.getenv("NAME")`` /
# ``os.environ["NAME"]`` / ``os.environ.get("NAME")`` read, or the NAME side of an
# inline ``"NAME=VALUE"`` literal handed to a child process (e.g. docker
# ``--env NAME=1``). This is stricter than "any uppercase literal" so an unrelated
# constant that merely spells a var name cannot silently mask a documented-but-
# unconsumed assignment.
_SRC_ENV_READ = re.compile(r"""(?:getenv\(\s*|environ(?:\.get\(\s*|\[\s*))['"]([A-Z][A-Z0-9_]{2,})['"]""")
_SRC_ENV_VALUE = re.compile(r"""['"]([A-Z][A-Z0-9_]{2,})=[^'"]*['"]""")


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
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        names.update(_SRC_ENV_READ.findall(text))
        names.update(_SRC_ENV_VALUE.findall(text))
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
