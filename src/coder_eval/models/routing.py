"""API routing configuration for the Claude Code agent SDK."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from coder_eval.models.enums import ApiBackend
from coder_eval.models.judge_defaults import DEFAULT_JUDGE_MODEL


if TYPE_CHECKING:
    from coder_eval.config import Settings


# Resolved-at-startup transport for the `llm_judge` criterion under DirectRoute:
# "anthropic" when ANTHROPIC_API_KEY is present, None otherwise (an enabled
# llm_judge then fails at dispatch). Bedrock never reaches this selection.
# Rationale: .claude/notes/contracts.md § Route resolution
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
    # The AGENT never reads this -- the SDK picks its own default. It exists so
    # ``checker_context.api_route.model`` has somewhere to land on Direct.
    model: str | None = None


@dataclass(frozen=True)
class BedrockRoute:
    """Route through AWS Bedrock with bearer token authentication.

    Deliberately carries NO credential field: the bearer token is a secret, and
    a route object flows through orchestrator state (``CheckContext``,
    ``environment_info`` recording, logging) that has no business handling one.
    Every consumer that actually needs the token (``ClaudeCodeAgent._build_sdk_env``
    for the agent subprocess, ``judge_bedrock.invoke_bedrock_judge_async`` for the
    judge's HTTP call) reads ``settings.aws_bearer_token_bedrock`` itself, via the
    shared ``coder_eval.config.settings`` singleton — the same source ``resolve_route``
    validated before constructing this route in the first place.
    """

    region: str
    model: str | None = None  # Cross-region model ID, e.g. "eu.anthropic.claude-sonnet-4-6"
    small_model: str | None = None  # Cross-region small model ID
    # FIXME(SDK#24168): Claude Code SDK injects x-anthropic-billing-header which
    # Bedrock rejects as a reserved keyword (HTTP 400). Set to False once SDK fixes this.
    disable_attribution_header: bool = True


@dataclass(frozen=True)
class LiteLLMRoute:
    """Route through a custom endpoint — either the AGENT's own LiteLLM proxy
    (an Anthropic-compatible gateway fronting Bedrock open-weight models), or, on
    the CHECKER side (``checker_context.api_route.route: litellm``), an arbitrary
    provider reached through the ``litellm`` library directly.

    Carries NO ``base_url``/credential field: this route object flows through
    orchestrator state that has no business handling config which should be read
    live from the environment.

    ``params``/``env_params`` (checker side only) cover the provider-specific
    kwargs this route has no dedicated field for. ``params`` is passed to
    ``litellm.acompletion`` verbatim; ``env_params`` maps a kwarg name to the ENV
    VAR NAME to resolve it from at call time, so a provider's config — secrets
    included — is representable without a secret landing in the task YAML. Only
    ``env_params`` is safe to record verbatim.

    Rationale: .claude/notes/contracts.md § LiteLLM params and env_params
    """

    model: str | None = None
    small_model: str | None = None
    params: dict[str, Any] | None = None
    env_params: dict[str, str] | None = None


ApiRoute = DirectRoute | BedrockRoute | LiteLLMRoute


# Stable string names for environment_info recording (decoupled from class names)
ROUTE_NAMES: dict[type, str] = {
    DirectRoute: "anthropic_direct",
    BedrockRoute: "aws_bedrock",
    LiteLLMRoute: "litellm",
}


def _bedrock_model_pair(model: str | None, small_model: str | None, region: str) -> tuple[str | None, str | None]:
    """Resolve ``(model, small_model)`` into Bedrock inference-profile ids, defaulting
    ``small_model`` to ``model`` when unset. Shared by every ``BedrockRoute`` construction
    site (``resolve_route``, ``_resolve_backend_route``, ``resolve_evaluation_route``'s
    pin-to-Claude branch) so the qualification logic can't drift between them.
    """
    resolved_small = small_model or model
    return to_bedrock_inference_profile(model, region), to_bedrock_inference_profile(resolved_small, region)


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
            # BEDROCK_MODEL is the only route-level model source; agent.model is
            # resolved later in the agent layer. Falling back to the main model is
            # load-bearing: ANTHROPIC_SMALL_FAST_MODEL is exported only when
            # small_model is set, and leaving it unset made every WebFetch fail.
            # Rationale: .claude/notes/contracts.md § Route resolution
            model, small_model = _bedrock_model_pair(
                settings.bedrock_model, settings.bedrock_small_model, settings.aws_region
            )
            return BedrockRoute(region=settings.aws_region, model=model, small_model=small_model)
        case ApiBackend.DIRECT:
            return DirectRoute(judge_transport=_resolve_direct_judge_transport(settings))
        case ApiBackend.LITELLM:
            # Raise, not assert: reached on the evaluate-only path without a
            # preceding validate_api_keys(), so it must survive `python -O`.
            settings._validate_litellm_settings()
            # Narrowing for pyright only — _validate_litellm_settings guarantees these.
            assert settings.litellm_base_url is not None
            assert settings.litellm_auth_token is not None
            # No inference-profile qualification: the id is passed verbatim to the gateway.
            small_model = settings.litellm_small_model or settings.litellm_model
            return LiteLLMRoute(
                model=settings.litellm_model,
                small_model=small_model,
            )
        case _:
            # Unreachable, but keeps the match exhaustive so every path returns
            # explicitly (CodeQL: mixed explicit/implicit returns).
            raise AssertionError(f"unhandled ApiBackend: {settings.api_backend!r}")


def _resolve_backend_route(
    settings: Settings,
    backend: ApiBackend,
    *,
    model_override: str | None = None,
    params_override: dict[str, Any] | None = None,
    env_params_override: dict[str, str] | None = None,
) -> ApiRoute:
    """Build the ``ApiRoute`` for an EXPLICITLY-requested backend.

    Used only by the ``checker_context.api_route`` override path. It RAISES,
    naming the missing env var, when the requested backend is not configured,
    rather than silently falling back to a different one: an explicit override
    that cannot be honored must fail loudly, not degrade to a backend the task
    author never asked for. Raise rather than assert, because this is reached on
    the evaluate-only path with no preceding key validation and must survive
    ``-O``.

    ``ApiBackend.LITELLM`` is the exception to "credentials come from the
    environment": a checker-side litellm route is built ENTIRELY from
    ``params_override`` / ``env_params_override``, never from
    ``settings.litellm_*``, and raises when ``model_override`` is absent — there
    is no default gateway model to fall back to.

    Rationale: .claude/notes/contracts.md § LiteLLM params and env_params
    """
    match backend:
        case ApiBackend.BEDROCK:
            if not settings.aws_bearer_token_bedrock or not settings.aws_region:
                raise ValueError(
                    "checker_context route 'bedrock' requires AWS_BEARER_TOKEN_BEDROCK and AWS_REGION to be set"
                )
            judge_model = model_override or settings.bedrock_model or DEFAULT_JUDGE_MODEL
            model, small_model = _bedrock_model_pair(judge_model, settings.bedrock_small_model, settings.aws_region)
            return BedrockRoute(region=settings.aws_region, model=model, small_model=small_model)
        case ApiBackend.DIRECT:
            if not settings.anthropic_api_key:
                raise ValueError("checker_context route 'direct' requires ANTHROPIC_API_KEY to be set")
            return DirectRoute(judge_transport="anthropic", model=model_override)
        case ApiBackend.LITELLM:
            if not model_override:
                msg = (
                    "checker_context route 'litellm' requires an explicit `checker_context.api_route.model` "
                    "— there is no default open-weight/gateway model to fall back to"
                )
                raise ValueError(msg)
            return LiteLLMRoute(
                model=model_override,
                params=params_override,
                env_params=env_params_override,
            )
        case _:
            # Unreachable, but keeps the match exhaustive so every path returns
            # explicitly (CodeQL: mixed explicit/implicit returns).
            raise AssertionError(f"unhandled ApiBackend: {backend!r}")


def resolve_evaluation_route(
    settings: Settings,
    agent_route: ApiRoute,
    *,
    backend_override: str | None = None,
    model_override: str | None = None,
    params_override: dict[str, Any] | None = None,
    env_params_override: dict[str, str] | None = None,
) -> ApiRoute:
    """Resolve the route used by the *evaluation* side — the ``llm_judge`` /
    ``agent_judge`` criteria — which is resolved SEPARATELY from the agent's own.

    An explicit ``backend_override`` always wins. Otherwise a Bedrock or Direct
    agent route is REUSED (with ``model_override`` applied), but a LiteLLM one is
    NOT: evaluation is pinned to a constant Claude backend instead — Bedrock when
    the credentials are present, else Direct.

    ``model_override`` comes FROM ``checker_context.api_route.model`` (the
    task-authored input) and lands on the RESOLVED route's ``model``. It is set
    only when a real override was given, never from the agent's own model.

    Rationale: .claude/notes/contracts.md § Route resolution
    """
    if backend_override is not None:
        try:
            backend = ApiBackend(backend_override)
        except ValueError as e:
            valid = ", ".join(b.value for b in ApiBackend)
            raise ValueError(f"checker_context route {backend_override!r} is not a known backend ({valid})") from e
        return _resolve_backend_route(
            settings,
            backend,
            model_override=model_override,
            params_override=params_override,
            env_params_override=env_params_override,
        )
    if isinstance(agent_route, BedrockRoute | DirectRoute):
        if isinstance(agent_route, BedrockRoute) and model_override:
            # Bedrock model ids must be region-qualified — reusing the agent's route
            # verbatim would ship a bare alias straight to the Bedrock API (400).
            qualified_model, _ = _bedrock_model_pair(model_override, None, agent_route.region)
            return replace(agent_route, model=qualified_model)
        return replace(agent_route, model=model_override)
    # agent_route is LiteLLMRoute → pin evaluation to a constant Claude backend.
    if settings.aws_bearer_token_bedrock and settings.aws_region:
        if model_override:
            model, small_model = _bedrock_model_pair(model_override, None, settings.aws_region)
        else:
            model, small_model = None, None
        return BedrockRoute(region=settings.aws_region, model=model, small_model=small_model)
    return DirectRoute(judge_transport=_resolve_direct_judge_transport(settings), model=model_override)


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
