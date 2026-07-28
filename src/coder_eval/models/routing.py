"""API routing configuration for the Claude Code agent SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from coder_eval.models.enums import ApiBackend
from coder_eval.models.judge_defaults import DEFAULT_JUDGE_MODEL


if TYPE_CHECKING:
    from coder_eval.config import Settings


# Resolved-at-startup transport for the `llm_judge` criterion under DirectRoute.
# - "anthropic": call api.anthropic.com via the Anthropic SDK (needs ANTHROPIC_API_KEY).
# - None: no ANTHROPIC_API_KEY; any enabled `llm_judge` under DirectRoute fails at
#   dispatch. The Bedrock backend routes the judge through the run's own backend
#   and never reaches this transport selection.
JudgeTransport = Literal["anthropic"]


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
    - ``None``: ANTHROPIC_API_KEY is absent; ``llm_judge`` fails fast at dispatch
      with a clear error. Non-judge runs are unaffected.

    Resolution happens once in ``resolve_route`` so the choice is deterministic
    across criteria and recorded in ``EvaluationResult.environment_info``.
    """

    judge_transport: JudgeTransport | None = "anthropic"


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


@dataclass(frozen=True)
class LiteLLMRoute:
    """Route through a custom Anthropic-compatible endpoint (e.g. a LiteLLM
    gateway fronting Bedrock open-weight models).

    The Claude Code SDK is pointed at ``base_url`` via ``ANTHROPIC_BASE_URL`` and
    authenticates with ``auth_token`` via ``ANTHROPIC_AUTH_TOKEN`` (bearer). The
    ``model``/``small_model`` ids are passed **verbatim** (no Bedrock
    inference-profile qualification) — the gateway maps them to its backend.
    """

    base_url: str
    auth_token: str
    model: str | None = None
    small_model: str | None = None


ApiRoute = DirectRoute | BedrockRoute | LiteLLMRoute


# Stable string names for environment_info recording (decoupled from class names)
ROUTE_NAMES: dict[type, str] = {
    DirectRoute: "anthropic_direct",
    BedrockRoute: "aws_bedrock",
    LiteLLMRoute: "litellm",
}


def resolve_route(settings: Settings) -> ApiRoute:
    """Resolve an ``ApiRoute`` from static settings.

    Handles the three supported backends (``DIRECT``, ``BEDROCK``, ``LITELLM``),
    whose route is fully determined by ``Settings``.

    Called after ``validate_api_keys()`` has verified credentials. Uses
    ``assert`` for type narrowing (not ``ValueError``) since the Bedrock/custom
    credential checks are an internal contract.
    """
    match settings.api_backend:
        case ApiBackend.BEDROCK:
            assert settings.aws_bearer_token_bedrock is not None, "Bedrock requires aws_bearer_token_bedrock"
            assert settings.aws_region is not None, "Bedrock requires aws_region"
            # BEDROCK_MODEL is the only route-level model source. CLI --model /
            # -D agent.model and task-YAML agent.model are resolved later in the
            # agent layer (via _resolve_effective_model), which also handles
            # the anthropic.* + region prefix qualification on bare aliases.
            # Fall back to the main model when no small/fast model is configured.
            # Claude Code routes WebFetch's page-summarization (and other "small,
            # fast" steps) through ANTHROPIC_SMALL_FAST_MODEL; on Bedrock that env
            # var is only exported when small_model is set (see
            # ClaudeCodeAgent._build_sdk_env). Leaving it unset made every
            # WebFetch fail with "model issues" under the Bedrock backend. The main
            # model is always a valid fallback, so default to it.
            small_model = settings.bedrock_small_model or settings.bedrock_model
            return BedrockRoute(
                bearer_token=settings.aws_bearer_token_bedrock,
                region=settings.aws_region,
                model=to_bedrock_inference_profile(settings.bedrock_model, settings.aws_region),
                small_model=to_bedrock_inference_profile(small_model, settings.aws_region),
            )
        case ApiBackend.DIRECT:
            return DirectRoute(judge_transport=_resolve_direct_judge_transport(settings))
        case ApiBackend.LITELLM:
            # Validate here (raise, not assert): resolve_route is reached on the
            # evaluate-only path WITHOUT a preceding validate_api_keys(), so this is
            # the only guard there and must survive `python -O`. Checks presence +
            # URL scheme, raising a field-named ValueError (review non-blocking #11).
            settings._validate_litellm_settings()
            # Narrowing for pyright only — _validate_litellm_settings guarantees these.
            assert settings.litellm_base_url is not None
            assert settings.litellm_auth_token is not None
            # No inference-profile qualification: the id is passed verbatim to the gateway.
            small_model = settings.litellm_small_model or settings.litellm_model
            return LiteLLMRoute(
                base_url=settings.litellm_base_url,
                auth_token=settings.litellm_auth_token,
                model=settings.litellm_model,
                small_model=small_model,
            )


def resolve_evaluation_route(settings: Settings, agent_route: ApiRoute) -> ApiRoute:
    """Resolve the route used by the *evaluation* side — the ``llm_judge`` /
    ``agent_judge`` criteria and the simulated user — which must stay on a
    constant Claude backend regardless of the agent under test, so grading and
    simulation stay comparable across models.

    - Agent on Bedrock/Direct: the judge already runs on Claude via that route,
      so reuse it unchanged (no behavior change for existing runs).
    - Agent on LiteLLM (open-weight): the agent route cannot serve a Claude
      judge, so pin evaluation to Bedrock (preferred, from the AWS bearer token)
      or Direct (``ANTHROPIC_API_KEY``). If neither is configured, fall back to a
      ``DirectRoute`` with no judge transport so ``llm_judge`` fails with its
      clean "unconfigured" error rather than silently scoring 0.0.
    """
    if isinstance(agent_route, BedrockRoute | DirectRoute):
        return agent_route
    # agent_route is LiteLLMRoute → pin evaluation to a constant Claude backend.
    if settings.aws_bearer_token_bedrock and settings.aws_region:
        judge_model = settings.bedrock_model or DEFAULT_JUDGE_MODEL
        qualified = to_bedrock_inference_profile(judge_model, settings.aws_region)
        return BedrockRoute(
            bearer_token=settings.aws_bearer_token_bedrock,
            region=settings.aws_region,
            model=qualified,
            small_model=qualified,
        )
    return DirectRoute(judge_transport=_resolve_direct_judge_transport(settings))


def _resolve_direct_judge_transport(settings: Settings) -> JudgeTransport | None:
    """Pick the judge transport for ``DirectRoute``.

    ``"anthropic"`` when ``ANTHROPIC_API_KEY`` is set (the judge calls
    api.anthropic.com); ``None`` otherwise — the run still starts, but any
    enabled ``llm_judge`` criterion fails at dispatch with a clear error.
    The Bedrock backend never reaches this path: its judge routes through the
    same backend as the run. The choice is made once at startup so it is
    deterministic across criteria and recorded in ``environment_info``.
    """
    return "anthropic" if settings.anthropic_api_key else None
