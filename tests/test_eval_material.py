"""Unit tests for the allowlist (default-deny) mask over auto-mounted plugin trees.

`eval_material.mask_dirs(root)` returns the child dirs to tmpfs-mask so that only
`.claude-plugin` + the manifest-declared skill dirs stay readable under a mounted
Claude-plugin root. Everything else (sibling task YAMLs, reference solutions,
`tests/`, `node_modules/`) is masked by default so it can never leak.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coder_eval.isolation.eval_material import mask_dirs


def _plugin_root(root: Path, *, skills: str | list[str] | None = None) -> Path:
    """Create a `.claude-plugin/plugin.json`, optionally declaring a skills path."""
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    payload: dict = {"name": "demo"}
    if skills is not None:
        payload["skills"] = skills
    (manifest_dir / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


class TestMaskDirs:
    def test_non_plugin_root_returns_empty(self, tmp_path: Path):
        # No .claude-plugin/plugin.json -> not a plugin root -> nothing masked.
        (tmp_path / "some_dir").mkdir()
        assert mask_dirs(tmp_path) == []

    def test_default_skills_layout_masks_non_skill_children(self, tmp_path: Path):
        root = _plugin_root(tmp_path)
        (root / "skills" / "demo").mkdir(parents=True)
        (root / "skills" / "demo" / "SKILL.md").write_text("x", encoding="utf-8")
        (root / "skills" / "demo" / "helper.py").write_text("x", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "reference").mkdir()
        (root / "node_modules").mkdir()
        (root / "fixtures").mkdir()

        masked = set(mask_dirs(root))

        assert masked == {
            (root / "tests").resolve(),
            (root / "reference").resolve(),
            (root / "node_modules").resolve(),
            (root / "fixtures").resolve(),
        }
        # Skill surface and manifest stay readable.
        assert (root / "skills").resolve() not in masked
        assert (root / ".claude-plugin").resolve() not in masked

    def test_skill_internal_assets_stay_readable(self, tmp_path: Path):
        root = _plugin_root(tmp_path)
        (root / "skills" / "demo" / "assets").mkdir(parents=True)
        (root / "tests").mkdir()

        masked = set(mask_dirs(root))

        # A skill's own subdirs are inside a kept dir -> never masked.
        assert (root / "skills" / "demo" / "assets").resolve() not in masked
        assert (root / "tests").resolve() in masked

    def test_nested_skills_path_does_not_over_expose_src(self, tmp_path: Path):
        root = _plugin_root(tmp_path, skills="src/skills")
        (root / "src" / "skills" / "demo").mkdir(parents=True)
        (root / "src" / "secret_lib").mkdir(parents=True)
        (root / "tests").mkdir()

        masked = set(mask_dirs(root))

        # src/skills is kept; src/secret_lib (a sibling under src) is masked; src
        # itself is NOT masked (it is an ancestor of the kept skills dir).
        assert (root / "src" / "skills").resolve() not in masked
        assert (root / "src").resolve() not in masked
        assert (root / "src" / "secret_lib").resolve() in masked
        assert (root / "tests").resolve() in masked

    def test_multiple_declared_skill_dirs_all_kept(self, tmp_path: Path):
        root = _plugin_root(tmp_path, skills=["skills", "extra_skills"])
        (root / "skills" / "a").mkdir(parents=True)
        (root / "extra_skills" / "b").mkdir(parents=True)
        (root / "tests").mkdir()

        masked = set(mask_dirs(root))

        assert (root / "skills").resolve() not in masked
        assert (root / "extra_skills").resolve() not in masked
        assert (root / "tests").resolve() in masked

    def test_symlinked_child_is_skipped(self, tmp_path: Path):
        root = _plugin_root(tmp_path)
        (root / "skills" / "demo").mkdir(parents=True)
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        os.symlink(outside, root / "evil_link")

        masked = set(mask_dirs(root))

        # A symlinked child is not a valid tmpfs mountpoint and resolves against
        # the container fs -> not masked.
        assert (root / "evil_link") not in masked
        assert outside.resolve() not in masked

    def test_reference_dir_under_root_is_masked(self, tmp_path: Path):
        root = _plugin_root(tmp_path)
        (root / "skills" / "demo").mkdir(parents=True)
        (root / "solution").mkdir()  # a reference dir sibling of skills

        assert (root / "solution").resolve() in set(mask_dirs(root))

    def test_files_at_root_are_not_masked(self, tmp_path: Path):
        # Only directories can be tmpfs mountpoints; a stray file is left alone.
        root = _plugin_root(tmp_path)
        (root / "skills" / "demo").mkdir(parents=True)
        (root / "README.md").write_text("x", encoding="utf-8")

        assert (root / "README.md").resolve() not in set(mask_dirs(root))


class TestSharedResolver:
    def test_uses_shared_manifest_skill_dirs(self):
        # SSOT: eval_material and CE065 must agree on "what is a skill dir".
        from coder_eval.agents._skills import manifest_skill_dirs
        from coder_eval.isolation import eval_material

        assert eval_material.manifest_skill_dirs is manifest_skill_dirs


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
