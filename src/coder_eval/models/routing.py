"""API routing configuration for the Claude Code agent SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from coder_eval.models.enums import ApiBackend


if TYPE_CHECKING:
    from coder_eval.config import Settings
    from coder_eval.proxy.config import ProxyConfig


# Resolved-at-startup transport for the `llm_judge` criterion under DirectRoute.
# - "anthropic": call api.anthropic.com via the Anthropic SDK (needs ANTHROPIC_API_KEY).
# - "llmgw": route through the LangChain LLM Gateway client (needs the LLMGW_* set).
# - None: no judge credentials configured; any enabled `llm_judge` fails at dispatch.
JudgeTransport = Literal["anthropic", "llmgw"]


# Bedrock cross-region inference profile prefixes.
_BEDROCK_KNOWN_PREFIXES: tuple[str, ...] = ("eu.", "us.", "apac.", "global.")


def to_bedrock_inference_profile(model: str | None, region: str | None) -> str | None:
    """Qualify a Claude alias into a Bedrock cross-region inference-profile id.

    Two transforms are applied in order:

    1. Vendor qualifier — a bare alias like ``claude-sonnet-4-6`` gets
       ``anthropic.`` prepended (skipped if it's already qualified or carries a
       region prefix).
    2. Region qualifier — the AWS region's inference-profile prefix
       (``eu.``/``us.``/``apac.``) is prepended so the same input works across
       regions. Ids that already carry a known prefix pass through unchanged so
       a user can pin a specific profile (e.g. ``global.anthropic.…``).

    Examples (region=eu-north-1):
        ``claude-sonnet-4-6`` → ``eu.anthropic.claude-sonnet-4-6``
        ``anthropic.claude-sonnet-4-6`` → ``eu.anthropic.claude-sonnet-4-6``
        ``us.anthropic.claude-sonnet-4-6`` → ``us.anthropic.claude-sonnet-4-6``
    """
    if not model or not region:
        return model
    model = model.strip()
    if not model:
        return None
    # 1. Vendor qualifier.
    if (
        not model.startswith(_BEDROCK_KNOWN_PREFIXES)
        and not model.startswith("anthropic.")
        and model.startswith("claude-")
    ):
        model = f"anthropic.{model}"
    # 2. Region qualifier.
    if model.startswith(_BEDROCK_KNOWN_PREFIXES):
        return model
    region_lower = region.lower()
    if region_lower.startswith("eu-"):
        return f"eu.{model}"
    if region_lower.startswith("us-"):
        return f"us.{model}"
    if region_lower.startswith("ap-"):
        return f"apac.{model}"
    return model


@dataclass(frozen=True)
class DirectRoute:
    """Route directly to Anthropic API for the agent.

    The agent inherits parent-env auth and lets the Claude Agent SDK pick its
    credential (API key / OAuth token / cached `claude login`). The
    ``judge_transport`` field separately controls which transport the
    ``llm_judge`` criterion uses, since the bare ``anthropic`` SDK can only
    authenticate via ``ANTHROPIC_API_KEY``:

    - ``"anthropic"``: judge calls api.anthropic.com (requires ANTHROPIC_API_KEY).
    - ``"llmgw"``: judge falls back to the LangChain LLM Gateway client
      (requires the LLMGW_* settings). Activated when ANTHROPIC_API_KEY is
      absent but full LLMGW creds are present.
    - ``None``: neither credential set; ``llm_judge`` fails fast at dispatch
      with a clear error. Non-judge runs are unaffected.

    Resolution happens once in ``resolve_route`` so the choice is deterministic
    across criteria and recorded in ``EvaluationResult.environment_info``.
    """

    judge_transport: JudgeTransport | None = "anthropic"


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


def resolve_route(settings: Settings) -> ApiRoute:
    """Resolve an ``ApiRoute`` from static settings.

    Handles only backends whose route is fully determined by ``Settings``
    (``DIRECT`` and ``BEDROCK``). PROXY is **not** handled here — its port is
    runtime state owned by ``Orchestrator``, which constructs
    ``ProxyRoute(port=self.proxy.port)`` inline next to the proxy-start block.

    Called after ``validate_api_keys()`` has verified credentials. Uses
    ``assert`` for type narrowing (not ``ValueError``) since the Bedrock
    credential checks are an internal contract.
    """
    match settings.api_backend:
        case ApiBackend.BEDROCK:
            assert settings.aws_bearer_token_bedrock is not None, "Bedrock requires aws_bearer_token_bedrock"
            assert settings.aws_region is not None, "Bedrock requires aws_region"
            # BEDROCK_MODEL is the only route-level model source. DEFAULT_AGENT_MODEL
            # and task-YAML agent.model are resolved later in the agent layer (via
            # _apply_cli_overrides + _resolve_effective_model), which also handles
            # the anthropic.* + region prefix qualification on bare aliases.
            return BedrockRoute(
                bearer_token=settings.aws_bearer_token_bedrock,
                region=settings.aws_region,
                model=to_bedrock_inference_profile(settings.bedrock_model, settings.aws_region),
                small_model=to_bedrock_inference_profile(settings.bedrock_small_model, settings.aws_region),
            )
        case ApiBackend.PROXY:
            msg = (
                "PROXY backend must construct ProxyRoute(port=...) directly with the running "
                + "proxy port; resolve_route handles only static-from-settings backends."
            )
            raise ValueError(msg)
        case ApiBackend.DIRECT:
            return DirectRoute(judge_transport=_resolve_direct_judge_transport(settings))


def _resolve_direct_judge_transport(settings: Settings) -> JudgeTransport | None:
    """Pick the judge transport for ``DirectRoute`` based on which creds are configured.

    Precedence: ``ANTHROPIC_API_KEY`` first, then the LLMGW credential set as a
    fallback. Returns ``None`` if neither is configured — the run still starts,
    but any enabled ``llm_judge`` criterion will fail at dispatch with a clear
    error. The choice is made once at startup so it is deterministic across
    criteria, audit-loggable, and recordable in ``environment_info``.
    """
    if settings.anthropic_api_key:
        return "anthropic"
    if _has_llmgw_credentials(settings):
        return "llmgw"
    return None


def _has_llmgw_credentials(settings: Settings) -> bool:
    """True iff the full LLMGW credential set required to build a chat model is present.

    Mirrors the fields checked by ``Settings._validate_llmgw_settings``. Kept
    in sync with that method — if one grows a field, so should the other.
    """
    return bool(
        settings.llmgw_url
        and settings.llmgw_client_id
        and settings.llmgw_client_secret
        and settings.llmgw_semantic_org_id
        and settings.llmgw_semantic_tenant_id
    )


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
