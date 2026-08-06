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

## Isolation model: COPY/PRUNE + GRADE-OUTSIDE

Under `--driver docker` the agent container receives **only read-only or copied inputs and its own throwaway workspace** — it never gets rw access to a host original, and never read access to grading material. Grading runs **outside the agent's reach**, on the host, after the container exits. The guiding rule is: **never chmod a host bind mount; give the agent only copies or read-only inputs.**

**Exit criterion:** a run leaves every host file **byte-for-byte AND metadata-identical** (contents, uid/gid, mode, mtime, symlink targets). Detector A below is the sensor for it.

Three coordinated moves close the criteria/grader leak by **absence**, not by permission:

1. **COPY/PRUNE the agent's inputs.** For each plugin, the host stages a *sanitized bundle copy* (`project_plugin_for_agent` — `skills`/`commands`/`agents`/`hooks`/`.claude-plugin` only, from the `PLUGIN_AGENT_ALLOWED_SUBDIRS` allowlist) and mounts that copy **read-only** at `/work/skills` (`CONTAINER_SKILL_DOCS_DIR`). The raw `$SKILLS_REPO_PATH` checkout, the reference, and the host task dir are **not mounted into the agent container at all**. The staged `task.yaml` is criteria-stripped via `agent_safe_dump()` (`success_criteria: []`, `reference: null`) and `context.json`'s `source_yaml` is nulled — so no grading material is in the agent's mount namespace.
2. **GRADE OUTSIDE the agent's reach (host).** The container runs the **agent only**; its artifacts cross the boundary via the `/work/output` bind mount. After the container exits, the host grades the copied-out artifacts through the orchestrator's evaluate-only re-grade path (`regrade_on_host`), using the full, unstripped `TaskDefinition` it still holds — with `TASK_DIR` pointing at the **real host task dir**, so `run_command`/`file_check` graders resolve `$TASK_DIR/check_*.py` against the host grader, never agent-written content. Only runs whose final status is `SUCCESS`/`FAILURE`/`MAX_TURNS_EXHAUSTED` are re-graded (an explicit allowlist); a terminal agent-side failure (`ERROR`/`TIMEOUT`/`BUILD_FAILED`/budget) stands untouched.
3. **`~/.uipath` copy-then-mount.** Like `~/.claude`, `~/.uipath` is forwarded as a throwaway rw **copy**, never the host original — so an agent can never overwrite the host credential.

### Detector A — host-unchanged-after-run

`tests/test_docker_host_unchanged.py`. Two variants: a **daemon-less proxy** (always runs in CI) that asserts no `-v` mount source is a host original mounted rw — only staging copies, `/work/input` (`:ro`), and `/work/output` — proving there is no rw host mount to mutate; and a **daemon-gated real-run** (`-m live`) that snapshots content hash + `os.lstat` metadata (mode, uid, gid, mtime) + symlink targets (including the mount root itself) of the host skills / task dir / reference before and after a real run and asserts they are **byte-for-byte AND metadata-identical**. The real-run check is **Linux-authoritative** (native overlayfs); macOS/Windows Docker Desktop's uid-remap masks host mutation.

### Detector B — zero-grading-material-in-agent-mount

`tests/test_docker_criteria_isolation.py`. Stages a task carrying real criteria + a plugin bundling grader/reference material, scans the **entire agent mount view** (`/work/input` + the sanitized skills copy) and asserts zero grading-material hits (criteria values, `check_*.py`, `RESOLUTION.md`, `reference_agents/`, reference values). A positive control asserts the **host** still holds the full criteria, so a vacuous "staged nothing" bug cannot pass.

## Residual leaks

Allowlist-by-absence has **no DAC backstop** (there is no permission barrier — the agent simply never receives the material), so the boundary correctness is load-bearing:

1. **Prune-boundary miss.** A plugin that puts answers *inside* an allowed dir (e.g. `skills/answers.md`) defeats the prune — `PLUGIN_AGENT_ALLOWED_SUBDIRS` is a coder_eval-side guess about what is answer-free. Durable fix (cross-repo follow-up): push the agent-bundle boundary into the skills repo (a manifest declaring the agent-safe surface).
2. **Reference/golden material inside the bundle.** A plugin bundling a reference solution under an allowed subtree ships to the agent. Detector B catches known sentinels, not an unknown golden file — reinforces risk 1.
3. **Un-stripped `task.yaml` fields / author-pointed mounts.** `agent_safe_dump` strips only `success_criteria`/`reference`. A task author who hides expected values in `initial_prompt`/`system_prompt`/pre-post commands/`metadata` leaks them to the agent (semantic, not mechanically enforceable — see the `agent_safe_dump` docstring). The remaining agent-container mounts are `template_sources[]` dirs, a stray absolute `system_prompt_file` (normally inlined+nulled at load), and any `sandbox.docker.extra_mounts` entries, all mounted `:ro`. All three now go through the **grader-dir overlap guard**: a mount whose source equals, contains, or is contained by the host task dir (`rt.task_file.parent` — holds `check_*.py` / reference / unstripped criteria) is a hard error, so the task dir can no longer be re-exposed that way. The residual risk is a mount pointed at *another* answer-bearing location outside the task tree — the guard can't know about it, so keep template/system-prompt/extra-mount paths off any grading material.
4. **Grade-outside boundary bleed.** If the host re-grade read agent-written content as if it were the reference, grading integrity would be compromised. Mitigation: the re-grade `Sandbox.task_dir` is the real host task dir (`rt.task_file.parent`), never the agent workspace (Detector-adjacent test in `tests/test_docker_regrade.py`).
5. **Baked image content.** Mocks/tooling baked into `docker/Dockerfile` must not encode task-specific expected values — authoring invariant + the baked-image scan (`tests/test_docker_image_no_answer_leak.py`).
6. **Env signposts.** `TASK_DIR`/`SKILLS_REPO_PATH` live on the grader (host) env only — never in the agent container's env (which has no task-dir/skills-repo mount to point at anyway).

## Boundary

| Layer | Location |
|---|---|
| Agent process (Claude Code SDK) | inside container |
| Sandbox setup + agent turn | inside container |
| **`task.json` (agent trajectory) serialization** | **container → host bind mount** |
| **Criterion checking / grading (GRADE-OUTSIDE)** | **host, after the container exits** |
| Per-criterion `aggregate()` (P/R/F1, suite thresholds) | host |
| Reports, run summary, experiment rollups | host |

`task.json` is the only artifact crossing the boundary (agent trajectory + artifacts). The host re-grade merges the real grades onto it and re-persists it, so the on-disk record carries both the trajectory and the authoritative grade.

## Limitations

- **Relative template paths**: `template_sources[].path` is resolved to a host absolute path *before* staging, so it won't exist inside the container unless you also forward the parent dir via `sandbox.docker.extra_mounts`.
- **No container reuse across tasks**: each task = one fresh container. Adds ~1–3 s startup overhead per task; negligible vs. LLM latency.
- **macOS Keychain auth**: not reachable from the container; set `ANTHROPIC_API_KEY` (direct) or Bedrock credentials instead.

### Early stop (`stop_early`) is not supported under `--driver docker`

Criterion-level early stop (a `stop_early:` block, driven by the `EarlyStopWatcher`)
relies on **live criterion verdicts computed during the agent turn**. Under COPY/PRUNE
+ GRADE-OUTSIDE the container runs the agent with the criteria **stripped**, and grading
happens on the host **after** the container exits — so the in-container watcher can never
arm. A `stop_early:` block is therefore a **no-op under docker**: the run does not stop
early. `DockerRunner` logs a loud warning when a task arms early stop under docker, so it
is a documented, signposted limitation rather than a silent one.

**Verdict is unaffected.** The host re-grade still grades the full criteria, and a run
that completes naturally gates strict-AND — the same authoritative outcome, just without
the early cutoff (a cost/time optimization) and its telemetry.

The leak-free way to make early stop work under docker is a **host-side watcher**: the
host already receives the container's per-tool-call event stream and already signals the
container via the heartbeat channel, and the agent already supports cooperative stop — so
the watcher can run on the host (where the full criteria live, never entering the
container), compute verdicts against the real criteria, and cooperatively signal the
container to stop. That is the intended follow-up; until then, run `stop_early` suites
with `--driver tempdir`.

## Architecture

The host's `DockerRunner` (`coder_eval/isolation/docker_runner.py`) renders the `docker run` argv, bind-mounts task inputs at `/work/input`, allocates an output dir at `/work/output`, and tails container stdout into `docker.log` in the task's run dir.

Inside the container, the entrypoint invokes `coder-eval _run-task-internal` (hidden subcommand), which loads the *criteria-stripped* staged YAML + context, runs the standard in-process Orchestrator (driver auto-coerced back to `tempdir`) to execute the **agent turn only**, and writes `task.json` to the output mount. The host then re-grades the copied-out artifacts (`regrade_on_host`) against the full criteria it holds, merges the authoritative grades onto the trajectory, and feeds the existing aggregation pipeline. The container never receives the criteria, reference, or graders (see [Isolation model](#isolation-model-copyprune--grade-outside)).

A `result_kind` discriminator on `CriterionResult` ensures `ClassificationCriterionResult` subclasses survive the JSON round-trip — without it, host-side aggregation would silently lose `observed_label`/`expected_label`.
