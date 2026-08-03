# evalboard

Minimal localhost dashboard for coder_eval runs. Next.js App Router, reads runs
from a local runs directory in server components — no database, no persistent
backend.

## Setup

```bash
cd coder_eval/evalboard
pnpm install
EVALBOARD_LOCAL_RUNS_DIR=/path/to/runs pnpm dev
# open http://localhost:3030
```

Point `EVALBOARD_LOCAL_RUNS_DIR` at any coder_eval runs directory; the listing
comes from the filesystem. `pnpm dev:local` is a shortcut for coder_eval's own
`runs/` (relative to `evalboard/`). Only directories containing a `run.json`
show up in the index — empty shells and the `latest` symlink are filtered out.

## Layout

- `/` — the 20 most recent runs, one row each, clickable. Includes a
  daily success-rate chart and tag rails for filtering.
- `/trends` — per-task pass rate and avg duration/cost/turns across the
  last 10 runs, with a tag filter and expandable per-task history.
- `/runs/latest` — shortcut that redirects to the newest run id.
- `/runs/<run-id>` — run summary (pass rate, cost, duration) + one row per task.
  A "Download run (.zip)" button bundles the whole run folder.
- `/runs/<run-id>/<task-id>` — per-task detail: success-criteria cards,
  artifact downloads, flow debug table, tool timeline, message timeline
  (per-message generation / exec time and output / cache-write / cache-read
  tokens, with each row expandable into thinking / tool / text sub-rows),
  tail of `task.log`. A "Download folder (.zip)" button bundles this task's
  folder.

`<task-id>` is the same string the eval framework writes to
`task_results[].task_id` (e.g., `skill-flow-calculator`) and equals the
subdir name under `<run-id>/<variant-id>/`, where `<variant-id>` is the
experiment arm — `default` for a single-config run, or the arm name (e.g.
`opus`, `with-skill`) in an A/B run. The task page selects the arm via `?v=`
(mirroring `?r=` for replicates); a bare URL resolves the run's actual arm.

## Conventions

- `/api/file?run=<id>&path=<relpath>` serves `.flow`, `.uipx`, etc. with
  path-traversal guard (`resolveSafePath`).
- `/api/download?run=<id>[&task=<id>][&v=<variant>]` streams a zip of a task
  folder (with `task`; `v` selects the arm, default `default`) or the whole run
  (without `task`). Files are gathered by `collectTaskFiles`
  / `collectRunFiles`, which reuse the `walkArtifacts` noise filter, and zipped
  by `lib/zip.ts` (a dependency-free DEFLATE writer).
- Pass rows render green (`bg-green-50 text-green-700`), failures render red
  (`bg-red-50 text-red-700`), on a white background.
