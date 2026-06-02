# Generic `-D path=value` / `--set` CLI overrides

**Status:** implemented · **Date:** 2026-06-01

## What it does

`coder-eval run` accepts a repeatable `-D path=value` (alias `--set`) flag that
overrides any field on the **resolved** `TaskDefinition` tree:

```bash
coder-eval run tasks/foo.yaml \
  -D agent.model=claude-opus-4-8 \
  -D run_limits.max_turns=30 \
  -D agent.sdk_options.effort=high \
  -D sandbox.docker.network=none
```

The dotted path is validated against the Pydantic schema at parse time (a typo
fails fast with a "did you mean?" suggestion), and the value is applied via a
nested-patch field-merge followed by re-validation through the model
constructor.

Only two bespoke flags survive as **active thin aliases** that emit the
equivalent `-D` entry: `--model` (→ `agent.model`) and `--driver`
(→ `sandbox.driver`). Four legacy flags — `--permission-mode`, `--max-turns`,
`--task-timeout`, `--turn-timeout` — remain as **hidden deprecated aliases**:
they still work and route through the same `-D` engine path, but print a
one-line deprecation hint to stderr nudging you to the `-D` form. The remaining
four — `--allowed-tools`, `--disallowed-tools`, `--plugins`, `--sdk-option` —
have been **removed** outright; express those via `-D` (e.g.
`-D agent.sdk_options.effort=high`). There is one engine path for the aliases
and `-D` alike.

## Where it sits in the 5-layer merge

`-D` is **layer 5** (CLI/.env) — an inline, highest-precedence, anonymous
override applied *after* the 4-layer experiment merge (default experiment →
experiment defaults → task YAML → variant) produces the resolved task.

```
default.yaml → experiment defaults → task YAML → variant → [ .env defaults < -D/aliases ]
                                                            └────────── layer 5 ──────────┘
```

Within layer 5, `.env` defaults (`DEFAULT_AGENT_MODEL`, `DEFAULT_PERMISSION_MODE`,
`DEFAULT_MAX_TURNS`) are the lowest contributor; a `-D`/alias on the same path
wins.

## Syntax & scope

- **Roots:** only `agent`, `run_limits`, and `sandbox` are overridable. Other
  top-level `TaskDefinition` fields (`initial_prompt`, `success_criteria`, …)
  are out of scope.
- **Value parsing:** values are YAML-scalar parsed, so `30` → int, `true`/`false`
  → bool, `null` → None, `[Read,Write]` → list. The YAML-1.1 truthy aliases
  (`on`/`off`/`yes`/`no`/`y`/`n`, case-insensitive) are kept as **strings** to
  avoid silently coercing e.g. `-D agent.model=on` to `True`.
- **Leaf-set deep-merge:** a path like `agent.sdk_options.effort=high` merges
  into the existing `sdk_options` dict, preserving sibling keys. Likewise
  `sandbox.docker.network=none` keeps the default `image`.
- **Free-form dicts:** paths below a `dict[str, Any]` field (e.g.
  `agent.sdk_options.*`) accept any key; the key's validity is enforced when the
  container is reconstructed (e.g. `sdk_options` keys must be valid, non-
  framework-managed `ClaudeAgentOptions` fields).
- **Whole-list replacement only:** `-D agent.allowed_tools=[Read,Write]` replaces
  the list. There is no list-index (`[0]`) or append (`[+]`) syntax.

## Alias table

The only surviving task-config aliases:

| Flag | Override path |
|------|---------------|
| `--model` | `agent.model` |
| `--driver` | `sandbox.driver` |

Everything else is expressed via `-D`. These four remain as **hidden deprecated
aliases** (still work, warn to stderr, route through `-D`):

| Deprecated alias | Equivalent `-D` |
|------|---------------|
| `--permission-mode plan` | `-D agent.permission_mode=plan` |
| `--max-turns 30` | `-D run_limits.max_turns=30` |
| `--task-timeout 600` | `-D run_limits.task_timeout=600` |
| `--turn-timeout 120` | `-D run_limits.turn_timeout=120` |

These four are **removed** — only the `-D` form works:

| Removed flag | Equivalent `-D` |
|------|---------------|
| `--allowed-tools Read,Write` | `-D agent.allowed_tools=[Read,Write]` |
| `--disallowed-tools Bash` | `-D agent.disallowed_tools=[Bash]` |
| `--plugins '[…]'` | `-D agent.plugins=[…]` |
| `--sdk-option effort=high` | `-D agent.sdk_options.effort=high` |

`--type` stays a dedicated flag (it re-parses the agent discriminated union) and
is injected into the agent patch; an explicit `-D agent.type=…` wins over it.

## Collision policy

A path set by **both** an alias and `-D` (or by **two** `-D` entries) is a hard
error at the CLI boundary — it never silently last-wins:

```
$ coder-eval run … --model opus -D agent.model=sonnet
Error: 'agent.model' set by both --model and -D; specify it once
```

Repeated `-D` on the same path is likewise a hard error ("set by -D more than
once").

## Lineage / provenance

Every applied override is recorded in the task's config lineage. CLI/`-D` entries
read `source="cli"` with `source_detail="-D <path>"`; `.env`-sourced defaults read
`.env DEFAULT_*`. This shows up in per-task config records and reports.

## Implementation

`-D` is just **layer 5** of the single generic merge resolver — see
[2026-06-01-declarative-merge-strategies.md](2026-06-01-declarative-merge-strategies.md).
`apply_overrides` groups the dotted overrides into one nested patch per root and
calls `config_merge.resolve_root` with a lineage-silent value seed (the resolved
model) + the recording `-D` layer, so each field obeys the **same** per-field
merge strategy at `-D` as at the variant layer (append fields append, etc.).

- `src/coder_eval/orchestration/config_merge.py` — the generic resolver
  (`merge_layers`, `resolve_root`, `validate_paths`, `MergeError`). Path
  validation (the "did you mean?" walk) lives here now.
- `src/coder_eval/orchestration/overrides.py` — the CLI-free layer-5 wrapper
  (`parse_scalar`, `parse_override`, `apply_overrides`). The old hand-rolled
  `validate_override_path` / `deep_merge` / `build_nested` / `_ROOT_MODELS` are
  gone — folded into `config_merge`.
- `src/coder_eval/orchestration/experiment.py::_apply_cli_overrides` — builds a
  per-path `.env` detail map and delegates to `apply_overrides` (no
  write-then-relabel lineage pass; provenance is intrinsic to each layer).
- `src/coder_eval/cli/run_command.py::_build_overrides` — the single CLI
  translation point for alias flags + `-D`/`--set`; path validation delegates to
  `config_merge.validate_paths`.
