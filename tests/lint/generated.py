"""Shared write/diff engine for the repo's generated-surface checkers.

Every generated surface in this repo follows one shape: a module renders the
intended content of one or more files from an authoritative source, a ``make``
target writes it, and a CE rule re-renders and diffs it against disk. The render
is what differs per surface; the write and the diff are the same code, and were
literally the same code copied twice (``doc_indexes`` CE028, ``plugin_reference``
CE033) before this module existed.

Both helpers take the already-rendered ``{path: text}`` mapping, so a module
contributes only its render and stays free of file plumbing:

    def write(repo_root):  return write_all(_rendered_files(repo_root))
    def check(repo_root):  return diff_all(_rendered_files(repo_root))

A missing target file is treated as empty rather than an error — a generated file
that has never been written is exactly the drift the diff exists to report, and
the write path creates it (parents included).
"""

from __future__ import annotations

import difflib
from pathlib import Path


def write_all(rendered: dict[Path, str]) -> list[Path]:
    """Write each file whose content differs from what was rendered.

    Returns every target path, written or already-current, so a caller can report
    the full surface it owns. Unchanged files are not rewritten, which keeps mtimes
    (and therefore ``make`` and watch-mode tooling) stable on a no-op regeneration.
    """
    written: list[Path] = []
    for path, text in rendered.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def diff_all(rendered: dict[Path, str]) -> dict[str, str]:
    """Unified diff per file whose on-disk content differs. Empty dict = clean."""
    findings: dict[str, str] = {}
    for path, text in rendered.items():
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            diff = difflib.unified_diff(
                current.splitlines(),
                text.splitlines(),
                fromfile=f"{path} (on disk)",
                tofile=f"{path} (generated)",
                lineterm="",
            )
            findings[str(path)] = "\n".join(diff)
    return findings
