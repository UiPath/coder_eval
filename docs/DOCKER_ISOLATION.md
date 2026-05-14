# Docker Isolation

Run each evaluation task inside its own fresh container. Strong host isolation and a pinned, reproducible agent runtime.

> Supersedes the agent-side FS perimeter flag from #199 (reverted in 9fe4320). The container boundary subsumes what that flag tried to do at the agent level.

## When to use

Set `sandbox.driver: docker` on a task (or pass `--driver docker` on the CLI) when you want:

- **Isolation from the host filesystem/network** — agent-generated code can't reach files outside the sandbox.
- **A pinned toolchain** — the image bakes in Python 3.13, Node 22 LTS, `@anthropic-ai/claude-code`, `uv`, and the matching `coder_eval` version, so results don't drift with host upgrades.

Aggregation (P/R/F1, suite thresholds, reports) always stays on the host. Each container is a sealed "run one task → emit one `task.json`" worker.

## One-time setup

```bash
make docker-image
```

Builds `coder-eval-agent:<pkg-version>` and tags it `:latest`. Requires `UV_INDEX_UIPATH_USERNAME` / `..._PASSWORD` in the environment (auto-sourced from `.env`).

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

## Authentication

> **macOS users — read this first.** Claude Code's OAuth tokens live in the macOS Keychain. The container has no path to the Keychain, so the bundled CLI inside will return `Not logged in · Please run /login` and every task will fail at iteration 1. Before running `--driver docker`, set one of these on the host:
>
> - `ANTHROPIC_API_KEY=...` (direct Anthropic), or
> - `API_BACKEND=proxy` + `LLMGW_*` credentials (UiPath LLM Gateway), or
> - `CLAUDE_CODE_USE_BEDROCK=1` + `AWS_BEARER_TOKEN_BEDROCK=...` + `AWS_REGION=...` (Bedrock).
>
> Linux hosts where Claude Code stores creds under `~/.claude` already work because that directory is bind-mounted into the container.

Credentials are forwarded via `--env VAR` (name-only, never embedded in argv) for these vars when set on the host: `ANTHROPIC_API_KEY`, `API_BACKEND`, `LLMGW_*`, `UIPATH_*`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_MODEL`. Override the list per task:

```yaml
sandbox:
  driver: docker
  docker:
    env_passthrough: ["MY_CUSTOM_TOKEN", "ANTHROPIC_API_KEY"]
```

### `HOME` is forwarded by default

The default `env_passthrough` includes `HOME` so the in-container `~/.claude` lookup resolves at the same path as on the host (the bind mount lands at `$HOME/.claude` symmetrically). Practical contract:

- `Path.home()` inside the container returns the host's `HOME` value (e.g. `/Users/akshaya` on macOS). The directory exists in the container because Docker auto-creates it as the mount parent for `~/.claude`.
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
