"""Symlinks in ``template_dir`` sources must survive the copy.

Regression test for the npm-workspace pattern used by the flow-v2
templates, where ``tools/node_modules/<pkg>`` is a relative symlink
to a sibling workspace dir (``../<pkg>``). If the copy replaces it
with an empty directory, every ``require('<pkg>/dist/...')`` in the
vendored Node tools fails with ``MODULE_NOT_FOUND``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coder_eval.models import SandboxConfig, TemplateDirSource
from coder_eval.sandbox import Sandbox


# Regression target is the npm-workspace pattern (`node_modules/<pkg>` is a
# relative symlink to `../<pkg>`). On Windows npm uses junctions, not
# symlinks, for workspaces — and Windows symlinks with relative paths hit
# edge cases that don't apply to the real-world scenario. POSIX-only.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="npm workspaces use junctions on Windows; this regression doesn't apply",
)


def test_symlink_in_template_is_preserved(tmp_path: Path) -> None:
    template = tmp_path / "template"
    (template / "tools" / "fil" / "dist").mkdir(parents=True)
    (template / "tools" / "fil" / "dist" / "parser.js").write_text("module.exports = {};\n")
    (template / "tools" / "node_modules").mkdir(parents=True)
    os.symlink("../fil", template / "tools" / "node_modules" / "fil-compiler")

    sandbox = Sandbox(
        SandboxConfig(
            driver="tempdir",
            template_sources=[TemplateDirSource(path=str(template))],
            ignore_patterns=["!dist", "!node_modules"],
        ),
        task_id="symlink-test",
    )
    try:
        sandbox.setup()
        copied_link = sandbox.sandbox_dir / "tools" / "node_modules" / "fil-compiler"

        assert copied_link.is_symlink(), (
            f"expected fil-compiler to remain a symlink, got "
            f"is_dir={copied_link.is_dir()} is_file={copied_link.is_file()}"
        )
        assert os.readlink(copied_link) == "../fil"
        assert (copied_link / "dist" / "parser.js").read_text() == "module.exports = {};\n"
    finally:
        sandbox.cleanup()


def test_negated_ignore_lets_node_modules_through(tmp_path: Path) -> None:
    template = tmp_path / "template"
    (template / "tools" / "node_modules" / "wabt").mkdir(parents=True)
    (template / "tools" / "node_modules" / "wabt" / "index.js").write_text("// wabt\n")

    sandbox = Sandbox(
        SandboxConfig(
            driver="tempdir",
            template_sources=[TemplateDirSource(path=str(template))],
            ignore_patterns=["!node_modules"],
        ),
        task_id="negated-ignore-test",
    )
    try:
        sandbox.setup()
        copied = sandbox.sandbox_dir / "tools" / "node_modules" / "wabt" / "index.js"
        assert copied.is_file(), "negated ignore should let node_modules through"
    finally:
        sandbox.cleanup()


def test_absolute_target_symlink_is_preserved(tmp_path: Path) -> None:
    """Absolute-target symlinks pass through verbatim — sandbox.py:300 docstring contract."""
    real_target = tmp_path / "real" / "data"
    real_target.mkdir(parents=True)
    (real_target / "marker.txt").write_text("absolute-target-ok\n")

    template = tmp_path / "template"
    template.mkdir()
    os.symlink(str(real_target), template / "data-link")

    sandbox = Sandbox(
        SandboxConfig(
            driver="tempdir",
            template_sources=[TemplateDirSource(path=str(template))],
        ),
        task_id="abs-symlink-test",
    )
    try:
        sandbox.setup()
        copied = sandbox.sandbox_dir / "data-link"

        assert copied.is_symlink(), "expected data-link to remain a symlink"
        assert os.readlink(copied) == str(real_target), (
            f"expected absolute target preserved verbatim; got {os.readlink(copied)!r}"
        )
        assert (copied / "marker.txt").read_text() == "absolute-target-ok\n"
    finally:
        sandbox.cleanup()


def _two_sources_sandbox(first: Path, second: Path, task_id: str) -> Sandbox:
    return Sandbox(
        SandboxConfig(
            driver="tempdir",
            template_sources=[TemplateDirSource(path=str(first)), TemplateDirSource(path=str(second))],
            ignore_patterns=["!node_modules"],
        ),
        task_id=task_id,
    )


def test_symlink_overwrites_existing_file(tmp_path: Path) -> None:
    """Second template's symlink correctly replaces a file the first template placed."""
    first = tmp_path / "first"
    first.mkdir()
    (first / "shared").write_text("plain file from first source\n")

    second = tmp_path / "second"
    second.mkdir()
    (second / "target").mkdir()
    (second / "target" / "marker.txt").write_text("via symlink\n")
    os.symlink("target", second / "shared")  # second's `shared` is a symlink

    sandbox = _two_sources_sandbox(first, second, "overwrite-file")
    try:
        sandbox.setup()
        shared = sandbox.sandbox_dir / "shared"
        assert shared.is_symlink(), "second source's symlink should have replaced first's file"
        assert (shared / "marker.txt").read_text() == "via symlink\n"
    finally:
        sandbox.cleanup()


def test_symlink_overwrites_existing_directory(tmp_path: Path) -> None:
    """Second template's symlink at the same path correctly rmtrees an existing directory."""
    first = tmp_path / "first"
    (first / "shared").mkdir(parents=True)
    (first / "shared" / "old.txt").write_text("from first\n")

    second = tmp_path / "second"
    second.mkdir()
    (second / "target").mkdir()
    (second / "target" / "new.txt").write_text("from second via link\n")
    os.symlink("target", second / "shared")

    sandbox = _two_sources_sandbox(first, second, "overwrite-dir")
    try:
        sandbox.setup()
        shared = sandbox.sandbox_dir / "shared"
        assert shared.is_symlink(), "second's symlink should have replaced first's directory"
        # The directory's contents (`old.txt`) should be gone — rmtree path
        assert not (shared / "old.txt").exists()
        assert (shared / "new.txt").read_text() == "from second via link\n"
    finally:
        sandbox.cleanup()


def test_symlink_overwrites_existing_symlink(tmp_path: Path) -> None:
    """Second template's symlink replaces the first template's symlink (unlink path)."""
    first_a = tmp_path / "first" / "a"
    first_a.mkdir(parents=True)
    (first_a / "from_a.txt").write_text("from a\n")
    os.symlink("a", first_a.parent / "link")

    second_b = tmp_path / "second" / "b"
    second_b.mkdir(parents=True)
    (second_b / "from_b.txt").write_text("from b\n")
    os.symlink("b", second_b.parent / "link")

    sandbox = _two_sources_sandbox(first_a.parent, second_b.parent, "overwrite-symlink")
    try:
        sandbox.setup()
        link = sandbox.sandbox_dir / "link"
        assert link.is_symlink()
        # Second source wins
        assert os.readlink(link) == "b"
        assert (link / "from_b.txt").read_text() == "from b\n"
    finally:
        sandbox.cleanup()
