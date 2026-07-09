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

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation.docker_runner import CONTAINER_ENTRYPOINT, DockerRunError, DockerRunner
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


def _docker_side_effect(*, build_ok=True, version_label="0.3.0"):
    """subprocess.run side_effect faking `docker build` + `docker image inspect`.

    - `docker build ...` -> success (or CalledProcessError if build_ok=False)
    - `docker image inspect ... org.coder-eval.version` -> CompletedProcess whose
      stdout is `version_label` (use "" to simulate a non-framework image with no
      label; `docker inspect` renders a missing label as the empty string).
    - any other docker call -> generic success.
    """

    def _run(argv, *args, **kwargs):
        if "build" in argv:
            if not build_ok:
                raise subprocess.CalledProcessError(1, argv, stderr="boom: bad layer")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{version_label}\n", "")
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
        # The build is the only subprocess call (no post-build entrypoint inspection).
        build_argv = run.call_args_list[0].args[0]
        assert build_argv[:3] == ["docker", "build", "-t"]
        assert build_argv[3] == "coder-eval-task-edit-pdf:built"
        assert build_argv[4] == "-f" and build_argv[5] == str(dockerfile)
        # Build context is the Dockerfile's PARENT dir, not "." / cwd.
        assert build_argv[6] == str(dockerfile.parent)

    def test_build_failure_raises_dockerbuilderror_with_log(self, tmp_path: Path, mocker) -> None:
        """A non-zero `docker build` surfaces as DockerBuildError (a DockerRunError
        subclass) carrying the build log for docker.log capture."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect(build_ok=False))
        runner = _make_runner(dockerfile_path=str(dockerfile))
        with pytest.raises(dr.DockerBuildError, match=r"Failed to build Docker image.*boom: bad layer") as ei:
            runner._build_image()
        # still a DockerRunError (existing handlers keep working) + carries the log
        assert isinstance(ei.value, DockerRunError)
        assert "boom: bad layer" in ei.value.build_log

    def test_accepts_runtime_image_with_version_label(self, tmp_path: Path, mocker) -> None:
        """An image carrying org.coder-eval.version (FROM coder-eval-agent) passes."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM coder-eval-agent:latest\n")
        mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect(version_label="0.3.0"))
        runner = _make_runner(task_id="ok", dockerfile_path=str(dockerfile))
        assert runner._build_image() == "coder-eval-task-ok:built"

    def test_rejects_image_without_version_label(self, tmp_path: Path, mocker) -> None:
        """A non-framework image (no org.coder-eval.version label) -> actionable DockerRunError.

        The host pins --entrypoint, so the build is no longer gated on the baked
        ENTRYPOINT; the runtime-image check uses the version label instead.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM ubuntu:24.04\n")
        mocker.patch.object(dr.subprocess, "run", side_effect=_docker_side_effect(version_label=""))
        runner = _make_runner(dockerfile_path=str(dockerfile))
        with pytest.raises(DockerRunError, match=r"FROM coder-eval-agent"):
            runner._build_image()

    def test_label_inspect_failure_is_soft(self, tmp_path: Path, mocker) -> None:
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


# --------------------------------------------------------------------------- #
# Drift guard: the host's pinned --entrypoint path MUST equal the script's
# install location baked by docker/Dockerfile. A rename of either alone would
# pass lint/type-check/unit tests and fail only at `docker run`.
# --------------------------------------------------------------------------- #
def test_container_entrypoint_matches_dockerfile_copy_destination() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = (repo_root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    # Find the COPY line whose destination installs the framework entrypoint script.
    copy_dests = [
        line.split()[-1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and line.rstrip().endswith(".sh")
    ]
    assert CONTAINER_ENTRYPOINT in copy_dests, (
        f"CONTAINER_ENTRYPOINT={CONTAINER_ENTRYPOINT!r} is not a COPY destination in "
        f"docker/Dockerfile (found {copy_dests!r}); host --entrypoint and the baked "
        "script path have drifted."
    )


def test_runtime_kit_entrypoint_matches_host_path() -> None:
    """Injected images (runtime-mode: inject) reuse the SAME host --entrypoint path.

    The converter's inject block COPYs docker/Dockerfile.runtime's entrypoint to
    CONTAINER_ENTRYPOINT, so a rename of either would break injected runs the same
    way the framework drift guard above protects the rebase path.
    """
    repo_root = Path(__file__).resolve().parents[1]
    runtime_df = repo_root / "docker" / "Dockerfile.runtime"
    if not runtime_df.is_file():  # runtime kit is optional tooling
        pytest.skip("docker/Dockerfile.runtime not present")
    text = runtime_df.read_text(encoding="utf-8")
    copy_dests = [
        line.split()[-1] for line in text.splitlines() if line.startswith("COPY ") and line.rstrip().endswith(".sh")
    ]
    assert CONTAINER_ENTRYPOINT in copy_dests, (
        f"CONTAINER_ENTRYPOINT={CONTAINER_ENTRYPOINT!r} is not a COPY destination in "
        f"docker/Dockerfile.runtime (found {copy_dests!r}); the host --entrypoint and the "
        "injected runtime-kit entrypoint path have drifted."
    )


def _runtime_dockerfile() -> Path | None:
    df = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.runtime"
    return df if df.is_file() else None


def test_runtime_kit_stamps_version_label() -> None:
    """The kit Dockerfile must stamp `org.coder-eval.version`.

    The host's :meth:`_assert_runtime_image` rejects an image lacking that label;
    the converter re-declares it on the *injected* image (guarded converter-side),
    and the kit carries it too for `docker inspect`/parity. Static guard so
    dropping the LABEL fails here, not only against a freshly-built image.
    """
    runtime_df = _runtime_dockerfile()
    if runtime_df is None:
        pytest.skip("docker/Dockerfile.runtime not present")
    assert any(
        ln.startswith("LABEL org.coder-eval.version") for ln in runtime_df.read_text(encoding="utf-8").splitlines()
    ), "docker/Dockerfile.runtime must stamp `LABEL org.coder-eval.version=…`."


def test_runtime_kit_path_matches_install_dirs() -> None:
    """The entrypoint's PATH dirs must match where the kit installs its tools.

    A rename of the venv/node dir in Dockerfile.runtime (or the PATH entry in the
    entrypoint) would pass lint/type/unit and fail only at `docker run` with
    `coder-eval: command not found`. Guard the source leg of the kit contract
    (the destination leg is covered above).
    """
    runtime_df = _runtime_dockerfile()
    entry = Path(__file__).resolve().parents[1] / "docker" / "coder_eval_runtime_entrypoint.sh"
    if runtime_df is None or not entry.is_file():
        pytest.skip("runtime kit files not present")
    df_text = runtime_df.read_text(encoding="utf-8")
    path_line = next(
        ln for ln in entry.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("export PATH=")
    )
    kit_dirs = re.findall(r"/opt/coder-eval[\w./-]*", path_line)
    assert kit_dirs, "entrypoint sets no /opt/coder-eval PATH dirs?"
    for d in kit_dirs:
        install_root = d.rsplit("/bin", 1)[0]  # /opt/coder-eval/venv/bin -> /opt/coder-eval/venv
        assert install_root in df_text, (
            f"entrypoint PATH dir {d!r} (install root {install_root!r}) is not an install target in "
            "docker/Dockerfile.runtime — the kit's PATH and install dirs have drifted."
        )


def test_claude_code_version_pin_matches_framework() -> None:
    """The Claude Code pin must be identical in the framework and runtime-kit
    Dockerfiles — else inject-mode tasks run a different agent version than
    rebase-mode tasks (a reproducibility/fairness drift, since the agent binary
    is a dominant non-model driver of results)."""
    root = Path(__file__).resolve().parents[1]
    runtime_df = _runtime_dockerfile()
    if runtime_df is None:
        pytest.skip("docker/Dockerfile.runtime not present")

    def _pin(df: Path) -> str | None:
        for ln in df.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*ARG CLAUDE_CODE_VERSION=(\S+)", ln)
            if m:
                return m.group(1)
        return None

    fw, kit = _pin(root / "docker" / "Dockerfile"), _pin(runtime_df)
    assert fw and kit, f"CLAUDE_CODE_VERSION ARG missing (framework={fw!r}, kit={kit!r})"
    assert fw == kit, (
        f"CLAUDE_CODE_VERSION drift: docker/Dockerfile pins {fw!r} but docker/Dockerfile.runtime pins {kit!r} — "
        "inject and rebase tasks would run different Claude Code versions."
    )
