"""CE026 — the GitHub Action's onboarding surfaces must stay truthful and self-sufficient.

Several surfaces introduce the same composite Action — ``README.md``,
``docs/CI_GATE.md``, ``docs/tutorials/02-ci-pipeline.md``, and now the Claude Code
plugin's ``ci`` skill, whose emitted workflow users copy verbatim — and each was
hand-maintained, so they drifted. The motivating bug: ``docs/CI_GATE.md`` claimed "there is nothing to
install" and offered a copy-pasteable ``uses:`` step with no agent runtime — the action
is agent-agnostic, so an integrator who copied it got a run that dies on a missing
``claude`` binary. The correcting paragraph was 11 lines away; the tutorial's snippet
showed the prerequisite steps; the reference page's did not.

Four clauses, all mechanical:

1. **Prerequisite parity.** The *first* fenced ``yaml`` block on a doc page that
   references the action (``uses: <owner>/coder_eval@…``) is the page's quickstart, so
   it must also show the agent-runtime steps. Later blocks on the same page are
   single-input illustrations and are skipped, which is what keeps the rule quiet.
2. **No unqualified zero-install absolute** in prose near such a block. This catches
   the *phrase*, not the *contradiction*: judging whether a paragraph 11 lines later
   states a real prerequisite is semantic reasoning no static rule should attempt, so
   the rule instead forces the absolute to be scoped where it is written
   ("no *Marketplace* install step").
3. **Marketplace slug parity.** Every ``github.com/marketplace/actions/<slug>`` link and
   the shields badge label must match ``action.yml``'s ``name:`` — the listing title,
   which a rename would silently 404 in four places at once.
4. **Input parity.** Every ``with:`` key on a snippet's ``uses: <owner>/coder_eval@…``
   step must be a real ``action.yml`` input. GitHub does not fail a workflow on an
   unknown input, so a renamed input leaves every snippet promising something the step
   no longer does — silently, and worst of all in the ``ci`` skill, whose output lands
   in *other people's* repositories where our CI can never see it.

Like CE027-CE031 this is deliberately NOT a ``BaseRule`` in ``tests/lint/runner.py``:
that runner is AST-only over ``.py`` files, whereas this rule reasons over Markdown and
YAML. It is wired as ``tests/test_custom_lint.py::TestCE026ActionDocSurfaces``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from tests.lint.doc_examples import extract_yaml_blocks


PREREQ_SKIP_MARKER = "<!-- lint-skip: action-prereq"
CLAIM_SKIP_MARKER = "<!-- lint-skip: absolute-claim"

# The agent-runtime prerequisite steps a page's primary Action snippet must show.
# Pinned to the executable reference rather than hand-maintained: the `action-dogfood`
# job in .github/workflows/pr-checks.yml runs exactly these before `uses: ./`, and
# TestCE026ActionDocSurfaces asserts the two stay in sync.
REQUIRED_PREREQ_TOKENS = ("actions/setup-node", "npm install -g @anthropic-ai/claude-code")

DOGFOOD_JOB = "action-dogfood"

# Absolutes that read as "no prerequisites" when they mean "no Marketplace install step".
ABSOLUTE_CLAIMS = (
    "nothing to install",
    "no setup required",
    "nothing to configure",
    "no prerequisites",
    "zero setup",
)
# A claim is fine when the same line says which install channel it is talking about.
CLAIM_QUALIFIERS = ("marketplace", "pypi", "npm", "pip install")
# How close prose must be to an Action snippet for the claim clause to apply.
CLAIM_PROXIMITY_LINES = 15

_ACTION_USES = re.compile(r"uses:\s*[\w.-]+/coder_eval@")
# The same reference as a parsed YAML *value* (``uses: UiPath/coder_eval@v0`` -> the value).
_ACTION_REF = re.compile(r"^[\w.-]+/coder_eval@")
_MARKETPLACE_URL = re.compile(r"github\.com/marketplace/actions/([\w.-]+)")
_SHIELDS_MARKETPLACE = re.compile(r"img\.shields\.io/badge/marketplace-([^-\s)]+)-")


@dataclass(frozen=True)
class Finding:
    """One CE026 violation, rendered as ``path:line — message``."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} — {self.message}"


def default_doc_paths(repo_root: Path) -> list[Path]:
    """Markdown surfaces that may introduce the Action: README, every docs page, and
    every plugin markdown file (the `ci` skill emits an Action snippet users copy verbatim)."""
    paths = [repo_root / "README.md"]
    paths.extend(sorted(p for p in (repo_root / "docs").rglob("*.md") if p.is_file()))
    paths.extend(sorted(p for p in (repo_root / "plugins").rglob("*.md") if p.is_file()))
    return [p for p in paths if p.is_file()]


def _preceding_line(lines: list[str], fence_line: int) -> str:
    """The line above a 1-based opening fence, where an opt-out marker would sit."""
    idx = fence_line - 2
    return lines[idx] if 0 <= idx < len(lines) else ""


def find_missing_prereqs(paths: list[Path]) -> list[Finding]:
    """Flag a page whose primary Action snippet omits the agent-runtime steps."""
    findings: list[Finding] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if not _ACTION_USES.search(text):
            continue
        lines = text.splitlines()
        primary = next((b for b in extract_yaml_blocks(path, text) if _ACTION_USES.search(b.text)), None)
        if primary is None:
            continue
        if PREREQ_SKIP_MARKER in _preceding_line(lines, primary.line):
            continue
        missing = [tok for tok in REQUIRED_PREREQ_TOKENS if tok not in primary.text]
        if missing:
            findings.append(
                Finding(
                    path,
                    primary.line,
                    "the page's first Action snippet is copy-pasteable but omits the agent-runtime "
                    f"prerequisite(s) {', '.join(repr(m) for m in missing)}; a task using the default "
                    "claude-code agent fails on a missing `claude` binary. Add the steps (as "
                    "docs/tutorials/02-ci-pipeline.md does) or opt out with "
                    f"`{PREREQ_SKIP_MARKER}: <reason> -->` above the fence",
                )
            )
    return findings


def find_unscoped_absolute_claims(paths: list[Path]) -> list[Finding]:
    """Flag an unqualified "nothing to install" in prose next to an Action snippet."""
    findings: list[Finding] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        snippet_lines = [i for i, line in enumerate(lines) if _ACTION_USES.search(line)]
        if not snippet_lines:
            continue
        for i, line in enumerate(lines):
            lowered = line.lower()
            claim = next((c for c in ABSOLUTE_CLAIMS if c in lowered), None)
            if claim is None or CLAIM_SKIP_MARKER in line:
                continue
            if any(q in lowered for q in CLAIM_QUALIFIERS):
                continue
            if not any(abs(i - s) <= CLAIM_PROXIMITY_LINES for s in snippet_lines):
                continue
            findings.append(
                Finding(
                    path,
                    i + 1,
                    f'unqualified absolute "{claim}" next to an Action snippet — the action installs '
                    "no coding-agent runtime, so this reads as 'no prerequisites'. Scope it to the "
                    f"channel you mean (e.g. 'no Marketplace install step') or add "
                    f"`{CLAIM_SKIP_MARKER}: <reason> -->` on the line",
                )
            )
    return findings


def action_listing_name(action_yml: Path) -> str:
    """The Marketplace listing title declared by ``action.yml``'s ``name:``."""
    data = yaml.safe_load(action_yml.read_text(encoding="utf-8"))
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AssertionError(f"{action_yml} declares no usable `name:` — the Marketplace listing title")
    return name.strip()


def marketplace_slug(listing_name: str) -> str:
    """GitHub's Marketplace URL slug for a listing title."""
    slug = re.sub(r"\s+", "-", listing_name.strip().lower())
    return re.sub(r"[^a-z0-9._-]", "", slug)


def decode_shields_label(label: str) -> str:
    """Decode a shields.io badge label back to its displayed text.

    shields renders ``_`` as a space and ``__`` / ``--`` as a literal ``_`` / ``-``,
    which is why the badge for ``coder_eval`` must be written ``coder__eval``.
    """
    placeholder_underscore, placeholder_hyphen = "\x00", "\x01"
    decoded = label.replace("__", placeholder_underscore).replace("--", placeholder_hyphen)
    decoded = decoded.replace("_", " ")
    return decoded.replace(placeholder_underscore, "_").replace(placeholder_hyphen, "-")


def find_slug_mismatches(paths: list[Path], listing_name: str) -> list[Finding]:
    """Flag Marketplace links/badges that disagree with ``action.yml``'s ``name:``."""
    expected_slug = marketplace_slug(listing_name)
    findings: list[Finding] = []
    for path in paths:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            for match in _MARKETPLACE_URL.finditer(line):
                if match.group(1) != expected_slug:
                    findings.append(
                        Finding(
                            path,
                            i + 1,
                            f"Marketplace link slug {match.group(1)!r} does not match action.yml "
                            f"`name: {listing_name}` (expected {expected_slug!r}) — the link 404s",
                        )
                    )
            for match in _SHIELDS_MARKETPLACE.finditer(line):
                shown = decode_shields_label(match.group(1))
                if shown != listing_name:
                    findings.append(
                        Finding(
                            path,
                            i + 1,
                            f"Marketplace badge label {match.group(1)!r} displays as {shown!r}, not "
                            f"action.yml `name: {listing_name}` (shields renders a single `_` as a "
                            "space, so the doubled form is required)",
                        )
                    )
    return findings


def action_input_names(action_yml: Path) -> set[str]:
    """The input names ``action.yml`` actually declares."""
    data = yaml.safe_load(action_yml.read_text(encoding="utf-8"))
    inputs = data.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise AssertionError(f"{action_yml} declares no usable `inputs:` block")
    return set(inputs)


def _iter_action_steps(node: object) -> Iterator[dict]:
    """Every mapping in a parsed YAML block that invokes the composite Action.

    Walks to any depth so it finds the step whether the snippet is a whole
    workflow (``jobs.<id>.steps``), a bare list of steps, or one step alone —
    all three shapes appear across the doc pages.
    """
    if isinstance(node, dict):
        uses = node.get("uses")
        if isinstance(uses, str) and _ACTION_REF.match(uses.strip()):
            yield node
        for value in node.values():
            yield from _iter_action_steps(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_action_steps(item)


def find_unknown_action_inputs(paths: list[Path], input_names: set[str]) -> list[Finding]:
    """Flag a snippet passing a ``with:`` key that ``action.yml`` does not declare."""
    findings: list[Finding] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if not _ACTION_USES.search(text):
            continue
        for block in extract_yaml_blocks(path, text):
            try:
                parsed = yaml.safe_load(block.text)
            except yaml.YAMLError:
                # Doc snippets are often deliberate fragments; an unparseable one
                # is not this clause's business (CE029 owns example validity).
                continue
            for step in _iter_action_steps(parsed):
                with_block = step.get("with")
                if not isinstance(with_block, dict):
                    continue
                for key in sorted(k for k in with_block if k not in input_names):
                    findings.append(
                        Finding(
                            path,
                            block.line,
                            f"snippet passes `with: {key}:`, which action.yml does not declare "
                            f"(inputs: {', '.join(sorted(input_names))}). GitHub does not fail a "
                            "workflow on an unknown input, so a reader who copies this gets a step "
                            "that silently ignores it",
                        )
                    )
    return findings


def dogfood_prereq_tokens(workflow: Path, job: str = DOGFOOD_JOB) -> set[str]:
    """Tokens for every step the dogfood job runs before invoking the local action.

    This is the executable proof that ``REQUIRED_PREREQ_TOKENS`` is still the real
    prerequisite set: CI runs these steps before ``uses: ./`` and the job passes.
    """
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = data["jobs"][job]["steps"]
    tokens: set[str] = set()
    for step in steps:
        uses = step.get("uses", "")
        if uses.strip() in {"./", "."}:
            break
        if uses:
            tokens.add(uses.split("@", 1)[0])
        run = step.get("run")
        if isinstance(run, str):
            tokens.update(line.strip() for line in run.splitlines() if line.strip())
    return tokens
