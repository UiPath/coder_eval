# Proxy token attribution

**Date:** 2026-05-28
**Audience:** developers / operators of `coder-eval`

Token usage is attributed per consumer — the main coding agent versus each
judge criterion — across all three API backends, with a Proxy-only
reconciliation invariant that proves no tokens were lost or double-counted.

This replaces three hand-rolled snapshot/diff sites with one context manager,
captures judge usage uniformly on every route, and keeps judge tokens out of
the main agent's bill.

## What it does

- **`measure_proxy(proxy)`** (`proxy/server.py`) is the single home for the
  "snapshot the proxy usage, do some work, take the delta" pattern. It yields a
  getter that returns the attributed `TokenUsage` for the work done inside the
  block (or `None` when the proxy is absent or the window carried no traffic).
  The three consumers — the main-agent turn (`orchestrator._communicate_with_retry`),
  the `llm_judge` API call, and the `agent_judge` sub-agent run — all route
  through it. It is correct **only because work on a given proxy is serialized**
  (one turn or one judge call at a time); cross-task isolation is guaranteed by
  each `Orchestrator` owning its own per-task proxy.

- **`JudgeCriterionResult.token_usage`** is populated on every route when the
  model reports usage (see the matrix below), kept distinct from the main
  agent's `EvaluationResult.total_token_usage`.

- **Reconciliation** (`orchestrator._reconcile_proxy_usage`) cross-checks the
  attributed total against the proxy's independent counter and logs the result.

## Per-backend matrix

Two distinct concerns: **entanglement** (judge tokens polluting the main total)
and **judge-usage capture** (`JudgeCriterionResult.token_usage` being populated
at all).

Entanglement is structurally possible **only on ProxyRoute** — the one route
where the main agent and the judges share one in-process counter
(`proxy.usage`). Direct/Bedrock have no shared accumulator, so there is nothing
to disentangle. Per-window attribution via `measure_proxy` resolves it.

| Route | main-agent usage | `llm_judge.token_usage` | `agent_judge.token_usage` |
|-------|------------------|-------------------------|---------------------------|
| DirectRoute (Anthropic) | SDK `ResultMessage` | Anthropic response `usage` | sub-agent `turn.token_usage` |
| BedrockRoute (direct) | SDK `ResultMessage` (native Bedrock transport) | Bedrock `/invoke` JSON `usage` | sub-agent `turn.token_usage` |
| ProxyRoute (LLMGW) | proxy delta | proxy delta (preferred) | proxy delta |

### Why direct-Bedrock main-agent usage is correct

Every "cannot parse Bedrock's event-stream usage" caveat in the code is scoped
to the **proxy/LLMGW path**: the proxy exposes an Anthropic-typed endpoint while
LLMGW returns Bedrock-origin SSE, so the bundled CLI — running in Anthropic mode —
mis-parses the usage shape and `ResultMessage` carries zeros (hence the proxy
delta is the recovery). On a **native** `BedrockRoute` the agent sets
`CLAUDE_CODE_USE_BEDROCK=1` and the CLI uses its first-class Bedrock transport,
which parses usage natively; the real `ResultMessage` usage flows onto
`TurnRecord.token_usage`. There is no compensating workaround anywhere for the
direct-Bedrock path (unlike the proxy path), confirming none is needed. Cost may
be approximate or absent on Bedrock, but input/output token counts arrive.

### Proxy-delta-over-response precedence

On ProxyRoute, `llm_judge` may have *both* a proxy delta and a usage block
echoed in the response body. The **proxy delta wins** (`proxy_delta() or
response_usage`) — it is the billed truth. On Direct/Bedrock the proxy is absent
and the response-reported usage is used.

`token_usage is None` means the backend surfaced no usage — kept distinct from a
zero `TokenUsage`, which matters for reconciliation (below).

## Reconciliation guarantee

At the end of a run, on ProxyRoute only, `_reconcile_proxy_usage` computes:

```
attributed = total_token_usage (main agent) + Σ judge token_usage
gap        = proxy.usage_total().total_tokens − attributed.total_tokens
```

- `gap == 0` → INFO: "proxy usage reconciled".
- `gap != 0` → WARNING with a per-field breakdown (total / main / judges).

**It is diagnostic only.** A non-zero gap logs a WARNING and nothing more — it
**never raises and never fails a run**, and the whole body is wrapped so a bug
in reconciliation itself can't abort a run. Rationale: a token-accounting gap is
an observability signal, not an eval-correctness failure; failing a run would
discard a valid task result over billing arithmetic.

### Proxy-only limit

Reconciliation needs the proxy's independent counter as ground truth. On
Direct/Bedrock there is no such counter, so the SDK/response numbers are trusted
by construction (the matrix above establishes they are correct). This is a
property of the guarantee *mechanism*, not a gap in attribution.

### Accepted under-attribution tolerance

A non-zero gap is expected and acceptable for:

- **Agent startup / MCP-server probing** — handshake traffic not tied to any turn.
- **Dropped retried partials** — on a retried iteration, older partials keep
  their zero `token_usage`; only the latest partial (or the returned turn) gets
  the iteration's proxy delta.
- **Judges with `token_usage is None`** — unknown usage contributes 0 to the
  attributed sum, so those tokens surface as part of the gap.

No configurable threshold is added (YAGNI): `gap != 0 → WARNING` is sufficient
and can grow a threshold later if a billing-exact consumer ever appears. Only
input+output tokens (`TokenUsage.total_tokens`) participate in the gap; cache
tokens are outside it by design.

## How `token_usage` relates to `total_token_usage`

- `EvaluationResult.total_token_usage` is the **main agent's** bill — the sum of
  every iteration's `TurnRecord.token_usage`. Judge/sub-agent usage is
  intentionally **excluded**; it represents eval-machinery overhead, not the
  agent under test.
- Each `JudgeCriterionResult.token_usage` carries that judge's own bill.
- In simulation `every_turn`/`both` mode the per-turn check replaces
  `success_criteria_results` wholesale, so the orchestrator keeps a dialog-wide
  ledger keyed by `(position, criterion_type)` and rewrites each judge result's
  `token_usage` to its cumulative dialog total — so two same-type judges stay
  distinct and earlier judge calls aren't dropped.
- Reconciliation's `attributed` re-combines the two (`main + Σ judges`) purely to
  cross-check against the proxy's counter; it does not move tokens between them.

## Suggested live smoke check (non-blocking)

The direct-Bedrock main-agent reasoning above is corroborated by code but worth
confirming empirically once: run a single trivial task on a native
`BedrockRoute` (`CLAUDE_CODE_USE_BEDROCK=1`) and confirm
`EvaluationResult.total_token_usage` carries non-zero input/output tokens. This
is a cheap confirmation, not a gate.
