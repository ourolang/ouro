"""Token kinds and the Token dataclass for the Ouro lexer."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .nodes import Span


class TokenKind(Enum):
    # Keywords (reserved identifiers — cannot be used as variable names)
    VAR = auto()
    CONST = auto()
    IF = auto()
    ELSE = auto()
    ELIF = auto()
    MATCH = auto()
    FOR = auto()
    IN = auto()
    RETURN = auto()
    PASS = auto()
    BREAK = auto()
    CONTINUE = auto()
    LOOP = auto()
    WHILE = auto()
    FN = auto()
    STRUCT = auto()
    IMPORT = auto()
    ASM = auto()
    TRUE = auto()
    FALSE = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    SELF_LOWER = auto()  # `self`
    SELF_UPPER = auto()  # `Self`
    TYPE = auto()  # `type` — annotation for type aliases (`Foo: type = ...`)
    EXTERN = auto()  # `extern` — declares an externally-linked symbol
    AS = auto()  # `as` — explicit cast operator (`x as i32`)
    LIKE = auto()  # `like` — duck-typed shape constraint (`like[__next__() -> T]`)
    ENUM = auto()  # `enum` — tagged-union sugar (variants without payload)

    # Identifiers (anything else that lexes as a name)
    IDENT = auto()

    # Literals
    INT = auto()  # value: int, suffix: str | None
    FLOAT = auto()  # value: float, suffix: str | None
    BYTE = auto()  # value: int (0..=255)
    STRING = auto()  # value: bytes, is_multiline: bool

    # Operators
    PLUS = auto()  # +
    MINUS = auto()  # -
    STAR = auto()  # *
    SLASH = auto()  # /
    PERCENT = auto()  # %
    EQ = auto()  # ==
    NE = auto()  # !=
    LT = auto()  # <
    LE = auto()  # <=
    GT = auto()  # >
    GE = auto()  # >=
    ASSIGN = auto()  # =
    TYPE_TEST = auto()  # ?=
    PIPE = auto()  # |  (type union; bitwise OR)
    AMP = auto()  # &  (bitwise AND)
    CARET = auto()  # ^  (bitwise XOR)
    SHL = auto()  # <<
    SHR = auto()  # >> — arithmetic on signed, logical on unsigned
    DOT_DOT = auto()  # ..
    ELLIPSIS = auto()  # ... (variadic param marker)
    ARROW = auto()  # ->
    QUESTION = auto()  # ?  (only legal inside brackets per spec)

    # Punctuation
    LPAREN = auto()  # (
    RPAREN = auto()  # )
    LBRACKET = auto()  # [
    RBRACKET = auto()  # ]
    LBRACE = auto()  # {
    RBRACE = auto()  # }
    COLON = auto()  # :
    COMMA = auto()  # ,
    DOT = auto()  # .
    SEMICOLON = auto()  # ;

    # Whitespace-significant tokens
    NEWLINE = auto()  # logical line end (only emitted at bracket_depth == 0)
    INDENT = auto()  # increase in indentation
    DEDENT = auto()  # decrease in indentation
    ASM_BODY = auto()  # raw text of an `asm` decl's body; data["text"]: str

    # End of file
    EOF = auto()


# Map keyword lexemes to their TokenKind.
KEYWORDS: dict[str, TokenKind] = {
    "var": TokenKind.VAR,
    "const": TokenKind.CONST,
    "if": TokenKind.IF,
    "else": TokenKind.ELSE,
    "elif": TokenKind.ELIF,
    "match": TokenKind.MATCH,
    "for": TokenKind.FOR,
    "in": TokenKind.IN,
    "return": TokenKind.RETURN,
    "pass": TokenKind.PASS,
    "break": TokenKind.BREAK,
    "continue": TokenKind.CONTINUE,
    "loop": TokenKind.LOOP,
    "while": TokenKind.WHILE,
    "fn": TokenKind.FN,
    "struct": TokenKind.STRUCT,
    "import": TokenKind.IMPORT,
    "asm": TokenKind.ASM,
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "and": TokenKind.AND,
    "or": TokenKind.OR,
    "not": TokenKind.NOT,
    "self": TokenKind.SELF_LOWER,
    "Self": TokenKind.SELF_UPPER,
    "type": TokenKind.TYPE,
    "extern": TokenKind.EXTERN,
    "as": TokenKind.AS,
    "like": TokenKind.LIKE,
    "enum": TokenKind.ENUM,
}


@dataclass
class Token:
    """A lexed token.

    `lexeme` is the exact source text. `data` carries kind-specific payload:
    - INT:    {"value": int, "suffix": str | None}
    - FLOAT:  {"value": float, "suffix": str | None}
    - BYTE:   {"value": int}                                   (0..=255)
    - STRING: {"value": bytes, "is_multiline": bool}
    - IDENT:  {"name": str}
    - others: {} (no payload)
    """

    kind: TokenKind
    span: Span
    lexeme: str
    data: dict[str, Any]
