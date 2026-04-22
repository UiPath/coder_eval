# Simulation: Multi-Turn User Dialog Evaluation

A feature for evaluating coding agents on tasks that are naturally conversational — clarifying questions, incremental requirements, mid-task corrections — rather than single fire-and-forget prompts.

When a task has an enabled `simulation` block, the orchestrator replaces its usual single-shot iteration loop with a dialog between the coding agent and a simulated user (a second LLM driven by a persona + goal). The coding agent never knows it's talking to a simulator.

**Design rationale:** see [2026-04-21-simulation-plan.md](./2026-04-21-simulation-plan.md).
**Full schema reference:** see [`../TASK_DEFINITION_GUIDE.md#simulation-multi-turn-user-dialog`](../TASK_DEFINITION_GUIDE.md#simulation-multi-turn-user-dialog).

---

## When to use it

Reach for simulation when any of these are true:

- The task's real-world workflow involves the user answering clarifying questions.
- Requirements are intentionally ambiguous and the agent should ask to narrow them down.
- You want to measure recovery behavior — can the agent course-correct after a mid-task user redirect?
- You need to benchmark "agreement with a picky stakeholder" vs. "agreement with a permissive stakeholder" (run as variants).

Reach for single-shot (no `simulation` block) when:

- The task is a well-specified, deterministic transformation (fix a bug, pass a test).
- You want fast, cheap, fully deterministic evaluation.
- Any variability in results would be noise, not signal.

## Minimal example

```yaml
task_id: clarify-requirements-smoke
description: Agent must ask for missing details before building.
initial_prompt: "I need a flow that processes invoices."

agent:
  type: claude-code

sandbox:
  driver: tempdir

success_criteria:
  - type: file_exists
    path: flow.json
    description: Produced flow file

simulation:
  enabled: true
  persona: |
    A non-technical BA. Mildly impatient but cooperative.
  goal: |
    Build a flow that (1) reads invoice PDFs from Outlook,
    (2) extracts vendor/amount/date, (3) posts to Google Sheets.
    Reveal (2) and (3) ONLY if the agent asks about them.
  max_turns: 10
  stop_token: "<<<DONE>>>"
```

The agent starts with `initial_prompt`. If it immediately builds a guess, criteria fail and the dialog continues. If it asks clarifying questions, the simulator answers per the goal + constraints, the agent iterates, and either the simulator emits `<<<DONE>>>` or the dialog hits `max_turns`.

### Pure-simulation mode (no `initial_prompt`)

`initial_prompt` is **optional** when `simulation.enabled: true`. Omit it to let the simulator generate the opening utterance as well — turn 1 of the dialog becomes a persona-driven opener instead of a pinned string:

```yaml
task_id: clarify-requirements-pure-sim
description: Agent must ask for missing details before building.
# No initial_prompt — simulator owns every user message including turn 1.

agent:
  type: claude-code
  system_prompt: |
    Load the uipath-flow skill before doing anything else.

sandbox:
  driver: tempdir

success_criteria:
  - type: file_exists
    path: flow.json
    description: Produced flow file

simulation:
  enabled: true
  persona: A non-technical BA, mildly impatient but cooperative.
  goal: |
    Build a flow that processes invoices end-to-end.
    Reveal details only when asked.
  max_turns: 10
  stop_token: "<<<DONE>>>"
```

Use this shape when the persona itself should decide how the user opens the conversation — e.g., measuring how much clarification the agent drives when even the opening ask is ambiguous. Put framework-level instructions ("load skill X first") on `agent.system_prompt` so the simulator's voice stays in character.

The opener call counts toward `simulator_input_tokens` / `simulator_output_tokens` like any other turn. If the simulator call fails on turn 1, the dialog aborts with `stop_reason: error` and `total_turns: 0` (the agent never runs).

## How termination works

Four stop conditions, checked in this order after every turn:

1. **All success criteria pass** AND `stop_on_criteria_pass: true` (requires `check_criteria: every_turn` or `both`).
2. **Simulator emits the stop token** (default `<<<END>>>`).
3. **`max_turns` reached** — hard cap on user↔agent exchanges.
4. **`max_total_tokens` exceeded** — optional dialog-wide budget covering both simulator and agent tokens.

Stop reason is recorded on `EvaluationResult.simulation.stop_reason` and rendered in the HTML report. Use it to tell "agent completed successfully" from "agent gave up" from "simulator cost-capped out."

## Variance: `n_trials`

Simulator output is stochastic even at temperature 0 (different sessions, different prompts). If you care about signal, set `n_trials` to 3–5 and look at pass-rate across trials rather than single-trial pass/fail.

Each trial becomes its own `ResolvedTask`:

- `task_id` suffixed `/trial-0`, `/trial-1`, …
- Separate run directory per trial, own `task.json` and `task.html`.
- `EvaluationResult.simulation.trial_id` / `n_trials` record the trial index.

Trials run in parallel (subject to `--max-parallel`). The batch summary aggregates trial-level pass rate; per-trial detail is available on disk.

## Variants: experimenting on the simulator itself

Because the simulator's persona and model dominate eval noise (per tau-bench lessons), it is often more useful to vary the simulator than the agent. Example experiment that measures agent robustness under two user personalities:

```yaml
experiment_id: persona-robustness
variants:
  - variant_id: terse_user
    simulation:
      persona: "You answer in at most one terse sentence."
  - variant_id: chatty_user
    simulation:
      persona: "You over-explain, ramble, and re-hash requirements."
```

The variant's `simulation` block is shallow-merged onto whatever the task provides, so only the overridden keys change.

## Security: reference solutions stay hidden

When a task has a `reference` solution, it is NEVER passed to the simulator — same posture as for the coding agent. Persona/goal/constraints go into the simulator's system prompt; nothing else. If you include criteria-specific guidance in the simulator prompt, keep it at a *requirements* level, not an *implementation* level.

## Debugging

- The full simulator system prompt is accessible via `UserSimulator.system_prompt` and is included in `task.log` at DEBUG level.
- Token accounting (`simulator_input_tokens`, `simulator_output_tokens`) shows up in the Simulation section of `task.html`.
- If the simulator repeatedly fails (`simulator_failures > 0` in telemetry), the most common cause is a route/model mismatch. The simulator uses the orchestrator's resolved `ApiRoute` (same `-b` flag as the coding agent) and lets Claude Code pick the effective model via `BedrockRoute.model` / env — verify those are set the way the coding agent expects.

## Cost knobs (roughly descending impact)

1. **`n_trials`** — linear cost multiplier. Start at 1; only crank up for variance-sensitive experiments.
2. **`max_turns`** — per-trial turn cap. Default 8 is deliberate; raise carefully.
3. **`check_criteria: every_turn`** — runs criteria after every agent response; cheap if criteria are file_exists, expensive if they run `pytest`.
4. **`max_total_tokens`** — emergency hatch for runaway dialogs. Pair with a turn cap, don't rely on it alone.

## Not in scope (yet)

- Multi-agent dialogs (agent ↔ agent).
- Human-in-the-loop mode (real users instead of a simulator).
- Simulator tool use (simulator producing pasted error messages, terminal output, etc.).
- Dialog-aware success criteria (e.g., "agent must ask ≥1 clarifying question"). Can be added later as new criterion types once the core loop stabilizes.
