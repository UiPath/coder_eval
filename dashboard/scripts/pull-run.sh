#!/usr/bin/env bash
# Pull a run (or list runs) from Azure Blob Storage.
#
# Uses the same auth as dashboard/src/dashboard/blob.py (az --auth-mode login):
# whatever identity is active for `az` (local `az login` or VM managed identity).
# Storage account + container are read from dashboard/.env.
#
# Usage:
#   dashboard/scripts/pull-run.sh list
#   dashboard/scripts/pull-run.sh <run-id> [dest-dir]
#   dashboard/scripts/pull-run.sh --container <name> <run-id> [dest-dir]
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
  $0                               Download the latest run (default dest: runs/<run-id>, falls back to tmp/runs/<run-id> if it exists)
  $0 list                          List run ids in the container
  $0 <run-id> [dest-dir]           Download run (default dest: runs/<run-id>, falls back to tmp/runs/<run-id> if it exists)
  $0 --container <name> ...        Override container (default from .env, else 'runs')
EOF
}

CONTAINER_OVERRIDE=""
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

if [[ -z "$ACCOUNT" ]]; then
  echo "error: AZURE_STORAGE_ACCOUNT is not set in $ENV_FILE" >&2
  exit 1
fi

if ! az account show --query "user.name" -o tsv >/dev/null 2>&1; then
  echo "error: az is not authenticated. Run 'az login' (or ensure managed identity is active)." >&2
  exit 1
fi

# List blob names from the container, restricted to top-level prefixes that
# look like timestamp-style run ids (YYYY-MM-DD_HH-MM-SS). `--num-results '*'`
# disables pagination; without it `az` caps results and breaks both `list`
# (truncated) and latest-resolution (sorts only the first page).
list_run_ids() {
  az storage blob list \
    --container-name "$CONTAINER" \
    --account-name "$ACCOUNT" \
    --auth-mode login \
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

echo "Downloading $CONTAINER/$RUN_ID/* from $ACCOUNT → $DEST"
# `az storage blob download-batch` preserves the full blob name (e.g.
# `<RUN_ID>/foo.json`) under --destination, which would nest files at
# $DEST/$RUN_ID/... — duplicating the run id. Stage to a temp parent and
# move the inner $RUN_ID directory's contents into $DEST so files land
# directly under $DEST regardless of what the user passed.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
az storage blob download-batch \
  --source "$CONTAINER" \
  --destination "$STAGE" \
  --pattern "$RUN_ID/*" \
  --account-name "$ACCOUNT" \
  --auth-mode login

if [[ -d "$STAGE/$RUN_ID" ]]; then
  shopt -s dotglob nullglob
  mv "$STAGE/$RUN_ID"/* "$DEST/"
  shopt -u dotglob nullglob
fi

COUNT="$(find "$DEST" -type f | wc -l | tr -d ' ')"
echo "Done: $COUNT file(s) in $DEST"
