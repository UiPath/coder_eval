# Run-Limit Parity

One task file, run on three harnesses, must be the same task. `run_limits.max_turns`
was the field that broke that promise hardest: Claude Code enforced it, and Codex and
Antigravity accepted it and never read it, so `max_turns: 6` ran capped on one
backend and unbounded on the other two.

This page is the contract for what each run limit means per harness.

## The table

| Limit | claude-code | codex | antigravity |
|---|---|---|---|
| `run_limits.max_turns` | native SDK cap (assistant messages) | visible-turn cap (resolved tool calls) | visible-turn cap (resolved tool calls) |
| `run_limits.turn_timeout` | watchdog, SIGKILL on the CLI subprocess | watchdog + cooperative interrupt | watchdog + cooperative interrupt |
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

**claude-code keeps its native SDK cap**, which counts assistant messages instead.
That is a real, honored cap, so it is left alone rather than restated in a
different unit; the same `max_turns: 20` therefore bounds slightly different things
on claude-code than on the other two. Documented rather than papered over.

### What a capped run looks like

The three signals a capped run leaves behind, on every backend:

- `final_status` is a completed status, not `agent_crash`. The cap is an ordinary
  end-of-run, so criteria are still checked against whatever the agent produced.
- `max_turns_exhausted: true` on the turn record.
- `visible_turn_count` equals the cap on Codex and Antigravity. On claude-code it
  is whatever tool calls fit inside the assistant-message budget, so it is
  bounded by the cap rather than equal to it.

## Timeouts are unchanged by this contract

`turn_timeout` and `task_timeout` already behaved consistently and are listed here
only so the parity table is complete. A timeout is a *failure* (partial turn
captured, `agent_crash` / timeout status); the turn cap is a *clean stop*. Conflating
them is the mistake this page exists to prevent: a task whose cap fires should not
look like a task whose harness hung.

## Related

- [Claude Code](CLAUDE_CODE.md) · [Codex](CODEX.md) · [Antigravity](ANTIGRAVITY.md)
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full `run_limits` schema
