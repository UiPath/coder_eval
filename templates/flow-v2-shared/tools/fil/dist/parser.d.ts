import * as AST from './ast';
export declare class Parser {
    private lexer;
    private inAsync;
    constructor(source: string);
    parse(): AST.Program;
    /**
     * Drain comments preceding the next token from the lexer's trivia stream.
     * Called by the attachment points (top-level dispatch, parseStatement) to
     * collect comments that belong to the node about to be parsed. Returns
     * `undefined` when there's nothing to attach so we don't add empty arrays
     * to every AST node.
     */
    private takeLeadingComments;
    /** Consumes `flow <kebab-id> { … };`. The `flow` keyword has already been peeked. */
    private parseFlowDeclaration;
    /** Consumes `action <name>: <typeRef> { … }?;`. The `action` keyword has already been peeked. */
    private parseActionDeclaration;
    /** Consumes `trigger <name>: <typeRef> { … }?;`. */
    private parseTriggerDeclaration;
    /**
     * Reads a kebab-style identifier: the head is an alphanumeric Identifier
     * token; subsequent `- <segment>` parts accept either an Identifier or a
     * Number (real-world flow ids like `workflow-1773896685530` need this).
     * Used for the flow id slot, which is the v1 flowId and may contain dashes.
     */
    private consumeKebabIdentifier;
    /**
     * Reads a node type-ref: dotted segments (each may contain dashes) plus
     * an optional `@<version>` suffix. Examples:
     *   `http`
     *   `uipath.connector.uipath-atlassian-jira.create-issue`
     *   `uipath.connector.x@1.0.0`
     * Stops at `{` or `;` (the action body / declaration terminator).
     */
    private parseTypeRef;
    /**
     * One segment of a type-ref. The lexer atomises mixed alphanumeric runs
     * into adjacent Number + Identifier tokens (e.g. `4989d2b6` → Number
     * "4989", Identifier "d2b6"; UUID-style segments need this), and `-`
     * separates further chunks. Concatenate everything until we hit a `.`,
     * `@`, `{`, `;`, or any non-alphanumeric / non-dash token.
     */
    private consumeTypeRefSegment;
    /**
     * A version after `@` is a `Number` (the lexer's number rule greedily
     * consumes one trailing `.<digits>`, so `1.0.0` tokenises as `1.0` and then
     * `.0`). Stitch consecutive Number tokens that start with `.` onto the
     * head. Also accepts a single Identifier (e.g. `latest`).
     */
    private consumeVersionString;
    private parseType;
    private parseFunctionDeclaration;
    private parseStatement;
    private parseStatementInner;
    private parseBlockStatement;
    private parseVariableDeclaration;
    private parseReturnStatement;
    private parseThrowStatement;
    private parseIfStatement;
    private parseWhileStatement;
    private parseForStatement;
    private parseSwitchStatement;
    private parseTryStatement;
    private parseBreakStatement;
    private parseContinueStatement;
    private parseExpressionStatement;
    private parseExpression;
    private parseAssignment;
    private parseConditional;
    private parseNullishCoalescing;
    private parseLogicalOr;
    private parseLogicalAnd;
    private parseBitwiseOr;
    private parseBitwiseXor;
    private parseBitwiseAnd;
    private parseEquality;
    private parseRelational;
    private parseShift;
    private parseAdditive;
    private parseMultiplicative;
    private parseExponential;
    private parseUnary;
    private parsePostfix;
    private parseCallMember;
    private parseArgumentList;
    private parsePrimary;
    private parseTemplateLiteral;
    private parseArrayExpression;
    private parseObjectExpression;
}
//# sourceMappingURL=parser.d.ts.map