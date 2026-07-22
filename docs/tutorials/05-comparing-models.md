---
description: >-
  A/B-test two Claude models on the same task with coder_eval's experiment layer
  and read the cross-variant report — a fair, like-for-like coding-agent model
  comparison.
---

# Tutorial 05 — Comparing Two Models

By the end you'll have A/B-tested two Claude models on the same task through the
experiment layer and read the cross-variant comparison report. Agents are
stochastic, so eyeballing two separate runs is noise — the experiment layer runs
both arms under identical conditions and scores them side by side. ~10 minutes.

## Prerequisites

- A working `coder-eval` checkout with an API key configured — see
  [Tutorial 01](01-first-evaluation.md). This tutorial uses the built-in
  `tasks/hello_date.yaml`.

## 1. Define the experiment

An experiment runs the same task once per **variant** and reports the arms side
by side — one config file, no copy-pasted tasks. Create
`experiments/haiku-vs-sonnet.yaml`:

```yaml
experiment_id: haiku-vs-sonnet
description: "Compare Haiku vs Sonnet on the same task"

defaults:
  agent:
    type: claude-code
    permission_mode: bypassPermissions

variants:
  - variant_id: haiku
    agent:
      model: claude-haiku-4-5-20251001
  - variant_id: sonnet
    agent:
      model: claude-sonnet-4-6
```

A variant declares only what differs — here just `agent.model`. Everything else
merges in from `defaults` and the task itself, so both arms are identical except
for the model. (The repo ships `experiments/model-comparison.yaml` as a
Sonnet-vs-Opus version of the same shape.)

## 2. Run both arms

```bash
uv run coder-eval run tasks/hello_date.yaml -e experiments/haiku-vs-sonnet.yaml
```

Bare names resolve too: `-e haiku-vs-sonnet` finds
`experiments/haiku-vs-sonnet.yaml`.

## 3. Read the comparison

The run writes the cross-variant report automatically when it finishes — there's
no separate report command for experiments:

```bash
cat runs/latest/experiment.md
```

It ranks the variants by weighted score and shows per-task results, durations,
and token usage side by side. Drill down from there:

- `runs/latest/experiment.json` — the full result data.
- `runs/latest/<variant_id>/variant.md` (e.g. `runs/latest/haiku/variant.md`) —
  per-variant aggregate detail.

## 4. Add statistical power (optional)

Agents are stochastic — one run per arm is noise, not signal. Repeat each arm to
see variance instead of a point estimate:

```bash
uv run coder-eval run tasks/hello_date.yaml -e haiku-vs-sonnet --repeats 5
```

(or set `repeats: 5` under `defaults:` in the experiment file). The report then
aggregates per-replicate scores per variant.

## Where to go deeper

- **Variants, prompt mutations, replicates, skill on/off comparisons** →
  [A/B Experiments](../AB_EXPERIMENTS.md)
- **Write the task you're comparing on** → [Tutorial 04](04-writing-a-task.md)
- **Full CLI & config reference** → [User Guide](../USER_GUIDE.md)
