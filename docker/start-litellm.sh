#!/usr/bin/env bash
#
# Start the LiteLLM proxy for coder_eval's `litellm` (open-weight) backend.
#
# Why this script exists: the proxy is a separate process that reads its Bedrock
# credential + master key from ITS OWN environment — it does NOT read coder_eval's
# .env. And coder_eval's .env uses bare `KEY=value` (no `export`), so `source .env`
# alone leaves those vars unexported → the proxy can't see them → HTTP 401
# "Unable to locate credentials". This script reads the needed values out of .env
# and exports them explicitly before launching, so that can't happen.
#
# Usage:
#   docker/start-litellm.sh                 # foreground; Ctrl-C to stop
# Overridable via env:
#   LITELLM_PORT (default 4000), LITELLM_CONFIG, ENV_FILE, LITELLM_MASTER_KEY
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
CONFIG="${LITELLM_CONFIG:-$REPO_ROOT/docker/litellm-config.yaml}"
PORT="${LITELLM_PORT:-4000}"

# Read a key from .env, returning ONLY the value: content between surrounding
# quotes if quoted (dropping any trailing `# comment`), else the bare value with
# an inline comment stripped. Pure bash — avoids BSD-sed backreference quirks and
# handles base64 values containing / + = safely.
read_env() {
  local line val
  line=$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1) || line=""
  [ -z "$line" ] && return 0
  val="${line#*=}"                              # strip leading `KEY=`
  val="${val#"${val%%[![:space:]]*}"}"          # trim leading whitespace
  if [[ "$val" == '"'* ]]; then                 # double-quoted → content between quotes
    val="${val#\"}"; val="${val%%\"*}"
  elif [[ "$val" == "'"* ]]; then               # single-quoted
    val="${val#\'}"; val="${val%%\'*}"
  else                                          # bare → drop inline comment + trailing ws
    val="${val%%#*}"
    val="${val%"${val##*[![:space:]]}"}"
  fi
  printf '%s' "$val"
}

# --- credentials the proxy needs (already-exported values win over .env) ---
export AWS_BEARER_TOKEN_BEDROCK="${AWS_BEARER_TOKEN_BEDROCK:-$(read_env AWS_BEARER_TOKEN_BEDROCK)}"
export AWS_REGION="${AWS_REGION:-$(read_env AWS_REGION)}"
export AWS_REGION="${AWS_REGION:-eu-north-1}"
# OpenRouter key for the openrouter/* models in the config (cost-optimization path).
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-$(read_env OPENROUTER_API_KEY)}"
# Master key = the key clients present. Default to .env's LITELLM_AUTH_TOKEN so the
# client and proxy match; fall back to the local dev key.
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-$(read_env LITELLM_AUTH_TOKEN)}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-spike-local}"

# --- preflight: fail loud instead of a runtime 401 ---
[ -f "$CONFIG" ] || { echo "ERROR: config not found: $CONFIG" >&2; exit 1; }
if [ -z "$AWS_BEARER_TOKEN_BEDROCK" ]; then
  echo "ERROR: AWS_BEARER_TOKEN_BEDROCK is empty — not in $ENV_FILE and not exported." >&2
  echo "       Set it in .env or 'export AWS_BEARER_TOKEN_BEDROCK=...' before running." >&2
  exit 1
fi
echo "config     : $CONFIG"
echo "region     : $AWS_REGION"
echo "bedrock tok: set (${#AWS_BEARER_TOKEN_BEDROCK} chars)"
echo "master key : $LITELLM_MASTER_KEY"

# --- stop any stale proxy on the port (the classic 'creds-less running proxy') ---
existing=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$existing" ]; then
  echo "stopping existing listener on :$PORT (pids: $existing)"
  # shellcheck disable=SC2086
  kill $existing 2>/dev/null || true
  sleep 1
fi

cat <<EOF

Proxy starting on http://127.0.0.1:$PORT  (Ctrl-C to stop)
Set these in coder_eval's .env (or shell) to use it:
  API_BACKEND=litellm
  LITELLM_BASE_URL=http://localhost:$PORT
  LITELLM_AUTH_TOKEN=$LITELLM_MASTER_KEY
  # then run:  coder-eval run <task> --model zai.glm-5   (or deepseek.v3.2 / moonshotai.kimi-k2.5)

EOF

exec uvx --from 'litellm[proxy]' litellm --config "$CONFIG" --host 127.0.0.1 --port "$PORT"
