# Syntax

Lexical rules and grammar for Ouro source files.

## File extension and encoding

- Source files use the `.ou` extension.
- Files are UTF-8 encoded.
- Strings are **byte slices** (`[]u8`), not UTF-16 or codepoint
  sequences. Source-level strings are encoded to UTF-8 at lex time.

## Indentation

Ouro is indentation-sensitive in the Python style. Significant
whitespace forms blocks:

- An **increase in leading-space count** opens a block (`INDENT`).
- A **decrease** closes one or more blocks (`DEDENT`).
- A line that drops below all open levels but doesn't match a
  previously-open level is an **inconsistent-indentation error**.

Rules:

- Use **spaces only**. Tabs are rejected at the lexer with an
  explicit error. (This is stricter than Python; the goal is
  unambiguity.)
- Indentation width is **flexible** — any positive number of spaces
  works, as long as you're consistent within nested blocks.
- **Blank lines and comment-only lines do not change indentation
  state.** They're allowed anywhere.
- Inside `()`, `[]`, `{}`, **newlines are suppressed** — you don't need
  continuation characters for multi-line expressions.

```ouro
io.println(
    "this works "
    "across newlines"           # ← inside parens, newline is whitespace
)
```

## Comments

```ouro
# Line comment — runs to end of line
x = 42  # also fine after a statement
```

Only line comments. There are no block / multi-line comments.

## Identifiers

```
ident ::= ident_start ident_cont*
ident_start ::= letter | '_'
ident_cont ::= letter | digit | '_'
letter ::= [A-Za-z]   (and any unicode letter — tested via str.isalpha())
digit ::= [0-9]
```

The dunder rule (enforced at lex time):

- `_name` (single leading underscore) — **private** identifier
  (struct-private convention; see
  [conventions.md](conventions.md#privacy))
- `__name__` (full dunder) — **reserved** for special methods
- `__name` (leading double underscore, *no* trailing `__`) — **rejected**
  with a lexer error

## Keywords

The following are reserved and cannot be used as identifiers:

```
var const ptr weak                # wrapper-type heads (also reserved as words in some contexts)
if elif else match for in
return pass break continue loop while
fn struct import
true false
and or not
self Self
```

(`weak` and `ptr` aren't TokenKinds — they're *recognized as wrapper
heads* during parse but lex as plain identifiers. The above is the
practical "don't shadow these" list.)

## Literals

### Integer literals

```ouro
42          # decimal, defaults to isize
1_000_000   # underscores as visual separators
0xFF        # hex
0b1010      # binary
0o755       # octal
42i64       # explicit type via suffix
0xFFu32     # hex with suffix
```

Integer suffixes: `i8`, `i16`, `i32`, `i64`, `isize`, `u8`, `u16`,
`u32`, `u64`, `usize`.

### Float literals

```ouro
3.14        # default f64
3.14f32     # explicit f32
1.5e-9      # scientific
1_234.567   # underscores
42f32       # suffix forces float even without `.`
```

Float suffixes: `f32`, `f64`. A suffix beginning with `f` promotes the
literal to a float regardless of whether `.` or `e` appeared.

### Bool literals

```ouro
true
false
```

### Byte literals

```ouro
'A'         # single byte (u8)
'\n'        # escape
'\x41'      # hex escape (= 'A')
```

A byte literal must contain exactly one byte. Multi-byte UTF-8
characters (e.g. `'é'`, which is 2 bytes) are rejected. Use a string
literal for multi-byte content.

Byte literals support these escapes: `\n \r \t \0 \\ \' \" \xNN`.

`\u{...}` escapes are **not** legal in byte literals (a codepoint may
span multiple bytes).

### String literals

```ouro
"hello"
"with \n escape"
"with hex \x41"
"with codepoint \u{1F600}"
"""
multi-line
string
"""
```

Single-quoted strings (`"..."`) terminate at the first unescaped `"`
or end-of-line — newlines inside are an error.

Triple-quoted strings (`"""..."""`) span multiple lines.

The string's value is **bytes** (UTF-8 encoded), not a `str`. Strings
in Ouro are byte slices `[]u8` — there is no character type and no
UCS-2 encoding.

Supported escapes in strings: same as byte literals plus `\u{HEX}`
(any number of hex digits) which expands to the codepoint's UTF-8
encoding.

## Operators

### Two-character

```
==  !=  <=  >=  ?=  ..  ->
```

### Single-character

```
+  -  *  /  %       # arithmetic
<  >                # comparison
=                   # assignment
|                   # type union (also bitwise OR — reserved)
?                   # inference placeholder (legal only inside [ ])
(  )  [  ]  {  }    # brackets
:  ,  .  ;          # punctuation
```

`?=` is the type-test operator (see [errors.md](errors.md)).
`->` is the function return-type arrow.
`..` is the range operator (used in slice indexing).
`?` inside type brackets means "infer this type" (e.g. `var[?]`).

## Expression precedence

From lowest to highest binding power:

| BP | Operator | Associativity |
|---:|---|---|
| 10 | `or` | left |
| 20 | `and` | left |
| 30 | `==` `!=` `<` `<=` `>` `>=` | left |
| 35 | `?=` | left (RHS is a type) |
| 40 | `\|` | left (in type context) |
| 50 | `+` `-` | left |
| 60 | `*` `/` `%` | left |
| 70 | unary `-` `not` | prefix |
| 80 | `.` `(...)` `[...]` `{...}` `..` | postfix |

So `a + b * c == d and e` parses as `((a + (b * c)) == d) and e`.

There is **no shorthand for ternary expressions** — use `if cond:
a else: b` (an `if` expression).

## Grammar (informal EBNF)

Notation: `?` optional, `*` zero-or-more, `+` one-or-more, `|`
alternation, `'literal'` literal terminals, `INDENT`/`DEDENT`/`NEWLINE`
synthetic whitespace tokens.

### File and top-level declarations

```
file        ::= top_decl*
top_decl    ::= function | struct | import_decl | const_decl | type_alias
              | extern_decl
import_decl ::= IDENT '=' 'import' '(' STRING ')' NEWLINE
const_decl  ::= IDENT (':' type)? '=' expr NEWLINE
type_alias  ::= IDENT generics? ':' 'type' '=' type NEWLINE
extern_decl ::= 'extern' IDENT '(' params? ')' return_type? NEWLINE
params      ::= param (',' param)* (',' '...')?
function    ::= 'fn' IDENT generics? '(' params? ')' return_type? ':' block
struct      ::= 'struct' IDENT generics? ':' INDENT struct_member+ DEDENT
generics    ::= '[' IDENT (',' IDENT)* ']'
return_type ::= '->' type
struct_member ::= field | function | 'pass'
field       ::= IDENT ':' type NEWLINE
params      ::= self_param (',' param_list)? | param_list
self_param  ::= 'self' (':' type)?
param_list  ::= param (',' param)*
param       ::= IDENT ':' type
```

### Types

```
type        ::= type_atom ('|' type_atom)*           # union
type_atom   ::= '[]' type_atom                       # slice
              | '?'                                  # infer placeholder
              | 'Self'
              | IDENT '[' type (',' type)* ']'       # generic / wrapper
              | IDENT                                # named
```

The six wrapper words `var`, `const`, `rc`, `arc`, `weak`, `ptr` use
the `name [ ... ]` form. They're recognized at parse time by name.

### Patterns (in match arms)

```
pattern     ::= '_' ':' (type ':')?                  # wildcard or `_: T:`
              | IDENT ':' type ':'                   # `n: T:` binding
              | expr ':'                             # value pattern
```

The colons in pattern syntax are tricky — see
[control-flow.md](control-flow.md#match) for how they're counted.

### Statements

```
stmt        ::= 'return' expr? NEWLINE
              | 'pass' NEWLINE
              | 'break' NEWLINE
              | 'continue' NEWLINE
              | 'for' IDENT (':' type)? 'in' expr ':' body
              | 'while' expr ':' body                    # desugars to loop
              | 'loop' ':' body
              | binding | assignment | expr_stmt

binding     ::= IDENT (':' type)? '=' expr NEWLINE
assignment  ::= lvalue '=' expr NEWLINE              # lvalue is FieldAccess, Index, Name
expr_stmt   ::= expr NEWLINE

block       ::= INDENT stmt+ DEDENT
body        ::= block | stmt (';' stmt)* NEWLINE     # inline body ok
```

### Expressions

```
expr        ::= expr binop expr                      # binary
              | unop expr                            # unary
              | expr '?=' type                       # type test
              | expr '.' IDENT                       # field access
              | expr '(' args? ')'                   # call
              | expr '[' (type|expr) (',' …)* ']'    # generic / index
              | expr '{' field_init (',' field_init)* '}'  # struct literal (only on Name/GenericInst/FieldAccess)
              | expr '..' expr?                      # range (in subscript)
              | atom

atom        ::= INT | FLOAT | BYTE | STRING | 'true' | 'false'
              | IDENT | 'self' | 'Self' | '_'
              | '(' expr ')'
              | if_expr | match_expr

if_expr     ::= 'if' expr ':' body (elif_cont | else_cont)?
elif_cont   ::= 'elif' expr ':' body (elif_cont | else_cont)?
else_cont   ::= 'else' ':' body

match_expr  ::= 'match' expr ':' INDENT match_arm+ DEDENT
match_arm   ::= pattern body

args        ::= arg (',' arg)*
arg         ::= IDENT ':' expr                       # named arg
              | expr                                 # positional

field_init  ::= IDENT ':' expr
```

## Statement vs. expression

`if` and `match` are **expressions** — they yield values. They can
appear at statement position (in which case the value is discarded)
or at expression position. See
[control-flow.md](control-flow.md) for the value semantics.

```ouro
# expression position
x = if cond: 100 else: 200

# statement position (value discarded)
if cond: io.println("yes") else: io.println("no")
```

## End-of-line

Every statement ends with a `NEWLINE`. Inside brackets, newlines are
suppressed (so multi-line argument lists work without `\` continuations).

`;` is an **optional** same-line separator:

```ouro
if x: a = 1; b = 2; c = 3
```

You can use `;` between statements anywhere a `NEWLINE` would be
accepted. Most code uses one statement per line.

## What this spec does NOT cover here

- **Semantic constraints** (e.g. "you can only call `?=` on a
  union-typed value") live in [errors.md](errors.md) and
  [types.md](types.md).
- **Operator behavior** (e.g. integer division semantics, shift
  signedness) lives in [expressions.md](control-flow.md#expressions).
- **Runtime layout** (slice fat pointer, ARC header) lives in
  [memory.md](memory.md).

## Cross-references

- The lexer ([compiler internals](../lexer.md)) is the source of
  truth for tokenization rules.
- The parser ([compiler internals](../parser.md)) is the source of
  truth for the grammar.
- For each token kind, see [`src/tokens.py`](../../src/tokens.py).
