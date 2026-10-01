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
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation import docker_runner as dr
from coder_eval.isolation import egress as egress_mod
from coder_eval.isolation.docker_runner import CONTAINER_ENTRYPOINT, DockerRunError, DockerRunner
from coder_eval.isolation.egress import (
    EGRESS_LABEL,
    EGRESS_LOG_HEADER,
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
from coder_eval.utils import get_default_docker_image_tag


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


def _runner(**docker: object) -> DockerRunner:
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type="claude-code"),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(**docker)),  # type: ignore[arg-type]
        success_criteria=[FileExistsCriterion(description="c", path="x.txt")],
    )
    rt = MagicMock()
    rt.task = task
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


class TestBridgeAndNoneArgvCharacterization:
    """The argv for ``bridge`` and ``none`` is pinned byte for byte."""

    @pytest.mark.parametrize("network", ["bridge", "none"])
    def test_argv_is_unchanged(self, hermetic_env: None, dirs: tuple[Path, Path], network: str) -> None:
        input_dir, output_dir = dirs
        runner = _runner(network=network, env_passthrough=["ANTHROPIC_API_KEY"], image="img:1")
        argv = runner._build_argv(input_dir, output_dir, container_name="c")
        assert argv == [
            "docker",
            "run",
            "--rm",
            "--name",
            "c",
            "--entrypoint",
            CONTAINER_ENTRYPOINT,
            "--cap-drop",
            "DAC_OVERRIDE",
            "--cap-drop",
            "DAC_READ_SEARCH",
            "--network",
            network,
            "--env",
            "ANTHROPIC_API_KEY",
            "--env",
            f"{IN_CONTAINER_ENV}=1",
            "--env",
            "TELEMETRY_ENABLED=false",
            "-v",
            f"{input_dir.resolve()}:{CONTAINER_INPUT_DIR}",
            "-v",
            f"{output_dir}:{CONTAINER_OUTPUT_DIR}",
            "img:1",
            "--output",
            CONTAINER_OUTPUT_DIR,
        ]


_HANDLE = EgressHandle(network="c-net", sidecar="c-egress", targets=("api.anthropic.com:443",))


def _env_pairs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == "--env"]


class TestLlmOnlyTaskArgv:
    def test_joins_only_the_internal_network_with_explicit_proxy_env(
        self, hermetic_env: None, dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
        runner = _runner(
            network="llm_only", env_passthrough=["ANTHROPIC_API_KEY"], env_passthrough_extra=["HTTPS_PROXY"]
        )
        argv = runner._build_argv(*dirs, container_name="c", egress=_HANDLE)
        assert argv[argv.index("--network") + 1] == "c-net"
        assert argv.count("--network") == 1
        assert argv[argv.index("--dns") + 1] == "192.0.2.1"
        env = _env_pairs(argv)
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            assert f"{name}={PROXY_URL}" in env
        assert f"NO_PROXY={NO_PROXY_HOSTS}" in env
        assert f"no_proxy={NO_PROXY_HOSTS}" in env
        assert "NODE_USE_ENV_PROXY=1" in env
        assert "LITELLM_LOCAL_MODEL_COST_MAP=True" in env
        assert "HTTPS_PROXY" not in env, "a host proxy must not be forwarded name-only"
        assert PROXY_URL == "http://coder-eval-egress:3128"

    def test_loopback_litellm_url_is_rewritten_without_add_host(
        self, hermetic_env: None, dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000")
        runner = _runner(network="llm_only", env_passthrough=["LITELLM_BASE_URL"])
        argv = runner._build_argv(*dirs, container_name="c", egress=_HANDLE)
        assert "LITELLM_BASE_URL=http://host.docker.internal:4000" in _env_pairs(argv)
        assert "--add-host" not in argv

    def test_without_a_handle_it_refuses_instead_of_falling_back_to_bridge(
        self, hermetic_env: None, dirs: tuple[Path, Path]
    ) -> None:
        with pytest.raises(DockerRunError, match="refusing to fall back to bridge"):
            _runner(network="llm_only")._build_argv(*dirs, container_name="c")


class TestSidecarArgv:
    def test_network_create_is_internal_without_a_host_gateway(self) -> None:
        argv = build_network_create_argv("n")
        assert argv[:2] == ["network", "create"]
        assert "--internal" in argv
        assert "--ipv6=false" in argv
        assert argv[argv.index("-o") + 1] == "com.docker.network.bridge.inhibit_ipv4=true"
        assert argv[argv.index("--label") + 1] == f"{EGRESS_LABEL}=1"
        assert argv[-1] == "n"

    def test_sidecar_create_is_locked_down_and_allows_each_target(self, tmp_path: Path) -> None:
        image = get_default_docker_image_tag()
        argv = build_sidecar_create_argv(
            sidecar="s",
            network="n",
            image=image,
            egress_dir=tmp_path / "egress",
            heartbeat=tmp_path / "hb",
            stale_seconds=20,
            targets=["a.example:443", "b.example:80"],
        )
        joined = " ".join(argv)
        assert argv[:3] == ["create", "--name", "s"]
        assert argv[argv.index("--network") + 1] == "n"
        assert "--network-alias coder-eval-egress" in joined
        assert "--sysctl net.ipv4.ip_forward=0" in joined
        assert "--user 65534:65534" in joined
        assert "--cap-drop ALL" in joined
        assert "--read-only" in argv
        assert "--security-opt no-new-privileges" in joined
        assert "--add-host host.docker.internal:host-gateway" in joined
        assert f"-v {tmp_path / 'egress'}:/work/egress:ro" in joined
        assert f"-v {tmp_path / 'hb'}:/work/heartbeat:ro" in joined
        assert argv[argv.index("--entrypoint") + 2] == image
        assert "--stale 20 " in joined
        assert [argv[i + 1] for i, t in enumerate(argv) if t == "--allow"] == ["a.example:443", "b.example:80"]
        assert "--rm" not in argv


class _FakeDocker:
    """Stands in for ``egress._docker``: records each call and answers by its first arguments."""

    def __init__(self, answers: dict[str, list[subprocess.CompletedProcess[str] | BaseException]] | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.answers = answers or {}

    async def __call__(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        key = " ".join(args[:2])
        queue = self.answers.get(key)
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


async def _enter_scope(tmp_path: Path, fake: _FakeDocker, monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> Path:
    monkeypatch.setattr(egress_mod, "_docker", fake)
    monkeypatch.setattr(egress_mod, "_NETWORK_RM_RETRY_SECONDS", 0)
    log_path = tmp_path / "docker.log"
    async with egress_scope(
        container_name="c",
        image="coder-eval-agent:x",
        egress_dir=tmp_path,
        heartbeat=tmp_path / "hb",
        stale_seconds=20,
        targets=["api.anthropic.com:443"],
        log_path=log_path,
        **kwargs,  # type: ignore[arg-type]
    ) as handle:
        assert handle == EgressHandle(network="c-net", sidecar="c-egress", targets=("api.anthropic.com:443",))
    return log_path


_HAPPY_ORDER = [
    "image inspect",
    "network create",
    "create --name",
    "network connect",
    "start c-egress",
    "exec c-egress",
    "logs c-egress",
    "rm -f",
    "network rm",
]


class TestEgressScope:
    async def test_happy_path_order_and_log_append(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker()
        (tmp_path / "docker.log").write_text("container line\n", encoding="utf-8")
        log_path = await _enter_scope(tmp_path, fake, monkeypatch)
        assert fake.verbs() == _HAPPY_ORDER
        assert fake.calls[3] == ("network", "connect", "bridge", "c-egress")
        assert fake.calls[5][-1] == "api.anthropic.com:443"
        assert log_path.read_text(encoding="utf-8") == (f"container line\n\n{EGRESS_LOG_HEADER}\negress log line\n")

    async def test_probe_failure_names_the_target_and_still_tears_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeDocker({"exec c-egress": [_failed(stdout="FAIL api.anthropic.com:443 HTTP/1.1 502 Bad Gateway\n")]})
        with pytest.raises(EgressSetupError, match=r"api\.anthropic\.com:443 HTTP/1\.1 502") as excinfo:
            await _enter_scope(tmp_path, fake, monkeypatch)
        assert isinstance(excinfo.value, DockerRunError)
        assert "egress_allowlist" in str(excinfo.value)
        assert fake.verbs()[-3:] == ["logs c-egress", "rm -f", "network rm"]

    async def test_missing_framework_image_is_a_clear_error_and_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeDocker({"image inspect": [_failed("No such image")]})
        with pytest.raises(EgressSetupError, match=r"framework image coder-eval-agent:x .*make docker-image"):
            await _enter_scope(tmp_path, fake, monkeypatch)
        assert fake.verbs() == ["image inspect"]

    async def test_image_skew_falls_back_to_latest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeDocker({"image inspect": [_failed("No such image")]})
        await _enter_scope(tmp_path, fake, monkeypatch, allow_image_skew=True)
        assert fake.calls[1][-1] == "coder-eval-agent:latest"
        create = fake.calls[3]
        assert create[create.index("--entrypoint") + 2] == "coder-eval-agent:latest"

    async def test_pool_exhaustion_maps_to_an_actionable_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stderr = "Error response from daemon: all predefined address pools have been fully subnetted"
        fake = _FakeDocker({"network create": [_failed(stderr)]})
        with pytest.raises(EgressSetupError) as excinfo:
            await _enter_scope(tmp_path, fake, monkeypatch)
        message = str(excinfo.value)
        assert "--max-parallel" in message
        assert f"docker network prune --filter label={EGRESS_LABEL}" in message
        assert "default-address-pools" in message
        assert fake.verbs()[-1] == "network rm", "a half-created network is still removed"

    async def test_cancellation_during_probe_tears_down_and_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeDocker({"exec c-egress": [asyncio.CancelledError()]})
        with pytest.raises(asyncio.CancelledError):
            await _enter_scope(tmp_path, fake, monkeypatch)
        assert fake.verbs()[-3:] == ["logs c-egress", "rm -f", "network rm"]

    async def test_a_real_task_cancel_during_the_body_still_finishes_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeDocker()
        monkeypatch.setattr(egress_mod, "_docker", fake)
        entered = asyncio.Event()

        async def _body() -> None:
            async with egress_scope(
                container_name="c",
                image="i",
                egress_dir=tmp_path,
                heartbeat=tmp_path / "hb",
                stale_seconds=20,
                targets=["a.example:443"],
                log_path=tmp_path / "docker.log",
            ):
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(_body())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.verbs()[-3:] == ["logs c-egress", "rm -f", "network rm"]

    async def test_repeated_cancels_during_teardown_still_finish_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeDocker()
        release = asyncio.Event()
        in_teardown = asyncio.Event()

        async def _slow_logs(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
            if args[0] == "logs":
                in_teardown.set()
                await release.wait()
            return await fake(*args, timeout=timeout)

        monkeypatch.setattr(egress_mod, "_docker", _slow_logs)
        entered = asyncio.Event()

        async def _body() -> None:
            async with egress_scope(
                container_name="c",
                image="i",
                egress_dir=tmp_path,
                heartbeat=tmp_path / "hb",
                stale_seconds=20,
                targets=["a.example:443"],
                log_path=tmp_path / "docker.log",
            ):
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
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.verbs()[-3:] == ["logs c-egress", "rm -f", "network rm"]

    async def test_a_cancel_during_docker_create_waits_for_it_before_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        create_started = threading.Event()
        release_create = threading.Event()

        def _sync(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            verb = " ".join(args[:2])
            if args[0] == "create":
                create_started.set()
                release_create.wait(10)
                order.append("create finished")
            else:
                order.append(verb)
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")

        monkeypatch.setattr(egress_mod, "_docker_sync", _sync)
        monkeypatch.setattr(egress_mod, "_NETWORK_RM_RETRY_SECONDS", 0)

        async def _body() -> None:
            async with egress_scope(
                container_name="c",
                image="i",
                egress_dir=tmp_path,
                heartbeat=tmp_path / "hb",
                stale_seconds=20,
                targets=["a.example:443"],
                log_path=tmp_path / "docker.log",
            ):
                raise AssertionError("setup must not complete")

        task = asyncio.create_task(_body())
        await asyncio.to_thread(create_started.wait, 10)
        task.cancel()
        await asyncio.sleep(0.05)
        release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert order.index("create finished") < order.index("rm -f")
        assert order[-1] == "network rm"

    async def test_network_rm_retries_while_endpoints_linger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        busy = _failed("error while removing network: network c-net has active endpoints")
        fake = _FakeDocker({"network rm": [busy, busy]})
        with caplog.at_level(logging.WARNING, logger=egress_mod.logger.name):
            await _enter_scope(tmp_path, fake, monkeypatch)
        assert fake.verbs().count("network rm") == 3
        assert "Could not remove" not in caplog.text

    async def test_network_rm_gives_up_with_the_prune_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = _FakeDocker({"network rm": [_failed("busy")] * 5})
        with caplog.at_level(logging.WARNING, logger=egress_mod.logger.name):
            await _enter_scope(tmp_path, fake, monkeypatch)
        assert fake.verbs().count("network rm") == 5
        assert PRUNE_HINT in caplog.text

    async def test_teardown_survives_a_docker_cli_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeDocker(
            {"logs c-egress": [OSError("docker vanished")], "rm -f": [subprocess.TimeoutExpired("d", 30)]}
        )
        await _enter_scope(tmp_path, fake, monkeypatch)
        assert fake.verbs()[-1] == "network rm"


class TestRunWiring:
    def _rt_runner(self, tmp_path: Path, network: str) -> DockerRunner:
        runner = _runner(network=network, env_passthrough=["ANTHROPIC_API_KEY"])
        rt = runner.rt
        rt.run_dir = tmp_path / "run"
        rt.replicate_index = 0
        rt.variant_id = "default"
        rt.config_lineage = {}
        rt.source_yaml = "# task"
        return runner

    def _no_daemon(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
        monkeypatch.setattr(dr, "_preflight", lambda: None)
        monkeypatch.setattr(dr, "_preflight_image_contract", lambda image, dockerfile: None)
        launches: list[tuple[object, ...]] = []

        async def _fake_exec(*argv: object, **kwargs: object) -> None:
            launches.append(argv)
            raise FileNotFoundError("docker run is not under test")

        monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
        return launches

    async def test_bridge_never_enters_the_egress_scope(
        self, hermetic_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launches = self._no_daemon(monkeypatch)

        def _boom(**kwargs: object) -> None:
            raise AssertionError("egress_scope must not run under bridge")

        monkeypatch.setattr(dr, "egress_scope", _boom)
        with pytest.raises(FileNotFoundError):
            await self._rt_runner(tmp_path, "bridge").run()
        assert len(launches) == 1
        assert "bridge" in launches[0]

    async def test_llm_only_runs_the_container_inside_the_scope(
        self, hermetic_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launches = self._no_daemon(monkeypatch)
        monkeypatch.setattr(dr.settings, "api_backend", ApiBackend.DIRECT)
        seen: dict[str, object] = {}

        @contextlib.asynccontextmanager
        async def _scope(**kwargs: object):
            seen.update(kwargs)
            egress_dir = kwargs["egress_dir"]
            assert isinstance(egress_dir, Path)
            seen["staged"] = (egress_dir / "egress_proxy.py").read_text(encoding="utf-8")
            seen["heartbeat_exists"] = Path(str(kwargs["heartbeat"])).is_file()
            try:
                yield EgressHandle(network="c-net", sidecar="c-egress", targets=tuple(kwargs["targets"]))  # type: ignore[arg-type]
            finally:
                seen["exited"] = True

        monkeypatch.setattr(dr, "egress_scope", _scope)
        with pytest.raises(FileNotFoundError):
            await self._rt_runner(tmp_path, "llm_only").run()
        assert seen["targets"] == ["api.anthropic.com:443", "platform.claude.com:443"]
        assert seen["image"] == get_default_docker_image_tag()
        assert seen["stale_seconds"] == dr.HEARTBEAT_STALE_SECONDS
        assert seen["heartbeat_exists"] is True
        assert "def main(" in str(seen["staged"])
        assert seen["exited"] is True
        argv = launches[0]
        assert argv[argv.index("--network") + 1] == "c-net"

    async def test_setup_failure_writes_a_synthetic_error_row(
        self, hermetic_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launches = self._no_daemon(monkeypatch)

        @contextlib.asynccontextmanager
        async def _scope(**kwargs: object):
            raise EgressSetupError("network: llm_only egress probe failed: x")
            yield None

        monkeypatch.setattr(dr, "egress_scope", _scope)
        runner = self._rt_runner(tmp_path, "llm_only")
        with pytest.raises(EgressSetupError):
            await runner.run()
        assert launches == []
        row = EvaluationResult.model_validate_json((runner.rt.run_dir / TASK_JSON_FILENAME).read_text(encoding="utf-8"))
        assert row.final_status == FinalStatus.ERROR
        assert "probe failed" in (row.error_message or "")

    async def test_unallowlistable_url_is_a_setup_error_row(
        self, hermetic_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launches = self._no_daemon(monkeypatch)
        monkeypatch.setattr(dr.settings, "api_backend", ApiBackend.BEDROCK)
        monkeypatch.setattr(dr.settings, "aws_region", "not a region")
        runner = self._rt_runner(tmp_path, "llm_only")
        with pytest.raises(EgressSetupError, match="AWS_REGION"):
            await runner.run()
        assert launches == []
        assert (runner.rt.run_dir / TASK_JSON_FILENAME).is_file()


class TestGradingDisclosure:
    def _task(self, **docker: object) -> TaskDefinition:
        return TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(image="img:1", **docker)),  # type: ignore[arg-type]
            success_criteria=[FileExistsCriterion(description="c", path="x.txt")],
        )

    @pytest.mark.parametrize("network", ["bridge", "none"])
    def test_bridge_and_none_text_is_unchanged(self, network: str) -> None:
        assert _container_dispatch_commands(self._task(network=network, egress_allowlist=["pypi.org"]), None) == [
            "docker run img:1 (with your credentials in its environment and a writable copy of ~/.claude)"
        ]

    def test_llm_only_names_the_mode_and_the_allowlist(self) -> None:
        [text] = _container_dispatch_commands(self._task(network="llm_only", egress_allowlist=["pypi.org"]), None)
        assert "--network llm_only" in text
        assert "(egress allowed to: pypi.org:443 plus the derived model-API hosts)" in text

    def test_llm_only_without_allowlist_names_only_the_mode(self) -> None:
        [text] = _container_dispatch_commands(self._task(network="llm_only"), None)
        assert "--network llm_only" in text
        assert "egress allowed to" not in text
