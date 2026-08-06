"""CE035 — every ``steps.<id>.outputs.<key>`` / ``needs.<job>.outputs.<key>`` reference
in a workflow must resolve to a key its writer actually produces.

The motivating bug shipped in ``verify-published-action.yml``: two steps read
``steps.parity.outputs.version``, but the ``parity`` step writes only ``pin`` /
``newest`` / ``lagging`` (the *shell variable* was ``VERSION``, the *output key* was
``newest``). GitHub expands an unwritten output to the empty string, so
``TAG_REF: v${{ steps.parity.outputs.version }}`` became the bare string ``v``,
``git show "v:action.yml"`` exited 128 under ``set -euo pipefail``, and the preflight
job was red on 100% of triggers — which, via ``needs: preflight``, meant the paid
end-to-end tier could never run at all.

Nothing caught it: the workflow is invisible to ruff, pyright, pytest and the AST lint
runner, and ``actionlint`` models ``steps.*.outputs`` as an open string map, so an
unwritten shell key is untyped and unflagged there too.

**Writers are mechanically enumerable, and this rule only reasons about the ones that
are.** For a referenced step id:

* ``run:`` step → the keys it echoes/prints into ``$GITHUB_OUTPUT``. Writers are
  collected by an over-approximating scan (any ``key=`` / ``key<<`` in an ``echo`` or
  ``printf`` in the body), because over-approximating *writers* can only make the rule
  quieter, never produce a false failure. If a body touches ``$GITHUB_OUTPUT`` in a way
  the scan cannot read (no key found at all), the step is skipped rather than guessed at.
* local composite (``uses: ./``) → the ``outputs:`` block of the repo's ``action.yml``.
* third-party ``uses:`` → **skipped**. Resolving those needs the action's own metadata,
  which is not on disk; pretending otherwise would fail on every pinned action.
* a missing step id, or a ``needs`` output absent from that job's ``outputs:`` map, is
  always a finding — those are fully enumerable from the file.

Like CE026-CE031 this is deliberately NOT a ``BaseRule`` in ``tests/lint/runner.py``:
that runner is AST-only over ``.py`` files, whereas this rule reasons over workflow YAML
plus embedded shell. It is wired as ``tests/test_custom_lint.py::TestCE035WorkflowOutputParity``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# `${{ steps.<id>.outputs.<key> }}` — the id and key charsets GitHub accepts.
STEP_OUTPUT_REF = re.compile(r"steps\.(?P<id>[A-Za-z_][A-Za-z0-9_-]*)\.outputs\.(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)")
NEEDS_OUTPUT_REF = re.compile(r"needs\.(?P<job>[A-Za-z_][A-Za-z0-9_-]*)\.outputs\.(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)")

# `echo "key=value"` / `printf 'key=%s' …` / `echo "key<<EOF"` (multiline form).
# Deliberately loose: see the module docstring on over-approximating writers.
OUTPUT_WRITE = re.compile(r"""(?:echo|printf)\s+[^\n]*?["']?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?:=|<<)""")


@dataclass(frozen=True)
class Finding:
    """One unresolvable output reference."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} — {self.message}"


def workflow_paths(repo_root: Path) -> list[Path]:
    """Every workflow file, plus the composite action definition."""
    paths = sorted(p for p in (repo_root / ".github" / "workflows").glob("*.yml") if p.is_file())
    action = repo_root / "action.yml"
    if action.is_file():
        paths.append(action)
    return paths


def _iter_strings(node: Any) -> list[str]:
    """Every string anywhere in a parsed YAML subtree."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _iter_strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _iter_strings(v)]
    return []


def _first_line_containing(lines: list[str], needle: str) -> int:
    for i, line in enumerate(lines, start=1):
        if needle in line:
            return i
    return 1


def _local_composite_outputs(repo_root: Path) -> set[str]:
    action = repo_root / "action.yml"
    if not action.is_file():
        return set()
    data = yaml.safe_load(action.read_text(encoding="utf-8")) or {}
    return set((data.get("outputs") or {}).keys())


def _written_keys(step: dict[str, Any]) -> set[str] | None:
    """Output keys a ``run:`` step writes, or ``None`` when they are not determinable."""
    body = step.get("run")
    if not isinstance(body, str):
        return None
    if "GITHUB_OUTPUT" not in body:
        return set()
    keys = {m.group("key") for m in OUTPUT_WRITE.finditer(body)}
    # A body that clearly writes outputs but yields no readable key (e.g. built by an
    # embedded interpreter) is unparseable, not empty — skip rather than guess.
    return keys or None


def _steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _check_step_refs(
    path: Path,
    lines: list[str],
    scope_name: str,
    steps: list[dict[str, Any]],
    scope_strings: list[str],
    composite_outputs: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    by_id = {s["id"]: s for s in steps if isinstance(s.get("id"), str)}
    seen: set[tuple[str, str]] = set()

    for text in scope_strings:
        for match in STEP_OUTPUT_REF.finditer(text):
            step_id, key = match.group("id"), match.group("key")
            if (step_id, key) in seen:
                continue
            seen.add((step_id, key))
            line = _first_line_containing(lines, match.group(0))

            step = by_id.get(step_id)
            if step is None:
                findings.append(
                    Finding(
                        path,
                        line,
                        f"{scope_name}: `steps.{step_id}.outputs.{key}` refers to step id "
                        f"'{step_id}', which does not exist in this job "
                        f"(ids present: {sorted(by_id) or 'none'})",
                    )
                )
                continue

            uses = step.get("uses")
            if isinstance(uses, str):
                if not uses.startswith("./"):
                    continue  # third-party action: outputs are not on disk — see docstring
                if key not in composite_outputs:
                    findings.append(
                        Finding(
                            path,
                            line,
                            f"{scope_name}: `steps.{step_id}.outputs.{key}` — the local composite "
                            f"action declares outputs {sorted(composite_outputs)}",
                        )
                    )
                continue

            written = _written_keys(step)
            if written is None:
                continue  # not a shell step, or writers not statically readable
            if key not in written:
                findings.append(
                    Finding(
                        path,
                        line,
                        f"{scope_name}: `steps.{step_id}.outputs.{key}` is never written — step "
                        f"'{step_id}' writes {sorted(written) or 'no outputs'} to $GITHUB_OUTPUT. "
                        "GitHub expands an unwritten output to the empty string, so this silently "
                        "becomes ''",
                    )
                )
    return findings


def _check_needs_refs(
    path: Path,
    lines: list[str],
    jobs: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    declared = {name: set((job.get("outputs") or {}).keys()) for name, job in jobs.items() if isinstance(job, dict)}
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for text in _iter_strings(job):
            for match in NEEDS_OUTPUT_REF.finditer(text):
                producer, key = match.group("job"), match.group("key")
                if (producer, key) in seen:
                    continue
                seen.add((producer, key))
                if producer not in declared:
                    continue  # unknown job name — actionlint's territory, not this rule's
                if key not in declared[producer]:
                    findings.append(
                        Finding(
                            path,
                            _first_line_containing(lines, match.group(0)),
                            f"job '{job_name}': `needs.{producer}.outputs.{key}` is not declared — job "
                            f"'{producer}' exposes {sorted(declared[producer]) or 'no outputs'}. It "
                            "expands to the empty string, so an emptiness gate on it silently skips",
                        )
                    )
    return findings


def find_unresolved_output_refs(paths: list[Path], repo_root: Path) -> list[Finding]:
    """Every output reference in ``paths`` that cannot resolve to a real writer."""
    composite_outputs = _local_composite_outputs(repo_root)
    findings: list[Finding] = []

    for path in paths:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            continue

        jobs = data.get("jobs")
        if isinstance(jobs, dict):
            for name, job in jobs.items():
                if not isinstance(job, dict):
                    continue
                findings.extend(
                    _check_step_refs(
                        path, lines, f"job '{name}'", _steps_of(job), _iter_strings(job), composite_outputs
                    )
                )
            findings.extend(_check_needs_refs(path, lines, jobs))

        # A composite action definition (`action.yml`) has one flat step list, and its
        # own `outputs:` block reads from those steps.
        runs = data.get("runs")
        if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
            scope_strings = _iter_strings(runs) + _iter_strings(data.get("outputs") or {})
            findings.extend(
                _check_step_refs(path, lines, "composite", _steps_of(runs), scope_strings, composite_outputs)
            )

    return findings
