# Closing the grading-material leak: two approaches compared

> **Scope note.** This is a design-decision document about *sandbox isolation*. It is unrelated to
> `docs/comparison.md`, which compares coder_eval to other eval frameworks (SWE-bench, Harbor, …).

- **Approach A** — [PR #88](https://github.com/UiPath/coder_eval/pull/88),
  `fix/docker-isolation-copy-prune`: **COPY/PRUNE + GRADE-OUTSIDE**. Open, draft, 8 commits,
  +5,754 / −214 across 35 files. Implemented and verified.
- **Approach B** — **Declarative trust split + DAC permissions**: `input_dir` / `reference_dir` on
  `TaskDefinition`, `$INPUT_DIR` / `$REFERENCE_DIR` substitution, agent runs as an unprivileged uid,
  `reference_dir` is root-owned `0700`, `task.yaml` itself lives in `reference_dir`. A design, not
  yet built.

---

## The problem

When coder_eval grades an agent, the grading material sits on the same filesystem the agent is
working in — **including under `--driver docker`, which is the mode users assume is safe**.

On `main`, an agent in the container can reach all of the following:

| Surface | Where it lives today |
|---|---|
| Full `success_criteria` | `/work/input/task.yaml`, staged verbatim (`docker_runner.py:605-620`) |
| Raw task YAML text | `context.json.source_yaml` |
| Reference solution | `reference.file` / `reference.directory`, auto-mounted (`:1253-1254`) |
| Task-dir graders (`check_*.py`, `RESOLUTION.md`, reference agents) | host task dir bind-mounted at its own host path (`:1176`) |
| Signposts pointing at all of the above | `TASK_DIR` / `SKILLS_REPO_PATH` in the agent's env |

The agent and the harness run as the **same user** in the container — both root, since neither
`docker/Dockerfile` nor `docker/Dockerfile.runtime` sets `USER` and `_build_argv` passes no
`--user`. So there is no permission barrier of any kind: a single `grep -r` reaches the answer key.

**This is measured, not theoretical.** PR #88 reports a nightly audit in which **~2.4% of replicates
read the suite instead of solving the task**, and an adversarial "check `/work`" prompt drives that
to **~100%**.

A separate but adjacent defect: `~/.uipath` (including `.auth`) was mounted **read-write from the
host original**, so an agent could overwrite operator credentials.

The root cause is structural: **the harness lives inside the sandbox it is supposed to be isolating
from.** The in-container process runs the agent *and* grades it (`run_task_internal_command.py:190-209`),
so the rubric has to be shipped into the container at `t=0`.

---

## Approach A — COPY/PRUNE + GRADE-OUTSIDE (PR #88)

**Closes the leak by absence, not by permission.** The container never contains grading material.

Three coordinated moves:

1. **COPY/PRUNE the agent's inputs.** The staged `task.yaml` is criteria-stripped via
   `agent_safe_dump()` (`success_criteria: []`, `reference: null`) and `context.json.source_yaml` is
   nulled. Plugins are projected to a *sanitized bundle copy* (`project_plugin_for_agent`, allowlist
   `PLUGIN_AGENT_ALLOWED_SUBDIRS` = `skills`/`commands`/`agents`/`hooks`/`.claude-plugin`) mounted
   `:ro` at `/work/skills`. The raw checkout, the reference, and the host task dir are **not mounted
   at all**. A **grader-dir overlap guard** hard-errors any `template_sources` / `system_prompt_file`
   / `extra_mounts` whose source contains the task dir.
2. **GRADE OUTSIDE.** The container runs the agent only. After it exits, the host grades the
   copied-out artifacts via `regrade_on_host`, using the full unstripped `TaskDefinition` it still
   holds, with `TASK_DIR` pointing at the real host task dir. Re-grade is restricted to an allowlist
   of final statuses (`SUCCESS` / `FAILURE` / `MAX_TURNS_EXHAUSTED`).
3. **Copy-then-mount host state.** `~/.uipath` and `~/.claude` are forwarded as throwaway rw
   **copies**; `~/.claude/jobs` (operator session timelines) is denylisted.

Plus **harness-outside**: `pre_run`/`post_run` invoke helper scripts under the skills-repo `tests/`
tree, which is no longer mounted. `post_run` becomes host-only; `pre_run` gains a per-command
`runs_in: host|agent` (default `host`, seeding the workspace through a `:ro` `/work/seed` staging
mount). The PR body flags this as a known blocker affecting **243 of 1,147 downstream tasks (~21%)**;
the branch implements the fix, with only the ~7 `uv sync` tasks needing `runs_in: agent`.

**Verification shipped with it:** Detector A (host-unchanged — daemon-less mount-shape proxy in CI
plus an `-m live` byte-and-metadata-identical real run), Detector B (zero-grading-material in the
agent mount view, with a positive control so "staged nothing" can't pass vacuously), a baked-image
answer scan, and live confirmation that an adversarial "read your grader" task fails on gpt / claude
/ gemini / kimi.

**Stated costs:** early stop is **disabled under docker** (criteria are gone, so the in-container
watcher can't arm — a warning is logged, verdict unaffected), and the design doc states plainly that
**allowlist-by-absence has no DAC backstop** — if the prune boundary is wrong, nothing else stops the
read.

## Approach B — Declarative trust split + DAC permissions

**Closes the leak by permission, with an author-declared boundary.**

- `TaskDefinition` gains `input_dir` (agent-visible fixtures) and `reference_dir` (answer key),
  substituted as `$INPUT_DIR` / `$REFERENCE_DIR` inside the task file.
- `task.yaml` itself lives in `reference_dir`, so the rubric is protected by the same single rule as
  everything else — no special case.
- In the container, the agent runs as an unprivileged uid; `reference_dir` is root-owned `0700`;
  `input_dir` and the workspace are chowned to the agent uid.
- Effectively this **splits the existing `$TASK_DIR` token into its two halves by trust level** —
  which is precisely why the leak exists today, since one token points at a directory mixing both.

Refinements this needs to be sound:

- **Scope-aware substitution.** `$REFERENCE_DIR` must be a hard validation error in agent-facing
  fields (`initial_prompt`, `system_prompt`, mutations) — resolving it there advertises the path and
  leaks it into logs and reports. `$INPUT_DIR` substitutes anywhere.
- **Copy, don't mount, `reference_dir`.** The host bind mount is `:ro`, so `chmod` fails `EROFS`;
  making it writable would mutate the user's real repo permissions with no crash-safe restore. Stage
  a container-local root-owned copy — which also lets the symmetric same-host-path mount disappear.
- **Static, not scoped, permissions.** A `with block_access(): …` context manager that flips modes
  on entry and restores on exit is not crash-safe (SIGKILL between enter and exit leaves them
  flipped). With two identities, the perms are simply set once at stage time and never restored.
- **Dataset files default to `reference_dir`** — for activation suites the JSONL rows carry
  `expected_skill`, which *is* the answer key.
- The `~/.claude` credential copy must stay agent-writable, so `0700`-root cannot be applied
  blanket — it is per-path.

---

## Pros and cons

| | Pros | Cons |
|---|---|---|
| **A — COPY/PRUNE + GRADE-OUTSIDE** (PR #88) | Closes the leak **even against a root agent** — no identity work needed. Fails safe: a file that isn't there can't be read. Also closes surfaces B doesn't touch: host-credential overwrite (`~/.uipath` rw), `~/.claude/jobs` exposure, env signposts, baked-image content. **Built, tested, and empirically verified** (detectors in CI + live adversarial probe across four models). Requires **no per-task authoring changes** for the leak fix itself. Removes the harness from the sandbox — the structural root cause. | **Early stop disabled under docker** — a shipped feature silently degrades on one driver. Forced `pre_run`/`post_run` host-side, touching **~21% of downstream tasks** and adding a new `runs_in` concept. **No DAC backstop** (the PR says so): the prune allowlist is "a coder_eval-side guess about what is answer-free", so a plugin putting answers in `skills/` defeats it. Detector B catches known sentinels, not an unknown golden file. Large blast radius: **+5,754 LOC / 35 files**, restructuring `orchestrator.py`. Nothing protects `tempdir`. |
| **B — Trust split + DAC permissions** | Supplies exactly the **DAC backstop A lacks** — a second, independent barrier. The boundary is **author-declared**, not framework-guessed, replacing A's residual risk #1 with an explicit contract. **Preserves early stop** (criteria stay in container memory, which the agent can't read) and **keeps `pre_run`/`post_run` in-container** (they run as root and can still read `reference_dir`) — no `runs_in`, no 243-task migration. Much smaller runtime change: a uid on spawn plus chowns at setup. Conceptually simple: one rule, "`reference_dir` is harness-only". | **Depends entirely on identity separation that doesn't exist yet** — both processes are root today, so `chmod 0700` is a *no-op* until the agent runs as a separate uid. Fails **open and silently**: one missed chown, one new mount, and there's no absence to fall back on. Real friction running the agent CLI unprivileged (HOME, npm cache, the `~/.claude` copy must stay writable). Pushes classification onto **1,147 downstream task authors** unless the default is "protected unless declared input". Does **not** address host-credential overwrite, baked-image content, or env signposts. **Also nothing for `tempdir`** (no second identity on the host). Unbuilt and unverified. |

### Head-to-head

| Axis | A — Absence | B — Permissions |
|---|---|---|
| Works against a root agent | ✅ | ❌ (requires uid split first) |
| Failure mode | Fails safe (nothing to read) | Fails open (silently, if a chown is missed) |
| Boundary defined by | Framework allowlist (a guess) | Task author (a declaration) |
| Early stop under docker | ❌ disabled | ✅ preserved |
| `pre_run` / `post_run` | Moved host-side; `runs_in` added; ~21% of tasks affected | Unchanged, in-container |
| Host credential / `~/.claude/jobs` / baked image | ✅ covered | ❌ out of scope |
| `tempdir` driver | ❌ | ❌ |
| Per-task migration | None for the leak fix | `input_dir` / `reference_dir` across the suite |
| Framework blast radius | +5,754 LOC, 35 files | Smaller runtime change; new schema + token rules |
| Status | Implemented, CI-gated, live-verified | Design only |

---

## Assessment

**These are not competing designs — B is the missing layer under A.**

PR #88's own residual-risk list names its weakest point: *"Allowlist-by-absence has no DAC backstop
… `PLUGIN_AGENT_ALLOWED_SUBDIRS` is a coder_eval-side guess about what is answer-free."* That is
precisely the gap Approach B fills, in two independent ways:

1. **As a mechanism** — a uid split gives the second barrier, so a prune-boundary miss is no longer
   game over.
2. **As a contract** — and this matters more. `input_dir`/`reference_dir` replaces the framework's
   guess with an author declaration, which is the durable fix PR #88 already identifies as a
   cross-repo follow-up ("push the agent-bundle boundary into the skills repo — a manifest declaring
   the agent-safe surface"). Approach B *is* that manifest, expressed in the task schema.

If only one ships, it should be **A**: it is built, measured against real leak rates, gated by
detectors in CI, and it closes several surfaces B never touches. B's mechanism half is also blocked
on identity work (non-root agent, credential-copy ownership) that A doesn't need.

The sequencing that gets the most value:

1. **Land A.** It is the structural fix — it removes the harness from the sandbox.
2. **Adopt B's declarative half next**, as the durable replacement for the prune allowlist. It is
   the higher-value part of B and is independent of the permission mechanism.
3. **Add B's uid split as defense-in-depth** where material must remain in-container.
4. **Revisit early stop.** A disables it under docker; the leak-free restoration is a host-side
   watcher over the live event stream — the host already receives every `ToolStartEvent` /
   `ToolEndEvent` with full `CommandTelemetry` (`streaming/wire.py`), and can already signal the
   container through the heartbeat channel. Worth first confirming the feature earns its keep: every
   `stop_early` usage in this repo is a fixture for testing early stop itself.
5. **State plainly that `tempdir` is not cheat-resistant** under either approach. Neither has a
   boundary there; it should be documented as a development driver.
