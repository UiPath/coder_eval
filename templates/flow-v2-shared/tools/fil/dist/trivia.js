"use strict";
// Trivia shared between the lexer (which captures comments) and the AST
// (which attaches them to declarations and statements). Kept in its own
// module so neither side has to depend on the other's internals.
Object.defineProperty(exports, "__esModule", { value: true });
exports.MAGIC_COMMENT_RE = void 0;
/** Matches `// <key>: <value>` line comments. The value runs to end of line, trimmed. */
exports.MAGIC_COMMENT_RE = /^\/\/\s*([a-z_][a-z0-9_]*)\s*:\s*(.+?)\s*$/;
//# sourceMappingURL=trivia.js.map