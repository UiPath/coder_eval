#!/usr/bin/env bash
set -euo pipefail

exec /usr/local/bin/coder_eval_drop_privilege.sh /usr/local/bin/claude "$@"
