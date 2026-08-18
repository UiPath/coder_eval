---
description: >-
  Write a Coder Eval task YAML from scratch — prompt, isolated sandbox, turn
  budget, and three success criteria — validate it without spending tokens, then
  run and score it.
---

# Tutorial 04 — Writing a Task

By the end you'll have authored a task YAML from scratch — a prompt, an isolated
sandbox, a soft turn budget, and three success criteria — validated it without
spending tokens, run it, and read the score. ~10 minutes.

## Prerequisites

- A working `coder-eval` checkout with an API key configured — see
  [Tutorial 01](01-first-evaluation.md).

## 1. Create the task file

A task is a single YAML file: the prompt sent to the agent, the agent
configuration, and the criteria that score whatever the agent leaves behind in
the sandbox. Create `tasks/fizzbuzz.yaml`:

```yaml
task_id: "fizzbuzz"
description: "Write and run a classic FizzBuzz script."
initial_prompt: >
  Create a Python file named fizzbuzz.py in the current working directory that
  prints the numbers 1 to 15, one per line, replacing multiples of 3 with
  'Fizz', multiples of 5 with 'Buzz', and multiples of both with 'FizzBuzz'.
  Then run it with: python fizzbuzz.py

agent:
  type: "claude-code"
  model: "claude-sonnet-5"
  permission_mode: "acceptEdits"
  setting_sources: []   # isolate the sandbox from your own CLAUDE.md/settings

run_limits:
  expected_turns: 5     # soft target — warns when exceeded, never aborts

success_criteria:
  - type: "file_exists"
    path: "fizzbuzz.py"
    description: "The script must be created."
  - type: "file_contains"
    path: "fizzbuzz.py"
    includes: ["FizzBuzz"]
    description: "The script must produce the combined FizzBuzz case."
  - type: "run_command"
    command: "python fizzbuzz.py"
    timeout: 10
    description: "The script must run successfully."
```

What each block does:

- **`initial_prompt`** — delivered to the agent inside a fresh sandbox
  directory; all file paths in the criteria are relative to that sandbox.
- **`agent.setting_sources: []`** — keeps your own `CLAUDE.md` and local
  settings out of the sandbox. Without it, a large host `CLAUDE.md` is injected
  into every API call, inflating cache-creation tokens and cost. Leave it out
  only when the task needs your MCP servers.
- **`run_limits.expected_turns`** — an efficiency target, not a cap: exceeding
  it logs a warning and adds a report badge but never aborts (use
  `run_limits.max_turns` for a hard cap).
- **`success_criteria`** — each criterion scores 0.0–1.0 and supports `weight`
  (default 1.0) and `pass_threshold` (default 0.9). `run_command` here only
  checks the exit code; it can also match stdout (`expected_stdout`) or read a
  continuous score from stdout — see the
  [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) for all 14 criterion
  types and every field.

## 2. Validate without spending tokens

```bash
uv run coder-eval plan tasks/fizzbuzz.yaml
```

`plan` checks the YAML schema, required tools, and API keys without calling the
model. Typos in criterion fields fail here (task models reject unknown keys)
instead of after a paid run.

## 3. Run it

```bash
uv run coder-eval run tasks/fizzbuzz.yaml
```

## 4. Read the result

```bash
uv run coder-eval report runs/latest
```

The report shows one row per criterion — score, pass/fail against its
threshold, and the weighted total. The agent's full transcript and the files it
created are preserved under `runs/latest/<variant>/<task>/<NN>/` (`task.json`,
`task.log`, `artifacts/`).

## Where to go deeper

- **All 14 criterion types, weights, thresholds** → [Task Definition Guide](../TASK_DEFINITION_GUIDE.md)
- **Stop a run early once the key criteria are decided** (an opt-in `stop_early:` block on a criterion; `run_limits.stop_early: false` is the run-level kill switch) → [Task Definition Guide → `stop_early`](../TASK_DEFINITION_GUIDE.md#stop_early-opt-in-early-stop)
- **Fan one task out over a dataset of rows** → [Bring Your Own Dataset](../DATASETS.md)
- **Full CLI & config reference** → [User Guide](../USER_GUIDE.md)
- **Compare two configurations on this task** → [Tutorial 05](05-comparing-models.md)
