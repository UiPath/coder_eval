"use strict";
// Formats FIL compile errors with surrounding source context for human and
// agent consumption. Mimics the output of mainstream compilers (rustc-style):
// file:line:col, the offending source line, and a caret pointing at the column.
Object.defineProperty(exports, "__esModule", { value: true });
exports.formatCompileErrors = formatCompileErrors;
function formatCompileErrors(errors, source, sourcePath) {
    const lines = source.split('\n');
    const out = [];
    out.push(`flow-run: FIL compilation failed (${errors.length} error${errors.length === 1 ? '' : 's'}):`);
    out.push('');
    for (const err of errors) {
        const stage = err.stage.padEnd(11);
        if (err.line !== undefined) {
            out.push(`  ${sourcePath}:${err.line}${err.col !== undefined ? `:${err.col}` : ''}: [${err.stage}] ${err.message}`);
            const srcLine = lines[err.line - 1] ?? '';
            const lineNumWidth = String(err.line).length;
            out.push(`    ${pad(err.line, lineNumWidth)} | ${srcLine}`);
            if (err.col !== undefined) {
                const caret = ' '.repeat(err.col - 1) + '^';
                out.push(`    ${' '.repeat(lineNumWidth)} | ${caret}`);
            }
        }
        else {
            out.push(`  ${sourcePath}: [${err.stage}] ${err.message}`);
        }
        out.push('');
    }
    out.push(`Fix the error${errors.length === 1 ? '' : 's'} above in ${sourcePath} and re-run flow-run.`);
    out.push(`(Note: typecheck/await-lift/emit errors don't yet carry line numbers — ` +
        `they appear without a position. The message itself names the offending construct.)`);
    return out.join('\n');
}
function pad(n, w) {
    const s = String(n);
    return s.length >= w ? s : ' '.repeat(w - s.length) + s;
}
//# sourceMappingURL=compile-errors.js.map