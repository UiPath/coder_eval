# Run-Limit Parity

One task file, run on three harnesses, must be the same task. `run_limits.max_turns`
was the field that broke that promise hardest: Claude Code enforced it, and Codex and
Antigravity accepted it and never read it, so `max_turns: 6` ran capped on one
backend and unbounded on the other two.

This page is the contract for what each run limit means per harness.

## The table

| Limit | claude-code | codex | antigravity |
|---|---|---|---|
| `run_limits.max_turns` | native SDK cap (agent-loop turns) | visible-turn cap (resolved tool calls) | visible-turn cap (resolved tool calls) |
| `run_limits.turn_timeout` | watchdog, SIGKILL on the CLI subprocess **tree** | watchdog + cooperative interrupt | watchdog, plus an earlier internal poll deadline — 80% of it with a tool call still ACTIVE, later (drain-aware, ≤95%) otherwise (see below) |
| `run_limits.task_timeout` | orchestrator-level, agent-agnostic | orchestrator-level, agent-agnostic | orchestrator-level, agent-agnostic |
| `run_limits.stop_early` | cooperative `should_stop` | cooperative `should_stop` | cooperative `should_stop` |

## `max_turns` counts visible turns on Codex and Antigravity

A "visible turn" is one entry in the run's timeline: one resolved tool call. It is
the unit `reports_stats.visible_turn_count` reports and the unit that lands in
`TurnRecord.commands`. Both backends count it live off the shared
`EventCollector.visible_turn_count`, so one `max_turns` value means one thing on
both.

They need their own counter because a native one would be meaningless: Codex and
Antigravity each deliver exactly **one SDK turn per `communicate()` call**, so an
SDK-level cap would clamp at 1 no matter what the task asked for.

The cap is enforced on the same loop boundary as the cooperative early stop: the
step or notification that reaches the cap is processed whole, and the next one is
never pulled. The in-flight turn is then cancelled server-side (best effort) so
the cap actually stops spend. A run cut this way finalizes cleanly as
`max_turns_exhausted` — it is not a crash, and it is not retried.

**claude-code keeps its native SDK cap.** That is a real, honored cap, so it is
left alone rather than reimplemented in a different unit. Its unit is the SDK's own
agent-loop turn, which absorbs an arbitrary number of *parallel* tool calls, so the
same number bounds very different amounts of work: under a prompt that encourages
batching, a cap of N here permits many more than N tool calls, where it buys exactly
N on the other two.

**So holding `max_turns` constant across harnesses does not hold the budget
constant.** If you are A/B-ing across backends and the cap is close to binding, that
is the number to distrust.

### What a capped run looks like

The signals a capped run leaves behind, on every backend:

- Criteria are still checked against whatever the agent produced, because the cap is
  an ordinary end-of-run rather than an error. So a capped run that nonetheless
  satisfies its criteria finishes as `SUCCESS`; one that does not finishes as
  `MAX_TURNS_EXHAUSTED` (reporting category `failed`, icon `M`). Never `ERROR`,
  and never retried.
- `max_turns_exhausted: true` on the task record.
- On Codex and Antigravity, the count of *resolved* tool calls the model itself
  issued equals the cap. Two things can add a further *recorded* command, and
  neither means the cap leaked:
    - A tool call already in flight when the cap fires is force-closed and recorded
      with `result_status: unknown` rather than dropped, so the trajectory shows what
      was interrupted.
    - On Codex, a sub-agent's inner tool calls are recovered from its rollout after
      the pump stops, so the child's work and its tokens still reach the record. The
      cap bounds what the model was allowed to do, not what the record may explain.

## What a timeout looks like

On Claude Code and Codex a `turn_timeout` breach is a *failure*: the watchdog fires
at the deadline, the partial turn is preserved on `pending_turn`, and the turn is
marked `crashed`. The orchestrator then attempts success-criteria grading against
whatever was salvaged (`_grade_after_forced_kill`) before falling back to
`FinalStatus.TIMEOUT` if criteria don't pass.

Antigravity's poll loop (next section) can exit two ways, and only one of them
stops earlier and more gently than the other two backends.

## Antigravity backgrounds anything over 10 seconds

The Antigravity localharness has a **10-second maximum synchronous wait** for shell
commands. Past it, the harness moves the command to a background task and hands the
model a task id instead of a result. That is harness behavior, not something
coder_eval configures.

What coder_eval does about it: the turn polls for the backgrounded result rather
than finalizing the moment the step stream goes idle, so slow work does finish and
its real exit code reaches the model. Without that poll, a command over the 10s
boundary left the tool call unresolved and the turn was graded on work that had not
happened yet. Each individual step-fetch inside a poll cycle is itself bounded (30s)
so a genuinely non-idle connection can't block the whole poll loop — see
`_RECEIVE_STEPS_PER_STEP_TIMEOUT_SECONDS` in `antigravity_agent.py`.

The wait is bounded by a **fraction of `turn_timeout`**, not by `turn_timeout`
itself, and which fraction applies depends on what the turn is doing:

- **A tool call is still ACTIVE** (stuck — a state that cannot resolve itself):
  **80%**. Exiting well before the watchdog is what lets the graceful path
  (force-close the orphan, grade the turn normally) reliably win that race.
- **No tool call open, the connection is merely quiet** (a >=30s gap between
  steps is ordinary agent behaviour — a long thinking burst): **as late as it
  can be while still leaving room for one worst-case drain** — concretely
  `turn_timeout - (30s + 5s)`, clamped to the 80% floor below and a 95% ceiling
  above (`_quiet_poll_deadline_offset`). Such a turn may still finish on its
  own, so it keeps nearly the whole budget: 265s of a 300s default.

  The subtraction, rather than a flat fraction, is what makes the graceful exit
  *reachable*. The post-sleep guard only decides whether to START a drain — it
  cannot interrupt one — so the last cycle admitted before the deadline can
  still spend a full 30s inside `_drain`. A flat 95% left only 15s of margin
  against that 30s overshoot, so the watchdog fired mid-drain and its hard
  task-cancel skipped the exit's bounded `conversation.cancel()`, leaving the
  harness live while criteria read the sandbox. For a `turn_timeout` too small
  to fit two drains there is no margin to preserve and the 80% floor applies.

A task that sets no timeout (or a non-positive one, which arms no watchdog) falls
back to a flat **600s** wall-clock backstop for both cases. A single clock bounds
the loop in either mode: an earlier revision also imposed **120 poll cycles** in
parallel, and that was removed — a cycle's cost is bimodal (against a genuinely
backgrounded job the connection is idle and `receive_steps()` returns
immediately, so a cycle costs only the 5s poll interval; against a wedged
connection every re-drain burns the full 30s per-step timeout), so a cycle count
tuned for one mode is wrong for the other, and a cap of 120 silently overrode a
large configured `turn_timeout`. What happens when the bound is hit depends on
whether a tool call is still open:

- **An orphaned tool call still ACTIVE**: force-closed as unresolved and the turn is
  graded on everything else — the graceful path, and the residual divergence from
  Claude Code/Codex: a long `npm install` or build runs to completion here the way
  it does on the other two, but a command that never finishes reads as an ordinary
  low score rather than a timeout.
- **Nothing ACTIVE at all** (the connection never produced a clean end and no tool
  call is in flight to explain the silence): Antigravity now raises a turn timeout
  and marks the turn crashed too, same as Claude Code and Codex, so the
  orchestrator's forced-kill grading path gets a chance to run instead of the turn
  silently finalizing as an ordinary, unmarked completion.

## Timeouts are not turn caps

Both now evaluate success criteria — a timeout runs
`Orchestrator._grade_after_forced_kill` against whatever the agent produced, so a
timed-out task that already satisfied its criteria finalizes `SUCCESS` rather than
being discarded. What still separates them is the DEFAULT status when criteria do
not pass (`TIMEOUT` vs `MAX_TURNS_EXHAUSTED`) and the `crashed` mark on the partial
turn: a cap is a clean stop mid-trajectory, a timeout means the harness was cut off.
Conflating them is the mistake this page exists to prevent: a task whose cap fires
should not look like a task whose harness hung.

## Reproducing

`tasks/run_limits/` holds one fixture per limit: `max_turns_cap.yaml` asks for more
sequential work than its cap allows, and `turn_timeout.yaml` runs a command that
outlives its watchdog. Run either with `--type claude-code` / `--type codex` /
`--type antigravity` to check a backend against the contract above.

## Related

- [Claude Code](CLAUDE_CODE.md) · [Codex](CODEX.md) · [Antigravity](ANTIGRAVITY.md)
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full `run_limits` schema
