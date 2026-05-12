#!/usr/bin/env bash
# verify.sh <workflow.json>
#
# Imports the workflow into a sandboxed n8n DB (under ./.n8n) to validate
# its structural shape. Exit code 0 on a clean import, non-zero with an
# error message otherwise.
#
# This does NOT execute the workflow — credentials aren't required.
set -euo pipefail

FILE="${1:?usage: verify.sh <workflow.json>}"
if [[ ! -f "$FILE" ]]; then
  echo "error: $FILE not found" >&2
  exit 2
fi

# Validate JSON first
python3 -c "import json; json.load(open('$FILE'))" || { echo "error: $FILE is not valid JSON" >&2; exit 3; }

# Fresh sandboxed n8n DB each run
SANDBOX="$(cd "$(dirname "$FILE")" && pwd)/.n8n"
rm -rf "$SANDBOX"

export N8N_USER_FOLDER="$SANDBOX"
OUT=$(n8n import:workflow --input="$FILE" 2>&1)
RC=$?

# Trim noise (migrations / deprecation warnings) and keep the result line
echo "$OUT" | grep -v -E "^(Starting migration|Finished migration|\[Migrate|Deprecation warning|Added workflow|No encryption key|Migrations in progress|No SSH keys|\[MoveSshKeysToDatabase|\[MigrateExternalSecretsToEntityStorage|\[CreateCredentialDependencyTable)" || true

exit $RC
