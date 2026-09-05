"""CE047 — every marketing/onboarding surface must name every built-in agent.

The roster of supported harnesses is restated in prose on a handful of surfaces
that nothing mechanically ties to the code: the README, the docs home, the
comparison page, ``llms.txt``, the ``mkdocs.yml`` site description, the Pages
stub, and the packaging metadata. Adding a harness means remembering all seven —
which is exactly how **OpenCode shipped while being absent from most of them**:
the agent worked, but a reader (or a crawler, or an LLM answering "which agents
does Coder Eval support?") was told it did not exist. There is no error, no test
failure, and no user report for that; the surface just quietly under-sells the
framework.

``AgentKind`` is the framework's own list of built-ins (it is deliberately NOT the
closed set of valid ``agent.type`` values — the ``AgentRegistry`` is authoritative
and plugins extend it — but every built-in *is* in the enum, and a built-in is
what these surfaces promise). This rule derives the expected roster from that
enum and asserts each surface mentions each agent by name.

Scope note: this is a **presence** check over a file (or, where a file is mostly
unrelated content, over one extracted region — ``mkdocs.yml``'s
``site_description``, ``pyproject.toml``'s ``description`` + ``keywords``). It
cannot tell a good sentence from a bad one; it only makes "we forgot this harness
exists" impossible to ship. Third-party plugin agents are out of scope — they own
their own docs.

It is intentionally NOT a ``BaseRule`` in ``tests/lint/runner.py``: that runner is
AST-only over ``.py`` files, whereas this reasons over Markdown/YAML/TOML/HTML
surfaces. It is wired as a dedicated test in
``tests/test_custom_lint.py::TestCE047AgentRosterParity`` (precedent: CE026-CE031).
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
# spelling. Matching is case-insensitive and substring-based, so "Codex" also
# covers "OpenAI Codex" and `coder-eval[codex]`.
AGENT_DISPLAY_NAMES: dict[str, tuple[str, ...]] = {
    "claude-code": ("Claude Code",),
    "codex": ("Codex",),
    # Google's harness is named on some surfaces by its model ("Gemini"), which is
    # an acceptable spelling of the same row.
    "antigravity": ("Antigravity", "Gemini"),
    "opencode": ("OpenCode",),
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
)


def roster_kinds() -> list[str]:
    """The user-facing built-in agent kinds, derived from ``AgentKind``."""
    from coder_eval.models import AgentKind

    return sorted(k.value for k in AgentKind if k.value not in NON_ROSTER_KINDS)


def missing_agents_in(text: str, kinds: list[str] | None = None) -> list[str]:
    """Roster kinds that ``text`` names by none of their accepted spellings."""
    haystack = text.lower()
    return [
        kind
        for kind in (kinds if kinds is not None else roster_kinds())
        if not any(name.lower() in haystack for name in AGENT_DISPLAY_NAMES.get(kind, (kind,)))
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
