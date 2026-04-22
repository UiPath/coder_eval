# Multi-Turn User Simulation for Interactive Coding Agents

**Status:** Implemented
**Branch:** `akshaya/simulation`
**Owner:** @akshaya.shanbhogue
**User-facing docs:** [2026-04-21-simulation-guide.md](./2026-04-21-simulation-guide.md) · [TASK_DEFINITION_GUIDE.md#simulation](../TASK_DEFINITION_GUIDE.md#simulation-multi-turn-user-dialog)

---

## 1. Problem Statement

`coder_eval` today is a **single-shot** evaluator: the full `initial_prompt` is handed to the coding agent, the agent works to completion, and success criteria are checked. If criteria fail, an outer loop sends deterministic (or LLM-reviewer-generated) feedback and retries.

This model breaks down for a growing class of real-world use cases where the coding agent is part of an **interactive dialog with a human user** — the agent asks clarifying questions, the user supplies incremental requirements, the user corrects course, etc. We cannot realistically evaluate these agents by cramming the entire user journey into one prompt, because:

- The agent never gets to ask clarifying questions (or if it does, they go unanswered).
- Behavior that depends on mid-task user redirection (common in UiPath flows, debugging sessions, refactoring walk-throughs) is untestable.
- Scoring "did the agent drive a good conversation?" is impossible when there's no conversation to score.

We need a way to evaluate coding agents **inside a simulated multi-turn dialog**, where a second LLM plays the role of the user.

## 2. Opportunities

- **Realistic eval signal.** Interactive benchmarks (tau-bench, SWE-agent interactive mode) consistently find that single-shot benchmarks over-estimate agent quality on ambiguous tasks. Simulation exposes weaknesses in clarification, grounding, and recovery.
- **Reuses existing infrastructure.** Sandbox, criteria checking, snapshotting, telemetry, experiment variants, and reporting all carry over unchanged. The new surface area is narrow: a user-simulator module + a revised inner loop.
- **Natural experiment axis.** Simulator model, persona, and temperature become new variant dimensions in the 5-layer merge — letting us measure things like "does agent X hold up under a terse user vs. a chatty user."
- **Cheap per-task breadth via trajectories.** Running `n_trials` dialogs per task gives a variance estimate for free, which the current loop doesn't produce.
- **Composable with the LLM reviewer.** The post-dialog reviewer can still score the full trajectory, now with richer conversational context to judge against.

## 3. Risks

| Risk | Mitigation |
|---|---|
| **Simulator dominates eval noise** (tau-bench's biggest lesson) | Pin simulator model + temperature=0 by default; require deterministic seed; report simulator quality as a first-class metric. |
| **Cost explosion** — N turns × N trials × agent + simulator tokens | Hard `max_turns` cap; `max_total_tokens` budget per dialog; early-stop when all criteria pass. |
| **Simulator leaks the reference/solution into the dialog** — invalidates the eval | Simulator prompt template must be constrained to persona + goal only; `reference` field stays hidden from the simulator (same security posture as today for the agent). |
| **Ill-defined termination** leads to infinite or truncated dialogs | Three stop conditions ANDed: explicit `<<<END>>>` token from simulator, `max_turns`, and optional "criteria pass ⇒ stop early." |
| **Non-determinism breaks regression testing** | Always record the full transcript; replay mode re-runs criteria against a frozen transcript without re-invoking the simulator. |
| **Schema bloat** — `TaskDefinition` is already dense | New functionality lives under a single optional `simulation:` block. Tasks without it stay single-shot and byte-identical. |
| **Conflation of dialog-turns vs. task-iterations** | Keep `max_iterations` = task attempts (default 1 in simulation mode); add `max_turns` = intra-dialog exchanges. Separate fields, separate semantics. |

## 4. High-Level Plan

### 4.1 Semantic model

- **Iteration (existing):** one full attempt at the task. Criteria check + optional retry with feedback. In simulation mode this typically equals one dialog.
- **Turn (new meaning in simulation mode):** one `(simulated_user_message, agent_response)` exchange inside a dialog. Bounded by `simulation.max_turns`.
- **Trial (new):** one sampled dialog trajectory. Multiple trials per task for variance. Runs in parallel within the existing batch orchestration.

Default `max_iterations = 1` when `simulation` is set. Retrying a failed *dialog* rarely adds signal — trials are the right knob for variance; iterations are kept for the edge case where deterministic per-iteration feedback is still wanted.

`initial_prompt` is **optional** when `simulation.enabled: true`. When present, it is pinned as turn 1's user utterance (simulator responds from turn 2). When absent ("pure simulation"), the simulator generates turn 1 itself from persona + goal; an opener-generation failure aborts the dialog with `stop_reason: error` before the agent ever runs. Framework-level instructions (skill loading, guardrails) belong on `agent.system_prompt` in pure-simulation mode so the simulator's voice stays in character.

### 4.2 New task schema

A single optional `simulation:` block. Its presence switches the orchestrator into dialog mode; its absence keeps today's single-shot behavior bit-for-bit.

```yaml
task_id: uipath_flow_clarify_requirements
description: Agent must clarify ambiguous requirements before building the flow.
max_iterations: 1                     # almost always 1 in simulation mode
tags: [simulation, uipath-flow]

# The agent's FIRST message in the dialog. The simulator responds from turn 2 onward.
# OPTIONAL in simulation mode: when omitted, the simulator generates turn 1 itself
# from its persona + goal (pure-simulation mode).
initial_prompt: "I need a flow that processes invoices."

simulation:
  enabled: true

  # --- Persona and goal ---
  persona: |
    You are a non-technical business analyst at a mid-size accounting firm.
    You know what you need the flow to DO but not how automation works.
    You are mildly impatient but cooperative.
  goal: |
    You want a UiPath flow that:
      1. Reads invoice PDFs from an Outlook folder.
      2. Extracts vendor, amount, date.
      3. Posts to a Google Sheet.
    You will NOT volunteer requirements 2 and 3 unless asked.
    You do not know what "MCP", "activity", or "trigger" mean.
  # Optional: rules the simulator must follow (anti-leak, tone, etc.)
  constraints:
    - "Do not mention Google Sheets until the agent asks about the destination."
    - "Never paste code. You cannot read code."
    - "If the agent goes silent for two turns, say 'are you still there?'."

  # --- Termination ---
  max_turns: 12                       # hard cap on user<->agent exchanges
  stop_token: "<<<END>>>"             # simulator emits this when satisfied
  stop_on_criteria_pass: true         # end dialog early when all criteria pass
  max_total_tokens: 150000            # safety budget across the whole dialog

  # --- Sampling ---
  n_trials: 3                         # independent dialogs per task
  parallel_trials: true               # run trials concurrently (default true)

  # --- Criteria timing ---
  check_criteria: end_of_dialog       # one of: end_of_dialog | every_turn | both
  # "every_turn" enables stop_on_criteria_pass; "end_of_dialog" is cheapest.

# Everything below is UNCHANGED from single-shot mode.
sandbox: {...}
success_criteria: [...]
agent: {...}
llm_reviewer: {...}
```

### 4.3 Code changes

**New module: `src/coder_eval/simulation/`**
```
simulation/
├── __init__.py
├── config.py        # SimulationConfig pydantic model (all the YAML above)
├── user_simulator.py # UserSimulator class — runs a tools-disabled Claude Code agent, owns turn state
└── termination.py   # Stop condition evaluation (token / turns / criteria / budget)
```

The simulator runs as a **tools-disabled Claude Code agent** — same `ApiRoute` as the coding agent, but with `allowed_tools=[]`, `plugins=None`, `setting_sources=[]`, and an ephemeral empty scratch cwd. It is pure text in, text out. This means `-b bedrock` / `-b proxy` / `-b direct` automatically cover the simulator too (no parallel backend stack), and multi-turn conversation history is handled by the SDK's session resume (the simulator LLM sees its own past utterances as assistant messages and the agent's replies as user messages without any role-inversion plumbing). Tests can bypass the SDK by passing an `agent_override` implementing the ``Agent`` ABC.

**Modified: `src/coder_eval/models/tasks.py`**
- Add `simulation: SimulationConfig | None = None` field to `TaskDefinition`.
- Validator: if `simulation` is set and `simulation.check_criteria != 'end_of_dialog'`, require that *all* `success_criteria` are cheap (no `pytest`, no `run_command` with long timeouts) OR emit a warning. Otherwise per-turn checks become prohibitively slow.

**Modified: `src/coder_eval/orchestrator.py`**
- Branch at the top of the iteration loop on `task.simulation is not None`:
  - **Single-shot branch:** unchanged.
  - **Simulation branch:** call new `run_dialog(...)` which:
    1. Sends `initial_prompt` as the first agent input.
    2. Loops until termination:
       - Collect agent response into a `TurnRecord`.
       - (optional) run criteria; stop early on pass.
       - Invoke `UserSimulator.next_user_message(transcript)` → next prompt.
    3. Returns the final transcript + criteria results.
- Snapshotting stays per-turn (existing logic works — each turn already produces a `TurnRecord`).
- `n_trials` > 1 is handled one level up in `orchestration/batch.py` by expanding each simulation task into N pseudo-tasks with `trial_id` suffixes; reporting aggregates back.

**Modified: `src/coder_eval/models/results.py`**
- `TurnRecord` already has `user_input` + `agent_output` — no shape change needed; it cleanly holds simulated-user prompts.
- Add optional `trial_id: int | None` to `EvaluationResult` for trajectory disambiguation.
- Add `simulation_telemetry` sub-record: simulator tokens, wall-clock, stop reason.

**Modified: `src/coder_eval/orchestration/experiment.py`**
- Simulator model/persona/temperature become mergeable variant fields in the 5-layer resolver, same as `agent.model`. Zero new infrastructure — just new paths in the merge map.

**Modified: reports**
- `reports.py`: render dialog transcripts in the per-task markdown.
- `reports_experiment.py`: add "trials per task" and "avg turns to success" columns.
- HTML dashboard: reuse existing transcript viewer; add per-trial drill-down.

### 4.4 Config / experiment integration

- No change to the 5-layer merge logic — `simulation` is just another merge path.
- Experiment variants can override any simulator field:
  ```yaml
  variants:
    - id: terse_user
      simulation:
        persona: "You answer in at most one sentence."
    - id: chatty_user
      simulation:
        persona: "You over-explain and ramble."
  ```
- `experiments/default.yaml` gets a `simulation:` default block with `enabled: false`, so every existing task stays untouched.

### 4.5 Replay mode (follow-up, not v1)

Record every transcript to `runs/<run_id>/<task>/transcript.json`. Add `coder-eval replay <run_id>` that re-runs *only the criteria checkers* against a frozen transcript. This gives deterministic regression tests for criteria changes without re-paying simulator costs.

### 4.6 Testing strategy

- **Unit:** `UserSimulator` with a fake invoker (fixed canned responses) — tests turn accumulation, stop token handling, constraint injection.
- **Unit:** termination predicate — all four stop conditions, including interactions (e.g. criteria pass *and* max_turns reached in same turn).
- **Integration:** one end-to-end simulation task with a mock agent + mock simulator, asserting full transcript shape, `EvaluationResult`, and snapshot count.
- **Regression:** existing single-shot test suite MUST pass untouched — simulation is strictly additive.

### 4.7 Rollout

1. **M1:** `SimulationConfig` model + validator + docs. No orchestrator changes. (unblocks task authoring)
2. **M2:** `UserSimulator` + orchestrator dialog branch. Single-trial only. `check_criteria: end_of_dialog`.
3. **M3:** `n_trials`, parallel trials, per-turn criteria checks, early stop.
4. **M4:** Experiment-variant integration + reporting polish.
5. **M5:** Replay mode.

Each milestone is independently shippable behind the `simulation.enabled` flag.

## 5. Open Questions

1. **Criteria-per-turn cost.** Do we realistically expect anyone to turn `check_criteria: every_turn` on when pytest criteria exist? If not, we should forbid it at validation time rather than warn.
2. **Simulator tool access.** tau-bench's simulator is pure text. Should we ever let the simulator "run" things (e.g., paste a fake error message as if from their terminal)? Probably YAGNI until a task demands it.
3. **Where does `initial_prompt` live conceptually in simulation mode?** It's the first *agent* input, not a user utterance. The current field name is fine but worth calling out in the task-authoring guide to avoid confusion.
4. **Reference leakage.** The simulator receives persona + goal, but the `reference` ReferenceSource is never passed. Should we add a unit test that asserts the reference string never appears in any simulator prompt? (Cheap; worth it.)
5. **Trial aggregation semantics in reports.** `pass_rate` per task is obvious (trials_passed / n_trials), but what's the "representative" trajectory to render in the HTML report? First pass? Median? Worst? Leaning toward *worst-failing* to surface bugs.

## 6. Explicitly Out of Scope

- Multi-agent dialogs (agent ↔ agent).
- Human-in-the-loop evaluation (real users instead of simulators).
- Tool-use by the simulator.
- Dialog-aware success criteria (e.g. "agent must ask ≥1 clarifying question") — these can be added later as new criterion types once the core loop is in.
- Streaming the simulator's generation token-by-token to the UI (batched per-turn is fine).

---

**Next step:** align on §4.2 schema, §4.3 module layout, and the M1→M2 cut line. Once those land, implementation is mostly mechanical.
