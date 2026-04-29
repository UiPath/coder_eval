#!/usr/bin/env bash
# Daily skills-suite eval, invoked by the coder-eval-daily systemd timer.
# Pulls latest main, syncs deps, runs the dashboard pipeline under flock,
# pings Slack with the outcome.
#
# Auto-sources $REPO/.env and $REPO/dashboard/.env so all secrets/config land
# in the process env regardless of caller (interactive shell, systemd timer,
# manual smoke test). Required keys live in those two files:
#   .env:           AWS_BEARER_TOKEN_BEDROCK, AWS_REGION, BEDROCK_MODEL,
#                   LLMGW_*, GH_NPM_REGISTRY_TOKEN,
#                   UV_INDEX_UIPATH_PASSWORD (private uv index for uipath-llmgw-client),
#                   SLACK_WEBHOOK_URL (optional — empty = silent runs)
#   dashboard/.env: ADX_*, AZURE_STORAGE_ACCOUNT, AZURE_BLOB_CONTAINER,
#                   AZURE_STORAGE_KEY (when using --auth-mode key)
#
# Optional overrides (env, mostly for ad-hoc smoke tests):
#   TASK_PATTERN  glob/path of a single task yaml. When set, runs
#                 `coder-eval run <pattern>` directly and skips the
#                 dashboard wrapper (no upload/ingest, no analysis).
#   SUITE / MODEL / BACKEND   override the daily defaults.
set -euo pipefail

# Non-interactive shells (systemd, `ssh user@host '...'`) don't source ~/.bashrc,
# so user-local tool dirs aren't on PATH by default. Prepend them explicitly.
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"

REPO=/home/azureuser/uipath/coder_eval
SKILLS_REPO=/home/azureuser/uipath/skills
CLI_REPO=/home/azureuser/uipath/cli
LOG_DIR=/home/azureuser/runs-ci
LOCK=/var/lock/uip-daily.lock
MODEL="${MODEL:-claude-sonnet-4-6}"
BACKEND="${BACKEND:-bedrock}"
SUITE="${SUITE:-skills}"
BRANCH="${BRANCH:-main}"

cd "$REPO"
git fetch --quiet
git checkout --quiet "$BRANCH"
git pull --ff-only --quiet

# Source repo-local .env files into our environment. Done after the pull so
# we pick up any new keys added upstream.
for env_file in "$REPO/.env" "$REPO/dashboard/.env"; do
  if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
  fi
done

# Skills + CLI always track main — only coder_eval honors $BRANCH (for ad-hoc smoke tests).
(cd "$SKILLS_REPO" && git fetch --quiet && git checkout --quiet main && git pull --ff-only --quiet)
(cd "$CLI_REPO"    && git fetch --quiet && git checkout --quiet main && git pull --ff-only --quiet \
                   && bun install --silent && bun run dev:install-cli)

[ -d .venv ] || uv venv --python 3.13
uv pip install --python .venv/bin/python -e ".[dev]" --quiet
uv pip install --python .venv/bin/python -e "./dashboard" --quiet

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ).log"

if [ -n "${TASK_PATTERN:-}" ]; then
  echo "→ single-task smoke mode: $TASK_PATTERN" >&2
  CMD=(.venv/bin/coder-eval run "$TASK_PATTERN" --model "$MODEL" --backend "$BACKEND" --verbose)
  SUITE_LABEL="task=$(basename "$TASK_PATTERN" .yaml)"
else
  CMD=(.venv/bin/dashboard run --skip-pull --skip-login --suite "$SUITE" --model "$MODEL" --backend "$BACKEND" --verbose)
  SUITE_LABEL="$SUITE"
fi

set +e
# `flock` parses options before the lockfile path; -E must come before $LOCK
# or it's treated as the start of the command and execve fails with ENOENT.
flock -n -E 75 "$LOCK" "${CMD[@]}" > "$LOG" 2>&1
RC=$?
set -e

if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  PAYLOAD=$(CODER_EVAL_REPO="$REPO" .venv/bin/python \
    dashboard/scripts/ci/slack_summary.py \
      --rc "$RC" --log "$LOG" \
      --suite "$SUITE_LABEL" --model "$MODEL" --backend "$BACKEND" 2>/dev/null) || PAYLOAD=""
  if [ -n "$PAYLOAD" ]; then
    curl -sS -X POST -H 'Content-type: application/json' \
      --data "$PAYLOAD" "$SLACK_WEBHOOK_URL" >/dev/null || true
  fi
fi

exit "$RC"
