# Codex cache-write token accounting + gpt-5.4 / gpt-5.5 pricing

**Status:** implemented
**Date:** 2026-06-05
**Scope:** `agents/codex_agent.py`, `proxy/pricing.py`, `evalboard/lib/pricing.ts`
**Related:** [2026-06-05-codex-parallel-tool-calls.md](2026-06-05-codex-parallel-tool-calls.md)
(the investigation that moved Codex tasks onto `gpt-5.5`).

## Problem

Two issues surfaced while validating Codex token telemetry against the evalboard
cost view:

1. **The cost simulator was blank for Codex runs.** The evalboard hides the
   cost/thinking simulator whenever the run's model is not in the pricing table
   (`resolvePricing()` returns `null` → `buildThinkingModel()` returns `null` →
   the section is not rendered). `gpt-5.4` and `gpt-5.5` were missing from both
   the Python pricing table and its TypeScript mirror, so every run on those
   models rendered no cost.

2. **Codex's "fresh" prompt tokens were bucketed unlike Anthropic's.** The Codex
   SDK reports `input_tokens` *inclusive* of the cached prefix, plus
   `cached_input_tokens`. We previously stored the fresh slice
   (`input_tokens − cached`) in `TokenUsage.input_tokens`. That priced correctly
   but did not mirror how Anthropic reports a cached prompt, and it left the
   per-message `cache_creation` field empty — starving the evalboard's
   cache-cascade simulator of the cache-write mass it models.

## How OpenAI prompt caching is billed (the key fact)

OpenAI prompt caching is automatic and **bills only on cache *reads***:

| Bucket | gpt-5.5 rate | Notes |
|---|---|---|
| Fresh (uncached) input | $5.00 / 1M | full input rate |
| Cache **read** (hit) | $0.50 / 1M | 90% discount |
| Cache **write** | **$0.00** | not metered — populating the cache is free |
| Output | $30.00 / 1M | |

There is **no separate cache-write charge** and no cache-write token count in the
usage payload. This is the structural difference from Anthropic, which bills
cache *creation* at 1.25× and reports it as its own `cache_creation_input_tokens`
field.

A consequence worth internalizing: for OpenAI, the tokens you pay full input rate
for are exactly the *fresh* tokens, and those fresh tokens are also the ones
written to the cache for the next turn. So **fresh == cache-write == `input −
cached`**, and pricing them at the input rate is correct.

## Change 1 — Codex cache-write token bucketing

`CodexAgent` now buckets Codex/OpenAI usage to mirror Anthropic's cached-prompt
accounting, at both emission sites (`_token_usage_from_sdk` for the turn-level
`TokenUsage`, and `_flush_message` for per-message `AssistantMessage`s):

```
input_tokens                = 0
cache_creation_input_tokens = input_tokens(raw) − cached   # fresh == cache write
cache_read_input_tokens     = cached
```

- **Cost is unchanged.** `calculate_cost` prices `cache_creation_tokens` at
  `cache_write_per_mtok`, and the pricing table sets `cache_write == input rate`
  for every OpenAI model. So `(input − cached) × $5 + cached × $0.50` is computed
  identically whether the fresh slice rides the `input` or the `cache_creation`
  bucket. A regression test (`test_cache_write_reattribution_is_cost_neutral`)
  and an invariant test (`test_openai_cache_write_rate_equals_input_rate`) lock
  this in.
- **The evalboard now surfaces cache-write tokens** for Codex. The per-message
  `cache_creation_tokens` field is populated (on the first sub-message of each
  generation; follow-up emissions carry 0 to avoid double-counting per
  `message_id`), so the cost simulator's cascade has real data instead of zeros.
- **Edge cases** (covered by `TestCodexCacheWriteBucketing`): a cold cache
  (`cached = 0`) makes the whole prompt cache-write; a fully cached prompt
  (`cached = input`) yields zero cache-write; `cached > input` clamps to zero.

### Why `input_tokens = 0` and not "split"

When prompt caching is active, every uncached token processed is also written to
cache — there is no "input that is neither cached nor written." So the genuinely
brand-new-and-not-cached bucket is empty, and `input_tokens = 0` is the faithful
representation (the same shape Anthropic emits for a cached turn).

### Budget note (intentionally unchanged)

Run-time token caps (`max_input_tokens` / `max_total_tokens`) read
`input_tokens`, not `cache_creation`. Moving Codex's fresh tokens into
`cache_creation` therefore means those caps no longer count Codex's fresh prompt
input. This was a deliberate, scoped decision: **no Codex task uses token-count
caps** (the only task that sets `max_input_tokens` is a `claude-code` smoke test,
and the `experiments/default.yaml` caps are commented out), and `max_usd` is
unaffected because cost is unchanged. If a Codex task ever needs token-count
budgets, `orchestrator._check_run_limits` should count `cache_creation` for the
Codex agent.

## Change 2 — gpt-5.4 / gpt-5.5 pricing

Added to both the authoritative Python table (`proxy/pricing.py::_PRICING`) and
its evalboard mirror (`evalboard/lib/pricing.ts::PRICING`), keeping
`cache_write == input` per the OpenAI convention above:

| Model | input | output | cache-write | cache-read |
|---|---|---|---|---|
| `gpt-5.4` | $2.50 | $15.00 | $2.50 | $0.25 |
| `gpt-5.5` | $5.00 | $30.00 | $5.00 | $0.50 |

The two tables are kept in lockstep by `pricing-parity.test.ts`, which fails the
build on drift. With these entries present, the evalboard prices `gpt-5.4` /
`gpt-5.5` runs and the cost simulator renders.

> **Long-context surcharge (not modeled):** gpt-5.5 bills prompts over 272K
> input tokens at 2× input / 1.5× output for the rest of the session. The flat
> table does not encode this; cost for very-large-context runs will read low.

## Tests

- `tests/test_codex_agent.py::TestCodexCacheWriteBucketing` — turn-level bucketing
  (cold/partial/full cache, clamp, cost), per-message attribution, empty-usage.
- `tests/test_proxy_unit.py::TestPricingCalculation` — `gpt-5.4` / `gpt-5.5`
  rates, the `cache_write == input` invariant across OpenAI models, and
  cost-neutrality of the re-attribution.
- `evalboard/lib/__tests__/pricing-parity.test.ts` — Python↔TS table parity.
