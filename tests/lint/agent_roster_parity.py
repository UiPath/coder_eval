"""CE047 — every marketing/onboarding surface must name every built-in agent.

The expected roster is ``AgentKind`` minus ``NON_ROSTER_KINDS``. Each surface in
``ROSTER_SURFACES`` must name each agent by one of its ``AGENT_DISPLAY_NAMES``
spellings. ``AgentKind`` lists the built-ins only; the ``AgentRegistry`` stays
authoritative for valid ``agent.type`` values. Third-party plugin agents are out of
scope.

Blind spot: this is a presence check over a file, or over one extracted region
(``mkdocs.yml``'s ``site_description``, ``pyproject.toml``'s ``description`` +
``keywords``). It cannot tell a good sentence from a bad one.

Not a ``BaseRule``: it reasons over Markdown/YAML/TOML/HTML, and is wired as
``tests/test_custom_lint.py::TestCE047AgentRosterParity``.

Rationale: .claude/notes/lint-rules.md § CE047
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from pathlib import Path


# Agent kinds that are NOT part of the user-facing roster: `none` is the agentless
# system-task escape hatch and `unknown` is an internal sentinel for a task whose
# type could not be resolved. Neither is a harness anyone installs.
NON_ROSTER_KINDS: frozenset[str] = frozenset({"none", "unknown"})

# How each built-in agent is spelled in prose. Keyed by the `AgentKind` VALUE so a
# new built-in fails `test_every_builtin_kind_has_display_names` until its prose
# name is declared here — the enum stays the trigger, this table is only the
# spelling. Matching is case-insensitive and WORD-BOUNDARY anchored (see
# `missing_agents_in`), so a short name like "Pi" matches the standalone word but
# NOT "anthropic" / "ci-pipeline" / "copies"; "Codex" still covers "OpenAI Codex"
# and the bracketed `coder-eval[codex]`.
AGENT_DISPLAY_NAMES: dict[str, tuple[str, ...]] = {
    # The hyphenated `claude-code` spelling is listed alongside the prose form so
    # the word-boundary matcher accepts the packaging-metadata keyword too
    # (`\bClaude Code\b` would not match the hyphenated form).
    "claude-code": ("Claude Code", "claude-code"),
    "codex": ("Codex",),
    # Google's harness is named on some surfaces by its model ("Gemini"), which is
    # an acceptable spelling of the same row.
    "antigravity": ("Antigravity", "Gemini"),
    "opencode": ("OpenCode",),
    "pi": ("Pi",),
    "delegate": ("Delegate",),
}


def _whole_file(text: str) -> str:
    return text


def _mkdocs_site_description(text: str) -> str:
    """The `site_description:` block scalar — the rest of mkdocs.yml is nav/theme."""
    match = re.search(r"^site_description:.*?(?=^\S)", text, re.MULTILINE | re.DOTALL)
    return match.group(0) if match else ""


def _pyproject_marketing_text(text: str) -> str:
    """`description` + `keywords` — the strings PyPI shows and search engines index."""
    project = tomllib.loads(text).get("project", {})
    return project.get("description", "") + "\n" + "\n".join(project.get("keywords", []))


# (repo-relative path, why it matters, region extractor). Every entry must exist.
ROSTER_SURFACES: tuple[tuple[str, str, Callable[[str], str]], ...] = (
    ("README.md", "the GitHub landing page", _whole_file),
    ("docs/index.md", "the docs home on coder-eval.com", _whole_file),
    ("docs/comparison.md", "the 'how it compares' page", _whole_file),
    ("docs/llms.txt", "what LLMs read to answer questions about the project", _whole_file),
    ("mkdocs.yml", "the docs-site description (search results, link previews)", _mkdocs_site_description),
    (".github/pages-stub/index.html", "the coder-eval.com root stub", _whole_file),
    ("pyproject.toml", "the PyPI description and keywords", _pyproject_marketing_text),
    ("docs/USER_GUIDE.md", "the --type list and env-var table a user actually configures from", _whole_file),
    ("experiments/default.yaml", "the always-loaded default layer's agent.type comment", _whole_file),
    ("docs/EXTENDING.md", "the 'See also' built-in agent list", _whole_file),
)


def roster_kinds() -> list[str]:
    """The user-facing built-in agent kinds, derived from ``AgentKind``."""
    from coder_eval.models import AgentKind

    return sorted(k.value for k in AgentKind if k.value not in NON_ROSTER_KINDS)


def missing_agents_in(text: str, kinds: list[str] | None = None) -> list[str]:
    """Roster kinds that ``text`` names by none of their accepted spellings."""
    return [
        kind
        for kind in (kinds if kinds is not None else roster_kinds())
        if not any(
            re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) for name in AGENT_DISPLAY_NAMES.get(kind, (kind,))
        )
    ]


def find_roster_gaps(repo_root: Path) -> dict[str, list[str]]:
    """Map each surface that under-sells the roster to the agents it never names."""
    kinds = roster_kinds()
    gaps: dict[str, list[str]] = {}
    for rel, _why, extract in ROSTER_SURFACES:
        path = repo_root / rel
        if not path.is_file():
            gaps[rel] = ["<surface missing>"]
            continue
        missing = missing_agents_in(extract(path.read_text(encoding="utf-8")), kinds)
        if missing:
            gaps[rel] = missing
    return gaps
