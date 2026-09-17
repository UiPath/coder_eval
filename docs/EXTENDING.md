---
description: >-
  Extend Coder Eval with a custom agent, a custom success criterion, or model
  pricing — the plugin SPI (coder_eval.plugins entry-point group), the Agent ABC
  checklist, the @register_criterion decorator, and register_pricing.
---

# Extending Coder Eval

Coder Eval is extensible along three seams, all designed so a third party can add
capability **without editing the base package**:

1. **Agents** — register a new `agent.type` via the plugin SPI.
2. **Criteria** — add a new success-criterion type via a decorator + auto-discovery.
3. **Pricing** — contribute USD rates for models your plugin runs.

This guide covers all three. For the internal architecture notes see
[CLAUDE.md](https://github.com/UiPath/coder_eval/blob/main/CLAUDE.md).

---

## 1. Custom agents (the plugin SPI)

Agents register through the **`coder_eval.plugins` entry-point group** — there is no
closed enum or dispatch to edit. `agent.type` is an open string validated against
the `AgentRegistry`; built-in and third-party agents travel the exact same path.

### Wire up the entry point

In your plugin package's `pyproject.toml`:

```toml
[project.entry-points."coder_eval.plugins"]
my_plugin = "my_plugin:register"
```

At CLI init, `load_plugins()` imports each entry point and calls it with the
`AgentRegistry` **class** (not an instance). A hook that raises stops the load with a
`PluginLoadError` that names the entry point, so every command fails until the plugin is
fixed or uninstalled. A broken plugin is never skipped.

### The `register` hook

Import everything from `coder_eval.spi`, the stable plugin surface. `SPI_VERSION`
changes whenever an exported name changes its signature. Every `register` call must
pass the SPI version the agent was written against, as the literal number: registration
raises `TypeError` when it is not the version this coder_eval provides.

```python
from coder_eval.spi import AgentRegistry

def register(registry: type[AgentRegistry]) -> None:
    # Bind type string → config class → agent class, for SPI 1.
    registry.register("my-agent", MyAgentConfig, spi_version=1)(MyAgent)
    # Optionally contribute pricing here too (see §3):
    # register_pricing(MY_RATES)
```

`AgentRegistry.register(agent_kind, config_class, *, spi_version)` returns a decorator,
so the decorator form works too:

```python
@AgentRegistry.register("my-agent", MyAgentConfig, spi_version=1)
class MyAgent(Agent[MyAgentConfig]):
    ...
```

Registration validates the pair and raises `TypeError` if the agent class declares no
`contract`, if its `tool_names` presence does not match its contract, or if the config
class is not a `BaseAgentConfig` with `extra="forbid"` whose `type` Literal names the
kind.

Registration is **anti-shadow**: re-registering the same `(agent_class,
config_class)` pair is a no-op, but claiming an existing `agent.type` with a
*different* implementation raises `ValueError`. Two plugins can never silently fight
over one type.

### The config class

Subclass `BaseAgentConfig` with your own `type` discriminator:

```python
from typing import Literal
from coder_eval.spi import BaseAgentConfig

class MyAgentConfig(BaseAgentConfig):
    type: Literal["my-agent"] = "my-agent"
    my_option: str = "default"
```

The factory `create_agent(kind, config, …)` raises `TypeError` if the passed config
isn't an instance of the registered `config_class`, so keep them paired.

### The harness contract

Every agent class declares which uniform `BaseAgentConfig` fields reach its harness.
A task that sets a field the contract marks `UNSUPPORTED`, a `permission_mode` value
outside `permission_modes`, or a tool name outside `CANONICAL_TOOL_NAMES` is rejected
at resolution, so `coder-eval plan` fails before any run. This is a JSONL CLI agent
that appends a system prompt and honors `plan` and tool lists natively:

```python
from coder_eval.spi import (
    Agent,
    Enforcement,
    HarnessContract,
    PermissionMode,
    TimingBasis,
    ToolNameMap,
    UsageGranularity,
)

# native tool name -> canonical (Claude) name; also used for telemetry
_TOOL_NAME_MAP = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit", "task": "Agent"}

class MyAgent(Agent[MyAgentConfig]):
    contract = HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.UNSUPPORTED,
        permission_mode=Enforcement.ENFORCED,
        permission_modes=frozenset({PermissionMode.PLAN, PermissionMode.BYPASS_PERMISSIONS}),
        allowed_tools=Enforcement.ENFORCED,
        disallowed_tools=Enforcement.ENFORCED,
        cooperative_stop=True,
        usage_granularity=UsageGranularity.STEP,
        timing_basis=TimingBasis.TURN_CLOCK,
    )
    tool_names = ToolNameMap.from_inverse(
        _TOOL_NAME_MAP,
        no_equivalent=frozenset({"Glob", "Grep", "NotebookEdit", "Skill", "TodoWrite", "ToolSearch", "WebFetch", "WebSearch"}),
    )
```

- `system_prompt_semantics` `append` / `replace` mean the text reaches the model through
  the system or developer instruction channel. A harness that can only prefix the user
  turn declares `system_prompt=UNSUPPORTED`.
- Declare a `permission_modes` value only if the harness gives it the Claude Code
  meaning (`plan` is read-only, `bypassPermissions` runs every permitted tool).
- `tool_names` is required exactly when a tool-list row is `ENFORCED`. It must map every
  canonical name; list a name your harness has no tool for in `no_equivalent`.
- `timing_basis` says who stamps the turn. `TURN_CLOCK`: the `TurnEmitter` stamps every
  tool and the turn bracket from one clock. `CLI_EPOCH_MS`: your harness reports its own
  stamps, and you pass them for every main-thread tool and window.
- Set `cooperative_stop=True` only if your `communicate()` honors `should_stop`
  (needed for criterion-level `stop_early:` arming and for `run_limits.max_tool_calls`
  to cut a turn). `False` means early stop is rejected at resolution for your agent.

### The `Agent` ABC — implementation checklist

Call `super().__init__(config, route, cost_log_tags=cost_log_tags)` first in your
`__init__`, and declare `cost_log_tags` as a keyword-only parameter: the factory passes
it on every LiteLLM route.

Implement these three abstract methods:

- [ ] `async def start(self, working_directory, *, env_path_prepend=None, plugin_tools_dir=None, plugin_root: Path | None = None) -> None`
- [ ] `async def communicate(self, user_input, *, iteration: int, stream_callback=None, timeout=None, should_stop: Callable[[], StopReason | None] | None = None) -> TurnOutcome`
- [ ] `async def stop(self) -> None`

`plugin_root` is the staged plugin root, or `None` when the task sets no plugins. It holds
`<root>/skills/<name>/SKILL.md` (every harness) and `<root>/plugins/<name>` (each authored
plugin whole, for a harness that loads full plugins). Deliver it the harness's native way; do not scan for skills.

`should_stop` is the run's single stop poll. The `TurnMonitor` owns it: it reads your
event stream and decides every stop (armed criteria, the tool-call cap, the token and USD
budgets). Your agent does not count or cap anything. The budgets read
`TurnEndEvent.tokens` as a per-report DELTA and `AgentEndEvent.usage` as the attempt's
authoritative total, so never report cumulative tokens on a `TurnEndEvent`. Declare how
often you report them as `usage_granularity`. With `cooperative_stop=True`:

- [ ] Call `should_stop()` at each safe boundary (for example, after each resolved
      tool call, before you pull the next unit of work).
- [ ] When it returns a `StopReason`, stop pulling work and remember the reason.
- [ ] End the turn with `emitter.finalize(end_status_for(reason))` (both names come
      from `coder_eval.spi`). Do not raise.

Optional overrides (sensible defaults exist): `kill()`, `kill_sync()` (called from a
non-asyncio watchdog thread — must **not** await).

Write the turn through one `TurnEmitter` (do **not** build events, messages or a
`TurnRecord` yourself):

- [ ] Open it with `emitter = self._open_emitter(prompt=user_input, iteration=iteration,
      model=..., task_id=..., stream_callback=stream_callback)` and call `emitter.begin()`.
- [ ] Report what the harness did: `begin_inner_turn` / `end_inner_turn(tokens=delta)`,
      `text`, `open_tool` / `close_tool`, and `add_generation(message_id=..., window=close_window(...), parts=[Generation(...)])`.
- [ ] Return `emitter.finalize(status, ...)` for a clean end, or
      `emitter.fail(AgentEndStatus.CRASHED | TIMEOUT, reason)` for a failed one. A crash or
      timeout is an outcome, not an exception; an exception out of `communicate` is a bug.
- [ ] On an `asyncio.CancelledError` from outside (`asyncio.current_task().cancelling()` is
      not 0), call `emitter.fail(AgentEndStatus.CRASHED, "turn cancelled")`, then re-raise: the
      orchestrator recovers the record from its own collector. A `CancelledError` your SDK
      raised inside the turn (`cancelling()` is 0) is a failure of the turn: return
      `emitter.fail(AgentEndStatus.CRASHED, reason)` and do not re-raise, or the task row is lost.
- [ ] Run an SDK turn body under `run_with_watchdog(...)`, and return
      `emitter.fail(AgentEndStatus.TIMEOUT, format_timeout_reason(timeout))` on `WatchdogFired`.
- [ ] Call `self._mark_stopped()` in `stop()` after your own teardown.

The emitter owns the event protocol: one `AgentStartEvent`, one `AgentEndEvent` on every
exit, balanced inner turns and tool calls (orphans closed `unresolved`), and the record.
That is why `coder_eval.spi` exports no event class and no `EventCollector`. What a turn
needs from it:

```python
from coder_eval.spi import (
    AgentEndStatus,        # the status you pass to finalize / fail
    Generation,            # one part of a model generation: blocks + its own token delta
    JsonlDecoder,          # the per-turn reducer of a SubprocessJsonlAgent
    StopReason,
    SubprocessJsonlAgent,  # the base for a CLI that streams nd-JSON on stdout
    TokenUsage,
    ToolEndStatus,
    TurnClock,
    TurnEmitter,
    TurnEndStatus,
    TurnOutcome,
    WatchdogFired,
    Window,                # the bounds of one generation window; close_window returns it
    close_window,
    end_status_for,
    run_with_watchdog,
)
```

### A JSONL CLI agent: `SubprocessJsonlAgent`

If your harness is a CLI that runs one process per turn and prints nd-JSON events on
stdout, subclass `SubprocessJsonlAgent`. The base owns the transport: the spawn (with
`stdin` on `/dev/null`), the stderr drain, the read loop against the turn deadline, the
cooperative stop, the crash and timeout outcomes, and the reap. You supply the argv, the
environment, and a `JsonlDecoder` that turns one event into emitter calls:

```python
import os
from typing import Any

from coder_eval.spi import (
    AgentEndStatus,
    Generation,
    JsonlDecoder,
    SubprocessJsonlAgent,
    TimingBasis,
    TokenUsage,
    ToolEndStatus,
    TurnEmitter,
    TurnOutcome,
    close_window,
)


class MyDecoder(JsonlDecoder):
    def __init__(self, emitter: TurnEmitter) -> None:
        super().__init__(emitter)
        self.mark = emitter.now()  # where the next generation window opens

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message":
            now = self.emitter.now()
            tokens = TokenUsage(output_tokens=int(event.get("output_tokens", 0)))
            self.emitter.add_generation(
                message_id=event.get("id"),
                window=close_window(mark=self.mark, now=now),
                parts=[Generation(blocks=[], tokens=tokens)],
            )
            self.mark = now
        elif kind == "text":
            self.emitter.text(str(event.get("text", "")))
        elif kind == "tool_start":
            self.emitter.open_tool(str(event["id"]), str(event["name"]), event.get("args") or {})
        elif kind == "tool_end":
            status = ToolEndStatus.ERROR if event.get("is_error") else ToolEndStatus.OK
            self.emitter.close_tool(str(event["id"]), status=status, summary=event.get("output"))
        elif kind == "error":
            self.error = str(event.get("message"))  # the base crashes the turn on it

    def end(self, status: AgentEndStatus, *, reason: str | None = None) -> TurnOutcome:
        if status is AgentEndStatus.CRASHED or status is AgentEndStatus.TIMEOUT:
            return self.emitter.fail(status, reason or status.value)
        return self.emitter.finalize(status)


class MyAgent(SubprocessJsonlAgent[MyAgentConfig]):
    contract = HarnessContract(..., timing_basis=TimingBasis.TURN_CLOCK)  # the emitter stamps the tools
    cli_name = "MyCli"
    docs_page = "docs/agents/MY_CLI.md"
    recognized_events = frozenset({"message", "text", "tool_start", "tool_end", "error"})
    decoder = MyDecoder

    def argv(self, prompt: str) -> list[str]:
        return ["my-cli", "--json", "--model", self.config.model or "default", prompt]

    def env(self) -> dict[str, str]:
        return dict(os.environ)

    async def start(self, working_directory: str, **_: Any) -> None:
        self.working_directory = working_directory

    async def stop(self) -> None:
        await self.kill()
        self._mark_stopped()
```

A clean exit that produced no event named in `recognized_events` crashes the turn as
format drift. The in-tree example is `src/coder_eval/agents/pi_agent.py`.

### The sixth-harness checklist

- [ ] A `HarnessContract` (every field, `timing_basis` included).
- [ ] A config class and its registration.
- [ ] A translation from config to the harness's native call that delivers the staged
      `plugin_root`.
- [ ] A decoder: one object per turn that takes the harness's events and calls the emitter.
- [ ] `async def harness_version(self)`: the CLI or SDK version the agent drives, recorded as
      `environment_info.harness_version`. A `SubprocessJsonlAgent` gets `<executable> --version`
      from its `executable` class attribute.
- [ ] The `coder_eval.testing` sensors in your own tests: `replay` your decoder over a
      recorded stream, `assert_identity_closes` on the replay, `assert_stream_balanced` on
      its events, `conformance(kind, probes)` for the contract, and
      `stop_conformance(kind, probe)` when the contract declares `cooperative_stop`.

### Test your adapter: `coder_eval.testing`

The in-tree suites and a plugin's tests call the same module. It does not import
`pytest`: each check raises `AssertionError`.

| Sensor | What it checks |
|---|---|
| `replay(stream, make_decoder, clock=ScriptedClock(origin), end=...)` | Drives your decoder over a recorded stream through a real `TurnEmitter`. A `Tick(at_ms)` element moves the clock. Returns the record, the events and the bracket stamps. |
| `assert_identity_closes(record, started_at=..., ended_at=...)` | Head + generation + tool union + tail equals the turn's span. |
| `assert_stream_balanced(events)` | Every opened inner turn and tool call closes, and one turn has one start and one end. |
| `await conformance(kind, probes)` | Your agent rejects every field its contract marks unsupported, and `probes` has one check for each enforced cell. |
| `await stop_conformance(kind, probe)` | For every `StopReason`, your agent ends the turn with that reason's status at the first boundary. `probe(stop, reason)` runs one `communicate()` over a scripted harness that calls `FIRST_TOOL_ID` then `SECOND_TOOL_ID`, passes `stop` (a `StopAfterFirstTool`) as both `stream_callback` and `should_stop`, and returns the tool ids your agent pulled. |

```python
from datetime import datetime

from coder_eval.spi import AgentEndStatus
from coder_eval.testing import ScriptedClock, Tick, assert_identity_closes, assert_stream_balanced, replay


def test_a_recorded_turn_balances_and_closes():
    stream = [
        Tick(10), {"type": "tool_start", "id": "t1", "name": "Bash"},
        Tick(50), {"type": "tool_end", "id": "t1"},
        Tick(80), {"type": "message", "id": "m1", "output_tokens": 12},
        Tick(90),
    ]
    result = replay(stream, MyDecoder, clock=ScriptedClock(datetime(2026, 1, 1)),
                    end=lambda d: d.end(AgentEndStatus.COMPLETED))
    assert_stream_balanced(result.events)
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)
```

### Worked example

The in-tree worked example is the BYOA test fixture at
[`tests/fixtures/byoa_demo_plugin/`](https://github.com/UiPath/coder_eval/tree/main/tests/fixtures/byoa_demo_plugin)
— a minimal package with a `register` hook and the `coder_eval.plugins` entry point.
(It subclasses `ClaudeCodeAgent` for brevity; a real third-party agent implements the
`Agent` ABC from scratch.)

---

## 2. Custom success criteria

Criteria are discovered automatically by `pkgutil` scan of the `coder_eval.criteria`
package — a new checker just needs the `@register_criterion` decorator. Adding one is
two steps: a Pydantic **model** (the YAML schema) and a **checker** (the logic).

### Step 1 — the model

In `models/criteria.py`, subclass `BaseSuccessCriterion` and set the discriminator as
a `Literal` default, then add it to the `SuccessCriterion` union:

```python
class MyCriterion(BaseSuccessCriterion):
    type: Literal["my_criterion"] = "my_criterion"
    target: str
    # Set requires_agent = True (ClassVar) if you read turn_records.

# ...and add `| MyCriterion` to the SuccessCriterion discriminated union.
```

Union membership is required — a run validates that every union member's `type` has a
registered checker, and rejects unknown `type` tags in YAML. `BaseSuccessCriterion`
gives you `description`, `weight` (default 1.0; `0` = informational/non-gating),
`pass_threshold` (default 0.9) and `suite_thresholds` for free, with
`extra="forbid"` so YAML typos are caught.

### Step 2 — the checker

Drop a file in `coder_eval/criteria/` with a `@register_criterion` class:

```python
from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult

@register_criterion
class MyChecker(BaseCriterion[MyCriterion]):
    criterion_type = "my_criterion"          # must match the model discriminator

    def _check_impl(self, criterion, sandbox, *,
                    turn_records=None, context=None) -> CriterionResult:
        # `context` carries the live run state: `context.route` (for criteria
        # that call a model) and `context.reference_dir` (the staged reference
        # copy, for criteria that grade against a reference solution).
        ok = ...  # your logic
        return CriterionResult(
            criterion_type=self.criterion_type,
            description=criterion.description,
            score=1.0 if ok else 0.0,
            details="...",
        )
```

Notes:

- **Do not override `check()` / `check_async()`** — both are `@final` and wrap
  `_check_impl` / `_check_impl_async` with error handling (an exception becomes
  a score-0.0 result with the error captured; a `JudgeInfrastructureError`
  escalates instead).
- Implement exactly ONE of `_check_impl` (plain sync — the common case, shown
  above) or `_check_impl_async` (genuine async I/O — an async HTTP client or
  subprocess bridge; see `llm_judge`/`agent_judge`). Whichever you implement,
  `BaseCriterion` derives the other for free (`asyncio.to_thread` / `asyncio.run`),
  so there is no need to hand-maintain both. Overriding neither, or overriding
  BOTH, raises `TypeError` immediately at class-definition time (a shared
  abstract base for a family of checkers that intentionally implements neither
  can opt out with the `abstract=True` class keyword — every one of ITS
  subclasses is still checked normally). `SuccessChecker.check_all_async`
  — the orchestrator's entry point — awaits every `_check_impl_async`-native
  checker directly on the event loop instead of pinning a thread, and offloads
  everything else to `asyncio.to_thread`. Criteria currently run SEQUENTIALLY
  (strictly in declaration order, matching the sync `check_all`); running
  multiple judge criteria concurrently is a follow-up.
- If your checker overrides `_check_impl_async`, it MUST NOT do blocking work
  (file I/O, subprocess calls) directly on the event loop — that would stall
  the orchestrator's own loop for the duration of the call. Offload blocking
  calls with `await asyncio.to_thread(...)` (see `llm_judge`/`agent_judge`,
  which do this for their sandbox/reference file reads).
- The derived sync bridge (`_check_impl`'s default `asyncio.run(...)` call) can
  only run when no event loop is already running — calling the public sync
  `check()`/`check_all()` on an async-only checker from inside a running loop
  raises `CheckerMisuseError` (escalates, like `JudgeInfrastructureError`)
  rather than returning a wrong score. Always reach for `check_async()` /
  `check_all_async()` from async code.
- Return `score` in `[0.0, 1.0]` — binary criteria use `0.0`/`1.0`; fractional ones
  anything in between.
- For **suite-level metrics** on dataset-backed tasks, override
  `aggregate(criterion, per_row_results)`; the base emits
  `count/mean/median/std/min/max`, so your criterion is suite-thresholdable for free.
  Classification-style criteria return a `ClassificationCriterionResult` and layer
  accuracy / precision / recall / F1 / confusion on top.
- For **early stop**, make your criterion model subclass `LiveSuccessCriterion`
  (`models/criteria.py`) instead of `BaseSuccessCriterion`, implement its
  abstract `live_decidable_polarities()` (a pure function of the criterion's
  own fields — no `turn_records`, no checker instance), and override the
  checker's `live_verdict(...)`. `LiveSuccessCriterion` subclassing is the
  single source of truth for "is this criterion type live-observable" —
  `validate_early_stop`/`TurnMonitor` check `isinstance(c,
  LiveSuccessCriterion)` directly, no separate checker-side flag. A lint rule
  (`tests/test_custom_lint.py::TestCE025LiveVerdictConsistency`) keeps the
  model subclassing and the checker's `live_verdict` override paired.
- Your `live_verdict` must be **deterministic** (a pure function of the
  `turn_records` prefix — no wall-clock, randomness, or hidden instance state)
  and **monotonic** (once it returns `"pass"`/`"fail"` for some prefix, every
  longer prefix returns that same verdict) — `TurnMonitor`'s verdict
  latching and deferred stops silently depend on both. Lint rule CE036
  (`tests/lint/live_verdict_contract.py`) enforces this by replaying each live
  criterion against every prefix of recorded trajectories, and **fails until
  you add `ContractCase` fixtures** for the new type in the same change,
  reaching every polarity its instances claim via
  `live_decidable_polarities()`. An out-of-tree plugin criterion is invisible
  to CE036's union walk — and the module lives under `tests/`, which is not
  shipped in the PyPI wheel — so copy the replay pattern (a `ContractCase`-style
  fixture plus the prefix-by-prefix determinism/monotonicity walk) into your
  plugin's own test suite, using `tests/lint/live_verdict_contract.py` in this
  repo as the reference implementation.

> A duplicate `criterion_type` **overwrites** the earlier checker with a warning (not
> a hard error, unlike agents) — keep type strings unique.

---

## 3. Model pricing

Plugins that run their own models contribute USD rates through `register_pricing` —
there is **no** separate entry-point group; call it from the same `register()` hook.

```python
from coder_eval.spi import ModelPricing, register_pricing

# Rates are per MILLION tokens: (input, output, cache_write, cache_read)
MY_RATES = {
    "my-model-v1": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "my-free-model": ModelPricing(0.0, 0.0, 0.0, 0.0),   # a valid free-model entry
}

def register(registry):
    registry.register("my-agent", MyAgentConfig, spi_version=1)(MyAgent)
    register_pricing(MY_RATES)
```

Behavior:

- **Keys** are the bare model id as it appears in `agent.model` (vendor/Bedrock
  region prefixes like `eu.` / `anthropic.` are normalized off at lookup).
- **Idempotent** for identical rates; **raises `ValueError`** on a *conflicting* rate
  for an existing key (built-in or another plugin). Registration is all-or-nothing —
  a late conflict leaves nothing half-applied.
- **Zero rates are valid** (a genuinely free model) — the lookup uses "is a rate
  present", not truthiness, so an all-zero entry prices to `0.0` rather than falling
  through to the built-in table.
- The plugin overlay is consulted **before** the built-in table, so every consumer
  (agents, reports, the cost simulator) prices your model transparently via
  `calculate_cost(model, uncached_input, output, cache_creation=0, cache_read=0)`.
  (Pass `uncached_input`, not the total input.)

The base package ships **no** plugin rates; only the built-in table.

---

## See also

- [Claude Code](agents/CLAUDE_CODE.md) · [Codex](agents/CODEX.md) ·
  [Antigravity](agents/ANTIGRAVITY.md) · [OpenCode](agents/OPENCODE.md) ·
  [Pi](agents/PI.md) — the built-in agents, each registered via this same SPI
- [Task Definition Guide](TASK_DEFINITION_GUIDE.md) — the criterion catalogue
- [CLAUDE.md](https://github.com/UiPath/coder_eval/blob/main/CLAUDE.md) — architecture
  and extension points in depth
