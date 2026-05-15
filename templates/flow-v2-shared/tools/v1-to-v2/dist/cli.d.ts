#!/usr/bin/env node
/**
 * v1tov2 — CLI wrapper around convertV1ToV2.
 *
 * Usage:
 *   v1tov2 <input.flow> [--out-dir DIR] [--library DIR] [--write-manifest]
 *
 * Writes by default:
 *   <out-dir>/<basename>.fil           (self-contained — embeds the
 *                                       per-node data that used to live in
 *                                       the manifest sidecar)
 *   <out-dir>/bindings.json
 *
 * The `--write-manifest` flag additionally emits the legacy
 * `<basename>.manifest.flow` sidecar. Kept for incremental migration; new
 * v2 projects should not need it (`v2-to-v1` derives the equivalent
 * manifest from the FIL declarations).
 */
export {};
//# sourceMappingURL=cli.d.ts.map