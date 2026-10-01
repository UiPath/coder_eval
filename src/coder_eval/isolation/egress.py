"""Host side of ``network: llm_only``: the egress allowlist the sidecar proxy enforces.

Imports only :mod:`coder_eval.isolation.errors` and :mod:`coder_eval.models`, so
``docker_runner`` can import it without a cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from coder_eval.models import ApiBackend, SystemOneJudgeCriterion, normalize_egress_target


if TYPE_CHECKING:
    from collections.abc import Mapping

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
