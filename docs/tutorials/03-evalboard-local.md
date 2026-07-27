---
description: >-
  Browse Coder Eval runs in evalboard, a local web UI — pass rates, per-task
  detail, tool and message timelines, token costs, and artifact downloads,
  straight from your filesystem.
---

# Tutorial 03 — Browsing Results Locally with evalboard

`coder-eval` writes results as JSON, markdown, and logs under `runs/`. **evalboard**
is a small local web UI that renders those runs — pass rates, per-task detail,
tool and message timelines, artifact downloads — so you can explore a run in a
browser instead of reading raw files. It reads straight from your filesystem: no
database, no backend, nothing leaves your machine.

## Prerequisites

- A completed run (do [Tutorial 01](01-first-evaluation.md) first — you need a
  `runs/<timestamp>/` directory with a `run.json`).
- **Node.js 20+** and **pnpm**. The repo pins pnpm via `packageManager`, so the
  simplest way is Corepack:
  ```bash
  corepack enable    # provides the pinned pnpm
  ```

## 1. Install dependencies

```bash
cd evalboard
pnpm install
```

## 2. Point evalboard at your runs and start it

```bash
# Use coder_eval's own ./runs directory (shortcut):
pnpm dev:local

# ...or point at any runs directory (absolute path):
EVALBOARD_LOCAL_RUNS_DIR=/absolute/path/to/runs pnpm dev
```

Then open **http://localhost:3030**.

> Only directories that contain a `run.json` appear in the index — empty shells
> and the `latest` symlink are filtered out. If your run list is empty, confirm
> the run actually completed and that `EVALBOARD_LOCAL_RUNS_DIR` points at the
> parent `runs/` folder (not a single run).

## 3. Explore

| Page | What it shows |
| --- | --- |
| `/` | The 20 most recent runs, one clickable row each |
| `/runs/latest` | Redirects to the newest run |
| `/runs/<run-id>` | Run summary — pass rate, cost, duration — plus one row per task (green = pass, red = fail). "Download run (.zip)" bundles the whole folder |
| `/runs/<run-id>/<task-id>` | Per-task detail: success-criteria cards, the tool timeline, the message timeline (per-message tokens, expandable into thinking/tool/text), artifact downloads, and the tail of `task.log` |
| `/trends` | Per-task pass rate and average duration/cost/turns across recent runs |

`<task-id>` matches the `task_id` in the results and the subdirectory name under
`<run-id>/<variant>/` — the same layout described in the
[Output Structure](../USER_GUIDE.md#output-structure) reference.

## How it works (and why it's safe to run locally)

- **Filesystem-only in local mode.** Setting `EVALBOARD_LOCAL_RUNS_DIR` makes
  evalboard read runs from that directory and never reach out to any remote
  storage.
- **OSS edition by default.** evalboard runs in its open-source edition with no
  configuration; UiPath-internal surfaces are opt-in via `EVALBOARD_EDITION=internal`.
- **No database.** Run listings and detail pages are rendered on the fly from the
  files Coder Eval already wrote.

## Next steps

- **Produce more runs to compare** → [A/B Experiments](../AB_EXPERIMENTS.md)
- **Automate runs in CI, then download the artifact and open it here** →
  [Running Coder Eval in CI](02-ci-pipeline.md)
