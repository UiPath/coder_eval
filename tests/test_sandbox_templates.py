"""Tests for sandbox template functionality."""

import pytest

from coder_eval.models import RepoSource, SandboxConfig, StarterFile, StarterFilesSource, TemplateDirSource
from coder_eval.sandbox import Sandbox


class TestTemplateDir:
    """Tests for template_dir functionality."""

    def test_template_dir_basic(self, tmp_path):
        """Test copying template directory to sandbox."""
        # Create template directory
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "main.py").write_text("print('hello')")
        (template_dir / "README.md").write_text("# Test Project")

        # Create sandbox with template
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-template")

        try:
            sandbox_path = sandbox.setup()

            # Verify files copied
            assert (sandbox_path / "main.py").exists()
            assert (sandbox_path / "main.py").read_text() == "print('hello')"
            assert (sandbox_path / "README.md").exists()

            # Verify venv created (separate from template)
            assert (sandbox_path / ".venv").exists()
        finally:
            sandbox.cleanup(preserve=False)

    def test_template_dir_with_subdirs(self, tmp_path):
        """Test copying template with nested directory structure."""
        # Create template with subdirectories
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "src").mkdir()
        (template_dir / "src" / "module.py").write_text("def foo(): pass")
        (template_dir / "tests").mkdir()
        (template_dir / "tests" / "test_module.py").write_text("def test_foo(): pass")

        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-nested")

        try:
            sandbox_path = sandbox.setup()

            # Verify nested structure preserved
            assert (sandbox_path / "src" / "module.py").exists()
            assert (sandbox_path / "tests" / "test_module.py").exists()
            assert (sandbox_path / "src" / "module.py").read_text() == "def foo(): pass"
        finally:
            sandbox.cleanup(preserve=False)

    def test_template_dir_not_found(self, tmp_path):
        """Test error handling when template directory doesn't exist."""
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(tmp_path / "nonexistent")),
            ],
        )
        sandbox = Sandbox(config, task_id="test-notfound")

        with pytest.raises(RuntimeError, match="Template directory not found"):
            sandbox.setup()


class TestTemplateIgnorePatterns:
    """Tests for template ignore patterns."""

    def test_template_ignores_venv(self, tmp_path):
        """Test that .venv directory is skipped."""
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "main.py").write_text("print('test')")
        (template_dir / ".venv").mkdir()
        (template_dir / ".venv" / "bin").mkdir()
        (template_dir / ".venv" / "bin" / "python").write_text("fake python")

        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-ignore-venv")

        try:
            sandbox_path = sandbox.setup()

            # Verify main.py copied
            assert (sandbox_path / "main.py").exists()

            # Verify .venv from template was NOT copied
            # (sandbox creates its own .venv)
            venv_bin = sandbox_path / ".venv" / "bin"
            if venv_bin.exists():
                # If .venv exists, it should be the sandbox's venv, not the template's
                assert not (venv_bin / "python").exists() or (venv_bin / "python").is_symlink()
        finally:
            sandbox.cleanup(preserve=False)

    def test_template_ignores_git(self, tmp_path):
        """Test that .git directory is skipped."""
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "code.py").write_text("# code")
        (template_dir / ".git").mkdir()
        (template_dir / ".git" / "config").write_text("fake git")

        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-ignore-git")

        try:
            sandbox_path = sandbox.setup()

            # Verify code.py copied
            assert (sandbox_path / "code.py").exists()

            # Verify .git not copied
            assert not (sandbox_path / ".git").exists()
        finally:
            sandbox.cleanup(preserve=False)

    def test_template_ignores_pycache(self, tmp_path):
        """Test that __pycache__ is skipped."""
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "module.py").write_text("def func(): pass")
        (template_dir / "__pycache__").mkdir()
        (template_dir / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"fake pyc")

        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-ignore-pycache")

        try:
            sandbox_path = sandbox.setup()

            # Verify module.py copied
            assert (sandbox_path / "module.py").exists()

            # Verify __pycache__ not copied
            assert not (sandbox_path / "__pycache__").exists()
        finally:
            sandbox.cleanup(preserve=False)


class TestStarterFiles:
    """Tests for starter_files functionality."""

    def test_starter_files_simple(self, tmp_path):
        """Test creating single inline file."""
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                StarterFilesSource(
                    files=[
                        StarterFile(path="hello.py", content="print('Hello, World!')"),
                    ]
                ),
            ],
        )
        sandbox = Sandbox(config, task_id="test-starter-simple")

        try:
            sandbox_path = sandbox.setup()

            # Verify file created
            assert (sandbox_path / "hello.py").exists()
            assert (sandbox_path / "hello.py").read_text() == "print('Hello, World!')"
        finally:
            sandbox.cleanup(preserve=False)

    def test_starter_files_nested(self, tmp_path):
        """Test creating multiple files with subdirectories."""
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                StarterFilesSource(
                    files=[
                        StarterFile(path="README.md", content="# Project"),
                        StarterFile(path="src/main.py", content="def main(): pass"),
                        StarterFile(path="tests/test_main.py", content="def test_main(): pass"),
                    ]
                ),
            ],
        )
        sandbox = Sandbox(config, task_id="test-starter-nested")

        try:
            sandbox_path = sandbox.setup()

            # Verify all files created
            assert (sandbox_path / "README.md").exists()
            assert (sandbox_path / "src" / "main.py").exists()
            assert (sandbox_path / "tests" / "test_main.py").exists()
            assert (sandbox_path / "README.md").read_text() == "# Project"
        finally:
            sandbox.cleanup(preserve=False)

    def test_starter_files_path_traversal(self, tmp_path):
        """Test that path traversal is rejected."""
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                StarterFilesSource(
                    files=[
                        StarterFile(path="../etc/passwd", content="malicious"),
                    ]
                ),
            ],
        )
        sandbox = Sandbox(config, task_id="test-starter-traversal")

        with pytest.raises(RuntimeError, match=r"Invalid file path.*outside sandbox"):
            sandbox.setup()


class TestIntegration:
    """Integration tests for template functionality."""

    def test_full_task_with_template(self, tmp_path):
        """Test end-to-end task execution with template."""
        # Create template
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "calculator.py").write_text("def add(a, b):\n    return a + b\n")

        # Create sandbox and verify agent can work with template files
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-integration")

        try:
            sandbox_path = sandbox.setup()

            # Verify template file exists
            assert (sandbox_path / "calculator.py").exists()

            # Simulate agent modifying the file
            calc_file = sandbox_path / "calculator.py"
            original_content = calc_file.read_text()
            calc_file.write_text(original_content + "\ndef subtract(a, b):\n    return a - b\n")

            # Verify modification worked
            new_content = calc_file.read_text()
            assert "def add(a, b):" in new_content
            assert "def subtract(a, b):" in new_content
        finally:
            sandbox.cleanup(preserve=False)

    def test_agent_can_modify_template_files(self, tmp_path):
        """Test that agent can write to files from template."""
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                StarterFilesSource(
                    files=[
                        StarterFile(
                            path="main.py",
                            content="# TODO: Implement main function\n",
                        ),
                    ]
                ),
            ],
        )
        sandbox = Sandbox(config, task_id="test-modify")

        try:
            sandbox_path = sandbox.setup()

            # Verify starter file exists
            main_file = sandbox_path / "main.py"
            assert main_file.exists()
            assert "TODO" in main_file.read_text()

            # Simulate agent modification
            main_file.write_text("def main():\n    print('Done!')\n")

            # Verify write worked
            assert "def main():" in main_file.read_text()
            assert "TODO" not in main_file.read_text()
        finally:
            sandbox.cleanup(preserve=False)


class TestMultiSourceTemplates:
    """Tests for multi-source template_sources functionality."""

    def test_multiple_template_dirs(self, tmp_path):
        """Test applying multiple template directories sequentially."""
        # Create base template
        base_template = tmp_path / "base"
        base_template.mkdir()
        (base_template / "README.md").write_text("# Base Project")
        (base_template / "config.py").write_text("DEBUG = False")

        # Create override template
        override_template = tmp_path / "override"
        override_template.mkdir()
        (override_template / "config.py").write_text("DEBUG = True")
        (override_template / "extra.py").write_text("# Extra file")

        # Create sandbox with multiple sources (last wins)
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(base_template)),
                TemplateDirSource(path=str(override_template)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-multi-dir")

        try:
            sandbox_path = sandbox.setup()

            # Verify base files exist
            assert (sandbox_path / "README.md").exists()
            assert (sandbox_path / "README.md").read_text() == "# Base Project"

            # Verify override worked (last wins)
            assert (sandbox_path / "config.py").exists()
            assert (sandbox_path / "config.py").read_text() == "DEBUG = True"

            # Verify extra file from override
            assert (sandbox_path / "extra.py").exists()
        finally:
            sandbox.cleanup(preserve=False)

    def test_template_dir_and_starter_files(self, tmp_path):
        """Test combining template directory with starter files."""
        # Create base template
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "base.py").write_text("# Base code")

        # Create sandbox with template + starter files
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
                StarterFilesSource(
                    files=[
                        StarterFile(path="main.py", content="# Main entry point"),
                        StarterFile(path="config.yaml", content="debug: true"),
                    ]
                ),
            ],
        )
        sandbox = Sandbox(config, task_id="test-dir-starter")

        try:
            sandbox_path = sandbox.setup()

            # Verify template files
            assert (sandbox_path / "base.py").exists()

            # Verify starter files
            assert (sandbox_path / "main.py").exists()
            assert (sandbox_path / "config.yaml").exists()
            assert (sandbox_path / "main.py").read_text() == "# Main entry point"
        finally:
            sandbox.cleanup(preserve=False)

    def test_overwrite_behavior_last_wins(self, tmp_path):
        """Test that file overwrites follow last-wins semantics."""
        # Create two templates with overlapping files
        template1 = tmp_path / "template1"
        template1.mkdir()
        (template1 / "shared.py").write_text("# Version 1")
        (template1 / "unique1.py").write_text("# Unique to template1")

        template2 = tmp_path / "template2"
        template2.mkdir()
        (template2 / "shared.py").write_text("# Version 2")
        (template2 / "unique2.py").write_text("# Unique to template2")

        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template1)),
                TemplateDirSource(path=str(template2)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-overwrite")

        try:
            sandbox_path = sandbox.setup()

            # Verify the second version won (last-wins)
            assert (sandbox_path / "shared.py").read_text() == "# Version 2"

            # Verify unique files from both templates exist
            assert (sandbox_path / "unique1.py").exists()
            assert (sandbox_path / "unique2.py").exists()
            assert (sandbox_path / "unique1.py").read_text() == "# Unique to template1"
            assert (sandbox_path / "unique2.py").read_text() == "# Unique to template2"
        finally:
            sandbox.cleanup(preserve=False)


class TestMultiSourceValidation:
    """Tests for multi-source validation rules."""

    def test_repo_source_must_be_first(self, tmp_path):
        """Test that RepoSource must be first in the list."""
        template_dir = tmp_path / "template"
        template_dir.mkdir()

        # RepoSource not first - should raise error
        with pytest.raises(ValueError, match="RepoSource must be the first element"):
            SandboxConfig(
                driver="tempdir",
                template_sources=[
                    TemplateDirSource(path=str(template_dir)),
                    RepoSource(url="https://github.com/test/repo.git"),
                ],
            )

    def test_repo_source_first_is_valid(self, tmp_path):
        """Test that RepoSource as first source is valid."""
        template_dir = tmp_path / "template"
        template_dir.mkdir()

        # RepoSource first - should be valid
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                RepoSource(url="https://github.com/test/repo.git"),
                TemplateDirSource(path=str(template_dir)),
            ],
        )

        # Should not raise - configuration is valid
        assert config.template_sources[0].type == "repo"
        assert config.template_sources[1].type == "template_dir"

    def test_many_sources_warning(self, tmp_path):
        """Test that many sources triggers a warning."""
        import warnings

        template_dir = tmp_path / "template"
        template_dir.mkdir()

        # Create 11 sources (>10 threshold)
        sources = [TemplateDirSource(path=str(template_dir)) for _ in range(11)]

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            config = SandboxConfig(
                driver="tempdir",
                template_sources=sources,
            )

            # Verify warning was raised
            assert len(w) == 1
            assert "Many template sources" in str(w[0].message)
            assert "11" in str(w[0].message)

        # But config should still be valid
        assert len(config.template_sources) == 11


class TestMultiSourcePathResolution:
    """Tests for path resolution with multi-source templates."""

    def test_template_sources_path_resolution(self, tmp_path):
        """Test that TemplateDirSource paths are resolved."""
        from coder_eval.orchestration.task_loader import resolve_template_paths

        # Create template directory structure
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        template_dir = tmp_path / "templates" / "base"
        template_dir.mkdir(parents=True)

        # Use relative paths in config
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path="../templates/base"),  # Relative
            ],
        )

        from coder_eval.models import AgentConfig, AgentKind, FileExistsCriterion, TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="test",
            agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
            sandbox=config,
            success_criteria=[FileExistsCriterion(description="test", path="test.txt")],
        )

        # Resolve paths relative to tasks_dir
        resolved_task = resolve_template_paths(task, tasks_dir)

        # TemplateDirSource path should be resolved
        assert resolved_task.sandbox.template_sources[0].path == str((tasks_dir / "../templates/base").resolve())

    def test_mixed_relative_and_absolute_paths(self, tmp_path):
        """Test resolution with mix of relative and absolute paths."""
        from coder_eval.orchestration.task_loader import resolve_template_paths

        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        template1 = tmp_path / "templates" / "base"
        template1.mkdir(parents=True)
        template2 = tmp_path / "templates" / "override"
        template2.mkdir(parents=True)

        # Mix of relative and absolute
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path="../templates/base"),  # Relative
                TemplateDirSource(path=str(template2.resolve())),  # Absolute
            ],
        )

        from coder_eval.models import AgentConfig, AgentKind, FileExistsCriterion, TaskDefinition

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="test",
            agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
            sandbox=config,
            success_criteria=[FileExistsCriterion(description="test", path="test.txt")],
        )

        resolved_task = resolve_template_paths(task, tasks_dir)

        # First should be resolved, second unchanged
        assert resolved_task.sandbox.template_sources[0].path == str((tasks_dir / "../templates/base").resolve())
        assert resolved_task.sandbox.template_sources[1].path == str(template2.resolve())


class TestUiPathStarterTemplate:
    """Regression tests for uipath-starter template integrity."""

    def test_uipath_json_copied_to_sandbox(self):
        """Test that uipath.json from uipath-starter template is copied into the sandbox.

        Regression test: without uipath.json, `uv run uipath run main.py` fails with
        'Config file not found: uipath.json'. This test ensures the template includes
        a valid uipath.json and that the sandbox copy mechanism does not skip it.
        """
        import json
        from pathlib import Path

        template_dir = Path(__file__).parent.parent / "templates" / "uipath-starter"
        config = SandboxConfig(
            driver="tempdir",
            template_sources=[
                TemplateDirSource(path=str(template_dir)),
            ],
        )
        sandbox = Sandbox(config, task_id="test-uipath-json")

        try:
            sandbox_path = sandbox.setup()

            # uipath.json must exist in the sandbox
            uipath_json_path = sandbox_path / "uipath.json"
            assert uipath_json_path.exists(), "uipath.json missing from sandbox — uipath run will fail"

            # Must be valid JSON with a 'functions' mapping
            content = json.loads(uipath_json_path.read_text())
            assert "functions" in content, "uipath.json missing 'functions' key"
            assert "main.py" in content["functions"], "uipath.json missing 'main.py' entrypoint"
            assert content["functions"]["main.py"] == "main.py:main"
        finally:
            sandbox.cleanup(preserve=False)
