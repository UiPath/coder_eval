#!/usr/bin/env bash
# Build + deploy coder-evalboard to Azure App Service via Run From Package.
# (App Service is still named `flow-evalboard` in Azure — see ../DEPLOYMENT.md.)
# Context / why this shape: see ../DEPLOYMENT.md.
#
# Usage:
#   scripts/deploy.sh           # build + upload + restart
#   scripts/deploy.sh --skip-build   # reuse existing /tmp/fe-deploy
#
# Pre-reqs on your workstation:
#   - pnpm (matches pnpm-lock.yaml)
#   - npm (for the flat node_modules workaround)
#   - az CLI, logged in (`az login`) to the UiPath tenant
#   - A PIM-activated role that can read storage keys on rg-coder-eval-tests
#     (mgmt-plane Contributor inherited from DevTest-ML-EA-mgmt is enough)

set -euo pipefail

SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

APP=flow-evalboard
RG=rg-coder-eval-tests
STORAGE=coderevaltests
CONTAINER=runs
BLOB=deploys/flow-evalboard.zip
DEPLOY_DIR=/tmp/fe-deploy
ZIP=/tmp/fe-deploy.zip

# Resolve the evalboard repo dir (parent of this script's directory).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ $SKIP_BUILD -eq 0 ]]; then
  echo "==> pnpm build"
  pnpm build

  echo "==> assembling $DEPLOY_DIR"
  rm -rf "$DEPLOY_DIR"
  mkdir -p "$DEPLOY_DIR"
  cp -R .next/standalone/. "$DEPLOY_DIR/"
  cp -R .next/static "$DEPLOY_DIR/.next/"
  cp -R public "$DEPLOY_DIR/"

  # Next.js standalone + pnpm symlink layout breaks its deep module resolver
  # ("Cannot find module 'styled-jsx/package.json'"). Swap in a flat npm-installed
  # node_modules for the deploy.
  echo "==> npm install --omit=dev (flat node_modules for Next.js standalone)"
  rm -rf "$DEPLOY_DIR/node_modules"
  cp package.json "$DEPLOY_DIR/package.json"
  ( cd "$DEPLOY_DIR" && npm install --omit=dev --no-audit --no-fund >/dev/null )

  echo "==> local smoke test (node server.js on :8765)"
  (
    cd "$DEPLOY_DIR"
    PORT=8765 node server.js &
    SERVER_PID=$!
    trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
    # Wait for it to come up
    for _ in $(seq 1 20); do
      sleep 0.5
      if curl -s -o /dev/null -w "%{http_code}" --max-time 2 http://localhost:8765/ 2>/dev/null | grep -q "^200$"; then
        echo "    local HTTP 200 ✓"
        exit 0
      fi
    done
    echo "    local server did not return 200 — aborting deploy" >&2
    exit 1
  )

  echo "==> zipping $ZIP"
  rm -f "$ZIP"
  ( cd "$DEPLOY_DIR" && zip -qr "$ZIP" . )
fi

[[ -f "$ZIP" ]] || { echo "no zip at $ZIP (run without --skip-build)" >&2; exit 1; }

echo "==> uploading $(du -h "$ZIP" | cut -f1) to blob $CONTAINER/$BLOB"
KEY=$(az storage account keys list --account-name "$STORAGE" --resource-group "$RG" --query "[0].value" -o tsv)
az storage blob upload \
  --account-name "$STORAGE" --account-key "$KEY" \
  --container-name "$CONTAINER" --name "$BLOB" \
  --file "$ZIP" --overwrite --only-show-errors >/dev/null

echo "==> restarting $APP (container re-pulls the zip)"
az webapp restart --name "$APP" --resource-group "$RG"

# `az webapp restart` returns once the restart is queued — the old container is
# still serving requests for another ~30-60s while the new one pulls the zip
# and starts. Without this sleep the poll below can get a 200 from the stale
# container and exit claiming success on unchanged code. Proper fix is a
# build-version marker + polling endpoint; this sleep is the bandaid.
echo "==> sleeping 25s so the old container steps down before we poll"
sleep 25

echo "==> waiting for HTTP 200 (container healthy)"
URL="https://coder-evalboard.uipath-dev.com/"
for i in $(seq 1 24); do
  code=$(curl -sI -o /dev/null -w "%{http_code}" --max-time 8 -H "User-Agent: Mozilla/5.0" -H "Accept: text/html" "$URL" 2>/dev/null || true)
  if [[ "$code" == "302" || "$code" == "200" ]]; then
    echo "    $URL → HTTP $code ✓"
    exit 0
  fi
  echo "    attempt $i: HTTP $code (retrying in 15s)"
  sleep 15
done
echo "site did not come up within ~6 min — check logs: az webapp log tail --name $APP --resource-group $RG" >&2
exit 1
