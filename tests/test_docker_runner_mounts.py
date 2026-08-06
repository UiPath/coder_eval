"""Tests for DockerRunner mount-spec validation and container user/output setup.

Covers the post-merge follow-up hardening: ``:ro`` default when mode is
omitted, rejection of destinations that shadow framework-owned mounts
(``/work``, ``/``), and ``~`` / ``$VAR`` expansion on the source side.

Also covers protected output and agent-home mounts plus the container-side
``--output`` argument.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import (
    CLAUDE_COPY_IGNORE,
    CLAUDE_COPY_MAX_ATTEMPTS,
    CONTAINER_ENTRYPOINT,
    CONTAINER_OUTPUT_DIR,
    DockerRunError,
    DockerRunner,
    _copy_claude_home,
    _resolve_workspace_dir,
    _sanitize_container_name_component,
    _validate_extra_mount,
)
from coder_eval.models import AGENT_HOME, FileExistsCriterion, SandboxConfig, TaskDefinition


# DockerRunner targets Linux containers from POSIX hosts. On Windows the test
# fixtures use ``C:\\...`` paths that collide with docker's ``:`` mount-spec
# separator, and ``os.path.expanduser("~")`` honors ``USERPROFILE`` not
# ``HOME`` — neither reflects how `--driver docker` is actually used. Skip
# the module entirely on Windows runners.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


@pytest.fixture
def real_dir():
    with tempfile.TemporaryDirectory() as td:
        yield td


class TestValidateExtraMount:
    def test_default_mode_is_ro(self, real_dir):
        """Mode omitted → :ro appended. Mounting RW by silence is the wrong default."""
        result = _validate_extra_mount(f"{real_dir}:/mnt/x")
        assert result.endswith(":ro")

    def test_explicit_rw_preserved(self, real_dir):
        result = _validate_extra_mount(f"{real_dir}:/mnt/x:rw")
        assert result.endswith(":rw")

    def test_explicit_ro_preserved(self, real_dir):
        result = _validate_extra_mount(f"{real_dir}:/mnt/x:ro")
        assert result.endswith(":ro")

    def test_invalid_mode_rejected(self, real_dir):
        with pytest.raises(ValueError, match="mode must be"):
            _validate_extra_mount(f"{real_dir}:/mnt/x:rwx")

    def test_root_destination_rejected(self, real_dir):
        """Destination ``/`` would shadow the entire container rootfs."""
        with pytest.raises(ValueError, match="shadows a framework-owned mount"):
            _validate_extra_mount(f"{real_dir}:/")

    def test_work_dir_destination_rejected(self, real_dir):
        """``/work`` and children are owned by input/output staging."""
        with pytest.raises(ValueError, match="shadows a framework-owned mount"):
            _validate_extra_mount(f"{real_dir}:/work")

    def test_work_subdir_destination_rejected(self, real_dir):
        with pytest.raises(ValueError, match="shadows a framework-owned mount"):
            _validate_extra_mount(f"{real_dir}:/work/anything")

    def test_relative_destination_rejected(self, real_dir):
        with pytest.raises(ValueError, match="destination must be an absolute path"):
            _validate_extra_mount(f"{real_dir}:relative")

    def test_empty_destination_rejected(self, real_dir):
        with pytest.raises(ValueError, match="empty destination path"):
            _validate_extra_mount(f"{real_dir}:")

    def test_missing_source_rejected(self):
        with pytest.raises(ValueError, match="source path does not exist"):
            _validate_extra_mount("/definitely/not/a/real/path/qwerty:/mnt/x")

    def test_home_expansion_in_source(self, real_dir, monkeypatch):
        """``~`` in the source side expands to ``$HOME``."""
        monkeypatch.setenv("HOME", real_dir)
        result = _validate_extra_mount("~:/mnt/x")
        assert result.startswith(real_dir + ":")

    def test_var_expansion_in_source(self, real_dir, monkeypatch):
        """``$VAR`` in the source side gets expanded."""
        monkeypatch.setenv("MYDIR", real_dir)
        result = _validate_extra_mount("$MYDIR:/mnt/x")
        assert result.startswith(real_dir + ":")

    def test_malformed_no_colon(self):
        with pytest.raises(ValueError, match="expected `src:dst"):
            _validate_extra_mount("just-one-token")

    def test_too_many_colons(self, real_dir):
        with pytest.raises(ValueError, match="expected `src:dst"):
            _validate_extra_mount(f"{real_dir}:/mnt/x:ro:extra")


class TestDockerRunnerUserAndOutput:
    """Tests for Docker runner user/output directory handling."""

    def _make_runner(self, run_dir: Path | None = None) -> DockerRunner:
        """Helper to create a DockerRunner instance for testing."""
        if run_dir is None:
            run_dir = Path(tempfile.gettempdir()) / "test_run"

        task = TaskDefinition(
            task_id="test",
            description="test task",
            initial_prompt="test",
            sandbox=SandboxConfig(),
            success_criteria=[FileExistsCriterion(description="test criterion", path="test.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = run_dir
        rt.task_file = None

        return DockerRunner(rt)

    def test_output_mounted_to_container_output_dir(self):
        """Output directory should be mounted below the protected grader root."""
        runner = self._make_runner()

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            argv = runner._build_argv(input_dir, output_dir, container_name="test-container")

            # Find the -v flag that mounts the output directory
            volume_mounts = []
            for i, arg in enumerate(argv):
                if arg == "-v" and i + 1 < len(argv):
                    volume_mounts.append(argv[i + 1])

            # Check that output_dir is mounted to CONTAINER_OUTPUT_DIR
            output_mount = f"{output_dir}:{CONTAINER_OUTPUT_DIR}"
            assert output_mount in volume_mounts, f"Expected mount {output_mount} not found in {volume_mounts}"

    def test_output_argument_uses_container_path(self):
        """The --output argument should use the container-side path, not host path."""
        runner = self._make_runner()

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            argv = runner._build_argv(input_dir, output_dir, container_name="test-container")

            # --output must always be present (a vacuous `if` would let a dropped
            # flag pass silently).
            assert "--output" in argv
            output_arg = argv[argv.index("--output") + 1]
            # Should be the container-side path, not the host path
            assert output_arg == str(CONTAINER_OUTPUT_DIR)
            assert str(output_dir) not in output_arg

    def test_entrypoint_pinned_before_image(self):
        """Host pins the framework entrypoint via `docker run --entrypoint`.

        It must (a) point at the framework entrypoint, and (b) appear BEFORE the
        image reference -- `--entrypoint` is a `docker run` option, so it has to
        precede the image; anything after the image is the command, not a flag.
        """
        runner = self._make_runner()

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()

            argv = runner._build_argv(
                input_dir, output_dir, container_name="test-container", image="some-task-image:built"
            )

            assert "--entrypoint" in argv
            ep_idx = argv.index("--entrypoint")
            assert argv[ep_idx + 1] == CONTAINER_ENTRYPOINT
            # --entrypoint is a run option, so it precedes the image reference.
            assert ep_idx < argv.index("some-task-image:built")


class TestStagingPrefixSanitized:
    """Dataset fan-out ids are ``suite_id/row_id``; the ``/`` must be stripped from the
    staging mkdtemp prefix (as ``run()`` does) or task setup dies at 0s."""

    def test_sanitized_dataset_row_id_creates_staging_dir(self):
        safe = _sanitize_container_name_component("skill-flow-ixp-activation/explicit")
        assert "/" not in safe
        staging = tempfile.mkdtemp(prefix=f"coder_eval_docker_{safe}_")
        assert Path(staging).is_dir()
        Path(staging).rmdir()


class TestWindowsDriveLetterSource:
    """Drive-letter sources like ``C:\\data:/mnt/data:ro`` must parse on a POSIX host
    so a Windows-authored task at least survives spec validation. The
    ``Path.exists()`` check is patched out because the test host is POSIX —
    the parsing logic, not the existence check, is what we're covering.
    """

    @pytest.fixture
    def _patch_exists(self, monkeypatch):
        from coder_eval.isolation import docker_runner as dr

        monkeypatch.setattr(dr.Path, "exists", lambda self: True)

    @pytest.mark.parametrize(
        ("spec", "expected_src", "expected_mode"),
        [
            (r"C:\data:/mnt/data:ro", r"C:\data", "ro"),
            (r"C:\data:/mnt/data", r"C:\data", "ro"),
            (r"C:/data:/mnt/data:rw", "C:/data", "rw"),
            (r"c:\data:/mnt/data:rw", r"c:\data", "rw"),
        ],
    )
    def test_drive_letter_specs_parse(self, _patch_exists, spec, expected_src, expected_mode):
        result = _validate_extra_mount(spec)
        assert result == f"{expected_src}:/mnt/data:{expected_mode}"

    def test_posix_source_still_parses(self, real_dir):
        """Regression guard: a POSIX source (no drive letter) is unaffected."""
        result = _validate_extra_mount(f"{real_dir}:/mnt/data:ro")
        assert result == f"{real_dir}:/mnt/data:ro"

    def test_unc_path_source_parses(self, _patch_exists):
        r"""UNC paths (``\\server\share``) contain no colon so legacy splitter handles them."""
        result = _validate_extra_mount(r"\\server\share:/mnt/data:ro")
        assert result == r"\\server\share:/mnt/data:ro"

    def test_drive_letter_invalid_mode_rejected(self, _patch_exists):
        with pytest.raises(ValueError, match="mode must be"):
            _validate_extra_mount(r"C:\data:/mnt/data:bogus")

    def test_drive_letter_missing_destination_rejected(self, _patch_exists):
        with pytest.raises(ValueError, match="expected `src:dst"):
            _validate_extra_mount(r"C:\data")


class TestApiBackendForwarding:
    """The run's backend forwards into the container via the standard env allowlist.

    ``API_BACKEND`` is in the default ``env_passthrough`` allowlist, so when it is
    present in ``os.environ`` it forwards name-only (``--env API_BACKEND``) like
    every other allowlisted var — docker copies the value at run time. ``--backend``
    (CLI flag) syncs it into ``os.environ`` at ``run_command`` so the flag path and
    the env-var path behave identically; nothing about the backend is wired through
    a bespoke value-form ``--env K=V``.
    """

    def _make_runner(self) -> DockerRunner:
        task = TaskDefinition(
            task_id="test",
            description="test task",
            initial_prompt="test",
            sandbox=SandboxConfig(),
            success_criteria=[FileExistsCriterion(description="c", path="t.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = Path(tempfile.gettempdir()) / "test_run"
        rt.task_file = None
        return DockerRunner(rt)

    def _forwarded_name_only(self, argv: list[str]) -> bool:
        """True iff API_BACKEND forwards name-only (``--env API_BACKEND``)."""
        return any(arg == "--env" and i + 1 < len(argv) and argv[i + 1] == "API_BACKEND" for i, arg in enumerate(argv))

    def _forwarded_value_form(self, argv: list[str]) -> bool:
        """True iff API_BACKEND forwards via the (removed) bespoke ``--env API_BACKEND=...``."""
        return any(
            arg == "--env" and i + 1 < len(argv) and argv[i + 1].startswith("API_BACKEND=")
            for i, arg in enumerate(argv)
        )

    def _build(self, runner: DockerRunner) -> list[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            return runner._build_argv(input_dir, output_dir, container_name="c")

    @pytest.mark.parametrize("backend", ["bedrock", "direct"])
    def test_backend_in_env_forwarded_name_only(self, monkeypatch, backend):
        monkeypatch.setenv("API_BACKEND", backend)
        argv = self._build(self._make_runner())
        # Forwarded name-only via the standard allowlist — NOT the old bespoke value-form.
        assert self._forwarded_name_only(argv)
        assert not self._forwarded_value_form(argv)

    def test_backend_absent_from_env_not_forwarded(self, monkeypatch):
        """No API_BACKEND in os.environ → not forwarded, same as any allowlisted var."""
        monkeypatch.delenv("API_BACKEND", raising=False)
        argv = self._build(self._make_runner())
        assert not self._forwarded_name_only(argv)
        assert not self._forwarded_value_form(argv)


class TestContainerTelemetryDisabled:
    """The container is always launched with telemetry hard-disabled.

    The app ships a baked-in default connection string, so the in-container
    orchestrator would otherwise emit ``CoderEval.Task.End`` — and the host
    re-emits the same event after parsing the container result
    (``orchestration/batch.py``), double-counting every docker-driver task. The
    invariant is "container silent, host emits once", so ``_build_argv`` injects
    an explicit ``--env TELEMETRY_ENABLED=false`` (value form, not name-only) that
    overrides any inherited/baked value regardless of the host's telemetry state.
    """

    def _make_runner(self) -> DockerRunner:
        task = TaskDefinition(
            task_id="test",
            description="test task",
            initial_prompt="test",
            sandbox=SandboxConfig(),
            success_criteria=[FileExistsCriterion(description="c", path="t.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = Path(tempfile.gettempdir()) / "test_run"
        rt.task_file = None
        return DockerRunner(rt)

    def _build(self, runner: DockerRunner) -> list[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            return runner._build_argv(input_dir, output_dir, container_name="c")

    def _disabled_value_form(self, argv: list[str]) -> bool:
        return any(
            arg == "--env" and i + 1 < len(argv) and argv[i + 1] == "TELEMETRY_ENABLED=false"
            for i, arg in enumerate(argv)
        )

    def test_container_always_gets_telemetry_disabled(self, monkeypatch):
        # Present even when the host has telemetry explicitly ON — the container
        # must stay silent so the host's re-emit is the only Task.End.
        monkeypatch.setenv("TELEMETRY_ENABLED", "true")
        argv = self._build(self._make_runner())
        assert self._disabled_value_form(argv)

    def test_container_telemetry_disabled_when_host_env_unset(self, monkeypatch):
        monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
        argv = self._build(self._make_runner())
        assert self._disabled_value_form(argv)


class TestClaudeHomeRWCopyMount:
    """``~/.claude`` is forwarded as a throwaway lean RW copy, not the host dir.

    ``_prepare_host_mounts`` copies the host ``~/.claude`` (minus heavy
    per-session state) into a tmp dir and records it on
    ``_claude_mount_src``; ``_build_argv`` then mounts that copy read-WRITE at
    the dropped agent's dedicated ``/home/agent/.claude`` path. The old
    symmetric-host-home and two-layer schemes are gone.
    """

    def _make_runner(self) -> DockerRunner:
        task = TaskDefinition(
            task_id="test",
            description="test task",
            initial_prompt="test",
            sandbox=SandboxConfig(),
            success_criteria=[FileExistsCriterion(description="c", path="t.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = Path(tempfile.gettempdir()) / "test_run"
        rt.task_file = None
        return DockerRunner(rt)

    def _volume_mounts(self, argv: list[str]) -> list[str]:
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v" and i + 1 < len(argv)]

    @pytest.fixture
    def fake_home(self, tmp_path, monkeypatch):
        """A fake ``$HOME`` whose ``~/.claude`` seeds one entry per CLAUDE_COPY_IGNORE pattern.

        Driven by the constant so every current AND future denylist member gets
        drop-coverage for free (the prior fixture seeded only 4 of 13). Also seeds
        a top-level ``*.lock`` and a NESTED one (basename-glob matches at every
        level) plus an unrelated ``keep-me.json`` control that must survive.
        """
        home = tmp_path / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        # Kept: the small set the container needs + an unrelated control file.
        (claude / "plugins").mkdir()
        (claude / ".credentials.json").write_text("{}", encoding="utf-8")
        (claude / "settings.json").write_text("{}", encoding="utf-8")
        (claude / "keep-me.json").write_text("{}", encoding="utf-8")
        # Dropped: one seed per denylist pattern.
        for pattern in CLAUDE_COPY_IGNORE:
            if pattern == "*.lock":
                (claude / "top.lock").write_text("x", encoding="utf-8")
                (claude / "plugins" / "nested.lock").write_text("x", encoding="utf-8")  # nested → basename-glob
            elif "." in pattern and not pattern.startswith("."):
                (claude / pattern).write_text("x", encoding="utf-8")  # exact filename (history.jsonl)
            else:
                (claude / pattern).mkdir()  # directory entry (incl. dotdirs like .statusline_cache)
                (claude / pattern / "f").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.delenv("CODER_EVAL_NO_CLAUDE_MOUNT", raising=False)
        return home

    def test_rw_copy_mounted_at_agent_home(self, fake_home, tmp_path):
        runner = self._make_runner()
        staging = tmp_path / "staging"
        staging.mkdir()
        runner._prepare_host_mounts(staging)

        copy = runner._claude_mount_src
        assert copy is not None
        assert copy == staging / "claude-home"

        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        argv = runner._build_argv(input_dir, output_dir, container_name="c")
        mounts = self._volume_mounts(argv)

        host_claude = fake_home / ".claude"
        agent_claude = Path(AGENT_HOME) / ".claude"
        # Exactly one Claude mount: disposable copy → agent HOME, read-WRITE.
        assert f"{copy}:{agent_claude}" in mounts
        claude_mounts = [m for m in mounts if m.endswith(str(agent_claude))]
        assert claude_mounts == [f"{copy}:{agent_claude}"]
        # The retired two-layer scheme leaves no trace.
        assert f"{host_claude}:{host_claude}:ro" not in mounts
        assert not any("session-env" in m for m in mounts)

    def test_lean_copy_drops_every_denylist_entry_keeps_auth(self, fake_home, tmp_path):
        """Every CLAUDE_COPY_IGNORE pattern is dropped; auth/settings/plugins + control survive.

        Drives the drop assertions off the constant so no denylist member (incl. the
        ``*.lock`` basename-glob and the largest ``security/``) can be removed or
        silently fail to match without this test failing.
        """
        runner = self._make_runner()
        staging = tmp_path / "staging"
        staging.mkdir()
        runner._prepare_host_mounts(staging)

        copy = runner._claude_mount_src
        assert copy is not None
        # Kept: auth + settings + plugins + the unrelated control file.
        assert (copy / ".credentials.json").is_file()
        assert (copy / "settings.json").is_file()
        assert (copy / "plugins").is_dir()
        assert (copy / "keep-me.json").is_file()
        # Dropped: every denylist pattern (constant-driven so future entries are covered).
        for pattern in CLAUDE_COPY_IGNORE:
            if pattern == "*.lock":
                assert not (copy / "top.lock").exists()
                assert not (copy / "plugins" / "nested.lock").exists()  # basename-glob: every level
            else:
                assert not (copy / pattern).exists()

    def test_opt_out_skips_copy_and_mount(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")
        runner = self._make_runner()
        staging = tmp_path / "staging"
        staging.mkdir()
        runner._prepare_host_mounts(staging)

        assert runner._claude_mount_src is None
        assert not (staging / "claude-home").exists()

        input_dir = tmp_path / "input"
        output_dir = tmp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        argv = runner._build_argv(input_dir, output_dir, container_name="c")
        host_claude = fake_home / ".claude"
        assert not any(str(host_claude) in m for m in self._volume_mounts(argv))

    def test_copy_retries_transient_oserror_then_succeeds(self, fake_home, tmp_path, monkeypatch):
        """A file vanishing mid-walk (live host churn) is retried, not flipped to a task ERROR.

        The harness runs inside Claude Code, so the live ~/.claude is rewritten while
        copied; under --max-parallel a copytree can raise FileNotFoundError mid-walk.
        The bounded retry must recover so identical agent output isn't scored as ERROR
        by luck of timing.
        """
        real_copytree = shutil.copytree
        dest = tmp_path / "copy"
        calls = {"n": 0}

        # copytree recurses through the module-global name, so count only top-level
        # (dst == dest) invocations — the recursive subdir copies aren't retries.
        def flaky(src, dst, *args, **kwargs):
            if Path(dst) == dest:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise FileNotFoundError("entry vanished mid-walk")
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr("coder_eval.isolation.docker_runner.shutil.copytree", flaky)
        _copy_claude_home(fake_home / ".claude", dest)

        assert calls["n"] == 2  # failed once, succeeded on the retry
        assert (dest / ".credentials.json").is_file()  # clean copy after the partial was cleared

    def test_copy_raises_after_exhausting_retries(self, fake_home, tmp_path, monkeypatch):
        """A persistent copy failure raises DockerRunError after CLAUDE_COPY_MAX_ATTEMPTS tries."""
        calls = {"n": 0}

        def always_fail(*args, **kwargs):
            calls["n"] += 1
            raise FileNotFoundError("keeps vanishing")

        monkeypatch.setattr("coder_eval.isolation.docker_runner.shutil.copytree", always_fail)
        with pytest.raises(DockerRunError):
            _copy_claude_home(fake_home / ".claude", tmp_path / "copy")
        assert calls["n"] == CLAUDE_COPY_MAX_ATTEMPTS  # bounded, not infinite


def test_docker_isolation_doc_names_only_real_denylist_entries():
    """The DOCKER_ISOLATION.md lean-copy bullet must agree with CLAUDE_COPY_IGNORE.

    Drift-guard for the doc finding: the prose may enumerate a subset, but every
    directory it names in the *drops* clause must be a real denylist member, and the
    headline ``security/`` (the largest, previously-omitted drop) must be named — so
    the user-facing contract can't silently diverge from the source-of-truth constant.
    """
    doc = (Path(__file__).parent.parent / "docs" / "DOCKER_ISOLATION.md").read_text(encoding="utf-8")
    line = next(line for line in doc.splitlines() if "throwaway *lean copy*" in line)
    drops_clause = line.split("drops", 1)[1]  # exclude the kept set (plugins/ etc.) named before "drops"
    named = set(re.findall(r"`([A-Za-z0-9._-]+)/`", drops_clause))  # backticked `dir/` tokens
    assert named, "expected the doc to name some dropped dirs"
    extra = named - set(CLAUDE_COPY_IGNORE)
    assert not extra, f"doc names skip entries not in CLAUDE_COPY_IGNORE: {extra}"
    assert "security" in named, "doc must name the headline `security/` drop"


def test_copy_claude_home_tolerates_self_referential_symlink_loop(tmp_path: Path) -> None:
    """A self-referential symlink in the plugin cache must not make the ~/.claude copy
    recurse infinitely.

    Regression: a Claude plugin marketplace can ship a symlink like
    ``uipath-marketplace/plugins/uipath -> ..`` (so ``plugins/uipath/`` resolves to the
    plugin root). With ``copytree(symlinks=False)`` the copy FOLLOWED it and recursed
    ``plugins/uipath/plugins/uipath/...`` forever, raising OSError "too many levels of
    symbolic links" and aborting docker setup with FinalStatus.ERROR. Copying symlinks
    verbatim (``symlinks=True``) is correct and loop-proof.
    """
    host = tmp_path / "claude_home"
    mkt_plugins = host / "plugins" / "marketplaces" / "uipath-marketplace" / "plugins"
    mkt_plugins.mkdir(parents=True)
    # The loop: relative symlink to the parent dir.
    os.symlink("..", mkt_plugins / "uipath")
    # A normal file that must still copy through.
    (host / "settings.json").write_text('{"ok": true}')

    dest = tmp_path / "copy"
    _copy_claude_home(host, dest)  # must not raise (pre-fix: "too many levels of symbolic links")

    assert (dest / "settings.json").read_text() == '{"ok": true}'
    looped = dest / "plugins" / "marketplaces" / "uipath-marketplace" / "plugins" / "uipath"
    assert looped.is_symlink(), "the looping symlink must be copied verbatim, not followed"
    assert os.readlink(looped) == "..", "the symlink target must be preserved unchanged"


class TestWorkspaceDir:
    """Docker WORKDIR alignment: _resolve_workspace_dir + `-w` argv rendering."""

    def _make_runner(self, working_dir=None) -> DockerRunner:
        from coder_eval.models import DockerDriverConfig

        task = TaskDefinition(
            task_id="test",
            description="test task",
            initial_prompt="test",
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(working_dir=working_dir)),
            success_criteria=[FileExistsCriterion(description="c", path="t.txt")],
        )
        rt = MagicMock()
        rt.task = task
        rt.run_dir = Path(tempfile.gettempdir()) / "test_run"
        rt.task_file = None
        return DockerRunner(rt)

    def test_resolve_none_returns_none(self):
        assert _resolve_workspace_dir(None, "img") is None

    def test_resolve_concrete_path_passthrough(self):
        assert _resolve_workspace_dir("/root", "img") == "/root"

    def test_resolve_reserved_concrete_raises(self):
        with pytest.raises(DockerRunError, match="framework-reserved"):
            _resolve_workspace_dir("/work/output", "img")

    def test_resolve_auto_uses_inspected_workdir(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return MagicMock(stdout="/app\n")

        monkeypatch.setattr("coder_eval.isolation.docker_runner.subprocess.run", fake_run)
        assert _resolve_workspace_dir("auto", "img") == "/app"

    @pytest.mark.parametrize("workdir_out", ["", "/"])
    def test_resolve_auto_empty_or_root_falls_back(self, monkeypatch, workdir_out):
        monkeypatch.setattr(
            "coder_eval.isolation.docker_runner.subprocess.run",
            lambda *a, **k: MagicMock(stdout=workdir_out + "\n"),
        )
        assert _resolve_workspace_dir("auto", "img") == "/root"

    def test_resolve_auto_inspect_failure_falls_back(self, monkeypatch):
        import subprocess

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "docker")

        monkeypatch.setattr("coder_eval.isolation.docker_runner.subprocess.run", boom)
        assert _resolve_workspace_dir("auto", "img") == "/root"

    def _argv(self, runner) -> list[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            output_dir = Path(tmpdir) / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            return runner._build_argv(input_dir, output_dir, container_name="c", image="img")

    def test_argv_renders_w_when_set_and_no_bind_mount(self):
        runner = self._make_runner(working_dir="/root")
        runner._workspace_dir = "/root"  # normally set by run(); _build_argv reads it
        argv = self._argv(runner)
        assert "-w" in argv
        assert argv[argv.index("-w") + 1] == "/root"
        # No -v mount should target the workspace dir (capture is copy-out, not a mount).
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v" and i + 1 < len(argv)]
        assert not any(m.endswith(":/root") or m == "/root:/root" for m in mounts)

    def test_argv_no_w_when_unset(self):
        runner = self._make_runner(working_dir=None)
        assert runner._workspace_dir is None
        assert "-w" not in self._argv(runner)

    def test_argv_reserved_workspace_raises(self):
        runner = self._make_runner()
        runner._workspace_dir = "/work/output"  # bypasses model validator (direct set)
        with pytest.raises(DockerRunError, match="framework-reserved"):
            self._argv(runner)

    def test_container_paths_reexported_from_docker_runner(self):
        # Existing importers read CONTAINER_OUTPUT_DIR from docker_runner; keep that working.
        assert CONTAINER_OUTPUT_DIR == "/opt/coder-eval/grader/output"
