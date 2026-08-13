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
| `run_limits.turn_timeout` | watchdog, SIGKILL on the CLI subprocess | watchdog + cooperative interrupt | watchdog, plus an earlier internal poll deadline at 80% of it (see below) |
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
same number bounds very different amounts of work: see the measurement below.

### What a capped run looks like

The signals a capped run leaves behind, on every backend:

- `final_status` is a completed status, not an error. The cap is an ordinary
  end-of-run, so criteria are still checked against whatever the agent produced.
- `max_turns_exhausted: true` on the task record.
- On Codex and Antigravity, the count of *resolved* tool calls equals the cap.

## Measured

Two fixtures under `tasks/run_limits/`, one prompt per limit, run on all three
harnesses (`--type claude-code --backend bedrock` / `--type codex` /
`--type antigravity`).

**`max_turns_cap.yaml` — 12 sequential file writes requested, `max_turns: 4`:**

| Harness | resolved tool calls | `max_turns_exhausted` | `final_status` |
|---|---|---|---|
| claude-code | 4 | true | SUCCESS |
| codex | 4 | true | SUCCESS |
| antigravity | 4 | true | SUCCESS |

All three stop at 4 and finish cleanly, with the first file on disk so the criteria
still grade real work. Before this change, Codex and Antigravity ran the prompt to
completion and wrote all 12.

**A batching prompt (parallel tool calls encouraged), `max_turns: 2`:**

| Harness | resolved tool calls | assistant messages |
|---|---|---|
| claude-code | **12** | 14 |
| codex | 2 (+1 in-flight, recorded unresolved) | 1 |
| antigravity | 2 | 1 |

This is the divergence, quantified: on claude-code a cap of 2 permitted all 12
writes, because one SDK agent-loop turn carries as many parallel calls as the model
emits. Codex and Antigravity stop at 2. **Hold `max_turns` constant across harnesses
and it is not a constant budget** — if you are A/B-ing across backends and the cap
matters to the result, that is the number to distrust.

The Codex `+1` is the tool that was already in flight when the cap fired. The cap
stopped the run after 2 completed calls; the third is force-closed and recorded with
`result_status: unknown` rather than being silently dropped, so the trajectory shows
what was interrupted.

**`turn_timeout.yaml` — `sleep 240` under a 45s watchdog:**

| Harness | outcome | duration |
|---|---|---|
| claude-code | turn timeout, partial turn captured, `crashed: true` | 45.6s |
| codex | turn timeout, partial turn captured, `crashed: true` | 46.4s |
| antigravity | poll budget exhausted, tool force-closed, turn graded, `crashed: false` | 40.0s |

Claude Code and Codex behave identically: the watchdog fires at the deadline, the
partial turn is preserved, and the run ends as an error. Antigravity stops earlier
and more gently, for the reason below.

## Antigravity backgrounds anything over 10 seconds

The Antigravity localharness has a **10-second maximum synchronous wait** for shell
commands. Past it, the harness moves the command to a background task and hands the
model a task id instead of a result. Measured: `sleep 5` resolves normally in 10.4s
wall-clock; anything longer comes back immediately as a background task. That is
harness behavior, not something coder_eval configures.

What coder_eval does about it: the turn polls for the backgrounded result rather
than finalizing the moment the step stream goes idle. Measured on a command that
finishes inside the budget (`sleep 60` writing a file, `turn_timeout: 300`), same
task and model on either side:

| | outcome | duration |
|---|---|---|
| without the poll loop | FAILURE — file never written, tool left unresolved | 19.5s |
| with it | SUCCESS — file written, exit code reported back | 75.0s |

The wait is bounded by **80% of `turn_timeout`** (or 120 five-second cycles when the
task sets no timeout), not by `turn_timeout` itself. A job that outlives that bound
is force-closed as unresolved and the turn is graded on everything else, where
Claude Code and Codex instead raise a turn timeout and mark the turn crashed.

So the residual divergence is the terminal signal, not whether slow work completes:
a long `npm install` or build now runs to completion here the way it does on the
other two, but a command that never finishes reads as an ordinary low score rather
than a timeout.

## Timeouts are otherwise unchanged by this contract

A timeout is a *failure* (partial turn captured, error status); the turn cap is a
*clean stop*. Conflating them is the mistake this page exists to prevent: a task
whose cap fires should not look like a task whose harness hung.

## Related

- [Claude Code](CLAUDE_CODE.md) · [Codex](CODEX.md) · [Antigravity](ANTIGRAVITY.md)
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full `run_limits` schema
