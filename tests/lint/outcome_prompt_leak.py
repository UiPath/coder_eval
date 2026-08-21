"""CE057's reader — does an outcome row's prompt contain a value its EXPECTATIONS grade it on?

CE061 asks this of a criterion's own fields. It cannot see an outcome suite's real marking scheme,
because that lives in a directory of JSON beside the suite (`outcome-grader/expectations/<row>.json`)
which no criterion carries: the criterion is a `run_command` naming a script, and every string it
grades on is one indirection away. So the suite that most needs the check is precisely the one
CE061 is blind to.

A shared reader under ``tests/lint/`` rather than a numbered rule module, on the
``leak_detection.py`` / ``markdown_tables.py`` / ``task_yaml_discovery.py`` precedent — the rule
itself is a ``@pytest.mark.lint`` class in ``tests/test_custom_lint.py`` (CE065 / CE045 / CE052's
shape), because its subject is a JSONL plus a directory of JSON rather than one ``.py`` AST, and
``tests/lint/rules/`` holds ``BaseRule`` modules only.

**The primitive is imported, never re-implemented.** :mod:`coder_eval.leak_detection` already owns
``LEAK_LOCATOR_FIELDS``, ``LEAK_MIN_CHARS`` and ``string_leaves``, with two consumers pointing in
opposite directions; this is the third. A second copy would agree on ordinary input and diverge
exactly where one of them was written for.

**The boundary, so a green run is not mistaken for a proof.** Like CE061 this catches the
**verbatim** form only: the prompt literally contains a string the expectations assert. A prompt
that describes the graded behaviour in other words still needs a reviewer. And the spec carve-out
is the whole difficulty here — an outcome prompt legitimately states output paths, sheet names and
column names, because "follow the user's spec literally" is itself a graded behaviour. Those are
exactly what ``LEAK_LOCATOR_FIELDS`` is for; anything outside it is a leak.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

import yaml

from coder_eval.leak_detection import LEAK_LOCATOR_FIELDS, LEAK_MIN_CHARS, string_leaves


# The directory an outcome suite keeps its per-row marking scheme in, beside the suite YAML and
# deliberately OUTSIDE the mounted fixture — `outcome.yaml`'s `sandbox:` comment carries why.
EXPECTATIONS_DIRNAME = "expectations"


class LeakPair(NamedTuple):
    """One (row prompt, asserted value) comparison — the unit the non-vacuity assert counts."""

    suite: Path
    row_id: str
    value: str
    check: str


def graded_values(spec: object) -> list[tuple[str, str]]:
    """``(check key, asserted value)`` for every substantive string an expectations file grades on.

    Reads under ``checks`` ONLY. An expectations file's other top-level keys assert nothing about
    content and must not be able to fire: ``path`` is a locator by the same argument
    ``LEAK_LOCATOR_FIELDS`` makes for a criterion's, ``rules`` maps checks to rule ids, and
    ``_comment`` is instructions to the author. Within a check, :data:`LEAK_LOCATOR_FIELDS` are
    dropped wherever they appear in its params — a check keyed on a `path` names WHERE to look,
    which removes nondeterminism from the measurement without revealing WHAT is graded, exactly as
    it does for a criterion.

    Values shorter than ``LEAK_MIN_CHARS`` are dropped: ``"1"`` and ``"ok"`` collide by chance, and
    a leak worth flagging is a substantive string the author put in both places.
    """
    if not isinstance(spec, dict):
        return []
    checks = spec.get("checks")
    if not isinstance(checks, dict):
        return []
    found: list[tuple[str, str]] = []
    for key, params in checks.items():
        if not isinstance(params, dict):
            continue
        graded = {k: v for k, v in params.items() if k not in LEAK_LOCATOR_FIELDS}
        found += [(str(key), value) for value in string_leaves(graded) if len(value) >= LEAK_MIN_CHARS]
    return found


def _rows(rows_file: Path) -> list[dict]:
    """The JSONL's rows, skipping blank lines. A malformed line RAISES rather than vanishing."""
    return [json.loads(line) for line in rows_file.read_text(encoding="utf-8").splitlines() if line.strip()]


# `${row.<field>}` — the substitution `task_loader.expand_dataset` performs into `initial_prompt`.
_ROW_REF = re.compile(r"\$\{row\.([A-Za-z0-9_]+)\}")


def rendered_prompts(suite_yaml: Path, rows: list[dict]) -> dict[str, str]:
    """Row id -> the prompt that row's agent actually receives.

    **This is the whole correctness of the rule**, and getting it wrong makes CE057 a build-breaker
    that fires on the shape the shipped template teaches. Only `initial_prompt` reaches the agent,
    and only the row fields it interpolates reach `initial_prompt`. A row's other fields are
    CRITERION parameters: `expected_snippet` is fed to `file_check.includes`, so a suite whose
    expectations assert the same string is doing exactly what the template tells it to — the string
    is in the marking scheme twice and in the prompt zero times, which is not a leak. Comparing
    against every row field reported it as one.

    Rendering the real template also closes the converse gap: a graded value hard-coded into the
    SHARED prompt, where no row field is involved at all, is now visible.
    """
    document = yaml.safe_load(suite_yaml.read_text(encoding="utf-8"))
    template = document.get("initial_prompt") if isinstance(document, dict) else None
    if not isinstance(template, str):
        return {}

    def render(row: dict) -> str:
        return _ROW_REF.sub(lambda m: str(row.get(m.group(1), m.group(0))), template)

    return {str(row.get("id", "")): render(row) for row in rows}


def discover_outcome_suites(root: Path) -> list[tuple[Path, Path, Path]]:
    """``(suite YAML, rows JSONL, expectations dir)`` for every outcome suite under ``root``.

    Discovery is by CONTENT — a `task_id:`-bearing YAML beside a rows file and a directory named
    :data:`EXPECTATIONS_DIRNAME` — never by filename, so a suite an author called something else is
    still checked. A rows file with no suite YAML beside it is skipped: without the prompt template
    there is nothing to compare against, and guessing one is how the false positive above happened.
    """
    found: list[tuple[Path, Path, Path]] = []
    for expectations in sorted(root.rglob(EXPECTATIONS_DIRNAME)):
        if not expectations.is_dir() or not any(expectations.glob("*.json")):
            continue
        # The suite YAML and its rows file sit one level above the grader directory.
        suite_dir = expectations.parent.parent
        suites = [
            path
            for path in sorted([*suite_dir.glob("*.yaml"), *suite_dir.glob("*.yml")])
            if isinstance(document := yaml.safe_load(path.read_text(encoding="utf-8")), dict) and "task_id" in document
        ]
        for suite in suites:
            found += [(suite, rows, expectations) for rows in sorted(suite_dir.glob("*.jsonl"))]
    return found


def leaks(root: Path) -> tuple[list[LeakPair], int]:
    """Every verbatim leak under ``root``, and the number of prompt/value PAIRS compared.

    The pair count is what the rule asserts non-empty. Counting discovered *suites* instead would
    pass green over a suite whose expectations match no row at all — which is the shipped state
    this plan had to fix, and precisely the CE044/CE045 vacuous pass this rule cites.

    The compared prompt is the RENDERED ``initial_prompt`` (see :func:`rendered_prompts`), never
    the row's other fields: those are criterion parameters, and treating them as the prompt makes
    the rule fire on the shape the shipped template teaches.
    """
    found: list[LeakPair] = []
    pairs = 0
    for suite_yaml, rows_file, expectations in discover_outcome_suites(root):
        specs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(expectations.glob("*.json"))}
        prompts = rendered_prompts(suite_yaml, _rows(rows_file))
        for row_id, prompt in prompts.items():
            spec = specs.get(row_id)
            if spec is None:
                continue
            haystack = prompt.casefold()
            for check, value in graded_values(spec):
                pairs += 1
                if value.casefold() in haystack:
                    found.append(LeakPair(suite=rows_file, row_id=row_id, value=value, check=check))
    return found, pairs
