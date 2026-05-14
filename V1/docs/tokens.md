# `src/tokens.py` — the alphabet

> Defines every distinct kind of thing the lexer can produce, and the
> `Token` record that carries one. This file is **pure data definitions**:
> no logic, no imports beyond `Span`. It is the boundary between "raw
> source text" and "everything later in the pipeline."

## Why this file exists

A compiler turns text into a tree, and then the tree into a backend
representation. The very first decomposition is **lexing** — chopping
the source into atomic units. Before you can write the lexer, you need
to commit to *what those units are*. That commitment lives here.

Two reasons this file is small and stable:

1. **Adding a token kind is a pipeline-wide change.** A new keyword
   needs a `TokenKind` value, an entry in `KEYWORDS`, lexer logic,
   parser handling, often a new AST node, and possibly later passes.
   So we keep the surface area minimal and add carefully.
2. **Splitting `tokens.py` from `lexer.py` matters for layering.**
   The parser depends on `Token` and `TokenKind`; if those lived in
   `lexer.py`, the parser would transitively depend on the lexer's
   internals. Keeping them separate lets the parser say "I work on a
   token stream" without caring how that stream was produced.

## File contents at a glance

```
TokenKind enum        — every token category (keywords, idents, literals, operators, …)
KEYWORDS dict         — keyword lexeme → TokenKind lookup table
Token dataclass       — kind + span + lexeme + payload dict
```

That's the whole file. ~130 lines, no methods.

## `TokenKind` (lines 10-82)

A standard tagged-union approach: each token has exactly one `TokenKind`
value, and the lexer dispatches on it everywhere downstream. Five
groups, each with a comment header in the source:

### Keywords (lines 12-34)

Reserved words. The grammar refuses to use these as identifiers
even where one would be syntactically possible. Of note:

- **`SELF_LOWER` and `SELF_UPPER` are separate kinds** (lines 33-34).
  `self` is the receiver value; `Self` is the receiver type. The grammar
  treats them differently — one appears in expressions, the other in
  type slots. Splitting them at the lexer level makes parser dispatch
  trivial (`if tok.kind == TokenKind.SELF_LOWER: …`).

- **`AND`, `OR`, `NOT`** are keywords, not symbol operators. So
  `a and b` lexes as three tokens (`IDENT`, `AND`, `IDENT`), not two.
  This is a Python-style choice. Symbol-form `&&` / `||` would be
  fine semantically but visually noisier in indentation-heavy code.

- **`VAR`, `CONST`** are keywords *only used as wrapper-type heads*:
  `var[T]`, `const[T]`. They never appear bare. Lexing them as
  keywords (rather than identifiers) is purely defensive — it keeps
  the parser from accidentally accepting `var = 1` as a binding.

- **`LOOP` and `WHILE`** are both loop heads. `loop:` is the
  conditionless form (exit via `break`); `while cond:` is sugar that
  the parser desugars to a `Loop` with an `if not cond: break` guard
  prepended.  The two coexist by design — `loop` for top-or-bottom
  exit-via-break, `while` for top-of-iteration condition.

### `IDENT` (line 37)

The catch-all. After a word is read, if it's not in the `KEYWORDS`
table it becomes `IDENT`. The lexer carries the actual name in
`Token.data["name"]`. The lexer also enforces the dunder convention
(`__foo` without trailing `__` is rejected) at this stage — see
[lexer.md](lexer.md).

### Literals (lines 40-43)

Four kinds: `INT`, `FLOAT`, `BYTE`, `STRING`. The token's `data`
carries the **already-parsed** value, not the source text. So:

- `INT.data["value"] : int` and `INT.data["suffix"] : str | None`
- `FLOAT.data["value"] : float`, similarly
- `BYTE.data["value"] : int` (0-255 only)
- `STRING.data["value"] : bytes` (escape-processed!) and
  `STRING.data["is_multiline"] : bool`

This means the parser **never re-parses literals**. By the time it
sees `IntLiteral`, the integer is already a Python int with the right
value, the right radix already applied, the underscore separators
stripped. This is a deliberate "do work once, do it early" choice.

### Operators (lines 46-62)

Each multi-char operator has its own kind: `EQ` for `==`, `NE` for `!=`,
`TYPE_TEST` for `?=`, `DOT_DOT` for `..`, `ARROW` for `->`. The lexer
uses **maximal munch** — it tries two-char operators before one-char.

`PIPE` (line 59) is annotated `# type union; bitwise OR later` — currently
it is only ever used in type-union position (`T1 | T2`). When bitwise
OR for integers is added, the parser will disambiguate by context (it
already does this for `[...]` between generic args and value index).

`QUESTION` (line 62) is the standalone `?` token, only legal inside
type brackets — e.g. `var[?]` for inference. The lexer doesn't enforce
that constraint; the parser does, by the placement of the `_parse_type_atom`
case. If `?` appears anywhere else, you'll get a "unexpected token in
expression" error from the Pratt parser.

### Punctuation (lines 65-74)

Brackets, comma, colon, dot, semicolon. The lexer tracks bracket depth
(`bracket_depth`) so that `\n` inside `(`, `[`, or `{` is suppressed
as a `NEWLINE` token. This is what makes multi-line argument lists work
without a continuation character.

`SEMICOLON` exists but the language *doesn't* require it as a
statement terminator. It's an *optional* same-line separator:

```ouro
if x: a = 1; b = 2; c = 3
```

The parser accepts `;` between statements when it expects a NEWLINE.
Most code uses newlines.

### Whitespace-significant tokens (lines 77-79)

`NEWLINE`, `INDENT`, `DEDENT`. These are **synthesized** by the lexer
from physical whitespace, not present as characters in the source.

- `NEWLINE` ends a logical line — only emitted at `bracket_depth == 0`.
- `INDENT` opens a new indentation level — emitted when a non-blank
  line's indent column is greater than the top of `indent_stack`.
- `DEDENT` closes one level — emitted when indent drops to a lower
  open level. Multiple `DEDENT`s can fire from one source-level dedent
  if it crosses multiple levels at once.

### `EOF` (line 82)

Always the last token. The lexer guarantees this; the parser relies on
it for the `_at_eof()` check.

## `KEYWORDS` (lines 86-110)

```python
KEYWORDS: dict[str, TokenKind] = {
    "var": TokenKind.VAR, "const": TokenKind.CONST,
    "if": TokenKind.IF, "else": TokenKind.ELSE, "elif": TokenKind.ELIF,
    "match": TokenKind.MATCH, "for": TokenKind.FOR, "in": TokenKind.IN,
    …
}
```

A simple `dict` — no priority, no special casing, no contextual
keywords. After lexing a word, the lexer looks it up here:

```python
kind = KEYWORDS.get(lexeme, TokenKind.IDENT)
```

Hit → keyword token. Miss → identifier. This is the standard trick:
keywords are special only because the table says they are; the rest
of the lexer treats them like any other word.

## `Token` dataclass (lines 113-129)

```python
@dataclass
class Token:
    kind: TokenKind
    span: Span
    lexeme: str
    data: dict[str, Any]
```

Four fields:

- **`kind`** — the discriminator. Use `tok.kind == TokenKind.X` everywhere.
- **`span`** — source location. Imported from `nodes.py` (so types,
  patterns, and tokens all share one `Span` definition; no duplication).
- **`lexeme`** — the exact source text. Most tokens are 1-4 chars, but
  identifiers, strings, and numbers can be arbitrarily long. Useful
  for error messages: "expected COLON, got IDENT (`'foo'`)".
- **`data`** — kind-specific payload. The docstring (lines 117-123)
  documents the schema:
  - `INT`:    `{"value": int, "suffix": str | None}`
  - `FLOAT`:  `{"value": float, "suffix": str | None}`
  - `BYTE`:   `{"value": int}`
  - `STRING`: `{"value": bytes, "is_multiline": bool}`
  - `IDENT`:  `{"name": str}`
  - all other kinds: `{}` (empty)

## Design decision: `data: dict[str, Any]` is loose

This is the tradeoff. Pros:

- Adding a new payload field is one-line — no schema changes.
- Tokens with no payload share a literal `{}` with no type explosion.
- Easy to inspect in debug prints.

Cons:

- A typo in `tok.data["values"]` (instead of `"value"`) is a runtime
  `KeyError`, not a type error.
- The schema lives in a docstring, not in the type system.

A stricter alternative: separate `IntToken`, `StringToken`, etc.
dataclasses with typed fields, unified by a `Token` `Union`. That's
sound but adds boilerplate everywhere a token is constructed or
inspected. The bootstrap explicitly chose the loose route; tightening
later if it turns out to bite is straightforward.

## Why `Span` lives in `nodes.py`, not here

Both `Token` and AST nodes carry source spans. If `Span` lived in
`tokens.py`, then `nodes.py` would import from `tokens.py`, even though
nothing else in `nodes.py` cares about lexing. By putting `Span` at
the bottom of the dependency tree (`nodes.py`) and importing it
upward, both files stay clean.

Concretely: `tokens.py` imports `Span` from `.nodes`; `nodes.py`
imports nothing from `tokens.py`. The dependency graph is a DAG, not
a cycle.

## Cross-references

- The lexer ([`src/lexer.py`](../src/lexer.py)) is the only producer
  of `Token` values. See [lexer.md](lexer.md) for how the bytes
  become tokens.
- The parser ([`src/parser.py`](../src/parser.py)) is the only
  *consumer*. After it runs, no later pass touches tokens. See
  [parser.md](parser.md).
- `Span` is defined in [`src/nodes.py`](../src/nodes.py); see
  [nodes.md](nodes.md) for how spans propagate through the AST.

## Related tests

[`test/test_lexer.py`](../test/test_lexer.py) exercises every
`TokenKind`, the keyword table, and edge cases like `__name`
rejection and bracket-depth newline suppression. ~250 lines, ~30
tests.
