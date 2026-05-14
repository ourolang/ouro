"""Lexer tests."""

import pytest
from src.lexer import lex, LexerError
from src.tokens import TokenKind


def kinds(source: str) -> list[TokenKind]:
    return [t.kind for t in lex(source) if t.kind != TokenKind.EOF]


# ── Keywords ──────────────────────────────────────────────────────────────────


def test_keywords():
    src = "var const if else elif match for in return pass break continue loop while fn struct import true false and or not self Self"
    toks = [
        t
        for t in lex(src)
        if t.kind
        not in (TokenKind.NEWLINE, TokenKind.EOF, TokenKind.INDENT, TokenKind.DEDENT)
    ]
    expected = [
        TokenKind.VAR,
        TokenKind.CONST,
        TokenKind.IF,
        TokenKind.ELSE,
        TokenKind.ELIF,
        TokenKind.MATCH,
        TokenKind.FOR,
        TokenKind.IN,
        TokenKind.RETURN,
        TokenKind.PASS,
        TokenKind.BREAK,
        TokenKind.CONTINUE,
        TokenKind.LOOP,
        TokenKind.WHILE,
        TokenKind.FN,
        TokenKind.STRUCT,
        TokenKind.IMPORT,
        TokenKind.TRUE,
        TokenKind.FALSE,
        TokenKind.AND,
        TokenKind.OR,
        TokenKind.NOT,
        TokenKind.SELF_LOWER,
        TokenKind.SELF_UPPER,
    ]
    assert [t.kind for t in toks] == expected


def test_identifier():
    tok = lex("hello")[0]
    assert tok.kind == TokenKind.IDENT
    assert tok.data["name"] == "hello"


def test_underscore_identifier():
    tok = lex("_foo")[0]
    assert tok.kind == TokenKind.IDENT
    assert tok.lexeme == "_foo"


def test_dunder_rejected():
    with pytest.raises(LexerError, match="dunder"):
        lex("__foo")


# ── Integer literals ──────────────────────────────────────────────────────────


def test_int_decimal():
    tok = lex("42")[0]
    assert tok.kind == TokenKind.INT
    assert tok.data["value"] == 42
    assert tok.data["suffix"] is None


def test_int_hex():
    tok = lex("0xFF")[0]
    assert tok.kind == TokenKind.INT
    assert tok.data["value"] == 255


def test_int_binary():
    tok = lex("0b1010")[0]
    assert tok.data["value"] == 0b1010


def test_int_octal():
    tok = lex("0o755")[0]
    assert tok.data["value"] == 0o755


def test_int_suffix():
    tok = lex("42i64")[0]
    assert tok.data["value"] == 42
    assert tok.data["suffix"] == "i64"


def test_int_separators():
    tok = lex("1_000_000")[0]
    assert tok.data["value"] == 1_000_000


# ── Float literals ────────────────────────────────────────────────────────────


def test_float_basic():
    tok = lex("3.14")[0]
    assert tok.kind == TokenKind.FLOAT
    assert abs(tok.data["value"] - 3.14) < 1e-10


def test_float_scientific():
    tok = lex("1.5e-9")[0]
    assert tok.kind == TokenKind.FLOAT
    assert abs(tok.data["value"] - 1.5e-9) < 1e-20


def test_float_suffix():
    tok = lex("3.14f32")[0]
    assert tok.data["suffix"] == "f32"


def test_float_separators():
    tok = lex("3.141_592")[0]
    assert tok.kind == TokenKind.FLOAT


# ── Byte literals ─────────────────────────────────────────────────────────────


def test_byte_char():
    tok = lex("'A'")[0]
    assert tok.kind == TokenKind.BYTE
    assert tok.data["value"] == ord("A")


def test_byte_escape_n():
    tok = lex(r"'\n'")[0]
    assert tok.data["value"] == ord("\n")


def test_byte_escape_hex():
    tok = lex(r"'\x41'")[0]
    assert tok.data["value"] == 0x41


# ── String literals ───────────────────────────────────────────────────────────


def test_string_basic():
    tok = lex('"hello"')[0]
    assert tok.kind == TokenKind.STRING
    assert tok.data["value"] == b"hello"
    assert tok.data["is_multiline"] is False


def test_string_escape():
    tok = lex(r'"hel\nlo"')[0]
    assert tok.data["value"] == b"hel\nlo"


def test_string_multiline():
    tok = lex('"""line1\nline2"""')[0]
    assert tok.kind == TokenKind.STRING
    assert tok.data["is_multiline"] is True
    assert b"line1" in tok.data["value"]


def test_string_unterminated():
    with pytest.raises(LexerError):
        lex('"unterminated')


# ── Operators ─────────────────────────────────────────────────────────────────


def test_operators():
    cases = [
        ("+", TokenKind.PLUS),
        ("-", TokenKind.MINUS),
        ("*", TokenKind.STAR),
        ("/", TokenKind.SLASH),
        ("%", TokenKind.PERCENT),
        ("==", TokenKind.EQ),
        ("!=", TokenKind.NE),
        ("<", TokenKind.LT),
        ("<=", TokenKind.LE),
        (">", TokenKind.GT),
        (">=", TokenKind.GE),
        ("=", TokenKind.ASSIGN),
        ("?=", TokenKind.TYPE_TEST),
        ("|", TokenKind.PIPE),
        ("..", TokenKind.DOT_DOT),
        ("->", TokenKind.ARROW),
        ("?", TokenKind.QUESTION),
    ]
    for src, expected in cases:
        toks = [t for t in lex(src) if t.kind not in (TokenKind.NEWLINE, TokenKind.EOF)]
        assert len(toks) == 1 and toks[0].kind == expected, f"failed for {src!r}"


# ── Indentation ───────────────────────────────────────────────────────────────


def test_indent_dedent():
    src = "a:\n    b\n    c\n"
    ks = kinds(src)
    assert TokenKind.INDENT in ks
    assert TokenKind.DEDENT in ks


def test_newline_suppressed_in_brackets():
    # Newlines inside brackets are suppressed; only the trailing newline after `)` remains
    toks = lex("f(\n    a,\n    b\n)")
    inner = [
        t
        for t in toks
        if t.kind == TokenKind.NEWLINE and t.span.start_line < toks[-2].span.start_line
    ]  # before closing line
    assert inner == [], "newlines inside brackets should be suppressed"


# ── Comments ──────────────────────────────────────────────────────────────────


def test_comment_stripped():
    toks = [
        t
        for t in lex("x  # comment")
        if t.kind not in (TokenKind.NEWLINE, TokenKind.EOF)
    ]
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.IDENT


