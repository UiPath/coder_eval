"""Unit tests for ``.github/scripts/release_notes.py``.

The notes slicer runs exactly once per release, on ``main``, after the version
tag has already been pushed — and the prerelease dispatch skips the step, so
there is no rehearsal path. These tests are the only thing that exercises the
regex and its three failure modes before a real release depends on it.

The final test couples the regex to semantic-release's *actual* rendering of the
real ``CHANGELOG.md``: a changelog-template or heading-format change upstream
would otherwise degrade every Release body to ``--generate-notes`` with only a
``::warning::`` inside a ``continue-on-error: true`` step as the signal.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "release_notes.py"


def _load_script() -> ModuleType:
    """Import the script by path — ``.github/scripts`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = _load_script()


CHANGELOG = """# CHANGELOG

<!-- version list -->

## v0.8.10 (2026-07-24)

### Bug Fixes

- Newest entry

## v0.8.9 (2026-07-23)

### Features

- Middle entry with a link ([#38](https://example.invalid/pull/38))

## v0.8.1 (2026-07-01)

### Bug Fixes

- Oldest entry
"""


class TestExtractSection:
    def test_newest_section_stops_at_next_heading(self):
        """The \\Z branch is not taken when a later section exists."""
        body = release_notes.extract_section(CHANGELOG, "0.8.10")
        assert body == "### Bug Fixes\n\n- Newest entry"
        assert "v0.8.9" not in body

    def test_middle_section_is_bounded_on_both_sides(self):
        body = release_notes.extract_section(CHANGELOG, "0.8.9")
        assert body.startswith("### Features")
        assert body.endswith("([#38](https://example.invalid/pull/38))")
        assert "Newest entry" not in body
        assert "Oldest entry" not in body

    def test_last_section_in_file_uses_eof_branch(self):
        """The (?=^## v|\\Z) alternation's \\Z arm — no trailing heading to stop at."""
        assert release_notes.extract_section(CHANGELOG, "0.8.1") == "### Bug Fixes\n\n- Oldest entry"

    def test_absent_version_returns_empty_string(self):
        """Empty string is the contract that makes release.yml fall back to --generate-notes."""
        assert release_notes.extract_section(CHANGELOG, "9.9.9") == ""

    def test_version_prefix_does_not_match_longer_version(self):
        """Guards the trailing \\b: "0.8.1" must not slice the "v0.8.10" section.

        Without the word boundary this returns the 0.8.10 notes, i.e. a release
        published with a *different version's* changelog as its body.
        """
        body = release_notes.extract_section(CHANGELOG, "0.8.1")
        assert "Newest entry" not in body
        assert "Oldest entry" in body

    def test_regex_metacharacters_in_version_are_literal(self):
        """re.escape() — a PEP 440 local version must not be read as a pattern."""
        text = "## v1.0.0+local.1 (2026-01-01)\n\n- Local build\n"
        assert release_notes.extract_section(text, "1.0.0+local.1") == "- Local build"
        # The unescaped form would let "." match any character.
        assert release_notes.extract_section(text, "1.0.0+localX1") == ""

    def test_heading_only_section_yields_empty_body(self):
        """A section with no content is indistinguishable from absent — both fall back."""
        assert release_notes.extract_section("## v1.2.3 (2026-01-01)\n", "1.2.3") == ""


class TestMain:
    def test_writes_utf8_body_to_output_path(self, tmp_path: Path):
        """Round-trip the non-ASCII bytes the real CHANGELOG.md contains."""
        out = tmp_path / "release-notes.md"
        # Uses the repo's own CHANGELOG.md, which carries → — ✓.
        version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        assert release_notes.main(["release_notes.py", version, str(out)]) == 0
        assert out.read_text(encoding="utf-8").strip() != ""

    def test_absent_version_writes_empty_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        out = tmp_path / "release-notes.md"
        assert release_notes.main(["release_notes.py", "0.0.0", str(out)]) == 0
        assert out.exists(), "an empty file must still be created; release.yml tests it with [ -s ]"
        assert out.read_text(encoding="utf-8") == ""
        assert "::warning::" in capsys.readouterr().out

    @pytest.mark.parametrize("argv", [["release_notes.py"], ["release_notes.py", "1.2.3"]])
    def test_bad_argv_returns_usage_error(self, argv: list[str], capsys: pytest.CaptureFixture[str]):
        assert release_notes.main(argv) == 2
        assert "usage:" in capsys.readouterr().err


def test_current_version_has_a_slicable_changelog_section():
    """Couples the regex to semantic-release's real rendering, not a fixture of it.

    ``pyproject.toml``'s version is always the most recently released one at rest,
    so its section must exist in ``CHANGELOG.md`` and be non-empty. If a
    ``[tool.semantic_release.changelog]`` template change drops the ``v`` prefix or
    reshapes the heading, this fails here instead of silently degrading the next
    release's notes.
    """
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    body = release_notes.extract_section(changelog, version)
    assert body, f"no CHANGELOG.md section for the current version v{version}"
    # The slice must not bleed into the previous release's section.
    assert not body.lstrip().startswith("## v")
    assert "\n## v" not in body
