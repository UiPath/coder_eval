#!/usr/bin/env sh
# Snapshot the generated Flow artifacts into _outputs/ for post-run capture.
# Copies every *.flow / *.fil / bindings*.json (excluding build/tooling dirs),
# preserving relative paths, then makes the copies read-only so a later phase
# can't clobber them. Invoked from each csharp_authoring task's `post_run`.
set -eu

mkdir -p _outputs
find . -type d \( \
    -name .uipath -o -name .claude -o -name .venv -o -name node_modules \
    -o -name tools -o -name example -o -name _outputs -o -name .git \
    -o -name .flow-run -o -name bin -o -name obj \
  \) -prune \
  -o -type f \( -name "*.flow" -o -name "*.fil" -o -name "bindings*.json" \) \
  -exec sh -c 'f="${1#./}"; mkdir -p "_outputs/$(dirname "$f")" && cp "$1" "_outputs/$f"' _ {} \;
find _outputs -type f -exec chmod a-w {} +
