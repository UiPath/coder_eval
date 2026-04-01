"""API routing configuration for the Claude Code agent SDK."""

from dataclasses import dataclass


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
