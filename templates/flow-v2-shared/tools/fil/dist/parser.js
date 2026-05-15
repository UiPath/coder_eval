"use strict";
// Parser for the FIL compiler - builds AST from tokens
Object.defineProperty(exports, "__esModule", { value: true });
exports.Parser = void 0;
const lexer_1 = require("./lexer");
class Parser {
    constructor(source) {
        this.inAsync = false;
        this.lexer = new lexer_1.Lexer(source);
    }
    parse() {
        let flow;
        const actions = [];
        const triggers = [];
        const functions = [];
        const seenNodeNames = new Set();
        while (!this.lexer.check(lexer_1.TokenKind.EOF)) {
            // Skip semicolons at top level
            if (this.lexer.match(lexer_1.TokenKind.Semicolon))
                continue;
            const tok = this.lexer.current();
            if (tok.kind === lexer_1.TokenKind.Identifier && tok.value === 'flow') {
                if (flow) {
                    throw new Error(`Duplicate \`flow\` declaration at line ${tok.line}:${tok.col}`);
                }
                flow = this.parseFlowDeclaration();
            }
            else if (tok.kind === lexer_1.TokenKind.Identifier && tok.value === 'action') {
                const decl = this.parseActionDeclaration();
                if (seenNodeNames.has(decl.name)) {
                    throw new Error(`Duplicate node name "${decl.name}" at line ${tok.line}:${tok.col}`);
                }
                seenNodeNames.add(decl.name);
                actions.push(decl);
            }
            else if (tok.kind === lexer_1.TokenKind.Identifier && tok.value === 'trigger') {
                const decl = this.parseTriggerDeclaration();
                if (seenNodeNames.has(decl.name)) {
                    throw new Error(`Duplicate node name "${decl.name}" at line ${tok.line}:${tok.col}`);
                }
                seenNodeNames.add(decl.name);
                triggers.push(decl);
            }
            else {
                functions.push(this.parseFunctionDeclaration());
            }
        }
        if (!flow) {
            throw new Error('`flow <id> { name: "…", version: "…" };` declaration is required at the top of every FIL program');
        }
        // Required fields on the flow declaration: name + version.
        const fieldNames = new Set(flow.fields.properties.map((p) => p.key));
        if (!fieldNames.has('name')) {
            throw new Error('`flow` declaration is missing required field `name`');
        }
        if (!fieldNames.has('version')) {
            throw new Error('`flow` declaration is missing required field `version`');
        }
        return { kind: 'Program', flow, actions, triggers, functions };
    }
    // ─── Top-level declarations (flow / action / trigger) ─────────────────────
    /** Consumes `flow <kebab-id> { … };`. The `flow` keyword has already been peeked. */
    parseFlowDeclaration() {
        this.lexer.consume(); // consume the `flow` identifier
        const id = this.consumeKebabIdentifier('flow id');
        const fields = this.parseObjectExpression();
        this.lexer.match(lexer_1.TokenKind.Semicolon); // optional trailing `;`
        return { kind: 'FlowDeclaration', id, fields };
    }
    /** Consumes `action <name>: <typeRef> { … }?;`. The `action` keyword has already been peeked. */
    parseActionDeclaration() {
        this.lexer.consume(); // `action`
        const nameTok = this.lexer.expect(lexer_1.TokenKind.Identifier);
        this.lexer.expect(lexer_1.TokenKind.Colon);
        const typeRef = this.parseTypeRef();
        let fields;
        if (this.lexer.check(lexer_1.TokenKind.LBrace)) {
            fields = this.parseObjectExpression();
        }
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'ActionDeclaration', name: nameTok.value, typeRef, fields };
    }
    /** Consumes `trigger <name>: <typeRef> { … }?;`. */
    parseTriggerDeclaration() {
        this.lexer.consume(); // `trigger`
        const nameTok = this.lexer.expect(lexer_1.TokenKind.Identifier);
        this.lexer.expect(lexer_1.TokenKind.Colon);
        const typeRef = this.parseTypeRef();
        let fields;
        if (this.lexer.check(lexer_1.TokenKind.LBrace)) {
            fields = this.parseObjectExpression();
        }
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'TriggerDeclaration', name: nameTok.value, typeRef, fields };
    }
    /**
     * Reads a kebab-style identifier: the head is an alphanumeric Identifier
     * token; subsequent `- <segment>` parts accept either an Identifier or a
     * Number (real-world flow ids like `workflow-1773896685530` need this).
     * Used for the flow id slot, which is the v1 flowId and may contain dashes.
     */
    consumeKebabIdentifier(role) {
        const first = this.lexer.current();
        if (first.kind !== lexer_1.TokenKind.Identifier) {
            throw new Error(`Expected ${role} identifier at line ${first.line}:${first.col}, got ${first.kind} "${first.value}"`);
        }
        let name = first.value;
        this.lexer.consume();
        while (this.lexer.check(lexer_1.TokenKind.Minus)) {
            this.lexer.consume();
            const next = this.lexer.current();
            if (next.kind !== lexer_1.TokenKind.Identifier && next.kind !== lexer_1.TokenKind.Number) {
                throw new Error(`Expected identifier or number after \`-\` in ${role} at line ${next.line}:${next.col}`);
            }
            name += '-' + next.value;
            this.lexer.consume();
        }
        return name;
    }
    /**
     * Reads a node type-ref: dotted segments (each may contain dashes) plus
     * an optional `@<version>` suffix. Examples:
     *   `http`
     *   `uipath.connector.uipath-atlassian-jira.create-issue`
     *   `uipath.connector.x@1.0.0`
     * Stops at `{` or `;` (the action body / declaration terminator).
     */
    parseTypeRef() {
        const startTok = this.lexer.current();
        let name = this.consumeTypeRefSegment(startTok);
        while (this.lexer.check(lexer_1.TokenKind.Dot)) {
            this.lexer.consume();
            const seg = this.consumeTypeRefSegment(this.lexer.current());
            name += '.' + seg;
        }
        let version;
        if (this.lexer.check(lexer_1.TokenKind.At)) {
            this.lexer.consume();
            version = this.consumeVersionString();
        }
        return { kind: 'TypeRef', name, version };
    }
    /**
     * One segment of a type-ref. The lexer atomises mixed alphanumeric runs
     * into adjacent Number + Identifier tokens (e.g. `4989d2b6` → Number
     * "4989", Identifier "d2b6"; UUID-style segments need this), and `-`
     * separates further chunks. Concatenate everything until we hit a `.`,
     * `@`, `{`, `;`, or any non-alphanumeric / non-dash token.
     */
    consumeTypeRefSegment(firstTok) {
        if (firstTok.kind !== lexer_1.TokenKind.Identifier && firstTok.kind !== lexer_1.TokenKind.Number) {
            throw new Error(`Expected type segment at line ${firstTok.line}:${firstTok.col}, got ${firstTok.kind} "${firstTok.value}"`);
        }
        let seg = firstTok.value;
        this.lexer.consume();
        while (true) {
            const t = this.lexer.current();
            if (t.kind === lexer_1.TokenKind.Identifier || t.kind === lexer_1.TokenKind.Number) {
                seg += t.value;
                this.lexer.consume();
            }
            else if (t.kind === lexer_1.TokenKind.Minus) {
                this.lexer.consume();
                const next = this.lexer.current();
                if (next.kind === lexer_1.TokenKind.Identifier || next.kind === lexer_1.TokenKind.Number) {
                    seg += '-' + next.value;
                    this.lexer.consume();
                }
                else {
                    // Trailing `-` before a terminator (`.`, `@`, `{`, `;`, …).
                    // Real registry types end this way — e.g.
                    // `uipath.connector.uipath-microsoft-outlook365.create-event-calendar-event-`.
                    // Preserve the dash verbatim and end the segment.
                    seg += '-';
                    break;
                }
            }
            else {
                break;
            }
        }
        return seg;
    }
    /**
     * A version after `@` is a `Number` (the lexer's number rule greedily
     * consumes one trailing `.<digits>`, so `1.0.0` tokenises as `1.0` and then
     * `.0`). Stitch consecutive Number tokens that start with `.` onto the
     * head. Also accepts a single Identifier (e.g. `latest`).
     */
    consumeVersionString() {
        const tok = this.lexer.current();
        if (tok.kind === lexer_1.TokenKind.Identifier) {
            this.lexer.consume();
            return tok.value;
        }
        if (tok.kind !== lexer_1.TokenKind.Number) {
            throw new Error(`Expected version after \`@\` at line ${tok.line}:${tok.col}`);
        }
        let v = tok.value;
        this.lexer.consume();
        // Additional segments come in as separate Number tokens whose value
        // starts with `.` (e.g. `.0`). Concatenate them.
        while (this.lexer.check(lexer_1.TokenKind.Number) && this.lexer.current().value.startsWith('.')) {
            v += this.lexer.consume().value;
        }
        return v;
    }
    // ─── Type Parsing ─────────────────────────────────────────────────────────
    parseType() {
        const tok = this.lexer.current();
        if (tok.kind === lexer_1.TokenKind.Identifier || tok.kind === lexer_1.TokenKind.Void) {
            const typeName = tok.value;
            this.lexer.consume();
            if (typeName === 'Promise') {
                this.lexer.expect(lexer_1.TokenKind.Lt);
                const inner = this.parseType();
                this.lexer.expect(lexer_1.TokenKind.Gt);
                return { kind: 'Promise', inner };
            }
            switch (typeName) {
                case 'i32': return 'i32';
                case 'i64': return 'i64';
                case 'f64': return 'f64';
                case 'bool': return 'bool';
                case 'boolean': return 'bool';
                case 'string': return 'string';
                case 'json': return 'json';
                case 'void': return 'void';
                case 'number': return 'f64';
                case 'bigint': return 'i64';
                case 'DateTime': return 'DateTime';
                case 'TimeSpan': return 'TimeSpan';
                case 'any': return 'json';
                case 'object': return 'json';
                default: return 'json'; // unknown types treated as json
            }
        }
        throw new Error(`Expected type at line ${tok.line}:${tok.col}, got ${tok.kind} "${tok.value}"`);
    }
    // ─── Function Declaration ─────────────────────────────────────────────────
    parseFunctionDeclaration() {
        let isAsync = false;
        if (this.lexer.check(lexer_1.TokenKind.Async)) {
            this.lexer.consume();
            isAsync = true;
        }
        this.lexer.expect(lexer_1.TokenKind.Function);
        const nameTok = this.lexer.expect(lexer_1.TokenKind.Identifier);
        const name = nameTok.value;
        this.lexer.expect(lexer_1.TokenKind.LParen);
        const params = [];
        while (!this.lexer.check(lexer_1.TokenKind.RParen) && !this.lexer.check(lexer_1.TokenKind.EOF)) {
            const paramName = this.lexer.expect(lexer_1.TokenKind.Identifier).value;
            this.lexer.expect(lexer_1.TokenKind.Colon);
            const paramType = this.parseType();
            params.push({ name: paramName, type: paramType });
            if (!this.lexer.match(lexer_1.TokenKind.Comma))
                break;
        }
        this.lexer.expect(lexer_1.TokenKind.RParen);
        let returnType = 'void';
        if (this.lexer.match(lexer_1.TokenKind.Colon)) {
            returnType = this.parseType();
        }
        const prevAsync = this.inAsync;
        this.inAsync = isAsync;
        const body = this.parseBlockStatement();
        this.inAsync = prevAsync;
        return { kind: 'FunctionDeclaration', name, isAsync, params, returnType, body };
    }
    // ─── Statements ───────────────────────────────────────────────────────────
    parseStatement() {
        const tok = this.lexer.current();
        // Check for labeled statement
        if (tok.kind === lexer_1.TokenKind.Identifier && this.lexer.peek2().kind === lexer_1.TokenKind.Colon) {
            const label = tok.value;
            this.lexer.consume(); // identifier
            this.lexer.consume(); // colon
            const body = this.parseStatement();
            return { kind: 'LabeledStatement', label, body };
        }
        switch (tok.kind) {
            case lexer_1.TokenKind.LBrace:
                return this.parseBlockStatement();
            case lexer_1.TokenKind.Let:
            case lexer_1.TokenKind.Const:
            case lexer_1.TokenKind.Var:
                return this.parseVariableDeclaration();
            case lexer_1.TokenKind.Return:
                return this.parseReturnStatement();
            case lexer_1.TokenKind.Throw:
                return this.parseThrowStatement();
            case lexer_1.TokenKind.If:
                return this.parseIfStatement();
            case lexer_1.TokenKind.While:
                return this.parseWhileStatement();
            case lexer_1.TokenKind.For:
                return this.parseForStatement();
            case lexer_1.TokenKind.Switch:
                return this.parseSwitchStatement();
            case lexer_1.TokenKind.Try:
                return this.parseTryStatement();
            case lexer_1.TokenKind.Break:
                return this.parseBreakStatement();
            case lexer_1.TokenKind.Continue:
                return this.parseContinueStatement();
            case lexer_1.TokenKind.Semicolon:
                this.lexer.consume();
                return { kind: 'BlockStatement', body: [] };
            default:
                return this.parseExpressionStatement();
        }
    }
    parseBlockStatement() {
        this.lexer.expect(lexer_1.TokenKind.LBrace);
        const body = [];
        while (!this.lexer.check(lexer_1.TokenKind.RBrace) && !this.lexer.check(lexer_1.TokenKind.EOF)) {
            if (this.lexer.match(lexer_1.TokenKind.Semicolon))
                continue;
            body.push(this.parseStatement());
        }
        this.lexer.expect(lexer_1.TokenKind.RBrace);
        return { kind: 'BlockStatement', body };
    }
    parseVariableDeclaration() {
        const tok = this.lexer.consume();
        const declKind = tok.kind === lexer_1.TokenKind.Const ? 'const' : 'let';
        const name = this.lexer.expect(lexer_1.TokenKind.Identifier).value;
        let typeAnnotation;
        if (this.lexer.match(lexer_1.TokenKind.Colon)) {
            typeAnnotation = this.parseType();
        }
        let initializer;
        if (this.lexer.match(lexer_1.TokenKind.Eq)) {
            initializer = this.parseExpression();
        }
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return {
            kind: 'VariableDeclaration',
            declarationKind: declKind,
            name,
            typeAnnotation,
            initializer,
        };
    }
    parseReturnStatement() {
        this.lexer.expect(lexer_1.TokenKind.Return);
        let argument;
        // Check if there's an expression (no semicolon or newline-based ASI)
        if (!this.lexer.check(lexer_1.TokenKind.Semicolon) &&
            !this.lexer.check(lexer_1.TokenKind.RBrace) &&
            !this.lexer.check(lexer_1.TokenKind.EOF)) {
            argument = this.parseExpression();
        }
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'ReturnStatement', argument };
    }
    parseThrowStatement() {
        this.lexer.expect(lexer_1.TokenKind.Throw);
        const argument = this.parseExpression();
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'ThrowStatement', argument };
    }
    parseIfStatement() {
        this.lexer.expect(lexer_1.TokenKind.If);
        this.lexer.expect(lexer_1.TokenKind.LParen);
        const test = this.parseExpression();
        this.lexer.expect(lexer_1.TokenKind.RParen);
        const consequent = this.parseStatement();
        let alternate;
        if (this.lexer.match(lexer_1.TokenKind.Else)) {
            alternate = this.parseStatement();
        }
        return { kind: 'IfStatement', test, consequent, alternate };
    }
    parseWhileStatement() {
        this.lexer.expect(lexer_1.TokenKind.While);
        this.lexer.expect(lexer_1.TokenKind.LParen);
        const test = this.parseExpression();
        this.lexer.expect(lexer_1.TokenKind.RParen);
        const body = this.parseStatement();
        return { kind: 'WhileStatement', test, body };
    }
    parseForStatement() {
        this.lexer.expect(lexer_1.TokenKind.For);
        this.lexer.expect(lexer_1.TokenKind.LParen);
        // Peek to detect for..of or for..in
        const savedPos = this.lexer.current();
        // Try to parse init
        let initDecl;
        if (this.lexer.check(lexer_1.TokenKind.Let) || this.lexer.check(lexer_1.TokenKind.Const) || this.lexer.check(lexer_1.TokenKind.Var)) {
            const declKind = this.lexer.consume().kind === lexer_1.TokenKind.Const ? 'const' : 'let';
            const itemName = this.lexer.expect(lexer_1.TokenKind.Identifier).value;
            // Check for for..of or for..in
            if (this.lexer.check(lexer_1.TokenKind.Of)) {
                this.lexer.consume();
                const iterable = this.parseExpression();
                this.lexer.expect(lexer_1.TokenKind.RParen);
                const body = this.parseStatement();
                return { kind: 'ForOfStatement', declarationKind: declKind, item: itemName, iterable, body };
            }
            if (this.lexer.check(lexer_1.TokenKind.In)) {
                this.lexer.consume();
                const object = this.parseExpression();
                this.lexer.expect(lexer_1.TokenKind.RParen);
                const body = this.parseStatement();
                return { kind: 'ForInStatement', declarationKind: declKind, key: itemName, object, body };
            }
            // Regular for loop
            let typeAnnotation;
            if (this.lexer.match(lexer_1.TokenKind.Colon)) {
                typeAnnotation = this.parseType();
            }
            let initializer;
            if (this.lexer.match(lexer_1.TokenKind.Eq)) {
                initializer = this.parseExpression();
            }
            initDecl = { kind: 'VariableDeclaration', declarationKind: declKind, name: itemName, typeAnnotation, initializer };
        }
        this.lexer.expect(lexer_1.TokenKind.Semicolon);
        let test;
        if (!this.lexer.check(lexer_1.TokenKind.Semicolon)) {
            test = this.parseExpression();
        }
        this.lexer.expect(lexer_1.TokenKind.Semicolon);
        let update;
        if (!this.lexer.check(lexer_1.TokenKind.RParen)) {
            update = this.parseExpression();
        }
        this.lexer.expect(lexer_1.TokenKind.RParen);
        const body = this.parseStatement();
        return {
            kind: 'ForStatement',
            init: initDecl,
            test,
            update,
            body,
        };
    }
    parseSwitchStatement() {
        this.lexer.expect(lexer_1.TokenKind.Switch);
        this.lexer.expect(lexer_1.TokenKind.LParen);
        const discriminant = this.parseExpression();
        this.lexer.expect(lexer_1.TokenKind.RParen);
        this.lexer.expect(lexer_1.TokenKind.LBrace);
        const cases = [];
        while (!this.lexer.check(lexer_1.TokenKind.RBrace) && !this.lexer.check(lexer_1.TokenKind.EOF)) {
            if (this.lexer.match(lexer_1.TokenKind.Case)) {
                const test = this.parseExpression();
                this.lexer.expect(lexer_1.TokenKind.Colon);
                const consequent = [];
                while (!this.lexer.check(lexer_1.TokenKind.Case) &&
                    !this.lexer.check(lexer_1.TokenKind.Default) &&
                    !this.lexer.check(lexer_1.TokenKind.RBrace) &&
                    !this.lexer.check(lexer_1.TokenKind.EOF)) {
                    if (this.lexer.match(lexer_1.TokenKind.Semicolon))
                        continue;
                    consequent.push(this.parseStatement());
                }
                cases.push({ kind: 'SwitchCase', test, consequent });
            }
            else if (this.lexer.match(lexer_1.TokenKind.Default)) {
                this.lexer.expect(lexer_1.TokenKind.Colon);
                const consequent = [];
                while (!this.lexer.check(lexer_1.TokenKind.Case) &&
                    !this.lexer.check(lexer_1.TokenKind.Default) &&
                    !this.lexer.check(lexer_1.TokenKind.RBrace) &&
                    !this.lexer.check(lexer_1.TokenKind.EOF)) {
                    if (this.lexer.match(lexer_1.TokenKind.Semicolon))
                        continue;
                    consequent.push(this.parseStatement());
                }
                cases.push({ kind: 'SwitchCase', test: undefined, consequent });
            }
            else {
                break;
            }
        }
        this.lexer.expect(lexer_1.TokenKind.RBrace);
        return { kind: 'SwitchStatement', discriminant, cases };
    }
    parseTryStatement() {
        this.lexer.expect(lexer_1.TokenKind.Try);
        const block = this.parseBlockStatement();
        let handler;
        if (this.lexer.match(lexer_1.TokenKind.Catch)) {
            let param;
            if (this.lexer.match(lexer_1.TokenKind.LParen)) {
                param = this.lexer.expect(lexer_1.TokenKind.Identifier).value;
                this.lexer.expect(lexer_1.TokenKind.RParen);
            }
            const body = this.parseBlockStatement();
            handler = { kind: 'CatchClause', param, body };
        }
        let finalizer;
        if (this.lexer.match(lexer_1.TokenKind.Finally)) {
            finalizer = this.parseBlockStatement();
        }
        return { kind: 'TryStatement', block, handler, finalizer };
    }
    parseBreakStatement() {
        this.lexer.expect(lexer_1.TokenKind.Break);
        let label;
        if (this.lexer.check(lexer_1.TokenKind.Identifier) && !this.lexer.check(lexer_1.TokenKind.Semicolon)) {
            label = this.lexer.consume().value;
        }
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'BreakStatement', label };
    }
    parseContinueStatement() {
        this.lexer.expect(lexer_1.TokenKind.Continue);
        let label;
        if (this.lexer.check(lexer_1.TokenKind.Identifier)) {
            label = this.lexer.consume().value;
        }
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'ContinueStatement', label };
    }
    parseExpressionStatement() {
        const expression = this.parseExpression();
        this.lexer.match(lexer_1.TokenKind.Semicolon);
        return { kind: 'ExpressionStatement', expression };
    }
    // ─── Expressions ──────────────────────────────────────────────────────────
    parseExpression() {
        return this.parseAssignment();
    }
    parseAssignment() {
        const left = this.parseConditional();
        const assignOps = [
            lexer_1.TokenKind.Eq, lexer_1.TokenKind.PlusEq, lexer_1.TokenKind.MinusEq, lexer_1.TokenKind.StarEq,
            lexer_1.TokenKind.StarStarEq, lexer_1.TokenKind.SlashEq, lexer_1.TokenKind.PercentEq,
            lexer_1.TokenKind.AmpAmpEq, lexer_1.TokenKind.PipePipeEq, lexer_1.TokenKind.QuestionQuestionEq,
            lexer_1.TokenKind.AmpEq, lexer_1.TokenKind.PipeEq, lexer_1.TokenKind.CaretEq,
            lexer_1.TokenKind.LtLtEq, lexer_1.TokenKind.GtGtEq, lexer_1.TokenKind.GtGtGtEq,
        ];
        const cur = this.lexer.current();
        if (assignOps.includes(cur.kind)) {
            const op = cur.value;
            this.lexer.consume();
            const right = this.parseAssignment();
            return { kind: 'AssignmentExpression', operator: op, left, right };
        }
        return left;
    }
    parseConditional() {
        const test = this.parseNullishCoalescing();
        if (this.lexer.match(lexer_1.TokenKind.Question)) {
            const consequent = this.parseAssignment();
            this.lexer.expect(lexer_1.TokenKind.Colon);
            const alternate = this.parseAssignment();
            return { kind: 'ConditionalExpression', test, consequent, alternate };
        }
        return test;
    }
    parseNullishCoalescing() {
        let left = this.parseLogicalOr();
        while (this.lexer.check(lexer_1.TokenKind.QuestionQuestion)) {
            this.lexer.consume();
            const right = this.parseLogicalOr();
            left = { kind: 'LogicalExpression', operator: '??', left, right };
        }
        return left;
    }
    parseLogicalOr() {
        let left = this.parseLogicalAnd();
        while (this.lexer.check(lexer_1.TokenKind.PipePipe)) {
            this.lexer.consume();
            const right = this.parseLogicalAnd();
            left = { kind: 'LogicalExpression', operator: '||', left, right };
        }
        return left;
    }
    parseLogicalAnd() {
        let left = this.parseBitwiseOr();
        while (this.lexer.check(lexer_1.TokenKind.AmpAmp)) {
            this.lexer.consume();
            const right = this.parseBitwiseOr();
            left = { kind: 'LogicalExpression', operator: '&&', left, right };
        }
        return left;
    }
    parseBitwiseOr() {
        let left = this.parseBitwiseXor();
        while (this.lexer.check(lexer_1.TokenKind.Pipe)) {
            this.lexer.consume();
            const right = this.parseBitwiseXor();
            left = { kind: 'BinaryExpression', operator: '|', left, right };
        }
        return left;
    }
    parseBitwiseXor() {
        let left = this.parseBitwiseAnd();
        while (this.lexer.check(lexer_1.TokenKind.Caret)) {
            this.lexer.consume();
            const right = this.parseBitwiseAnd();
            left = { kind: 'BinaryExpression', operator: '^', left, right };
        }
        return left;
    }
    parseBitwiseAnd() {
        let left = this.parseEquality();
        while (this.lexer.check(lexer_1.TokenKind.Amp)) {
            this.lexer.consume();
            const right = this.parseEquality();
            left = { kind: 'BinaryExpression', operator: '&', left, right };
        }
        return left;
    }
    parseEquality() {
        let left = this.parseRelational();
        while (true) {
            const cur = this.lexer.current();
            if (cur.kind === lexer_1.TokenKind.EqEq || cur.kind === lexer_1.TokenKind.BangEq ||
                cur.kind === lexer_1.TokenKind.EqEqEq || cur.kind === lexer_1.TokenKind.BangEqEq) {
                const op = cur.value;
                this.lexer.consume();
                const right = this.parseRelational();
                left = { kind: 'BinaryExpression', operator: op, left, right };
            }
            else {
                break;
            }
        }
        return left;
    }
    parseRelational() {
        let left = this.parseShift();
        while (true) {
            const cur = this.lexer.current();
            if (cur.kind === lexer_1.TokenKind.Lt || cur.kind === lexer_1.TokenKind.LtEq ||
                cur.kind === lexer_1.TokenKind.Gt || cur.kind === lexer_1.TokenKind.GtEq ||
                cur.kind === lexer_1.TokenKind.Instanceof || cur.kind === lexer_1.TokenKind.In) {
                const op = cur.value;
                this.lexer.consume();
                const right = this.parseShift();
                left = { kind: 'BinaryExpression', operator: op, left, right };
            }
            else {
                break;
            }
        }
        return left;
    }
    parseShift() {
        let left = this.parseAdditive();
        while (true) {
            const cur = this.lexer.current();
            if (cur.kind === lexer_1.TokenKind.LtLt || cur.kind === lexer_1.TokenKind.GtGt || cur.kind === lexer_1.TokenKind.GtGtGt) {
                const op = cur.value;
                this.lexer.consume();
                const right = this.parseAdditive();
                left = { kind: 'BinaryExpression', operator: op, left, right };
            }
            else {
                break;
            }
        }
        return left;
    }
    parseAdditive() {
        let left = this.parseMultiplicative();
        while (true) {
            const cur = this.lexer.current();
            if (cur.kind === lexer_1.TokenKind.Plus || cur.kind === lexer_1.TokenKind.Minus) {
                const op = cur.value;
                this.lexer.consume();
                const right = this.parseMultiplicative();
                left = { kind: 'BinaryExpression', operator: op, left, right };
            }
            else {
                break;
            }
        }
        return left;
    }
    parseMultiplicative() {
        let left = this.parseExponential();
        while (true) {
            const cur = this.lexer.current();
            if (cur.kind === lexer_1.TokenKind.Star || cur.kind === lexer_1.TokenKind.Slash || cur.kind === lexer_1.TokenKind.Percent) {
                const op = cur.value;
                this.lexer.consume();
                const right = this.parseExponential();
                left = { kind: 'BinaryExpression', operator: op, left, right };
            }
            else {
                break;
            }
        }
        return left;
    }
    parseExponential() {
        const left = this.parseUnary();
        if (this.lexer.check(lexer_1.TokenKind.StarStar)) {
            this.lexer.consume();
            const right = this.parseExponential(); // right-associative
            return { kind: 'BinaryExpression', operator: '**', left, right };
        }
        return left;
    }
    parseUnary() {
        const cur = this.lexer.current();
        if (cur.kind === lexer_1.TokenKind.Bang || cur.kind === lexer_1.TokenKind.Tilde ||
            cur.kind === lexer_1.TokenKind.Minus || cur.kind === lexer_1.TokenKind.Plus) {
            const op = cur.value;
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'UnaryExpression', operator: op, argument, prefix: true };
        }
        if (cur.kind === lexer_1.TokenKind.Typeof) {
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'UnaryExpression', operator: 'typeof', argument, prefix: true };
        }
        if (cur.kind === lexer_1.TokenKind.Void) {
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'UnaryExpression', operator: 'void', argument, prefix: true };
        }
        if (cur.kind === lexer_1.TokenKind.Delete) {
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'UnaryExpression', operator: 'delete', argument, prefix: true };
        }
        if (cur.kind === lexer_1.TokenKind.PlusPlus) {
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'UpdateExpression', operator: '++', argument, prefix: true };
        }
        if (cur.kind === lexer_1.TokenKind.MinusMinus) {
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'UpdateExpression', operator: '--', argument, prefix: true };
        }
        if (cur.kind === lexer_1.TokenKind.Await && this.inAsync) {
            this.lexer.consume();
            const argument = this.parseUnary();
            return { kind: 'AwaitExpression', argument };
        }
        return this.parsePostfix();
    }
    parsePostfix() {
        let expr = this.parseCallMember();
        if (this.lexer.check(lexer_1.TokenKind.PlusPlus)) {
            this.lexer.consume();
            return { kind: 'UpdateExpression', operator: '++', argument: expr, prefix: false };
        }
        if (this.lexer.check(lexer_1.TokenKind.MinusMinus)) {
            this.lexer.consume();
            return { kind: 'UpdateExpression', operator: '--', argument: expr, prefix: false };
        }
        return expr;
    }
    parseCallMember() {
        let expr = this.parsePrimary();
        while (true) {
            if (this.lexer.check(lexer_1.TokenKind.Dot) || this.lexer.check(lexer_1.TokenKind.QuestionDot)) {
                const optional = this.lexer.current().kind === lexer_1.TokenKind.QuestionDot;
                this.lexer.consume();
                const prop = this.lexer.expect(lexer_1.TokenKind.Identifier);
                expr = {
                    kind: 'MemberExpression',
                    object: expr,
                    property: { kind: 'Identifier', name: prop.value },
                    computed: false,
                    optional,
                };
            }
            else if (this.lexer.check(lexer_1.TokenKind.LBracket)) {
                this.lexer.consume();
                const prop = this.parseExpression();
                this.lexer.expect(lexer_1.TokenKind.RBracket);
                expr = {
                    kind: 'MemberExpression',
                    object: expr,
                    property: prop,
                    computed: true,
                    optional: false,
                };
            }
            else if (this.lexer.check(lexer_1.TokenKind.LParen)) {
                this.lexer.consume();
                const args = this.parseArgumentList();
                this.lexer.expect(lexer_1.TokenKind.RParen);
                expr = { kind: 'CallExpression', callee: expr, arguments: args };
            }
            else if (this.lexer.check(lexer_1.TokenKind.Lt)) {
                // Type assertion: expr as Type or generic call (handle "as" keyword)
                // We don't consume here — it might be a comparison
                break;
            }
            else {
                break;
            }
        }
        // Handle "as Type" type assertion
        if (this.lexer.check(lexer_1.TokenKind.Identifier) && this.lexer.current().value === 'as') {
            this.lexer.consume();
            const assertedType = this.parseType();
            return { kind: 'TypeAssertion', expression: expr, assertedType };
        }
        return expr;
    }
    parseArgumentList() {
        const args = [];
        while (!this.lexer.check(lexer_1.TokenKind.RParen) && !this.lexer.check(lexer_1.TokenKind.EOF)) {
            if (this.lexer.check(lexer_1.TokenKind.DotDotDot)) {
                this.lexer.consume();
                const arg = this.parseAssignment();
                args.push({ kind: 'SpreadElement', argument: arg });
            }
            else {
                args.push(this.parseAssignment());
            }
            if (!this.lexer.match(lexer_1.TokenKind.Comma))
                break;
        }
        return args;
    }
    parsePrimary() {
        const tok = this.lexer.current();
        switch (tok.kind) {
            case lexer_1.TokenKind.Number: {
                this.lexer.consume();
                // Bigint literal: `123n` — parse as integer, raw retains the suffix so
                // downstream stages can treat it as i64.
                if (tok.value.endsWith('n')) {
                    const digits = tok.value.slice(0, -1);
                    const val = parseInt(digits, digits.startsWith('0x') || digits.startsWith('0X') ? 16 : 10);
                    const lit = { kind: 'Literal', value: val, raw: tok.value };
                    lit.filType = 'i64';
                    return lit;
                }
                const val = tok.value.includes('.') || tok.value.includes('e') || tok.value.includes('E')
                    ? parseFloat(tok.value)
                    : parseInt(tok.value, tok.value.startsWith('0x') || tok.value.startsWith('0X') ? 16 : 10);
                return { kind: 'Literal', value: val, raw: tok.value };
            }
            case lexer_1.TokenKind.String: {
                this.lexer.consume();
                return { kind: 'Literal', value: tok.value, raw: JSON.stringify(tok.value) };
            }
            case lexer_1.TokenKind.True: {
                this.lexer.consume();
                return { kind: 'Literal', value: true, raw: 'true' };
            }
            case lexer_1.TokenKind.False: {
                this.lexer.consume();
                return { kind: 'Literal', value: false, raw: 'false' };
            }
            case lexer_1.TokenKind.Null: {
                this.lexer.consume();
                return { kind: 'Literal', value: null, raw: 'null' };
            }
            case lexer_1.TokenKind.Undefined: {
                this.lexer.consume();
                return { kind: 'Literal', value: null, raw: 'undefined' };
            }
            case lexer_1.TokenKind.NoSubstTemplate: {
                this.lexer.consume();
                return { kind: 'TemplateLiteral', quasis: [tok.value], expressions: [] };
            }
            case lexer_1.TokenKind.TemplateHead: {
                return this.parseTemplateLiteral();
            }
            case lexer_1.TokenKind.LBracket: {
                return this.parseArrayExpression();
            }
            case lexer_1.TokenKind.LBrace: {
                return this.parseObjectExpression();
            }
            case lexer_1.TokenKind.LParen: {
                this.lexer.consume();
                const expr = this.parseExpression();
                this.lexer.expect(lexer_1.TokenKind.RParen);
                return expr;
            }
            case lexer_1.TokenKind.New: {
                this.lexer.consume();
                const callee = this.parseCallMember();
                let args = [];
                if (this.lexer.match(lexer_1.TokenKind.LParen)) {
                    args = this.parseArgumentList();
                    this.lexer.expect(lexer_1.TokenKind.RParen);
                }
                return { kind: 'NewExpression', callee, arguments: args };
            }
            case lexer_1.TokenKind.Identifier: {
                this.lexer.consume();
                return { kind: 'Identifier', name: tok.value };
            }
            // Handle keywords used as identifiers in some contexts
            case lexer_1.TokenKind.Async: {
                this.lexer.consume();
                return { kind: 'Identifier', name: 'async' };
            }
            default:
                throw new Error(`Unexpected token ${tok.kind} ("${tok.value}") at line ${tok.line}:${tok.col}`);
        }
    }
    parseTemplateLiteral() {
        const quasis = [];
        const expressions = [];
        // We need to reconstruct template literal from TemplateHead/Middle/Tail tokens
        // The lexer may have already tokenized the inner expressions
        // We handle it by tracking TemplateHead, TemplateMiddle, TemplateTail tokens
        if (this.lexer.check(lexer_1.TokenKind.TemplateHead)) {
            quasis.push(this.lexer.consume().value);
            // Parse expression
            expressions.push(this.parseExpression());
            // Expect TemplateMiddle or TemplateTail
            while (this.lexer.check(lexer_1.TokenKind.TemplateMiddle)) {
                quasis.push(this.lexer.consume().value);
                expressions.push(this.parseExpression());
            }
            if (this.lexer.check(lexer_1.TokenKind.TemplateTail)) {
                quasis.push(this.lexer.consume().value);
            }
        }
        return { kind: 'TemplateLiteral', quasis, expressions };
    }
    parseArrayExpression() {
        this.lexer.expect(lexer_1.TokenKind.LBracket);
        const elements = [];
        while (!this.lexer.check(lexer_1.TokenKind.RBracket) && !this.lexer.check(lexer_1.TokenKind.EOF)) {
            if (this.lexer.check(lexer_1.TokenKind.DotDotDot)) {
                this.lexer.consume();
                elements.push({ kind: 'SpreadElement', argument: this.parseAssignment() });
            }
            else {
                elements.push(this.parseAssignment());
            }
            if (!this.lexer.match(lexer_1.TokenKind.Comma))
                break;
        }
        this.lexer.expect(lexer_1.TokenKind.RBracket);
        return { kind: 'ArrayExpression', elements };
    }
    parseObjectExpression() {
        this.lexer.expect(lexer_1.TokenKind.LBrace);
        const properties = [];
        while (!this.lexer.check(lexer_1.TokenKind.RBrace) && !this.lexer.check(lexer_1.TokenKind.EOF)) {
            // Get key
            let key;
            const keyTok = this.lexer.current();
            if (keyTok.kind === lexer_1.TokenKind.Identifier || keyTok.kind === lexer_1.TokenKind.String ||
                keyTok.kind === lexer_1.TokenKind.Number) {
                key = keyTok.value;
                this.lexer.consume();
            }
            else {
                // Handle keyword as key
                key = keyTok.value;
                this.lexer.consume();
            }
            if (this.lexer.match(lexer_1.TokenKind.Colon)) {
                const value = this.parseAssignment();
                properties.push({ kind: 'ObjectProperty', key, value, shorthand: false });
            }
            else {
                // Shorthand property: { name } => { name: name }
                properties.push({
                    kind: 'ObjectProperty',
                    key,
                    value: { kind: 'Identifier', name: key },
                    shorthand: true,
                });
            }
            if (!this.lexer.match(lexer_1.TokenKind.Comma))
                break;
        }
        this.lexer.expect(lexer_1.TokenKind.RBrace);
        return { kind: 'ObjectExpression', properties };
    }
}
exports.Parser = Parser;
//# sourceMappingURL=parser.js.map