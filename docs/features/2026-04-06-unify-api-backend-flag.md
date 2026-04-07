# Unify API Routing with `--backend` Flag

**Date**: 2026-04-06
**Status**: Implemented

## Goal

Replace two boolean settings (`BEDROCK_ENABLED`, `LLMGW_PROXY_ENABLED`) and the `--proxy/--no-proxy` CLI flag with a single `--backend {direct,bedrock,proxy}` flag and `API_BACKEND` env var, eliminating routing precedence confusion.

## Problem

The old design used two independent booleans:

```python
# config.py (old)
llmgw_proxy_enabled: bool = False
bedrock_enabled: bool = False
```

This created 4 possible states, including the ambiguous "both enabled" case that required implicit precedence logic and a warning log:

```python
# orchestrator.py (old)
if settings.bedrock_enabled and settings.llmgw_proxy_enabled:
    logger.warning("Both bedrock_enabled and llmgw_proxy_enabled are set; Bedrock takes precedence.")
```

The `--proxy/--no-proxy` CLI flag only controlled the proxy boolean, leaving Bedrock routing as `.env`-only.

## Solution

A single `ApiBackend` StrEnum with 3 mutually exclusive values:

```python
class ApiBackend(StrEnum):
    DIRECT = "direct"    # Anthropic API directly
    BEDROCK = "bedrock"  # AWS Bedrock (bearer token auth)
    PROXY = "proxy"      # Local LLM Gateway proxy (OAuth2 S2S)
```

**CLI**: `--backend {direct,bedrock,proxy}` using `click.Choice` (matches existing `--stream` pattern).

**Env var**: `API_BACKEND=bedrock` — pydantic-settings coerces automatically via StrEnum.

## Key Design Decisions

### `resolve_route(settings, *, proxy_port=None) -> ApiRoute`

Factory function using `match` + `assert_never` for exhaustive enum dispatch. Reads `settings.api_backend` directly (no redundant `backend` parameter). Uses `assert` for type narrowing since it runs on a validated code path after `validate_api_keys()`.

### `proxy_config_from_settings(settings, *, task_id) -> ProxyConfig`

Shared helper centralizing the Settings-to-ProxyConfig mapping previously duplicated between the orchestrator's `_start_proxy()` and autogen CLI. The old autogen version had subtly different `or ""` fallbacks; the centralized function uses consistent assert-based preconditions.

### `click.Choice` for `--backend`

Already used in the codebase for `--stream`. Provides automatic validation, shell auto-complete, and clear error messages with no manual validation code.

### Validation strategy

- `validate_api_keys()` in `config.py` provides early, user-friendly `ValueError` for missing credentials
- `resolve_route()` uses `assert` for type narrowing (programming contract, not user validation)
- Autogen CLI calls `validate_api_keys()` before `proxy_config_from_settings()` to ensure friendly errors

## Changes

| File | Change |
|------|--------|
| `models/enums.py` | Add `ApiBackend` StrEnum |
| `models/routing.py` | Add `resolve_route()`, `proxy_config_from_settings()` |
| `models/__init__.py` | Export new enum and functions |
| `config.py` | Replace `bedrock_enabled` + `llmgw_proxy_enabled` with `api_backend` |
| `cli/run_command.py` | `--backend` with `click.Choice` replaces `--proxy/--no-proxy` |
| `tools/autogen/cli.py` | Same `--backend` flag, Bedrock guard, proxy validation |
| `orchestrator.py` | Enum dispatch + `resolve_route()`, delete `_start_proxy()` |
| `.env.example` | `API_BACKEND` replaces `BEDROCK_ENABLED` + `LLMGW_PROXY_ENABLED` |
| `README.md` | Updated routing section, CLI table, config table |

## Migration

Replace in `.env`:
- `BEDROCK_ENABLED=true` → `API_BACKEND=bedrock`
- `LLMGW_PROXY_ENABLED=true` → `API_BACKEND=proxy`
- Old vars are silently ignored (`extra="ignore"` in Settings)

## Usage

```bash
# Direct API (default)
coder-eval run tasks/hello_date.yaml

# Via AWS Bedrock
coder-eval run tasks/hello_date.yaml --backend bedrock

# Via LLM Gateway proxy
coder-eval run tasks/hello_date.yaml --backend proxy
```
