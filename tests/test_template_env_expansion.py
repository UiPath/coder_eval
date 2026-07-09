"""Tests for env-var expansion in TemplateDirSource paths.

Covers ``coder_eval.orchestration.task_loader.resolve_template_source_paths``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_eval.models import RepoSource, TemplateDirSource
from coder_eval.orchestration.task_loader import resolve_template_source_paths


def test_defined_env_var_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "skill"
    target.mkdir()
    monkeypatch.setenv("MY_SKILL_PATH", str(target))

    source = TemplateDirSource(path="$MY_SKILL_PATH")
    resolve_template_source_paths([source], base_dir=tmp_path)

    assert source.path == str(target)


def test_defined_env_var_curly_brace_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "skill"
    target.mkdir()
    monkeypatch.setenv("MY_SKILL_PATH", str(target))

    source = TemplateDirSource(path="${MY_SKILL_PATH}")
    resolve_template_source_paths([source], base_dir=tmp_path)

    assert source.path == str(target)


def test_undefined_env_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NONEXISTENT_SKILL_VAR", raising=False)
    source = TemplateDirSource(path="$NONEXISTENT_SKILL_VAR")

    with pytest.raises(ValueError, match=r"\$NONEXISTENT_SKILL_VAR"):
        resolve_template_source_paths([source], base_dir=tmp_path)


def test_undefined_env_var_error_names_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The error should be actionable — name the unresolved var, not just fail."""
    monkeypatch.delenv("MISSING_ONE", raising=False)
    monkeypatch.delenv("MISSING_TWO", raising=False)
    source = TemplateDirSource(path="$MISSING_ONE/sub/${MISSING_TWO}")

    with pytest.raises(ValueError) as exc_info:
        resolve_template_source_paths([source], base_dir=tmp_path)

    msg = str(exc_info.value)
    assert "$MISSING_ONE" in msg
    assert "$MISSING_TWO" in msg


def test_absolute_path_without_env_vars_unchanged(tmp_path: Path) -> None:
    abs_path = str(tmp_path / "existing")
    source = TemplateDirSource(path=abs_path)
    resolve_template_source_paths([source], base_dir=tmp_path)
    assert source.path == abs_path


def test_relative_path_resolved_against_base_dir(tmp_path: Path) -> None:
    source = TemplateDirSource(path="templates/my-skill")
    resolve_template_source_paths([source], base_dir=tmp_path)
    assert source.path == str((tmp_path / "templates" / "my-skill").resolve())


def test_env_var_pattern_does_not_match_dataset_row_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`${row.field}` (dataset substitution) must not trigger env-var handling.
    The env regex requires `[A-Za-z_][A-Za-z0-9_]*` which excludes the dot."""
    # Control: `${row.field}` in a path should not be mistaken for an undefined
    # env var. We do not expect dataset vars in template paths in practice, but
    # the regexes must remain mutually disjoint so the two layers don't collide.
    monkeypatch.delenv("row", raising=False)
    # Using a placeholder that looks like a dataset var — the env-var regex
    # must not flag "row" here because the full match requires the closing `}`
    # after `[A-Za-z_][A-Za-z0-9_]*` (no dot). The string below therefore must
    # NOT raise ValueError for an undefined env var named "row".
    source = TemplateDirSource(path="${row.field}")
    # Neither var matches our env pattern, so no error. expandvars leaves it
    # untouched; `Path(...)` treats it as a literal relative segment.
    resolve_template_source_paths([source], base_dir=tmp_path)
    assert "row.field" in source.path


def test_non_template_dir_sources_are_skipped(tmp_path: Path) -> None:
    """Only TemplateDirSource entries are touched; RepoSource is untouched."""
    repo = RepoSource(url="https://example.invalid/repo.git")
    resolve_template_source_paths([repo], base_dir=tmp_path)
    # No error, no mutation.
    assert repo.url == "https://example.invalid/repo.git"


def test_env_var_with_path_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """$VAR/subdir should expand and keep the suffix."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("SKILLS_ROOT", str(skills))

    source = TemplateDirSource(path="$SKILLS_ROOT/composer")
    resolve_template_source_paths([source], base_dir=tmp_path)

    assert source.path == str(skills / "composer")
