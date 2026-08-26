"""API routing configuration for the Claude Code agent SDK."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

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
    # Unlike BedrockRoute/LiteLLMRoute, the AGENT never reads this — the Claude Agent
    # SDK picks its own default when unset. It exists so ``checker_context.api_route.model``
    # (see resolve_evaluation_route) has somewhere to land when the eval side is on Direct.
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
    (an Anthropic-compatible gateway fronting Bedrock open-weight models), or,
    on the CHECKER side (``checker_context.api_route.route: litellm``), an
    arbitrary provider reached through the ``litellm`` library directly.

    AGENT side: the Claude Code SDK is pointed at the gateway via
    ``ANTHROPIC_BASE_URL``/``ANTHROPIC_AUTH_TOKEN``. Deliberately carries NO
    ``base_url``/credential field for this — same reasoning as ``BedrockRoute``'s
    docstring: this route object flows through orchestrator state
    (``environment_info`` recording, logging) that has no business handling
    config that should always be read live from the environment.
    ``ClaudeCodeAgent._build_sdk_env`` reads ``settings.litellm_base_url``/
    ``settings.litellm_auth_token`` itself, the same source ``resolve_route``
    validated before constructing this route.

    CHECKER side (``invoke_litellm_judge_async``): unlike the agent path, this
    is NOT sourced from ``coder_eval.config.settings`` at all — the task author
    fully owns it via ``params``/``env_params`` below (a gateway-routed judge
    model rarely reuses the same proxy/credential the AGENT's own LiteLLM
    backend points at). There is no implicit fallback to
    ``settings.litellm_base_url``/``settings.litellm_auth_token``; if the
    provider needs ``api_base``/``api_key``, the task author sets them via
    ``params``/``env_params`` like any other kwarg.

    ``params``/``env_params`` (checker side only, from
    ``checker_context.api_route.{params,env_params}``): ``litellm.acompletion``
    takes dozens of provider-specific kwargs (``api_base``, ``api_key``,
    ``aws_access_key_id``, ``vertex_project``, ``api_version``, ...) that this
    route has no dedicated field for. ``params`` is passed through verbatim as
    extra kwargs. ``env_params`` maps a kwarg name to the ENV VAR NAME to
    resolve it from at call time — e.g. ``{api_key: LITELLM_AUTH_TOKEN,
    aws_access_key_id: AWS_ACCESS_KEY_ID}`` — so an arbitrary provider's config
    (including secrets) is representable without a secret ever landing in the
    task YAML. Both are ``None`` unless a task author set them; ``env_params``'s
    values are env var *names*, never secrets, so it is safe to record verbatim
    in ``environment_info`` (unlike ``params``, which a task author could — but
    shouldn't — put a raw secret into).
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
            model, small_model = _bedrock_model_pair(
                settings.bedrock_model, settings.bedrock_small_model, settings.aws_region
            )
            return BedrockRoute(region=settings.aws_region, model=model, small_model=small_model)
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
                model=settings.litellm_model,
                small_model=small_model,
            )


def _resolve_backend_route(
    settings: Settings,
    backend: ApiBackend,
    *,
    model_override: str | None = None,
    params_override: dict[str, Any] | None = None,
    env_params_override: dict[str, str] | None = None,
) -> ApiRoute:
    """Build the ``ApiRoute`` for an EXPLICITLY-requested backend.

    Used only by the ``checker_context.api_route`` override path (see
    ``resolve_evaluation_route``): raises ``ValueError`` naming the missing env
    var when that backend isn't configured, rather than silently falling back
    to a different backend — an explicit override that can't be honored must
    fail loudly, not degrade to a backend the task author didn't ask for.

    ``model_override`` (``checker_context.api_route.model``) wins over the
    backend's own env-configured default model when set.

    ``ApiBackend.LITELLM`` is the one exception to "env-sourced ``Settings``
    fields, credentials always come from the environment": unlike
    BEDROCK/DIRECT (which reuse the agent's own env-configured credentials, since
    grading still needs to reach the SAME Claude backend), a checker-side litellm
    route is not assumed to share the agent's LiteLLM proxy/gateway at all — it
    is built ENTIRELY from ``params_override``/``env_params_override``
    (``checker_context.api_route.{params,env_params}``), never from
    ``settings.litellm_base_url``/``settings.litellm_auth_token``. Those two
    settings fields are the AGENT's own LiteLLM-backend config (see
    ``resolve_route``) — reusing them here would silently point the judge at
    infrastructure the task author never named.
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
            # ApiBackend covers exactly BEDROCK/DIRECT/LITELLM above; this arm is
            # unreachable but makes the match exhaustive so every path returns
            # explicitly (CodeQL: mixed explicit/implicit returns, PR #137 review).
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
    ``agent_judge`` criteria and the simulated user — which must stay on a
    constant Claude backend regardless of the agent under test, so grading and
    simulation stay comparable across models.

    All overrides come from the reserved ``checker_context.api_route`` namespace
    (see ``TaskDefinition.checker_context``) — ``route`` (``backend_override``)
    picks the backend, ``model`` (``model_override``) picks the model on
    whichever route is resolved, and ``params``/``env_params`` (``params_override``/
    ``env_params_override``) only ever apply when ``backend_override`` resolves to
    ``litellm`` (see ``_resolve_backend_route``). Criteria never read any of
    these directly; they only ever see the resulting ``CheckContext.route``.

    - ``backend_override`` set: build that backend's route from env, regardless
      of ``agent_route`` — an explicit task/variant choice always wins. Raises
      ``ValueError`` if the string isn't a known ``ApiBackend`` or that backend
      isn't configured (see ``_resolve_backend_route``).
    - Agent on Bedrock/Direct (no ``backend_override``): the judge already runs
      on Claude via that route, so reuse it — except its ``model`` is always
      reset to ``model_override`` (``None`` when unset). The agent's own
      env-sourced model (e.g. ``BEDROCK_MODEL``) must NOT leak into the judge's
      default: ``route.model`` must mean "an explicit override was given", not
      "whatever the agent happens to be using" — otherwise an unpinned judge
      silently starts grading with a different model whenever the agent's
      model changes, breaking before/after comparability (PR #137 review:
      "the judge loses DEFAULT_JUDGE_MODEL as its floor").
    - Agent on LiteLLM (open-weight, no ``backend_override``): the agent route
      cannot serve a Claude judge, so pin evaluation to Bedrock (preferred, from
      the AWS bearer token) or Direct (``ANTHROPIC_API_KEY``), honoring
      ``model_override`` there too — same "no override, no baked-in model" rule
      as above. If neither backend is configured, fall back to a ``DirectRoute``
      with no judge transport so ``llm_judge`` fails with its clean
      "unconfigured" error rather than silently scoring 0.0.
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
