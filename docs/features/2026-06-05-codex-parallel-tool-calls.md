# Codex parallel tool calls — the model-level flag is the gate (and why it's off)

**Status:** empirical finding (patched binary built & run) · **Date:** 2026-06-05
**Context:** Follow-up to the Codex `messages` reconstruction work
([2026-06-04-codex-message-reconstruction.md](2026-06-04-codex-message-reconstruction.md)).
Out of the box, gpt-5-codex emits exactly **one tool call per generation**, so
the API-parallel message shape (N `tool_use` blocks in one `AssistantMessage`)
never appears. This records *why*, and what it means for coder_eval.

## Two notions of "parallel"

1. **API-parallel** — multiple `tool_use` blocks in **one** model generation.
   This is the shape that collapses into a single `AssistantMessage` with N
   `tool_use` blocks. gpt-5-codex does not produce it out of the box because the
   request carries `parallel_tool_calls: false`.
2. **Wall-clock-parallel** — tool calls from **different** generations whose OS
   processes overlap in time (`unified_exec` background execution). This is the
   only concurrency gpt-5-codex actually ships out of the box, and it's a
   timestamp-overlap signal, not a message-grouping concern.

## The empirical test

The flag is computed client-side in Codex (`parallel_tool_calls` derived from
the per-model `supports_parallel_tool_calls`, resolved from OpenAI's remote model
catalog). On an Azure / API-key path the catalog is unavailable, so Codex falls
back to a hardcoded `supports_parallel_tool_calls: false`, and the field is not
config-overridable — effectively a compile-time `false` in the binary. The
serving endpoint merely honors whatever value Codex sends; there is no Azure-side
toggle that rewrites an inbound request body.

To settle whether gpt-5-codex *can't* emit parallel calls or merely *isn't asked
to*, we built a patched Codex binary from tag `rust-v0.134.0` (the version the
`openai_codex` SDK bundles) forcing `parallel_tool_calls: true`, and ran
`tasks/codex_parallel_single_gen.yaml` with `CODEX_DEBUG_EVENTS=1`.

**The flag is the only gate.** 11 of 27 generations emitted two
`commandExecution` calls before a single `thread/tokenUsage/updated` — the
genuine API-parallel shape. Unpatched it *never* occurs; patched it occurs
constantly. So our message-grouping branch in `codex_agent.py` is **reachable,
not dead**, when the flag is set.

**But forcing it on breaks the agent loop.** The task failed (turn timeout after
27 generations / 38 commands). Every command returned `exit_code=0` with correct
output, yet the model emitted **zero `agentMessage` items** and **never attempted
a file write** — it re-issued the same two read-only probes turn after turn,
making no forward progress. It never integrated "I have both answers → write the
files." (We proved the behavior, not the exact internal cause.) So OpenAI's
catalog gates `parallel_tool_calls` off for this model as a **deliberate
correctness gate**, not an arbitrary default.

This is **per-model**, not an API/reasoning-model ban: Codex's own catalog ships
other reasoning models (the Bedrock `gpt-5.4` / `gpt-oss` entries) with
`supports_parallel_tool_calls: true`, and the tool executor is parallel-capable
across the board.

## Conclusion for coder_eval

- With the stock binary, gpt-5-codex's transcript is always one tool call per
  generation → one `tool_use` block per `AssistantMessage`. Correct and expected,
  not a reconstruction bug.
- The API-parallel grouping logic in `codex_agent.py` (record `tool_use` at
  `item/started`, flush the message at `thread/tokenUsage/updated`, patch
  `is_error` at `item/completed`) is correct by construction and provably
  collapses a multi-tool generation into one message — exercised directly with
  the patched binary (11/27 generations grouped correctly). The branch is
  *latent* under the stock binary, not wrong; no change needed.
- The concurrency gpt-5-codex actually exhibits is `unified_exec` background
  overlap across generations. Surfacing it (if desired) is a wall-clock
  interval-overlap computation over `CommandTelemetry` timestamps, not a
  message-grouping change.

## Artifacts

- Test tasks: `tasks/codex_parallel_commands.yaml`,
  `tasks/codex_parallel_single_gen.yaml`.
- Source inspected: `github.com/openai/codex` `codex-rs/` (`rust-v0.134.0`).
