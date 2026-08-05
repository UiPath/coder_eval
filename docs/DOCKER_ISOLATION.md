---
description: >-
  Run each Coder Eval task in its own fresh Docker container — strong host
  isolation, a pinned reproducible agent runtime, and custom images for
  task-specific dependencies.
---

# Docker Isolation

Run each evaluation task inside its own fresh container. Strong host isolation and a pinned, reproducible agent runtime.

## When to use

Set `sandbox.driver: docker` on a task (or pass `--driver docker` on the CLI —
a thin alias for `-D sandbox.driver=docker`) when you want:

- **Isolation from the host filesystem/network** — agent-generated code can't reach files outside the sandbox.
- **A pinned toolchain** — the image bakes in Python 3.13, Node 22 LTS, `@anthropic-ai/claude-code`, `uv`, and the matching `coder_eval` version, so results don't drift with host upgrades.

Aggregation (P/R/F1, suite thresholds, reports) always stays on the host. Each container is a sealed "run one task → emit one `task.json`" worker.

## One-time setup

```bash
make docker-image        # core + both built-in agents (default; no credentials)
# opt in to the UiPath extra (resolves from public PyPI; no credentials):
make docker-image-full
```

Both build `coder-eval-agent:<pkg-version>` and tag it `:latest`.

- **`make docker-image`** installs the core package plus **both built-in agents** — claude-code (baked above) and Codex (`--extra codex`, public PyPI). It needs **no credentials** and covers the common case: claude-code or Codex tasks scored with `run_command` / `file_contains` (incl. converted skillsbench tasks). `llm_judge` / `agent_judge` work here too (they route through the run's Anthropic/Bedrock backend).
- **`make docker-image-full`** additionally installs the `uipath` extra. The `uipath` SDK resolves from **public PyPI** (per `uv.lock`), so the build needs **no credentials**. Use this only for tasks that shell out to the in-host `uipath` CLI. (Codex is already in the default image — no extra needed.)

> **Codex sandbox under Docker.** Codex's Landlock-backed `read-only` / `workspace-write` sandboxes can't initialize inside the eval container — their writes/execs fail silently and the agent produces no artifacts (a `score=0` FAILURE with no loud error). The docker runner sets `CODER_EVAL_IN_CONTAINER=1`, and the Codex agent honors it by falling back to `full-access`: the container itself is the trust boundary. Host runs (tempdir) are unaffected — Landlock works there and the marker is unset. So Codex tasks run under `--driver docker` with their natural `acceptEdits` permission mode; no need to set `bypassPermissions` by hand.

## Running a task in Docker

```bash
# Single task
coder-eval run path/to/task.yaml --driver docker

# All tasks
coder-eval run --driver docker

# Or in the task YAML
sandbox:
  driver: docker
  docker:
    network: bridge         # or "none" for sealed runs
    image: my-custom:tag    # override the default image
```

## Using a pre-built custom image

When your tasks need extra tools or dependencies, extend the framework image once and point tasks at
the result — no per-task build. The custom image must extend `coder-eval-agent:<version>` so it
inherits the coder-eval runtime, the entrypoint script, and the `org.coder-eval.version` label the
host's preflight check reads. (For a task whose Dockerfile can't be rebased onto Debian, use
[the runtime kit](#tasks-that-bring-their-own-base-image-the-runtime-kit-coder-eval-runtime)
instead. To have coder-eval build the image per task rather than pre-building it, see
[Building the image from a task Dockerfile](#building-the-image-from-a-task-dockerfile).)

```dockerfile
FROM coder-eval-agent:<version>          # match the version your host runs
RUN apt-get update && apt-get install -y --no-install-recommends custom-tool \
    && rm -rf /var/lib/apt/lists/*
```

```bash
docker build -t my-team/image:latest .
```

Select it either in the task YAML:

```yaml
sandbox:
  driver: docker
  docker:
    image: my-team/image:latest
```

…or from the CLI, which overrides whatever the YAML says:

```bash
coder-eval run task.yaml -D sandbox.docker.image=my-team/image:latest
```

The default when you set nothing is `coder-eval-agent:<installed package version>`.

A worked example ships in-tree: `tasks/byod_smoke_test.yaml` runs against
`templates/byod_smoke_test/Dockerfile`, which extends the framework image and drops a marker file
that the task's success criterion then asserts — proving the custom image was actually used. (The
`byod_*` names here mean "Bring Your Own **Docker**"; they are unrelated to the
[Bring Your Own Dataset](DATASETS.md) guide, which is about fanning one task out over data rows.)

```bash
make docker-image                                                   # base image first
docker build -t byod-custom-image:0.1.0 templates/byod_smoke_test/  # then the derived one
coder-eval run tasks/byod_smoke_test.yaml
```

### Troubleshooting custom images

| Symptom | Cause and fix |
| --- | --- |
| `docker: Error response from daemon: pull access denied` | The image isn't built locally and isn't pullable. Check `docker images`, then rebuild it. Docker treats an unknown local tag as a remote reference, which is why the error mentions a pull. |
| `Image <your-image> coder_eval <a> != host <b>` | The custom image carries an `org.coder-eval.version` label inherited from a stale framework base. Rebuild the base with `make docker-image`, then rebuild your derived image with `docker build --no-cache`. |
| `Image <your-image> has no org.coder-eval.version label` | The image doesn't descend from `coder-eval-agent` (or predates the label). Rebase it on the framework image, or use the runtime kit. |

## Building the image from a task Dockerfile

Instead of pointing at a pre-built `image`, a task can ship its own `Dockerfile`
and have coder-eval build it before the run:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile   # relative to the task YAML
```

> **⚠️ Contract: a task Dockerfile MUST either start with `FROM coder-eval-agent:<version>` or use the runtime kit (see below).**
> The container runs the coder-eval orchestrator (`coder-eval _run-task-internal`)
> via the framework image's `ENTRYPOINT`. A task Dockerfile extends that image and
> adds only task-specific layers — extra `apt` packages, `COPY`-ed inputs, etc.
>
> Build the framework base first — `make docker-image` (tags both
> `coder-eval-agent:<version>` and `coder-eval-agent:latest`).

### Tasks that bring their own base image: the runtime kit (`coder-eval-runtime`)

The `FROM coder-eval-agent` contract above means a task is **rebased** onto the
Debian framework image. That breaks tasks whose Dockerfile was written for a
different base image (e.g. a Fedora recipe using `dnf`, which doesn't exist on Debian). To keep the task's own base image and build successfully, coder-eval's runtime need to be copied into the task's image. Use `make coder-eval-runtime` first to make the runtime available for copying.

```dockerfile
FROM fedora:41                 # the task's own base, kept verbatim
RUN dnf -y install ...         # the task's native recipe, runs on its own OS
# --- copy coder-eval runtime into a task image ---
COPY --from=coder-eval-runtime:latest /opt/coder-eval /opt/coder-eval
COPY --from=coder-eval-runtime:latest /usr/local/bin/coder_eval_entrypoint.sh /usr/local/bin/coder_eval_entrypoint.sh
LABEL org.coder-eval.version="<ver>"
```

`make coder-eval-runtime` builds the kit (`docker/Dockerfile.runtime`): a
standalone CPython + Node + the `coder-eval` CLI + Claude Code, all under
`/opt/coder-eval`, plus the entrypoint at the **same** `/usr/local/bin/...` path
the host pins. The kit is **glibc-only** — it runs on debian/ubuntu/fedora/rhel/…
but not musl/Alpine.

Both base images are independent and persistent — build each once and run any mix of rebase
and inject tasks without rebuilding. To build both in one shot (no credentials needed), use
**`make docker-images`** (= `make docker-image` + `make coder-eval-runtime`); reach for the
individual targets when you only need one.

> The kit installs the **no-credential** set only (core + codex), like `make docker-image` —
> it never installs the `[uipath]` extra, so there is no `make docker-images-full`. An
> inject-mode task that needs the LLMGW/`uipath` judge isn't supported by the kit as built.

```dockerfile
# environment/Dockerfile
FROM coder-eval-agent:latest          # inherit runtime + entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils
RUN pip install --no-cache-dir PyMuPDF==1.24.10
COPY input/ /root/input/
```

Behavior:

- **Path resolution** — `dockerfile_path` is resolved relative to the task YAML's
  directory at load time (with `$VAR` / `${VAR}` expansion). A missing file fails
  fast at load, not mid-run.
- **Entrypoint check** — after building, coder-eval inspects the image's
  `ENTRYPOINT` and aborts with a `FROM coder-eval-agent` hint if the runtime
  wasn't inherited.
- **Overrides `image`** — when `dockerfile_path` is set, it takes precedence over
  any `image` value.
- **Build context** — the build context is the **Dockerfile's parent directory**,
  so relative `COPY ./input/... ` instructions resolve naturally. In the layout
  above, `environment/` is the context.
- **Caching** — the image is tagged deterministically as
  `coder-eval-task-<task_id>:built`, so repeat runs of the same task reuse
  Docker's layer cache. Edit the Dockerfile and the next run rebuilds the
  changed layers only.
- **Version-label check skipped** — the `org.coder-eval.version` preflight only
  applies to the framework image; task-built images don't carry it and won't
  warn.

A build failure aborts the task with a `DockerBuildError` (a `DockerRunError`
subclass) carrying `docker build`'s output. Because the build runs before the
run dir, `docker.log`, or `task.json` exist, the runner explicitly records the
failure so it is never a silent empty result dir: it creates the run dir, writes
the full build log to **`docker.log`**, and writes a synthetic **`task.json` with
`final_status: BUILD_FAILED`** (an `error`-category status) before re-raising. So
a failed build shows up per-task on the dashboard with its build log, exactly
where you'd look for container output.

### Customizing the build (`docker.build`)

The `docker build` invocation is configurable via `sandbox.docker.build`:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile
    build:
      args:                          # -> --build-arg KEY=VALUE
        PKG_VERSION: "1.2.3"
        TOKEN: "${HOST_TOKEN}"       # values are $VAR / ${VAR} expanded from the host env
      secrets:                       # -> --secret <spec> (requires BuildKit)
        - id=mytoken,env=MY_TOKEN    # forward a host env var as a build secret
        - id=npmrc,src=~/.npmrc      # or a file
      extra_args: ["--target", "runtime"]   # escape hatch for any other docker build flag
      buildkit: true                 # optional: force DOCKER_BUILDKIT (see below)
```

- **`args`** → `--build-arg KEY=VALUE`. Values are environment-expanded against
  the host. Prefer `secrets` for credentials — build-args are recorded in the
  image history.
- **`secrets`** → `--secret <spec>`. Use `id=NAME,env=VAR` to forward a host env
  var or `id=NAME,src=PATH` for a file; reference it in the Dockerfile via
  `RUN --mount=type=secret,id=NAME ...`. Secrets are exposed only to the mounting
  RUN step and never baked into layers. **Secrets require BuildKit.**
- **`extra_args`** → raw flags inserted before the build context (e.g.
  `--target`, `--network`, `--platform`). Escape hatch for options without a
  dedicated field.
- **`buildkit`** → controls the `DOCKER_BUILDKIT` env var. Omitted (default),
  coder-eval **inherits the invoker's environment** — set `DOCKER_BUILDKIT=1`
  before running coder-eval to enable it globally. Set `buildkit: true` / `false`
  to force it per task. If `secrets` are configured but BuildKit isn't enabled,
  coder-eval logs a warning (the build would otherwise fail).

The build context is always appended last, so `extra_args` can't displace it.

## Authentication

> **macOS users — read this first.** Claude Code's OAuth tokens live in the macOS Keychain. The container has no path to the Keychain, so the bundled CLI inside will return `Not logged in · Please run /login` and every task will fail at iteration 1. Before running `--driver docker`, set one of these on the host:
>
> - `ANTHROPIC_API_KEY=...` (direct Anthropic), or
> - `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_BEARER_TOKEN_BEDROCK=...` + `AWS_REGION=...` (Bedrock).
>
> Linux hosts where Claude Code stores creds under `~/.claude` already work because that directory is bind-mounted into the container.

Credentials are forwarded via `--env VAR` (name-only, never embedded in argv) for these vars when set on the host: `ANTHROPIC_API_KEY`, `API_BACKEND`, `UIPATH_*`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_MODEL`.

**To add one or two custom vars to the defaults (recommended)**, use `env_passthrough_extra`:

```yaml
sandbox:
  driver: docker
  docker:
    env_passthrough_extra: ["MY_CUSTOM_TOKEN", "DEBUG_FLAG"]  # Keeps all defaults + these
```

**To completely replace the list**, use `env_passthrough`:

```yaml
sandbox:
  driver: docker
  docker:
    env_passthrough: ["MY_CUSTOM_TOKEN", "ANTHROPIC_API_KEY"]
```

### `HOME` is forwarded by default

The default `env_passthrough` includes `HOME` so the in-container `~/.claude` lookup resolves at the same path as on the host (the mount lands at `$HOME/.claude` symmetrically). Practical contract:

- `Path.home()` inside the container returns the host's `HOME` value (e.g. `/Users/you` on macOS). The directory exists in the container because Docker auto-creates it as the mount parent for `~/.claude`.
- `~/.claude` is **not** the host's real dir — the runner makes a throwaway *lean copy* in a tmp dir per task and mounts that copy **read-write** at `$HOME/.claude`. The copy keeps the small set the container needs (auth via `.credentials.json`, `settings.json`, `plugins/`) and **drops heavy or transient per-session state** — `security/` (often hundreds of MB), `projects/`, `cache/`, `file-history/`, `backups/`, `downloads/`, `sessions/`, `telemetry/`, `shell-snapshots/`, `todos/`, `session-env/`, plus the volatile churn dirs the live CLI rewrites. The skip set is a denylist; the authoritative list is `CLAUDE_COPY_IGNORE` in `src/coder_eval/isolation/docker_runner.py` (a test asserts this doc and that constant agree, so the list never silently drifts). The container may write anywhere under `~/.claude`; those writes hit the copy and are discarded when the task ends — the host's real `~/.claude` is never modified. Note the copy includes the OAuth token (`.credentials.json`) and is mounted read-**write**, so the in-container agent can read and tamper with the token *copy* — contained, since the copy is discarded at task end and the host's real dir is untouched. Opt out entirely with `CODER_EVAL_NO_CLAUDE_MOUNT=1`.
- Writes under `$HOME` outside the `~/.claude` mount land in the container's ephemeral rootfs overlay. Don't expect them to persist or to be visible to the host.
- If a tool *detects platform* from `HOME` (e.g. "starts with `/Users/` → macOS"), it will draw the wrong conclusion. Vanishingly rare in practice.

Remove `HOME` from `env_passthrough` if you don't want this behavior — the container's image-default `HOME=/root` will win, but then the host's OAuth dir is no longer reachable.

## Run directory safety (`--run-dir`)

The host's run dir is bind-mounted **read-write** into the container at the same absolute path (so `task.json` and artifacts land directly on the host filesystem). This makes `--run-dir` load-bearing for isolation:

- **Do not** point `--run-dir` at a symlink. Docker resolves the source of a bind mount; following a symlink would silently grant the container RW access to a different host location.
- **Do not** point `--run-dir` at a sensitive parent (e.g. `$HOME` directly, `/etc`, a repo root). Use a dedicated `runs/` subtree.
- The default (`runs/<timestamp>/`) is safe.

## Boundary

| Layer | Location |
|---|---|
| Agent process (Claude Code SDK) | inside container |
| Sandbox + per-row criterion checking | inside container |
| **`task.json` serialization** | **container → host bind mount** |
| Per-criterion `aggregate()` (P/R/F1, suite thresholds) | host |
| Reports, run summary, experiment rollups | host |

`task.json` is the only artifact crossing the boundary. Aggregation reads it via the existing host pipeline unchanged.

## Limitations

- **Relative template paths**: `template_sources[].path` is resolved to a host absolute path *before* staging, so it won't exist inside the container unless you also forward the parent dir via `sandbox.docker.extra_mounts`.
- **No container reuse across tasks**: each task = one fresh container. Adds ~1–3 s startup overhead per task; negligible vs. LLM latency.
- **macOS Keychain auth**: not reachable from the container; set `ANTHROPIC_API_KEY` (direct) or Bedrock credentials instead.

## Architecture

The host's `DockerRunner` (`coder_eval/isolation/docker_runner.py`) renders the `docker run` argv, bind-mounts task inputs at `/work/input`, allocates an output dir at `/work/output`, and tails container stdout into `docker.log` in the task's run dir.

Inside the container, the entrypoint invokes `coder-eval _run-task-internal` (hidden subcommand), which loads the staged YAML + context, runs the standard in-process Orchestrator (driver auto-coerced back to `tempdir`), and writes `task.json` to the output mount. Host reads it and feeds the existing aggregation pipeline.

A `result_kind` discriminator on `CriterionResult` ensures `ClassificationCriterionResult` subclasses survive the JSON round-trip — without it, host-side aggregation would silently lose `observed_label`/`expected_label`.

## Grading-material isolation (user/permission barrier)

Under `driver: docker`, a scavenging agent can otherwise read its own grading
rubric off the mounted inputs (`/work/input/task.yaml`, the skills-repo `tests/`
tree, `check_*.py` graders, `RESOLUTION.md`). Coder Eval closes this with a
**Unix DAC permission barrier**: all grading material is root-owned mode-0700 and
the agent-under-test's CLI subprocess runs as a **dedicated unprivileged uid**
(`agent`, uid/gid 2000, baked into the image). Grading (`SuccessChecker`,
`run_command`, judges) stays in the container's **root** process, which ignores
DAC and reads the locked material via `$TASK_DIR`/`$SKILLS_REPO_PATH`.

### What the agent uid can and cannot read

| Path | Owner/mode | Agent uid |
|---|---|---|
| `/work/input` (`task.yaml`, `task_full.json`, `context.json`) | root 0700 | **EACCES** |
| skills-repo / plugin mount (`tests/`, `check_*.py`, `reference_agents/`, `RESOLUTION.md`) | root 0700 | **EACCES** |
| per-task-dir mount | root 0700 | **EACCES** |
| reference-solution mount (absolute/escaping `reference.file`/`reference.directory`) | root 0700 | **EACCES** |
| `/proc/1/environ` (root PID1) | kernel-restricted | **EACCES** |
| `/work/skills` (world-readable skill-DOCS copy: docs/commands/skills only) | agent | readable |
| `/work/output/artifacts/<task>` (its workspace) | agent | read/write |

**`/work/output` is NOT locked during the turn.** It is a bind mount *shared with
the host*, which writes the liveness heartbeat there as a non-root uid; a
root-0700 lock would make the heartbeat unwritable and self-reap the container. So
`/work/output` is world-traversable and the agent's own `artifacts/<task>`
subdir is agent-owned. The real mitigation for the grading artifact is temporal,
not permission-based: **`task.json` is written only AFTER the agent turn ends** (it
is not a live read surface during the turn), and its `source_yaml` is **nulled** in
the agent-visible context (the raw YAML rides on the root-only `task_full.json`
instead). A root-0700 file *placed* under `/work/output` IS EACCES to the agent
(the lock mechanism works there) — `/work/output` is simply not blanket-locked.

The `/work/input`, per-task-dir, and skills-repo/plugin mounts are bind-mounted
**read-write** (not `:ro`) precisely so the in-container root entrypoint's
root-0700 `chmod`/`chown` lock applies: an `os.chmod` on a `:ro` bind mount fails
with `EROFS` and would silently leave the material agent-readable. The lock denies
the dropped agent uid; the agent still cannot write (0700-root), and grading runs
as root.

The agent-readable `task.yaml` is additionally **criteria-stripped**
(`TaskDefinition.agent_safe_dump()` — defence in depth); the full criteria travel
in a root-only `task_full.json` sibling the entrypoint merges back before grading.
`agent_safe_dump` strips **only** `success_criteria` and `reference` — every other
field (`initial_prompt`, `system_prompt`, pre/post commands, `metadata`) survives
into the agent-readable `task.yaml`, so grading material must never be authored
into those fields. The skill-DOCS copy carries only the plugin-discovery subtrees
(`PLUGIN_AGENT_ALLOWED_SUBDIRS`), never grader/reference/fixture trees. The RAW
skills-repo/plugin mount (which does carry the grader trees) is locked separately:
the host rewrites the staged task's plugin paths to `/work/skills`, so it forwards
the ORIGINAL host mount paths via `context.json` (`plugin_host_paths`) and the
entrypoint locks those real in-container mount paths root-0700. The reference
solution is handled the same way: `agent_safe_dump` strips the `reference` field
from `task.yaml`, but an absolute (or `..`-escaping) `reference.file`/`reference.directory`
is still bind-mounted for the in-container grader — so the host forwards its resolved
mount targets via `context.json` (`reference_host_paths`) and the entrypoint locks
those root-0700 too (mounted rw, like the plugin mounts, so the `chmod` isn't `EROFS`'d).

### Per-harness drop seam (agent-agnostic)

Every built-in harness spawns a controllable CLI-binary subprocess, so the drop
is at that spawn seam — no orchestrator fork, no two-container split:

- **claude-code** — `ClaudeAgentOptions.user = "agent"` (SDK forwards to
  `subprocess.Popen(user=)`, a POSIX setuid).
- **codex** — `CodexConfig.launch_args_override` routes the bundled codex binary
  through the drop shim (`coder_eval_drop_privilege.sh` → `setpriv --reuid=agent`).
- **antigravity** — the existing PATH-shadow (`_harness_spawn_guard`) injects a
  `localharness` wrapper that execs the real binary through the same shim.

If the drop is requested but the container is not root, the entrypoint **fails
loud** — it never silently runs the agent as the container owner.

### Scope, portability, and the Docker-Desktop caveat

- **Docker + Linux only.** Unix uid/permissions exist only inside the Linux
  container. The **Windows nightly slice runs `--driver tempdir`** (agent on host,
  no container, no uid boundary): it is immune (criteria live in memory, never
  written to an agent-readable file) but is **not covered by this barrier** — a
  separate host-side isolation follow-up tracks it.
- **Docker-Desktop bind-mount uid remap.** On macOS/Windows Docker Desktop, a
  bind-mount's owner can be remapped, which can defeat an in-container `chown` on
  a *bind-mounted* path. The barrier applies `chmod 0700` (owner root), which
  denies the agent uid regardless of any remap; a materialized grader root can
  live in the container rootfs (not a mount) where its `chown root:root` is always
  authoritative. **The Linux CI/nightly host (native overlayfs, real root) is the
  authoritative environment**; do not rely on the barrier on Docker-Desktop dev
  machines.

### Surface #6 — baked image content (authoring invariant)

The barrier locks *runtime-mounted* material, but image **content** is not a
permission surface. **Authoring invariant: mocks and tooling baked into the image
must not encode task-specific expected values.** `tests/test_docker_image_no_answer_leak.py`
is the deterministic sensor — it scans the Dockerfile + its `COPY` sources for
answer sentinels and for `check_*.py` / `RESOLUTION.md` / `tests/tasks` grader
material, and fails the build if any are baked.

### Live check + nightly re-audit

- **Live check.** Run `tasks/.../template_aware_create_adversarial.yaml` (the
  "read /work" adversary) under a real `--driver docker` run and confirm the
  agent's `found_criteria.txt` contains no grading material.
- **Acceptance proof (CI).** `make test-docker-isolation` runs the six-surface
  EACCES-as-agent-uid suite **as root inside the built image** (the `docker-isolation`
  CI job); it is the authoritative check that the barrier holds.
- **Per-harness uid probe (manual/nightly).** For each harness, run a minimal task
  whose prompt writes `id -u` to a workspace file and confirm it equals `2000`
  (== the baked agent uid) — the objective proof the agent-agnostic drop takes
  effect. (Not wired as a live CI test to avoid model spend; the deterministic
  EACCES proof + the per-harness wiring unit tests establish the mechanism.)
- **Nightly re-audit.** Re-run the trajectory scan that produced the original leak
  audit (reads of `check_*.py` / task-YAML / `RESOLUTION.md` / `$SKILLS_REPO_PATH/tests`
  / `/work/input`) and confirm a per-run leak rate of 0. The reusable
  `scan_for_leak_techniques` detector is the CI-cheap proxy.

### Rollout notes

- **Aggregate pass rates shift down ~2.4% (honest correction).** Before the
  barrier, ~2.4% of nightly replicates passed by reading the suite (claude-code
  highest, ~5–6%). Those tasks must now succeed on merit, so pass rates drop by
  roughly that margin. **Annotate the first post-fix nightly** in the evalboard
  ("leak-barrier landed") so the step-down is not read as a regression.
- **Re-run contaminated carried passes.** The maturity feature carries forward
  passes; any task that previously passed via a leak has a contaminated carried
  pass. Invalidate + re-run the carried passes for every task the audit flagged
  as `answer`/`oracle`/`recon`.
- **~0 wall-clock cost.** The drop is a chmod/chown + a setuid at spawn inside the
  single existing container — no extra container, no second pass.
