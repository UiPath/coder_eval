"""`network: llm_only` and `egress_allowlist` on DockerDriverConfig: values, normalization, rejection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import DockerDriverConfig, normalize_egress_target


def test_network_modes_and_allowlist_defaults():
    assert DockerDriverConfig().network == "bridge"
    assert DockerDriverConfig().egress_allowlist == []
    assert DockerDriverConfig(network="llm_only").network == "llm_only"
    with pytest.raises(ValidationError):
        DockerDriverConfig(network="internal")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("  API.Anthropic.com  ", "api.anthropic.com:443"),
        ("pypi.org:443", "pypi.org:443"),
        ("host.docker.internal:4000", "host.docker.internal:4000"),
        ("10.0.0.5:8080", "10.0.0.5:8080"),
        ("example.com:080", "example.com:80"),
    ],
)
def test_entries_normalize_to_host_port(entry: str, expected: str):
    assert normalize_egress_target(entry) == expected
    assert DockerDriverConfig(egress_allowlist=[entry]).egress_allowlist == [expected]


@pytest.mark.parametrize(
    "entry",
    [
        "https://pypi.org",
        "pypi.org/simple",
        "*.googleapis.com",
        "pypi.org:0",
        "pypi.org:70000",
        "pypi.org:",
        "",
        "[::1]:443",
        "-bad.example.com",
        "user@pypi.org",
        "evil.com\n:443",
        "\u212aube.io",
    ],
)
def test_invalid_entries_raise_naming_the_entry(entry: str):
    with pytest.raises(ValidationError, match="egress_allowlist entry"):
        DockerDriverConfig(egress_allowlist=[entry])


async def test_llm_only_without_the_docker_driver_is_refused_before_any_agent_runs(tmp_path):
    from unittest.mock import patch

    from coder_eval.models import FinalStatus, ResolvedTask, TaskDefinition
    from coder_eval.orchestration.batch import run_batch
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestrator import Orchestrator

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent={"type": "claude-code"},
        sandbox={"driver": "tempdir", "docker": {"network": "llm_only"}},
        success_criteria=[{"type": "file_exists", "path": "x.txt", "description": "x"}],
    )
    run_dir = tmp_path / "run"
    rt = ResolvedTask(task=task, task_file=tmp_path / "t.yaml", run_dir=run_dir / "default" / "t", variant_id="default")
    with patch.object(Orchestrator, "__init__", side_effect=AssertionError("an agent must not run")) as init:
        _summary, results = await run_batch([rt], BatchRunConfig(run_dir=run_dir, max_parallel=1))
    init.assert_not_called()
    [result] = results
    assert result.result.final_status == FinalStatus.ERROR
    assert "llm_only needs sandbox.driver: docker" in (result.result.error_message or "")
