# Turn Counting — The Visible-Events Model

How "turns" are counted across the orchestrator, reports, and evalboard.
One definition, applied uniformly.

## What a "turn" means

A turn is **one entry rendered in the Turn timeline**:

- Each tool invocation contributes 1.
- The agent's final text reply (when present) contributes 1.

That's it. The displayed turn count equals the number of rows a user
would see if they expanded the Turn timeline section on the task
detail page.

```
displayed_turns = num_tool_calls + (1 if final_reply else 0)
```

## Why not use the SDK's `num_turns`?

`ResultMessage.num_turns` from `claude-agent-sdk` counts assistant
*messages*. That's not the same as visible events:

- A single assistant message can pair a `tool_use` block with trailing
  text — that's **1 SDK turn but 2 visible events**.
- A single assistant message can carry multiple `tool_use` blocks
  (parallel tool use) — **1 SDK turn but N visible events**.

So `num_turns` drifts from "what the user can point at in the
timeline" in ways that depend on how the model packed content into
messages. Three concrete reasons we prefer visible events:

1. **Predictability.** `visible = tools + reply` is a fixed formula.
   The user looking at the dashboard can always derive it from what
   they see; they never wonder "why does the count not match the
   rows?".
2. **Robust to SDK accounting changes.** If a future Claude version
   bundles more aggressively or less, `num_turns` shifts while the
   agent's actual behavior stays identical. Visible-events stays
   stable.
3. **`expected_turns` reads naturally.** Setting `expected_turns: 3`
   means "I expect ≤3 agent actions." Not "≤3 inference API calls."
   The first matches how users budget; the second is an engineering
   metric.

`num_turns` (Python: `TurnRecord.num_turns`, TypeScript:
`total_turns` / `totalTurns`) is kept in the data layer for cost /
debug surfaces that care about API call counts. It's just not the
headline.

## Where it lives in code

| Layer | Function | File |
|---|---|---|
| Python helper | `visible_turn_count(result)` | `src/coder_eval/reports_stats.py` |
| Python helper | `has_final_reply(result)` | `src/coder_eval/reports_stats.py` |
| Python overage | `expected_turns_overage(result)` | `src/coder_eval/reports_stats.py` |
| Orchestrator warning | `_check_expected_turns` | `src/coder_eval/orchestrator.py` |
| TS helper | `displayedTurns(actualCommands, hasFinalReply)` | `evalboard/lib/turns.ts` |
| Row-level field | `has_final_reply` on `task_results[*]` | emitted by `eval_result_to_task_dict` in `reports_experiment.py` |

All "Turns" surfaces in evalboard — grid sort, grid cell, trends per-task
cell, detail-page `TURNS` stat, Turn timeline header — call
`displayedTurns()`. The Python orchestrator warning and report badge
compare against `visible_turn_count()`. Same rule, two languages.

## How `has_final_reply` is detected

Walk `result.turns` from the end looking for a `TurnRecord` whose
`result_summary.result` is a non-empty string. That's the agent's final
text answer from the SDK's `ResultMessage`. Walking from the end means
a crashed earlier turn never shadows a real final reply on a later
iteration.

```python
def has_final_reply(result: EvaluationResult) -> bool:
    for t in result.turns:
        if t.result_summary is not None:
            r = t.result_summary.result
            if isinstance(r, str) and r.strip():
                return True
    return False
```

Per-task `task.json` always carries this signal (it's persisted on
every `TurnRecord`). Run-level `run.json` carries it as a row-level
boolean (`has_final_reply: bool`) emitted by
`eval_result_to_task_dict`. Evalboard's `readTaskDetail` overrides the
row-level boolean from `finalAssistantText != null` so the detail page
is always self-consistent even on legacy runs whose `run.json`
predates the field.

## Worked examples

For a task with `expected_turns: 3`:

| Conversation shape | tools | reply | visible turns | warns? |
|---|---|---|---|---|
| Query → reply only ("What is 2+2?") | 0 | yes | 1 | no |
| Query → 1 tool → reply | 1 | yes | 2 | no |
| Query → 4 tools → reply (fibonacci) | 4 | yes | 5 | yes (5 > 3) |
| Query → 4 tools, crashed before reply | 4 | no | 4 | yes (4 > 3) |
| Query → 1 parallel-tool turn (3 tools) → reply | 3 | yes | 4 | yes (4 > 3) |

The SDK's `num_turns` might read 5, 5, or 2 for these cases depending
on message packing — we don't surface that distinction.

## Subagent calls count as one turn

When the agent invokes a `Task` (or `Agent`) tool to delegate to a
subagent, that delegation contributes **1** visible turn — the same as
any other tool call. The subagent's internal conversation (which may
itself contain many tool calls and turns) is opaque to the parent and
is not expanded into the parent's timeline.

Example: parent does Read → Task(subagent runs 20 internal turns) →
reply. Visible turns = 3.

This is usually the right level of abstraction for `expected_turns`:
the budget reads as "how many moves should the agent take to solve
this?", treating delegation as a single move. If you care about the
subagent's internal cost, use `max_input_tokens` / `max_output_tokens` /
`max_usd` instead — the SDK rolls subagent token usage into the
parent's totals, so token / USD budgets catch runaway subagents even
when `expected_turns` doesn't.

The evaluator-side `agent_judge` criterion runs through
`SubAgentRunner` (see `src/coder_eval/evaluation/sub_agent.py`),
**not** the agent SDK's `Task` tool, so it doesn't appear in the
subject agent's `commands` or contribute to its turn count at all.

## Legacy data caveat

`run.json` files written before `has_final_reply` was added carry
`has_final_reply: undefined`, which evalboard reads as `false`. On
those runs the grid/trends Turns cell reads 1 less than the detail
page (which derives the signal from per-task `task.json` directly).
Re-running `coder-eval` over an old run regenerates the field and
closes the gap. See the consistency matrix in PR #299.

## Related

- [Run Limits](2026-05-11-run-limits.md) — `expected_turns` lives in
  the same `run_limits` block as the structural and budget caps; this
  doc covers only the counting rule.
