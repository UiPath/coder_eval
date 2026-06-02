# Declarative per-field merge strategies + one generic resolver

**Status:** implemented · **Date:** 2026-06-01

## What it does

Config resolution used to have **two** independent merge implementations: the
4-layer experiment merge (`resolve_task_for_variant`) and the layer-5 `-D` merge
(`apply_overrides`). They drifted — the same field could merge differently
depending on which layer supplied its value (e.g. `-D` *replaced*
`env_passthrough_extra` while a variant *appended* it; `-D agent.system_prompt_file`
crashed where a variant cleared the sibling).

Now there is **one** generic resolver. Each config field declares *how it merges*
once, on the model, and the resolver applies that strategy identically at every
layer. Both paths call the same entry point:

```
config_merge.resolve_root(root, [Layer, Layer, ...], lineage=...)
```

`resolve_task_for_variant` (layers 1–4) and `apply_overrides` (layer 5) differ
only in *which `Layer` list they build* — never in how merge or reconstruction
happens.

### The unification invariant (architectural contract)

> The resolved value of any field on the three `-D`-reachable roots
> (`agent` / `run_limits` / `sandbox`) is a pure function of
> `(field strategy, ordered layer values)` — independent of *which* layer
> supplied a value.

Enforced executably by `tests/test_merge_unification.py`: for representative
patches, `resolve(P as a variant)` == `resolve(P as -D)` for every root. A
companion lineage-parity test asserts layer 5 preserves the layers-1–4 provenance
of every field it doesn't touch.

## Merge strategies

A field's strategy is read off its Pydantic `FieldInfo` by
`merge_strategy_of` (`coder_eval/models/merge_strategy.py`). Without an explicit
annotation it falls back to a **type-aware default**:

| Field type | Default strategy | Meaning |
|---|---|---|
| nested `BaseModel` (e.g. `sandbox.docker`, `python`, `node`, `limits`) | `deep` | recurse into the sub-model, honoring *its* fields' strategies |
| free-form `dict` (e.g. `agent.sdk_options`) | `deep` | recursive dict merge (sub-dicts merge, leaves replace) |
| `list` | `replace` | last layer wins |
| scalar | `replace` | last layer wins |

`MergeField(strategy=...)` overrides the default. It is only needed where the
default is wrong — in practice, **lists that should append**:

| Field | Strategy | Why |
|---|---|---|
| `sandbox.template_sources` | `append` | sources accumulate across layers |
| `sandbox.docker.env_passthrough_extra` | `append` | extras add to the default allowlist |
| `agent.claude_settings` (`str \| dict \| None`) | `deep` | explicit — the deep-dict branch merges dicts, a `str`/`None` replaces |
| every other list (`allowed_tools`, `disallowed_tools`, `plugins`, `ignore_patterns`, `setting_sources`, `mock_path_dirs`, `docker.env_passthrough`, `docker.extra_mounts`, `*.env_packages`) | `replace` | whole-list replacement |

**Lint rule CE014** requires every `list`-typed field on the three root models
(and nested sandbox sub-models) to declare a strategy explicitly — a list is the
one type whose `replace` default is easy to mean-otherwise. Nested-model and
free-form-dict fields may rely on the `deep` default (a plain `Field` is fine),
so the nested-replace regression is structurally impossible.

`"deep"` distinguishes two sub-cases by field type:
- **deep-model** (`docker`/`limits`/`python`/`node`): recurse into the nested
  model's fields, honoring their strategies (so `docker.env_passthrough_extra`
  appends while `docker.network` replaces).
- **deep-dict** (`sdk_options`, and `claude_settings` when its value is a dict):
  recursive dict merge; a non-dict value (`str`/`None`) replaces.

## Exclusion groups

A model can declare mutually-exclusive field groups via a class attribute:

```python
class BaseAgentConfig(BaseModel):
    _merge_exclusive_groups: ClassVar[...] = (("system_prompt", "system_prompt_file"),)
```

Before applying a layer's patch, the resolver clears the *other* members of any
group the layer sets (and drops their lineage). This makes
`-D agent.system_prompt_file=p` clear an inherited `system_prompt` uniformly at
every layer instead of crashing the exclusivity validator.

## Lineage

The walk emits dotted lineage (`agent.model`, `run_limits.max_turns`,
`sandbox.docker`, …) as a side effect, at a granularity that bounds the key set:
- `replace` / `append` → one entry at the field path.
- **deep-dict** → one entry per top-level sub-key (e.g. `agent.sdk_options.effort`).
- **deep-model** → one coarse entry at the model-field path (e.g. `sandbox.docker`),
  recording the highest-precedence layer that touched anything in the subtree.

The layer-5 value seed is **lineage-silent** (`record_lineage=False`): it
contributes values only, so the layers-1–4 provenance survives for every field
`-D`/`.env` doesn't touch. Provenance detail (`.env DEFAULT_*` vs `-D <path>`) is
intrinsic to each `Layer`, replacing the old write-then-relabel pass.

## Behavior changes (vs. the pre-refactor merge)

These were latent divergences; unification picks the more-correct behavior:
- **`-D` honors the field strategy.** `-D sandbox.docker.env_passthrough_extra`
  now **appends** (was replace); `-D agent.system_prompt_file` clears the sibling
  (was a crash). Tradeoff: a user cannot "replace, not append" an append field
  via `-D` — the deliberate price of one rule per field.
- **Nested-model sub-objects deep-merge** at layers 1–4 (was shallow whole-object
  replace — a variant setting `docker.network` no longer drops a lower layer's
  `docker.image`).
- **`sdk_options` nested-dict values merge recursively** at layers 1–4 (was shallow).

`template_sources` keeps its documented **task-first** append order (the task's
base templates, then experiment-defaults and variant overlays appended after —
"appended after task's base templates"). It is the one append field whose order
is NOT layer order, so the resolver re-adds it as dedicated synthetic layers in
task → exp-defaults → variant order rather than letting it ride the layer-ordered
dumps. (`env_passthrough_extra`, by contrast, appends in layer order.)

## Also folded through the engine (Phase 7)

Beyond the three `-D`-reachable roots, two more single-merge resolutions now flow
through `merge_layers` (they carried no divergence risk — this is consolidation):
- **`simulation`** — `merge_layers` over `SimulationConfig` (every field uses the
  type-aware default; the historical shallow all-replace is preserved). Required
  `persona`/`goal` still raise on construction; lineage stays a single coarse
  `simulation` entry crediting the most-specific source.
- **`pre_run` / `post_run`** — both declare `strategy="append"`; `post_run` adds
  `append_order="reverse"` (the `MergeField` hint added for this) so the task's
  commands run first and experiment-defaults cleanup runs last, while `pre_run`
  appends in layer order (exp-defaults setup first).

### Still hand-coded
- **`repeats`** — a scalar "last non-None wins, skipping the task layer" with a
  `<= 99` guard. Quirky enough (skips a layer) that routing it through the engine
  would obscure rather than clarify; left as-is.

## Implementation

- `coder_eval/models/merge_strategy.py` — `MergeField`, `merge_strategy_of`,
  `classify_annotation`, the type-aware default.
- `coder_eval/orchestration/config_merge.py` — `Layer`, `merge_layers`,
  `resolve_root` (the single entry point), `validate_paths`, `MergeError`.
- `coder_eval/orchestration/experiment.py::resolve_task_for_variant` — builds the
  layers-1–4 `Layer` lists per root.
- `coder_eval/orchestration/overrides.py::apply_overrides` — builds the layer-5
  `Layer` list (silent seed + `-D` patch).
- `tests/lint/rules/ce014_merge_strategy_declared.py` — CE014.

## Related

- [2026-06-01-generic-d-overrides.md](2026-06-01-generic-d-overrides.md) — the
  `-D`/`--set` CLI surface (layer 5).
- [2026-05-11-run-limits.md](2026-05-11-run-limits.md) — `run_limits` is now the
  only accepted shape for run-time caps (legacy `agent.*`/top-level timing shims
  removed in this change).
