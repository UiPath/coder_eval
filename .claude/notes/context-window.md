# Capping the agent's context window

Analysis and design for `agent.context_window`: one harness-neutral field that caps the
context window an agent works within, set per agent config or per experiment variant.

## Problem and evidence

A benchmark of Claude Code on Sonnet 5 (Bedrock, 1M-token window) let runs grow to
250k-480k tokens of context. Above about 150k tokens each extended-thinking step got
about 10x slower, so most runs hit the 2-hour task limit. The operator wants to measure
pass rate and cost at several context caps, for example one variant at 200k and one at
400k, on the same tasks.

Before this change no harness-neutral way existed:

- **claude-code**: `claude_settings` (`--settings`) could carry `autoCompactWindow`. That
  works for one harness only, and when `claude_settings` is a file path a variant cannot
  merge one key into it.
- **codex**: `CodexAgentConfig` has no pass-through. `CodexAgent._build_thread_options`
  builds the thread `config` dict itself, so no value reached Codex.
- **antigravity, opencode, pi, delegate**: no knob is wired.

## How each harness controls its window

### claude-code

Verified in the CLI the SDK spawns. `claude-agent-sdk` runs its **bundled** CLI before it
looks for `claude` on `PATH` (`SubprocessCLITransport._find_cli`), so the CLI that runs is
the one the SDK wheel bundles, not the one the agent image installs with npm:

| coder_eval | claude-agent-sdk (uv.lock) | bundled CLI | image npm CLI |
|---|---|---|---|
| 0.12.1 | 0.2.124 | 2.1.216 | 2.1.177 |
| 0.12.12 | 0.2.159 | 2.1.281 | 2.1.281 |

`environment_info.claude_code_cli` records `claude -v` from `PATH`, so a 0.12.1 run
records 2.1.177 while 2.1.216 ran. That is a separate defect and is not fixed here.

Three inputs set the auto-compact window. Both bundled CLIs (2.1.216 and 2.1.281) contain
the same resolution function. The highest precedence comes first:

1. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment variable: a plain token count. It
   outranks the flag and every settings scope.
2. `--autocompact <auto|tokens>`: added in 2.1.221 (CLI reference). It is absent from
   2.1.216 as a parsed option.
3. `autoCompactWindow` settings key. The settings schema is
   `int().min(100000).max(1000000).optional().catch(undefined)`, so an out-of-range
   value is **silently dropped**.

The effective window is `min(model context window, configured value)`. Auto-compaction
starts when the context approaches that window.

### codex

Verified in the `openai-codex-cli-bin` 0.156.1 binary (the pin) and its source at tag
`rust-v0.156.1`:

- `model_context_window` ("Size of the context window for the model, in tokens"):
  `with_config_overrides` sets the model's `context_window` to
  `min(value, max_context_window)`. Three things follow from it. The default
  auto-compact limit becomes 90% of it. The hard cap, which forces compaction, becomes
  `context_window * effective_context_window_percent / 100`. The remaining-window figure
  the model is shown also changes.
- `model_auto_compact_token_limit` ("Token usage threshold triggering auto-compaction"):
  the trigger only, clamped to 90% of the resolved window. It does not change the window.
- `model_auto_compact_token_limit_scope` (`total` by default, or `body_after_prefix`) and
  `model_post_turn_compact_threshold_percent` (turn-end compaction, off by default) tune
  when compaction starts. They are not caps.

The thread `config` dict `thread_start` takes is the same override surface that
coder_eval already uses for `enabled_tools` and `model_providers`.

### antigravity, opencode, pi, delegate

None of them has a context-window knob wired in coder_eval. They are out of scope, and
they reject the field.

## Options considered

**Per-harness pass-through** (a Codex `config` pass-through next to `claude_settings`).
It is flexible, but a variant would have to name a different key per harness, and the
recorded value would mean something different on each. It also opens a free-form
pass-through on Codex with no denylist. Rejected.

**A field under `run_limits`.** Run limits are caps that the orchestrator or the agent
enforces on a run (turns, time, tokens, USD), and the parity table lists them. A context
window is not a spend cap. It is a harness setting that changes what the agent does, and
it depends on the agent type, which `run_limits` does not know. The guide also says that
`agent:` holds no run-time caps. Rejected.

**A field on the agent config.** One name, accepted on a type-less config so that
experiment defaults and variants can set it, and validated against the concrete type
when the config resolves. **Chosen.**

Naming: `context_window`, in tokens. It names the quantity the operator caps, matches
Codex's `model_context_window`, and avoids "auto-compact", which describes a mechanism of
one harness. `max_context_tokens` was considered, but it reads as a per-request input
limit, which neither harness enforces.

## Recommended design

`BaseAgentConfig.context_window: int | None` (default `None`, `gt=0`).

- Each config class declares `_context_window_range: ClassVar[tuple[int, int | None] |
  None]`. `None` (the base default) means that the harness cannot apply the field.
  `ClaudeCodeAgentConfig` declares `(100_000, 1_000_000)` and `CodexAgentConfig` declares
  `(1, None)`.
- `check_context_window_supported` (model validator) rejects an unsupported type or an
  out-of-range value as soon as the type is known. A plugin agent inherits `None`, so it
  also rejects the field until it opts in.
- The field uses the default `replace` merge strategy. It is set at any of the five
  layers, and with `-D agent.context_window=N`.

Per-harness mapping:

- **claude-code**: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=<N>` in the SDK `env`. The
  environment variable was chosen over the settings key and the flag for four reasons.
  It has the highest precedence, so host, project and `claude_settings` values cannot
  override the cap. It works whether `claude_settings` is a dict or a file path. Both
  bundled CLIs read it, which the flag does not. `extra_args` stays framework-owned.
  `claude_settings.autoCompactWindow` together with `context_window` is a load error,
  because the environment variable would silently override it.
- **codex**: `model_context_window: <N>` in the thread `config`. No trigger key is set,
  so Codex compacts at its default 90% of the window.

## Validation

- `gt=0` on every config, so a type-less config rejects nonsense as well.
- claude-code: 100000-1000000, the CLI's documented range. Outside that range the CLI
  drops a settings value silently, so coder_eval enforces the range itself.
- codex: any positive integer. Codex clamps the value to the model's catalog maximum.
- every other type: rejected at load, naming the supported types. A variant that
  switches `type` to an unsupported harness fails when the experiment resolves.

## Recording

The resolved agent config is persisted as `agent_config` on every task row (`run.json`
and `task.json`). `agent.context_window` therefore appears per task, and config lineage
records which layer set it (for example, the variant). The experiment report groups by
variant, so a cap per variant groups without new report code. On claude-code the
`sdk_options` dump also shows the environment variable in `env`.

## Open questions

- **A silent clamp to the model's window.** Neither harness reports that the model's
  window lowered the cap. A cap above the model's window runs at the model's window.
  Recording the effective window would need a per-model catalog in coder_eval.
- **Trigger points differ.** Codex compacts at 90% of the window. Claude Code compacts
  when the context approaches the window, minus an internal buffer. Equal caps therefore
  do not compact at exactly the same token count. A separate harness-neutral trigger
  field is possible, but it is not added until a benchmark needs it.
- **A host `CLAUDE_CODE_AUTO_COMPACT_WINDOW` leaks into uncapped runs.** The SDK starts
  the CLI with `os.environ` underneath `options.env`. When the field is unset, a host
  value still applies. Clearing it would change behaviour for current users. It is
  recorded in this note but not changed.
- **The recorded CLI version is wrong.** See the version table above:
  `environment_info.claude_code_cli` should record the bundled CLI version.
- **Other harnesses.** OpenCode (`limit.context` in provider config) and Pi may have
  equivalents. Each one needs its own verification before it opts in.
