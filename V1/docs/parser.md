# `src/parser.py` — tokens → AST

> Recursive-descent parser with Pratt-style expression handling.
> Consumes a flat token list (from the lexer) and produces a `File`
> AST node. Errors stop immediately; no recovery.

## What the parser is responsible for

The parser turns a token stream into a structured tree. Every later
pass consumes that tree and produces something else. So the parser is
**the source of truth for syntactic structure** — what's grammatical,
how operators bind, what counts as a "statement" vs. an "expression."

Three properties of this parser worth keeping in mind:

1. **It's hybrid.** Statements, declarations, and types are
   recursive-descent. Expressions use **Pratt parsing** with explicit
   binding-power tables. The split is pragmatic: rec-desc is great
   for grammar that has a clear "next token says what to do" shape;
   Pratt is great for operator-heavy expression grammars.
2. **It's mostly single-pass.** The only place backtracking happens is
   the `[...]` postfix dispatch (generic instantiation vs. value index).
3. **It punts on semantics.** "Is `x = 1` a fresh binding or a
   re-assignment?" The parser doesn't know, doesn't try to know, and
   emits a `Binding` node that the type checker disambiguates later.

## File structure

```
imports                                     (lines 1-75)
ParseError                                  (lines 81-88)
_INFIX_BP, _PREFIX_BP, _POSTFIX_BP          (lines 96-121)    Pratt tables
Parser class                                (lines 127-1203)
  cursor utilities                          (lines 135-186)
  type parsers                              (lines 188-278)
  pattern parsers                           (lines 280-348)
  expression parser (Pratt)                 (lines 350-664)
  if / match parsers                        (lines 673-780)
  block / statement parsers                 (lines 782-963)
  top-level parsers                         (lines 965-1165)
  parse_file (entry)                        (lines 1169-1203)
parse() free function                       (lines 1209-1211)
```

## `ParseError` (lines 81-88)

```python
class ParseError(Exception):
    def __init__(self, message, span):
        super().__init__(message)
        self.span = span
    def __str__(self):
        return format_diagnostic(super().__str__(), self.span)
```

Same shape as `LexerError`. `__str__` defers to the shared
`format_diagnostic` helper in
[`src/diagnostics.py`](../src/diagnostics.py) — see
[lexer.md](lexer.md#errors) for the rendered format.
**No recovery** — first error stops the parse.

## Pratt machinery (lines 91-121)

```python
_INFIX_BP: dict[TokenKind, tuple[int, int]] = {
    TokenKind.OR: (10, 11),
    TokenKind.AND: (20, 21),
    TokenKind.EQ: (30, 31), NE: (30, 31), LT: (30, 31), …
    TokenKind.TYPE_TEST: (35, 36),
    TokenKind.PIPE: (40, 41),
    TokenKind.PLUS: (50, 51), MINUS: (50, 51),
    TokenKind.STAR: (60, 61), SLASH: (60, 61), PERCENT: (60, 61),
}

_PREFIX_BP: dict[TokenKind, int] = {
    TokenKind.MINUS: 70,
    TokenKind.NOT: 70,
}

_POSTFIX_BP = 80
```

### What "binding power" means

Binding power is a number. Higher = tighter. When parsing an
expression and you see an operator, you compare its left binding
power (`left_bp`) to the **minimum binding power** the current parse
level demands. If `left_bp <= min_bp`, you stop — the operator
"belongs to the outer parse." Otherwise you consume it and recurse
with `right_bp` as the new minimum.

The `(left, right)` pair encodes associativity:

- **`(50, 51)` for `+` (left > right offset): left-associative.**
  `a + b + c` parses as `(a + b) + c`. After consuming `+`, we recurse
  with `min_bp = 51`; the next `+` has `left_bp = 50`, which is `≤ 51`,
  so we stop and let the outer call handle it.
- **`(50, 50)` would be right-associative.** Same trick reversed.
- **`(50, 49)` would also be right-associative**, more aggressively.

### Three subtle entries

- **`TYPE_TEST` (BP 35)** — `?=` is in the infix table because it's
  written infix. But its right side is a **type**, not an expression.
  The Pratt loop has special handling at line 479:
  ```python
  if op_tok.kind == TokenKind.TYPE_TEST:
      ...
      typ = self._parse_type()      # not _parse_expr!
  ```

- **`PIPE` (BP 40)** — used in expression position only when needed
  for type unions inside `?=` RHS or other type contexts. The lexer
  doesn't distinguish "type-pipe" from "expr-pipe"; the parser
  contextually disambiguates.

- **`POSTFIX_BP = 80`** — single number, highest. `.`, `(...)`,
  `[...]`, struct literal `{...}`, range `..` all bind at this level.
  When you write `obj.foo(a, b)[i].bar`, the postfix loop chains them
  left-associatively at this BP.

## Cursor utilities (lines 135-186)

```python
def _peek(self, offset=0):
    idx = self._pos + offset
    return self._tokens[-1] if idx >= len(self._tokens) else self._tokens[idx]

def _advance(self):
    tok = self._tokens[self._pos]
    if self._pos < len(self._tokens) - 1:
        self._pos += 1
    return tok

def _check(self, *kinds):
    return self._peek().kind in kinds

def _eat(self, *kinds):
    tok = self._peek()
    if tok.kind not in kinds:
        raise ParseError(f"expected {expected}, got {tok.kind.name}", tok.span)
    return self._advance()
```

Five primitives. `_eat` is the workhorse — used hundreds of times.
`_check` is a non-consuming variant. `_peek(offset)` allows lookahead;
`offset=1` gives "the token after current."

`_skip_newlines()` and `_eat_newlines()` (lines 164-186) are the same
function — eat any number of `NEWLINE`/`SEMICOLON` tokens. They're
duplicated for documentation purposes (one is for "I want to ignore
trailing whitespace," the other for "I expect at least optional
whitespace here"). Tightening to one method is fine; we kept two for
self-documenting calls.

`_span_from(start_token)` builds a span from a starting token to the
**previously consumed** token:

```python
def _span_from(self, start):
    end = self._tokens[max(0, self._pos - 1)]
    return Span(self._file, start.span.start_line, start.span.start_col,
                end.span.end_line, end.span.end_col)
```

The pattern is: at the start of parsing a construct, `start = self._peek()`
or `self._eat(...)`. At the end, `span = self._span_from(start)`. The
result covers the entire construct.

## Type parsers (lines 188-278)

```python
def _parse_type(self):
    """Parse a type, including top-level union T1 | T2."""
    first = self._parse_type_atom()
    if not self._check(TokenKind.PIPE): return first
    variants = [first]
    while self._check(TokenKind.PIPE):
        self._advance()
        variants.append(self._parse_type_atom())
    return UnionType(span=..., variants=variants)

def _parse_type_atom(self):
    # `[]T` slice
    # `?` infer
    # `Self`
    # IDENT / VAR / CONST → wrapper or generic or named
```

Order of precedence inside `_parse_type_atom`:

1. `[` → `SliceType` (consume `[]`, recurse for inner)
2. `?` → `InferType`
3. `Self` → `SelfType`
4. IDENT/VAR/CONST followed by `[` → either `WrapperType` (if name is
   one of `var`/`const`/`weak`/`ptr`) or `GenericType`
5. IDENT alone → `NamedType`

The wrapper-name check is a **string compare on lexeme** at line
243. `var` and `const` are also keywords (TokenKinds), but `weak` and
`ptr` aren't. The IDENT/VAR/CONST tuple at line 238 is what makes the
parser accept all four uniformly.

## Pattern parsers (lines 280-348)

The trickiest dispatch in the grammar. Match arm patterns:

| Source | Pattern node | Colon counting |
|---|---|---|
| `_:` | `WildcardPattern` | 1 colon |
| `_: T:` | `TypePattern(binding=None, type=T)` | 2 colons |
| `n: T:` | `TypePattern(binding="n", type=T)` | 2 colons |
| `expr:` | `ValuePattern(value=expr)` | 1 colon |

`_parse_pattern` reads the **first colon** as a normal `_eat(COLON)`,
then checks the token *after* it. If it looks like a type-starter
(`IDENT`, `SELF_UPPER`, `[`, `?`), it's a type pattern with a second
colon coming. Otherwise it's a wildcard, and we're done.

For `n: T:` (type pattern with binding), the parser does *3-token
lookahead* (line 322-323):

```python
if next_tok.kind == TokenKind.COLON:
    after_colon = self._peek(2)
    if after_colon.kind in (IDENT, SELF_UPPER, LBRACKET, QUESTION):
        # type pattern
```

If the lookahead doesn't see a type-start at position 2, we fall
through to the value-pattern path, which **rewinds** to the beginning
and parses an expression. The first colon is then re-consumed as the
arm-body colon at line 347.

This is the only place in the parser besides the `[...]` postfix
where rewinding-by-state happens, and it's done by careful colon
ordering rather than `try/except`.

## Expression parser (lines 350-664) — the Pratt loop

```python
def _parse_expr(self, min_bp):
    tok = self._peek()

    # ── Prefix / atoms ──────────────────────────────────
    if tok.kind in _PREFIX_BP:                   # -x, not x
        bp = _PREFIX_BP[tok.kind]; self._advance()
        operand = self._parse_expr(bp)
        lhs = UnaryOp(span=..., op=tok.lexeme, operand=operand)

    elif tok.kind == TokenKind.LPAREN:           # (expr)
        self._advance(); lhs = self._parse_expr(0); self._eat(RPAREN)

    elif tok.kind == TokenKind.IF:    lhs = self._parse_if()
    elif tok.kind == TokenKind.MATCH: lhs = self._parse_match()

    # literals
    elif tok.kind == TokenKind.INT:    self._advance(); lhs = IntLiteral(...)
    elif tok.kind == TokenKind.FLOAT:  ...
    elif tok.kind == TokenKind.TRUE:   self._advance(); lhs = BoolLiteral(value=True)
    ...

    # `_` discard
    elif tok.kind == TokenKind.IDENT and tok.lexeme == "_":
        self._advance(); lhs = Discard(span=tok.span)

    # name (incl. self / Self)
    elif tok.kind in (IDENT, SELF_LOWER, SELF_UPPER):
        self._advance(); lhs = Name(span=tok.span, name=tok.lexeme)

    else:
        raise self._err(f"unexpected token in expression: ...")

    # ── Postfix / infix loop ────────────────────────────
    while True:
        op_tok = self._peek()

        if op_tok.kind == TokenKind.DOT and _POSTFIX_BP > min_bp:
            # field access
            ...; lhs = FieldAccess(...); continue

        if op_tok.kind == TokenKind.LPAREN and _POSTFIX_BP > min_bp:
            # call
            ...; lhs = Call(...); continue

        if op_tok.kind == TokenKind.LBRACKET and _POSTFIX_BP > min_bp:
            # subscript or generic instantiation
            lhs = self._parse_bracket_postfix(lhs); continue

        if (op_tok.kind == TokenKind.LBRACE
                and _POSTFIX_BP > min_bp
                and isinstance(lhs, (Name, GenericInstantiation, FieldAccess))):
            # struct literal — only legal after specific lhs shapes
            lhs = self._parse_struct_literal(lhs); continue

        if op_tok.kind == TokenKind.TYPE_TEST:
            # `?=` — RHS is a Type, not an Expression
            ...; lhs = TypeTest(...); continue

        if op_tok.kind in _INFIX_BP:
            left_bp, right_bp = _INFIX_BP[op_tok.kind]
            if left_bp <= min_bp: break
            self._advance()
            rhs = self._parse_expr(right_bp)
            lhs = BinaryOp(...); continue

        if op_tok.kind == TokenKind.DOT_DOT and _POSTFIX_BP > min_bp:
            # range
            ...; lhs = Range(...); continue

        break

    return lhs
```

A few things deserve a closer look.

### Why `_POSTFIX_BP > min_bp` even for postfix ops

Postfix ops have a single BP (80). The check `if _POSTFIX_BP > min_bp`
serves the same purpose as the `left_bp <= min_bp: break` for infix
ops: if the outer parse demanded a higher minimum, we leave the
postfix for the outer to consume.

In practice, with `_POSTFIX_BP = 80` (highest) and `min_bp` rarely
exceeding `61`, this check almost always passes. It's there for
correctness in unusual contexts.

### Struct literal restriction (lines 470-475)

```python
if (op_tok.kind == TokenKind.LBRACE
        and _POSTFIX_BP > min_bp
        and isinstance(lhs, (Name, GenericInstantiation, FieldAccess))):
    lhs = self._parse_struct_literal(lhs); continue
```

We only treat `{` as a struct literal opener when `lhs` is one of
those specific shapes. Why? Because `{` also appears in match-arm
inline syntax and in struct-init contexts, and we don't want to
accidentally consume it.

For example, `match x: 1: foo` — when parsing `foo`, if we see `1:`
as a `:`-following-IntLiteral, we don't want to also consume `{...}`
as a struct literal of `foo`. The constraint to specific lhs types
prevents this.

### `_parse_bracket_postfix` — the only backtracking spot (lines 573-615)

```python
def _parse_bracket_postfix(self, lhs):
    self._eat(LBRACKET)

    saved_pos = self._pos
    try:
        type_args = []
        if not self._check(RBRACKET):
            type_args.append(self._parse_type())
            while self._check(COMMA):
                self._advance()
                type_args.append(self._parse_type())
        close = self._eat(RBRACKET)
        return GenericInstantiation(...)
    except ParseError:
        self._pos = saved_pos

    # fall back to value index
    idx_expr = self._parse_expr(0)
    close = self._eat(RBRACKET)
    return Index(...)
```

The ambiguity: `x[i]` could be `Index` (i is an expression, name) or
`GenericInstantiation` (i is a type, name).

The heuristic: try parsing the contents as **types first**. If we can
successfully consume types and reach `]`, treat it as a generic
instantiation. Otherwise rewind and parse as a value index.

This is the **only place** the parser uses speculative parsing. It's
done with a saved cursor + try/except on `ParseError`. Note that side
effects (the AST nodes built inside `_parse_type`) get discarded
because the rewind only resets `_pos`; nothing else in the parser
holds references to those nodes.

### Named arguments (lines 554-571)

```python
def _parse_one_arg(self):
    start = self._peek()
    if start.kind == TokenKind.IDENT and self._peek(1).kind == TokenKind.COLON:
        name = start.lexeme
        self._advance(); self._advance()  # name, :
        val = self._parse_expr(0)
        return Argument(span=..., name=name, value=val)
    val = self._parse_expr(0)
    return Argument(span=val.span, name=None, value=val)
```

A 2-token lookahead distinguishes `f(x: 1)` (named) from `f(x)`
(positional). If both tokens look right, we consume them as a named
arg. Otherwise we parse as expression — which would re-parse `x`
inside, and `:` would just be left for the caller.

This is one of the only places in the parser where "what does this
look like?" matters. Everywhere else, the first token is enough.

### `_expr_to_type` (lines 650-664)

```python
def _expr_to_type(self, expr):
    if isinstance(expr, Name):
        return NamedType(span=expr.span, name=expr.name)
    if isinstance(expr, GenericInstantiation):
        if isinstance(expr.base, Name):
            return GenericType(span=expr.span, base=expr.base.name, args=expr.type_args)
    if isinstance(expr, FieldAccess):
        return NamedType(span=expr.span, name=f"{...}.{expr.field}")
    raise ParseError("expression cannot be used as a type", expr.span)
```

When parsing a struct literal like `Foo {...}`, we've already parsed
`Foo` as an expression (a `Name`). To convert it back into a type, we
use `_expr_to_type`. This function handles the three cases that
matter: bare name, generic instantiation, and dotted access.

The dotted access case constructs a `NamedType` with a dotted name,
which is a soft hack — the resolver and type checker don't really
understand dotted names beyond modules. It's there for forward
compatibility.

## `if` and `match` parsers (lines 673-780)

`_parse_if` and `_parse_if_from_elif` are nearly duplicated. The
difference is the leading keyword: `if` vs. `elif`. We could
refactor to share more, but the duplication is shallow and clear.

The `elif` chain: `if A: ... elif B: ... else: ...` parses as

```
If(A, then,
    Block([ExprStatement(If(B, then, else_block))]))
```

where the inner `If` is parsed by `_parse_if_from_elif`. **No special
"elif" node** — we flatten to nested ifs at parse time so later
passes don't need a special case.

`_parse_match` follows the standard block pattern: scrutinee, `:`,
indent, repeat (pattern, body), dedent. The match arm body uses
`_parse_arm_body` which is just `_parse_body` (line 779-780).

## `_parse_body` (lines 675-694)

The bridge between block syntax and inline syntax:

```python
def _parse_body(self):
    if self._check(TokenKind.INDENT):
        return self._parse_block()
    # inline: parse one statement, optionally followed by `;`-separated more
    stmts = [self._parse_statement()]
    while self._check(TokenKind.SEMICOLON):
        ...
    return Block(span=..., statements=stmts)
```

This makes:

```ouro
if x: return 1            # inline single statement
if x:                     # block of one or more statements
    return 1
if x: a = 1; b = 2; c     # inline multiple statements via `;`
```

all work identically. Used by `if` / `else` / `for` / `loop` / match
arms.

## Block parser (lines 784-809)

```python
def _parse_block(self):
    self._eat(TokenKind.INDENT)
    stmts = []
    while not self._check(DEDENT, EOF):
        self._skip_newlines()
        if self._check(DEDENT, EOF): break
        stmts.append(self._parse_statement())
        while self._check(SEMICOLON):
            ...
        self._skip_newlines()
    self._eat(TokenKind.DEDENT)
    return Block(...)
```

The `_skip_newlines` calls cope with blank lines inside blocks. The
inner `while` loop handles `;`-separated statements on a single line.

## Statement parser (lines 811-963)

```python
def _parse_statement(self):
    tok = self._peek()

    if tok.kind == RETURN:    return self._parse_return()
    if tok.kind == PASS:      ...; return Pass(...)
    if tok.kind == BREAK:     ...; return Break(...)
    if tok.kind == CONTINUE:  ...; return Continue(...)
    if tok.kind == FOR:       return self._parse_for()
    if tok.kind == LOOP:      return self._parse_loop()
    if tok.kind == WHILE:     return self._parse_while()  # desugars to Loop

    if tok.kind == IF:    return ExprStatement(span=..., expr=self._parse_if())
    if tok.kind == MATCH: return ExprStatement(span=..., expr=self._parse_match())

    # binding or assignment or expression
    return self._parse_binding_or_assign_or_expr()
```

`if` and `match` at statement position get wrapped in `ExprStatement` —
treating them as side-effecting expressions whose return value is
discarded.

### `_parse_binding_or_assign_or_expr` (lines 905-963)

The most ambiguous part of the statement grammar:

```python
# `name: Type = expr`
if tok.kind == IDENT and self._peek(1).kind == COLON:
    name = ...; self._advance(); self._advance()
    ann = self._parse_type()
    self._eat(ASSIGN)
    val = self._parse_expr(0)
    return Binding(span=..., name=name, type=ann, value=val)

# `name = expr`
if tok.kind == IDENT and self._peek(1).kind == ASSIGN:
    name = ...; self._advance(); self._advance()
    val = self._parse_expr(0)
    return Binding(span=..., name=name, type=None, value=val)

# fall through: parse as expression
expr = self._parse_expr(0)

if self._check(ASSIGN):
    self._advance()
    val = self._parse_expr(0)
    return Assignment(span=..., target=expr, value=val)

return ExprStatement(span=expr.span, expr=expr)
```

Three cases:
1. **Typed binding** — IDENT followed by COLON → consume both, parse
   type, expect `=`, parse value, emit `Binding(type=ann)`.
2. **Inferred binding** — IDENT followed by ASSIGN → emit
   `Binding(type=None)`.
3. **Expression** — parse, then check for trailing `=`. If yes, emit
   `Assignment(target=expr, value=val)`. Otherwise, `ExprStatement(expr)`.

This is where the **parser can't tell binding from re-assignment**.
The phrase `n = n + 1` always parses as `Binding(name="n", type=None,
value=BinaryOp("+", Name("n"), IntLit(1)))`. The type checker, when
it sees this, looks up `n` in scope and decides:

- `n` already bound as `var[T]` somewhere → re-assignment
- `n` not bound → fresh binding

This split is what we discussed in the typechecker session — see
[typechecker.md](typechecker.md).

## Top-level parsers (lines 965-1165)

`_parse_function`, `_parse_struct`, `_parse_import`,
`_parse_top_level_binding`. Each is straight-line recursive descent:

```python
def _parse_function(self):
    self._eat(FN)
    name = self._eat(IDENT)

    generics = []
    if self._check(LBRACKET):
        # `[T, U]`
        ...

    self._eat(LPAREN)
    self_param, params = self._parse_params()
    self._eat(RPAREN)

    return_type = None
    if self._check(ARROW):
        self._advance()
        return_type = self._parse_type()

    self._eat(COLON)
    self._eat_newlines()
    body = self._parse_block()
    return Function(...)
```

### `_parse_params` and self handling (lines 1030-1070)

```python
def _parse_params(self):
    if self._check(RPAREN): return None, []

    first = self._peek()
    if first.kind == TokenKind.SELF_LOWER:
        self._advance()
        if self._check(COLON):
            self._advance()
            typ = self._parse_type()
            self_param = SelfParam(span=..., type=typ, is_default=False)
        else:
            # bare `self` → ptr[Self]
            ptr_self = WrapperType(wrapper="ptr", inner=SelfType(...))
            self_param = SelfParam(span=..., type=ptr_self, is_default=True)

        if self._check(COMMA):
            self._advance()
        else:
            return self_param, []

    # regular parameters
    params = []
    while not self._check(RPAREN):
        params.append(self._parse_one_param())
        if not self._check(COMMA): break
        self._advance()
    return self_param, params
```

The bare-`self` synthesis (line 1048-1054) is an explicit AST
construction:

```python
ptr_self = WrapperType(span=first.span, wrapper="ptr", inner=SelfType(span=first.span))
```

The synthesized `WrapperType` and `SelfType` get the `self` token's
span. Later passes don't re-derive this default — they read it from
`SelfParam.type` directly.

## `parse_file` entry (lines 1169-1203)

```python
def parse_file(self):
    decls = []
    while not self._at_eof():
        self._skip_newlines()
        if self._at_eof(): break

        tok = self._peek()
        if tok.kind == FN:        decls.append(self._parse_function())
        elif tok.kind == STRUCT:  decls.append(self._parse_struct())
        elif tok.kind == IDENT:
            # `name = import(...)` or `name [: Type] = expr`
            next_tok = self._peek(1)
            if (next_tok.kind == ASSIGN
                    and self._peek(2).kind == IMPORT):
                decls.append(self._parse_import())
            else:
                name_tok = self._advance()
                decls.append(self._parse_top_level_binding(name_tok))
        else:
            raise self._err(...)

        self._skip_newlines()
    return File(span=..., path=self._file, declarations=decls)
```

Top-level dispatch: `fn`, `struct`, or IDENT (which is either an
import or a top-level binding). The lookahead at line 1186-1190 picks
between `import(...)` and a regular binding.

## Error recovery: there isn't any

`_eat` raises immediately. There's no resync-to-next-newline, no
"skip until DEDENT and try again." For a v1 bootstrap, this is
acceptable — the user fixes one error, recompiles, sees the next.

A production parser would add panic-mode recovery so the user can
see all parse errors per compile. The `_eat` method would need to
become "expected this, log the error, but pretend I saw it and keep
going." That's a fair amount of work and we deferred it.

## Quirks worth noting

1. **`open_tok = self._eat(TokenKind.INDENT)` in `_parse_block`** —
   ruff once flagged this as F841 ("unused variable"). It's actually
   used at line 803-808 for the span construction, but the use is
   far from the assignment so the warning fires. Worth knowing if you
   ever rerun ruff strictly.

2. **`_skip_newlines` and `_eat_newlines` are identical.** Documentation
   value only. If you tighten to one method, choose `_skip_newlines`
   (the more common name).

3. **The `f"{tok.kind.name} ({tok.lexeme!r})"` error message format**
   — kind name + lexeme for clarity. `RBRACKET (']')` is more useful
   than `RBRACKET`.

## What this pass does NOT do

- **Resolve names.** `Name(name="foo")` doesn't know what `foo` refers
  to. That's the resolver's job.
- **Determine types.** Same — type checker.
- **Check semantic correctness.** `return` outside a function would
  parse fine; the resolver catches it.
- **Validate match exhaustiveness.** Open question; not yet checked
  anywhere.
- **Anything to do with whitespace.** The lexer already handled it.

## Cross-references

- The lexer ([`src/lexer.py`](../src/lexer.py)) is the only producer
  of input. See [lexer.md](lexer.md).
- The AST nodes ([`src/nodes.py`](../src/nodes.py)) are the output
  vocabulary. See [nodes.md](nodes.md).
- The resolver ([`src/resolver.py`](../src/resolver.py)) and type
  checker ([`src/typechecker.py`](../src/typechecker.py)) are the
  next consumers.

## Related tests

[`test/test_parser.py`](../test/test_parser.py) — ~580 lines, ~75
tests. Covers:
- Top-level: import, top-level binding, function (minimal, with
  generics, with return type), struct (empty, fields, methods)
- Types: every `Type` shape — named, slice, wrapper, generic, infer,
  union, self
- Expressions: literals, names, field access, calls (positional,
  named, no args), generic instantiation, struct literal, binary ops
  (precedence!), unary, type test, type test on union
- Statements: bindings (typed, inferred), assignment, return
  (with/without value), pass, break, continue, for (typed,
  discard, basic), loop
- if / elif / else / inline if
- match (value patterns, type patterns, inline arms)
- Self params (bare, explicit, static method without self)
- Parse error cases (unexpected token, missing colon)
