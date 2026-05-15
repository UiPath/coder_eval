/**
 * Derive a `ManifestFile` from a FIL `Program`'s top-level `flow`/`action`/
 * `trigger` declarations.
 *
 * Phase 2 of the manifest-removal work: v2 projects can drop the
 * `<Name>.manifest.flow` sidecar entirely and let `v2-to-v1` synthesize
 * the equivalent manifest from FIL itself. The synthesizer reads:
 *
 *   - `flow <id> { name, version, … };`  → ManifestFile header
 *   - `action <name>: <type> { … };`     → ManifestFile.nodes[<id>]
 *   - `trigger <name>: <type> { … };`    → ManifestFile.nodes[<id>]
 *
 * Short type aliases (`http`, `script`, `start`, …) are resolved to the
 * canonical v1 type strings; fully-qualified types pass through unchanged.
 * The result feeds into the existing `expandManifest` → `filToFlow` pipeline
 * with no other changes.
 */
import * as AST from 'fil-compiler/dist/ast';
import type { ManifestFile } from 'v1-to-v2';
export declare function filProgramToManifest(program: AST.Program): ManifestFile | null;
//# sourceMappingURL=fil-to-manifest.d.ts.map