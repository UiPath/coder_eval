"""Golden-master test for the C2 packager's emitted directory.

The regression this test is FOR: a transpiler's characteristic failure mode
is plausible-looking drift — a field silently starts mapping to the wrong
TOML key, a template loses a line — that no unit assertion happens to probe.
Diffing the whole emitted tree against a committed snapshot catches that
class of bug directly, cheaper than enumerating every field in prose.

The source fixture (``tests/_fixtures/harbor_export_golden/source_task/``)
deliberately exercises every C2 mapping row at once: ``dockerfile_path`` with
no ``WORKDIR`` (exercises the append-a-WORKDIR path), ``reference:``,
``run_limits.task_timeout``, resource limits, ``network: none``, and both a
filesystem criterion and a ``reference_comparison`` criterion (proving the
placeholder-agent design survives end to end, not just in an isolated unit test).

Regenerate after an INTENTIONAL mapping change with::

    GOLDEN_REGEN=1 uv run pytest tests/test_harbor_export_golden.py

and review the resulting diff before committing.

Rationale: .claude/notes/reporting.md § The non-obvious constraint in the emitted task.yaml
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coder_eval.harbor.packager import export_task


_FIXTURE_ROOT = Path(__file__).parent / "_fixtures" / "harbor_export_golden"
_SOURCE_TASK = _FIXTURE_ROOT / "source_task" / "task.yaml"
_EXPECTED_DIR = _FIXTURE_ROOT / "expected"
_REGEN = os.environ.get("GOLDEN_REGEN", "").strip().lower() in {"1", "true", "yes", "on"}


_OUT_DIR_PLACEHOLDER = "<OUT_DIR>"


def _relative_files(root: Path, *, out_dir: Path) -> dict[str, str]:
    """Every file under root, as {posix-relative-path: text-content}.

    ``environment/docker-compose.yaml`` embeds an absolute bind-mount source path
    for ``environment/task.yaml`` (``_write_docker_compose_mounts`` always mounts
    it read-only rather than ``COPY``ing it in) -- that path is ``out_dir``, which
    is a fresh ``tmp_path`` on every test run and would never match a committed
    golden file byte-for-byte. Normalize it to a stable placeholder before
    comparing (and before writing the golden fixture itself under
    ``GOLDEN_REGEN=1``), same as any other run-specific value this test would
    otherwise have to special-case.

    Compose volume specs are always POSIX-style (``_write_docker_compose_mounts``
    emits ``as_posix()``, since docker-compose volume specs are POSIX regardless
    of the exporter's host OS) -- so the placeholder substitution must match on
    the POSIX form of ``out_dir`` too, not the OS-native (backslash, on Windows)
    form ``str()``/``resolve()`` would give.
    """
    out_dir_str = out_dir.resolve().as_posix()
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8").replace(out_dir_str, _OUT_DIR_PLACEHOLDER)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_export_matches_the_committed_golden_tree(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    export_task(_SOURCE_TASK, out_dir)
    actual = _relative_files(out_dir, out_dir=out_dir)

    if _REGEN:
        for rel_path, content in actual.items():
            dest = _EXPECTED_DIR / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        # test.sh's executable bit is part of the contract (C1.1) but git/the
        # filesystem, not file content, carries it -- regenerate it too so a
        # fresh checkout of the golden dir stays runnable if ever copied out.
        (_EXPECTED_DIR / "tests" / "test.sh").chmod(0o755)
        return

    assert _EXPECTED_DIR.is_dir(), "no committed golden tree yet -- run with GOLDEN_REGEN=1 first"
    # out_dir=_EXPECTED_DIR here is a no-op substitution -- the committed golden content
    # already holds the literal _OUT_DIR_PLACEHOLDER, never a real absolute path.
    expected = _relative_files(_EXPECTED_DIR, out_dir=_EXPECTED_DIR)

    assert set(actual) == set(expected), (
        f"emitted file set drifted from the golden tree.\n"
        f"  only in export: {sorted(set(actual) - set(expected))}\n"
        f"  only in golden: {sorted(set(expected) - set(actual))}"
    )
    for rel_path in expected:
        assert actual[rel_path] == expected[rel_path], f"content drifted for {rel_path}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: NTFS has no chmod executable bit")
def test_test_sh_is_executable_in_the_golden_tree() -> None:
    """The executable bit is part of C1.1's contract and isn't captured by file content."""
    test_sh = _EXPECTED_DIR / "tests" / "test.sh"
    assert test_sh.is_file()
    assert test_sh.stat().st_mode & 0o111
