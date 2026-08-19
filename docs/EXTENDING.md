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
`AgentRegistry` **class** (not an instance). A third-party hook that raises is logged
and skipped; only a failing *built-in* registration is fatal.

### The `register` hook

```python
from coder_eval.agents.registry import AgentRegistry

def register(registry: type[AgentRegistry]) -> None:
    # Bind type string → config class → agent class.
    registry.register("my-agent", MyAgentConfig)(MyAgent)
    # Optionally contribute pricing here too (see §3):
    # register_pricing(MY_RATES)
```

`AgentRegistry.register(agent_kind, config_class)` returns a decorator, so the
decorator form works too:

```python
@AgentRegistry.register("my-agent", MyAgentConfig)
class MyAgent(Agent[MyAgentConfig]):
    ...
```

Registration is **anti-shadow**: re-registering the same `(agent_class,
config_class)` pair is a no-op, but claiming an existing `agent.type` with a
*different* implementation raises `ValueError`. Two plugins can never silently fight
over one type.

### The config class

Subclass `BaseAgentConfig` with your own `type` discriminator:

```python
from typing import Literal
from coder_eval.models import BaseAgentConfig   # importable from coder_eval.models

class MyAgentConfig(BaseAgentConfig):
    type: Literal["my-agent"] = "my-agent"
    my_option: str = "default"
```

The factory `create_agent(kind, config, …)` raises `TypeError` if the passed config
isn't an instance of the registered `config_class`, so keep them paired.

### The `Agent` ABC — implementation checklist

Implement these three abstract methods:

- [ ] `async def start(self, working_directory, *, env_path_prepend=None, plugin_tools_dir=None) -> None`
- [ ] `async def communicate(self, user_input, *, stream_callback=None, timeout=None, max_turns=None, should_stop=None) -> TurnRecord`
- [ ] `async def stop(self) -> None`

Optional overrides (sensible defaults exist): `kill()`, `kill_sync()` (called from a
non-asyncio watchdog thread — must **not** await), `discard_pending_turn()`.

Follow the shared turn lifecycle (do **not** hand-assemble a `TurnRecord`):

- [ ] Call `self._begin_turn()` at the top of `communicate()`.
- [ ] Call `self._end_turn_ok()` on the success path.
- [ ] Call `self._mark_stopped()` in `stop()` after your own teardown.
- [ ] Before raising on a mid-turn failure, set `self.pending_turn` to a
      `crashed=True` `TurnRecord` (built from an `EventCollector`), then raise
      `AgentCrashError` / `TurnTimeoutError` (bare — no payload). The orchestrator
      drains it and calls `discard_pending_turn()`.

Emit the standardized event protocol (you are the **sole emitter**): one
`AgentStartEvent` at the top of `communicate()` and one matching `AgentEndEvent` on
**every** exit path (emit from `finally`), a `TurnStart`/`TurnEnd` pair per inner
turn, and `ToolStart`/`ToolEnd` per tool call (close orphaned tools with
`status=unresolved`). Fan events through an internal `EventCollector` — it builds the
returned `TurnRecord`, the single agent-agnostic capture path.

Set `supports_cooperative_stop: ClassVar[bool] = True` only if your `communicate()`
actually honors `should_stop` (needed for criterion-level `stop_early:` arming). Leaving it
`False` means early stop is rejected at resolution for your agent — which is correct
if you can't stop cooperatively.

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

    def _check_impl(self, criterion, sandbox, reference_code=None, *,
                    turn_records=None, context=None) -> CriterionResult:
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
  `validate_early_stop`/`EarlyStopWatcher` check `isinstance(c,
  LiveSuccessCriterion)` directly, no separate checker-side flag. A lint rule
  (`tests/test_custom_lint.py::TestCE025LiveVerdictConsistency`) keeps the
  model subclassing and the checker's `live_verdict` override paired.
- Your `live_verdict` must be **deterministic** (a pure function of the
  `turn_records` prefix — no wall-clock, randomness, or hidden instance state)
  and **monotonic** (once it returns `"pass"`/`"fail"` for some prefix, every
  longer prefix returns that same verdict) — `EarlyStopWatcher`'s verdict
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
from coder_eval.pricing import ModelPricing, register_pricing

# Rates are per MILLION tokens: (input, output, cache_write, cache_read)
MY_RATES = {
    "my-model-v1": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "my-free-model": ModelPricing(0.0, 0.0, 0.0, 0.0),   # a valid free-model entry
}

def register(registry):
    registry.register("my-agent", MyAgentConfig)(MyAgent)
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
  [Antigravity](agents/ANTIGRAVITY.md) — the built-in agents, each registered via
  this same SPI
- [Task Definition Guide](TASK_DEFINITION_GUIDE.md) — the criterion catalogue
- [CLAUDE.md](https://github.com/UiPath/coder_eval/blob/main/CLAUDE.md) — architecture
  and extension points in depth
