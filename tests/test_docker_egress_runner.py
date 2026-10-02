"""``network: llm_only`` in the docker runner: argv, sidecar lifecycle and teardown (no daemon)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation import egress as egress_mod
from coder_eval.isolation.docker_runner import CONTAINER_ENTRYPOINT, DockerRunError, DockerRunner
from coder_eval.isolation.egress import (
    EGRESS_LABEL,
    NO_PROXY_HOSTS,
    PROXY_URL,
    PRUNE_HINT,
    EgressHandle,
    build_network_create_argv,
    build_sidecar_create_argv,
    egress_scope,
)
from coder_eval.isolation.errors import EgressSetupError
from coder_eval.models import (
    CONTAINER_INPUT_DIR,
    CONTAINER_OUTPUT_DIR,
    IN_CONTAINER_ENV,
    ApiBackend,
    DockerDriverConfig,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.regrade import _container_dispatch_commands
from coder_eval.path_utils import TASK_JSON_FILENAME


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")

SETUP = ["image inspect", "network create", "create --name", "network connect", "start c-egress", "exec c-egress"]
TEARDOWN = ["logs c-egress", "rm -f", "network rm"]


def _task(**docker: object) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type="claude-code"),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(**docker)),  # type: ignore[arg-type]
        success_criteria=[FileExistsCriterion(description="c", path="x.txt")],
    )


def _runner(**docker: object) -> DockerRunner:
    rt = MagicMock()
    rt.task = _task(**docker)
    rt.run_dir = Path(tempfile.gettempdir()) / "test_run_egress"
    rt.task_file = None
    return DockerRunner(rt)


@pytest.fixture
def hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*DockerDriverConfig().env_passthrough, *(n for n in os.environ if n.startswith("LITELLM_"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CODER_EVAL_NO_CLAUDE_MOUNT", "1")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    input_dir, output_dir = tmp_path / "in", tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    return input_dir, output_dir


@pytest.mark.parametrize("network", ["bridge", "none"])
def test_bridge_and_none_argv_is_unchanged(hermetic_env: None, dirs: tuple[Path, Path], network: str) -> None:
    input_dir, output_dir = dirs
    runner = _runner(network=network, env_passthrough=["ANTHROPIC_API_KEY"], image="img:1")
    argv = runner._build_argv(input_dir, output_dir, container_name="c")
    assert argv == [
        *["docker", "run", "--rm", "--name", "c", "--entrypoint", CONTAINER_ENTRYPOINT],
        *["--cap-drop", "DAC_OVERRIDE", "--cap-drop", "DAC_READ_SEARCH", "--network", network],
        *["--env", "ANTHROPIC_API_KEY", "--env", f"{IN_CONTAINER_ENV}=1", "--env", "TELEMETRY_ENABLED=false"],
        *["-v", f"{input_dir.resolve()}:{CONTAINER_INPUT_DIR}", "-v", f"{output_dir}:{CONTAINER_OUTPUT_DIR}"],
        *["img:1", "--output", CONTAINER_OUTPUT_DIR],
    ]


def test_llm_only_joins_only_the_internal_network_with_explicit_proxy_env(
    hermetic_env: None, dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000")
    runner = _runner(
        network="llm_only",
        env_passthrough=["ANTHROPIC_API_KEY", "LITELLM_BASE_URL"],
        env_passthrough_extra=["HTTPS_PROXY"],
    )
    handle = EgressHandle(network="c-net", sidecar="c-egress", targets=("api.anthropic.com:443",))
    argv = runner._build_argv(*dirs, container_name="c", egress=handle)
    assert argv.count("--network") == 1
    assert argv[argv.index("--network") + 1] == "c-net"
    assert argv[argv.index("--dns") + 1] == "192.0.2.1"
    assert "--add-host" not in argv
    env = [argv[i + 1] for i, token in enumerate(argv) if token == "--env"]
    proxy_env = [f"{name}={PROXY_URL}" for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")]
    assert set(proxy_env) | {f"NO_PROXY={NO_PROXY_HOSTS}", f"no_proxy={NO_PROXY_HOSTS}"} <= set(env)
    assert "LITELLM_BASE_URL=http://host.docker.internal:4000" in env
    assert "HTTPS_PROXY" not in env, "a host proxy must not be forwarded name-only"


def test_llm_only_without_a_handle_refuses_to_fall_back_to_bridge(hermetic_env: None, dirs: tuple[Path, Path]) -> None:
    with pytest.raises(DockerRunError, match="refusing to fall back to bridge"):
        _runner(network="llm_only")._build_argv(*dirs, container_name="c")


def test_network_and_sidecar_are_locked_down(tmp_path: Path) -> None:
    network = build_network_create_argv("n")
    assert {"--internal", "--ipv6=false", "com.docker.network.bridge.inhibit_ipv4=true"} <= set(network)
    sidecar = build_sidecar_create_argv(
        sidecar="s",
        network="n",
        image="img",
        egress_dir=tmp_path / "egress",
        heartbeat=tmp_path / "hb",
        stale_seconds=20,
        targets=["a.example:443", "b.example:80"],
    )
    joined = " ".join(sidecar)
    for flag in ("--sysctl net.ipv4.ip_forward=0", "--user 65534:65534", "--cap-drop ALL", "--read-only"):
        assert flag in joined
    assert "--security-opt no-new-privileges" in joined
    assert f"-v {tmp_path / 'egress'}:/work/egress:ro" in joined
    assert [sidecar[i + 1] for i, t in enumerate(sidecar) if t == "--allow"] == ["a.example:443", "b.example:80"]


class _FakeDocker:
    """Stands in for ``egress._docker``: records each call and answers by its first arguments."""

    def __init__(self, answers: dict[str, list[subprocess.CompletedProcess[str] | BaseException]] | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.answers = answers or {}

    async def __call__(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        queue = self.answers.get(" ".join(args[:2]))
        if queue:
            answer = queue.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        stdout = "egress log line\n" if args[0] == "logs" else ""
        return subprocess.CompletedProcess(["docker", *args], 0, stdout, "")

    def verbs(self) -> list[str]:
        return [" ".join(call[:2]) for call in self.calls]


def _failed(stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], 1, stdout, stderr)


def _scope(tmp_path: Path) -> contextlib.AbstractAsyncContextManager[EgressHandle]:
    return egress_scope(
        container_name="c",
        image="coder-eval-agent:x",
        egress_dir=tmp_path,
        heartbeat=tmp_path / "hb",
        stale_seconds=20,
        targets=["api.anthropic.com:443"],
        log_path=tmp_path / "egress.log",
    )


async def _enter_scope(tmp_path: Path, fake: _FakeDocker, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(egress_mod, "_docker", fake)
    monkeypatch.setattr(egress_mod, "_NETWORK_RM_RETRY_SECONDS", 0)
    async with _scope(tmp_path) as handle:
        assert handle == EgressHandle(network="c-net", sidecar="c-egress", targets=("api.anthropic.com:443",))
    return tmp_path / "egress.log"


async def _cancelled(task: asyncio.Task[None]) -> bool:
    try:
        await task
    except asyncio.CancelledError:
        return True
    return False


async def test_scope_order_and_a_planted_log_symlink_is_replaced_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "victim_rc"
    victim.write_text("original\n", encoding="utf-8")
    (tmp_path / "egress.log").symlink_to(victim)
    fake = _FakeDocker()
    log_path = await _enter_scope(tmp_path, fake, monkeypatch)
    assert fake.verbs() == [*SETUP, *TEARDOWN]
    assert victim.read_text(encoding="utf-8") == "original\n"
    assert not log_path.is_symlink()
    assert log_path.read_text(encoding="utf-8") == "egress log line\n"


PROBE_502 = _failed(stdout="FAIL api.anthropic.com:443 HTTP/1.1 502\n")
NO_IMAGE = _failed("No such image")


@pytest.mark.parametrize(
    ("step", "answer", "raised", "match", "verbs"),
    [
        ("exec c-egress", PROBE_502, EgressSetupError, r"502.*allowlist", [*SETUP, *TEARDOWN]),
        ("exec c-egress", asyncio.CancelledError(), asyncio.CancelledError, None, [*SETUP, *TEARDOWN]),
        ("image inspect", NO_IMAGE, EgressSetupError, r"coder-eval-agent:x .*make docker-image", SETUP[:1]),
    ],
    ids=["probe-failure", "cancel-during-probe", "missing-framework-image"],
)
async def test_a_failed_setup_tears_down_what_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, answer: Any, raised: Any, match: Any, verbs: list[str]
) -> None:
    fake = _FakeDocker({step: [answer]})
    with pytest.raises(raised, match=match):
        await _enter_scope(tmp_path, fake, monkeypatch)
    assert fake.verbs() == verbs


async def test_repeated_cancels_during_teardown_still_finish_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = _FakeDocker()
    release, in_teardown, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def _slow_logs(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        if args[0] == "logs":
            in_teardown.set()
            await release.wait()
        return await fake(*args, timeout=timeout)

    monkeypatch.setattr(egress_mod, "_docker", _slow_logs)

    async def _body() -> None:
        async with _scope(tmp_path):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(_body())
    await entered.wait()
    task.cancel()
    await in_teardown.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    assert await _cancelled(task)
    assert fake.verbs()[-3:] == TEARDOWN


async def test_a_cancel_during_docker_create_waits_for_it_before_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    create_started, release_create = threading.Event(), threading.Event()

    def _sync(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if args[0] == "create":
            create_started.set()
            release_create.wait(10)
            order.append("create finished")
        else:
            order.append(" ".join(args[:2]))
        return subprocess.CompletedProcess(["docker", *args], 0, "", "")

    monkeypatch.setattr(egress_mod, "_docker_sync", _sync)
    monkeypatch.setattr(egress_mod, "_NETWORK_RM_RETRY_SECONDS", 0)

    async def _body() -> None:
        async with _scope(tmp_path):
            raise AssertionError("setup must not complete")

    task = asyncio.create_task(_body())
    await asyncio.to_thread(create_started.wait, 10)
    task.cancel()
    await asyncio.sleep(0.05)
    release_create.set()
    assert await _cancelled(task)
    assert order.index("create finished") < order.index("rm -f")
    assert order[-1] == "network rm"


@pytest.mark.parametrize(("failures", "attempts", "gives_up"), [(2, 3, False), (5, 5, True)])
async def test_network_rm_retries_then_gives_up_with_the_prune_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failures, attempts, gives_up
) -> None:
    fake = _FakeDocker({"network rm": [_failed("network c-net has active endpoints")] * failures})
    with caplog.at_level(logging.WARNING, logger=egress_mod.logger.name):
        await _enter_scope(tmp_path, fake, monkeypatch)
    assert fake.verbs().count("network rm") == attempts
    assert (PRUNE_HINT in caplog.text) is gives_up


async def test_a_network_that_was_never_created_is_not_reported_as_leaked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeDocker(
        {
            "network create": [_failed("all predefined address pools have been fully subnetted")],
            "network rm": [_failed("Error response from daemon: network c-net not found")],
        }
    )
    with (
        caplog.at_level(logging.WARNING, logger=egress_mod.logger.name),
        pytest.raises(EgressSetupError, match="--max-parallel") as excinfo,
    ):
        await _enter_scope(tmp_path, fake, monkeypatch)
    assert f"docker network prune --filter label={EGRESS_LABEL}" in str(excinfo.value)
    assert fake.verbs().count("network rm") == 1
    assert "Could not remove" not in caplog.text


async def test_teardown_survives_a_docker_cli_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDocker({"logs c-egress": [OSError("docker vanished")], "rm -f": [subprocess.TimeoutExpired("d", 30)]})
    await _enter_scope(tmp_path, fake, monkeypatch)
    assert fake.verbs()[-1] == "network rm"


def _rt_runner(tmp_path: Path, network: str) -> DockerRunner:
    runner = _runner(network=network, env_passthrough=["ANTHROPIC_API_KEY"])
    runner.rt.run_dir = tmp_path / "run"
    runner.rt.replicate_index = 0
    runner.rt.variant_id = "default"
    runner.rt.config_lineage = {}
    runner.rt.source_yaml = "# task"
    return runner


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    monkeypatch.setattr(dr, "_preflight", lambda: None)
    monkeypatch.setattr(dr, "_preflight_image_contract", lambda image, dockerfile: None)
    seen: list[tuple[object, ...]] = []

    async def _fake_exec(*argv: object, **kwargs: object) -> None:
        seen.append(argv)
        raise FileNotFoundError("docker run is not under test")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    return seen


async def test_llm_only_runs_the_container_inside_the_scope(
    hermetic_env: None, launches: list[tuple[object, ...]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dr.settings, "api_backend", ApiBackend.DIRECT)
    seen: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def _fake_scope(**kwargs: Any):
        seen["targets"] = kwargs["targets"]
        seen["staged"] = (kwargs["egress_dir"] / "egress_proxy.py").is_file()
        seen["heartbeat"] = Path(kwargs["heartbeat"]).is_file()
        try:
            yield EgressHandle(network="c-net", sidecar="c-egress", targets=tuple(kwargs["targets"]))
        finally:
            seen["exited"] = True

    monkeypatch.setattr(dr, "egress_scope", _fake_scope)
    with pytest.raises(FileNotFoundError):
        await _rt_runner(tmp_path, "llm_only").run()
    assert seen.pop("targets") == ["api.anthropic.com:443", "platform.claude.com:443"]
    assert seen == {"staged": True, "heartbeat": True, "exited": True}
    assert launches[0][launches[0].index("--network") + 1] == "c-net"


@pytest.mark.parametrize(("cause", "match"), [("probe", "probe failed"), ("region", "AWS_REGION")])
async def test_setup_failure_writes_a_synthetic_error_row(
    hermetic_env: None,
    launches: list[tuple[object, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cause,
    match,
) -> None:
    @contextlib.asynccontextmanager
    async def _failing_scope(**kwargs: object):
        raise EgressSetupError("network: llm_only egress probe failed: x")
        yield None

    if cause == "probe":
        monkeypatch.setattr(dr, "egress_scope", _failing_scope)
    else:
        monkeypatch.setattr(dr.settings, "api_backend", ApiBackend.BEDROCK)
        monkeypatch.setattr(dr.settings, "aws_region", "not a region")
        monkeypatch.setattr(dr.settings, "aws_bearer_token_bedrock", "tok")
    runner = _rt_runner(tmp_path, "llm_only")
    with pytest.raises(EgressSetupError, match=match):
        await runner.run()
    assert launches == []
    row = EvaluationResult.model_validate_json((runner.rt.run_dir / TASK_JSON_FILENAME).read_text(encoding="utf-8"))
    assert row.final_status == FinalStatus.ERROR
    assert match in (row.error_message or "")


@pytest.mark.parametrize("docker", [{"network": "bridge", "egress_allowlist": ["pypi.org"]}, {"network": "none"}])
def test_bridge_and_none_grading_disclosure_is_unchanged(docker: dict[str, Any]) -> None:
    assert _container_dispatch_commands(_task(image="img:1", **docker), None) == [
        "docker run img:1 (with your credentials in its environment and a writable copy of ~/.claude)"
    ]


def test_llm_only_grading_disclosure_names_the_mode_and_the_allowlist() -> None:
    [text] = _container_dispatch_commands(_task(image="img:1", network="llm_only", egress_allowlist=["pypi.org"]), None)
    assert "--network llm_only" in text
    assert "(egress allowed to: pypi.org:443 plus the derived model-API hosts)" in text
    [bare] = _container_dispatch_commands(_task(image="img:1", network="llm_only"), None)
    assert "--network llm_only" in bare
    assert "egress allowed to" not in bare
