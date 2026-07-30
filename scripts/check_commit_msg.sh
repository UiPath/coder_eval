#!/usr/bin/env bash
# Enforces Conventional Commits locally, mirroring .github/workflows/conventional-commits.yml.
set -euo pipefail

MSG_FILE="$1"
MSG="$(head -n1 "$MSG_FILE")"
PATTERN='^(feat|fix|refactor|docs|style|test|chore|perf|ci|build|revert)(\([a-z0-9._/-]+\))?(!)?: .+'

if echo "$MSG" | grep -Eq '^Merge (pull request|branch|remote-tracking)'; then
  exit 0
fi

if ! echo "$MSG" | grep -Eq "$PATTERN"; then
  echo "ERROR: commit message does not follow Conventional Commits."
  echo "  Got:      $MSG"
  echo "  Expected: <type>[optional scope]: <description>"
  echo "  Types:    feat | fix | refactor | docs | style | test | chore | perf | ci | build | revert"
  echo "  Examples: feat(sandbox): add preservation mode"
  echo "            fix: handle empty dataset gracefully"
  exit 1
fi
