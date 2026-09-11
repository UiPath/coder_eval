# Run-Limit Parity

One task file, run on any harness, must be the same task. `run_limits.max_turns`
was the field that broke that promise hardest: Claude Code enforced it, and Codex and
Antigravity accepted it and never read it, so `max_turns: 6` ran capped on one
backend and unbounded on the other two.

This page is the contract for what each run limit means per harness, plus the shared
`agent` fields whose meaning still differs across them.

## The table

| Limit | claude-code | codex | antigravity | opencode | pi |
|---|---|---|---|---|---|
| `run_limits.max_turns` | native SDK cap (agent-loop turns) | visible-turn cap (resolved tool calls) | visible-turn cap (resolved tool calls) | native step cap (the CLI's own agent-loop steps) | native turn cap (the CLI's own `turn_start` agent-loop steps) |
| `run_limits.turn_timeout` | watchdog, SIGKILL on the CLI subprocess | watchdog + cooperative interrupt | watchdog, plus an earlier internal poll deadline at 80% of it (see below) | deadline enforced in-loop and on the final reap; SIGTERM→SIGKILL on the CLI's whole process group | deadline enforced in-loop and on the final reap; SIGTERM→SIGKILL on the CLI's whole process group |
| `run_limits.task_timeout` | orchestrator-level, agent-agnostic | orchestrator-level, agent-agnostic | orchestrator-level, agent-agnostic | orchestrator-level, agent-agnostic | orchestrator-level, agent-agnostic |
| `run_limits.stop_early` | cooperative `should_stop` | cooperative `should_stop` | cooperative `should_stop` | cooperative `should_stop` (event granularity) | cooperative `should_stop` (event granularity — Pi streams incrementally) |

## Timing capture

What each harness records about *when* things happened, and how much of a task's
wall clock its numbers account for.

| Field | claude-code | codex | antigravity | opencode | pi |
|---|---|---|---|---|---|
| `generation_duration_ms` source | harness clock: previous SDK event → this message | SDK item stamps, minus tool execution inside the window | harness clock: previous flush → this flush, minus tool execution inside the window | harness clock per CLI step, minus tool execution inside the step | harness clock per CLI turn, minus tool execution inside the turn |
| what the **first** window covers | turn start → msg0, so dispatch + TTFT are INSIDE it | the first SDK item's own start, so CLI boot + TTFT are OUTSIDE it | turn start → first flush, so dispatch + TTFT are INSIDE it | the first `step_start`, so CLI boot + TTFT are OUTSIDE it | the first `turn_start`, so CLI boot + TTFT are OUTSIDE it |
| `harness_startup_ms` (turn head) | 0.0 — the window above already covers it | ~3.1 s — CLI boot fused with TTFT | 0.0 — the window above already covers it | ~2.5 s — CLI boot fused with TTFT | ~0.23 s — CLI boot fused with TTFT |
| `harness_teardown_ms` (turn tail) | ~1.3 s | ~13 ms | ~7 ms | ~26 ms | ~19 ms |
| tool `duration_ms` source | measured around the tool result | SDK `completed_at_ms − started_at_ms`; the item's own `duration_ms` only as a fallback | measured ACTIVE → DONE | measured around the tool event | measured around the tool event |
| `execution_started_at` / `execution_completed_at` | derived from the measured duration | SDK stamps (both, or neither) | measured at ACTIVE / DONE | measured | measured |
| `generation_completed_at` | set | `None` — see below | `None` | `None` | `None` |
| `message_id` source | SDK `message_id`; `None` when the stream carries none; `subagent-<tool_use_id>` for a synthesized sub-agent terminal | synthetic `turn_id-msg-N`, shared across the sub-messages of one generation; `turn_id-subagent-N` for recovered sub-agent generations | synthetic `turn_id-msg-N`, one per generation | CLI `messageID` | CLI `responseId`; `None` when absent |
| `Σ generation + ∪ tool + head + tail ≈ turn duration` | yes | yes | yes | yes | yes |

**`generation_duration_ms` is model-generation time, not `completed_at − started_at`.**
All five harnesses can have tool execution inside a generation window, and all
five subtract it. Four interleave it structurally: Antigravity reports a `Step`
for the tool and only a later
`usage_metadata` `Step` cuts the message; Codex's message window is seeded from
the first item's start and extended to the last item's completion; OpenCode
opens its window at `step_start` and closes it at `step_finish`, and Pi at
`turn_start` / `turn_end`, with every tool call running inside. In each the
span between the recorded bounds legitimately CONTAINS tool time that the model
did not spend generating, so each subtracts it — the **union** of the closed tool intervals
clipped to the window (`coder_eval/timing.py::busy_ms`), never the sum, because
tool calls overlap: Antigravity resolves several from one `Step` and backgrounds
anything over ten seconds, and Codex spawns collab agents concurrently. Summing
them over-subtracts by exactly the overlap and, with enough concurrency, drives
the result to a clamped zero.

The consequence worth knowing: on an emission that carries *only* a tool call,
the whole measured window was that tool running, so the recorded generation
time is legitimately `0.0`. That is a measurement, not a placeholder — `None`
is what "never measured" looks like.

**`claude-code` subtracts at finalization, not as it flushes.** It was once
exempt entirely, on the premise that because it marks the end of the previous
SDK event and reads again when the next message arrives, a tool's execution
falls *between* two windows rather than inside one. Measured, that premise does
not hold: a tool's timer starts at the **emission** carrying its `tool_use`
block, and one assistant turn spans several emissions, so a later emission's
window runs concurrently with a tool already timing. On a task issuing five
parallel writes, five reads and two concurrent `Bash` calls the overlap was
482 ms and 340 ms on two ~18-25 s turns, and the four-bucket residual came out
at exactly `-481 ms` and `-339 ms`; the other four overlapped by ~2.0-2.3 s on
the same task and still reconciled to within 1.2 ms, because they subtract it.

It cannot subtract while flushing, because a tool issued by an earlier emission
is still running when the next window closes and its interval does not exist
yet. `_ClaudeTurnState._subtract_tool_time_from_windows` therefore runs once at
finalization, when every span is known. After it, the same task reconciles to
**1.4 ms (0.006% of wall)** over four turns that all carried overlapping tool
calls.

**The head and tail are measured, not normalized.** Generation and tool are
only two of the four buckets. The turn's **head** (turn start → first
generation window) and **tail** (last window → turn end) are booked as
`TurnRecord.harness_startup_ms` / `harness_teardown_ms`, computed once at the
`EventCollector` seam by `coder_eval/timing.py::decompose_turn`. The tool term
is the **union** of the command intervals, for the same reason the subtraction
above is — Pi resolved a `Write` and a `Bash` overlapping by 18.4 ms in one
measured turn, and summing their durations books that overlap twice. The head
and tail exclude tool execution by that same rule and that same helper, which
is what keeps the four buckets disjoint: a tool is not confined to a
generation window (Antigravity force-closes an orphan at finalization, inside
the tail, and backgrounds anything over ten seconds), so a span that escapes
one would otherwise be counted both as tool and as head or tail. With all
four buckets and the union, six live turns per harness reconcile to within
1.7 ms of `duration_seconds` (worst case 0.014% of wall clock; the residual is
clock skew, since head and tail are measured between wall-clock event stamps
while `duration_seconds` is the agent's own monotonic span, and its sign flips
between harnesses). `scripts/timing/decompose_run.py` reproduces the table. The head and tail
figures in the table above are means of six live `tasks/hello_date` turns per
harness and move with CLI cache warmth, so read their ORDER OF MAGNITUDE, not
the digits.

What the head CONTAINS differs per harness and is deliberately **not**
decomposed, because the divergence is real and unfixable in both directions:

- On an **in-process SDK** (claude-code, antigravity) the first generation
  window starts at turn entry, so dispatch and time-to-first-token are already
  inside it. Excluding them is not possible — neither harness stamps a
  per-message arrival to fall back to, and `started_at == completed_at` would
  be the CE059 defect. **Read their `0.0` head as "nothing is left over", not
  as a measured interval**: the window actually opens marginally BEFORE the
  `AgentStartEvent` stamp (claude-code builds its turn state, then
  `_build_claude_query`, and only then emits the event), so the raw figure is
  negative and clamps. The setup between those two points is therefore booked
  as generation — **measured at 0.03 ms, and 0.10 ms with four plugin roots**,
  so it is the sub-millisecond skew the clamp exists for rather than hidden
  overhead. Emitting the event earlier would make the `0.0` a measurement
  instead of a clamp but would not change it, since the window's start stamp
  also precedes the build; only re-seeding the window after the build would
  surface that time, and that is the seeding change ruled out above.
  `TestClaudeHeadIsStructurallyZero` pins the build cost so this stays true.
- On a **subprocess harness** (codex, opencode, pi) the first window cannot
  start before the first event the CLI emits, so the head is one opaque
  interval fusing CLI boot, provider resolution, dispatch and TTFT. Measured on
  OpenCode: the process spawns in ~3 ms and its first `step_start` lands at
  ~3.9 s, with no marker in between.

So the fields are named for the **interval they measure**, never for what they
contain. Do not rename them `cli_boot_ms` or `ttft_ms` — that would claim a
split nobody performed. A measured `0.0` head is an answer; `None` is what
"never measured" looks like (a turn that produced no assistant message).

**Why Codex leaves `generation_completed_at` as `None`.** It means "when the
model finished emitting the `tool_use` block". Codex's stream does not carry
that per tool; deriving it from the flush time would be a guess. Note also that
`CommandTelemetry.timestamp` is the tool's own start on codex, antigravity,
opencode and pi, and the generation-completed moment on claude-code. Nothing
orders on it — `TurnRecord.commands` is sorted by `sequence_number` — but the
field's own docstring still describes only the claude-code reading.

**Codex `duration_ms` covers more than the command run.** Derived from the item
stamps, it is the item's lifecycle (queueing and approval included) rather than
the SDK's own narrower command-execution figure, which it deliberately overrides
— the SDK reported `0` for 70 of 211 commands in one nightly. `fileChange` and
generic tool items now carry a duration where they previously carried none, so
`avg_command_time_ms` and `total_command_time_ms` for a Codex run describe every
tool call rather than shell commands alone.

**`message_id` is what splits the timeline.** The evalboard groups assistant
emissions by `message_id`, and falls back to a wall-clock gap threshold
(`SAME_EMISSION_GAP_MS`, 100 ms, in `evalboard/lib/runs.ts`) when either side
lacks one. Antigravity's `Step` stream carries no message id, so the harness
synthesizes one — and it must, because this harness's generation windows are
*contiguous* by construction: each opens exactly where the previous one closed,
so the gap between two of them is always 0 ms and the fallback would fold a
whole turn's generations into a single row. Nothing about the numbers would
look wrong, because the consumer SUMS a group's token buckets and durations;
what is lost is per-generation thinking / text / tool attribution. CE060 makes
the kwarg mandatory in `src/coder_eval/agents/` for that reason. Note the two
synthetic schemes read differently on purpose: Codex deliberately REPEATS one
id across the sub-messages of a single generation — that is exactly the "the
CLI split one API response" signal the field exists to carry — while
Antigravity's are all distinct, because it emits one message per generation
with every block inside it. Runs recorded before a harness captured the field
still carry `null` and still depend on the gap fallback, which is why it stays.

### Known divergences

- **Delegate (`delegate-sdk`, out of tree)** records `duration_ms` but no
  execution bounds, so its tool calls cannot be placed on a timeline. Its
  coverage is ~88%. Mirror the Codex change in `coder_eval_uipath`
  (audit P3-1).
- **Antigravity books orphan-poll waiting as agent duration.** A task can spend
  `0.8 × turn_timeout` waiting on a tool call that never reaches DONE — 14 tasks
  and 9.6h of one 83h run. Only CLOSED tool intervals are subtracted, so that
  wait stays inside whichever generation window contains it, and the force-close
  records `execution_completed_at` while leaving `duration_ms` as `None`
  (audit P2-1).

- **`TurnStartEvent` is emitted at inconsistent points.** Antigravity and Codex
  fire it at turn entry, before the pump; claude-code, OpenCode and Pi fire it
  when a generation begins. Nothing in the timing accounting reads it — the
  head and tail are measured from the first and last `AssistantMessage`
  instead, which is uniform across all five — so this is recorded rather than
  fixed. It is NOT a `max_turns` hazard: `EventCollector.visible_turn_count` is
  `len(self._commands)`, derived from `ToolEndEvent`, and `_turn_starts` feeds
  only `assistant_turn_count` on the no-`AgentEndEvent` fallback path. The real
  cost of normalizing it is that the event drives the live renderers, so moving
  it changes the turn boundaries users watch during a run.

All three are deliberately deferred; see `c/time-bugs-audit.md` for the
measurements.

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

**OpenCode also keeps a native unit — its stream's own steps.** Unlike Codex and
Antigravity, `opencode run` executes a real multi-step agent loop per invocation
and streams it (`step_start` / `step_finish`), so the natural agent-loop unit
exists and is honored: `max_turns: N` allows N complete steps and cuts the run
when step N+1 begins, with the completed steps' tokens intact. A step is one
assistant generation and may carry several tool calls — so, as with claude-code,
the same number is a looser tool-call budget than on the visible-turn backends.

**Pi keeps a native unit too — its `turn_start` agent-loop steps.** Like OpenCode,
`pi -p --mode json` runs a real multi-step agent loop per invocation and streams it
(`turn_start` / `turn_end`), so `max_turns: N` allows N complete turns and cuts the
run when turn N+1 begins, with the completed turns' tokens intact. Pi streams
incrementally, so the cut genuinely stops spend mid-run. A Pi turn is one assistant
generation and may carry several tool calls — the same looser budget as claude-code
and OpenCode.

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

| | claude-code | codex | antigravity | pi |
|---|---|---|---|---|
| `<path>/skills/<name>/SKILL.md` (plugin root) | **required** | accepted | accepted | accepted |
| `<path>/<name>/SKILL.md` (bare skills dir) | **loads nothing** | accepted | accepted | **loads, but undetected** † |

claude-code hands the value to the SDK as a *plugin directory*, and a plugin's skills
live at `<plugin>/skills/<name>/SKILL.md`. Point it at the directory that directly
parents the skill directories and no skill loads. Codex
(`codex_agent._setup_skills`) and Antigravity (`antigravity_agent._resolve_skills_paths`)
both scan **both** layouts and take whichever actually holds a `<skill>/SKILL.md`.

† Pi uses the shared `_plugin_skill_dirs` resolver, whose bare-dir fallback resolves a
bare skills directory to itself and passes it as `--skill <dir>`, so the skill *does*
load and the agent can use it. But `skill_triggered` detects engagement by matching a
`skills/<name>/` segment in the read path (`_SKILL_PATH_RE`), which a bare dir lacks — so
an **activation suite** on a bare dir still scores recall 0 even though the skill ran.
Net effect for activation suites is therefore the same silent-0 as claude-code, via a
different mechanism; use the plugin-root shape (lint rule CE045 holds `SKILL_SOURCE_PATH`
to it for exactly this reason).

So `.claude/skills` works on two backends out of three and fails on the third — and
fails without an error. The agent simply is not offered the skill, every positive row
of an activation suite scores 0, and the suite reports recall 0.0. That is
indistinguishable from a skill that never triggers, which is the finding such a suite
exists to produce. It shipped in six documentation surfaces at once for exactly this
reason.

Probe it — but **read the namespace, not the presence**. Claude Code discovers a
project's own `./.claude/skills/` natively, independent of `--plugin-dir`, so run
from a repo root and BOTH commands list the skill: the deeper one only looks
correct. The plugin loaded iff the name carries the root's prefix.

```bash
# Run from a directory that is NOT the skill's own repo root.
claude --plugin-dir /path/to/root        # lists `root:<skill>`  <- plugin loaded
claude --plugin-dir /path/to/root/skills # lists nothing         <- loaded nothing
```

A bare `<skill>` with no prefix is project discovery, not your plugin.

**Write the plugin root.** It is correct on all three, so there is never a reason to
write the deeper form. For `.claude/skills/my-skill/SKILL.md` that is `.claude`.

Note what else that pulls in: a plugin root loads the **whole** plugin, so an
`agents/`, `commands/` or `hooks/` directory sitting beside `skills/` becomes visible
to the evaluated agent as well. Verified — a root holding `skills/probe-beta/`,
`agents/probe-subagent.md` and `commands/probe-cmd.md` offers all three as
`root:probe-beta`, `root:probe-subagent` and `root:probe-cmd`. Pointing a suite at a
repo's `.claude` therefore hands the agent every project subagent, which can answer a
request the skill was supposed to answer. Stage a minimal root when the suite must
isolate one skill.

`SKILL_SOURCE_PATH` — the variable `/coder-eval:check-skill` emits — is held to the
plugin-root shape by lint rule CE045. The rule keys on that variable name only; it is
**not** a statement that other variables may use the deeper form. `$PLUGIN_PATH`, for
one, feeds `experiments/plugin-comparison.yaml`, whose default agent is claude-code,
so the same requirement applies there and is unlinted.

**OpenCode and Pi both honor the *skills* half of a plugin.** OpenCode maps each
local plugin root to its `skills.paths`; Pi maps each to a `--skill <dir>` argument —
both via the same `_plugin_skill_dirs` resolver — so both **can** run activation
suites. A plugin's non-skill assets (agents/hooks/commands/MCP servers) are dropped on
both. See [OpenCode](OPENCODE.md) and [Pi § plugins](PI.md#known-limitations).

## Pi enforces `system_prompt` but not the tool allowlists

- **`system_prompt` is ENFORCED** (`--append-system-prompt`, semantics `append`) — a
  small win over OpenCode, which drops it.
- **`allowed_tools` / `disallowed_tools` are NOT enforced.** Pi's built-in tools are
  lowercase (`bash`/`read`/`write`/`edit`/`grep`/`find`/`ls`), but the shared config
  default (`experiments/default.yaml`) sets Claude-namespaced names
  (`Bash`/`Read`/`Write`/…). Forwarding those to `--tools` would allowlist tools that
  do not exist in Pi and strip the agent of ALL tools — so, like OpenCode (drops them),
  Codex (forwards `disallowed_tools` without SDK enforcement), and Antigravity (does not
  read them), Pi ignores them and runs with its full native toolset. A task that needs a
  restricted Pi toolset would have to name Pi's lowercase tools — a documented follow-up.
- **`permission_mode` is NOT enforced** — Pi headless print mode auto-runs tools and
  exposes only project-file trust (`--approve` / `--no-approve`), no tool-approval
  mode; the sandbox driver is the isolation boundary (same as Codex/Antigravity).
- **`system_prompt_file` is NOT read** (use inline `system_prompt`), matching
  Codex/Antigravity.
- **Built-in auto-retry.** Pi retries a transient/provider error *internally* (another
  `agent_start` cycle in the same invocation, flagged `willRetry: true`), which the
  harness folds into one turn. The internal retry is bounded by
  `turn_timeout` / `task_timeout`.

Full detail: [Pi](PI.md).

## Reproducing

`tasks/run_limits/` holds one fixture per limit: `max_turns_cap.yaml` asks for more
sequential work than its cap allows, and `turn_timeout.yaml` runs a command that
outlives its watchdog. Run either with `--type claude-code` / `--type codex` /
`--type antigravity` / `--type opencode` / `--type pi` to check a backend against the
contract above.

## Related

- [Claude Code](CLAUDE_CODE.md) · [Codex](CODEX.md) · [Antigravity](ANTIGRAVITY.md) · [OpenCode](OPENCODE.md) · [Pi](PI.md)
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full `run_limits` schema
