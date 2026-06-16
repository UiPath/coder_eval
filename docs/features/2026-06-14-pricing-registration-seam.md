# Pricing registration seam for plugin-contributed model rates

**Status:** implemented · **Date:** 2026-06-14

## What it does

`coder_eval.proxy.pricing` ships a built-in rate card (`_PRICING`) for the
Anthropic and OpenAI models the framework prices directly. Out-of-tree agent
plugins now run their own models (e.g. the `coder_eval_uipath` Delegate agent
runs `virtuoso-1-5` / `gemini-3-5-flash`), whose rates do not belong in base.

`register_pricing()` is the seam that lets a plugin contribute those rates at
load time without editing base:

```python
from coder_eval.proxy.pricing import ModelPricing, register_pricing

register_pricing({"virtuoso-1-5": ModelPricing(0.95, 4.0, 0.0, 0.16)})
```

Registered rates are kept in a `_REGISTERED_PRICING` overlay that
`calculate_cost` consults *before* the built-in table. The lookup uses an
explicit `is not None` check rather than truthiness so a later `__bool__` on
`ModelPricing` (or a type change) can't make a falsy-but-present rate fall
through to the built-in table:

```python
key = _normalize_model(model)
registered = _REGISTERED_PRICING.get(key)
pricing = registered if registered is not None else _PRICING.get(key)
```

Every existing `calculate_cost` consumer (proxy server, Claude/Codex agents)
picks up plugin rates transparently — the function signature is unchanged.

## The anti-shadow rule

Registration is **idempotent** for an identical rate but **raises** on a
*conflicting* rate for a key that already exists in either the overlay or the
built-in table:

```python
register_pricing({"acme-1": ModelPricing(1, 2, 3, 4)})
register_pricing({"acme-1": ModelPricing(1, 2, 3, 4)})  # no-op
register_pricing({"acme-1": ModelPricing(9, 2, 3, 4)})  # ValueError
register_pricing({"claude-opus-4-8": <different rate>}) # ValueError — can't reprice a base model
```

This mirrors `AgentRegistry`'s anti-shadow rule: plugin load order can never
silently change a model's price, so cost numbers stay reproducible. The check
relies on `ModelPricing` being a `@dataclass(frozen=True)`, so `==` is
structural.

Registration is expected once, at plugin-load / CLI-init time (single-threaded);
it is not designed for concurrent mutation.

## How plugins use it

A plugin's `coder_eval.plugins` entry-point `register()` hook calls
`register_pricing()` alongside registering its agent. Base ships **no** plugin
rates; the plugin owns its rate card. See the BYOA plugin SPI
(`2026-06-11-byoa-agent-plugin-spi.md`) for the registration lifecycle.
