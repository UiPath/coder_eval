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

# Acquire the daily lock BEFORE pre-flight. Otherwise a second invocation's
# pkill would SIGTERM the legitimate in-flight run's children before flock
# detected the conflict. FD-based flock (held for the rest of the script)
# replaces the previous inner-only `flock -n -E 75 "$LOCK" "${CMD[@]}"`.
exec 9>>"$LOCK"
flock -n 9 || exit 75

# ---- pre-flight cleanup (aggressive — every run starts as fresh as possible) ----
# Kill stray uip/eval processes from a prior crashed run (OOM, 4h SIGTERM,
# manual ctrl-c). Safe under the lock above — no live daily.sh to disturb.
pkill -f 'uip flow debug'        2>/dev/null || true
pkill -f 'uv run coder-eval'     2>/dev/null || true
pkill -f '.venv/bin/coder-eval'  2>/dev/null || true
pkill -f '.venv/bin/dashboard'   2>/dev/null || true

# Sweep sandbox tempdirs. With procs killed above, anything left is orphan.
rm -rf /tmp/coder_eval_* 2>/dev/null || true

# Best-effort scrub of a global @uipath/cli install. Per CI_DESIGN: npm-global
# lands plugins under root-owned /usr/lib/node_modules/@uipath and the maestro
# auto-install EACCES-es. Our PATH puts ~/.bun/bin first so the bun build
# wins anyway, but we still try to clean up. Never fatal — warn and continue.
if [ -d /usr/lib/node_modules/@uipath ] || npm ls -g --depth=0 2>/dev/null | grep -q '@uipath/cli'; then
  echo "WARNING: global @uipath/cli install detected — attempting best-effort removal" >&2
  npm uninstall -g @uipath/cli            2>/dev/null || true
  sudo -n npm uninstall -g @uipath/cli    2>/dev/null || true
  sudo -n rm -rf /usr/lib/node_modules/@uipath 2>/dev/null || true
  if [ -d /usr/lib/node_modules/@uipath ] || npm ls -g --depth=0 2>/dev/null | grep -q '@uipath/cli'; then
    echo "WARNING: global @uipath/cli still present — Run: sudo rm -rf /usr/lib/node_modules/@uipath. Continuing." >&2
  fi
fi

# Wipe package caches + per-user globals so every run does a fresh resolve.
# Adds ~30-90s to cold runs but kills the whole class of stale-pin / cache-
# poisoning bugs (typescript@5.9.3 nested dirs in 2026-05 was the Node-side
# version of this; without venv recreate below, we still had the Python-side
# version uncovered). Bun-bin uip symlink dropped here, re-created by bun
# link in the dev:cli:install step below.
uv cache clean 2>/dev/null || true
rm -rf "$HOME/.bun/install/cache"  2>/dev/null || true
rm -rf "$HOME/.bun/install/global" 2>/dev/null || true
rm -f  "$HOME/.bun/bin/uip"        2>/dev/null || true
rm -rf "$HOME/.npm/_cacache"       2>/dev/null || true
# ---- end pre-flight ----

cd "$REPO"
git fetch --quiet origin
git checkout --quiet "$BRANCH"
git reset --hard --quiet "origin/$BRANCH"

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
# Hard reset (not --ff-only pull) so any local cruft / untracked conflicts /
# half-applied rebase from a prior session can't silently abort the wrapper.
(cd "$SKILLS_REPO" && git fetch --quiet origin && git checkout --quiet main && git reset --hard --quiet origin/main)

# CLI install: do a recursive node_modules clean before `bun install`.
# `bun install --silent` is incremental and won't prune stale per-package
# typescript copies left behind when upstream version pins shift; we got
# bitten by this in 2026-05 when an upstream `ignoreDeprecations` bump
# landed against stale nested typescript@5.9.3 dirs from an earlier
# install. The clean adds ~30s but eliminates the whole class of bug.
#
# cli has also flipped between `dev:install-cli` and `dev:cli:install`
# (governance rollout PR #1525 → revert #1883); tolerate either by trying both.
(cd "$CLI_REPO" && git fetch --quiet origin && git checkout --quiet main && git reset --hard --quiet origin/main \
                && find . -name node_modules -type d -prune -exec rm -rf {} + \
                && bun install --silent \
                && (bun run dev:install-cli || bun run dev:cli:install))

# Sanity: warn if PATH lookup of `uip` doesn't land in ~/.bun/bin. We only
# check the PATH-resolved location, not `readlink -f` — bun link symlinks
# ~/.bun/bin/uip into the cli source tree (cli/packages/cli/dist/index.js),
# so resolving the symlink chain is a false positive. Never fatal.
UIP_PATH="$(command -v uip 2>/dev/null || true)"
if [ -z "$UIP_PATH" ] || [ "$(dirname "$UIP_PATH")" != "$HOME/.bun/bin" ]; then
  echo "WARNING: uip resolves unexpectedly: ${UIP_PATH:-<not found>}. Continuing." >&2
fi

# Recreate .venv from scratch. `uv pip install -e` is incremental and never
# prunes packages dropped from pyproject.toml — wiping and rebuilding
# eliminates Python-side stale-dep drift between runs.
rm -rf .venv
uv venv --python 3.13
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
# We already hold the daily lock from the top of the script (FD 9) — no
# inner flock needed.
"${CMD[@]}" > "$LOG" 2>&1
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
