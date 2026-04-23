# evalboard

Minimal localhost dashboard for coder_eval runs. Next.js App Router, reads
runs directly from Azure Blob Storage in server components — no database,
no persistent backend.

## Setup

```bash
cd coder_eval/evalboard
pnpm install
az login        # on an Azure VM with managed identity this is automatic
pnpm dev
# open http://localhost:3030
```

Auth uses `DefaultAzureCredential` — `az login` locally, VM managed identity
in production (grant **Storage Blob Data Reader** on the storage account).

## Layout

Evalboard is the **drill-down companion** to the ADX dashboard. ADX owns
trends, heatmaps, and time-range filtering; evalboard owns the clickable
path into a specific run's artifacts, criteria, and logs.

- `/` — the 20 most recent runs, one row each, clickable.
- `/runs/latest` — shortcut that redirects to the newest run id.
- `/runs/<run-id>` — run summary (pass rate, cost, duration) + one row per
  task. Link this from the ADX dashboard's per-run cell.
- `/runs/<run-id>/<task-id>` — per-task detail: success-criteria cards,
  artifact downloads, flow debug table, tool timeline, tail of `task.log`.
  Link this from the ADX dashboard's per-task row.

`<task-id>` is the same string the eval framework writes to
`task_results[].task_id` (e.g., `skill-flow-calculator`) and equals the
subdir name under `<run-id>/default/`.

## Deploy

`./scripts/deploy.sh` from this directory. Builds, uploads to blob storage, and restarts the App Service. The script is the authoritative deploy flow — see [DEPLOYMENT.md](DEPLOYMENT.md) for architecture, auth wiring, and troubleshooting context.

## Caching

Runs are lazy-downloaded from blob on first view and served from disk
thereafter. Cache lives at `./runs-remote` (gitignored). Run folders are
immutable once uploaded, so the cache is never auto-invalidated. To
force-refresh a run, delete `runs-remote/<run-id>/` and reload.

Storage account (`coderevaltests`), container (`runs`), and the ADX
dashboard URL are hardcoded in `lib/config.ts` and `lib/blob.ts` — no env
file needed.

## Conventions

- `/api/file?run=<id>&path=<relpath>` serves `.flow`, `.uipx`, etc. with
  path-traversal guard (`resolveSafePath`).
- Pass rows render green (`bg-green-50 text-green-700`), failures render red
  (`bg-red-50 text-red-700`), on a white background.
