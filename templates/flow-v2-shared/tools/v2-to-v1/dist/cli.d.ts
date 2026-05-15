#!/usr/bin/env node
/**
 * v2tov1 — reconstruct a v1 .flow from a v2 project.
 *
 * Usage:
 *   v2tov1 <input> [--fil <name.fil>] [--manifest <name.manifest.flow>]
 *                  [--bindings <bindings.json>] [--library <dir>]
 *                  [--out <out.flow>]
 *
 * <input> can be either:
 *   - a .fil file              → preferred; manifest is synthesized from the
 *                                FIL's `flow`/`action`/`trigger` declarations
 *   - a .manifest.flow file    → legacy; the manifest is read explicitly and
 *                                the .fil is looked up alongside it
 *
 * If both `--manifest` and a `.fil` are present, the manifest is preferred
 * (acts as an override during incremental migration).
 *
 * Output: writes <out.flow> AND bindings.json (verbatim copy of the input
 * bindings) to the output directory — together they form the v1 artifact set.
 */
export {};
//# sourceMappingURL=cli.d.ts.map