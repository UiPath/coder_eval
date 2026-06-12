"""Tests for DockerRunner mount-spec validation and container user/output setup.

Covers the post-merge follow-up hardening: ``:ro`` default when mode is
omitted, rejection of destinations that shadow framework-owned mounts
(``/work``, ``/``), and ``~`` / ``$VAR`` expansion on the source side.

Also covers user/output directory fixes: --user flag on POSIX, output
directory mounted to /work/output, and --output argument using container path.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import (
    CONTAINER_ENTRYPOINT,
    CONTAINER_OUTPUT_DIR,
    DockerRunner,
    _sanitize_container_name_component,
    _validate_extra_mount,
)
from coder_eval.models import FileExistsCriterion, SandboxConfig, TaskDefinition


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
        """Output directory should be mounted to CONTAINER_OUTPUT_DIR (/work/output)."""
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

    @pytest.mark.parametrize("backend", ["bedrock", "direct", "proxy"])
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
