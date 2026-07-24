#!/usr/bin/env python3
"""Slice one version's section out of ``CHANGELOG.md`` for a GitHub Release body.

Used by ``.github/workflows/release.yml``'s "Publish GitHub Release" step.

This lives in a real module rather than inline in a ``run:`` heredoc because
code inside a heredoc is structurally invisible to ruff, pyright, pytest and
coverage — and this particular code has three distinct failure modes (version
absent, section is the file's last, version string carrying regex
metacharacters) on a path that executes exactly once per release, against
production ``main``, after the tag is already pushed. The prerelease dispatch
skips the step, so there is no rehearsal; ``tests/test_release_notes.py`` is
what exercises it before it matters.

Usage::

    release_notes.py <version> <output-path>

Writes the section body (heading line excluded, stripped) to ``<output-path>``,
or an empty file when no matching section exists — the caller treats an empty
file as "fall back to GitHub's generated notes".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]

# semantic-release's default changelog template renders headings as
# "## v0.8.9 (2026-07-23)". Capture everything after that heading line up to the
# next "## v" heading, or EOF for the newest entry (the common case on a release
# run, since the just-generated section is at the top of the file).
#
# The trailing \b on the version is load-bearing: without it "0.8.1" would match
# the "## v0.8.10" heading and publish the wrong section.
_SECTION_TEMPLATE = r"(?m)^## v{version}\b.*?$(.*?)(?=^## v|\Z)"


def extract_section(changelog_text: str, version: str) -> str:
    """Return the changelog body for ``version``, or ``""`` when absent.

    ``version`` is regex-escaped, so a PEP 440 local/prerelease version
    containing metacharacters (``+``, ``!``) is matched literally.
    """
    pattern = _SECTION_TEMPLATE.format(version=re.escape(version))
    match = re.search(pattern, changelog_text, re.S)
    return match.group(1).strip() if match else ""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: release_notes.py <version> <output-path>", file=sys.stderr)
        return 2
    version, out_path = argv[1], argv[2]
    # Encoding is pinned rather than left to the ambient locale: CHANGELOG.md
    # verifiably carries non-ASCII (arrows, em dashes, check marks), so a
    # non-UTF-8 locale would raise UnicodeDecodeError here and — under the
    # caller's `continue-on-error: true` — silently publish no Release at all.
    text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    body = extract_section(text, version)
    Path(out_path).write_text(body, encoding="utf-8")
    if not body:
        # Surfaced as a workflow annotation; the caller falls back to
        # --generate-notes when the file is empty.
        print(f"::warning::no CHANGELOG section found for v{version}; using GitHub's generated notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
