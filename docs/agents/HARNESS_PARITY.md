# Harness Config Parity

One task file, run on three harnesses, must be the same task. This page is the
contract for how each shared `agent:` field is implemented on Claude Code, Codex,
and Antigravity — and, where a backend genuinely cannot implement one, what it
does instead.

The declarations here are not prose: each agent class carries them as
`config_support`, and resolution rejects a task that sets a field its agent
declares it cannot honor.

## Support states

| State | Meaning | What happens |
|-------|---------|--------------|
| **honored** | Implemented faithfully. | Nothing to declare — the default. |
| **approximated** | Acted on, with a documented divergence. | The agent warns at `start()`; resolution allows it. |
| **unhonored** | Read by nothing. | Resolution **hard-errors** if the task sets it to a non-default value. |

An agent declares only its divergences. An empty `config_support` asserts it
honors every shared field — so a field silently dropped without a declaration is
a bug, not a shortcut.

## The table

| Field | claude-code | codex | antigravity |
|---|---|---|---|
| `model` | honored | honored | honored |
| `system_prompt` / `system_prompt_file` | honored (`--append-system-prompt`) | honored (`developer_instructions`) | honored (`system_instructions` section) |
| `allowed_tools` | honored | honored (`enabled_tools`) | honored (`CapabilitiesConfig.enabled_tools`) |
| `disallowed_tools` | honored | **approximated** — forwarded as `disabled_tools`, not enforced by the SDK | honored (subtracted from the allowlist) |
| `permission_mode` | honored | **approximated** — every mode runs full-access | **approximated** — every mode runs `policy.allow_all()` |
| `plugins` | honored | honored (symlinked into `.agents/skills/`) | honored (`skills_paths`) |
| `run_limits.max_turns` | honored (native SDK turn cap) | honored (visible-turn cap) | honored (visible-turn cap) |
| `run_limits.stop_early` | honored | honored | honored |

## `system_prompt` means *append*

`agent.system_prompt` is extra text **appended to the harness's own default agent
prompt**. It does not replace it.

Append is the only semantics all three can express safely. Full replacement is
expressible too, but a task-level guardrail (`"Do not access files in sibling
runs/* directories"`) is not a whole agent prompt — substituting one for Codex's
base instructions or Antigravity's core mandates would gut the harness rather than
constrain it. So the field is defined as the safe one, and each backend maps it to
its own additive knob:

| Harness | Additive knob | Replacement knob (deliberately unused) |
|---|---|---|
| claude-code | `--append-system-prompt` | `--system-prompt` |
| codex | `developer_instructions` | `base_instructions` |
| antigravity | `system_instructions` (str → `TemplatedSystemInstructions`) | `CustomSystemInstructions` |

Write task guardrails here, not a persona.

> **Note on the claude-code change.** This field previously mapped to
> `--system-prompt`, which *replaced* Claude Code's prompt — and because the SDK
> emits `--system-prompt ""` for `None`, a run that set nothing got **no** system
> prompt at all, while Codex and Antigravity kept their full vendor prompts. Both
> cases now route through the preset, so every harness starts from its own default
> prompt and adds the task's text on top. Expect claude-code numbers to move
> against a pre-change baseline.

## `max_turns` counts visible turns on Codex and Antigravity

A "visible turn" is one entry in the run's timeline: one resolved tool call. It is
the unit `reports_stats.visible_turn_count` reports and the unit that lands in
`TurnRecord.commands`. Both backends count it live off the shared
`EventCollector.visible_turn_count`, so one `max_turns` value means one thing on
both.

They need their own counter because a native one would be meaningless: Codex and
Antigravity each deliver exactly **one SDK turn per `communicate()` call**, so an
SDK-level cap would clamp at 1 no matter what the task asked for. Before this,
both simply ignored the field.

The cap is enforced on the same loop boundary as the cooperative early stop: the
step or notification that reaches the cap is processed whole, and the next one is
never pulled. The in-flight turn is then cancelled server-side (best effort) so
the cap actually stops spend. A run cut this way finalizes cleanly as
`max_turns_exhausted` — it is not a crash, and it is not retried.

**claude-code keeps its native SDK cap**, which counts assistant messages instead.
That is a real, honored cap, so it is left alone rather than restated in a
different unit; the same `max_turns: 20` therefore bounds slightly different things
on claude-code than on the other two. Documented rather than papered over.

## `permission_mode` does not confine any harness

On Codex and Antigravity, every mode runs unconfined, by design:

- coder_eval's isolation boundary is the **sandbox driver** — a Docker container,
  or an ephemeral per-task tempdir it creates and discards. An in-agent approval
  policy on top of that is redundant.
- Codex's own OS sandbox actively breaks on the paths we run: Landlock is
  unavailable inside the container, the `bwrap` re-exec is denied on constrained CI
  agents, and Windows has no OS sandbox at all. In each case writes fail silently
  and the task scores 0 with no loud error.
- The modes below `bypassPermissions` differ only in *what they would ask a human
  about*, and there is no human on a headless eval path.

This is declared as **approximated** rather than unhonored — the isolation the
field implies is provided, one layer down — so setting `bypassPermissions` on a
nightly does not fail resolution.

**For adversarial or untrusted evals, use the Docker driver.** The tempdir/host
driver is a working directory, not a confinement boundary, on any of the three.

## Related

- [Claude Code](CLAUDE_CODE.md) · [Codex](CODEX.md) · [Antigravity](ANTIGRAVITY.md)
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full `agent:` schema
- [Extending Coder Eval](../EXTENDING.md) — declaring `config_support` on a new agent
