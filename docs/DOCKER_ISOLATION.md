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

- **`make docker-image`** installs the core package plus the built-in agents. It needs **no credentials** and carries the `uid-gid-v1` isolation capability used by secure Docker runs. Static file/transcript criteria and `llm_judge` work in protected mode. Privileged dynamic criteria (`run_command`, `uipath_eval`, and `agent_judge`) currently fail closed; see [compatibility limits](#limitations).
- **`make docker-image-full`** additionally installs the `uipath` extra. The `uipath` SDK resolves from **public PyPI** (per `uv.lock`), so the build needs **no credentials**. Use this only for tasks that shell out to the in-host `uipath` CLI. (Codex is already in the default image — no extra needed.)

> **Codex sandbox under Docker.** Codex's Landlock-backed `read-only` / `workspace-write` sandboxes can't initialize inside the eval container. The runner therefore uses Codex `full-access` inside the agent's own security domain. The boundary is the dedicated Linux agent UID, cleared capabilities, `no_new_privs`, and the protected harness paths—not Landlock and not a root agent process.

## Agent/grader identity boundary

`sandbox.docker.agent_isolation` defaults to `true`. The container harness and grader remain root, while every evaluated Claude, Codex, or Antigravity subprocess runs as `agent:agent` (`2000:2000`).

The agent launcher clears inheritable, ambient, and bounding capabilities and sets `no_new_privs`. Generated work is placed in `/work/agent`. Hidden task data, results, raw task/plugin/reference/template sources, and grader inputs live below root-only `/opt/coder-eval/grader`. Raw source bind mounts remain read-only and are never chmod/chowned; only disposable staging copies and the generated workspace are changed.

Older/custom images must declare `org.coder-eval.agent-isolation=uid-gid-v1`. A protected run rejects an image without that label before making an LLM call. Images derived with `FROM coder-eval-agent:<current-version>` inherit it. Runtime-kit injection into an unrelated base does not yet provide the required Linux users and `setpriv` launchers, so it is not compatible with protected mode.

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

> **Protected-mode compatibility:** the current runtime kit does not install the
> dedicated identities, `setpriv` launchers, protected directory layout, or the
> `org.coder-eval.agent-isolation=uid-gid-v1` capability label. Because
> `agent_isolation` defaults to `true`, an inject-mode image fails closed at
> preflight. For now, extend `coder-eval-agent:<version>` for protected runs.
> Setting `agent_isolation: false` permits legacy runtime-kit migration but does
> not provide the boundary described on this page.

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
COPY input/ /work/agent/input/
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

### Agent HOME and Claude state

In protected mode, the host `HOME` value is not forwarded to the evaluated subprocess. The agent uses `/home/agent`:

- `~/.claude` is **not** the host's real directory. The runner makes a throwaway *lean copy* and mounts it read-write at `/home/agent/.claude`. The copy keeps the small authentication/settings/plugin set and **drops heavy or transient per-session state** — `security/`, `projects/`, `cache/`, `file-history/`, `backups/`, `downloads/`, `sessions/`, `telemetry/`, `shell-snapshots/`, `todos/`, `session-env/`, plus volatile CLI churn directories. The authoritative skip set is `CLAUDE_COPY_IGNORE` in `docker_runner.py`. Writes affect only the disposable copy. Opt out with `CODER_EVAL_NO_CLAUDE_MOUNT=1`.
- Other writes below `/home/agent` or `/work/agent` are ephemeral until the harness captures the workspace into the protected output mount.
- Harness-only variables such as `SKILLS_REPO_PATH`, `TASK_DIR`, and `CODER_EVAL_*` are removed from agent SDK environments. Required model API credentials remain available.

When `agent_isolation: false` is explicitly selected for migration, the legacy host-HOME behavior may still apply. That mode is not a security boundary.

## Run directory safety (`--run-dir`)

The host's run dir is bind-mounted read-write at `/opt/coder-eval/grader/output`, below a root-only parent. The agent cannot traverse it; the harness captures `/work/agent` there after the agent lifecycle. This still makes `--run-dir` load-bearing for host safety:

- **Do not** point `--run-dir` at a symlink. Docker resolves the source of a bind mount; following a symlink would silently grant the container RW access to a different host location.
- **Do not** point `--run-dir` at a sensitive parent (e.g. `$HOME` directly, `/etc`, a repo root). Use a dedicated `runs/` subtree.
- The default (`runs/<timestamp>/`) is safe.

## Boundary

| Layer | Location |
|---|---|
| Agent process and descendants | container, UID/GID `2000:2000`, `/work/agent` |
| Harness + supported criterion checking | container, root, `/opt/coder-eval/grader` |
| **`task.json` serialization** | **container → host bind mount** |
| Per-criterion `aggregate()` (P/R/F1, suite thresholds) | host |
| Reports, run summary, experiment rollups | host |

`task.json`, logs, and captured workspace artifacts cross through the protected output bind mount. Aggregation reads `task.json` through the existing host pipeline unchanged.

## Limitations

- **Dynamic privileged graders**: `run_command`, `uipath_eval`, and `agent_judge` are rejected in protected mode until a separate minimal-input grader sandbox exists. This prevents candidate-controlled code from turning a privileged grader into a confused deputy. Migrate to static built-in criteria or explicitly disable isolation only for a trusted transitional run.
- **Custom work directories and extra mounts**: protected mode currently rejects `docker.working_dir` and `docker.extra_mounts` because their agent/private audience is ambiguous. Use the generated `/work/agent` workspace and `template_sources`.
- **Runtime-kit injection**: not yet compatible with protected mode. Extend the current framework image instead.
- **No container reuse across tasks**: each task = one fresh container. Adds ~1–3 s startup overhead per task; negligible vs. LLM latency.
- **macOS Keychain auth**: not reachable from the container; set `ANTHROPIC_API_KEY` (direct) or Bedrock credentials instead.

## Architecture

The host's `DockerRunner` rewrites host paths to protected container paths, renders `docker run`, and tails container stdout into `docker.log`. Inputs land at `/opt/coder-eval/grader/input`, output at `/opt/coder-eval/grader/output`, the raw task directory at `/opt/coder-eval/grader/task_dir`, and the agent workspace at `/work/agent`.

Inside the container, the root entrypoint verifies the protected parent. The standard Orchestrator prepares the workspace as root, grants only that generated tree to UID 2000, and launches the selected agent through the shared privilege-drop policy. The host reads the final result from the protected output mount and feeds the existing aggregation pipeline.

Protected runs use Docker's init reaper and default to a 512-process limit when `limits.max_pids` is not specified. An explicit `max_pids` value takes precedence. Before trusted post-run/finalization begins, the harness stops the SDK, repeatedly kills every remaining UID-2000 process, and fails closed if that UID cannot be emptied.

A `result_kind` discriminator on `CriterionResult` ensures `ClassificationCriterionResult` subclasses survive the JSON round-trip — without it, host-side aggregation would silently lose `observed_label`/`expected_label`.
