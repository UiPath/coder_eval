#!/usr/bin/env bash
# Build + deploy coder-evalboard to Azure App Service via OneDeploy (zip deploy).
# (App Service is still named `flow-evalboard` in Azure — see ../DEPLOYMENT.md.)
# Context / why this shape: see ../DEPLOYMENT.md.
#
# Usage:
#   scripts/deploy.sh                # build + deploy
#   scripts/deploy.sh --skip-build   # reuse existing /tmp/fe-deploy.zip
#   scripts/deploy.sh --build-only   # build the zip only, no deploy (used by CI)
#
# Pre-reqs on your workstation (manual deploys only):
#   - pnpm (matches pnpm-lock.yaml)
#   - npm (for the flat node_modules workaround)
#   - az CLI, logged in (`az login`) to the UiPath tenant
#   - A role that can deploy to the App Service (e.g. Website Contributor on
#     rg-coder-eval-tests; PIM-activate via DevTest-ML-EA-mgmt)
#
# CI uses --build-only and hands the resulting zip to `azure/webapps-deploy@v3`,
# which deploys via the App Service publish profile. See
# .github/workflows/deploy-evalboard.yml.

set -euo pipefail

SKIP_BUILD=0
BUILD_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    --build-only) BUILD_ONLY=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

APP=flow-evalboard
RG=rg-coder-eval-tests
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

  # Skip the local smoke test under --build-only. The smoke test boots Next.js
  # and hits /, which fetches from blob storage via DefaultAzureCredential —
  # that needs an interactive az login, which CI runners don't have. CI's next
  # step (azure/webapps-deploy@v3) is the real deploy gate.
  if [[ $BUILD_ONLY -eq 0 ]]; then
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
  fi

  echo "==> zipping $ZIP"
  rm -f "$ZIP"
  ( cd "$DEPLOY_DIR" && zip -qr "$ZIP" . )
fi

[[ -f "$ZIP" ]] || { echo "no zip at $ZIP (run without --skip-build)" >&2; exit 1; }

if [[ $BUILD_ONLY -eq 1 ]]; then
  echo "==> --build-only: zip ready at $ZIP, skipping deploy"
  exit 0
fi

echo "==> deploying $(du -h "$ZIP" | cut -f1) to $APP via OneDeploy"
az webapp deploy \
  --name "$APP" --resource-group "$RG" \
  --src-path "$ZIP" --type zip \
  --only-show-errors >/dev/null

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
