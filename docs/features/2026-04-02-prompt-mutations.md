# Prompt Mutations

Prompt mutations are ordered transforms applied to a task's `initial_prompt` at variant resolution time.
They enable A/B testing of prompt phrasing without duplicating task definitions.

## Mutation Types

| Type | Fields | Description |
|------|--------|-------------|
| `prefix` | `content`, `separator` (default `\n\n`) | Prepends text before the prompt |
| `suffix` | `content`, `separator` (default `\n\n`) | Appends text after the prompt |
| `replace` | `pattern`, `replacement`, `regex` (default `false`) | Find/replace — literal string or regex |
| `template` | `variables` (dict) | Substitutes `{variable_name}` placeholders |
| `rephrase` | `instructions`, `model`, `temperature`, `max_tokens` | Rewrites the prompt via UiPath LLM Gateway |

Mutations are applied sequentially — each operates on the result of the previous.

## Configuration

Mutations are configured in experiment YAML on `ExperimentVariant` and/or `ExperimentDefaults`:

```yaml
experiment_id: prompt-style-comparison

defaults:
  prompt_mutations:
    - type: prefix
      content: "Follow best practices."  # applied to all variants

variants:
  - variant_id: baseline
    # No mutations — original task prompt as-is

  - variant_id: step-by-step
    prompt_mutations:
      - type: prefix
        content: "Think step by step before writing any code."

  - variant_id: concise
    prompt_mutations:
      - type: replace
        pattern: "Create"
        replacement: "Write a minimal"
      - type: suffix
        content: "Keep it under 20 lines."
```

### Full prompt replacement

Variants can also replace the entire prompt instead of mutating it:

```yaml
variants:
  - variant_id: custom-prompt
    initial_prompt: "Completely different prompt text."

  - variant_id: custom-from-file
    initial_prompt_file: prompts/custom.txt  # relative to experiment YAML
```

`prompt_mutations`, `initial_prompt`, and `initial_prompt_file` are **mutually exclusive** on a variant.

## Resolution Order

Prompt mutations are applied during the **RESOLVE** phase, after file resolution and before CLI overrides:

```
Base initial_prompt (from task YAML)
  → experiment.defaults.prompt_mutations (if any)
  → variant.prompt_mutations (if any)
  → final TaskDefinition.initial_prompt
```

If a variant sets `initial_prompt` or `initial_prompt_file`, all mutations are skipped — the replacement is used directly.

By the time the orchestrator runs, `TaskDefinition.initial_prompt` already contains the final text.

## Rephrase Mutations

The `rephrase` type sends the current prompt to an LLM with rewriting instructions. It uses the same UiPath LLM Gateway + LangChain integration as `LLMReviewer`.

```yaml
- type: rephrase
  instructions: "Rewrite in a formal, specification-like tone."
  model: "anthropic.claude-sonnet-4-6"  # optional, uses default gateway model
  temperature: 0.2                       # optional, default 0.2
```

The rephrase callback is created lazily — LLM Gateway credentials are only required when a rephrase mutation is present. LLM clients are cached per `(model, temperature, max_tokens)` tuple.

**Note:** Rephrase is inherently non-deterministic. Use low temperature for more consistent results.

## Lineage Tracking

Mutated prompts are recorded in config lineage with `source: "mutation"` and a detail string describing the operations applied (e.g., `"2 ops (prefix, suffix) from experiment-defaults + variant 'step-by-step'"`).
