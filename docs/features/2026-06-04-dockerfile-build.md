# Build the docker image from a task-supplied Dockerfile

**Status:** implemented · **Date:** 2026-06-04

## What it does

A task can now declare its own `Dockerfile` instead of pointing at a pre-built
image. When `sandbox.docker.dockerfile_path` is set, coder-eval builds the image
on the host before launching the container:

```yaml
sandbox:
  driver: docker
  docker:
    dockerfile_path: ./environment/Dockerfile   # relative to the task YAML
```

This is the natural fit for self-contained task suites (e.g. the skillsbench
layout, where each task ships an `environment/Dockerfile` plus its `input/`
data) — no separate "build and push an image" step.

## Contract: extend the framework image

The container runs the **whole orchestrator** via the framework image's
`ENTRYPOINT` (`coder-eval _run-task-internal "$@"`), so a task Dockerfile cannot
*replace* that image — it must **extend** it:

```dockerfile
FROM coder-eval-agent:latest   # inherit runtime + entrypoint
RUN apt-get install -y poppler-utils
COPY input/ /root/input/
```

A bare `FROM ubuntu` has no entrypoint; `docker run <image> --output ...` then
tries to exec `--output` and fails with
`exec: "--output": executable file not found in $PATH`. To turn that cryptic
runtime failure into an actionable one, `_build_image` runs
`_assert_runtime_entrypoint` after the build: it `docker image inspect`s the
`Config.Entrypoint` and raises `DockerRunError` (pointing at
`FROM coder-eval-agent:<version>`) unless the framework entrypoint
(`entrypoint.sh`) was inherited. An inspect failure is soft (the subsequent
`docker run` surfaces real problems). This was the gap behind the first reported
bug — the original cut built a bare image and failed opaquely at `docker run`.

(The alternative models — auto-layering the runtime onto an arbitrary base, or
running the task image as a *separate* agent-env container à la terminal-bench —
were considered and rejected: more code / fragile across distros, or a large
docker-runner redesign. Extending the base image is the minimal, robust fit for
the orchestrator-in-container architecture.)

## Design

### 1. Load-time path resolution (`orchestration/task_loader.py`)

`resolve_dockerfile_path(task, base_dir)` runs inside `load_task` alongside the
existing `resolve_template_paths` / `resolve_initial_prompt_file` resolvers. It:

- expands `$VAR` / `${VAR}` (mirroring `resolve_template_source_paths`),
- resolves a relative path against the task YAML's directory → absolute,
- raises `FileNotFoundError` if the file is absent.

A missing Dockerfile is therefore a **load-time** error (surfaced as
`ValueError` through `load_task`, consistent with a missing
`initial_prompt_file`), not an opaque `docker build` failure mid-run.

### 2. Image build (`isolation/docker_runner.py::DockerRunner._build_image`)

A new `_build_image()` method resolves the image to run:

- No `dockerfile_path` → returns the configured `image` unchanged.
- `dockerfile_path` set → `docker build -t <tag> -f <dockerfile> <context>`.
  - **Build context** = the Dockerfile's **parent directory**, so relative
    `COPY` paths resolve.
  - **Tag** = `coder-eval-task-<sanitized,lowercased task_id>:built` —
    deterministic, so Docker's layer cache is reused across runs.
  - A non-zero build → `DockerRunError` carrying `docker build`'s stderr.

`run()` calls `_build_image()` via `asyncio.to_thread` (it shells out, like the
other docker calls there), passes the resolved image into `_build_argv`, and
**skips** `_preflight_image_version` when a Dockerfile is used (task-built
images don't carry the `org.coder-eval.version` label).

### 3. `_build_argv` stays pure

`_build_argv` now takes an `image: str | None = None` parameter (defaulting to
the configured image) and contains **no** build logic. This preserves the
existing invariant — "argv rendering stays pure for testability" — so the mount
/ argv unit tests still run without a docker daemon. The build is the only
side-effect, and it lives in `_build_image`.

## Related change: `weight: 0` is now valid

Self-contained task Dockerfiles often pair with a `run_command` criterion that
*executes* a verifier script but should not itself contribute to the score (a
separate `file_contains` on the verifier's output does the scoring). That
"run but don't score" pattern needs `weight: 0`, which the model previously
rejected (`gt=0.0`).

`BaseSuccessCriterion.weight` is now `ge=0.0`. The scoring math already handles
it: a weight-0 criterion contributes `score*0` to the numerator and `0` to the
denominator, so it is excluded from the weighted average, and the existing
`total_weight > 0 else 0.0` guard covers the all-zero edge case. A weight-0
criterion still runs and its pass/fail still appears in reports.

## Tests

- `tests/test_image_from_dockerfiles.py` — load-time resolution (relative/abs,
  env-var, missing-file), `_build_image` (deterministic tag, parent-dir context,
  build-failure → `DockerRunError`, no-op without a Dockerfile), and
  `_build_argv` purity (image threaded through; no subprocess).
- `tests/test_continuous_scoring.py` — `weight=0` accepted, excluded from the
  weighted score, and the all-zero-weights guard.

## Notes / limitations

- Concurrent runs of the *same* task share the `coder-eval-task-<id>:built` tag.
  Docker serializes by tag, so this is safe; it also means a rebuild from one
  run is visible to the next (intended — that's the caching win).
- `pyproject.toml` `norecursedirs` gained `"resources"` so task fixtures under
  `tests/resources/` (which ship their own verifier `tests/test_*.py`) are not
  collected by the framework's own suite.
