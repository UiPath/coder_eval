# Tasks

Task definitions for coder_eval. Each `*.yaml` is one task (see
[docs/TASK_DEFINITION_GUIDE.md](../docs/TASK_DEFINITION_GUIDE.md) for the schema).
Files are organized by purpose, not just dumped flat — this README is the map.
New here? The curated examples in [`samples/`](./samples) are the best place to
start.

Every task file must carry at least one `tags:` entry (enforced by
`tests/test_tags.py`). Tags are how the runner selects subsets, e.g.
`coder-eval run tasks/*.yaml --tags smoke-pass`.

## `samples/` — start here

Curated, runnable real-world tasks — a genuine evaluation rather than a toy, and
the best first stop. Each is self-contained and documented in its own
[`samples/README.md`](./samples).

## Feature / config examples (root)

Minimal tasks that each demonstrate one framework feature; referenced from the
docs.

| Task | Demonstrates |
|------|--------------|
| `fibonacci_with_template` | `template_dir` starter files |
| `inline_starter_example` | inline starter files |
| `sentiment_classification` | `classification_match` + a JSONL dataset (`datasets/`) |
| `mock_path_dirs_smoke` | mocking CLIs on `PATH` (uses `mock_path_dirs_template_dir/`) |
| `test_sandbox` | the smallest possible sandbox task |

## `agents/` — agent feature-tests

Per-agent capability probes (hello-world, streaming, sub-agents, parallel tool
calls, skills discovery, disallowed-tools). Grouped by the `claude_` / `codex_` /
`antigravity_` / `subagent_` prefix. Several need a specific backend or extra
(`--type codex`, `GEMINI_API_KEY`, etc.), so they are **not** in the CI smoke
buckets — run them individually or with a `-e` experiment.

## Smoke / self-test sentinels (root)

Tiny tasks the CI and `make` targets run to prove the framework itself works.
**These stay at the root** because CI and the Makefile select them with the
single-level glob `tasks/*.yaml --tags <bucket>` — moving them into a subdir
would drop them from those runs.

| Tag bucket | Meaning | Run by |
|------------|---------|--------|
| `smoke-pass` | Expected to **succeed** | `pr-checks.yml`, `make test-smoke` |
| `smoke-fail` | Expected to **fail** — failure-detection sentinels (coder-eval exits non-zero, which CI asserts) | `pr-checks.yml`, `make test-smoke` |
| `smoke-variants` | Multi-variant resolver check (run with `-e experiments/smoke_variants.yaml`) | `pr-checks.yml` |
| `smoke` | Umbrella over the pass + fail buckets | ad hoc |

Members: `hello_date`, `agentless_smoke_test`, `byod_smoke_test`,
`dataset_example`, `smoke_agent_judge`, `smoke_llm_judge`, `smoke_negative_path`,
`smoke_budget_exceeded`, `smoke_cost_budget_exceeded`, `smoke_task_timeout`,
`smoke_variants`, `token_check`.

## Support subdirectories

| Directory | What it holds |
|-----------|---------------|
| `datasets/` | JSONL datasets referenced by dataset-backed tasks |
| `dockerfile_build_example/` | A task that builds its own Dockerfile |
| `mock_path_dirs_template_dir/` | Template dir (mock CLI bins) consumed by `mock_path_dirs_smoke` |
| `python_cli_simulated_judged/` | Simulated multi-turn dialog + judged scoring |
| `internal/` | Dev smoke tests exercising harness internals (e.g. session-resumption / context retention) |
