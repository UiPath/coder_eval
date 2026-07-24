"""CE029 — self-contained YAML examples in the docs must validate against their models.

A published example that does not parse is worse than no example: readers copy it,
hit a ``ValidationError``, and conclude the feature is broken. This rule caught
exactly that — the ``prompt_mutations`` recipe in ``docs/AB_EXPERIMENTS.md`` used
``text:`` where the field is ``content:``, and every mutation model declares
``extra="forbid"``, so the published snippet raised
``variants.1.prompt_mutations.0.suffix.content Field required``.

Scope is deliberately narrow — the rule only validates blocks it can *prove* are
whole documents, because a false positive on an illustrative fragment would make
``make lint`` a nuisance and get the rule deleted:

* a block is a **task** when it has ``task_id`` + ``initial_prompt`` +
  ``success_criteria``, and an **experiment** when it has ``experiment_id`` +
  ``variants``. Anything else is a fragment and is skipped — including a bare
  ``success_criteria:`` list, which is the single most common doc shape.
* **schematic** blocks are skipped: a doc that writes ``agent: { ... }`` to mean
  "and so on" parses to the literal key ``"..."``. The task guide's overview block
  uses that form deliberately.
* a block that is not valid YAML at all is skipped rather than reported — it
  cannot be classified, so the rule has no basis for claiming it is a broken
  *example* as opposed to deliberately-invalid illustrative text.
* escape hatch: a block preceded by ``<!-- lint-skip: doc-yaml -->`` is skipped,
  for a future example that is intentionally partial in a way the heuristic
  cannot see.

Like CE027, this is intentionally NOT a ``BaseRule`` registered in
``tests/lint/runner.py``: that runner is AST-only and walks ``.py`` files, whereas
this rule reasons over Markdown. It is wired as a dedicated test in
``tests/test_custom_lint.py::TestCE029DocYamlExamples``.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


SKIP_MARKER = "<!-- lint-skip: doc-yaml -->"

_YAML_INFO_STRINGS = frozenset({"yaml", "yml"})

# Keys that make a block a whole document rather than an illustrative fragment.
_TASK_KEYS = frozenset({"task_id", "initial_prompt", "success_criteria"})
_EXPERIMENT_KEYS = frozenset({"experiment_id", "variants"})

# The "and so on" placeholder docs use inside a flow mapping (``agent: { ... }``),
# which YAML parses as the literal key ``...``.
_SCHEMATIC = "..."


@dataclass(frozen=True)
class YamlBlock:
    """One fenced ```yaml block lifted out of a Markdown file."""

    path: Path
    line: int  # 1-based line number of the opening fence
    text: str
    skip_marked: bool


def extract_yaml_blocks(path: Path, text: str) -> list[YamlBlock]:
    """Pull every fenced ```yaml / ```yml block out of a Markdown document.

    Tracks the opening fence's exact backtick run so a nested fence (pymdownx
    superfences allows ````) closes against the right marker rather than the
    first ``` it meets.
    """
    blocks: list[YamlBlock] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped.startswith("```"):
            i += 1
            continue
        ticks = len(stripped) - len(stripped.lstrip("`"))
        # First info-string token only — mkdocs-material allows attributes
        # (```yaml title="x"), and skipping those would silently drop a block.
        info = stripped[ticks:].strip().lower().split()
        if not info or info[0] not in _YAML_INFO_STRINGS:
            i += 1
            continue
        fence = "`" * ticks
        start = i
        body: list[str] = []
        i += 1
        while i < len(lines):
            closing = lines[i].strip()
            if closing.startswith(fence) and not closing[ticks:].strip():
                break
            body.append(lines[i])
            i += 1
        blocks.append(
            YamlBlock(
                path=path,
                line=start + 1,
                text="\n".join(body),
                skip_marked=_has_skip_marker(lines, start),
            )
        )
        i += 1
    return blocks


def _has_skip_marker(lines: list[str], fence_index: int) -> bool:
    """True when ``<!-- lint-skip: doc-yaml -->`` precedes the fence (blank lines allowed)."""
    j = fence_index - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    return j >= 0 and lines[j].strip() == SKIP_MARKER


def is_schematic(data: Any) -> bool:
    """True when the parsed block uses the literal ``...`` placeholder anywhere.

    ``agent: { ... }`` means "and other keys" to a reader and parses to the key
    ``"..."``. Such a block documents *shape*, not a runnable document.
    """
    if isinstance(data, dict):
        return any(k == _SCHEMATIC or is_schematic(v) for k, v in data.items())
    if isinstance(data, list):
        return any(is_schematic(v) for v in data)
    return data == _SCHEMATIC


def classify(data: Any) -> str | None:
    """``"task"``, ``"experiment"``, or None when the block is a fragment."""
    if not isinstance(data, dict):
        return None
    keys = set(data)
    if keys >= _TASK_KEYS:
        return "task"
    if keys >= _EXPERIMENT_KEYS:
        return "experiment"
    return None


def validate_block(block: YamlBlock) -> str | None:
    """Validate one block; return an error string, or None when it passes or is skipped."""
    from coder_eval.models import ExperimentDefinition, TaskDefinition

    if block.skip_marked:
        return None
    try:
        data = yaml.safe_load(block.text)
    except yaml.YAMLError:
        return None  # unclassifiable — see the module docstring
    if is_schematic(data):
        return None
    kind = classify(data)
    if kind is None:
        return None

    model = TaskDefinition if kind == "task" else ExperimentDefinition
    try:
        with warnings.catch_warnings():
            # Unknown top-level task fields warn rather than raise (soft-launch
            # policy); that is CE009's territory, not this rule's.
            warnings.simplefilter("ignore")
            model.model_validate(data)
    except Exception as exc:  # any validation failure is the finding
        return f"line {block.line} ({kind}): {_first_error(exc)}"
    return None


def _first_error(exc: Exception) -> str:
    """The first concrete validation error, not Pydantic's "N validation errors" header.

    The header alone ("2 validation errors for ExperimentDefinition") tells an
    author nothing about which key is wrong, which is the whole point of the rule.
    """
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
        except (IndexError, TypeError):
            return str(exc).strip().splitlines()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return f"{loc}: {first.get('msg', '')}".strip(": ")
    return str(exc).strip().splitlines()[0]


def find_invalid_doc_examples(doc_paths: list[Path]) -> dict[str, list[str]]:
    """Map each doc path to the self-contained YAML examples in it that don't validate."""
    findings: dict[str, list[str]] = {}
    for path in doc_paths:
        if not path.is_file():
            continue
        errors = [
            err
            for block in extract_yaml_blocks(path, path.read_text(encoding="utf-8"))
            if (err := validate_block(block)) is not None
        ]
        if errors:
            findings[str(path)] = errors
    return findings


def default_doc_paths(repo_root: Path) -> list[Path]:
    """The Markdown surfaces CE029 scans: README plus every page under docs/."""
    paths = [repo_root / "README.md"]
    docs = repo_root / "docs"
    if docs.is_dir():
        paths.extend(sorted(docs.rglob("*.md")))
    return paths
