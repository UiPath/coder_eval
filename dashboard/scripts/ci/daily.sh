#!/usr/bin/env bash
# Daily skills-suite eval, invoked by the coder-eval-daily systemd timer.
# Pulls latest main, syncs deps, runs the dashboard pipeline under flock,
# pings Slack with the outcome.
#
# Auto-sources $REPO/.env and $REPO/dashboard/.env so all secrets/config land
# in the process env regardless of caller (interactive shell, systemd timer,
# manual smoke test). Required keys live in those two files:
#   .env:           AWS_BEARER_TOKEN_BEDROCK, AWS_REGION, BEDROCK_MODEL,
#                   LLMGW_*,
#                   UV_INDEX_UIPATH_PASSWORD (private uv index PAT for
#                     uipath-llmgw-client; also passed as the password build
#                     secret to `make docker-image` — username is optional,
#                     Azure Artifacts accepts the PAT alone),
#                   GH_NPM_REGISTRY_TOKEN (PAT with read:packages — installs uip from GitHub Packages @alpha),
#                   SLACK_WEBHOOK_URL (optional — empty disables ping;
#                     when set, slack_summary.py auto-suppresses on
#                     wrapper failure or low task count to avoid spamming
#                     the channel with broken/test-run results)
#   dashboard/.env: ADX_*, AZURE_STORAGE_ACCOUNT, AZURE_BLOB_CONTAINER,
#                   AZURE_STORAGE_KEY (when using --auth-mode key)
#
# Host prerequisites: a working Docker daemon. Skills tasks since
# skills#856 (2026-05-20) run inside the coder-eval-agent container,
# built fresh from $REPO/docker/Dockerfile on every run.
#
# Optional overrides (env, mostly for ad-hoc smoke tests):
#   TASK_PATTERN  glob/path of a single task yaml. When set, runs
#                 `coder-eval run <pattern>` directly and skips the
#                 dashboard wrapper (no upload/ingest, no analysis).
#   SUITE / MODEL / BACKEND   override the daily defaults.
#   BRANCH         coder_eval branch (default: main).
#   SKILLS_BRANCH  skills branch (default: main). Set both when
#                  dry-running a coder_eval + skills PR pair before merge.
set -euo pipefail

# Non-interactive shells (systemd, `ssh user@host '...'`) don't source ~/.bashrc,
# so user-local tool dirs aren't on PATH by default. Prepend them explicitly.
# ~/.npm-global/bin is where `npm install -g` lands (npm prefix is user-writable
# on this VM); ~/.local/bin holds uv etc.
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

REPO=/home/azureuser/uipath/coder_eval
SKILLS_REPO=/home/azureuser/uipath/skills
LOG_DIR=/home/azureuser/runs-ci
LOCK=/var/lock/uip-daily.lock
BACKEND="${BACKEND:-bedrock}"
SUITE="${SUITE:-skills}"
BRANCH="${BRANCH:-main}"
SKILLS_BRANCH="${SKILLS_BRANCH:-main}"
# MODEL is resolved AFTER sourcing .env so it can fall back to $BEDROCK_MODEL
# (the full Bedrock id, e.g. eu.anthropic.claude-sonnet-4-6) — the host
# CLI's short aliases don't resolve inside the coder-eval-agent container.

# Acquire the daily lock BEFORE pre-flight. Otherwise a second invocation's
# pkill would SIGTERM the legitimate in-flight run's children before flock
# detected the conflict. FD-based flock is held for the rest of the script.
exec 9>>"$LOCK"
flock -n 9 || exit 75

# ---- pre-flight cleanup (aggressive — every run starts as fresh as possible) ----
# Kill stray uip/eval processes from a prior crashed run (OOM, 4h SIGTERM,
# manual ctrl-c). Safe under the lock above — no live daily.sh to disturb.
pkill -f 'uip flow debug'                       2>/dev/null || true
pkill -f 'uv run coder-eval'                    2>/dev/null || true
pkill -f '.venv/bin/coder-eval'                 2>/dev/null || true
pkill -f '.venv/bin/dashboard'                  2>/dev/null || true
# claude_agent_sdk spawns a bundled `claude` binary as a subprocess of the
# orchestrator. When the orchestrator dies (pkill above, OOM, ctrl-c), this
# binary gets reparented to init and survives — observed running 23 min
# post-kill, still hitting Bedrock. Match the full path to avoid catching
# unrelated `claude` binaries on PATH.
pkill -f 'claude_agent_sdk/_bundled/claude'     2>/dev/null || true

# Sweep sandbox tempdirs. With procs killed above, anything left is orphan.
rm -rf /tmp/coder_eval_* 2>/dev/null || true

# Fail fast if the docker daemon isn't reachable — skills tasks run inside
# coder-eval-agent containers, so a broken daemon would surface as opaque
# per-task errors mid-suite.
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon unreachable (skills tasks require docker since skills#856)" >&2
  exit 1
fi

# Reap orphan coder-eval-agent containers from a prior crashed run. The
# daily.sh `pkill` above kills the dashboard process; child containers
# launched via `docker run` survive their parent and need explicit cleanup.
docker ps -aq --filter "ancestor=coder-eval-agent" 2>/dev/null \
  | xargs -r docker rm -f >/dev/null 2>&1 || true

# Wipe caches so every run does a fresh resolve. Adds ~30s to cold runs but
# eliminates stale-pin / cache-poisoning bugs (paired with the .venv recreate
# below for the Python side).
uv cache clean 2>/dev/null || true
rm -rf "$HOME/.npm/_cacache" 2>/dev/null || true
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

# Resolve MODEL after sourcing .env so the default falls back to $BEDROCK_MODEL
# (full Bedrock id). Caller-set MODEL still wins.
MODEL="${MODEL:-${BEDROCK_MODEL:-}}"
if [ -z "$MODEL" ]; then
  echo "ERROR: MODEL not set and BEDROCK_MODEL missing from .env" >&2
  exit 1
fi

# Skills tracks $SKILLS_BRANCH (default main). Hard reset (not --ff-only pull)
# so any local cruft / untracked conflicts / half-applied rebase from a prior
# session can't silently abort the wrapper.
(cd "$SKILLS_REPO" && git fetch --quiet origin && git checkout --quiet "$SKILLS_BRANCH" && git reset --hard --quiet "origin/$SKILLS_BRANCH")

# Install uip CLI from GitHub Packages @alpha — the cli's `ci.yml`
# publishes `-alpha.<date>.<run>` prereleases under the `alpha` dist-tag on
# every merge to `cli/main`, so this picks up the freshest CLI per run.
# Public npmjs @latest only moves on GitHub Releases (~weeks behind main).
# Mirrors skills/.github/workflows/smoke-skills.yml. The .npmrc lives in a
# tempdir so the auth token never appears on argv and is wiped each run; the
# tempdir also isolates the install from any future workspace-aware
# package.json at the repo root.
if [ -z "${GH_NPM_REGISTRY_TOKEN:-}" ]; then
  echo "ERROR: GH_NPM_REGISTRY_TOKEN not set (needed to install @uipath/cli@alpha from GitHub Packages)" >&2
  exit 1
fi
install_dir="$(mktemp -d)"
cat > "$install_dir/.npmrc" <<EOF
@uipath:registry=https://npm.pkg.github.com/
//npm.pkg.github.com/:_authToken=${GH_NPM_REGISTRY_TOKEN}
EOF
(cd "$install_dir" && npm install -g @uipath/cli@alpha)
rm -rf "$install_dir"

# Hard-fail if uip didn't land on PATH — without it, downstream eval tasks
# would silently produce garbage.
UIP_PATH="$(command -v uip 2>/dev/null || true)"
if [ -z "$UIP_PATH" ]; then
  echo "ERROR: uip not on PATH after install" >&2
  exit 1
fi
echo "→ uip $(uip --version 2>/dev/null || echo unknown) ($UIP_PATH)"

# Recreate .venv from scratch. `uv pip install -e` is incremental and never
# prunes packages dropped from pyproject.toml — wiping and rebuilding
# eliminates Python-side stale-dep drift between runs.
rm -rf .venv
uv venv --python 3.13
uv pip install --python .venv/bin/python -e ".[dev]" --quiet
uv pip install --python .venv/bin/python -e "./dashboard" --quiet

# Build the coder-eval-agent image. Mirrors skills/.github/workflows/smoke-skills.yml.
# UV_INDEX_UIPATH_PASSWORD is the only one we hard-require: the Azure Artifacts
# feed accepts the PAT as the password with an empty username, and the
# Dockerfile reads both `--secret`s via `cat ... || true` (see docker/Dockerfile).
if [ -z "${UV_INDEX_UIPATH_PASSWORD:-}" ]; then
  echo "ERROR: UV_INDEX_UIPATH_PASSWORD not set (needed for docker image build)" >&2
  exit 1
fi
make docker-image

# Build the skills extension image (skills PR #918). The skills `nightly.yaml`
# and `smoke.yaml` experiments reference `image: skills-image:latest` to get a
# container with `@uipath/cli` and `@uipath/admin-tool` (plus any other
# skill-specific tooling) pre-installed on top of coder-eval-agent. Without
# this build, those experiments fail at container start with "image not found".
docker build \
  --build-arg CODER_EVAL_IMAGE=coder-eval-agent:latest \
  --build-arg NPM_AUTH_TOKEN="$GH_NPM_REGISTRY_TOKEN" \
  -t skills-image:latest \
  -f "$SKILLS_REPO/tests/docker/Dockerfile" \
  "$SKILLS_REPO"

# The skills `nightly.yaml` experiment mounts `~/.uipath:/.uipath:rw` so
# the in-container uip CLI reuses the host's cached creds. ROPC login
# (see PR #276) already creates this dir on first `uip login`, but make
# it idempotent so a freshly imaged VM doesn't bind-mount a missing path.
mkdir -p "$HOME/.uipath"

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
# The FD 9 flock at script start serializes this block.
"${CMD[@]}" > "$LOG" 2>&1
RC=$?
set -e

# Slack ping. Suppression policy (RC != 0, partial runs, low task count)
# lives inside slack_summary.py — daily.sh just sends whatever payload it
# produces. Empty stdout = silent run.
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
