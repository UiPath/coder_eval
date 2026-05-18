#!/usr/bin/env node
/**
 * v2tov1 — reconstruct a v1 .flow from a v2 project.
 *
 * Usage:
 *   v2tov1 <input.fil> [--bindings <bindings.json>] [--library <dir>]
 *                      [--out <out.flow>]
 *
 * The manifest is always synthesized from the FIL's
 * `flow`/`action`/`trigger` declarations.
 *
 * Output: writes <out.flow> AND bindings.json (verbatim copy of the input
 * bindings) to the output directory — together they form the v1 artifact set.
 */
export {};
//# sourceMappingURL=cli.d.ts.map