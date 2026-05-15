/**
 * Phase A wire-up — populate v1 `inputs.detail.configuration` from a FIL
 * program's `executeNode("nodeId", {…})` argument expressions.
 *
 * Intent: an agent-authored FIL flow can write
 *
 *   await executeNode("sendEmail", { to: vars.user.email, subject: "Hi" });
 *
 * without ever touching the v2 manifest. The expansion in `manifest.ts`
 * still rebuilds the connector's scaffolding (Tier A defaults, library-
 * derived statics, fieldsContainer cache, …); this module folds the FIL
 * argument's per-instance field values into that scaffolding using the
 * tier-A v1 expression classifier.
 *
 * Walking strategy: we re-scan the FIL AST tracking a Scope (FIL name → v1
 * path) at each call site. The scope is built the same way `filToFlow`
 * builds it for the round-trip's nodeVar bindings — function params bind
 * to `vars.<name>`, every variable declaration adds another `vars.<name>`,
 * for-of iterators add `iterator.<name>` in their child scope. We do this
 * walk independently rather than threading scope out of `filToFlow` so
 * each executeNode call gets the *exact* scope visible at its location.
 *
 * Storage:
 *   - If the connector configuration's `fieldsContainer.inputFields` has
 *     an entry whose `id`/`name` matches the FIL key, set its `value`.
 *   - Otherwise stash the value in `optionalConfiguration[<key>]` (a
 *     generic, validate-tolerant catch-all).
 *
 * Values that don't classify (await, arbitrary calls, etc.) are recorded
 * on the `skipped` list so the caller can surface them as conversion
 * errors. We never throw from this pass — partial wire-up is still a
 * better v1 file than nothing.
 */
import * as AST from 'fil-compiler/dist/ast';
import type { FlowFile } from 'v1-to-v2';
export interface ApplyResult {
    /** Number of (node, field) pairs successfully resolved. */
    applied: number;
    /** Field references the classifier rejected. Caller may decide to fail. */
    skipped: Array<{
        nodeId: string;
        field: string;
        reason: string;
    }>;
    /** Node ids referenced by FIL that don't exist in the flow. */
    unknownNodeIds: string[];
}
/**
 * Walk every `await executeNode("nodeId", <argExpr>)` in `main()` (and any
 * subflow function), classify the input expression's properties, and merge
 * the resolved v1 expressions into the corresponding flow node's
 * `inputs.detail.configuration`.
 *
 * Mutates `flow` in place. Returns a diagnostic record.
 */
export declare function applyFilInputExpressions(flow: FlowFile, program: AST.Program): ApplyResult;
//# sourceMappingURL=apply-input-expressions.d.ts.map