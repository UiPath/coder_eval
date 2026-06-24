# User Telemetry (OpenTelemetry → Azure Application Insights)

**Related PR:** _TBD_

## What it does

`coder_eval` emits discrete **usage-telemetry** events to the Azure Application
Insights `customEvents` table using the OpenTelemetry logs SDK + the Azure
Monitor exporter. The events are lifecycle signals only — run start, task end,
and per-command outcomes — used to understand how the framework is used
(volume, agent/model mix, durations, success/failure rates). It is a
cross-cutting side-channel: it is **not** part of the evaluation data path and
never touches `EvaluationResult`, `task.json`, criteria, or reports.

The telemetry SDK ships **by default** (the `opentelemetry-sdk` and
`azure-monitor-opentelemetry-exporter` packages are core dependencies) — there
is no extra to install. Telemetry is gated **off by configuration**, not by
dependency presence.

## Privacy stance

Events carry **only enums, counts, durations, config-derived identifiers, an
anonymous install id, and non-PII platform identity**. They never contain
prompts, file contents, source code, repo paths, dialog history, or any
free-text user content. Property values are coerced to scalars before emission;
anything non-scalar is stringified and `None` values are dropped.

The `InstallId` is a random UUID generated once and persisted to the user config
file (`~/.config/coder-eval/config.json`, key `install_id`) — it identifies an
**install, not a person**, and never derives from a username, email, or hostname.
Platform dimensions (`OS` / `OSVersion` / `Arch` / `PythonVersion`) come from the
stdlib `platform` module and carry no user identity.

`TaskId` / `VariantId` are author-defined free-text and could encode sensitive
data, so they are emitted as a **stable, truncated SHA-256 hash** (`hash_identifier`),
never verbatim — the raw string never reaches the telemetry store. The hash is
deterministic across runs and installs, so dashboards can still group/slice by a
consistent per-task key.

## Enabling / disabling

Telemetry is **off** unless a connection string is configured — this repo ships
**no** embedded connection string.

**Enable:** set a connection string via any of these environment variables.
When more than one is set, they resolve in `AliasChoices` declaration order
(first present wins):

| Env var | Notes |
|---|---|
| `TELEMETRY_CONNECTION_STRING` | The bare field name (highest precedence) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Standard App Insights variable |
| `UIPATH_AI_CONNECTION_STRING` | UiPath-internal alias |

(In practice only one is set, so precedence rarely matters.)

**Disable** (even when a connection string is set):

| Mechanism | Effect |
|---|---|
| `TELEMETRY_ENABLED=false` | Settings flag (default `true`) — the single canonical disable gate |

With telemetry off (the default for local/base runs), every telemetry call is a
cheap no-op — no events, no network, no behavior change.

> **The install-id config file is best-effort.** If `~/.config/coder-eval/config.json`
> can't be created (e.g. read-only home), telemetry **still emits events — just
> without the `InstallId` dimension** — and never crashes. Telemetry is fully
> non-fatal; there is no command (including `run`) that exits on a telemetry
> failure.

### Recommended resource

Point coder_eval at its **own dedicated** App Insights resource — **not** a
customer-facing product-telemetry resource. A single batch emits hundreds of
`Task.End` events; routing those into a product resource would pollute its
dashboards and conflate "users ran the product" with "we ran an eval batch."
Provisioning that resource and supplying its connection string is an
operational task (a deployment input, not a code artifact).

## Event catalog

All events carry these **enrichment** dimensions (added to every event):

| Dimension | Value |
|---|---|
| `Version` | coder-eval version |
| `SessionId` | one UUID per process |
| `InstallId` | anonymous per-install UUID, persisted in `~/.config/coder-eval/config.json` (best-effort — omitted if it can't be written) |
| `Source` | constant `"coder-eval"` |
| `IsCI` | `True` when the `CI` env var is set |
| `OS` / `OSVersion` / `Arch` | `platform.system()` / `release()` / `machine()` |
| `PythonVersion` | `platform.python_version()` |

### `CoderEval.Run.Start`
Emitted once per `coder-eval run` invocation (CLI/batch layer).

| Property | Meaning |
|---|---|
| `TaskFileCount` | Number of task **files** (pre dataset/variant expansion) |
| `MaxParallel` | `--max-parallel` value |
| `AgentType` | `--type` override or `"default"` |
| `StreamMode` | `"full"` / `"minimal"` / `"none"` |
| `Resume` | `--resume` flag |
| `ExperimentProvided` | Whether an explicit `--experiment` was passed |

> `TaskFileCount` counts files *before* dataset fan-out and variant resolution
> (those happen later). Per-task counts are reconstructable by counting
> `CoderEval.Task.End` / `.Failed` events.

### `CoderEval.Task.End` / `CoderEval.Task.Failed`
Emitted once per finalized task from the orchestrator. The `.Failed` name is
used for the hard-failure statuses (`ERROR`, `TIMEOUT`,
`TOKEN_BUDGET_EXCEEDED`, `COST_BUDGET_EXCEEDED`); every other status (including
`FAILURE` = ran-but-scored-below-threshold, and `MAX_TURNS_EXHAUSTED`) uses
`.End`. The exact status is always in the `Status` property regardless.

| Property | Meaning |
|---|---|
| `TaskId` / `VariantId` | Stable SHA-256 hash of the task + experiment-variant ids (never verbatim) |
| `Status` | Exact `FinalStatus` value |
| `DurationMs` | Wall-clock task duration |
| `Score` | Weighted score |
| `Iterations` | Iteration (turn) count |
| `AgentType` / `Model` | Resolved agent kind + model |
| `Driver` | Sandbox driver (`tempdir` / `docker`) |

> Token counts are intentionally **not** emitted — this is usage telemetry, not
> eval analytics. Per-task token/cost remains in `task.json` and the reports.

### `CoderEval.Cli.<command>`
Emitted once per public CLI command (`run`, `plan`, `evaluate`, `report`,
`proxy`) on completion. The hidden in-container `_run-task-internal` command is
**not** instrumented (it would double-count).

| Property | Meaning |
|---|---|
| `Status` | `"Succeeded"` / `"Failed"` |
| `DurationMs` | Command wall-clock duration |
| `ErrorType` | `""`, `"Exit"` (non-zero `typer.Exit`), or the exception class name |

> The `run` command emits `CoderEval.Cli.run` **and** `CoderEval.Run.Start` +
> N×`CoderEval.Task.End/.Failed` — intentional, at different granularities.

## Where it sits in the flow

Telemetry touches exactly three seams:

1. **Process init** — `cli/__init__.py::main()` (the Typer `@app.callback`,
   alongside `load_plugins()`): one-time `init_telemetry(version=...)`.
2. **Run start** — `cli/run_command.py::_run_all_tasks()`: emits
   `CoderEval.Run.Start`, then `flush_telemetry()` in a `finally`.
3. **Task end** — `orchestrator.py::_finalize_result()`: emits
   `CoderEval.Task.End` / `.Failed`. Under `--driver docker` the orchestrator
   runs *inside* the container (where telemetry is off — the connection-string
   env vars aren't forwarded), so the host re-emits the same event from
   `orchestration/batch.py` after the container result is parsed. Both paths
   build the event through the shared `build_task_event` helper, so the two
   drivers are at parity and there is no double-counting.

Per-command events are wired by wrapping each command at registration with the
`track_command(name)` decorator.

## How it works (customEvents routing)

The Azure Monitor exporter routes an OpenTelemetry **log record** to the
`customEvents` table (instead of the default `traces` table) iff the record
carries the attribute `microsoft.custom_event.name`. coder_eval reaches that
attribute through plain stdlib logging: a dedicated logger
(`coder_eval.telemetry.events`, `propagate=False` so events never reach the
console/`task.log`) has an OTel `LoggingHandler` attached, and `track_event`
calls `logger.info(name, extra={...})`. So the OTel/Azure imports live only
inside `init_telemetry`, and `track_event` is pure stdlib — a cheap no-op when
telemetry is off.

## Non-fatal contract

Every public telemetry function (`init_telemetry`, `track_event`,
`flush_telemetry`, `shutdown_telemetry`) wraps its body in `try/except
Exception` and logs a warning rather than raising — telemetry must never break
a run. This is enforced mechanically by the **CE019** custom lint rule.
Runtime export failures are swallowed by the exporter's background batch
processor, so `track_event` only enqueues and can never block or raise from a
network fault.
