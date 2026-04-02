"""Configuration for the LLM Gateway proxy."""

from dataclasses import dataclass, field


# Default model name mapping from Anthropic CLI names to LLM Gateway names.
# The gateway uses the AWS Bedrock model ID format: anthropic.{name}-v{version}:0
# Note: Latest model aliases (e.g. claude-sonnet-4-6) may use shorter gateway IDs
# without the -v1:0 suffix — these are gateway-specific and verified empirically.
DEFAULT_MODEL_MAP: dict[str, str] = {
    # Claude 4.6 (latest aliases + dated)
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
    "claude-sonnet-4-6-20250514": "anthropic.claude-sonnet-4-6",
    "claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6-20250514": "anthropic.claude-opus-4-6-v1",
    # Claude 4.0 (dated only — short aliases point to 4.6)
    "claude-sonnet-4-20250514": "anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-20250514": "anthropic.claude-opus-4-20250514-v1:0",
    # Claude 4.5 (short aliases + -latest + dated)
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-5-latest": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-5-20251101": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-sonnet-4-5-latest": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-sonnet-4-5-20250929": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-haiku-4-5-latest": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-haiku-4-5-20251001": "anthropic.claude-haiku-4-5-20251001-v1:0",
    # Claude 3.7
    "claude-3-7-sonnet-latest": "anthropic.claude-3-7-sonnet-20250219-v1:0",
    "claude-3-7-sonnet-20250219": "anthropic.claude-3-7-sonnet-20250219-v1:0",
    # Claude 3.5
    "claude-3-5-sonnet-latest": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-20241022": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-20240620": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    # Note: claude-3-5-haiku is NOT supported by the LLM Gateway (verified 2026-03-24)
    # Claude 3
    "claude-3-opus-20240229": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet-20240229": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku-20240307": "anthropic.claude-3-haiku-20240307-v1:0",
}


@dataclass(frozen=True)
class ProxyConfig:
    """Configuration for the LLM Gateway proxy server."""

    # LLM Gateway connection
    llmgw_url: str
    client_id: str
    client_secret: str

    # Organization context for passthrough URL
    org_id: str
    tenant_id: str

    # LLM Gateway headers
    requesting_product: str = "coder-eval"
    requesting_feature: str = "claude-code-agent"
    user_id: str = ""
    timeout_seconds: int = 300

    # Passthrough endpoint settings
    vendor: str = "awsbedrock"
    api_flavor: str = "invoke"

    # Model name mapping (CLI name -> gateway name). Falls back to DEFAULT_MODEL_MAP.
    model_map: dict[str, str] = field(default_factory=dict)

    # Optional task ID for log context (prefixes log messages with [task_id])
    task_id: str | None = None
