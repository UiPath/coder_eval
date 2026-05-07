#!/usr/bin/env bash
# Pull a run (or list runs) from Azure Blob Storage.
#
# Uses the same auth as dashboard/src/dashboard/blob.py: when AZURE_STORAGE_KEY
# is set in dashboard/.env, uses `--auth-mode key`; otherwise falls back to
# `--auth-mode login` (whatever identity is active for `az` — local `az login`
# or the VM's managed identity).
# Storage account + container + (optional) key are read from dashboard/.env.
#
# By default, pulls only the high-signal files needed for triage / analysis,
# skipping the bulky per-task agent workspace artifacts (which routinely add
# multiple GB per run from `.venv` and similar). The default file set is:
#
#   - <run-id>/run.json, run.md
#   - <run-id>/analysis.md (if present)
#   - <run-id>/experiment.* (any extension at run root)
#   - <run-id>/<variant>/<task-id>/<replicate>/task.{json,html,log}
#   - <run-id>/<variant>/<task-id>/<replicate>/artifacts/**/*.flow
#     (Maestro flow definitions — small text files needed for flow-task triage)
#
# Use --full to pull everything (the prior behavior, including artifacts/).
#
# Usage:
#   dashboard/scripts/pull-run.sh list
#   dashboard/scripts/pull-run.sh <run-id> [dest-dir]
#   dashboard/scripts/pull-run.sh --container <name> <run-id> [dest-dir]
#   dashboard/scripts/pull-run.sh --full <run-id> [dest-dir]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$DASHBOARD_DIR")"
ENV_FILE="$DASHBOARD_DIR/.env"

# Anchor relative dest paths to repo root so behavior is independent of CWD.
cd "$REPO_ROOT"

usage() {
  cat <<EOF
Usage:
  $0                               Download the latest run, targeted file set (default dest: runs/<run-id>, falls back to tmp/runs/<run-id> if it exists)
  $0 list                          List run ids in the container
  $0 <run-id> [dest-dir]           Download run, targeted file set
  $0 --full <run-id> [dest-dir]    Download every blob under <run-id>/ (incl. per-task artifacts/ workspace)
  $0 --container <name> ...        Override container (default from .env, else 'runs')

Targeted file set (default — used by triage / analysis):
  <run-id>/{run.json,run.md,analysis.md}
  <run-id>/experiment.*
  <run-id>/<variant>/<task-id>/<replicate>/task.{json,html,log}
  <run-id>/<variant>/<task-id>/<replicate>/artifacts/**/*.flow
EOF
}

CONTAINER_OVERRIDE=""
FULL_PULL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
    --container)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      CONTAINER_OVERRIDE="$2"
      shift 2
      ;;
    --full)
      FULL_PULL=1
      shift
      ;;
    *)
      break
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: $ENV_FILE not found — populate it the same way the dashboard does." >&2
  exit 1
fi

# Extract key=value lines from .env: tolerate leading whitespace, inline `# comment`,
# trailing whitespace, and surrounding quotes. Mirrors what pydantic-settings reads
# in dashboard/src/dashboard/config.py for the same keys.
get_env() {
  local key="$1"
  sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*(.*)$/\1/p" "$ENV_FILE" \
    | tail -n1 \
    | sed -E "s/[[:space:]]+#.*$//; s/[[:space:]]*$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

ACCOUNT="$(get_env AZURE_STORAGE_ACCOUNT)"
CONTAINER_ENV="$(get_env AZURE_BLOB_CONTAINER)"
CONTAINER="${CONTAINER_OVERRIDE:-${CONTAINER_ENV:-runs}}"
ACCOUNT_KEY="$(get_env AZURE_STORAGE_KEY)"

if [[ -z "$ACCOUNT" ]]; then
  echo "error: AZURE_STORAGE_ACCOUNT is not set in $ENV_FILE" >&2
  exit 1
fi

# Build az auth flags once: prefer key when present, else fall back to login.
# When using key auth, export AZURE_STORAGE_KEY (the env var az auto-picks up)
# instead of passing --account-key on the command line, so the secret never
# appears in argv — visible via `ps` / `/proc/*/cmdline` to other users on the
# host. Targeted mode multiplies the exposure window (one az process per blob)
# vs the prior single download-batch invocation.
if [[ -n "$ACCOUNT_KEY" ]]; then
  export AZURE_STORAGE_KEY="$ACCOUNT_KEY"
  AUTH_ARGS=(--auth-mode key)
else
  AUTH_ARGS=(--auth-mode login)
  if ! az account show --query "user.name" -o tsv >/dev/null 2>&1; then
    echo "error: AZURE_STORAGE_KEY is unset and az is not authenticated." >&2
    echo "       Either set AZURE_STORAGE_KEY in $ENV_FILE or run 'az login'." >&2
    exit 1
  fi
fi

# List blob names from the container, restricted to top-level prefixes that
# look like timestamp-style run ids (YYYY-MM-DD_HH-MM-SS). `--num-results '*'`
# disables pagination; without it `az` caps results and breaks both `list`
# (truncated) and latest-resolution (sorts only the first page).
list_run_ids() {
  az storage blob list \
    --container-name "$CONTAINER" \
    --account-name "$ACCOUNT" \
    "${AUTH_ARGS[@]}" \
    --num-results '*' \
    --query "[].name" -o tsv 2>/dev/null \
    | awk -F/ 'NF>1 && $1 ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}$/ {print $1}' \
    | sort -u
}

if [[ "${1:-}" == "list" ]]; then
  list_run_ids
  exit 0
fi

if [[ $# -eq 0 ]]; then
  echo "No run-id given — resolving latest run id..."
  RUN_ID="$(list_run_ids | tail -n1)"
  if [[ -z "$RUN_ID" ]]; then
    echo "error: no runs found in container '$CONTAINER'" >&2
    exit 1
  fi
  echo "Latest run: $RUN_ID"
  DEST=""
else
  RUN_ID="$1"
  DEST="${2:-}"
fi

if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_.][A-Za-z0-9._-]*$ ]]; then
  echo "error: invalid run-id '$RUN_ID' (must start with alnum/./_ and contain only A-Z, a-z, 0-9, '.', '_', '-')" >&2
  exit 2
fi

if [[ -z "$DEST" ]]; then
  if [[ -e "runs/$RUN_ID" ]]; then
    echo "warning: runs/$RUN_ID already exists — falling back to tmp/runs/$RUN_ID" >&2
    DEST="tmp/runs/$RUN_ID"
  else
    DEST="runs/$RUN_ID"
  fi
fi
mkdir -p "$DEST"

# Stage everything to a temp parent so we can atomically move into $DEST
# at the end and avoid leaving partial pulls in the canonical runs/ tree.
# `az storage blob download[-batch]` preserves the full blob name (incl. the
# leading $RUN_ID/) under --destination, so we move the inner $RUN_ID directory
# contents into $DEST after download completes.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
if [[ "$FULL_PULL" -eq 1 ]]; then
  echo "Downloading $CONTAINER/$RUN_ID/* (FULL — includes per-task artifacts/) from $ACCOUNT → $DEST"
  az storage blob download-batch \
    --source "$CONTAINER" \
    --destination "$STAGE" \
    --pattern "$RUN_ID/*" \
    --account-name "$ACCOUNT" \
    "${AUTH_ARGS[@]}"
else
  echo "Listing blobs under $CONTAINER/$RUN_ID/ ..."
  ALL_BLOBS="$(az storage blob list \
    --container-name "$CONTAINER" \
    --account-name "$ACCOUNT" \
    "${AUTH_ARGS[@]}" \
    --num-results '*' \
    --prefix "$RUN_ID/" \
    --query "[].name" -o tsv 2>/dev/null)"

  # Filter: keep only the targeted file set. NF counts segments after `awk -F/`.
  # NF==2 = run-root file (run.json, run.md, analysis.md, experiment.*).
  # NF==5 = task file at <run>/<variant>/<task_id>/<replicate>/task.{json,html,log}.
  # NF>=7, $5=="artifacts", *.flow = Maestro flow under per-task artifacts/
  #   workspace. Depth varies (NF=8..13 observed) because the agent's project
  #   layout — <task-id>/[<solution>/]<project>/<name>.flow — isn't fixed, so
  #   we anchor on segment 5 and the .flow extension instead of a hard NF.
  # Contract: if the run-output layout changes (e.g., extra nesting level under
  # <run-id>/), all three guards must be updated or matching files will be
  # silently dropped. Anchored prefix-match on $1 keeps blobs from other run
  # ids from leaking in if a future caller widens the --prefix.
  WANTED="$(printf '%s\n' "$ALL_BLOBS" | awk -F/ -v r="$RUN_ID" '
    NF == 2 && $1 == r && \
      ($2 == "run.json" || $2 == "run.md" || $2 == "analysis.md" || $2 ~ /^experiment\./) { print; next }
    NF == 5 && $1 == r && $5 ~ /^task\.(json|html|log)$/ { print; next }
    NF >= 7 && $1 == r && $5 == "artifacts" && /\.flow$/ { print; next }
  ')"

  if [[ -z "$WANTED" ]]; then
    echo "error: no targeted files found under $CONTAINER/$RUN_ID/ — is the run id correct, or did the upload not finish?" >&2
    exit 1
  fi

  TOTAL=$(printf '%s\n' "$WANTED" | wc -l | tr -d ' ')
  echo "Downloading $TOTAL targeted file(s) (run/experiment metadata + task.{json,html,log} + artifacts/**/*.flow) → $DEST"
  echo "  (use --full to also pull the rest of the per-task artifacts/ workspace)"

  # Parallel per-blob download. Each `az storage blob download` invocation has
  # ~0.5-1s of Python startup; xargs -P 16 keeps overall wall-clock low.
  # AUTH_ARGS is forwarded as positional args after the {} placeholder so the
  # bash -c subprocess inherits the same auth mode (key vs login) chosen above.
  # AZURE_STORAGE_KEY (when set) is exported above and inherited by subprocesses
  # automatically, so the secret never appears in argv.
  #
  # Failures: each worker appends the failed blob name to FAIL_LOG and exits 1
  # so xargs's own exit code (123) signals "at least one input failed". Short
  # appends to a single file from parallel workers are atomic on POSIX as long
  # as each write is below PIPE_BUF (~4KB on Linux); blob names are well under
  # that. We `|| true` the pipeline so the script can read FAIL_LOG itself
  # rather than abort mid-summary, then exit non-zero with a precise count.
  FAIL_LOG="$STAGE/.failures"
  : > "$FAIL_LOG"
  export STAGE CONTAINER ACCOUNT FAIL_LOG
  printf '%s\n' "$WANTED" | xargs -P 16 -I{} bash -c '
    blob="$1"; shift
    out="$STAGE/$blob"
    mkdir -p "$(dirname "$out")"
    if ! az storage blob download \
        --container-name "$CONTAINER" \
        --account-name "$ACCOUNT" \
        "$@" \
        --name "$blob" \
        --file "$out" \
        --no-progress >/dev/null; then
      echo "$blob" >> "$FAIL_LOG"
      exit 1
    fi
  ' _ {} "${AUTH_ARGS[@]}" || true

  FAIL_COUNT="$(wc -l < "$FAIL_LOG" | tr -d ' ')"
  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "error: $FAIL_COUNT of $TOTAL blob download(s) failed — see az error output above" >&2
    exit 1
  fi
fi

if [[ -d "$STAGE/$RUN_ID" ]]; then
  shopt -s dotglob nullglob
  mv "$STAGE/$RUN_ID"/* "$DEST/"
  shopt -u dotglob nullglob
fi

COUNT="$(find "$DEST" -type f | wc -l | tr -d ' ')"
echo "Done: $COUNT file(s) in $DEST"
