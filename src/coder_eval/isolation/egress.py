"""Host side of ``network: llm_only``: the egress allowlist and the per-task proxy sidecar.

Imports only :mod:`coder_eval.isolation.errors` and :mod:`coder_eval.models`, so
``docker_runner`` can import it without a cycle.

Rationale: .claude/notes/isolation.md § The egress sidecar (network: llm_only)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from coder_eval.isolation.errors import EgressSetupError
from coder_eval.models import ApiBackend, SystemOneJudgeCriterion, normalize_egress_target


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
    from pathlib import Path

    from coder_eval.config import Settings
    from coder_eval.models import TaskDefinition


logger = logging.getLogger(__name__)

# Docker Desktop's stable host alias from a bridge-network container. Auto-resolves
# on macOS/Windows; on Linux it must be published via `--add-host`.
_DOCKER_HOST_ALIAS = "host.docker.internal"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_DIRECT_TARGETS = ("api.anthropic.com:443", "platform.claude.com:443")


def _rewrite_loopback_for_container(url: str) -> str | None:
    """Rewrite a loopback URL to the docker host alias, preserving scheme/port/path.

    Returns the rewritten URL, or None if the host is not loopback (forward as-is).
    A LiteLLM proxy on the HOST is unreachable at localhost from inside a bridge
    container, so ``http://localhost:4000`` -> ``http://host.docker.internal:4000``.
    """
    parts = urlsplit(url)
    if parts.hostname not in _LOOPBACK_HOSTS:
        return None
    netloc = _DOCKER_HOST_ALIAS if parts.port is None else f"{_DOCKER_HOST_ALIAS}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def forwarded_env_names(task: TaskDefinition) -> set[str]:
    """Names of the host variables the container receives: ``env_passthrough`` plus ``env_passthrough_extra``."""
    docker = task.sandbox.docker
    return set(docker.env_passthrough) | set(docker.env_passthrough_extra)


def _url_target(source: str, url: str, *, rewrite_loopback: bool = False) -> str | None:
    """``host:port`` of an ``http(s)`` URL with a host, else None.

    ``source`` names the URL in errors and warnings; the URL itself is never echoed,
    because it may carry credentials.
    """
    try:
        if rewrite_loopback:
            url = _rewrite_loopback_for_container(url) or url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.hostname in _LOOPBACK_HOSTS:
            logger.warning(
                "%s points at a loopback host, which the egress sidecar cannot reach; it is not allowlisted.", source
            )
            return None
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        return normalize_egress_target(f"{parts.hostname}:{port}")
    except ValueError:
        raise ValueError(f"{source} has a host or port that network: llm_only cannot allowlist.") from None


def _backend_targets(settings: Settings) -> list[str]:
    if settings.api_backend == ApiBackend.DIRECT:
        return list(_DIRECT_TARGETS)
    if settings.api_backend == ApiBackend.BEDROCK:
        region = settings.aws_region
        if not region:
            logger.warning("api_backend is bedrock but AWS_REGION is unset: no Bedrock host is allowlisted.")
            return []
        try:
            return [
                normalize_egress_target(f"bedrock-runtime.{region}.amazonaws.com"),
                normalize_egress_target(f"bedrock.{region}.amazonaws.com"),
            ]
        except ValueError:
            raise ValueError("AWS_REGION is not a valid region name for a Bedrock host.") from None
    return []


def resolve_egress_targets(task: TaskDefinition, *, env: Mapping[str, str], settings: Settings) -> list[str]:
    """Every ``host:port`` the task's container may reach under ``network: llm_only``.

    The union of the API backend's hosts, the host of every forwarded ``*_URL``
    variable, the agent config's ``egress_hosts``, each ``system_one_judge``
    ``base_url``, and ``sandbox.docker.egress_allowlist``. ``env`` is the host
    environment; only the variables the container receives count. Pure: reads only
    its arguments. Sorted and de-duplicated.

    Raises:
        ValueError: A forwarded URL, a judge ``base_url`` or ``AWS_REGION`` names a host
            that cannot be allowlisted.
    """
    forwarded = {name: env[name] for name in forwarded_env_names(task) if env.get(name)}
    targets = set(_backend_targets(settings))
    for name in sorted(forwarded):
        if name.endswith("_URL"):
            target = _url_target(name, forwarded[name], rewrite_loopback=name == "LITELLM_BASE_URL")
            if target is not None:
                targets.add(target)
    if task.agent is not None:
        targets.update(task.agent.egress_hosts(forwarded))
    for criterion in task.success_criteria:
        if isinstance(criterion, SystemOneJudgeCriterion):
            target = _url_target("system_one_judge base_url", criterion.base_url)
            if target is not None:
                targets.add(target)
    targets.update(task.sandbox.docker.egress_allowlist)
    return sorted(targets)


EGRESS_PROXY_MODULE = "egress_proxy.py"
EGRESS_PROXY_ALIAS = "coder-eval-egress"
EGRESS_PROXY_PORT = 3128
EGRESS_LABEL = "org.coder-eval.egress"
SIDECAR_EGRESS_DIR = "/work/egress"
SIDECAR_HEARTBEAT = "/work/heartbeat"
FALLBACK_SIDECAR_IMAGE = "coder-eval-agent:latest"
PROXY_URL = f"http://{EGRESS_PROXY_ALIAS}:{EGRESS_PROXY_PORT}"
NO_PROXY_HOSTS = "localhost,127.0.0.1,::1"
# Never forwarded from the host under llm_only: a host value would shadow the sidecar's.
PROXY_ENV_NAMES = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "NODE_USE_ENV_PROXY",
    }
)
# TEST-NET-1 (RFC 5737): never routed. As the task container's upstream resolver it keeps
# Docker's embedded DNS answering the sidecar alias while external lookups go nowhere, even on
# a daemon that forwards internal-network queries from the host namespace (CVE-2024-29018).
BLACKHOLE_DNS = "192.0.2.1"
EGRESS_LOG_HEADER = "=== egress proxy (network: llm_only) ==="
PRUNE_HINT = (
    f"docker rm -f $(docker ps -aq --filter label={EGRESS_LABEL}); "
    + f"docker network prune -f --filter label={EGRESS_LABEL}"
)

_POOL_EXHAUSTED = "all predefined address pools have been fully subnetted"
_DOCKER_TIMEOUT_SECONDS = 30.0
_PROBE_TIMEOUT_SECONDS = 5
_NETWORK_RM_ATTEMPTS = 5
_NETWORK_RM_RETRY_SECONDS = 1.0


@dataclass(frozen=True)
class EgressHandle:
    """The per-task internal network and proxy sidecar a ``network: llm_only`` container joins."""

    network: str
    sidecar: str
    targets: tuple[str, ...]


def task_container_egress_argv() -> list[str]:
    """``docker run`` arguments the task container gets under ``network: llm_only``.

    A black-hole upstream resolver, and explicit (non-secret) ``--env`` pairs: the proxy
    variables point every tool at the sidecar, and ``LITELLM_LOCAL_MODEL_COST_MAP`` stops
    litellm fetching its cost map from a host that is not allowlisted.
    """
    argv: list[str] = ["--dns", BLACKHOLE_DNS]
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        argv += ["--env", f"{name}={PROXY_URL}"]
    for name in ("NO_PROXY", "no_proxy"):
        argv += ["--env", f"{name}={NO_PROXY_HOSTS}"]
    return [*argv, "--env", "NODE_USE_ENV_PROXY=1", "--env", "LITELLM_LOCAL_MODEL_COST_MAP=True"]


def build_network_create_argv(network: str) -> list[str]:
    """``docker`` arguments that create the per-task internal network with no host-reachable gateway."""
    return [
        "network",
        "create",
        "--internal",
        "--ipv6=false",
        "-o",
        "com.docker.network.bridge.inhibit_ipv4=true",
        "--label",
        f"{EGRESS_LABEL}=1",
        network,
    ]


def build_sidecar_create_argv(
    *,
    sidecar: str,
    network: str,
    image: str,
    egress_dir: Path,
    heartbeat: Path,
    stale_seconds: float,
    targets: Sequence[str],
) -> list[str]:
    """``docker`` arguments that create (not start) the proxy sidecar. Pure."""
    argv = [
        "create",
        "--name",
        sidecar,
        "--label",
        f"{EGRESS_LABEL}=1",
        "--network",
        network,
        "--network-alias",
        EGRESS_PROXY_ALIAS,
        "--add-host",
        f"{_DOCKER_HOST_ALIAS}:host-gateway",
        "--sysctl",
        "net.ipv4.ip_forward=0",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--memory",
        "256m",
        "--pids-limit",
        "256",
        "-v",
        f"{egress_dir}:{SIDECAR_EGRESS_DIR}:ro",
        "-v",
        f"{heartbeat}:{SIDECAR_HEARTBEAT}:ro",
        "--entrypoint",
        "python3",
        image,
        "-I",
        f"{SIDECAR_EGRESS_DIR}/{EGRESS_PROXY_MODULE}",
        "serve",
        "--listen",
        f"0.0.0.0:{EGRESS_PROXY_PORT}",
        "--heartbeat",
        SIDECAR_HEARTBEAT,
        "--stale",
        f"{stale_seconds:g}",
    ]
    for target in targets:
        argv += ["--allow", target]
    return argv


def build_probe_argv(sidecar: str, targets: Sequence[str]) -> list[str]:
    """``docker`` arguments that CONNECT to every target through the running sidecar."""
    return [
        "exec",
        sidecar,
        "python3",
        "-I",
        f"{SIDECAR_EGRESS_DIR}/{EGRESS_PROXY_MODULE}",
        "probe",
        "--proxy",
        f"{EGRESS_PROXY_ALIAS}:{EGRESS_PROXY_PORT}",
        "--timeout",
        str(_PROBE_TIMEOUT_SECONDS),
        *targets,
    ]


def _docker_sync(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )


async def _docker(*args: str, timeout: float = _DOCKER_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """Run one ``docker`` command to its end, even when the caller is cancelled meanwhile.

    A cancelled worker thread keeps running, so a cancel that did not wait for it
    would let teardown race a ``docker create`` still in flight and leak its container.
    """
    return await _run_to_completion(asyncio.to_thread(_docker_sync, args, timeout))


async def _checked(what: str, *args: str, timeout: float = _DOCKER_TIMEOUT_SECONDS) -> str:
    """Run one setup step; return stdout, or raise EgressSetupError naming the step."""
    try:
        result = await _docker(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EgressSetupError(f"network: llm_only could not {what}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if _POOL_EXHAUSTED in detail:
            raise EgressSetupError(
                "network: llm_only could not create its per-task network: Docker has no free address pool "
                + f"({_POOL_EXHAUSTED}). Lower --max-parallel, remove leaked networks with "
                + f"`docker network prune --filter label={EGRESS_LABEL}`, or widen the daemon's "
                + "`default-address-pools` setting."
            )
        raise EgressSetupError(f"network: llm_only could not {what}: {detail}")
    return result.stdout


async def _resolve_sidecar_image(image: str, *, allow_image_skew: bool) -> str:
    candidates = [image, FALLBACK_SIDECAR_IMAGE] if allow_image_skew and image != FALLBACK_SIDECAR_IMAGE else [image]
    last_error: EgressSetupError | None = None
    for candidate in candidates:
        try:
            await _checked(f"inspect image {candidate}", "image", "inspect", "--format", "{{.Id}}", candidate)
            return candidate
        except EgressSetupError as exc:
            last_error = exc
    raise EgressSetupError(
        f"network: llm_only needs the framework image {image} for its egress sidecar. Run `make docker-image`. "
        + f"({last_error})"
    )


async def _probe(sidecar: str, targets: Sequence[str]) -> None:
    try:
        result = await _docker(*build_probe_argv(sidecar, targets))
    except (OSError, subprocess.SubprocessError) as exc:
        raise EgressSetupError(f"network: llm_only egress probe could not run: {exc}") from exc
    if result.returncode == 0:
        return
    failing = [line.removeprefix("FAIL ") for line in result.stdout.splitlines() if line.startswith("FAIL ")]
    detail = "; ".join(failing) or (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
    raise EgressSetupError(
        f"network: llm_only egress probe failed: {detail}. Check that this host can reach these targets, add "
        + "a missing host with sandbox.docker.egress_allowlist, and note that chaining to an upstream "
        + "(corporate) proxy is not supported."
    )


def _append_sidecar_log(log_path: Path, text: str) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{EGRESS_LOG_HEADER}\n{text}")
        if text and not text.endswith("\n"):
            handle.write("\n")


async def _best_effort(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return await _docker(*args)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("docker %s failed during egress teardown: %s", " ".join(args[:2]), exc)
        return None


async def _teardown(*, network: str | None, sidecar: str | None, log_path: Path) -> None:
    if sidecar is not None:
        logs = await _best_effort("logs", sidecar)
        if logs is not None and logs.returncode == 0:
            try:
                await asyncio.to_thread(_append_sidecar_log, log_path, logs.stdout + logs.stderr)
            except OSError as exc:
                logger.warning("Could not append the egress proxy log to %s: %s", log_path, exc)
        await _best_effort("rm", "-f", sidecar)
    if network is None:
        return
    for attempt in range(1, _NETWORK_RM_ATTEMPTS + 1):
        removed = await _best_effort("network", "rm", network)
        if removed is not None and removed.returncode == 0:
            return
        if attempt < _NETWORK_RM_ATTEMPTS:
            await asyncio.sleep(_NETWORK_RM_RETRY_SECONDS)
    logger.warning("Could not remove egress network %s; remove leaked egress resources with: %s", network, PRUNE_HINT)


async def _run_to_completion[T](awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` to its end across any number of cancels of the caller, then re-raise the cancel."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("A docker step failed while its caller was cancelled: %s", task.exception())
        raise asyncio.CancelledError
    return task.result()


@contextlib.asynccontextmanager
async def egress_scope(
    *,
    container_name: str,
    image: str,
    egress_dir: Path,
    heartbeat: Path,
    stale_seconds: float,
    targets: Sequence[str],
    log_path: Path,
    allow_image_skew: bool = False,
) -> AsyncIterator[EgressHandle]:
    """Create the internal network and proxy sidecar, probe every target, yield, then tear both down.

    ``egress_dir`` holds the proxy module and ``heartbeat`` must already exist on the
    host. Teardown runs on every exit path and appends the sidecar log to ``log_path``.

    Raises:
        EgressSetupError: Any setup step (image, network, sidecar, probe) failed.
    """
    network = f"{container_name}-net"
    sidecar = f"{container_name}-egress"
    created_network: str | None = None
    created_sidecar: str | None = None
    try:
        sidecar_image = await _resolve_sidecar_image(image, allow_image_skew=allow_image_skew)
        created_network = network
        await _checked("create its per-task network", *build_network_create_argv(network))
        created_sidecar = sidecar
        await _checked(
            "create the egress sidecar",
            *build_sidecar_create_argv(
                sidecar=sidecar,
                network=network,
                image=sidecar_image,
                egress_dir=egress_dir,
                heartbeat=heartbeat,
                stale_seconds=stale_seconds,
                targets=targets,
            ),
        )
        await _checked("attach the egress sidecar to the bridge network", "network", "connect", "bridge", sidecar)
        await _checked("start the egress sidecar", "start", sidecar)
        if targets:
            await _probe(sidecar, targets)
        else:
            logger.warning("network: llm_only with an empty allowlist: the container can reach no host at all.")
        logger.info("Egress sidecar %s on %s allows: %s", sidecar, network, ", ".join(targets) or "(nothing)")
        yield EgressHandle(network=network, sidecar=sidecar, targets=tuple(targets))
    finally:
        await _run_to_completion(_teardown(network=created_network, sidecar=created_sidecar, log_path=log_path))
