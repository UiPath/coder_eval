"""Baked-image guard: the agent image must not carry task-specific answers.

Under COPY/PRUNE + GRADE-OUTSIDE the runtime leak is closed by absence (criteria
stripped, graders/reference not mounted). But image CONTENT (baked mocks /
tooling) is a separate surface: an answer-bearing file baked into
``docker/Dockerfile`` would ship to every agent regardless of the mount policy.
This is a deterministic, daemon-free scan of ``docker/Dockerfile`` + the sources
it ``COPY``s: it asserts no fixture answer sentinels and no ``check_*.py`` /
``RESOLUTION.md`` / ``tests/tasks`` grader material is baked.

This is the authoring-invariant sensor documented in docs/DOCKER_ISOLATION.md
(residual leak #5): mocks/tooling baked into the image must not encode
task-specific expected values.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile"

# The fixtures' real hidden expected VALUES (must never be baked into the image).
_ANSWER_SENTINELS = ("UiPath.Template.REFramework", "LEAKED-ANSWER-SENTINEL-9f3a2b")


def _copied_sources() -> list[Path]:
    """Host paths referenced by ``COPY``/``ADD`` in the Dockerfile that exist in the checkout.

    A missing referenced path is treated as "nothing baked" (skip) — a source checkout
    may not carry every build-context artifact. Wildcards / whole-dir copies are walked.
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")
    sources: list[Path] = []
    for m in re.finditer(r"(?m)^\s*(?:COPY|ADD)\s+(.+)$", text):
        parts = m.group(1).split()
        # Drop --flag args and the final destination; the rest are sources.
        srcs = [p for p in parts[:-1] if not p.startswith("--")]
        for s in srcs:
            p = (_REPO_ROOT / s).resolve()
            if p.exists():
                sources.append(p)
    return sources


def _iter_files(paths: list[Path]):
    for p in paths:
        if p.is_dir():
            yield from (f for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            yield p


def test_dockerfile_present():
    assert _DOCKERFILE.is_file(), "docker/Dockerfile must exist"


def test_no_answer_sentinels_baked():
    hits: list[str] = []
    for f in _iter_files(_copied_sources()):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for sentinel in _ANSWER_SENTINELS:
            if sentinel in text:
                hits.append(f"{sentinel} in {f}")
    assert not hits, f"answer sentinels baked into the image build context: {hits}"


def test_no_grader_material_baked():
    offenders: list[str] = []
    for f in _iter_files(_copied_sources()):
        name = f.name
        rel_parts = f.parts
        is_grader = (name.startswith("check_") and name.endswith(".py")) or name == "RESOLUTION.md"
        is_suite_tree = "tests" in rel_parts and "tasks" in rel_parts
        if is_grader or is_suite_tree:
            offenders.append(str(f))
    assert not offenders, f"grader material baked into the image: {offenders}"


def test_no_uid_drop_machinery_in_dockerfile():
    """The COPY/PRUNE design has NO uid-drop barrier: the Dockerfile must not bake
    an `agent` user / setpriv shim (the superseded approach's host-mutating
    machinery). Guards against an accidental re-introduction."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert "AGENT_UID" not in text
    assert "setpriv" not in text
    assert "coder_eval_drop_privilege" not in text
    assert not re.search(r"useradd\s+-u", text)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
