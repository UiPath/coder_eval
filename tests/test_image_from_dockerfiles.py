"""Tests for building a docker image from a task-supplied Dockerfile.

Covers the three layers of the `sandbox.docker.dockerfile_path` feature:

  1. Load-time path resolution (orchestration.task_loader.resolve_dockerfile_path):
     relative -> absolute against the task YAML dir, env-var expansion, and a
     load-time existence check.
  2. Image build (DockerRunner._build_image): deterministic cached tag, build
     context = the Dockerfile's parent dir, docker build failures -> DockerRunError.
     subprocess is mocked, so these run without a docker daemon.
  3. argv purity (DockerRunner._build_argv): the resolved image is threaded in as
     a parameter and the builder performs no side effects.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner
from coder_eval.models import (
    DockerBuildConfig,
    DockerDriverConfig,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.task_loader import load_task, resolve_dockerfile_path


def _write_task_with_dockerfile(tmp_path: Path, dockerfile_rel: str = "./environment/Dockerfile") -> Path:
    """Write a minimal, self-contained docker task YAML + Dockerfile under tmp_path.

    Returns the task YAML path. Kept independent of the checked-in skillsbench
    fixtures so this suite isn't coupled to their (unrelated) scoring config.
    """
    dockerfile = tmp_path / "environment" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_text("FROM scratch\n")
    task_yaml = {
        "task_id": "df-task",
        "description": "d",
        "initial_prompt": "p",
        "sandbox": {"driver": "docker", "docker": {"dockerfile_path": dockerfile_rel}},
        "success_criteria": [{"type": "file_exists", "path": "o", "description": "c"}],
    }
    task_file = tmp_path / "task.yaml"
    task_file.write_text(yaml.safe_dump(task_yaml))
    return task_file


def _make_runner(
    *,
    task_id: str = "edit-pdf",
    dockerfile_path: str | None = None,
    image: str = "configured:img",
    build: DockerBuildConfig | None = None,
) -> DockerRunner:
    """Build a DockerRunner over a minimal real TaskDefinition.

    `rt` is a MagicMock (matching tests/test_docker_runner_mounts.py) but
    `rt.task` is a real TaskDefinition so docker config / task_id resolve
    naturally. `rt.task_file = None` avoids the symmetric task-dir mount path.
    """
    docker = DockerDriverConfig(image=image, dockerfile_path=dockerfile_path)
    if build is not None:
        docker.build = build
    task = TaskDefinition(
        task_id=task_id,
        description="test",
        initial_prompt="do the thing",
        sandbox=SandboxConfig(driver="docker", docker=docker),
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
    )
    rt = MagicMock()
    rt.task = task
    rt.task_file = None
    return DockerRunner(rt)


def _docker_side_effect(*, entrypoint=("/usr/local/bin/entrypoint.sh",), build_ok=True):
    """subprocess.run side_effect faking `docker build` + `docker image inspect`.

    - `docker build ...`   -> success CompletedProcess (or CalledProcessError if build_ok=False)
    - `docker image inspect ... {{json .Config.Entrypoint}}` -> CompletedProcess whose
      stdout is the JSON-encoded `entrypoint` (a list, or None for "no entrypoint").
    """

    def _run(argv, *args, **kwargs):
        if "build" in argv:
            if not build_ok:
                raise subprocess.CalledProcessError(1, argv, stderr="boom: bad layer")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "inspect" in argv:
            payload = json.dumps(list(entrypoint) if entrypoint is not None else None)
            return subprocess.CompletedProcess(argv, 0, payload, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return _run


# --------------------------------------------------------------------------- #
# Layer 1: load-time path resolution
# --------------------------------------------------------------------------- #
class TestResolveDockerfilePath:
    def test_load_task_resolves_relative_path_to_absolute(self, tmp_path: Path) -> None:
        """A `./environment/Dockerfile` resolves to an existing absolute path via load_task."""
        task_file = _write_task_with_dockerfile(tmp_path)
        task, _raw = load_task(task_file)
        resolved = task.sandbox.docker.dockerfile_path
        assert resolved is not None
        resolved_path = Path(resolved)
        assert resolved_path.is_absolute()
        assert resolved_path.is_file()
        assert resolved_path == (tmp_path / "environment" / "Dockerfile").resolve()

    def test_unset_dockerfile_path_is_noop(self, tmp_path: Path) -> None:
        """A docker task without a dockerfile_path keeps it None."""
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(driver="docker"),
            success_criteria=[FileExistsCriterion(description="c", path="o")],
        )
        out = resolve_dockerfile_path(task, tmp_path)
        assert out.sandbox.docker.dockerfile_path is None

    def test_missing_dockerfile_raises_at_load(self, tmp_path: Path) -> None:
        """A referenced-but-absent Dockerfile fails at load time.

        load_task wraps resolution errors as ValueError (same as a missing
        initial_prompt_file), with the underlying message preserved.
        """
        task_yaml = {
            "task_id": "missing-dockerfile",
            "description": "d",
            "initial_prompt": "p",
            "sandbox": {"driver": "docker", "docker": {"dockerfile_path": "nope/Dockerfile"}},
            "success_criteria": [{"type": "file_exists", "path": "o", "description": "c"}],
        }
        task_file = tmp_path / "task.yaml"
        task_file.write_text(yaml.safe_dump(task_yaml))
        with pytest.raises(ValueError, match="Dockerfile not found"):
            load_task(task_file)

    def test_resolve_helper_raises_filenotfound_directly(self, tmp_path: Path) -> None:
        """resolve_dockerfile_path (called directly) raises the raw FileNotFoundError."""
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(dockerfile_path="missing/Dockerfile")),
            success_criteria=[FileExistsCriterion(description="c", path="o")],
        )
        with pytest.raises(FileNotFoundError, match="Dockerfile not found"):
            resolve_dockerfile_path(task, tmp_path)

    def test_env_var_expansion_in_path(self, tmp_path: Path, monkeypatch) -> None:
        """`$VAR` in the path expands before resolution."""
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        monkeypatch.setenv("MY_DF_DIR", str(tmp_path))
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(dockerfile_path="$MY_DF_DIR/Dockerfile")),
            success_criteria=[FileExistsCriterion(description="c", path="o")],
        )
        out = resolve_dockerfile_path(task, tmp_path)
        assert out.sandbox.docker.dockerfile_path == str(tmp_path / "Dockerfile")


# --------------------------------------------------------------------------- #
# Layer 2: image build (subprocess mocked)
# --------------------------------------------------------------------------- #
class TestBuildImage:
    def test_returns_configured_image_when_no_dockerfile(self, mocker) -> None:
        """Without a dockerfile_path, _build_image returns `image` and never shells out."""
        run = mocker.patch.object(dr.subprocess, "run")
        runner = _make_runner(dockerfile_path=None, image="my-registry/img:1.2")
        assert runner._build_image() == "my-registry/img:1.2"
        run.assert_not_called()

    def test_builds_with_deterministic_tag_and_parent_context(self, tmp_path: Path, mocker) -> None:
        """A dockerfile_path triggers `docker build` with the parent dir as context."""
        dockerfile = tmp_path / "environment" / "Dockerfile"
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        run = mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect())
        runner = _make_runner(task_id="Edit-PDF", dockerfile_path=str(dockerfile))

        image = runner._build_image()

        # Deterministic, lowercased per-task tag.
        assert image == "coder-eval-task-edit-pdf:built"
        # First subprocess call is the build (a second `docker image inspect` validates the entrypoint).
        build_argv = run.call_args_list[0].args[0]
        assert build_argv[:3] == ["docker", "build", "-t"]
        assert build_argv[3] == "coder-eval-task-edit-pdf:built"
        assert build_argv[4] == "-f" and build_argv[5] == str(dockerfile)
        # Build context is the Dockerfile's PARENT dir, not "." / cwd.
        assert build_argv[6] == str(dockerfile.parent)

    def test_build_failure_raises_dockerrunerror(self, tmp_path: Path, mocker) -> None:
        """A non-zero `docker build` surfaces as DockerRunError with stderr."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect(build_ok=False))
        runner = _make_runner(dockerfile_path=str(dockerfile))
        with pytest.raises(DockerRunError, match=r"Failed to build Docker image.*boom: bad layer"):
            runner._build_image()

    def test_rejects_image_without_entrypoint(self, tmp_path: Path, mocker) -> None:
        """A built image with no inherited entrypoint -> actionable DockerRunError (FROM coder-eval-agent)."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM ubuntu:24.04\n")
        mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect(entrypoint=None))
        runner = _make_runner(dockerfile_path=str(dockerfile))
        with pytest.raises(DockerRunError, match=r"FROM coder-eval-agent"):
            runner._build_image()

    def test_rejects_foreign_entrypoint(self, tmp_path: Path, mocker) -> None:
        """An entrypoint that isn't the framework runtime -> DockerRunError."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM ubuntu:24.04\n")
        mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect(entrypoint=["/bin/bash"]))
        runner = _make_runner(dockerfile_path=str(dockerfile))
        with pytest.raises(DockerRunError, match=r"does not inherit the coder-eval runtime entrypoint"):
            runner._build_image()

    def test_accepts_inherited_framework_entrypoint(self, tmp_path: Path, mocker) -> None:
        """An image inheriting the framework entrypoint passes and returns the tag."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        mocker.patch.object(
            dr.subprocess, "run", side_effect=_docker_side_effect(entrypoint=["/usr/local/bin/entrypoint.sh"])
        )
        runner = _make_runner(task_id="ok", dockerfile_path=str(dockerfile))
        assert runner._build_image() == "coder-eval-task-ok:built"

    def test_entrypoint_inspect_failure_is_soft(self, tmp_path: Path, mocker) -> None:
        """If `docker image inspect` itself fails, don't block -- the run surfaces real issues."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")

        def _run(argv, *a, **k):
            if "build" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise subprocess.CalledProcessError(1, argv, stderr="inspect boom")

        mocker.patch.object(dr.subprocess, "run", side_effect=_run)
        runner = _make_runner(task_id="ok", dockerfile_path=str(dockerfile))
        assert runner._build_image() == "coder-eval-task-ok:built"  # no raise


# --------------------------------------------------------------------------- #
# Layer 2b: docker build customization (build args / secrets / extra flags)
# --------------------------------------------------------------------------- #
class TestBuildCustomization:
    @staticmethod
    def _build_argv(tmp_path: Path, mocker, build: DockerBuildConfig):
        """Run _build_image with a build config and return (build_argv, build_env)."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        run = mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect())
        runner = _make_runner(task_id="t", dockerfile_path=str(dockerfile), build=build)
        runner._build_image()
        call = run.call_args_list[0]  # first call is the build
        return call.args[0], call.kwargs.get("env", {})

    def test_build_args_emitted(self, tmp_path: Path, mocker) -> None:
        argv, _ = self._build_argv(tmp_path, mocker, DockerBuildConfig(args={"FOO": "bar", "N": "1"}))
        assert "--build-arg" in argv
        assert "FOO=bar" in argv
        assert "N=1" in argv

    def test_build_arg_values_are_env_expanded(self, tmp_path: Path, mocker, monkeypatch) -> None:
        monkeypatch.setenv("MY_BUILD_VAL", "from-env")
        argv, _ = self._build_argv(tmp_path, mocker, DockerBuildConfig(args={"TOKEN": "${MY_BUILD_VAL}"}))
        assert "TOKEN=from-env" in argv
        assert "TOKEN=${MY_BUILD_VAL}" not in argv

    def test_secrets_emitted(self, tmp_path: Path, mocker) -> None:
        argv, _ = self._build_argv(tmp_path, mocker, DockerBuildConfig(secrets=["id=tok,env=MY_TOKEN"]))
        i = argv.index("--secret")
        assert argv[i + 1] == "id=tok,env=MY_TOKEN"

    def test_extra_args_inserted_before_context(self, tmp_path: Path, mocker) -> None:
        argv, _ = self._build_argv(tmp_path, mocker, DockerBuildConfig(extra_args=["--target", "runtime"]))
        assert argv[-1] == str(tmp_path)  # context is always last
        ti = argv.index("--target")
        assert argv[ti + 1] == "runtime"
        assert ti < len(argv) - 1  # before the trailing context

    def test_buildkit_inherited_when_unset(self, tmp_path: Path, mocker, monkeypatch) -> None:
        """buildkit=None (default) does not set DOCKER_BUILDKIT -- it inherits the invoker's env."""
        monkeypatch.delenv("DOCKER_BUILDKIT", raising=False)
        _, env = self._build_argv(tmp_path, mocker, DockerBuildConfig())
        assert "DOCKER_BUILDKIT" not in env

    def test_buildkit_inherits_host_value(self, tmp_path: Path, mocker, monkeypatch) -> None:
        """buildkit=None passes through whatever the invoker exported."""
        monkeypatch.setenv("DOCKER_BUILDKIT", "0")
        _, env = self._build_argv(tmp_path, mocker, DockerBuildConfig())
        assert env.get("DOCKER_BUILDKIT") == "0"  # not overridden

    def test_buildkit_forced_on(self, tmp_path: Path, mocker, monkeypatch) -> None:
        monkeypatch.delenv("DOCKER_BUILDKIT", raising=False)
        _, env = self._build_argv(tmp_path, mocker, DockerBuildConfig(buildkit=True))
        assert env.get("DOCKER_BUILDKIT") == "1"

    def test_buildkit_forced_off_overrides_host(self, tmp_path: Path, mocker, monkeypatch) -> None:
        monkeypatch.setenv("DOCKER_BUILDKIT", "1")
        _, env = self._build_argv(tmp_path, mocker, DockerBuildConfig(buildkit=False))
        assert env.get("DOCKER_BUILDKIT") == "0"

    def test_secrets_without_buildkit_warns(self, tmp_path: Path, mocker, monkeypatch, caplog) -> None:
        """Configuring secrets without BuildKit logs an actionable warning (secrets need BuildKit)."""
        monkeypatch.delenv("DOCKER_BUILDKIT", raising=False)
        with caplog.at_level(logging.WARNING, logger="coder_eval.isolation.docker_runner"):
            self._build_argv(tmp_path, mocker, DockerBuildConfig(secrets=["id=t,env=X"]))
        assert "BuildKit is not enabled" in caplog.text

    def test_no_customization_keeps_minimal_argv(self, tmp_path: Path, mocker) -> None:
        argv, _ = self._build_argv(tmp_path, mocker, DockerBuildConfig())
        assert "--build-arg" not in argv and "--secret" not in argv
        assert argv[-1] == str(tmp_path)

    def test_build_arg_secret_value_never_reaches_logs(self, tmp_path: Path, mocker, monkeypatch, caplog) -> None:
        """The build log must not include the argv (and thus no --build-arg secret), but docker still gets it."""
        monkeypatch.setenv("LEAKY", "TOPSECRET")
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        run = mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect())
        runner = _make_runner(
            task_id="t", dockerfile_path=str(dockerfile), build=DockerBuildConfig(args={"TOKEN": "${LEAKY}"})
        )

        with caplog.at_level(logging.INFO, logger="coder_eval.isolation.docker_runner"):
            runner._build_image()

        # The build command (and its --build-arg values) must not be logged at all.
        assert "TOPSECRET" not in caplog.text
        assert "--build-arg" not in caplog.text
        # ...but the real docker build argv still received the expanded value.
        build_argv = run.call_args_list[0].args[0]
        assert "TOKEN=TOPSECRET" in build_argv


# --------------------------------------------------------------------------- #
# Layer 3: argv purity
# --------------------------------------------------------------------------- #
class TestBuildArgvImage:
    def test_uses_provided_image(self, monkeypatch) -> None:
        """The image passed to _build_argv is the one placed in the run argv."""
        monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")  # keep argv deterministic
        runner = _make_runner(image="configured:img")
        with tempfile.TemporaryDirectory() as td:
            in_dir, out_dir = Path(td) / "in", Path(td) / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            argv = runner._build_argv(in_dir, out_dir, container_name="c", image="built:xyz")
        assert "built:xyz" in argv
        assert "configured:img" not in argv

    def test_falls_back_to_config_image(self, monkeypatch) -> None:
        """With no image arg, _build_argv uses the configured image."""
        monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")
        runner = _make_runner(image="configured:img")
        with tempfile.TemporaryDirectory() as td:
            in_dir, out_dir = Path(td) / "in", Path(td) / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            argv = runner._build_argv(in_dir, out_dir, container_name="c")
        assert "configured:img" in argv

    def test_build_argv_performs_no_subprocess(self, monkeypatch, mocker) -> None:
        """_build_argv must stay pure -- it must never shell out to docker build."""
        monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")
        run = mocker.patch.object(dr.subprocess, "run")
        runner = _make_runner(dockerfile_path="/some/Dockerfile", image="configured:img")
        with tempfile.TemporaryDirectory() as td:
            in_dir, out_dir = Path(td) / "in", Path(td) / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            runner._build_argv(in_dir, out_dir, container_name="c", image="built:xyz")
        run.assert_not_called()
