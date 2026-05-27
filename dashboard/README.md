# coder-eval-dashboard

Full pipeline for running coder-eval tests and publishing results to Azure Blob Storage.

This is an independent package that lives alongside `coder-eval` in the same repo. It pulls repos, builds the UiPath CLI, runs coder-eval tests, and uploads results to Blob Storage — all in a single command.

## Setup

```bash
cd dashboard
uv sync
cp .env.example .env   # then fill in your values
```

### Required environment variables

| Variable | Description | Example |
|----------|-------------|---------|
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | `5db48574-8a20-418f-b488-1fafd8d021df` |
| `AZURE_STORAGE_ACCOUNT` | Storage account name | `coderevaltests` |
| `AZURE_BLOB_CONTAINER` | Blob container (default: `runs`) | `runs` |

To view current config values:

```bash
uv run dashboard config
```

### Optional environment variables (for `dashboard run`)

| Variable | Description | Default |
|----------|-------------|---------|
| `CLI_DIR` | Path to UiPath CLI repo checkout | Sibling of coder_eval repo |
| `UIP_AUTHORITY` | UiPath identity endpoint | _(required for flow suites)_ |
| `UIP_CLIENT_ID` | UiPath client ID | _(required for flow suites)_ |
| `UIP_CLIENT_SECRET` | UiPath client secret | _(required for flow suites)_ |
| `UIP_TENANT` | UiPath tenant | _(required for flow suites)_ |

### Authentication

All commands authenticate via `az login` (Azure CLI credential). Make sure you're logged in:

```bash
az login
```

The VM's managed identity is pre-configured with Storage Blob Data Contributor on the storage account.

## Commands

### Full pipeline (single command)

```bash
uv run dashboard run
```

This runs the complete pipeline:

1. **Pull & build UiPath CLI** — `git pull` + `bun install` + `bun run build`
2. **Pull coder_eval** — `git pull`
3. **Run test suites** — invokes `coder-eval run` for each suite
4. **Generate AI analysis** — invokes `/coder-eval-run-analysis` via Claude Code
5. **Upload to Blob Storage** — archives the full run directory

Options:

```bash
# Run only the skills suite
uv run dashboard run --suite skills

# Use a different model
uv run dashboard run --model claude-sonnet-4-6

# Skip slow steps during development
uv run dashboard run --skip-pull --skip-analysis

# Override tag filter (the `smoke` suite uses `smoke-pass` by default;
# pass `--tags smoke-fail` to exercise the negative-path sentinel instead.)
uv run dashboard run --tags smoke-pass

# Parallelize tasks within each suite (overrides the suite's built-in default)
uv run dashboard run --suite skills -j 8

# Pick an API backend for the agent
uv run dashboard run --suite skills --backend bedrock

# Use interactive UiPath auth (personal OAuth) instead of client-credentials from .env
uip login --interactive
uv run dashboard run --suite skills --skip-login
```

Available suites: `skills`, `smoke`.

Full flag list: `uv run dashboard run --help`.

### Individual commands

```bash
# Upload a run to Blob Storage
uv run dashboard upload ../runs/2026-03-23_17-41-23/
```

### Pull a run from Blob Storage

`dashboard/scripts/pull-run.sh` downloads run blobs back to a local directory. Auth and config come from `dashboard/.env` exactly like the upload path — no extra credentials needed.

By default it pulls only the high-signal files needed for triage / analysis (`run.json`, `run.md`, `analysis.md`, `experiment.*`, per-task `task.{json,html,log}`, and per-task `artifacts/**/*.flow` — Maestro flow definitions, ~1 KB each) — roughly ~37 MB / ~550 files vs the prior ~7+ GB / 8000+ files when the rest of the per-task `artifacts/` workspace is also pulled. Pass `--full` to opt back into the prior behavior when you need to inspect the agent's `.venv` / rendered artifacts.

```bash
dashboard/scripts/pull-run.sh                       # latest run, targeted set → runs/<run-id>
dashboard/scripts/pull-run.sh list                  # list run ids in the container
dashboard/scripts/pull-run.sh <run-id>              # specific run, targeted set → runs/<run-id>
dashboard/scripts/pull-run.sh <run-id> some/dir     # custom destination
dashboard/scripts/pull-run.sh --full <run-id>       # full pull (incl. per-task artifacts/ workspace)
dashboard/scripts/pull-run.sh --container <name> .. # override AZURE_BLOB_CONTAINER
```

If `runs/<run-id>` already exists locally, the script warns and writes to `tmp/runs/<run-id>` instead.

### Alternative: `python -m`

All commands also work via:

```bash
uv run python -m dashboard run
uv run python -m dashboard upload ../runs/2026-03-23_17-41-23/
```

## Tests

```bash
uv run pytest
```

## Project structure

```
dashboard/
├── pyproject.toml              # Package metadata and dependencies
├── .env.example                # Environment variable template
├── src/dashboard/
│   ├── cli.py                  # Click CLI: run, upload, config
│   ├── config.py               # Pydantic settings from env vars
│   ├── build.py                # UiPath CLI pull & build
│   ├── run.py                  # coder-eval test invocation
│   ├── analysis.py             # AI analysis generation via Claude Code
│   └── blob.py                 # Azure Blob upload (az CLI)
├── scripts/
│   └── pull-run.sh             # Download a run from Azure Blob Storage (az CLI)
└── tests/
```
