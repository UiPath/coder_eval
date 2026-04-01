# API Routing Refactor: Implementation Plan (v2)

> v2 incorporates feedback from gemini-3 and codex review against KISS/DRY/YAGNI.
> Changes from v1: deleted `resolve_api_route_from_settings()` (YAGNI), deleted `BedrockRoute.validate()` (DRY — validation lives in config only), moved route models to `models/routing.py` (follows existing pattern), removed `self.proxy_port` from orchestrator (redundant with `self.route`), added explicit justification for approach choice.

## Goal

Replace the ad-hoc Bedrock routing (raw `dict[str, str]` threaded through the call chain) with a typed `ApiRoute` union. This brings Bedrock to the same level of consistency as the existing proxy pattern: typed config, validation, clean separation between orchestrator and agent.

## Why this approach (and not something simpler)

Three alternatives were considered:

| Approach | Description | Verdict |
|----------|-------------|---------|
| **A: No new types** | Add `_build_sdk_env()` to agent, pass individual params (`proxy_port`, `bedrock_token`, `bedrock_region`, ...) | Rejected — agent constructor gains 5 optional params where only certain combinations are valid. This is the current problem restated. |
| **B: Enum** | `RouteMode` enum (`DIRECT`, `PROXY`, `BEDROCK`) + individual params | Rejected — enum doesn't carry data, so you still need the loose params alongside it. No type-safety gain. |
| **C: Typed union** | 3 frozen dataclasses (~30 lines, no logic) in `models/routing.py` | **Chosen** — each route carries exactly its data; `match` in `_build_sdk_env()` is exhaustive; follows existing codebase pattern (criteria types, template sources use discriminated unions). |

Option C earns its weight because:
1. **The dataclasses are tiny** — 30 lines total, no methods, no logic. This is not heavy abstraction.
2. **Follows existing patterns** — the codebase already uses discriminated unions (`SuccessCriterion`, `TemplateSource`). A new developer will recognize the pattern.
3. **Prevents invalid states** — you can't create a `DirectRoute` with a `bearer_token` or a `ProxyRoute` with a `region`. With Options A/B, all params exist on every call.
4. **Exhaustive matching** — `match route:` with 3 cases means adding a 4th route (e.g., Vertex) produces a type error if unhandled.

## Current State (what's wrong)

- **`config.py`**: 5 flat Bedrock settings, no validation (proxy has `_validate_llmgw_settings()`)
- **`orchestrator.py`**: 3 separate `if/elif/else` blocks for routing (in `_setup()`, `_init_result()`, `_build_bedrock_env()`)
- **`claude_code_agent.py`**: Knows Bedrock internals — inspects `bedrock_env["ANTHROPIC_MODEL"]` to compute `effective_model`
- **No tests** for Bedrock routing logic
- SDK workaround (`CLAUDE_CODE_ATTRIBUTION_HEADER=0`) is buried with no lifecycle management

## Target State

```
Settings → validate_api_keys() validates early
                    ↓
Orchestrator._setup() constructs ApiRoute directly (no factory)
                    ↓
Orchestrator._create_agent(route) passes ApiRoute
                    ↓
ClaudeCodeAgent._build_sdk_env(route) → (env_dict, effective_model)
                    ↓
Orchestrator._init_result() records ROUTE_NAMES[type(route)] + metadata
```

---

## Step-by-step Implementation

### Step 1: Create `src/coder_eval/models/routing.py`

New file in `models/` (where all pure data models live). These are dumb data containers — no validation logic, no factory functions.

```python
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
    model: str | None = None           # Cross-region model ID, e.g. "eu.anthropic.claude-sonnet-4-6"
    small_model: str | None = None     # Cross-region small model ID
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
```

Then export from `models/__init__.py`:

```python
from .routing import ROUTE_NAMES, ApiRoute, BedrockRoute, DirectRoute, ProxyRoute
```

Usage in orchestrator's `_init_result()`: `self.result.environment_info["api_routing"] = ROUTE_NAMES[type(self.route)]`

**Design decisions:**

- **`models/routing.py` not top-level** — project puts pure data models in `models/`, logic elsewhere.
- **No `name` field on dataclasses** — a `ROUTE_NAMES` lookup dict is simpler than `field(default=..., init=False)` on each class, and keeps the dataclasses truly minimal. The names are only needed in one place (`_init_result`).
- **No `validate()` on `BedrockRoute`** — validation lives in `config.py` alongside `_validate_llmgw_settings()`. One validation site per concern.
- **No `resolve_api_route_from_settings()` factory** — only the orchestrator needs routing resolution, and it already has the `if/elif/else` + async proxy startup. A factory that returns `None` for one of its variants is a leaky abstraction.

**Files touched**: `src/coder_eval/models/routing.py` (new), `src/coder_eval/models/__init__.py` (add exports).

---

### Step 2: Add `_validate_bedrock_settings()` to `config.py`

Single source of truth for Bedrock validation, matching the existing proxy pattern.

**File**: `src/coder_eval/config.py`

**Changes**:

1. Add method after `_validate_llmgw_settings()` (~line 102):

```python
def _validate_bedrock_settings(self) -> None:
    """Validate that required AWS Bedrock settings are present.

    Raises:
        ValueError: If required Bedrock settings are missing
    """
    missing = []
    if not self.aws_bearer_token_bedrock:
        missing.append("AWS_BEARER_TOKEN_BEDROCK")
    if not self.aws_region:
        missing.append("AWS_REGION")
    if missing:
        raise ValueError(
            f"Bedrock routing is enabled but missing required settings: {', '.join(missing)}."
            + " Please set them in your .env file."
        )
```

2. Update `validate_api_keys()` — add after the proxy validation block:

```python
if self.bedrock_enabled:
    self._validate_bedrock_settings()
```

**Files touched**: `src/coder_eval/config.py` only.

---

### Step 3: Refactor `orchestrator.py` to use `ApiRoute`

Replace `self.bedrock_env` dict + `self.proxy_port` with a single `self.route: ApiRoute`. Delete `_build_bedrock_env()`.

**File**: `src/coder_eval/orchestrator.py`

**Changes**:

1. **Add import** (top of file):
```python
from .models import ROUTE_NAMES, ApiRoute, BedrockRoute, DirectRoute, ProxyRoute
```

2. **Replace fields** in `__init__` (~line 138-141):

Remove:
```python
self.bedrock_env: dict[str, str] | None = None
```
Remove:
```python
self.proxy_port: int | None = None
```
Add:
```python
self.route: ApiRoute | None = None
```
Keep `self.proxy: LLMGatewayProxy | None = None` — needed for lifecycle (`proxy.stop()` in cleanup, usage aggregation).

3. **Refactor `_setup()`** (~lines 407-414). Replace the routing decision block:

```python
# Determine API routing: Bedrock > LLM Gateway proxy > direct
if settings.bedrock_enabled:
    self.route = BedrockRoute(
        bearer_token=settings.aws_bearer_token_bedrock or "",
        region=settings.aws_region or "",
        model=settings.bedrock_model,
        small_model=settings.bedrock_small_model,
    )
    self.logger.info("API routing: AWS Bedrock (bearer token, region=%s)", self.route.region)
elif settings.llmgw_proxy_enabled:
    self.logger.info("API routing: LLM Gateway proxy (via %s)", settings.llmgw_url)
    await self._start_proxy()
    assert self.proxy is not None and self.proxy.port is not None
    self.route = ProxyRoute(port=self.proxy.port)
else:
    self.route = DirectRoute()
    self.logger.info("API routing: direct Anthropic API")
```

4. **Refactor `_init_result()`** (~lines 438-447). Replace routing env_info block:

```python
# Record API routing mode
assert self.route is not None
self.result.environment_info["api_routing"] = ROUTE_NAMES[type(self.route)]
if isinstance(self.route, BedrockRoute):
    self.result.environment_info["aws_region"] = self.route.region
    if self.route.model:
        self.result.environment_info["bedrock_model"] = self.route.model
elif isinstance(self.route, ProxyRoute):
    self.result.environment_info["llmgw_url"] = settings.llmgw_url or ""
```

5. **Delete `_build_bedrock_env()` entirely** (~lines 483-504). Logic moves to agent's `_build_sdk_env()`.

6. **Update `_start_proxy()`**: Remove `self.proxy_port = await self.proxy.start()`, just call `await self.proxy.start()`. Port is accessed via `self.proxy.port`.

7. **Update `_create_agent()`** (~line 519):
```python
assert self.route is not None
return ClaudeCodeAgent(self.task.agent, route=self.route)
```

8. **Update any remaining `self.proxy_port` references** to use `self.proxy.port` (e.g., in logging).

**Files touched**: `src/coder_eval/orchestrator.py` only.

---

### Step 4: Refactor `claude_code_agent.py` to use `ApiRoute`

Replace `proxy_port` + `bedrock_env` params with a single `route: ApiRoute`. Add `_build_sdk_env()` as a static method.

**File**: `src/coder_eval/agents/claude_code_agent.py`

**Changes**:

1. **Add import** (top of file):
```python
from coder_eval.models import ApiRoute, BedrockRoute, DirectRoute, ProxyRoute
```

2. **Update `__init__` signature** (~lines 117-132):

```python
def __init__(
    self,
    config: AgentConfig,
    route: ApiRoute | None = None,
):
    """Initialize the Claude Code agent.

    Args:
        config: Agent configuration
        route: API routing configuration. If None, uses DirectRoute.
    """
    self.config = config
    self.route = route or DirectRoute()
    # ... rest unchanged (remove self.proxy_port, self.bedrock_env)
```

3. **Add `_build_sdk_env()` static method**:

```python
@staticmethod
def _build_sdk_env(route: ApiRoute) -> tuple[dict[str, str], str | None]:
    """Build SDK environment variables and resolve effective model for the given route.

    Returns:
        Tuple of (env_vars_dict, model_override_or_None).
    """
    match route:
        case BedrockRoute() as br:
            env: dict[str, str] = {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_BEARER_TOKEN_BEDROCK": br.bearer_token,
                "AWS_REGION": br.region,
            }
            if br.disable_attribution_header:
                # FIXME(SDK#24168): Remove when SDK no longer injects reserved header
                env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
            if br.model:
                env["ANTHROPIC_MODEL"] = br.model
            if br.small_model:
                env["ANTHROPIC_SMALL_FAST_MODEL"] = br.small_model
            return env, br.model

        case ProxyRoute() as pr:
            return {
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{pr.port}",
                "ANTHROPIC_API_KEY": "llmgw-proxy",
            }, None

        case DirectRoute():
            return {}, None
```

4. **Update `communicate()`** (~lines 201-235). Replace env + effective_model block:

```python
# Build env overrides and resolve model for the configured API route
env, route_model = self._build_sdk_env(self.route)
effective_model = route_model or self.config.model

options = ClaudeAgentOptions(
    cwd=str(self.working_directory),
    permission_mode=self.config.permission_mode,
    allowed_tools=self.config.allowed_tools or [],
    disallowed_tools=self.config.disallowed_tools or [],
    model=effective_model,
    max_turns=self.config.max_turns,
    system_prompt=self.config.system_prompt,
    plugins=plugins,
    setting_sources=self.config.setting_sources or ["project"],
    stderr=capture_stderr,
    env=env,
)
```

**Files touched**: `src/coder_eval/agents/claude_code_agent.py` only.

---

### Step 5: Add tests

**File**: `tests/test_routing.py` (new)

Focus on `_build_sdk_env()` — it's the only method with logic. Route dataclasses are trivial frozen containers; don't over-test constructors.

```python
class TestBuildSdkEnv:
    """Test ClaudeCodeAgent._build_sdk_env() for all route types."""

    def test_direct_returns_empty_env(self):
        """DirectRoute produces empty env and no model override."""

    def test_proxy_returns_base_url_and_dummy_key(self):
        """ProxyRoute produces ANTHROPIC_BASE_URL and dummy API key."""

    def test_proxy_no_model_override(self):
        """ProxyRoute does not override model."""

    def test_bedrock_basic_env(self):
        """BedrockRoute produces CLAUDE_CODE_USE_BEDROCK, token, region."""

    def test_bedrock_attribution_header_disabled(self):
        """disable_attribution_header=True sets CLAUDE_CODE_ATTRIBUTION_HEADER=0."""

    def test_bedrock_attribution_header_enabled(self):
        """disable_attribution_header=False omits the header key."""

    def test_bedrock_model_override(self):
        """BedrockRoute.model returned as effective_model."""

    def test_bedrock_no_model_returns_none(self):
        """BedrockRoute without model returns None (use task config)."""

    def test_bedrock_small_model(self):
        """BedrockRoute.small_model appears in env as ANTHROPIC_SMALL_FAST_MODEL."""
```

**File**: `tests/test_config_precedence.py` (update existing)

```python
def test_config_bedrock_missing_token():
    """Bedrock enabled + missing token raises ValueError."""

def test_config_bedrock_missing_region():
    """Bedrock enabled + missing region raises ValueError."""

def test_config_bedrock_valid():
    """Bedrock enabled with valid settings passes."""

def test_config_bedrock_disabled_skips_validation():
    """Bedrock disabled skips validation even if settings are empty."""
```

**Files touched**: `tests/test_routing.py` (new), `tests/test_config_precedence.py` (update).

---

### Step 6: Cleanup and verify

1. Run `make verify` (format + lint + typecheck + test + coverage)
2. Grep for stale references:
   - `grep -r "bedrock_env" src/` → 0 hits
   - `grep -r "_build_bedrock_env" src/` → 0 hits
   - `grep -rn "self.proxy_port" src/coder_eval/orchestrator.py` → 0 hits
   - `grep -rn "proxy_port" src/coder_eval/agents/` → 0 hits

---

## Files Changed Summary

| File | Action | Description |
|------|--------|-------------|
| `src/coder_eval/models/routing.py` | **CREATE** | `ApiRoute` union: `DirectRoute`, `ProxyRoute`, `BedrockRoute` (pure data) |
| `src/coder_eval/models/__init__.py` | EDIT | Export route types |
| `src/coder_eval/config.py` | EDIT | Add `_validate_bedrock_settings()`, call from `validate_api_keys()` |
| `src/coder_eval/orchestrator.py` | EDIT | Replace `bedrock_env`+`proxy_port` with `route: ApiRoute`, delete `_build_bedrock_env()` |
| `src/coder_eval/agents/claude_code_agent.py` | EDIT | Replace `proxy_port`+`bedrock_env` with `route: ApiRoute`, add `_build_sdk_env()` |
| `tests/test_routing.py` | **CREATE** | Tests for `_build_sdk_env()` and config validation |
| `tests/test_config_precedence.py` | EDIT | Add Bedrock validation tests |

## Verification Checklist

- [x] `make format` passes
- [x] `make check` passes (no lint errors)
- [x] `make typecheck` passes (pyright)
- [x] `make test` passes (all existing + new tests)
- [ ] `make verify` passes (coverage >= 80%)
- [x] `grep -r "bedrock_env" src/` returns 0 hits
- [x] `grep -r "_build_bedrock_env" src/` returns 0 hits
- [x] `grep -rn "self.proxy_port" src/coder_eval/orchestrator.py` returns 0 hits
- [x] `grep -rn "proxy_port" src/coder_eval/agents/` returns 0 hits
- [ ] CLI `--bedrock` / `--no-bedrock` still works
- [ ] CLI `--proxy` / `--no-proxy` still works
- [x] `EvaluationResult.environment_info["api_routing"]` values unchanged: `"aws_bedrock"`, `"llmgw_proxy"`, `"anthropic_direct"`

## v1 -> v2 Changelog

| What changed | Why (principle) |
|---|---|
| Deleted `resolve_api_route_from_settings()` | **YAGNI** — only orchestrator needs routing decision; factory that returns `None` for proxy is a leaky abstraction |
| Deleted `BedrockRoute.validate()` | **DRY** — validation lives in `config._validate_bedrock_settings()` only, matching proxy pattern |
| Moved `routing.py` to `models/routing.py` | **KISS** — follows existing convention: pure data models live in `models/` |
| Removed `self.proxy_port` from orchestrator | **DRY** — redundant with `self.proxy.port` and `self.route.port`; was only needed because `self.route` didn't exist |
| Trimmed route dataclass tests | **YAGNI** — frozen dataclasses with no logic don't need constructor tests; focus tests on `_build_sdk_env()` |
| Replaced `name` field with `ROUTE_NAMES` dict | **KISS** — `field(default=..., init=False)` on every class is ceremony for a value used in one place; a plain dict is simpler |
| Added "Why this approach" section | Explicitly justifies Option C (typed union) over Option A (no types) and Option B (enum) |
