/**
 * Derive a flow-run `FlowManifest` from a parsed FIL `Program`'s top-level
 * `flow` / `action` / `trigger` declarations.
 *
 * Mirrors v2-to-v1's `filProgramToManifest`: walks `program.actions` +
 * `program.triggers` and rebuilds the manifest shape the dispatcher and
 * preflight resolver consume.
 */
import * as AST from 'fil-compiler/dist/ast';
import type { FlowManifest } from './types';
export declare function filProgramToManifest(program: AST.Program): FlowManifest;
