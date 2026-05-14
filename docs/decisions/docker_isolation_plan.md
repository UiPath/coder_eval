# Docker Isolation Plan

## Goal

Run each evaluation task inside its own Docker container — strong host isolation **and** a pinned, reproducible agent + tool runtime. Aggregation (P/R/F1, suite thresholds, reports) stays on the host.

## Boundary

The container is a sealed, stateless **"run one task → emit one `task.json`"** worker.

| Layer | Where | Why |
|---|---|---|
| Agent process (Claude Code SDK) | Inside container | Isolation only holds if the agent itself is contained |
| Sandbox filesystem | Inside container | Agent file writes never touch the host |
| Per-row criterion checking (incl. `pytest`, `run_command`, `pylint_score`, `classification_match`, `llm_judge`) | Inside container | Criteria that exec code need the same env; emit `List[CriterionResult]` |
| `EvaluationResult` JSON (`task.json`) | **Serialization boundary** | Already exists today at `orchestrator.py:506` |
| Per-criterion `aggregate()` + classification overlay (P/R/F1, confusion) | Host | Pure function of `List[CriterionResult]`; nothing sandbox-specific |
| `suite_thresholds` gate, suite rollups, reports, experiment rollups | Host | Same |

The container's contract is exactly: read inputs (task YAML, agent config, env) → write one `task.json` to a host-mounted volume → exit. No cross-task communication, no shared state.

## Prereq Fix (Phase 0)

`ClassificationCriterionResult` currently round-trips as base `CriterionResult` because `CriterionResult` has no discriminator. The host-side aggregator reads `task.json` via `EvaluationResult.model_validate_json` (`reports.py:534`) and silently loses `observed_label` / `expected_label`. **This is already a latent bug**; Docker makes the JSON path load-bearing for every task, so it must be fixed first.

**Fix:** add a `result_kind: Literal["base", "classification"]` discriminator on `CriterionResult` with `default="base"`, override to `"classification"` on the subclass, and use `Annotated[..., Field(discriminator="result_kind")]` for the union in `EvaluationResult.criterion_results`. Add a regression test that round-trips a mixed list through JSON.

## Phases

### Phase 0 — Discriminator fix
- Add `result_kind` discriminator on `CriterionResult` / `ClassificationCriterionResult`.
- Switch `EvaluationResult.criterion_results` to a discriminated union list.
- Test: build a list with both subclasses, dump JSON, validate back, assert types preserved.
- `make verify` green.

### Phase 1 — Boundary at the orchestrator entry point (not inside Sandbox)
Cleaner than abstracting `SandboxBackend`: keep `Sandbox`/`Orchestrator`/criteria untouched. The container internally runs the same in-process flow; the host just routes one task to "spawn a container" instead of "call Orchestrator.run()".

- Add `SandboxConfig.isolation: Literal["process", "docker"] = "process"` and a `DockerIsolationConfig` model (image, network, cpu/mem caps, extra mounts).
- New module `coder_eval/isolation/docker_runner.py` with `DockerRunner.run_task(task, config, output_dir) -> EvaluationResult`. Internals: `docker run --rm` with bind mounts → container executes `coder-eval _run-task-internal …` → host reads `task.json` from output mount.
- New internal CLI subcommand `coder-eval _run-task-internal --task <yaml> --output <dir>`. Inside the container, it does what `run_command` does today for one task: build Orchestrator + Sandbox + Agent, run, write `task.json`. Marked internal (leading underscore), hidden from help.
- `orchestration/batch.py` dispatch: when `isolation == "docker"`, route to `DockerRunner`; otherwise current path unchanged.

### Phase 2 — Docker image
- `docker/Dockerfile`: pinned Python 3.13 + Node LTS + `@anthropic-ai/claude-code` + `uv` + `git`. `coder_eval` installed from the build context wheel (host + container versions guaranteed identical).
- `docker/entrypoint.sh`: thin wrapper that execs `coder-eval _run-task-internal "$@"`.
- `make docker-image` target: builds and tags `coder-eval-agent:<pkg-version>`.

### Phase 3 — DockerRunner wiring
- Bind mounts: task YAML (ro), task dir for `TASK_DIR` env (ro), templates (ro), `/work/output` (rw).
- Anthropic credentials passed via env (`ANTHROPIC_API_KEY` etc.) — never baked into the image.
- Network `bridge` by default (LLM calls + package installs require it); opt-out per task via `docker.network: none`.
- Resource caps from `ResourceLimits` → `--cpus`, `--memory`, `--pids-limit`.
- Streaming: container stdout is line-buffered NDJSON; host tails via `docker run` stream and replays into the existing `StreamCallback`. Minimal renderer changes.
- Container exit code mirrors task pass/fail; host parses `task.json` regardless of exit code (criterion failures aren't process failures).

### Phase 4 — Batch + CLI wiring
- `run_batch` is unchanged structurally — it already spawns N orchestrator runs in parallel, each producing one `task.json`. The only difference: with `isolation=docker`, each orchestrator's `Sandbox` is a `DockerBackend`.
- CLI: `--isolation docker` flag on `coder-eval run` overrides `SandboxConfig.isolation`.
- Aggregation, suite rollups, reports: **zero changes** — they read `task.json` files as they already do.

### Phase 5 — Hardening
- Image-version assertion: container writes its `coder_eval` version into `task.json` metadata; host warns/errors on mismatch.
- Timeout: host-side `docker kill` if a container exceeds `task_timeout` (defense in depth on top of the in-container timeout).
- `make lint` rule (CE-new): forbid `subprocess.run` / `os.system` in `agents/` and `criteria/` — must go through `SandboxBackend.exec`. Prevents accidental host execution.
- Docs: `docs/DOCKER_ISOLATION.md` user guide.

## Non-goals (for now)

- Reusing containers across tasks (state-bleed risk; the per-task spawn cost is dwarfed by LLM latency).
- Running aggregation in a container.
- Custom per-task images (single pinned image; tasks bring their deps in via `requirements.txt` inside the sandbox if needed).
- Windows host support (Linux + macOS Docker Desktop only).
