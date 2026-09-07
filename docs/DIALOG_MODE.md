---
description: >-
  Evaluate coding agents in conversation, not one-shot — Coder Eval's dialog
  mode drives a simulated user with a persona and a goal, so you can measure
  clarifying questions, requirement discovery, and multi-turn recovery.
---

# Dialog Mode: multi-turn user simulation

Most evaluation harnesses hand an agent one perfect prompt and grade the artifact. Real users don't
write perfect prompts. They ask for something vague, answer questions, change their mind, and hold
back requirements until asked.

Dialog mode replaces the single-shot loop with a conversation between the coding agent and a
**simulated user** — a second LLM driven by a persona and a goal. It is the only way to measure the
behaviors that only exist in conversation.

## When to use it

Reach for dialog mode when the thing you want to score is conversational:

- **Does the agent ask?** A good agent asks a clarifying question instead of guessing when the
  request is underspecified. Give the simulator a goal that withholds a requirement and see whether
  the agent surfaces it.
- **Requirement discovery.** Score whether the agent eventually extracts everything it needs across
  several turns, not whether it one-shots a fully-specified prompt.
- **Multi-turn recovery.** The agent goes down the wrong path, the user corrects it — does it
  recover, or double down?
- **Prompt robustness.** A persona that is terse, impatient, or non-technical is a much harsher test
  than your hand-tuned `initial_prompt`.

Stay in single-shot mode when the task is genuinely fire-and-forget. Dialog mode multiplies cost and
adds variance; don't pay for it when the extra turns measure nothing.

## How it works

Add an enabled `simulation:` block to any task and the orchestrator swaps its iteration loop for a
dialog driver:

```yaml
simulation:
  enabled: true
  persona: |
    You are a user with one specific request. You give the request only when
    asked, and then you wait for the agent to do it.
  goal: |
    Get the agent to echo your string back verbatim. Reveal the string only
    when the agent asks what you want.
  max_turns: 4
```

The mechanics:

- The task's `initial_prompt` is the user's **opening message**. The simulator takes over from turn
  two, seeing the agent's last reply and producing the next user utterance.
- `initial_prompt` is **optional** in dialog mode. Omit it and the simulator writes the opener too,
  from its persona and goal — closer to a cold-start user.
- Each exchange is one user message plus the agent's full response (the agent may make many tool
  calls inside a single exchange).
- The simulator is a **tools-disabled Claude Code agent** with `allowed_tools: []`, an explicit
  deny-list, and no plugins or settings sources. It is pure text-in / text-out, and it **cannot see
  the sandbox** — no files, no terminal, no agent reasoning. Only what the agent writes in the chat.
- The simulator resolves its own `ApiRoute` independently of `checker_context.api_route` (that
  override is judge-only — see [Checker Context](TASK_DEFINITION_GUIDE.md#checker-context)) — same
  resolution as the coding agent's own route (`--backend direct` / `--backend bedrock`), including
  the pin to a constant Claude backend when the agent itself runs on an open-weight LiteLLM route.
  The **model** is separately
  pinned by `simulation.model` (see [Simulation](TASK_DEFINITION_GUIDE.md#simulation)), not inherited
  from the route — so an A/B varying the subject model doesn't silently vary the simulated user too.

**Agent-kind constraint.** The *subject* agent can be any registered kind — the dialog driver only
calls the agent's `communicate()`, so Codex and plugin agents work. The *simulator*, however, is
always a Claude Code agent. A dialog run therefore needs the `claude` CLI on `PATH` and working
Anthropic or Bedrock credentials **even when the agent under test is not Claude**.

**Early stop is not available here.** [Criterion-level `stop_early:` arming](TASK_DEFINITION_GUIDE.md#stop_early-opt-in-early-stop)
is single-shot-only; an armed criterion alongside `simulation.enabled` is a hard error at resolution
time, not a silent no-op (disarm with the `run_limits.stop_early: false` kill switch to run anyway).
Use `stop_on_criteria_pass` (below) for the dialog equivalent.

Every field's default and constraint lives in one place — the
[Task Definition Guide's simulation section](TASK_DEFINITION_GUIDE.md#simulation-multi-turn-user-dialog).
This page deliberately does not restate them.

## Designing the persona and goal

`persona` is *who is talking*; `goal` is *what they want*. Keep them separate — a persona that
smuggles in the requirements defeats the point.

The most useful pattern is **withholding**. State the full goal for the simulator, then instruct it
not to volunteer part of it:

```yaml
persona: |
  A non-technical business analyst who knows the outcome they want but not how
  automation works. Mildly impatient, answers questions directly.
goal: |
  Build a flow that reads invoice PDFs from an Outlook folder, extracts
  vendor/amount/date, and posts the result to Google Sheets.
  Do NOT volunteer the Google Sheets requirement unless the agent asks where
  the output should go.
constraints:
  - "Do not paste code — you cannot read code."
  - "If the agent goes silent for two turns, ask 'are you still there?'."
```

Now the task measures something a single-shot prompt cannot: whether the agent asks about the output
destination at all. Pair it with a criterion that checks the Sheets integration exists and you have a
clean signal.

`constraints` are behavioral rules, not more requirements. Good ones bound *how* the simulator
speaks ("no code", "one question at a time", "reveal X only if asked"). Resist writing constraints
that quietly hand the agent the answer.

## How a dialog ends

After each exchange the driver evaluates the stop conditions **in this order**, first match wins:

1. **`run_limits` breach** (`run_limit_exceeded`) — a subject-agent budget cap tripped. The
   end-of-dialog criteria still run for partial credit, then the task **aborts**.
2. **`stop_on_criteria_pass`** (`criteria_passed`) — every success criterion passes. Requires
   per-turn checking (`check_criteria: every_turn` or `both`); pairing it with the default
   `end_of_dialog` is rejected at load time, since there would be nothing to check against.
3. **`max_turns`** (`max_turns`) — the hard cap on exchanges. The agent exhausting its *own* inner
   `max_turns` mid-exchange ends the dialog with the same reason.
4. **`max_total_tokens`** (`budget`) — the dialog-wide budget across simulator **and** agent. The
   dialog ends and the task is **still scored** — unlike
   [`run_limits.max_total_tokens`](TASK_DEFINITION_GUIDE.md#run-limits), which covers the subject
   agent only and aborts.
5. **`stop_token`** (`stop_token`) — only if none of the above fired is the simulator asked for
   another message; the sentinel token in *that fresh utterance* ends the dialog. This is the
   workhorse in practice — the simulator decides, in character, that it got what it wanted — but it
   is evaluated **last**, so a turn that trips `max_turns` or the budget never gets the chance to
   produce it.

A simulator call that raises ends the dialog with `error` (and increments
`simulation.simulator_failures`). The reason is recorded as `simulation.stop_reason` on the result,
so you can tell a conversation that finished from one that merely ran out of turns.

> **`check_criteria: every_turn` multiplies criterion cost.** Criteria are re-evaluated after every
> exchange, so an `llm_judge` or `agent_judge` criterion in a 12-turn dialog runs twelve times.
> That is often more expensive than the dialog itself. Use `end_of_dialog` (the default) unless you
> genuinely need early stop or a per-turn record; `both` gives you the per-turn trace *and* an
> authoritative final check.

## Trials and variance

A single dialog is one sample from a stochastic process — the simulator improvises as much as the
agent does. `n_trials` runs the conversation N independent times per (task, variant):

```yaml
simulation:
  enabled: true
  n_trials: 5
```

Each trial is fanned out as its own unit of work with its own sandbox, its own run directory
(`runs/<ts>/<variant_id>/<task_id>/<NN>/`, zero-padded index), and its own `task.json`. On the result,
`simulation.replicate_index` and `simulation.n_trials` identify the trial, and reports render a
"Trial *k* of *N*" row. Trials reuse the replicate machinery, so when a simulation task sets
`n_trials > 1` it takes precedence over experiment-level `repeats`.

Trials are ordinary batch units: they run concurrently exactly as far as `--max-parallel` allows —
there is no per-task trial-concurrency switch; scheduling is entirely `--max-parallel`'s job.

## Grading a dialog

Set `include_dialog: true` on every `llm_judge` / `agent_judge` criterion in a simulated task. This
is not optional polish — without it the judge sees only the agent's outputs and no user side, so a
requirement the user stated in turn 3 looks like something the agent invented. Judges routinely
score that as a hallucination.

```yaml
success_criteria:
  - type: llm_judge
    description: "Agent satisfied the requirement the user gave mid-conversation"
    prompt: |
      Read the DIALOG block. Grade the agent on whether it satisfied the
      requirement the user actually stated — not what you assume the task is.
    include_dialog: true
    pass_threshold: 0.9
```

The rendered dialog is wrapped as `UNTRUSTED DATA` with a rubric guard telling the judge that claims
made only by the *simulated user* may be invented, and not to penalize the agent for going along
with them. That guard is what keeps a chatty simulator from poisoning the grade.

## What it costs

Budget roughly `max_turns × n_trials` agent turns per (task, variant) — the worst case, since the
dialog usually stops on the stop token first. On top of that:

- **Simulator tokens**, one generation per turn. Small next to the agent's, but not free, and they
  are reported separately as `simulation.simulator_input_tokens` / `simulator_output_tokens`.
- **Criterion cost**, multiplied by turn count under `check_criteria: every_turn` or `both`.

Cap it with [`run_limits`](TASK_DEFINITION_GUIDE.md#run-limits) — but note the accounting boundary:
`run_limits` budgets count the **subject agent only**, so simulator spend is invisible to
`max_total_tokens` and `max_usd`. To bound the conversation as a whole, use
`simulation.max_total_tokens`, which counts both sides.

## Dialog mode as an experiment axis

Everything on the `simulation:` block is overridable per variant, which makes the conversation itself
the thing under test. A variant sets a partial `simulation:` block that shallow-merges onto the
task's:

```yaml
variants:
  - variant_id: patient-user
    simulation:
      persona: "A patient engineer who answers questions precisely."
  - variant_id: terse-user
    simulation:
      persona: "A terse, impatient user who gives one-line answers."
```

Productive axes: persona (terse vs. chatty, technical vs. not), how much the goal withholds, and
`n_trials` as a cost/confidence trade. See [A/B Experiments](AB_EXPERIMENTS.md) for the full
experiment layer.

## Security posture

The task's `reference` solution is **never** passed to the simulator, exactly as it is never passed
to the coding agent. A simulator that could read the reference would leak it into the dialog and the
agent would be graded against an answer it was handed.

What the simulator *does* see, all inlined into its system prompt: `persona`, `goal`, `constraints`,
the task's **`description`**, and the pinned `initial_prompt` when there is one. The `description` is
easy to forget — it reads like evaluator-private metadata, but the simulator gets it verbatim. If
your persona withholds a requirement, don't spell that requirement out in `description`, or the
simulator may volunteer what you meant it to keep back.

The simulator also runs with no tools, no plugins, and no settings sources, so it cannot read or
write the sandbox even if its persona instructs it to.

## See also

- [Task Definition Guide → Simulation](TASK_DEFINITION_GUIDE.md#simulation-multi-turn-user-dialog) —
  every field, default, and constraint.
- [A/B Experiments](AB_EXPERIMENTS.md) — running personas as variants.
- [Run Limits](TASK_DEFINITION_GUIDE.md#run-limits) — the caps that bound a dialog's cost.
