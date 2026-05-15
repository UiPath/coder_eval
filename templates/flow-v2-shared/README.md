# flow-v2-shared

Shared scripts and vendored tools for the three flow-v2 task templates
(`flow-v2`, `flow-v2-library`, `flow-v2-process-resources`). All three
templates stack this directory as a second `template_dir` source so the
build / check / runtime helpers land in the sandbox.

## Layout

| File | Role |
| --- | --- |
| `flow-v2-env.sh` | Sourced helper; sets `FLOW_V2_LIBRARY_*` cache paths and `FLOW_V2_FIL` / `FLOW_V2_FLOW_RUN` / `FLOW_V2_V2_TO_V1` tool paths. |
| `setup-library.sh` | One-shot build of both library caches. Idempotent; `--force` rebuilds. |
| `check-library.sh` | Guard invoked by `verify.sh` / `convert.sh`; fails with a pointer at the setup task when the library cache is missing. |
| `check-tools.sh` | Guard that confirms `tools/` was synced; fails with a pointer at `sync-tools.sh` when the vendored binaries are missing. |
| `sync-tools.sh` | Re-vendors the flow-v2 toolchain from a local flow-v2 working copy. **Coding agents working on flow-v2 must run this** after building changes to the compiler / converter / runner so coder_eval picks them up. |
| `tools/` | Vendored flow-v2 toolchain (`fil/`, `v1-to-v2/`, `v2-to-v1/`, `flow-run/` + `node_modules/` with `wabt`). Produced by `sync-tools.sh`. |
| `generate_connectors.py` | Calls `uip flow registry` and `uip is resources/connectors` to produce the JSON library. Vendored from `flow-v2/integrations/generate_connectors.py`. |
| `convert_library_to_md.py` | Derives the markdown library from the JSON library. |

## Library cache layout

```
$FLOW_V2_LIBRARY_CACHE_DIR/           default: ~/.cache/coder-eval/flow-v2
├── library-json/                     consumed by flow-run --library and v1↔v2 tools
│   ├── index.json
│   └── <connector-key>/<op>@<ver>.json + .v1def.json
└── library-md/                       consumed by the coding agent in flow-v2-library
    ├── index.json
    └── <connector-key>/<op>@<ver>.md
```

## One-time setup (library cache)

```bash
bash <flow-v2-shared>/setup-library.sh          # checks then builds
bash <flow-v2-shared>/setup-library.sh --force  # rebuild from scratch
```

The build calls `uip flow registry get` ~1200 times so it's slow on first
run; subsequent runs are no-ops unless `--force` is passed. Tasks call
`check-library.sh` at the top of `verify.sh` / `convert.sh` and fail fast
with a pointer at the setup task if the cache is missing.

## One-time setup (tool deps)

`tools/<pkg>/dist/`, `tools/package.json`, and `tools/package-lock.json`
are committed, but `tools/node_modules/` is gitignored (no point churning
binary deps in git history). After a fresh clone, install the deps once:

```bash
( cd <coder_eval>/templates/flow-v2-shared/tools && npm ci --omit=dev )
```

`check-tools.sh` prints the exact command when it notices the install
is missing, so this is also fine to do lazily on first failure.

## Re-vendoring the toolchain

When you change anything in `fil/`, `v1-to-v2/`, `v2-to-v1/`, or `flow-run/`
in the flow-v2 repo, re-run the sync from your flow-v2 working copy so the
coder_eval templates pick up the change:

```bash
# Default: reads from $FLOW_V2 (or $HOME/src/flow-v2)
bash <coder_eval>/templates/flow-v2-shared/sync-tools.sh

# Explicit source:
bash <coder_eval>/templates/flow-v2-shared/sync-tools.sh --source ~/work/flow-v2

# Skip the upstream build step (when you've already `npm run build`-ed):
bash <coder_eval>/templates/flow-v2-shared/sync-tools.sh --skip-build
```

The script copies each package's `dist/` and stripped `package.json` (no
devDependencies, no scripts) into `tools/<pkg>/`, writes a workspaces
wrapper at `tools/package.json`, and runs `npm install --omit=dev` to
symlink workspace packages to each other and pull `wabt`. Total footprint
~7 MB.

**Coding agents working on flow-v2 packages:** treat `sync-tools.sh` as
the final step of your change. A green coder_eval task that runs against
stale vendored binaries is not a green test of your fix.
