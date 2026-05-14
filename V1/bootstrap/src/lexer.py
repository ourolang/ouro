"""Ouro lexer.

Indentation-aware: emits synthetic INDENT / DEDENT tokens at logical line
starts, and suppresses NEWLINE inside brackets so continuation lines work
naturally.

Maximal-munch: multi-character operators (`?=`, `==`, `..`, `->`, `<=`,
`>=`, `!=`) are recognized as a single token.

Errors are raised as `LexerError`, with a span pointing at the offending
location. No recovery in v1.
"""

from .diagnostics import format_diagnostic
from .nodes import Span
from .tokens import KEYWORDS, Token, TokenKind


class LexerError(Exception):
    """Raised on a lexing failure. `.span` carries the source location."""

    def __init__(self, message: str, span: Span):
        super().__init__(message)
        self.span = span

    def __str__(self) -> str:
        return format_diagnostic(self.args[0], self.span)


# Characters that legally start an identifier.
def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


# Characters that may appear after the first character of an identifier.
def _is_ident_cont(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


# Single ASCII escape sequences in byte and string literals.
_SIMPLE_ESCAPES: dict[str, int] = {
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "0": 0x00,
    "\\": 0x5C,
    "'": 0x27,
    '"': 0x22,
}


class Lexer:
    """Stateful lexer; call `lex()` once and consume the resulting list."""

    def __init__(self, source: str, file: str = "<input>"):
        self.source = source
        self.file = file
        self.pos = 0
        self.line = 1
        self.col = 1

        # INDENT/DEDENT tracking.
        self.indent_stack: list[int] = [0]

        # Bracket nesting — while > 0, NEWLINE tokens are suppressed and
        # indentation at line starts is ignored (continuation behavior).
        self.bracket_depth = 0

        self.tokens: list[Token] = []
        self.at_line_start = True  # processing the start of a logical line

        # `asm` decl tracking.  We enter `_in_asm_decl` when we tokenize
        # the `asm` keyword at the start of a logical line.  When we see
        # a COLON at bracket_depth 0 while in this state, the next
        # logical line is captured raw as an ASM_BODY token instead of
        # being tokenized.  See _handle_line_start.
        self._in_asm_decl: bool = False
        self._asm_decl_indent: int = 0  # indent level of the asm header
        self._asm_body_pending: bool = False  # next indented line starts the body

    # ─── Public entry point ─────────────────────────────────────────────────

    def lex(self) -> list[Token]:
        while not self._at_eof():
            if self.at_line_start and self.bracket_depth == 0:
                self._handle_line_start()
                continue

            ch = self._peek()

            if ch == "\n":
                self._handle_newline()
            elif ch in " \t":
                self._advance()
            elif ch == "#":
                self._skip_line_comment()
            elif _is_ident_start(ch):
                self._lex_identifier_or_keyword()
            elif ch.isdigit():
                self._lex_number()
            elif ch == '"':
                self._lex_string()
            elif ch == "'":
                self._lex_byte()
            else:
                self._lex_operator_or_punct()

        # Close out any open indentation levels.
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self._emit_synthetic(TokenKind.DEDENT)

        # Final NEWLINE for the trailing line, if not already emitted.
        if self.tokens and self.tokens[-1].kind not in (
            TokenKind.NEWLINE,
            TokenKind.DEDENT,
        ):
            self._emit_synthetic(TokenKind.NEWLINE)

        self._emit_synthetic(TokenKind.EOF)
        return self.tokens

    # ─── Position helpers ──────────────────────────────────────────────────

    def _at_eof(self) -> bool:
        return self.pos >= len(self.source)

    def _peek(self, offset: int = 0) -> str:
        i = self.pos + offset
        if i >= len(self.source):
            return ""
        return self.source[i]

    def _advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _here(self) -> tuple[int, int]:
        return (self.line, self.col)

    def _span(self, start_line: int, start_col: int) -> Span:
        return Span(self.file, start_line, start_col, self.line, self.col)

    # ─── Indentation / line management ─────────────────────────────────────

    def _handle_line_start(self):
        """At the start of a logical line, measure indent and emit INDENT/DEDENT."""

        # Skip blank and comment-only lines without changing indent state.
        scan = self.pos
        while scan < len(self.source) and self.source[scan] in " \t":
            scan += 1
        if scan >= len(self.source) or self.source[scan] == "\n":
            # Blank line — let the main loop consume it as whitespace + newline.
            self.at_line_start = False
            return
        if self.source[scan] == "#":
            # Comment-only line — let the main loop consume the comment + newline.
            self.at_line_start = False
            return

        # Real content: measure indent column (count of leading spaces).
        indent = 0
        while self.pos < len(self.source) and self.source[self.pos] in " \t":
            ch = self.source[self.pos]
            if ch == "\t":
                # Reject tabs to keep semantics unambiguous (Python 3 also forbids
                # mixing tabs and spaces; we go further and disallow tabs entirely
                # for now). Revisit when there's a concrete reason.
                raise LexerError(
                    "tabs are not allowed for indentation; use spaces",
                    self._span(self.line, self.col),
                )
            indent += 1
            self._advance()

        current = self.indent_stack[-1]
        if indent > current:
            self.indent_stack.append(indent)
            self._emit_synthetic(TokenKind.INDENT)
            # If this INDENT opens an asm decl's body, capture all
            # subsequent lines verbatim until indentation drops back
            # to (or below) the asm header's level.  Emits one
            # ASM_BODY token, then the appropriate DEDENT.
            if self._asm_body_pending:
                self._capture_asm_body(body_indent=indent)
                return
        else:
            while indent < self.indent_stack[-1]:
                self.indent_stack.pop()
                self._emit_synthetic(TokenKind.DEDENT)
            if indent != self.indent_stack[-1]:
                raise LexerError(
                    f"inconsistent indentation: {indent} does not match any open level "
                    f"{self.indent_stack}",
                    self._span(self.line, self.col),
                )

        self.at_line_start = False

    def _capture_asm_body(self, body_indent: int) -> None:
        """Consume source lines verbatim as the body of an `asm` decl.

        Already at the first non-whitespace character of the body's
        first line; the leading body indentation has been stripped by
        `_handle_line_start`.  We collect lines until we hit a line
        whose indentation is at or below the `asm` header's indent.

        On exit:
          - One ASM_BODY token has been emitted carrying every body
            line (each with leading body_indent spaces stripped).
          - The pop-back-to-header DEDENT has been emitted.
          - State flags are cleared.
        """
        start_line, start_col = self.line, self.col
        body_lines: list[str] = []
        # We're currently positioned at the first source char of the
        # first body line.  Back up to that line's start so we can grab
        # the line wholesale.
        line_start_pos = self.pos - (self.col - 1)

        while line_start_pos < len(self.source):
            # Find end of this line.
            line_end = line_start_pos
            while line_end < len(self.source) and self.source[line_end] != "\n":
                line_end += 1
            raw = self.source[line_start_pos:line_end]

            # Blank / whitespace-only line: include it as empty.
            stripped = raw.lstrip(" ")
            if stripped == "" or stripped.startswith("\n"):
                body_lines.append("")
            else:
                # Measure this line's indentation.
                line_indent = len(raw) - len(raw.lstrip(" "))
                if line_indent < body_indent:
                    # End of body — back out to the start of this line.
                    break
                body_lines.append(raw[body_indent:])

            # Advance past this line.
            if line_end < len(self.source):
                line_end += 1  # consume the '\n'
            # Update lexer position + line/col bookkeeping.
            self.pos = line_end
            self.line += 1
            self.col = 1
            line_start_pos = line_end

        end_line, end_col = self.line, self.col
        body_text = "\n".join(body_lines)

        # Emit the ASM_BODY token spanning the captured region.
        self.tokens.append(
            Token(
                kind=TokenKind.ASM_BODY,
                span=Span(self.file, start_line, start_col, end_line, end_col),
                lexeme=body_text,
                data={"text": body_text},
            )
        )
        # The next iteration of the main loop will re-enter
        # _handle_line_start at the line that ended the body, which
        # will emit the DEDENT(s) naturally.
        self._asm_body_pending = False
        self._in_asm_decl = False
        self.at_line_start = True

    def _handle_newline(self):
        """A `\\n` outside any bracket ends the logical line."""

        start_line, start_col = self._here()
        self._advance()  # consume \n
        if self.bracket_depth == 0:
            # Suppress NEWLINE if the previous token was already NEWLINE / INDENT / DEDENT
            # (avoids spurious blank-line newlines).
            if self.tokens and self.tokens[-1].kind not in (
                TokenKind.NEWLINE,
                TokenKind.INDENT,
                TokenKind.DEDENT,
            ):
                self.tokens.append(
                    Token(
                        kind=TokenKind.NEWLINE,
                        span=Span(
                            self.file, start_line, start_col, self.line, self.col
                        ),
                        lexeme="\n",
                        data={},
                    )
                )
            self.at_line_start = True

    def _skip_line_comment(self):
        # Consume `#` and everything up to (but not including) the newline.
        while not self._at_eof() and self._peek() != "\n":
            self._advance()

    # ─── Tokenizers ────────────────────────────────────────────────────────

    def _lex_identifier_or_keyword(self):
        start_line, start_col = self._here()
        start_pos = self.pos
        self._advance()  # first char (already known to be ident-start)
        while not self._at_eof() and _is_ident_cont(self._peek()):
            self._advance()
        lexeme = self.source[start_pos : self.pos]

        # Reject `__name` (double leading without trailing `__`).
        if lexeme.startswith("__") and not lexeme.endswith("__"):
            raise LexerError(
                f"ambiguous naming convention: `{lexeme}` — "
                f"use `_name` for private or `__name__` for dunder",
                self._span(start_line, start_col),
            )

        kind = KEYWORDS.get(lexeme, TokenKind.IDENT)
        data: dict = {"name": lexeme} if kind == TokenKind.IDENT else {}
        self.tokens.append(
            Token(
                kind=kind,
                span=self._span(start_line, start_col),
                lexeme=lexeme,
                data=data,
            )
        )

        # Mark that we're now inside an asm decl header.  The signature
        # tokens lex normally; once we see the trailing COLON at
        # bracket_depth 0, we flip to body-capture mode.
        if kind == TokenKind.ASM:
            self._in_asm_decl = True
            # Remember the indent level at which the header sits — the
            # body must end when indentation drops back to (or below) it.
            self._asm_decl_indent = self.indent_stack[-1]

    def _lex_number(self):
        """Lex an integer or float literal with optional suffix.

        Supports decimal, 0x hex, 0b binary, 0o octal for integers; decimal
        with optional `.frac` and/or `eEXP` for floats. Underscores are
        permitted as digit separators. A trailing identifier (e.g. `i64`,
        `u8`, `f32`) is captured as the numeric suffix.
        """

        start_line, start_col = self._here()
        start_pos = self.pos

        is_float = False
        radix = 10

        # Detect radix prefixes.
        if self._peek() == "0" and self._peek(1) in ("x", "b", "o"):
            self._advance()  # 0
            prefix = self._advance()  # x, b, or o
            radix = {"x": 16, "b": 2, "o": 8}[prefix]
            digits_start = self.pos
            while not self._at_eof() and (self._peek() in "0123456789abcdefABCDEF_"):
                self._advance()
            if self.pos == digits_start:
                raise LexerError(
                    f"empty {prefix}-prefixed numeric literal",
                    self._span(start_line, start_col),
                )
            digits = self.source[digits_start : self.pos].replace("_", "")
        else:
            # Decimal integer or float.
            while not self._at_eof() and (
                self._peek().isdigit() or self._peek() == "_"
            ):
                self._advance()

            # Fractional part.
            if self._peek() == "." and self._peek(1).isdigit():
                is_float = True
                self._advance()  # .
                while not self._at_eof() and (
                    self._peek().isdigit() or self._peek() == "_"
                ):
                    self._advance()

            # Exponent.
            if self._peek() in ("e", "E"):
                is_float = True
                self._advance()  # e or E
                if self._peek() in ("+", "-"):
                    self._advance()
                exp_start = self.pos
                while not self._at_eof() and (
                    self._peek().isdigit() or self._peek() == "_"
                ):
                    self._advance()
                if self.pos == exp_start:
                    raise LexerError(
                        "missing exponent digits",
                        self._span(start_line, start_col),
                    )

            digits = self.source[start_pos : self.pos].replace("_", "")

        # Optional suffix: an identifier-ish run immediately following the digits.
        suffix: str | None = None
        if not self._at_eof() and _is_ident_start(self._peek()):
            sfx_start = self.pos
            while not self._at_eof() and _is_ident_cont(self._peek()):
                self._advance()
            suffix = self.source[sfx_start : self.pos]
            # Float suffix (e.g. `f32`) implies the literal is a float even without `.` or `e`.
            if suffix.startswith("f"):
                is_float = True

        lexeme = self.source[start_pos : self.pos]
        span = self._span(start_line, start_col)

        if is_float:
            try:
                value = float(digits)
            except ValueError as exc:
                raise LexerError(
                    f"invalid float literal `{lexeme}`: {exc}", span
                ) from exc
            self.tokens.append(
                Token(
                    kind=TokenKind.FLOAT,
                    span=span,
                    lexeme=lexeme,
                    data={"value": value, "suffix": suffix},
                )
            )
        else:
            try:
                value = int(digits, radix)
            except ValueError as exc:
                raise LexerError(
                    f"invalid integer literal `{lexeme}`: {exc}", span
                ) from exc
            self.tokens.append(
                Token(
                    kind=TokenKind.INT,
                    span=span,
                    lexeme=lexeme,
                    data={"value": value, "suffix": suffix},
                )
            )

    def _lex_string(self):
        """Lex `"..."` or `\"\"\"...\"\"\"`.

        Escape sequences are processed at lex time; the token's `value` is
        a `bytes` object containing the UTF-8 representation of the
        unescaped string content.
        """

        start_line, start_col = self._here()
        start_pos = self.pos

        # Distinguish triple-quoted from single-quoted.
        if self._peek(1) == '"' and self._peek(2) == '"':
            self._advance()  # "
            self._advance()  # "
            self._advance()  # "
            content = self._read_string_body(
                triple=True, start_line=start_line, start_col=start_col
            )
            is_multiline = True
        else:
            self._advance()  # "
            content = self._read_string_body(
                triple=False, start_line=start_line, start_col=start_col
            )
            is_multiline = False

        span = self._span(start_line, start_col)
        self.tokens.append(
            Token(
                kind=TokenKind.STRING,
                span=span,
                lexeme=self.source[start_pos : self.pos],
                data={"value": content, "is_multiline": is_multiline},
            )
        )

    def _read_string_body(self, triple: bool, start_line: int, start_col: int) -> bytes:
        out = bytearray()
        while True:
            if self._at_eof():
                raise LexerError(
                    "unterminated string literal",
                    self._span(start_line, start_col),
                )
            ch = self._peek()
            if triple:
                if ch == '"' and self._peek(1) == '"' and self._peek(2) == '"':
                    self._advance()
                    self._advance()
                    self._advance()
                    return bytes(out)
            else:
                if ch == '"':
                    self._advance()
                    return bytes(out)
                if ch == "\n":
                    raise LexerError(
                        "unterminated string literal: newline in single-quoted string",
                        self._span(start_line, start_col),
                    )

            if ch == "\\":
                out.extend(
                    self._read_escape(
                        in_string=True, start_line=start_line, start_col=start_col
                    )
                )
            else:
                self._advance()
                out.extend(ch.encode("utf-8"))

    def _lex_byte(self):
        """Lex `'A'` — a single-byte literal (u8 0..=255)."""

        start_line, start_col = self._here()
        start_pos = self.pos
        self._advance()  # opening '

        if self._at_eof():
            raise LexerError(
                "unterminated byte literal", self._span(start_line, start_col)
            )

        ch = self._peek()
        if ch == "\\":
            byte_val = self._read_escape(
                in_string=False, start_line=start_line, start_col=start_col
            )
            if len(byte_val) != 1:
                raise LexerError(
                    "byte literal escape must produce exactly one byte",
                    self._span(start_line, start_col),
                )
            value = byte_val[0]
        else:
            self._advance()
            encoded = ch.encode("utf-8")
            if len(encoded) != 1:
                raise LexerError(
                    "byte literal must contain a single ASCII byte; "
                    "use a string literal for multi-byte content",
                    self._span(start_line, start_col),
                )
            value = encoded[0]

        if self._peek() != "'":
            raise LexerError(
                "byte literal not closed by `'`; only one byte allowed",
                self._span(start_line, start_col),
            )
        self._advance()  # closing '

        self.tokens.append(
            Token(
                kind=TokenKind.BYTE,
                span=self._span(start_line, start_col),
                lexeme=self.source[start_pos : self.pos],
                data={"value": value},
            )
        )

    def _read_escape(self, in_string: bool, start_line: int, start_col: int) -> bytes:
        """Process a `\\X` escape, advancing past it. Returns the unescaped bytes."""

        self._advance()  # consume backslash
        if self._at_eof():
            raise LexerError(
                "unterminated escape sequence",
                self._span(start_line, start_col),
            )
        ch = self._advance()
        if ch in _SIMPLE_ESCAPES:
            return bytes([_SIMPLE_ESCAPES[ch]])
        if ch == "x":
            # Exactly two hex digits.
            hex_chars = ""
            for _ in range(2):
                if self._at_eof() or self._peek() not in "0123456789abcdefABCDEF":
                    raise LexerError(
                        "`\\xNN` requires exactly two hex digits",
                        self._span(start_line, start_col),
                    )
                hex_chars += self._advance()
            return bytes([int(hex_chars, 16)])
        if ch == "u":
            if not in_string:
                raise LexerError(
                    "`\\u{...}` is not legal in byte literals (a codepoint may span multiple bytes)",
                    self._span(start_line, start_col),
                )
            if self._peek() != "{":
                raise LexerError(
                    "`\\u` must be followed by `{HEX}`",
                    self._span(start_line, start_col),
                )
            self._advance()  # {
            hex_chars = ""
            while not self._at_eof() and self._peek() != "}":
                hex_chars += self._advance()
            if self._peek() != "}":
                raise LexerError(
                    "unterminated `\\u{...}` escape",
                    self._span(start_line, start_col),
                )
            self._advance()  # }
            try:
                cp = int(hex_chars, 16)
                return chr(cp).encode("utf-8")
            except ValueError as exc:
                raise LexerError(
                    f"invalid `\\u{{{hex_chars}}}` codepoint",
                    self._span(start_line, start_col),
                ) from exc
        raise LexerError(
            f"unknown escape sequence `\\{ch}`",
            self._span(start_line, start_col),
        )

    def _lex_operator_or_punct(self):
        start_line, start_col = self._here()
        ch = self._peek()
        # Three-char: `...` ellipsis (variadic marker).
        if self._peek() == "." and self._peek(1) == "." and self._peek(2) == ".":
            self._advance()
            self._advance()
            self._advance()
            self.tokens.append(
                Token(
                    kind=TokenKind.ELLIPSIS,
                    span=self._span(start_line, start_col),
                    lexeme="...",
                    data={},
                )
            )
            return
        # Two-char operators (try longest first).
        two = self._peek() + self._peek(1)

        # Two-char operators.
        two_char_map: dict[str, TokenKind] = {
            "==": TokenKind.EQ,
            "!=": TokenKind.NE,
            "<=": TokenKind.LE,
            ">=": TokenKind.GE,
            "?=": TokenKind.TYPE_TEST,
            "..": TokenKind.DOT_DOT,
            "->": TokenKind.ARROW,
            "<<": TokenKind.SHL,
            ">>": TokenKind.SHR,
        }
        if two in two_char_map:
            self._advance()
            self._advance()
            self.tokens.append(
                Token(
                    kind=two_char_map[two],
                    span=self._span(start_line, start_col),
                    lexeme=two,
                    data={},
                )
            )
            return

        # Single-char operators and punctuation.
        single_char_map: dict[str, TokenKind] = {
            "+": TokenKind.PLUS,
            "-": TokenKind.MINUS,
            "*": TokenKind.STAR,
            "/": TokenKind.SLASH,
            "%": TokenKind.PERCENT,
            "<": TokenKind.LT,
            ">": TokenKind.GT,
            "=": TokenKind.ASSIGN,
            "|": TokenKind.PIPE,
            "&": TokenKind.AMP,
            "^": TokenKind.CARET,
            "?": TokenKind.QUESTION,
            "(": TokenKind.LPAREN,
            ")": TokenKind.RPAREN,
            "[": TokenKind.LBRACKET,
            "]": TokenKind.RBRACKET,
            "{": TokenKind.LBRACE,
            "}": TokenKind.RBRACE,
            ":": TokenKind.COLON,
            ",": TokenKind.COMMA,
            ".": TokenKind.DOT,
            ";": TokenKind.SEMICOLON,
        }
        if ch in single_char_map:
            self._advance()
            kind = single_char_map[ch]
            if kind in (TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.LBRACE):
                self.bracket_depth += 1
            elif kind in (TokenKind.RPAREN, TokenKind.RBRACKET, TokenKind.RBRACE):
                if self.bracket_depth == 0:
                    raise LexerError(
                        f"unmatched `{ch}`",
                        self._span(start_line, start_col),
                    )
                self.bracket_depth -= 1
            self.tokens.append(
                Token(
                    kind=kind,
                    span=self._span(start_line, start_col),
                    lexeme=ch,
                    data={},
                )
            )
            # COLON at top-level inside an asm decl header marks the
            # transition: the next indented block is the body.
            if (
                kind == TokenKind.COLON
                and self.bracket_depth == 0
                and self._in_asm_decl
            ):
                self._asm_body_pending = True
            return

        raise LexerError(
            f"unexpected character `{ch}` (U+{ord(ch):04X})",
            self._span(start_line, start_col),
        )

    def _emit_synthetic(self, kind: TokenKind):
        """Emit a synthetic INDENT/DEDENT/NEWLINE/EOF at the current position."""
        self.tokens.append(
            Token(
                kind=kind,
                span=self._span(self.line, self.col),
                lexeme="",
                data={},
            )
        )


def lex(source: str, file: str = "<input>") -> list[Token]:
    """Convenience entry point: lex a complete source string into a token list."""
    return Lexer(source, file).lex()
