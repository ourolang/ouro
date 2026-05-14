# `src/lexer.py` — source bytes → tokens

> Stateful scanner that consumes a source string and produces a flat
> list of `Token` values, including synthetic `INDENT` / `DEDENT` /
> `NEWLINE` tokens. After this pass, the parser never has to think
> about whitespace, indentation, or escape sequences.

## What the lexer is responsible for

In a compiler that takes whitespace seriously, the lexer absorbs
**three orthogonal concerns** that would otherwise leak into the parser:

1. **Tokenization proper** — recognize keywords, identifiers, numeric
   and string literals, operators.
2. **Indentation** — turn changes in leading whitespace into explicit
   `INDENT` and `DEDENT` tokens.
3. **Escape processing** — `"\n"` becomes a real newline character at
   lex time, not at parse time. The parser sees `bytes` payloads
   ready for use.

If any of these leaked into the parser, the parser would be much
harder to read and test. The whole point of a separate lex pass is to
make the parser's job purely structural.

## Architecture: stateful scanner in one class

```python
class Lexer:
    def __init__(self, source, file):
        self.source = source           # the entire input string
        self.file = file               # for error messages
        self.pos = 0                   # cursor into source
        self.line = 1; self.col = 1    # current line/column (1-indexed)
        self.indent_stack = [0]        # open indentation levels
        self.bracket_depth = 0         # nesting of (), [], {}
        self.tokens = []               # output list
        self.at_line_start = True      # process indent on next iteration
```

Single use — the `lex()` method walks the source from start to finish,
consumes the cursor, returns the tokens. Don't call it twice; create
a new `Lexer`.

The module-level convenience function `lex(source, file)` (line 626)
wraps construction + invocation. That's the public API.

## The main loop (lines 70-108)

```python
def lex(self):
    while not self._at_eof():
        if self.at_line_start and self.bracket_depth == 0:
            self._handle_line_start()    # might emit INDENT or DEDENT(s)
            continue

        ch = self._peek()

        if ch == "\n":           self._handle_newline()
        elif ch in " \t":        self._advance()         # skip
        elif ch == "#":          self._skip_line_comment()
        elif _is_ident_start(ch): self._lex_identifier_or_keyword()
        elif ch.isdigit():       self._lex_number()
        elif ch == '"':          self._lex_string()
        elif ch == "'":          self._lex_byte()
        else:                    self._lex_operator_or_punct()

    # cleanup at EOF
    while len(self.indent_stack) > 1:
        self.indent_stack.pop(); self._emit_synthetic(TokenKind.DEDENT)
    if self.tokens and self.tokens[-1].kind not in (NEWLINE, DEDENT):
        self._emit_synthetic(TokenKind.NEWLINE)
    self._emit_synthetic(TokenKind.EOF)
    return self.tokens
```

This is **dispatch by first character**. No regex, no DFA, just a
small ladder of conditionals. Every branch consumes at least one
character (or commits to a multi-char tokenizer), so the loop always
makes progress.

The cleanup at the end is important: at EOF we may still have
`indent_stack > [0]` (because the source ended without dedenting), so
we synthesize the necessary `DEDENT`s. We also ensure there's a final
`NEWLINE` so the parser's "loop until you see NEWLINE" patterns
terminate. Then `EOF` caps the stream.

## Cursor management (lines 110-135)

```python
def _peek(self, offset=0):
    i = self.pos + offset
    return "" if i >= len(self.source) else self.source[i]

def _advance(self):
    ch = self.source[self.pos]
    self.pos += 1
    if ch == "\n":
        self.line += 1; self.col = 1
    else:
        self.col += 1
    return ch
```

`_peek()` is non-destructive lookahead with a configurable offset
(used in 2- and 3-char operator detection). It returns `""` past
EOF rather than raising — the dispatch ladder treats `""` like
"unknown character," safely fizzling out.

`_advance()` consumes one character and **maintains line/column
state**. This is the *only* place `line` and `col` change. If you
ever add a tokenizer that bumps `pos` without going through
`_advance`, line numbers will be wrong everywhere downstream.

`_here()` returns the current `(line, col)` tuple. `_span(start_line,
start_col)` builds a `Span` from the given start to the current
position. These two together are how every emitter constructs span
information for its tokens.

## Indentation handling — the heart of the lexer

This is the most subtle part. Two methods cooperate: `_handle_line_start`
and `_handle_newline`.

### `_handle_line_start` (lines 139-185)

Called only at `at_line_start and bracket_depth == 0`. Three sub-cases:

**1. Blank or comment-only line** (lines 144-153)

```python
scan = self.pos
while scan < len(self.source) and self.source[scan] in " \t":
    scan += 1
if scan >= len(self.source) or self.source[scan] == "\n":
    self.at_line_start = False; return        # blank line
if self.source[scan] == "#":
    self.at_line_start = False; return        # comment-only
```

We **peek** ahead without consuming. If the line has nothing but
whitespace + (newline | comment), we drop the line-start flag and
fall back into the main loop. The main loop will then consume the
whitespace as a no-op, the comment to end-of-line, and the `\n` as a
`NEWLINE` (which suppresses subsequent NEWLINEs because the previous
token was already a NEWLINE). **No INDENT/DEDENT is emitted for blank
lines.** This matters: a blank line shouldn't reset block structure.

**2. Tabs are forbidden** (lines 159-166)

```python
if ch == "\t":
    raise LexerError("tabs are not allowed for indentation; use spaces", ...)
```

Python 3's mixed tab/space rules are subtle and historically a source
of bugs. The Ouro lexer takes the strictest position: tabs error out
at the first occurrence. If users want tab-indented code, they can use
their editor to convert before saving.

**3. Real content — measure indent, compare to stack** (lines 170-183)

```python
current = self.indent_stack[-1]
if indent > current:
    self.indent_stack.append(indent)
    self._emit_synthetic(TokenKind.INDENT)
else:
    while indent < self.indent_stack[-1]:
        self.indent_stack.pop()
        self._emit_synthetic(TokenKind.DEDENT)
    if indent != self.indent_stack[-1]:
        raise LexerError("inconsistent indentation: ...", ...)
```

This is the **classic Python indentation algorithm**:

- Greater indent → push, emit one `INDENT`.
- Less indent → pop *while bigger*, emit one `DEDENT` per pop.
- After popping, the indent has to match an existing level exactly.
  If it doesn't (e.g. dedenting to a column between two open levels),
  it's a hard error.

The invariant: after handling a line start, `indent_stack[-1]` is the
column we're currently at, and we've emitted exactly the
INDENT/DEDENT count needed to bring the parser's view in sync.

### `_handle_newline` (lines 187-210)

```python
def _handle_newline(self):
    start_line, start_col = self._here()
    self._advance()   # consume \n
    if self.bracket_depth == 0:
        if self.tokens and self.tokens[-1].kind not in (NEWLINE, INDENT, DEDENT):
            self.tokens.append(Token(NEWLINE, span=..., lexeme="\n", data={}))
        self.at_line_start = True
```

Two important points:

**1. Bracket depth suppresses newlines.** If we're inside `(`, `[`, or
`{`, the `\n` advances position but emits no token. This is what makes
this work without continuation characters:

```ouro
io.printf(
    "hello, %s\nyou are %d\n",
    name,
    age,
)
```

The lexer sees this as one logical line. The parser never has to deal
with line continuation.

**2. Don't emit a NEWLINE after another NEWLINE/INDENT/DEDENT.** Empty
lines or block boundaries already have a logical end-of-line marker;
we don't double up. This prevents spurious empty-statement issues in
the parser.

After emitting (or not), we set `at_line_start = True` so the next
iteration of the main loop runs `_handle_line_start`.

## Identifier / keyword tokenizer (lines 219-244)

```python
def _lex_identifier_or_keyword(self):
    start = self._here_pos()
    self._advance()  # already known to be ident-start
    while not self._at_eof() and _is_ident_cont(self._peek()):
        self._advance()
    lexeme = self.source[start:self.pos]

    # Reject `__name` (double leading without trailing `__`).
    if lexeme.startswith("__") and not lexeme.endswith("__"):
        raise LexerError(
            f"ambiguous naming convention: `{lexeme}` — "
            "use `_name` for private or `__name__` for dunder", ...)

    kind = KEYWORDS.get(lexeme, TokenKind.IDENT)
    data = {"name": lexeme} if kind == TokenKind.IDENT else {}
    self.tokens.append(Token(kind, span, lexeme, data))
```

The dunder rule is **enforced at the earliest possible point**.
`_foo` is allowed (private). `__foo__` is allowed (dunder). `__foo`
is rejected (ambiguous). This decision happens here, in the lexer,
because at later passes the distinction is harder to make cleanly.

Keyword detection is `KEYWORDS.get(lexeme, IDENT)`: the dict-lookup
trick. No special-casing per keyword.

## Numeric literals (lines 246-353)

The longest tokenizer in the file because it has to handle:

- **Four radixes**: decimal, `0x` hex, `0b` binary, `0o` octal
- **Floats**: integer.fraction, optional `e`/`E` exponent
- **Suffixes**: `i64`, `u8`, `f32`, etc.
- **Underscores** as visual separators: `1_000_000`, `0xFF_FF`

Pseudocode:

```
if peek == '0' and peek(1) in 'xbo':
    consume prefix; collect [0-9a-fA-F_]+; set radix
else:
    collect digits + underscores
    if peek == '.' and peek(1).isdigit():
        is_float = True
        consume '.'; collect more digits
    if peek in 'eE':
        is_float = True
        consume 'e'; optional sign; collect digits
        require at least one digit

# optional suffix
if peek is ident-start:
    collect identifier; if starts with 'f', force is_float

emit FLOAT or INT with stripped digits and suffix
```

A few details worth flagging:

**Underscores stripped before parsing** (line 274, 308):

```python
digits = self.source[digits_start:self.pos].replace("_", "")
```

Python's `int(s, radix)` and `float(s)` don't accept underscores
(actually Python 3 does, but we strip preemptively). This means
`1_000` and `1000` produce identical tokens.

**Float-by-suffix promotion** (lines 318-319):

```python
if suffix.startswith("f"):
    is_float = True
```

`42f32` parses as a float (42.0), not as an int with a suffix. This
matters because the AST distinguishes `IntLiteral` from `FloatLiteral`
— the suffix has to be inspected before deciding which node kind to
emit.

**The `.` lookahead trick** (line 283):

```python
if self._peek() == "." and self._peek(1).isdigit():
```

We require a digit after the dot. Why? Because `42.method()` is field
access on the integer 42 (or, more honestly, will be when we add such
a thing). Without this check, the lexer would consume `42.` as a
malformed float. Two-char lookahead resolves the ambiguity cleanly.

## String literals (lines 355-425)

```python
def _lex_string(self):
    if self._peek(1) == '"' and self._peek(2) == '"':
        # triple-quoted
        ...
    else:
        # single-quoted
        ...
```

Three-char lookahead distinguishes `"""..."""` from `"..."`. The body
parser is shared:

```python
def _read_string_body(self, triple, ...):
    out = bytearray()
    while True:
        if EOF: error
        ch = peek
        if triple and ch + peek(1) + peek(2) == '"""': consume; return bytes(out)
        if not triple and ch == '"': consume; return bytes(out)
        if not triple and ch == '\n': error  # newline in single-quoted

        if ch == '\\':
            out.extend(self._read_escape(in_string=True, ...))
        else:
            self._advance()
            out.extend(ch.encode("utf-8"))
```

The output is `bytes`. UTF-8 encoding happens at lex time. This means
even if the source file is UTF-8, the string token already carries
the encoded bytes — the parser doesn't need to think about character
encoding.

## Byte literals (lines 427-475)

```python
def _lex_byte(self):
    self._advance()  # opening '
    ch = self._peek()
    if ch == '\\':
        byte_val = self._read_escape(in_string=False, ...)
        if len(byte_val) != 1: error  # escape produced multiple bytes
        value = byte_val[0]
    else:
        self._advance()
        encoded = ch.encode("utf-8")
        if len(encoded) != 1: error  # multi-byte char in byte literal
        value = encoded[0]
    if peek != "'": error
    self._advance()
    emit BYTE with value
```

Strict invariant: a byte literal contains exactly one byte (u8 0-255).
Multi-byte UTF-8 characters error out. `\u{...}` escapes are also
forbidden in byte literals (line 500-505) because a codepoint may
span multiple bytes.

This means `'A'` is fine, `'\n'` is fine, `'\xFF'` is fine, but
`'é'` (which is 2 bytes in UTF-8) is rejected with a clear error.

## Escape processing (lines 477-532)

The full escape table:

```python
_SIMPLE_ESCAPES = {
    "n": 0x0A, "r": 0x0D, "t": 0x09, "0": 0x00,
    "\\": 0x5C, "'": 0x27, '"': 0x22,
}
```

Plus:

- `\xNN` — exactly two hex digits, one byte
- `\u{HEX}` — Unicode codepoint, expanded to its UTF-8 bytes
  (string-only)

Anything else is `unknown escape sequence`. There's deliberately no
`\v`, `\f`, `\b` etc. — those are rarely used and easy to add later.

`_read_escape` is shared between strings and byte literals via the
`in_string: bool` flag. Byte literals reject `\u{...}`.

## Operator and punctuation tokenizer (lines 534-612)

```python
def _lex_operator_or_punct(self):
    ch = self._peek()
    two = self._peek() + self._peek(1)

    # 2-char operators (try first)
    if two in two_char_map: emit; return

    # 1-char operators
    if ch in single_char_map:
        ...
        # bracket-depth bookkeeping
        if kind in (LPAREN, LBRACKET, LBRACE): self.bracket_depth += 1
        elif kind in (RPAREN, RBRACKET, RBRACE):
            if self.bracket_depth == 0: error("unmatched ...")
            self.bracket_depth -= 1
        emit; return

    error("unexpected character")
```

The two-char-first ordering is **maximal munch**: when both `=` and
`==` could match, the longer one wins. Without this we'd lex `==` as
two `ASSIGN` tokens.

The bracket-depth bookkeeping is what enables `_handle_newline`'s
suppression behavior. Note: an unmatched closing bracket errors *at
lex time*. An unmatched opening bracket isn't caught here — but it
leaves `bracket_depth > 0` at EOF, which means newlines keep being
suppressed and the parser will see something unexpected. The error
surfaces, just later and less precisely.

## Synthetic tokens (lines 614-623)

```python
def _emit_synthetic(self, kind):
    self.tokens.append(Token(kind, span=zero_width_span, lexeme="", data={}))
```

Used for `INDENT`, `DEDENT`, `NEWLINE` (when emitted from `_handle_newline`,
the newline has a real lexeme `"\n"`; only `_emit_synthetic` produces
empty lexemes), and `EOF`.

The span is zero-width at the current position. This is mostly for
parser error messages — if the parser complains "expected COLON, got
DEDENT" the user sees the line+col where the dedent happened, even
though there's no character there.

## Errors

```python
class LexerError(Exception):
    def __init__(self, message, span):
        super().__init__(message)
        self.span = span
    def __str__(self):
        return format_diagnostic(self.args[0], self.span)
```

Every error carries a span. `__str__` defers to the shared
`format_diagnostic` helper in
[`src/diagnostics.py`](../src/diagnostics.py), which renders a caret
diagnostic with source-line context:

```
/path/to/file.ou:5:12: wrong number of arguments
  5 |     return add(1)
    |            ^^^^^^
```

When the file isn't readable (e.g. `"<input>"` from inline-source
tests), the renderer degrades to the single-line form so existing
substring assertions keep working.

There's **no error recovery** — first error stops the lex. For a
production compiler, you'd implement panic-mode: skip to the next
likely-safe point (newline, closing bracket) and continue. For a
bootstrap, "first error wins" is fine.

## What this pass does NOT do

- **Comment retention.** Line comments (`# ...`) are stripped. There's
  no `COMMENT` token. If we ever want doc-comment extraction, we'll
  add it.
- **String concatenation.** `"foo" "bar"` lexes as two tokens (a
  parse error).
- **Number range checking.** `0xFFFFFFFFFFFFFFFFFF` (too big for any
  numeric type) lexes successfully; the type checker catches it.
- **Position validation for `?`, `Self`, etc.** The lexer emits the
  raw token; the parser decides where they're legal.

## Cross-references

- Token types live in [`src/tokens.py`](../src/tokens.py); see
  [tokens.md](tokens.md).
- The parser ([`src/parser.py`](../src/parser.py)) consumes the token
  list directly. See [parser.md](parser.md).
- Span construction shares `Span` from [`src/nodes.py`](../src/nodes.py).

## Related tests

[`test/test_lexer.py`](../test/test_lexer.py) — ~30 tests covering:
- All keywords lex to the right TokenKind
- The dunder rejection rule
- Every numeric form (decimal, hex, binary, octal, floats, scientific,
  suffixes, separators)
- String basics, escapes, multi-line, unterminated
- Byte literals (char, escape-n, escape-hex)
- All operator forms
- Newline suppression inside brackets
- Comment stripping
