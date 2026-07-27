---
description: >-
  Install coder-eval, point it at an API key or your Claude Code login, run a
  built-in task in a sandbox, and read your first scored coding-agent result —
  in about 5 minutes.
---

# Tutorial 01 — Your First Evaluation

By the end you'll have installed `coder-eval`, pointed it at an API key, run a
built-in task, and read the result. ~5 minutes.

## Prerequisites

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — `brew install uv` (macOS) or `pip install uv`
- **Claude CLI** — `brew install claude` ([install guide](https://docs.anthropic.com/claude/docs/claude-code))
- An **Anthropic API key**

## 1. Install

```bash
git clone https://github.com/UiPath/coder_eval.git
cd coder_eval
uv sync --extra dev
```

> **Just want the CLI?** `uv tool install coder-eval` installs the released
> `coder-eval` command on your PATH in its own isolated environment — then run
> every command below without the `uv run` prefix. Keep the clone anyway: the
> example `tasks/` used in this tutorial live in the repo.

`uv sync` creates a `.venv/`. **Activate it** to run `coder-eval` directly and
drop the `uv run` prefix used throughout this tutorial:

```bash
source .venv/bin/activate
# now `coder-eval ...` works on its own; e.g. `coder-eval run tasks/hello_date.yaml`
```

The `uv run coder-eval ...` form below works with or without activating — `uv run`
resolves the same `.venv` — so use whichever you prefer.

## 2. Configure your API key

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

> **Heads up:** usage telemetry is **on by default**. To turn it off, add
> `TELEMETRY_ENABLED=false` to your `.env`. See the
> [User Guide](../USER_GUIDE.md#usage-telemetry).

## 3. Validate before running

```bash
uv run coder-eval plan tasks/hello_date.yaml
```

`plan` checks the task's syntax, required tools, and API keys without spending any
tokens — a good habit before every run.

## 4. Run your first task

```bash
uv run coder-eval run tasks/hello_date.yaml
```

This spins up an isolated sandbox, sends the task prompt to the agent, records
every tool call, and scores the result against the task's success criteria.

Add `--stream full` to watch the agent work in real time:

```bash
uv run coder-eval run tasks/hello_date.yaml --stream full
```

## 5. Read the result

```bash
uv run coder-eval report runs/latest
```

`runs/latest` is a symlink to the newest `runs/<ts>/`. The per-task result lives at
`runs/latest/<variant>/<task>/<NN>/task.json` (with `task.log` and `artifacts/` beside
it); the run-level `run.json`/`run.md` sit at the run root. See the
[Output Structure](../USER_GUIDE.md#output-structure) reference for the full layout.

## Next steps

- **Write your own task** → [Task Definition Guide](../TASK_DEFINITION_GUIDE.md)
- **Compare configurations** → [A/B Experiments](../AB_EXPERIMENTS.md)
- **Full CLI & config reference** → [User Guide](../USER_GUIDE.md)
