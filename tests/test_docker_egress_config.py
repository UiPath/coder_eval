"""`network: llm_only` and `egress_allowlist` on DockerDriverConfig: values, normalization, rejection."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from coder_eval.models import DockerDriverConfig, normalize_egress_target


def test_defaults_are_bridge_and_empty_allowlist():
    cfg = DockerDriverConfig()
    assert cfg.network == "bridge"
    assert cfg.egress_allowlist == []


def test_network_admits_exactly_three_modes():
    annotation = DockerDriverConfig.model_fields["network"].annotation
    assert set(typing.get_args(annotation)) == {"bridge", "none", "llm_only"}


def test_llm_only_accepted_and_unknown_mode_rejected():
    assert DockerDriverConfig(network="llm_only").network == "llm_only"
    with pytest.raises(ValidationError):
        DockerDriverConfig(network="internal")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("API.Anthropic.com", "api.anthropic.com:443"),
        ("pypi.org", "pypi.org:443"),
        ("pypi.org:443", "pypi.org:443"),
        ("  pypi.org  ", "pypi.org:443"),
        ("host.docker.internal:4000", "host.docker.internal:4000"),
        ("10.0.0.5:8080", "10.0.0.5:8080"),
        ("example.com:080", "example.com:80"),
        ("localhost:65535", "localhost:65535"),
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
        "pypi.org:abc",
        "",
        "   ",
        "::1",
        "[::1]:443",
        "-bad.example.com",
        "user@pypi.org",
        "evil.com\n:443",
        "pypi.org:44\n3",
        "\u212aube.io",
    ],
)
def test_invalid_entries_raise_naming_the_entry(entry: str):
    with pytest.raises(ValueError, match="egress_allowlist entry"):
        normalize_egress_target(entry)
    with pytest.raises(ValidationError, match="egress_allowlist entry"):
        DockerDriverConfig(egress_allowlist=[entry])


def test_duplicates_are_kept_in_the_list():
    cfg = DockerDriverConfig(egress_allowlist=["pypi.org", "pypi.org:443"])
    assert cfg.egress_allowlist == ["pypi.org:443", "pypi.org:443"]


def test_allowlist_accepted_under_bridge():
    cfg = DockerDriverConfig(network="bridge", egress_allowlist=["pypi.org"])
    assert cfg.network == "bridge"
    assert cfg.egress_allowlist == ["pypi.org:443"]


def test_unknown_key_still_forbidden():
    with pytest.raises(ValidationError):
        DockerDriverConfig(egress_allow=["pypi.org"])  # type: ignore[call-arg]
