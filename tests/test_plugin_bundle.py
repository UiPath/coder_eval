"""Tests for the agent-visible plugin bundle (file-level allowlist + digest verification).

Covers the tempdir-driver answer-key-leak fix: the agent's ``plugins[].path``
must point at a verified bundle carrying only plugin-discovery content — never
the raw skills checkout with its ``RESOLUTION.md`` answers, ``check_*.py``
graders, and ``tests/`` fixtures — while grading (``run_command`` criteria via
``$SKILLS_REPO_PATH``) keeps the raw path.
"""

import os
from pathlib import Path

import pytest

from coder_eval import plugin_bundle
from coder_eval.plugin_bundle import (
    PluginBundleError,
    build_manifest,
    manifest_path_for,
    stage_agent_plugins,
    stage_bundle,
    verify_bundle,
)


# Symlink creation on Windows requires either admin privileges or Developer
# Mode enabled; CI runners usually have neither. Mark the tests that rely on
# os.symlink so they skip cleanly there.
_SKIP_NO_SYMLINK = pytest.mark.skipif(
    os.name == "nt",
    reason="Symlink creation on Windows requires admin or Developer Mode; not asserted in CI.",
)


@pytest.fixture(autouse=True)
def _isolated_staging(tmp_path: Path, monkeypatch):
    """Fresh per-test bundle cache + staging root (module-level state otherwise leaks)."""
    monkeypatch.setattr(plugin_bundle, "_BUNDLE_CACHE", {})
    monkeypatch.setattr(plugin_bundle, "_STAGING_ROOT", tmp_path / "bundle-staging")


def _make_skills_repo(root: Path) -> Path:
    """A miniature skills checkout: plugin content PLUS its own answer key."""
    repo = root / "skills-repo"
    (repo / "skills" / "uipath-troubleshoot" / "references").mkdir(parents=True)
    (repo / "skills" / "uipath-troubleshoot" / "SKILL.md").write_text("# troubleshoot", encoding="utf-8")
    (repo / "skills" / "uipath-troubleshoot" / "references" / "guide.md").write_text("guide", encoding="utf-8")
    # A nested `tests` dir inside a skill is LEGITIMATE shipped client content
    # (mirrors skills/uipath-coded-apps/assets/scripts/dashboards/tests/ in the
    # real repo) — it must project into the bundle, unlike the repo-root tests/.
    (repo / "skills" / "uipath-coded-apps" / "assets" / "scripts" / "dashboards" / "tests").mkdir(parents=True)
    (repo / "skills" / "uipath-coded-apps" / "assets" / "scripts" / "dashboards" / "tests" / "dash.test.ts").write_text(
        "test", encoding="utf-8"
    )
    (repo / "commands").mkdir()
    (repo / "commands" / "triage.md").write_text("cmd", encoding="utf-8")
    (repo / ".claude-plugin").mkdir()
    (repo / ".claude-plugin" / "marketplace.json").write_text("{}", encoding="utf-8")
    # Grading material that must NEVER reach the agent:
    (repo / "tests" / "tasks" / "t1").mkdir(parents=True)
    (repo / "tests" / "tasks" / "t1" / "check_result.py").write_text("assert True", encoding="utf-8")
    (repo / "tests" / "tasks" / "t1" / "RESOLUTION.md").write_text("the answer", encoding="utf-8")
    (repo / "reference_agents").mkdir()
    (repo / "reference_agents" / "golden.py").write_text("golden", encoding="utf-8")
    (repo / "README.md").write_text("readme", encoding="utf-8")
    return repo


def _bundle_rel_paths(bundle_dir: Path) -> set[str]:
    return {p.relative_to(bundle_dir).as_posix() for p in bundle_dir.rglob("*") if not p.is_dir()}


class TestAdversarialProjection:
    """Tempdir flavor of PR #85's adversarial probe: no graded material in the bundle."""

    def test_bundle_contains_only_allowed_subtrees(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        bundle = tmp_path / "bundle"
        stage_bundle(repo, bundle)
        assert _bundle_rel_paths(bundle) == {
            "skills/uipath-troubleshoot/SKILL.md",
            "skills/uipath-troubleshoot/references/guide.md",
            "skills/uipath-coded-apps/assets/scripts/dashboards/tests/dash.test.ts",
            "commands/triage.md",
            ".claude-plugin/marketplace.json",
        }

    def test_bundle_has_no_answer_key_material(self, tmp_path: Path):
        """The adversarial assertion: nothing an agent could grade itself with."""
        repo = _make_skills_repo(tmp_path)
        bundle = tmp_path / "bundle"
        stage_bundle(repo, bundle)
        for path in bundle.rglob("*"):
            rel = path.relative_to(bundle).as_posix().lower()
            assert not rel.startswith("tests/")  # the repo-root grader tree
            assert "tests/tasks" not in rel
            # Exact-name semantics, matching the builder: a doc like
            # reference-resolution.md is legitimate; the answer file is not.
            assert path.name.lower() != "resolution.md"
            assert not (path.name.lower().startswith("check_") and path.suffix == ".py")
            assert "reference_agents" not in rel

    def test_empty_source_yields_empty_bundle_loudly(self, tmp_path: Path, caplog):
        repo = tmp_path / "no-plugin-content"
        (repo / "tests").mkdir(parents=True)
        (repo / "README.md").write_text("x", encoding="utf-8")
        with caplog.at_level("WARNING"):
            staged, digests = stage_agent_plugins([{"type": "local", "path": str(repo)}])
        bundle_dir = Path(staged[0]["path"])
        assert bundle_dir != repo.resolve()
        assert _bundle_rel_paths(bundle_dir) == set()
        assert str(repo.resolve()) in digests
        assert any("EMPTY plugin bundle" in r.message for r in caplog.records)


class TestHiddenMaterialInsideAllowedSubtrees:
    """A file-level allowlist, not a subdir allowlist: grading material filed
    under skills/ fails the BUILD loudly instead of shipping."""

    def test_resolution_md_inside_skills_fails_build(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        (repo / "skills" / "uipath-troubleshoot" / "RESOLUTION.md").write_text("leak", encoding="utf-8")
        with pytest.raises(PluginBundleError, match="hidden grading material"):
            build_manifest(repo)

    def test_check_script_inside_skills_fails_build(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        (repo / "skills" / "uipath-troubleshoot" / "check_output.py").write_text("leak", encoding="utf-8")
        with pytest.raises(PluginBundleError, match="hidden grading material"):
            build_manifest(repo)

    def test_case_insensitive_match(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        (repo / "skills" / "uipath-troubleshoot" / "Resolution.MD").write_text("leak", encoding="utf-8")
        with pytest.raises(PluginBundleError, match="hidden grading material"):
            build_manifest(repo)

    def test_nested_tests_dir_inside_skill_is_allowed(self, tmp_path: Path):
        """Regression lock: a skill's own `tests` folder is shipped client
        content, NOT grading material — the real skills repo carries
        skills/uipath-coded-apps/assets/scripts/dashboards/tests/ and a deep
        `tests` path-component check rejected the whole repo. Grading material
        lives at the repo-root tests/ tree, which the subtree allowlist already
        excludes by construction. Do not reintroduce a directory-name check."""
        repo = _make_skills_repo(tmp_path)
        bundle = tmp_path / "bundle"
        manifest = stage_bundle(repo, bundle)
        nested = "skills/uipath-coded-apps/assets/scripts/dashboards/tests/dash.test.ts"
        assert nested in manifest.files
        assert (bundle / nested).exists()


class TestSymlinkSafety:
    @_SKIP_NO_SYMLINK
    def test_symlink_escaping_source_root_rejected(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, repo / "skills" / "uipath-troubleshoot" / "leak.txt")
        with pytest.raises(PluginBundleError, match="escapes the source root"):
            build_manifest(repo)

    @_SKIP_NO_SYMLINK
    def test_in_root_symlink_copied_verbatim(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        os.symlink("SKILL.md", repo / "skills" / "uipath-troubleshoot" / "alias.md")
        bundle = tmp_path / "bundle"
        manifest = stage_bundle(repo, bundle)
        link = bundle / "skills" / "uipath-troubleshoot" / "alias.md"
        assert link.is_symlink()
        assert os.readlink(link) == "SKILL.md"
        assert manifest.symlinks == {"skills/uipath-troubleshoot/alias.md": "SKILL.md"}

    @_SKIP_NO_SYMLINK
    def test_self_referential_symlink_is_loop_proof(self, tmp_path: Path):
        """Marketplace-style `skills/loop -> ..` must neither recurse nor escape."""
        repo = _make_skills_repo(tmp_path)
        os.symlink("..", repo / "skills" / "loop")
        bundle = tmp_path / "bundle"
        manifest = stage_bundle(repo, bundle)
        assert manifest.symlinks == {"skills/loop": ".."}
        assert (bundle / "skills" / "loop").is_symlink()


class TestDigestVerification:
    def _staged(self, tmp_path: Path) -> Path:
        repo = _make_skills_repo(tmp_path)
        bundle = tmp_path / "bundle"
        stage_bundle(repo, bundle)
        return bundle

    def test_manifest_recorded_next_to_bundle(self, tmp_path: Path):
        bundle = self._staged(tmp_path)
        mpath = manifest_path_for(bundle)
        assert mpath.exists()
        assert mpath.parent == bundle.parent  # never inside the agent-visible tree
        manifest = verify_bundle(bundle)
        assert manifest.digest and len(manifest.digest) == 64

    def test_tampered_file_fails_closed(self, tmp_path: Path):
        bundle = self._staged(tmp_path)
        (bundle / "skills" / "uipath-troubleshoot" / "SKILL.md").write_text("tampered", encoding="utf-8")
        with pytest.raises(PluginBundleError, match="does not match its manifest digest"):
            verify_bundle(bundle)

    def test_undeclared_file_fails_closed(self, tmp_path: Path):
        bundle = self._staged(tmp_path)
        (bundle / "skills" / "smuggled.md").write_text("extra", encoding="utf-8")
        with pytest.raises(PluginBundleError, match="undeclared"):
            verify_bundle(bundle)

    def test_missing_declared_file_fails_closed(self, tmp_path: Path):
        bundle = self._staged(tmp_path)
        (bundle / "commands" / "triage.md").unlink()
        with pytest.raises(PluginBundleError, match="missing"):
            verify_bundle(bundle)

    def test_edited_manifest_fails_its_own_digest(self, tmp_path: Path):
        bundle = self._staged(tmp_path)
        mpath = manifest_path_for(bundle)
        mpath.write_text(mpath.read_text(encoding="utf-8").replace("troubleshoot", "troublesh00t"), encoding="utf-8")
        with pytest.raises(PluginBundleError, match="fails its own digest"):
            verify_bundle(bundle)


class TestStageAgentPlugins:
    def test_rewrites_path_expands_env_var_and_records_digest(self, tmp_path: Path, monkeypatch):
        repo = _make_skills_repo(tmp_path)
        monkeypatch.setenv("SKILLS_REPO_PATH", str(repo))
        staged, digests = stage_agent_plugins([{"type": "local", "path": "$SKILLS_REPO_PATH"}])
        assert staged[0]["type"] == "local"
        bundle_dir = Path(staged[0]["path"])
        assert bundle_dir != repo.resolve()
        assert (bundle_dir / "skills" / "uipath-troubleshoot" / "SKILL.md").exists()
        assert not (bundle_dir / "tests").exists()
        assert digests == {str(repo.resolve()): verify_bundle(bundle_dir).digest}

    def test_bundle_built_once_per_source(self, tmp_path: Path, monkeypatch):
        repo = _make_skills_repo(tmp_path)
        calls: list[Path] = []
        real_stage = plugin_bundle.stage_bundle

        def counting_stage(source: Path, bundle_dir: Path):
            calls.append(source)
            return real_stage(source, bundle_dir)

        monkeypatch.setattr(plugin_bundle, "stage_bundle", counting_stage)
        plugins = [{"type": "local", "path": str(repo)}]
        first, _ = stage_agent_plugins(plugins)
        second, _ = stage_agent_plugins(plugins)
        assert len(calls) == 1  # once per run, not once per task
        assert first[0]["path"] == second[0]["path"]

    async def test_concurrent_staging_copies_exactly_once(self, tmp_path: Path, monkeypatch):
        """run_batch executes tasks concurrently (asyncio + to_thread); N
        concurrent stagings of the same source must produce exactly ONE copy —
        a lock that merely serializes N copies would still be a regression at
        suite scale (~297 tasks x a 17 MB skills checkout)."""
        import asyncio

        repo = _make_skills_repo(tmp_path)
        copies: list[Path] = []
        real_stage = plugin_bundle.stage_bundle

        def counting_stage(source: Path, bundle_dir: Path):
            copies.append(bundle_dir)
            return real_stage(source, bundle_dir)

        monkeypatch.setattr(plugin_bundle, "stage_bundle", counting_stage)
        plugins = [{"type": "local", "path": str(repo)}]
        results = await asyncio.gather(
            *(asyncio.to_thread(stage_agent_plugins, plugins) for _ in range(8)),
        )
        assert len(copies) == 1  # one copy total, not one per concurrent task
        staged_paths = {staged[0]["path"] for staged, _ in results}
        assert staged_paths == {str(copies[0])}

    def test_drifted_bundle_aborts_instead_of_falling_back(self, tmp_path: Path):
        repo = _make_skills_repo(tmp_path)
        plugins = [{"type": "local", "path": str(repo)}]
        staged, _ = stage_agent_plugins(plugins)
        bundle_dir = Path(staged[0]["path"])
        (bundle_dir / "skills" / "planted.md").write_text("drift", encoding="utf-8")
        with pytest.raises(PluginBundleError, match="does not match its manifest digest"):
            stage_agent_plugins(plugins)

    def test_nonexistent_path_passes_through_unchanged(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("NOT_A_REAL_SKILLS_VAR", raising=False)
        plugins = [{"type": "local", "path": "$NOT_A_REAL_SKILLS_VAR/skills"}]
        staged, digests = stage_agent_plugins(plugins)
        assert digests == {}
        # The agents' own loud missing-path warnings stay authoritative.
        assert "NOT_A_REAL_SKILLS_VAR" in staged[0]["path"]

    def test_entry_without_path_passes_through(self):
        staged, digests = stage_agent_plugins([{"type": "local"}])
        assert staged == [{"type": "local"}]
        assert digests == {}


class TestOrchestratorSeam:
    async def test_setup_seam_rewrites_task_agent_plugins(self, tmp_path: Path, monkeypatch):
        """The orchestrator rewrites agent.plugins in place before agent creation."""
        from datetime import datetime

        from coder_eval.models import EvaluationResult, FinalStatus, TaskDefinition
        from coder_eval.orchestrator import Orchestrator

        repo = _make_skills_repo(tmp_path)
        monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        task = TaskDefinition.model_validate(
            {
                "task_id": "bundle-seam",
                "description": "d",
                "initial_prompt": "p",
                "agent": {"type": "claude-code", "plugins": [{"type": "local", "path": str(repo)}]},
                "success_criteria": [{"type": "file_exists", "description": "out exists", "path": "out.txt"}],
            }
        )
        orch = Orchestrator(task, run_dir=tmp_path / "run", variant_id="default")
        orch.result = EvaluationResult(
            task_id="bundle-seam",
            task_description="d",
            variant_id="default",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status=FinalStatus.FAILURE,
            iteration_count=0,
        )
        await orch._stage_agent_plugin_bundles()
        assert task.agent is not None and task.agent.plugins is not None
        bundle_dir = Path(task.agent.plugins[0]["path"])
        assert bundle_dir != repo.resolve()
        assert (bundle_dir / "skills" / "uipath-troubleshoot" / "SKILL.md").exists()
        assert not (bundle_dir / "tests").exists()
        assert orch.result.environment_info["plugin_bundles"] == {str(repo.resolve()): verify_bundle(bundle_dir).digest}

    async def test_setup_seam_skipped_in_container(self, tmp_path: Path, monkeypatch):
        """In-container runs keep the docker driver's own staging surface (PR #85)."""
        from coder_eval.models import TaskDefinition
        from coder_eval.orchestrator import Orchestrator

        repo = _make_skills_repo(tmp_path)
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        task = TaskDefinition.model_validate(
            {
                "task_id": "bundle-seam-docker",
                "description": "d",
                "initial_prompt": "p",
                "agent": {"type": "claude-code", "plugins": [{"type": "local", "path": str(repo)}]},
                "success_criteria": [{"type": "file_exists", "description": "out exists", "path": "out.txt"}],
            }
        )
        orch = Orchestrator(task, run_dir=tmp_path / "run", variant_id="default")
        await orch._stage_agent_plugin_bundles()
        assert task.agent is not None and task.agent.plugins is not None
        assert task.agent.plugins[0]["path"] == str(repo)


class TestAgentEnvScrubVsGradingEnv:
    """SKILLS_REPO_PATH is masked from every agent subprocess but stays fully
    resolvable in the grading env (run_command criteria invoke
    `python3 $SKILLS_REPO_PATH/tests/.../check_*.py`)."""

    def test_grading_env_still_resolves_skills_repo_path(self, tmp_path: Path, monkeypatch):
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox

        monkeypatch.setenv("SKILLS_REPO_PATH", str(tmp_path / "skills"))
        sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="grading-env")
        try:
            sandbox.setup()
            env = sandbox._build_run_command_env()
            assert env["SKILLS_REPO_PATH"] == str(tmp_path / "skills")
        finally:
            sandbox.cleanup()

    def test_claude_sdk_env_masks_harness_vars(self, monkeypatch):
        from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
        from coder_eval.models import DirectRoute

        monkeypatch.setenv("SKILLS_REPO_PATH", "/host/skills")
        monkeypatch.setenv("CODER_EVAL_RAW_SDK_LOG", "1")
        env, _ = ClaudeCodeAgent._build_sdk_env(DirectRoute())
        assert env["SKILLS_REPO_PATH"] == ""
        assert env["CODER_EVAL_RAW_SDK_LOG"] == ""

    def test_scrub_overrides_are_explicit_not_omitted(self, monkeypatch):
        """The SDK merges {**os.environ, **options.env}: an omitted key would
        inherit; only an explicit empty override masks it."""
        from coder_eval.utils import scrub_agent_env_overrides

        monkeypatch.setenv("SKILLS_REPO_PATH", "/host/skills")
        monkeypatch.setenv("CODER_EVAL_DEBUG", "1")
        scrub = scrub_agent_env_overrides()
        assert scrub["SKILLS_REPO_PATH"] == ""
        assert scrub["CODER_EVAL_DEBUG"] == ""
        # SKILLS_REPO_PATH masked even when unset in the parent (deterministic contract).
        monkeypatch.delenv("SKILLS_REPO_PATH")
        assert scrub_agent_env_overrides()["SKILLS_REPO_PATH"] == ""
