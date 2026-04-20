# evalboard

Minimal localhost dashboard for `coder_eval/runs/`. Next.js App Router, reads
the filesystem directly from server components — no DB, no API backend.

## Setup

```bash
cd coder_eval/evalboard
pnpm install
pnpm dev
# open http://localhost:3030
```

## Layout

- `/` — table of all runs with pass/fail + score + cost.
- `/runs/<id>` — expandable success-criteria cards, artifact downloads,
  tail of `task.log`.

## Configuration

- `EVALBOARD_RUNS_DIR` (optional) — absolute path to the runs directory.
  Defaults to `../runs/` relative to `evalboard/`. Copy `.env.example` to
  `.env.local` to set it persistently, or export it inline:
  `EVALBOARD_RUNS_DIR=/path/to/other/runs pnpm dev`.

## Conventions

- Reads from `RUNS_DIR` (see Configuration above).
- `/api/file?run=<id>&path=<relpath>` serves `.flow`, `.uipx`, etc. with
  path-traversal guard (`resolveSafePath`).
- Pass rows render green (`bg-green-50 text-green-700`), failures render red
  (`bg-red-50 text-red-700`), on a white background.
