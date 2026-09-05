---
title: "Evaluate AI coding agents and their skills — Coder Eval"
description: >-
  Coder Eval is Playwright for coding agents: an open-source, agent-agnostic
  framework to evaluate, benchmark, and A/B-test AI coding agents and their skills
  in a sandbox — declarative YAML tasks, weighted scoring, cost/token telemetry, and
  CI gates for Claude Code, Codex, Antigravity (Gemini), and OpenCode.
---

# Evaluate AI coding agents and their skills — Coder Eval

**Coder Eval** (`pip install coder-eval` / `uv tool install coder-eval`) is an
open-source, **agent-agnostic** framework for **evaluating AI coding agents and their
skills** — built for benchmark authors, CLI builders, and skill builders — with
sandboxing, reproducibility, and data-driven analysis. It runs a real agent —
**Claude Code**, **OpenAI Codex**, **Google Antigravity (Gemini)**, or **OpenCode** —
against declarative YAML tasks in a sandbox. Changing harness is one field
(`agent.type`); the tasks, criteria, scoring, telemetry, and reports stay the same.
It is **not a fixed leaderboard**: you bring the tasks and you bring the scoring.

If you have ever asked *"how do I benchmark coding agents on my own domain tasks?"*,
*"how do I test whether my skill actually triggers?"*, *"how do I compare Claude Code
vs. Codex vs. Gemini vs. OpenCode on the same suite?"*, or *"how do I gate CI on
coding-agent quality?"* — this is the framework for that.

<p align="center">
  <img src="assets/hero.gif" alt="Coder Eval running a sandboxed coding-agent evaluation from a YAML task and browsing the scored result in evalboard" width="100%">
</p>

## What Coder Eval does

- **Declarative YAML tasks** with pinned dependencies and clear success criteria
- **Sandboxed execution** in isolated environments with resource limits
- **Weighted, continuous scoring** (0.0–1.0) with fractional credit and thresholds
- **Many criterion types** — from file checks to code similarity and LLM-graded rubrics
- **Agent-agnostic by design** — Claude Code, OpenAI Codex, Antigravity (Gemini), and OpenCode today; add your own harness through the plugin SPI
- **Experiment layer** — A/B agent configs (models, tools, prompts) side-by-side
- **Full telemetry** — every tool call, token counts, and cost, with real-time streaming

## Use cases

- **Benchmark coding agents** — score an agent across a suite of tasks with weighted pass/fail thresholds
- **Compare models & configs** — A/B-test Claude Code vs. Codex vs. Gemini vs. OpenCode, model vs. model, tool-on vs. tool-off, prompt vs. prompt
- **Test whether a skill triggers** — verify an agent actually engages a target
  skill (`skill_triggered`) and score skill-driven suites (SkillsBench-style), on
  whichever harness your users run
- **Keep skills fresh in CI** — re-validate skills on every change or on a schedule; catch silent regressions when models, prompts, or the skills themselves drift
- **Gate CI on agent quality** — run the suite in GitHub Actions and fail the build on regressions
- **Bring your own dataset** — fan one task out over many rows for larger benchmark suites

## Quick start

```bash
# 1. Install the coder-eval CLI on your PATH (isolated environment)
uv tool install coder-eval

# 2. Grab the runnable example tasks
git clone https://github.com/UiPath/coder_eval.git
cd coder_eval

# 3. Credentials — optional if you're already logged in to Claude Code
#    (`claude login`, reused automatically); otherwise set ANTHROPIC_API_KEY
cp .env.example .env

# 4. Validate, run, and read your first evaluation
coder-eval plan tasks/hello_date.yaml   # validate (no tokens spent)
coder-eval run  tasks/hello_date.yaml   # run your first evaluation
coder-eval report runs/latest           # view the result
```

Prefer Coder Eval as a project dependency instead of a CLI tool? `uv add
coder-eval` or `pip install coder-eval`. Hacking on Coder Eval itself? Clone,
`uv sync --extra dev`, and prefix commands with `uv run`.

New here? Start with **[Tutorial 01 — Your First Evaluation](tutorials/01-first-evaluation.md)**.

## Where to go next

<!-- docs-index:start -->
| Guide | What's in it |
| --- | --- |
| [Tutorials](tutorials/README.md) | Step-by-step walkthroughs — start here |
| [User Guide](USER_GUIDE.md) | Full CLI, configuration, output, and environment-variable reference |
| [Task Definition Guide](TASK_DEFINITION_GUIDE.md) | The task-file schema — all criterion types, scoring, templates |
| [Claude Code](agents/CLAUDE_CODE.md) | Configuring and running the default Claude Code agent |
| [Codex](agents/CODEX.md) | Running the OpenAI Codex agent |
| [Antigravity (Gemini)](agents/ANTIGRAVITY.md) | Running the Google Antigravity / Gemini agent |
| [OpenCode](agents/OPENCODE.md) | Running the OpenCode agent on open-weight models |
| [Run-Limit Parity](agents/HARNESS_PARITY.md) | What each run_limits field means on every harness |
| [A/B Experiments](AB_EXPERIMENTS.md) | Compare models / tools / prompts across the same tasks |
| [Bring Your Own Dataset](DATASETS.md) | Fan a single task out over a dataset |
| [Dialog Mode](DIALOG_MODE.md) | Evaluate agents in multi-turn conversation via a simulated user |
| [Docker Isolation](DOCKER_ISOLATION.md) | The container sandbox driver, with custom images |
| [CI Gate & GitHub Action](CI_GATE.md) | Run Coder Eval as a CI gate — the Marketplace Action, JUnit output, run reports |
| [Claude Code Plugin](PLUGIN.md) | Install the Claude Code plugin — author, run, and analyze suites from inside the agent |
| [Extending Coder Eval](EXTENDING.md) | Author a custom agent, criterion, or model pricing via the plugin SPI |
| [Report Schema](REPORT_SCHEMA.md) | Field-level reference for run.json / variant.json / task.json |
| [How It Compares](comparison.md) | vs. SWE-bench, SkillsBench, Harbor, OpenAI Evals, hand-rolled scripts |
<!-- docs-index:end -->

## How Coder Eval compares

- **vs. SWE-bench and fixed benchmarks** — SWE-bench is a fixed dataset; Coder Eval
  is a *framework* for authoring your own tasks in YAML, so you evaluate the skills
  and workflows you care about (and can still wrap a fixed dataset via
  [Bring Your Own Dataset](DATASETS.md)).
- **vs. other agent-eval frameworks (e.g. Harbor) and LLM-eval tools (OpenAI Evals)** —
  OpenAI Evals grades model text; Harbor targets large-scale agent eval and RL
  optimization. Coder Eval is purpose-built for coding-agent/skill suites — weighted
  0.0–1.0 file/command scoring, a `skill_triggered` activation check, an experiment
  layer for A/Bs, and evalboard. See the full comparison.
- **vs. hand-rolled scripts** — reproducible sandboxes, weighted criteria,
  cost/token telemetry, A/B experiments, and CI-ready pass/fail gates out of the box.

See the full [comparison](comparison.md) for details.
