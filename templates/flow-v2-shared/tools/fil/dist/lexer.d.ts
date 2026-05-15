export declare enum TokenKind {
    Number = "Number",
    String = "String",
    TemplateHead = "TemplateHead",
    TemplateMiddle = "TemplateMiddle",
    TemplateTail = "TemplateTail",
    NoSubstTemplate = "NoSubstTemplate",
    True = "true",
    False = "false",
    Null = "null",
    Undefined = "undefined",
    Identifier = "Identifier",
    Let = "let",
    Const = "const",
    Function = "function",
    Async = "async",
    Await = "await",
    Return = "return",
    If = "if",
    Else = "else",
    While = "while",
    For = "for",
    Of = "of",
    In = "in",
    Switch = "switch",
    Case = "case",
    Default = "default",
    Break = "break",
    Continue = "continue",
    Try = "try",
    Catch = "catch",
    Finally = "finally",
    Throw = "throw",
    New = "new",
    Typeof = "typeof",
    Instanceof = "instanceof",
    Void = "void",
    Delete = "delete",
    Var = "var",
    LBrace = "{",
    RBrace = "}",
    LParen = "(",
    RParen = ")",
    LBracket = "[",
    RBracket = "]",
    Dot = ".",
    DotDotDot = "...",
    Comma = ",",
    Semicolon = ";",
    Colon = ":",
    Question = "?",
    QuestionDot = "?.",
    QuestionQuestion = "??",
    QuestionQuestionEq = "??=",
    Arrow = "=>",
    Plus = "+",
    Minus = "-",
    Star = "*",
    StarStar = "**",
    Slash = "/",
    Percent = "%",
    Eq = "=",
    EqEq = "==",
    EqEqEq = "===",
    BangEq = "!=",
    BangEqEq = "!==",
    Lt = "<",
    LtEq = "<=",
    Gt = ">",
    GtEq = ">=",
    Bang = "!",
    AmpAmp = "&&",
    PipePipe = "||",
    Amp = "&",
    Pipe = "|",
    Caret = "^",
    Tilde = "~",
    LtLt = "<<",
    GtGt = ">>",
    GtGtGt = ">>>",
    PlusPlus = "++",
    MinusMinus = "--",
    PlusEq = "+=",
    MinusEq = "-=",
    StarEq = "*=",
    StarStarEq = "**=",
    SlashEq = "/=",
    PercentEq = "%=",
    AmpAmpEq = "&&=",
    PipePipeEq = "||=",
    AmpEq = "&=",
    PipeEq = "|=",
    CaretEq = "^=",
    LtLtEq = "<<=",
    GtGtEq = ">>=",
    GtGtGtEq = ">>>=",
    At = "@",
    EOF = "EOF"
}
export interface Token {
    kind: TokenKind;
    value: string;
    line: number;
    col: number;
}
export declare class Lexer {
    private source;
    private pos;
    private line;
    private col;
    private tokens;
    private tokenIndex;
    constructor(source: string);
    private peek;
    private advance;
    private addToken;
    private skipWhitespaceAndComments;
    private readString;
    private tokenize;
    private tokenizeTemplate;
    private tokenizeSingle;
    current(): Token;
    peek2(offset?: number): Token;
    consume(): Token;
    expect(kind: TokenKind): Token;
    check(kind: TokenKind): boolean;
    match(...kinds: TokenKind[]): boolean;
    getTokens(): Token[];
}
//# sourceMappingURL=lexer.d.ts.map