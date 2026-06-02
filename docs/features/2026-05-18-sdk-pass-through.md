# SDK Pass-Through Options — `sdk_options`

A typed pass-through dict on `AgentConfig` (and inside
`AgentJudgeCriterion.agent`) for Claude Code SDK `ClaudeAgentOptions`
fields that `coder_eval` doesn't own directly. Values are forwarded
verbatim to the SDK constructor; new SDK knobs are zero-code to expose.

## What it does

`AgentConfig.sdk_options: dict[str, Any]` is splatted into
`ClaudeAgentOptions(**sdk_options)` in
`ClaudeCodeAgent._build_options`. At YAML-load time a Pydantic
validator rejects:

1. Keys that are not `ClaudeAgentOptions` dataclass fields, and
2. Keys that `coder_eval` already owns as typed `AgentConfig` fields
   or that the framework manages internally (`model`,
   `permission_mode`, `allowed_tools`, `cwd`, `resume`, …).

The deny-list also covers security-critical knobs whose acceptance
would bypass the `AgentJudgeCriterion` `setting_sources=[]` invariant:
`hooks`, `mcp_servers`, `cli_path`, `extra_args`, `agents`,
`can_use_tool`, `permission_prompt_tool_name`, `tools`, `sandbox`,
`skills`, `add_dirs`. Those must be set through the typed
`AgentConfig` surface (or are simply not user-settable).

Value types are NOT validated by `coder_eval` — the SDK handles that
when constructing `ClaudeAgentOptions`. Mistyped values fail at the
SDK call site with a clear error rather than being re-mirrored here.

## What lives where

| Field | Owner | Where it goes |
|---|---|---|
| `model`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `plugins`, `system_prompt`, `system_prompt_file`, `setting_sources`, `claude_settings`, `ignore_patterns` | `AgentConfig` (typed) | Hand-mirrored because the orchestrator / judge / lineage tracker reasons about them (logs, A/B's, mutates). |
| Everything else on `ClaudeAgentOptions` that is not framework-managed (e.g. `effort`, `betas`, `fallback_model`, `max_thinking_tokens`, `thinking`, …) | `sdk_options` (pass-through) | No coder_eval logic depends on them; the SDK is the source of truth. The exact allow-list is computed at runtime from `_VALID_SDK_OPTION_FIELDS - _FRAMEWORK_OWNED_SDK_FIELDS` (see `src/coder_eval/models/agent_config.py`), so it tracks the installed SDK without needing a doc update on every bump. |

Note: `include_partial_messages` used to be in the pass-through bucket; it's now framework-owned. `ClaudeCodeAgent` always sets it to `True` so it can recover per-emission `output_tokens` from raw stream events — see `2026-05-28-per-message-token-recording.md`.

## How to configure

### Task YAML

```yaml
agent:
  type: "claude-code"
  model: "claude-sonnet-4-6"
  sdk_options:
    effort: high
    max_thinking_tokens: 2048
```

### Experiment YAML (defaults or variant)

```yaml
defaults:
  agent:
    sdk_options: {effort: medium}
variants:
  - variant_id: deep
    agent:
      sdk_options: {effort: xhigh}
```

### `agent_judge` criterion

The judge has its own nested `AgentConfig`:

```yaml
- type: "agent_judge"
  prompt: "..."
  agent:
    model: "claude-haiku-4-5-20251001"
    sdk_options: {effort: low}
```

Partial blocks (e.g. only `model:`) keep the judge's hardened defaults
(read-only toolkit, `bypassPermissions`, hook/MCP ignore patterns) for
fields the user didn't set. A security floor (`.claude`, `.mcp.json`,
`_reference` ignore patterns, `setting_sources=[]`) is enforced by
`_build_agent_config` regardless of YAML.

**Scope of SDK-option overrides and the 5-layer merge:** they apply *only*
to the top-level `AgentConfig.sdk_options` consumed by the coder agent.
Judge SDK options stay YAML-only — `criterion.agent.sdk_options` is
bound at task-load time and not re-merged at CLI / experiment / variant
layers. To vary judge options across an experiment, edit the task YAML
directly (or split into a per-variant judge spec) rather than overriding
on the command line.

### CLI

```bash
coder-eval run … -D agent.sdk_options.effort=high -D agent.sdk_options.max_thinking_tokens=2048
```

Each `-D agent.sdk_options.KEY=VALUE` is repeatable. Values are run through
`yaml.safe_load`, so `true`/`false`/`null`/numbers coerce naturally;
strings pass through unchanged.

As of 2026-06-01, SDK options are set through the generic `-D`/`--set`
override mechanism (`-D agent.sdk_options.k=v`); the original dedicated
`--sdk-option` flag was removed in favor of it. See
[2026-06-01-generic-d-overrides.md](2026-06-01-generic-d-overrides.md).

## Deep-merge across the 5 layers

`sdk_options` is the only `AgentConfig` key that deep-merges. Every
other key still replaces wholesale on each layer.

```
default experiment defaults → experiment defaults → task YAML → variant → CLI
```

Each layer contributes / overrides individual SDK keys; missing keys
are preserved from the lower layer. So setting
`sdk_options: {effort: high}` at the default layer and
`sdk_options: {max_thinking_tokens: 2048}` at the variant layer
yields `{effort: high, max_thinking_tokens: 2048}` — not the
variant's dict in isolation.

## Per-key lineage

Each `sdk_options` key gets its own lineage entry:

```
agent.sdk_options.effort           → source=variant
agent.sdk_options.max_thinking_tokens → source=default
```

`-D agent.sdk_options.effort=high` records `source=cli, source_detail="-D agent.sdk_options.effort=high"`.

## Pointers

- Splat site: `src/coder_eval/agents/claude_code_agent.py` —
  `ClaudeAgentOptions(... **self.config.sdk_options)`.
- Merge site: `src/coder_eval/orchestration/config_merge.py` — the generic
  resolver deep-merges this key (free-form `dict` → `deep` strategy).
- CLI parse: `src/coder_eval/cli/run_command.py:_parse_sdk_options`.
- Validator: `AgentConfig._validate_sdk_options_keys` with module
  constants `_VALID_SDK_OPTION_FIELDS` /
  `_FRAMEWORK_OWNED_SDK_FIELDS`.

## Why this exists

`coder_eval` previously hand-mirrored each SDK option as a typed
`AgentConfig` field. Adding `effort` alone touched 8 files (model
field × 3, `BatchRunConfig` field, `typer.Option`,
`_apply_cli_overrides` branch, lineage record, docs / YAML). The SDK
has dozens more knobs. Pass-through replaces the recurring per-knob
tax with one mechanism. Fields the orchestrator actually reasons about
(logs, varies across experiments, mutates) stay typed.
