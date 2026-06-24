# Docker Isolation

Run each evaluation task inside its own fresh container. Strong host isolation and a pinned, reproducible agent runtime.

> Supersedes the agent-side FS perimeter flag from #199 (reverted in 9fe4320). The container boundary subsumes what that flag tried to do at the agent level.

## When to use

Set `sandbox.driver: docker` on a task (or pass `--driver docker` on the CLI —
a thin alias for `-D sandbox.driver=docker`; see the
[generic `-D` overrides spec](features/2026-06-01-generic-d-overrides.md)) when you want:

- **Isolation from the host filesystem/network** — agent-generated code can't reach files outside the sandbox.
- **A pinned toolchain** — the image bakes in Python 3.13, Node 22 LTS, `@anthropic-ai/claude-code`, `uv`, and the matching `coder_eval` version, so results don't drift with host upgrades.

Aggregation (P/R/F1, suite thresholds, reports) always stays on the host. Each container is a sealed "run one task → emit one `task.json`" worker.

## One-time setup

```bash
make docker-image        # agnostic core (default; no extras, no credentials)
# opt in to the UiPath + Codex extras (needs private-index credentials):
make docker-image-full
```

Both build `coder-eval-agent:<pkg-version>` and tag it `:latest`.

- **`make docker-image`** installs the agnostic core package only (no extras), mirroring `pip install coder-eval`. It needs **no credentials** and is enough for the common case — claude-code tasks scored with `run_command` / `file_contains` (incl. converted skillsbench tasks). It omits the LLM-Gateway judge (`llm_judge` via UiPath LLMGW) and the Codex agent.
- **`make docker-image-full`** adds the `uipath` + `codex` extras. The `uipath` extra pulls `uipath-llmgw-client` from a private index, so it needs `UV_INDEX_UIPATH_USERNAME` / `..._PASSWORD` in the environment (auto-sourced from `.env`); without them the build fails with a `401 Unauthorized` on the UiPath feed. Use this only for tasks that need the LLMGW judge or the Codex agent.

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

## Building the image from a task Dockerfile

Instead of pointing at a pre-built `image`, a task can ship its own `Dockerfile`
and have coder-eval build it before the run:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile   # relative to the task YAML
```

> **⚠️ Contract: a task Dockerfile MUST start `FROM coder-eval-agent:<version>`.**
> The container runs the coder-eval orchestrator (`coder-eval _run-task-internal`)
> via the framework image's `ENTRYPOINT`. A task Dockerfile extends that image and
> adds only task-specific layers — extra `apt` packages, `COPY`-ed inputs, etc. A
> bare base like `FROM ubuntu:24.04` has **no entrypoint**, so `docker run` treats
> the run flags as the command and fails with
> `exec: "--output": executable file not found in $PATH`. coder-eval now detects
> this after the build and aborts with an actionable error instead.
>
> Build the framework base first — `make docker-image` (tags both
> `coder-eval-agent:<version>` and `coder-eval-agent:latest`).

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

A build failure aborts the task with a `DockerRunError` carrying `docker build`'s
stderr.

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
> - `API_BACKEND=proxy` + `LLMGW_*` credentials (UiPath LLM Gateway), or
> - `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_BEARER_TOKEN_BEDROCK=...` + `AWS_REGION=...` (Bedrock).
>
> Linux hosts where Claude Code stores creds under `~/.claude` already work because that directory is bind-mounted into the container.

Credentials are forwarded via `--env VAR` (name-only, never embedded in argv) for these vars when set on the host: `ANTHROPIC_API_KEY`, `API_BACKEND`, `LLMGW_*`, `UIPATH_*`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_MODEL`.

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

- `Path.home()` inside the container returns the host's `HOME` value (e.g. `/Users/akshaya` on macOS). The directory exists in the container because Docker auto-creates it as the mount parent for `~/.claude`.
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
| LLM Gateway proxy (when `API_BACKEND=proxy`) | inside container |
| **`task.json` serialization** | **container → host bind mount** |
| Per-criterion `aggregate()` (P/R/F1, suite thresholds) | host |
| Reports, run summary, experiment rollups | host |

`task.json` is the only artifact crossing the boundary. Aggregation reads it via the existing host pipeline unchanged.

## Limitations

- **Relative template paths**: `template_sources[].path` is resolved to a host absolute path *before* staging, so it won't exist inside the container unless you also forward the parent dir via `sandbox.docker.extra_mounts`.
- **No container reuse across tasks**: each task = one fresh container. Adds ~1–3 s startup overhead per task; negligible vs. LLM latency.
- **macOS Keychain auth**: not reachable from the container; use `API_BACKEND=proxy` instead.

## Architecture

The host's `DockerRunner` (`coder_eval/isolation/docker_runner.py`) renders the `docker run` argv, bind-mounts task inputs at `/work/input`, allocates an output dir at `/work/output`, and tails container stdout into `docker.log` in the task's run dir.

Inside the container, the entrypoint invokes `coder-eval _run-task-internal` (hidden subcommand), which loads the staged YAML + context, runs the standard in-process Orchestrator (driver auto-coerced back to `tempdir`), and writes `task.json` to the output mount. Host reads it and feeds the existing aggregation pipeline.

A `result_kind` discriminator on `CriterionResult` ensures `ClassificationCriterionResult` subclasses survive the JSON round-trip — without it, host-side aggregation would silently lose `observed_label`/`expected_label`.
