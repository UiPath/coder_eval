"use strict";
// Lexer (tokenizer) for the FIL compiler
Object.defineProperty(exports, "__esModule", { value: true });
exports.Lexer = exports.TokenKind = void 0;
var TokenKind;
(function (TokenKind) {
    // Literals
    TokenKind["Number"] = "Number";
    TokenKind["String"] = "String";
    TokenKind["TemplateHead"] = "TemplateHead";
    TokenKind["TemplateMiddle"] = "TemplateMiddle";
    TokenKind["TemplateTail"] = "TemplateTail";
    TokenKind["NoSubstTemplate"] = "NoSubstTemplate";
    TokenKind["True"] = "true";
    TokenKind["False"] = "false";
    TokenKind["Null"] = "null";
    TokenKind["Undefined"] = "undefined";
    // Identifiers
    TokenKind["Identifier"] = "Identifier";
    // Keywords
    TokenKind["Let"] = "let";
    TokenKind["Const"] = "const";
    TokenKind["Function"] = "function";
    TokenKind["Async"] = "async";
    TokenKind["Await"] = "await";
    TokenKind["Return"] = "return";
    TokenKind["If"] = "if";
    TokenKind["Else"] = "else";
    TokenKind["While"] = "while";
    TokenKind["For"] = "for";
    TokenKind["Of"] = "of";
    TokenKind["In"] = "in";
    TokenKind["Switch"] = "switch";
    TokenKind["Case"] = "case";
    TokenKind["Default"] = "default";
    TokenKind["Break"] = "break";
    TokenKind["Continue"] = "continue";
    TokenKind["Try"] = "try";
    TokenKind["Catch"] = "catch";
    TokenKind["Finally"] = "finally";
    TokenKind["Throw"] = "throw";
    TokenKind["New"] = "new";
    TokenKind["Typeof"] = "typeof";
    TokenKind["Instanceof"] = "instanceof";
    TokenKind["Void"] = "void";
    TokenKind["Delete"] = "delete";
    TokenKind["Var"] = "var";
    // Punctuation
    TokenKind["LBrace"] = "{";
    TokenKind["RBrace"] = "}";
    TokenKind["LParen"] = "(";
    TokenKind["RParen"] = ")";
    TokenKind["LBracket"] = "[";
    TokenKind["RBracket"] = "]";
    TokenKind["Dot"] = ".";
    TokenKind["DotDotDot"] = "...";
    TokenKind["Comma"] = ",";
    TokenKind["Semicolon"] = ";";
    TokenKind["Colon"] = ":";
    TokenKind["Question"] = "?";
    TokenKind["QuestionDot"] = "?.";
    TokenKind["QuestionQuestion"] = "??";
    TokenKind["QuestionQuestionEq"] = "??=";
    TokenKind["Arrow"] = "=>";
    // Operators
    TokenKind["Plus"] = "+";
    TokenKind["Minus"] = "-";
    TokenKind["Star"] = "*";
    TokenKind["StarStar"] = "**";
    TokenKind["Slash"] = "/";
    TokenKind["Percent"] = "%";
    TokenKind["Eq"] = "=";
    TokenKind["EqEq"] = "==";
    TokenKind["EqEqEq"] = "===";
    TokenKind["BangEq"] = "!=";
    TokenKind["BangEqEq"] = "!==";
    TokenKind["Lt"] = "<";
    TokenKind["LtEq"] = "<=";
    TokenKind["Gt"] = ">";
    TokenKind["GtEq"] = ">=";
    TokenKind["Bang"] = "!";
    TokenKind["AmpAmp"] = "&&";
    TokenKind["PipePipe"] = "||";
    TokenKind["Amp"] = "&";
    TokenKind["Pipe"] = "|";
    TokenKind["Caret"] = "^";
    TokenKind["Tilde"] = "~";
    TokenKind["LtLt"] = "<<";
    TokenKind["GtGt"] = ">>";
    TokenKind["GtGtGt"] = ">>>";
    TokenKind["PlusPlus"] = "++";
    TokenKind["MinusMinus"] = "--";
    TokenKind["PlusEq"] = "+=";
    TokenKind["MinusEq"] = "-=";
    TokenKind["StarEq"] = "*=";
    TokenKind["StarStarEq"] = "**=";
    TokenKind["SlashEq"] = "/=";
    TokenKind["PercentEq"] = "%=";
    TokenKind["AmpAmpEq"] = "&&=";
    TokenKind["PipePipeEq"] = "||=";
    TokenKind["AmpEq"] = "&=";
    TokenKind["PipeEq"] = "|=";
    TokenKind["CaretEq"] = "^=";
    TokenKind["LtLtEq"] = "<<=";
    TokenKind["GtGtEq"] = ">>=";
    TokenKind["GtGtGtEq"] = ">>>=";
    // Special
    TokenKind["At"] = "@";
    TokenKind["EOF"] = "EOF";
})(TokenKind || (exports.TokenKind = TokenKind = {}));
const KEYWORDS = {
    let: TokenKind.Let,
    const: TokenKind.Const,
    function: TokenKind.Function,
    async: TokenKind.Async,
    await: TokenKind.Await,
    return: TokenKind.Return,
    if: TokenKind.If,
    else: TokenKind.Else,
    while: TokenKind.While,
    for: TokenKind.For,
    of: TokenKind.Of,
    in: TokenKind.In,
    switch: TokenKind.Switch,
    case: TokenKind.Case,
    default: TokenKind.Default,
    break: TokenKind.Break,
    continue: TokenKind.Continue,
    try: TokenKind.Try,
    catch: TokenKind.Catch,
    finally: TokenKind.Finally,
    throw: TokenKind.Throw,
    new: TokenKind.New,
    typeof: TokenKind.Typeof,
    instanceof: TokenKind.Instanceof,
    void: TokenKind.Void,
    delete: TokenKind.Delete,
    var: TokenKind.Var,
    true: TokenKind.True,
    false: TokenKind.False,
    null: TokenKind.Null,
    undefined: TokenKind.Undefined,
};
class Lexer {
    constructor(source) {
        this.pos = 0;
        this.line = 1;
        this.col = 1;
        this.tokens = [];
        this.tokenIndex = 0;
        this.source = source;
        this.tokenize();
    }
    peek(offset = 0) {
        return this.source[this.pos + offset] ?? '';
    }
    advance() {
        const ch = this.source[this.pos++];
        if (ch === '\n') {
            this.line++;
            this.col = 1;
        }
        else {
            this.col++;
        }
        return ch;
    }
    addToken(kind, value, line, col) {
        this.tokens.push({ kind, value, line, col });
    }
    skipWhitespaceAndComments() {
        while (this.pos < this.source.length) {
            const ch = this.peek();
            if (ch === ' ' || ch === '\t' || ch === '\r' || ch === '\n') {
                this.advance();
            }
            else if (ch === '/' && this.peek(1) === '/') {
                // Line comment
                while (this.pos < this.source.length && this.peek() !== '\n') {
                    this.advance();
                }
            }
            else if (ch === '/' && this.peek(1) === '*') {
                // Block comment
                this.advance();
                this.advance();
                while (this.pos < this.source.length) {
                    if (this.peek() === '*' && this.peek(1) === '/') {
                        this.advance();
                        this.advance();
                        break;
                    }
                    this.advance();
                }
            }
            else {
                break;
            }
        }
    }
    readString(quote) {
        let result = '';
        while (this.pos < this.source.length) {
            const ch = this.peek();
            if (ch === quote) {
                this.advance();
                break;
            }
            if (ch === '\\') {
                this.advance();
                const esc = this.advance();
                switch (esc) {
                    case 'n':
                        result += '\n';
                        break;
                    case 't':
                        result += '\t';
                        break;
                    case 'r':
                        result += '\r';
                        break;
                    case '\\':
                        result += '\\';
                        break;
                    case '"':
                        result += '"';
                        break;
                    case "'":
                        result += "'";
                        break;
                    case '0':
                        result += '\0';
                        break;
                    default:
                        result += esc;
                        break;
                }
            }
            else {
                result += this.advance();
            }
        }
        return result;
    }
    tokenize() {
        while (this.pos < this.source.length) {
            this.skipWhitespaceAndComments();
            if (this.pos >= this.source.length)
                break;
            const startLine = this.line;
            const startCol = this.col;
            const ch = this.peek();
            // Numbers
            if (ch >= '0' && ch <= '9' || (ch === '.' && this.peek(1) >= '0' && this.peek(1) <= '9')) {
                let num = '';
                if (ch === '0' && (this.peek(1) === 'x' || this.peek(1) === 'X')) {
                    num += this.advance() + this.advance();
                    while (/[0-9a-fA-F]/.test(this.peek()))
                        num += this.advance();
                }
                else {
                    while (this.peek() >= '0' && this.peek() <= '9')
                        num += this.advance();
                    if (this.peek() === '.') {
                        num += this.advance();
                        while (this.peek() >= '0' && this.peek() <= '9')
                            num += this.advance();
                    }
                    if (this.peek() === 'e' || this.peek() === 'E') {
                        num += this.advance();
                        if (this.peek() === '+' || this.peek() === '-')
                            num += this.advance();
                        while (this.peek() >= '0' && this.peek() <= '9')
                            num += this.advance();
                    }
                }
                // Optional bigint suffix `n`. Keep it on the value so the parser can detect it.
                if (this.peek() === 'n') {
                    num += this.advance();
                }
                this.addToken(TokenKind.Number, num, startLine, startCol);
                continue;
            }
            // Strings
            if (ch === '"' || ch === "'") {
                this.advance(); // consume opening quote
                const str = this.readString(ch);
                this.addToken(TokenKind.String, str, startLine, startCol);
                continue;
            }
            // Template literals
            if (ch === '`') {
                this.advance(); // consume backtick
                this.tokenizeTemplate(startLine, startCol);
                continue;
            }
            // Identifiers and keywords
            if (ch === '_' || ch === '$' || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')) {
                let ident = '';
                while (this.pos < this.source.length) {
                    const c = this.peek();
                    if (c === '_' || c === '$' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) {
                        ident += this.advance();
                    }
                    else {
                        break;
                    }
                }
                const kw = KEYWORDS[ident];
                if (kw !== undefined) {
                    this.addToken(kw, ident, startLine, startCol);
                }
                else {
                    this.addToken(TokenKind.Identifier, ident, startLine, startCol);
                }
                continue;
            }
            // Multi-char operators and punctuation
            this.advance(); // consume current char
            switch (ch) {
                case '{':
                    this.addToken(TokenKind.LBrace, ch, startLine, startCol);
                    break;
                case '}':
                    this.addToken(TokenKind.RBrace, ch, startLine, startCol);
                    break;
                case '(':
                    this.addToken(TokenKind.LParen, ch, startLine, startCol);
                    break;
                case ')':
                    this.addToken(TokenKind.RParen, ch, startLine, startCol);
                    break;
                case '[':
                    this.addToken(TokenKind.LBracket, ch, startLine, startCol);
                    break;
                case ']':
                    this.addToken(TokenKind.RBracket, ch, startLine, startCol);
                    break;
                case ',':
                    this.addToken(TokenKind.Comma, ch, startLine, startCol);
                    break;
                case ';':
                    this.addToken(TokenKind.Semicolon, ch, startLine, startCol);
                    break;
                case ':':
                    this.addToken(TokenKind.Colon, ch, startLine, startCol);
                    break;
                case '~':
                    this.addToken(TokenKind.Tilde, ch, startLine, startCol);
                    break;
                case '@':
                    this.addToken(TokenKind.At, ch, startLine, startCol);
                    break;
                case '.':
                    if (this.peek() === '.' && this.peek(1) === '.') {
                        this.advance();
                        this.advance();
                        this.addToken(TokenKind.DotDotDot, '...', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Dot, ch, startLine, startCol);
                    }
                    break;
                case '?':
                    if (this.peek() === '.') {
                        this.advance();
                        this.addToken(TokenKind.QuestionDot, '?.', startLine, startCol);
                    }
                    else if (this.peek() === '?') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.QuestionQuestionEq, '??=', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.QuestionQuestion, '??', startLine, startCol);
                        }
                    }
                    else {
                        this.addToken(TokenKind.Question, ch, startLine, startCol);
                    }
                    break;
                case '!':
                    if (this.peek() === '=') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.BangEqEq, '!==', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.BangEq, '!=', startLine, startCol);
                        }
                    }
                    else {
                        this.addToken(TokenKind.Bang, ch, startLine, startCol);
                    }
                    break;
                case '=':
                    if (this.peek() === '=') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.EqEqEq, '===', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.EqEq, '==', startLine, startCol);
                        }
                    }
                    else if (this.peek() === '>') {
                        this.advance();
                        this.addToken(TokenKind.Arrow, '=>', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Eq, ch, startLine, startCol);
                    }
                    break;
                case '<':
                    if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.LtEq, '<=', startLine, startCol);
                    }
                    else if (this.peek() === '<') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.LtLtEq, '<<=', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.LtLt, '<<', startLine, startCol);
                        }
                    }
                    else {
                        this.addToken(TokenKind.Lt, ch, startLine, startCol);
                    }
                    break;
                case '>':
                    if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.GtEq, '>=', startLine, startCol);
                    }
                    else if (this.peek() === '>') {
                        this.advance();
                        if (this.peek() === '>') {
                            this.advance();
                            if (this.peek() === '=') {
                                this.advance();
                                this.addToken(TokenKind.GtGtGtEq, '>>>=', startLine, startCol);
                            }
                            else {
                                this.addToken(TokenKind.GtGtGt, '>>>', startLine, startCol);
                            }
                        }
                        else if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.GtGtEq, '>>=', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.GtGt, '>>', startLine, startCol);
                        }
                    }
                    else {
                        this.addToken(TokenKind.Gt, ch, startLine, startCol);
                    }
                    break;
                case '+':
                    if (this.peek() === '+') {
                        this.advance();
                        this.addToken(TokenKind.PlusPlus, '++', startLine, startCol);
                    }
                    else if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.PlusEq, '+=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Plus, ch, startLine, startCol);
                    }
                    break;
                case '-':
                    if (this.peek() === '-') {
                        this.advance();
                        this.addToken(TokenKind.MinusMinus, '--', startLine, startCol);
                    }
                    else if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.MinusEq, '-=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Minus, ch, startLine, startCol);
                    }
                    break;
                case '*':
                    if (this.peek() === '*') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.StarStarEq, '**=', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.StarStar, '**', startLine, startCol);
                        }
                    }
                    else if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.StarEq, '*=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Star, ch, startLine, startCol);
                    }
                    break;
                case '/':
                    if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.SlashEq, '/=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Slash, ch, startLine, startCol);
                    }
                    break;
                case '%':
                    if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.PercentEq, '%=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Percent, ch, startLine, startCol);
                    }
                    break;
                case '&':
                    if (this.peek() === '&') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.AmpAmpEq, '&&=', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.AmpAmp, '&&', startLine, startCol);
                        }
                    }
                    else if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.AmpEq, '&=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Amp, ch, startLine, startCol);
                    }
                    break;
                case '|':
                    if (this.peek() === '|') {
                        this.advance();
                        if (this.peek() === '=') {
                            this.advance();
                            this.addToken(TokenKind.PipePipeEq, '||=', startLine, startCol);
                        }
                        else {
                            this.addToken(TokenKind.PipePipe, '||', startLine, startCol);
                        }
                    }
                    else if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.PipeEq, '|=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Pipe, ch, startLine, startCol);
                    }
                    break;
                case '^':
                    if (this.peek() === '=') {
                        this.advance();
                        this.addToken(TokenKind.CaretEq, '^=', startLine, startCol);
                    }
                    else {
                        this.addToken(TokenKind.Caret, ch, startLine, startCol);
                    }
                    break;
                default:
                    // Skip unknown characters
                    break;
            }
        }
        this.addToken(TokenKind.EOF, '', this.line, this.col);
    }
    tokenizeTemplate(startLine, startCol, hasHead = false) {
        let str = '';
        while (this.pos < this.source.length) {
            const ch = this.peek();
            if (ch === '`') {
                this.advance();
                this.addToken(hasHead ? TokenKind.TemplateTail : TokenKind.NoSubstTemplate, str, startLine, startCol);
                return;
            }
            if (ch === '$' && this.peek(1) === '{') {
                this.advance();
                this.advance();
                this.addToken(hasHead ? TokenKind.TemplateMiddle : TokenKind.TemplateHead, str, startLine, startCol);
                // Now tokenize the expression until matching }
                let depth = 1;
                while (this.pos < this.source.length && depth > 0) {
                    this.skipWhitespaceAndComments();
                    if (this.pos >= this.source.length)
                        break;
                    const c = this.peek();
                    if (c === '{')
                        depth++;
                    else if (c === '}') {
                        depth--;
                        if (depth === 0) {
                            this.advance();
                            break;
                        }
                    }
                    // Re-tokenize from here (but properly nested)
                    // We need to handle nested braces - let's do it properly
                    const innerStart = this.pos;
                    // We need to tokenize the expression
                    this.tokenizeSingle();
                }
                // Continue with next template segment
                str = '';
                hasHead = true;
                continue;
            }
            if (ch === '\\') {
                this.advance();
                const esc = this.advance();
                switch (esc) {
                    case 'n':
                        str += '\n';
                        break;
                    case 't':
                        str += '\t';
                        break;
                    case 'r':
                        str += '\r';
                        break;
                    case '\\':
                        str += '\\';
                        break;
                    case '`':
                        str += '`';
                        break;
                    case '$':
                        str += '$';
                        break;
                    default:
                        str += esc;
                        break;
                }
            }
            else {
                str += this.advance();
            }
        }
        // Unterminated template
        this.addToken(TokenKind.TemplateTail, str, startLine, startCol);
    }
    tokenizeSingle() {
        this.skipWhitespaceAndComments();
        if (this.pos >= this.source.length)
            return;
        const startLine = this.line;
        const startCol = this.col;
        const ch = this.peek();
        if (ch === '`') {
            this.advance();
            this.tokenizeTemplate(startLine, startCol);
            return;
        }
        if (ch === '}') {
            // Don't consume, will be handled by template tokenizer
            return;
        }
        // Otherwise just tokenize one token
        // This is a simplified approach - consume until we know the token boundaries
        // For correctness, we recursively call back into the main loop
        // by saving state and re-entering
        const savedPos = this.pos;
        const savedLine = this.line;
        const savedCol = this.col;
        const savedLength = this.tokens.length;
        // Actually, let's just handle template string by tokenizing the full expression
        // The TemplateHead/TemplateMiddle/TemplateTail approach is complex.
        // For simplicity, we'll treat template literals specially.
        // The tokenizeTemplate already handles the structure, so we just need to
        // recursively tokenize the expression inside ${...}
        // Since we're in a loop calling tokenizeSingle, we need to be careful.
        // Actually call the main character-level tokenization
        // which is effectively inline from the main tokenize loop
        // Numbers
        if (ch >= '0' && ch <= '9') {
            let num = '';
            while (this.peek() >= '0' && this.peek() <= '9')
                num += this.advance();
            if (this.peek() === '.') {
                num += this.advance();
                while (this.peek() >= '0' && this.peek() <= '9')
                    num += this.advance();
            }
            this.addToken(TokenKind.Number, num, startLine, startCol);
            return;
        }
        if (ch === '"' || ch === "'") {
            this.advance();
            const str = this.readString(ch);
            this.addToken(TokenKind.String, str, startLine, startCol);
            return;
        }
        if (ch === '_' || ch === '$' || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')) {
            let ident = '';
            while (this.pos < this.source.length) {
                const c = this.peek();
                if (c === '_' || c === '$' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) {
                    ident += this.advance();
                }
                else {
                    break;
                }
            }
            const kw = KEYWORDS[ident];
            if (kw !== undefined) {
                this.addToken(kw, ident, startLine, startCol);
            }
            else {
                this.addToken(TokenKind.Identifier, ident, startLine, startCol);
            }
            return;
        }
        // Single char tokens
        this.advance();
        switch (ch) {
            case '(':
                this.addToken(TokenKind.LParen, ch, startLine, startCol);
                break;
            case ')':
                this.addToken(TokenKind.RParen, ch, startLine, startCol);
                break;
            case '[':
                this.addToken(TokenKind.LBracket, ch, startLine, startCol);
                break;
            case ']':
                this.addToken(TokenKind.RBracket, ch, startLine, startCol);
                break;
            case '.':
                this.addToken(TokenKind.Dot, ch, startLine, startCol);
                break;
            case ',':
                this.addToken(TokenKind.Comma, ch, startLine, startCol);
                break;
            case ';':
                this.addToken(TokenKind.Semicolon, ch, startLine, startCol);
                break;
            case ':':
                this.addToken(TokenKind.Colon, ch, startLine, startCol);
                break;
            case '+':
                this.addToken(TokenKind.Plus, ch, startLine, startCol);
                break;
            case '-':
                this.addToken(TokenKind.Minus, ch, startLine, startCol);
                break;
            case '*':
                this.addToken(TokenKind.Star, ch, startLine, startCol);
                break;
            case '/':
                this.addToken(TokenKind.Slash, ch, startLine, startCol);
                break;
            case '%':
                this.addToken(TokenKind.Percent, ch, startLine, startCol);
                break;
            case '!':
                this.addToken(TokenKind.Bang, ch, startLine, startCol);
                break;
            case '=':
                this.addToken(TokenKind.Eq, ch, startLine, startCol);
                break;
            case '<':
                this.addToken(TokenKind.Lt, ch, startLine, startCol);
                break;
            case '>':
                this.addToken(TokenKind.Gt, ch, startLine, startCol);
                break;
            case '&':
                this.addToken(TokenKind.Amp, ch, startLine, startCol);
                break;
            case '|':
                this.addToken(TokenKind.Pipe, ch, startLine, startCol);
                break;
            case '^':
                this.addToken(TokenKind.Caret, ch, startLine, startCol);
                break;
            case '~':
                this.addToken(TokenKind.Tilde, ch, startLine, startCol);
                break;
            case '?':
                this.addToken(TokenKind.Question, ch, startLine, startCol);
                break;
            case '{':
                this.addToken(TokenKind.LBrace, ch, startLine, startCol);
                break;
            default: break;
        }
    }
    // Public interface for the parser
    current() {
        return this.tokens[this.tokenIndex] ?? { kind: TokenKind.EOF, value: '', line: 0, col: 0 };
    }
    peek2(offset = 1) {
        return this.tokens[this.tokenIndex + offset] ?? { kind: TokenKind.EOF, value: '', line: 0, col: 0 };
    }
    consume() {
        const tok = this.tokens[this.tokenIndex];
        if (this.tokenIndex < this.tokens.length - 1) {
            this.tokenIndex++;
        }
        return tok;
    }
    expect(kind) {
        const tok = this.current();
        if (tok.kind !== kind) {
            throw new Error(`Expected ${kind} but got ${tok.kind} ("${tok.value}") at line ${tok.line}:${tok.col}`);
        }
        return this.consume();
    }
    check(kind) {
        return this.current().kind === kind;
    }
    match(...kinds) {
        for (const k of kinds) {
            if (this.current().kind === k) {
                this.consume();
                return true;
            }
        }
        return false;
    }
    getTokens() {
        return this.tokens;
    }
}
exports.Lexer = Lexer;
//# sourceMappingURL=lexer.js.map