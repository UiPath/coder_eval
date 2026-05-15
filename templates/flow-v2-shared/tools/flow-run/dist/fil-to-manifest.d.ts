/**
 * Derive a flow-run `FlowManifest` from a parsed FIL `Program`'s top-level
 * `flow` / `action` / `trigger` declarations.
 *
 * This is the Phase 4 counterpart to v2-to-v1's `filProgramToManifest`:
 * after the manifest-removal work, flow-run no longer reads a
 * `<Name>.manifest.flow` sidecar. It parses the FIL source, walks
 * `program.actions` + `program.triggers`, and rebuilds the manifest shape
 * the dispatcher and preflight resolver already know how to consume.
 */
import * as AST from 'fil-compiler/dist/ast';
import type { FlowManifest } from './types';
export declare function filProgramToManifest(program: AST.Program): FlowManifest;
