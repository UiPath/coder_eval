# Coder Eval — evaluate & benchmark AI coding agents and Claude Code skills

[![PyPI](https://img.shields.io/pypi/v/coder-eval.svg)](https://pypi.org/project/coder-eval/)
[![Website](https://img.shields.io/badge/website-coder--eval.com-1f6feb.svg)](https://coder-eval.com)
[![Docs](https://img.shields.io/badge/docs-uipath.github.io%2Fcoder__eval-1f6feb.svg)](https://uipath.github.io/coder_eval/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/UiPath/coder_eval/actions/workflows/pr-checks.yml/badge.svg)](https://github.com/UiPath/coder_eval/actions/workflows/pr-checks.yml)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Coder Eval** (`pip install coder-eval` / `uv tool install coder-eval`) is an open-source framework for
**evaluating and benchmarking AI coding agents and their skills** — built for CLI
and skill builders — with sandboxing, reproducibility, and data-driven analysis.
It runs a real agent (**Claude Code**, **Codex**, or **Google Antigravity /
Gemini**) in a sandbox against declarative YAML tasks, then scores the files and
commands it actually produced. Not an "agentic coding" benchmark: it measures how
effective your CLI and skills are when used by coding agents.

Reach for it when you want to **test whether a Claude Code skill triggers**,
**A/B-test Claude Code vs. Codex vs. Gemini** (or model vs. model, prompt vs.
prompt), or **gate CI on coding-agent quality**. Unlike fixed datasets (SWE-bench,
SkillsBench) that rank models on a shared leaderboard, Coder Eval evaluates the
tasks, skills, and workflows *you* ship — with weighted 0.0–1.0 criteria, a
`skill_triggered` activation check, an A/B experiment layer, and per-tool cost
telemetry. See [How it compares](https://uipath.github.io/coder_eval/comparison/).
📚 **Full docs:** **[uipath.github.io/coder_eval](https://uipath.github.io/coder_eval/)**.

<p align="center">
  <img src="docs/assets/hero.gif" alt="Coder Eval running the hello_date task: a sandboxed agent writes and runs a script from a YAML task, then the scored result is browsed in evalboard" width="100%">
</p>

- **Declarative YAML tasks** with pinned dependencies and clear success criteria
- **Sandboxed execution** in isolated environments with resource limits
- **Weighted, continuous scoring** (0.0–1.0) with fractional credit and thresholds
- **Many criterion types** — from file checks to code similarity and LLM-graded rubrics
- **Agent abstraction** — Claude Code, Codex, and Antigravity (Gemini) today, extensible via a plugin SPI
- **Experiment layer** — A/B agent configs (models, tools, prompts) side-by-side
- **Full telemetry** — every tool call, token counts, and cost, with real-time streaming

## What you can do with it

- **Benchmark coding agents** — score an agent across a suite of tasks with weighted scoring and pass/fail thresholds
- **Compare models & configs** — A/B-test Claude vs. Codex vs. Gemini, model vs. model, tool-on vs. tool-off, prompt vs. prompt
- **Evaluate skills** — verify an agent actually engages a target skill (`skill_triggered`) and score skill-driven suites (SkillsBench-style)
- **Keep skills up to date in CI** — re-validate your skills on every change or on a schedule; catch silent regressions when models, prompts, or the skills themselves drift
- **Gate CI on agent quality** — run the suite in GitHub Actions and fail the build on regressions
- **Bring your own dataset** — fan one task out over many rows for larger benchmark suites

> **Keeping skills fresh?** Run Coder Eval as a scheduled GitHub Actions job so your
> skills are continuously re-evaluated against the latest model — a skill that quietly
> stops triggering surfaces as a failing criterion before your users hit it. See
> **[Tutorial 02 — Running coder_eval in CI](docs/tutorials/02-ci-pipeline.md)**.

## Quick Start

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/) 0.8+, and the
[Claude CLI](https://docs.anthropic.com/claude/docs/claude-code) (`brew install claude`).
Developed on macOS; CI runs on Linux.

```bash
git clone https://github.com/UiPath/coder_eval.git
cd coder_eval

uv sync --extra dev          # install core + dev tools
cp .env.example .env         # then set ANTHROPIC_API_KEY — or skip that: an
                             # existing Claude Code login (`claude login`) is
                             # picked up automatically

uv run coder-eval plan tasks/hello_date.yaml   # validate (no tokens spent)
uv run coder-eval run  tasks/hello_date.yaml   # run your first evaluation
uv run coder-eval report runs/latest           # view the result
```

New here? Follow **[Tutorial 01 — Your First Evaluation](docs/tutorials/01-first-evaluation.md)**.

The optional `[uipath]` extra (`uv sync --extra dev --extra uipath`) adds the in-host
`uipath` SDK for local sandbox parity; it installs from public PyPI (no credentials
required). Without it the framework runs end-to-end; uipath-dependent features fail
at dispatch with a clear hint.

**Using Coder Eval in CI or another project?** Install the published package
instead of cloning:

```bash
uv tool install coder-eval    # puts the `coder-eval` CLI on your PATH,
                              # in its own isolated environment

uv tool install "coder-eval[codex,antigravity]"   # same, with agent extras
coder-eval --version                              # verify the install
```

To add it as a project dependency instead: `uv add coder-eval` or
`pip install coder-eval`. In a real CI gate, pin to a specific released version
so a harness upgrade can't silently move your results. (The example `tasks/`
live in this repo — clone it or point the CLI at your own task files.) See
[Tutorial 02 — Running coder_eval in CI](docs/tutorials/02-ci-pipeline.md) for
the full setup.

## Telemetry

> 📊 **Usage telemetry is on by default.** `coder-eval` sends **anonymous** usage
> telemetry (command names, outcomes, counts, durations, an anonymous install id,
> platform info) to help improve the tool. It **never** captures prompts, file
> contents, or repo paths, and prints a one-time notice on first run. **To disable
> it, set `TELEMETRY_ENABLED=false`** in your `.env` or environment. See
> [Usage Telemetry](docs/USER_GUIDE.md#usage-telemetry) for details and how to route
> it to your own resource.

## Documentation

| Guide | What's in it |
| --- | --- |
| [Tutorials](docs/tutorials/README.md) | Step-by-step walkthroughs — start here |
| [User Guide](docs/USER_GUIDE.md) | Full CLI, configuration, output, and environment-variable reference |
| [Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md) | The task-file schema — all criterion types, scoring, templates |
| [A/B Experiments](docs/AB_EXPERIMENTS.md) | Compare models / tools / prompts across the same tasks |
| [Bring Your Own Dataset](docs/BYOD.md) | Fan a single task out over a dataset |
| [Codex Agent Guide](docs/CODEX_AGENT_GUIDE.md) | Running the Codex agent |
| [Docker Isolation](docs/DOCKER_ISOLATION.md) | The container sandbox driver |
| [CLAUDE.md](CLAUDE.md) | Architecture, key patterns, and extension points |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, quality bar, and how to contribute |

## How it compares

- **vs. fixed benchmarks (SWE-bench, SkillsBench)** — they score a canonical dataset;
  Coder Eval scores *your* tasks with continuous 0.0–1.0 weighted criteria (and can
  still wrap a fixed dataset via [Bring Your Own Dataset](docs/BYOD.md)).
- **vs. large-scale / RL harnesses (Harbor)** — Harbor targets scale and RL rollouts;
  Coder Eval targets weighted, skill-aware suites gated in CI.
- **vs. model-output eval tools (OpenAI Evals)** — they grade model text; Coder Eval
  runs a full agent in a sandbox and scores the files and commands it produced.
- **vs. hand-rolled scripts** — reproducible sandboxes, weighted criteria,
  cost/token telemetry, A/B experiments, and CI-ready pass/fail gates out of the box.

See the full [comparison — with sources](https://uipath.github.io/coder_eval/comparison/).

## Task Definition

A task is a YAML file: a prompt, the agent config, a sandbox, and success criteria.

```yaml
task_id: "hello_world"
description: "Create a Python script that prints Hello, World!"
initial_prompt: "Create hello.py that prints 'Hello, World!'"

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]

sandbox:
  driver: "tempdir"
  python: {}

success_criteria:
  - type: "file_exists"
    path: "hello.py"
    description: "hello.py must be created"
  - type: "run_command"
    command: "python hello.py"
    timeout: 10
    description: "Script must execute successfully"
```

Tasks can omit the `agent` section entirely — defaults resolve from the experiment
layer (`experiments/default.yaml`). For the full schema and every criterion type,
see the [Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md).

> **Tip:** In Claude Code, use `/coder-eval-task-create` to scaffold a task from a
> natural-language description, and `/coder-eval-run-analysis runs/latest` to get
> improvement suggestions from a completed run.

## Development

```bash
make install    # package + dev + [uipath] deps + pre-commit hooks
make verify     # format + lint + typecheck + test + coverage (CI equivalent)
```

Run `make verify` before pushing — it mirrors CI (80% coverage threshold). See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow, commit conventions, and
extension points (new criteria, new agents).

## Known limits & non-goals

- **Not a fixed benchmark or leaderboard** — Coder Eval scores *your* tasks and ships
  example tasks, not a canonical scored dataset.
- **Tasks execute real code** — run untrusted tasks only under the container driver
  (see [Docker Isolation](docs/DOCKER_ISOLATION.md)); the `tempdir` driver is not a
  security boundary.
- **Bring your own model credentials** — Anthropic, Bedrock, or Gemini keys; Coder Eval
  does not proxy or supply model access.
- **Python 3.13+ only.**

## Support & security

- **Security vulnerabilities** — report privately via [SECURITY.md](SECURITY.md); never open a public issue.
- **Bugs & questions** — open a [GitHub issue](https://github.com/UiPath/coder_eval/issues).
- **Everything else** — reach the maintainers privately at **coder-eval@uipath.com**.

## License

© 2026 UiPath. Licensed under the Apache License, Version 2.0 — see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

## Acknowledgments

Built with the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk),
[Pydantic](https://pydantic.dev/), [Typer](https://typer.tiangolo.com/), and
[Rich](https://rich.readthedocs.io/).
