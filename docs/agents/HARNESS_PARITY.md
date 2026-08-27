# Run-Limit Parity

One task file, run on three harnesses, must be the same task. `run_limits.max_turns`
was the field that broke that promise hardest: Claude Code enforced it, and Codex and
Antigravity accepted it and never read it, so `max_turns: 6` ran capped on one
backend and unbounded on the other two.

This page is the contract for what each run limit means per harness, plus the shared
`agent` fields whose meaning still differs across them.

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
marked `crashed`.

Antigravity stops earlier and more gently, for the reason in the next section.

## Antigravity backgrounds anything over 10 seconds

The Antigravity localharness has a **10-second maximum synchronous wait** for shell
commands. Past it, the harness moves the command to a background task and hands the
model a task id instead of a result. That is harness behavior, not something
coder_eval configures.

What coder_eval does about it: the turn polls for the backgrounded result rather
than finalizing the moment the step stream goes idle, so slow work does finish and
its real exit code reaches the model. Without that poll, a command over the 10s
boundary left the tool call unresolved and the turn was graded on work that had not
happened yet.

The wait is bounded by **80% of `turn_timeout`** (or 120 five-second cycles when the
task sets no timeout), not by `turn_timeout` itself. A job that outlives that bound
is force-closed as unresolved and the turn is graded on everything else, where
Claude Code and Codex instead raise a turn timeout and mark the turn crashed.

So the residual divergence is the terminal signal, not whether slow work completes:
a long `npm install` or build runs to completion here the way it does on the other
two, but a command that never finishes reads as an ordinary low score rather than a
timeout.

## Timeouts are not turn caps

A timeout is a *failure* (partial turn captured, error status); the turn cap is a
*clean stop*. Conflating them is the mistake this page exists to prevent: a task
whose cap fires should not look like a task whose harness hung.

## `agent.plugins[].path` accepts different depths per harness

Not a run limit, but the same promise: one task file, three harnesses, same meaning.
This field breaks it silently.

| | claude-code | codex | antigravity |
|---|---|---|---|
| `<path>/skills/<name>/SKILL.md` (plugin root) | **required** | accepted | accepted |
| `<path>/<name>/SKILL.md` (bare skills dir) | **loads nothing** | accepted | accepted |

claude-code hands the value to the SDK as a *plugin directory*, and a plugin's skills
live at `<plugin>/skills/<name>/SKILL.md`. Point it at the directory that directly
parents the skill directories and no skill loads. Codex
(`codex_agent._setup_skills`) and Antigravity (`antigravity_agent._resolve_skills_paths`)
both scan **both** layouts and take whichever actually holds a `<skill>/SKILL.md`.

So `.claude/skills` works on two backends out of three and fails on the third — and
fails without an error. The agent simply is not offered the skill, every positive row
of an activation suite scores 0, and the suite reports recall 0.0. That is
indistinguishable from a skill that never triggers, which is the finding such a suite
exists to produce. It shipped in six documentation surfaces at once for exactly this
reason.

Probe it in one command, against whichever harness you doubt:

```bash
claude --plugin-dir "$(pwd)/.claude"        # loads .claude:<skill>
claude --plugin-dir "$(pwd)/.claude/skills" # loads nothing
```

**Write the plugin root.** It is correct on all three, so there is never a reason to
write the deeper form. For `.claude/skills/my-skill/SKILL.md` that is `.claude`.
`SKILL_SOURCE_PATH` — the variable `/coder-eval:check-skill` emits — is held to this by
lint rule CE045; `PLUGIN_PATH` in the Codex docs is deliberately outside that rule,
since a skills directory is valid there.

## Reproducing

`tasks/run_limits/` holds one fixture per limit: `max_turns_cap.yaml` asks for more
sequential work than its cap allows, and `turn_timeout.yaml` runs a command that
outlives its watchdog. Run either with `--type claude-code` / `--type codex` /
`--type antigravity` to check a backend against the contract above.

## Related

- [Claude Code](CLAUDE_CODE.md) · [Codex](CODEX.md) · [Antigravity](ANTIGRAVITY.md)
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full `run_limits` schema
