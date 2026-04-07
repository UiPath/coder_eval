"""API routing configuration for the Claude Code agent SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coder_eval.models.enums import ApiBackend


if TYPE_CHECKING:
    from coder_eval.config import Settings
    from coder_eval.proxy.config import ProxyConfig


@dataclass(frozen=True)
class DirectRoute:
    """Route directly to Anthropic API (uses ANTHROPIC_API_KEY from environment)."""


@dataclass(frozen=True)
class ProxyRoute:
    """Route through local LLM Gateway proxy."""

    port: int


@dataclass(frozen=True)
class BedrockRoute:
    """Route through AWS Bedrock with bearer token authentication."""

    bearer_token: str
    region: str
    model: str | None = None  # Cross-region model ID, e.g. "eu.anthropic.claude-sonnet-4-6"
    small_model: str | None = None  # Cross-region small model ID
    # FIXME(SDK#24168): Claude Code SDK injects x-anthropic-billing-header which
    # Bedrock rejects as a reserved keyword (HTTP 400). Set to False once SDK fixes this.
    disable_attribution_header: bool = True


ApiRoute = DirectRoute | ProxyRoute | BedrockRoute


# Stable string names for environment_info recording (decoupled from class names)
ROUTE_NAMES: dict[type, str] = {
    DirectRoute: "anthropic_direct",
    ProxyRoute: "llmgw_proxy",
    BedrockRoute: "aws_bedrock",
}


def resolve_route(settings: Settings, *, proxy_port: int | None = None) -> ApiRoute:
    """Construct the appropriate ApiRoute for the given backend.

    Called after validate_api_keys() has verified credentials.
    Uses assert for type narrowing (not ValueError) since this is an internal contract.
    """
    match settings.api_backend:
        case ApiBackend.BEDROCK:
            assert settings.aws_bearer_token_bedrock is not None, "Bedrock requires aws_bearer_token_bedrock"
            assert settings.aws_region is not None, "Bedrock requires aws_region"
            return BedrockRoute(
                bearer_token=settings.aws_bearer_token_bedrock,
                region=settings.aws_region,
                model=settings.bedrock_model,
                small_model=settings.bedrock_small_model,
            )
        case ApiBackend.PROXY:
            assert proxy_port is not None, "Proxy backend requires proxy_port"
            return ProxyRoute(port=proxy_port)
        case ApiBackend.DIRECT:
            return DirectRoute()


def proxy_config_from_settings(settings: Settings, *, task_id: str) -> ProxyConfig:
    """Build a ProxyConfig from Settings fields.

    Centralizes the Settings → ProxyConfig mapping used by both the orchestrator and autogen CLI.
    """
    from coder_eval.proxy.config import ProxyConfig

    assert settings.llmgw_url is not None
    assert settings.llmgw_client_id is not None
    assert settings.llmgw_client_secret is not None
    assert settings.llmgw_semantic_org_id is not None
    assert settings.llmgw_semantic_tenant_id is not None

    return ProxyConfig(
        llmgw_url=settings.llmgw_url,
        client_id=settings.llmgw_client_id,
        client_secret=settings.llmgw_client_secret,
        org_id=settings.llmgw_semantic_org_id,
        tenant_id=settings.llmgw_semantic_tenant_id,
        requesting_product=settings.llmgw_requesting_product,
        requesting_feature=settings.llmgw_requesting_feature,
        user_id=settings.llmgw_semantic_user_id or "",
        timeout_seconds=settings.llmgw_timeout_seconds,
        vendor=settings.llmgw_proxy_vendor,
        api_flavor=settings.llmgw_proxy_api_flavor,
        task_id=task_id,
    )
