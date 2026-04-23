---
allowed-tools: Bash(*), Read(*), Glob(*)
description: Pull a run (or list runs) from Azure Blob Storage using dashboard credentials
---

## Context

Download data uploaded by the dashboard pipeline from Azure Blob Storage. Prefer running `dashboard/scripts/pull-run.sh` directly — it implements all the logic below. This slash command is a thin wrapper for conversational use.

The script uses the same `az` CLI + `--auth-mode login` approach as `dashboard/src/dashboard/blob.py`, so no extra credentials are needed — whatever identity is active for `az` (local `az login` or VM managed identity) is used. Storage account and container come from `dashboard/.env` (`AZURE_STORAGE_ACCOUNT`, `AZURE_BLOB_CONTAINER`, default container `runs`). Runs are stored under the prefix `<run_id>/...`.

## Arguments

`$ARGUMENTS` is parsed as space-separated flags. Supported options:

| Argument | Default | Description |
|----------|---------|-------------|
| *(none)* | — | Download the latest run (resolved by lexical sort of `YYYY-MM-DD_HH-MM-SS`-prefixed run ids) |
| `run-id=<id>` | *(latest)* | Run id (top-level blob prefix) to download |
| `list` | false | List available run ids and exit |
| `dest=<path>` | `runs/<run-id>`, falling back to `tmp/runs/<run-id>` if it exists | Local destination directory |
| `container=<name>` | *(from .env, else `runs`)* | Override blob container |

Examples:
- `/coder-eval-pull-run` — download the latest run
- `/coder-eval-pull-run list` — list run ids in the container
- `/coder-eval-pull-run run-id=20260423-1530-abcd` — download that run
- `/coder-eval-pull-run run-id=20260423-1530-abcd dest=tmp/my-run` — custom destination

## Execution

Translate the parsed arguments into a single `dashboard/scripts/pull-run.sh` invocation:

| Parsed args | Command |
|-------------|---------|
| *(empty)* | `dashboard/scripts/pull-run.sh` |
| `list` | `dashboard/scripts/pull-run.sh list` |
| `run-id=X` | `dashboard/scripts/pull-run.sh X` |
| `run-id=X dest=Y` | `dashboard/scripts/pull-run.sh X Y` |
| `container=C ...` | `dashboard/scripts/pull-run.sh --container C ...` |

Run from the repo root (the script anchors paths to `$REPO_ROOT` regardless of CWD, but staying at root keeps log paths predictable). Surface the script's stdout/stderr to the user verbatim — it already prints the resolved run id, destination, and file count.

## Constraints

- Always invoke the script; do not call `az` directly from this command.
- Never print or ask for connection strings, SAS tokens, or account keys.
- Do not modify `dashboard/.env` or any other config.
- If the script fails, surface the error and stop — do not retry blindly.
