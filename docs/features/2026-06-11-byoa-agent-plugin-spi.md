# Bring-Your-Own-Agent (BYOA) Plugin SPI

**Status**: shipped · **Date**: 2026-06-11

coder-eval discovers custom coding agents from any installed package through a
Python entry-point plugin SPI. A third party can add a new `agent.type` — usable
in any task YAML and on the `-D agent.*` CLI overrides — **without editing
coder-eval**.

## What it does

- A package declares an entry point in the `coder_eval.plugins` group pointing at
  a `register(registry)` callable.
- At startup coder-eval calls `load_plugins()`, which scans every installed
  distribution for that group and invokes each `register` hook with the
  `AgentRegistry`.
- The plugin binds a *kind string* to an `(agent_class, config_class)` pair.
- From then on `agent: {type: <kind>}` validates, merges, and dispatches to the
  plugin's agent — the registry, not a closed enum/union, is the source of truth.

coder-eval registers its own built-in agents (Claude Code, Codex, NoOp) through
the *same* entry point (`coder_eval = "coder_eval.agents:register_builtins"`), so
the discovery path is always exercised and cannot silently rot.

## Authoring a plugin

`pyproject.toml`:

```toml
[project]
name = "my-cool-agent"
dependencies = ["coder-eval>=0.2,<0.3"]   # pin a compatible range

[project.entry-points."coder_eval.plugins"]
my_cool_agent = "my_cool_agent.plugin:register"
```

`my_cool_agent/plugin.py`:

```python
from typing import Literal
from coder_eval.agent import Agent
from coder_eval.models import BaseAgentConfig

class MyAgentConfig(BaseAgentConfig):
    type: Literal["my-cool-agent"] = "my-cool-agent"   # the kind used in task YAML
    some_custom_field: str = "default"

class MyAgent(Agent[MyAgentConfig]):
    async def start(self, working_directory, **kw): ...
    async def communicate(self, user_input, **kw): ...   # returns a TurnRecord
    async def stop(self): ...

def register(registry):  # registry is the AgentRegistry class (type[AgentRegistry])
    registry.register("my-cool-agent", MyAgentConfig)(MyAgent)
```

> **A real agent must satisfy the full `Agent` ABC contract**, not just the three
> methods above: emit the standardized event protocol, drive the turn lifecycle
> (`_begin_turn` / `_end_turn_ok`), build the returned `TurnRecord` via an
> `EventCollector`, and implement `kill_sync()` if it holds OS resources. See the
> "Adding a New Agent" section in `CLAUDE.md` and the built-in agents in
> `coder_eval/agents/` as references. The simplest path is to subclass an existing
> agent (as the test fixture does) and override only what differs.
>
> **Guardrail asymmetry:** the `_FRAMEWORK_OWNED_SDK_FIELDS` denylist and the
> `get_sdk_options()` / `get_environment_info()` report hooks are defined on the
> built-in config/agent classes, not on `BaseAgentConfig` / `Agent` themselves. A
> plugin that subclasses a built-in inherits them; a plugin written directly
> against the base classes does not — it skips the SDK-option guardrail and renders
> a thinner report. Subclass a built-in to inherit both.

After `pip install coder-eval my-cool-agent`:

```yaml
agent:
  type: my-cool-agent
  some_custom_field: hello
```

works in any task YAML, and `-D agent.some_custom_field=...` validates against the
plugin's config class.

> **Entry points are install-time metadata.** Editing `pyproject.toml` is not
> enough — (re)install the package (`pip install -e .` / `uv sync`) so the entry
> point is written into the distribution metadata `load_plugins()` scans.

## Where it fits in the evaluation flow

| Seam | Behavior |
|---|---|
| Discovery | `coder_eval/plugins.py::load_plugins()` — idempotent, re-entrancy-safe, isolates a failing plugin (logs + skips). Called at CLI init; `ensure_plugins_loaded()` is the lazy safety-net for library/test use. |
| Registration | `AgentRegistry` is keyed by kind string (`str | AgentKind`); `register` / `get` / `list_kinds` / `registrations`. |
| Config dispatch | `parse_agent_config(type=...)` looks the kind up in the registry and validates against its config class. `type=None` returns a bare `BaseAgentConfig` (deferred resolution). Unknown kind → `ValueError` listing registered kinds. |
| Task schema | `TaskDefinition.agent` and `EvaluationResult.agent_config` use `ResolvedAgentConfig` = `Annotated[SerializeAsAny[BaseAgentConfig], BeforeValidator(coerce)]`: a dict is coerced to its registered subclass, and subclass-only fields survive `model_dump()`/reload (so a plugin agent's config round-trips into `task.json`). |
| CLI merge | `config_merge._root_model_types("agent")` enumerates the registry, so `-D agent.<plugin_field>` gets validation + did-you-mean for free. |

## Backward compatibility

Fully preserved. `AgentKind` remains a `StrEnum`; existing task YAMLs
(`type: claude-code | codex | none`) validate and serialize identically; all model
exports (`AgentConfig`, `parse_agent_config`, …) are unchanged. Because `AgentKind`
is a `str` subclass, `AgentKind.NONE == "none"`, so `type` stored as a plain string
keeps `is_none_agent` and all built-in comparisons working.

## Scope

This SPI opens the **agent** seam only. Criteria already have their own
auto-discovery (`@register_criterion`); API routes/backends, CLI sub-apps, and the
evalboard storage driver are separate future seams (see `proposal.md` §5).

## Tests

- `tests/test_plugins.py` — discovery, idempotency, failure isolation, the
  built-in entry point.
- `tests/test_agent_config_registry_dispatch.py` — dispatch, `SerializeAsAny`
  round-trip (incl. a non-union plugin kind), `-D` validation, open `agent_type`.
- `tests/test_byoa_plugin.py` — offline end-to-end through the out-of-tree fixture
  plugin (`tests/fixtures/byoa_demo_plugin`): discovery → task validation → merge →
  `create_agent`, no core edits.
- `tests/test_byoa_plugin_live.py` (`-m live`) — the fixture plugin's agent runs a
  **real** Anthropic turn; wired into the `byoa-live-tests` CI job in
  `.github/workflows/pr-checks.yml`.
