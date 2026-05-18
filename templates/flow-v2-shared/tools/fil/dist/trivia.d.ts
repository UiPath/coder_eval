/**
 * A captured comment from the source. Comments are not tokens — they're
 * held in a parallel stream the parser pulls from at attachment points
 * (statement + declaration boundaries) and stores in
 * `BaseNode.leadingComments`.
 *
 * Magic comments (`// <key>: <value>`) carry metadata that survives the
 * round-trip through FIL ↔ v1 .flow (script-node names, descriptions,
 * etc.). Plain comments preserve developer intent but have no semantic
 * effect on the runtime.
 */
export interface Comment {
    /** Verbatim comment text, including the leading `//` or block-comment delimiters. */
    raw: string;
    kind: 'line' | 'block';
    /** 1-based source line of the opening `/`. */
    line: number;
    /** 1-based column. */
    col: number;
    /** Absolute byte offset in source, for ordering against tokens. */
    pos: number;
    /** True iff this is a `// <key>: <value>` line comment with a registered key shape. */
    isMagic: boolean;
    /** Parsed key, when `isMagic`. */
    key?: string;
    /** Parsed value, when `isMagic`. */
    value?: string;
}
/** Matches `// <key>: <value>` line comments. The value runs to end of line, trimmed. */
export declare const MAGIC_COMMENT_RE: RegExp;
//# sourceMappingURL=trivia.d.ts.map