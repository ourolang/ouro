# `src/nodes.py` — the AST type system

> Defines every concrete AST node class, grouped into five categories
> (Types, Patterns, Expressions, Statements, Top-level), each with a
> Python `Union` alias. This file is **all data definitions, no
> logic**. It's the contract every later pass reads against.

## Why an AST is just dataclasses

Some compilers use a class hierarchy for AST nodes (`class Expression:
…`, `class IntLiteral(Expression): …`). Ouro uses **flat dataclasses
with a Union** instead:

```python
@dataclass class IntLiteral(Node): value: int; suffix: Optional[str]
@dataclass class FloatLiteral(Node): value: float; suffix: Optional[str]
…
Expression = Union[IntLiteral, FloatLiteral, …, Match]
```

Reasons:

1. **Discrimination is `isinstance`, not virtual dispatch.** Each pass
   walks the tree and dispatches on type. With dataclasses + Union,
   `isinstance(node, IntLiteral)` is the only check we need.
2. **No "abstract method" boilerplate.** Each pass writes its own
   logic; nothing about a node knows how to type-check itself or
   resolve itself or codegen itself. This separation of concerns is
   the entire point.
3. **Cheap to add a node.** New node = new dataclass + add to the
   relevant Union. No methods to override.

The downside: if you forget a case in an `isinstance` chain, there's
no compile-time exhaustiveness check. We accept this; the test suite
catches most of it.

## File structure

```
Span                          (lines 16-30)        source location helper
Node                          (lines 33-37)        base class with `span`
─── Types
    NamedType                 (lines 43-48)        `i32`, `Connection`
    GenericType               (lines 50-56)        `LinkedList[i32]`
    WrapperType               (lines 58-64)        `var[T]`, `ptr[T]`
    SliceType                 (lines 66-71)        `[]T`
    UnionType                 (lines 73-78)        `T1 | T2`
    InferType                 (lines 80-83)        `?`
    SelfType                  (lines 85-88)        `Self`
    Type = Union[…]           (lines 90-98)
─── Patterns
    ValuePattern              (lines 104-109)      `200:`
    TypePattern               (lines 111-117)      `n: i32:` or `_: Err:`
    WildcardPattern           (lines 119-122)      `_:`
    Pattern = Union[…]        (line 124)
─── Expressions
    IntLiteral, FloatLiteral, BoolLiteral, ByteLiteral, StringLiteral
    Name, Discard
    FieldAccess, Index, Range
    Argument, Call, GenericInstantiation
    FieldInit, StructLiteral
    BinaryOp, UnaryOp, TypeTest
    If, MatchArm, Match
    Expression = Union[…]
─── Statements
    ExprStatement, Binding, Assignment
    Return, Pass, Break, Continue
    For, Loop, Block
    Statement = Union[…]
─── Top-level
    Parameter, SelfParam
    Function, StructField, Struct
    Import, TopLevelBinding
    File
    TopLevel = Union[…]
```

## `Span` (lines 16-30)

```python
@dataclass(frozen=True)
class Span:
    file: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
```

Half-open `[start, end)`. Lines and columns 1-indexed for humans.
`frozen=True` so spans are hashable — they can be used as dict keys
or set members, useful for tooling.

Span lives here, in `nodes.py`, rather than in `tokens.py` so both
the AST and the token system can depend on it without a cycle. See
[tokens.md](tokens.md).

## `Node` base class (lines 33-37)

```python
@dataclass(kw_only=True)
class Node:
    span: Span
```

The only thing every node has in common is `span`. Marking the field
**keyword-only** at the base means subclass dataclasses can declare
their own positional fields without colliding:

```python
@dataclass class Function(Node):
    name: str
    generics: list[str]
    self_param: Optional[SelfParam]
    params: list[Parameter]
    return_type: Optional[Type]
    body: Block
```

Construction looks like:

```python
Function(name="f", generics=[], self_param=None, params=[],
         return_type=None, body=block, span=sp)
```

Without `kw_only=True`, `span` would have to be the first parameter
(because base-class dataclass fields come first), making every
construction call awkward.

## Types (lines 40-98)

The shape of every type slot in the language: `name: T`, `-> T`,
`var[T]`, etc.

### `NamedType(name: str)`

Just a name. `i32`, `Connection`, `T`. The resolver turns this into a
symbol; the type checker into a `Ty`. The lexer's word-tokenizer +
`KEYWORDS` table guarantees `name` is a real identifier.

### `GenericType(base: str, args: list[Type])`

`LinkedList[i32]`, `Result[T, E]`. The base is a *name* (string), not
a `Type` — generic instantiation always starts with a named base.
There's deliberately no support for "second-class" generic
applications like `(List | Stack)[i32]`.

### `WrapperType(wrapper: str, inner: Type)`

`var[T]`, `const[T]`, `weak[T]`, `ptr[T]`. The wrapper is a string
that's one of these four values. The parser enforces this:
`_parse_type_atom` only recognizes those four words as wrappers
(line 243 of parser.py).

The reason `wrapper` is a `str` rather than an `Enum`: it's simpler,
serializable, and only ever produced from a known set of inputs. A
typo in the parser would emit something wrong, but the resolver and
type checker would both immediately fail to recognize it. No silent
bugs, just a louder error.

### `SliceType(element: Type)`

`[]T`. Just a wrapper around the element type. The runtime
representation (`{ptr, len}` fat pointer) is determined by the
codegen, not encoded in the AST.

### `UnionType(variants: list[Type])`

`T1 | T2 | T3`. The parser flattens at AST construction time; the
type checker normalizes further to a `frozenset` (no duplicates) when
converting to `Ty`.

The grammar restricts where `|` can appear — it's only legal in *type
context*. The Pratt parser's `PIPE` infix entry (BP 40) handles type
unions inside expressions like `x ?= T1 | T2` because the right side
of `?=` is a type. Bitwise `|` is reserved for later.

### `InferType` and `SelfType`

Singletons — no fields besides `span`.

`InferType` is the `?` placeholder, only legal inside a type wrapper:
`var[?]`, `const[?]`. The parser emits it for the `?` token in type
position (line 228). Used in inferred bindings:

```ouro
n: var[?] = 0    # n's type is var[isize], inferred from RHS
```

`SelfType` is `Self` in type position — the enclosing struct type.
The lexer has `SELF_UPPER` for the `Self` keyword.

## Patterns (lines 101-124)

Three pattern kinds, used only inside `match` arms:

```python
@dataclass class ValuePattern(Node):     value: Expression
@dataclass class TypePattern(Node):      binding: Optional[str]; type: Type
@dataclass class WildcardPattern(Node):  pass
```

| Source | Pattern |
|---|---|
| `200:` | `ValuePattern(value=IntLiteral(200))` |
| `_:` | `WildcardPattern` |
| `n: i32:` | `TypePattern(binding="n", type=NamedType("i32"))` |
| `_: ParseError:` | `TypePattern(binding=None, type=NamedType("ParseError"))` |

The parser disambiguates these by colon-counting (`_parse_pattern`,
lines 282-348 of parser.py). The colon syntax is what makes Ouro's
match feel Python-like — the `:` separates pattern from body, and
some patterns happen to use `:` internally too.

## Expressions (lines 127-323) — the biggest category

### Literals (lines 130-167)

Five literal kinds. Each carries its **already-parsed value**:

- `IntLiteral.value: int` (e.g. `0xFF` becomes `255` here, not `"0xFF"`)
- `IntLiteral.suffix: Optional[str]` (e.g. `"i64"`, `"u32"`)
- `FloatLiteral.value: float`, suffix similarly
- `BoolLiteral.value: bool`
- `ByteLiteral.value: int` (0-255)
- `StringLiteral.value: bytes` (UTF-8 already encoded by the lexer)
- `StringLiteral.is_multiline: bool` (was the source `"""..."""`?)

The `is_multiline` flag is mostly informational; the byte content is
the same regardless of how the source quoted it. A future formatter
would use it to preserve original quoting style.

### Names and access (lines 170-204)

```python
Name(name: str)        # bare identifier reference
Discard               # the `_` placeholder
FieldAccess(obj, field: str)  # obj.field
Index(obj, index)      # obj[i] or obj[start..end]
Range(start, end)      # only inside Index
```

`Discard` is its own node type rather than `Name(name="_")` because
discarding has different semantics from naming. A `Discard` cannot be
read; the resolver doesn't try to resolve it.

`FieldAccess.field` is a string, not a `Name` node. There's no
"resolve a field name" — fields are looked up against the type at
type-check time, not against any scope.

`Range` only appears inside `Index`. Standalone `start..end` would
parse but doesn't have a meaning yet. The codegen handles slice
indexing via Range when we add it; for now `Index(SliceTy, IntLit)`
is the only form.

### Calls and construction (lines 207-244)

```python
Argument(name: Optional[str], value: Expression)
Call(callee: Expression, args: list[Argument])
GenericInstantiation(base: Expression, type_args: list[Type])
FieldInit(name: str, value: Expression)
StructLiteral(type: Type, fields: list[FieldInit])
```

Two interesting points:

**`Argument.name` is `Optional`.** Positional args have `name=None`;
keyword args (`f(x: 1)`) have `name="x"`. This unifies positional and
named into one list — no separate "keyword args" dict — which makes
the parser simpler at the cost of putting reordering responsibility on
later passes.

**`GenericInstantiation.base: Expression`.** Could be a `Name`
(`LinkedList`) or a `FieldAccess` (`Mod.LinkedList`). The base is
parsed as an expression and turned into a type at type-check time.

**`StructLiteral(type, fields)` with empty `fields` means "zeroed".**
`Foo {}` is the syntax for default-initialization, equivalent to
calling `Foo.__zeroed__()` (when we add that machinery).

### Operators (lines 247-271)

```python
BinaryOp(op: str, left, right)
UnaryOp(op: str, operand)
TypeTest(operand, type: Type)
```

`op` is a string — `"+"`, `"=="`, `"and"`, etc. The codegen has tables
keyed by these strings. **No exhaustiveness check** — if someone adds
a new operator, they have to remember to update the codegen tables.

`TypeTest` is the `?=` operator. Its right side is a `Type`, not an
`Expression`, which is why the Pratt parser has special handling for it.

### Control flow as expressions (lines 274-301)

```python
If(condition, then_block: Block, else_block: Optional[Block])
MatchArm(pattern: Pattern, body: Block)
Match(scrutinee, arms: list[MatchArm])
```

These are **Expression** nodes. Used at expression position (the value
of an `if`/`match` is the value of the executed branch) and at
statement position (via `ExprStatement`). The parser routes both:

```python
if tok.kind == TokenKind.IF:
    expr = self._parse_if()
    return ExprStatement(span=expr.span, expr=expr)
```

For `elif`: `If.else_block` wraps a single-statement Block containing
another `If`. So `if a: ... elif b: ... else: ...` parses as
`If(a, ..., Block([ExprStatement(If(b, ..., else_block))]))`. This is a
classic AST flattening; later passes don't need a special "elif" case.

## Statements (lines 326-419)

```python
ExprStatement(expr)
Binding(name, type: Optional[Type], value)
Assignment(target, value)
Return(value: Optional[Expression])
Pass, Break, Continue
For(binding: str, binding_type: Optional[Type], iterable, body: Block)
Loop(body: Block)
Block(statements: list[Statement])
```

### The Binding/Assignment split

Read the docstring on `Assignment` (lines 347-356) carefully:

> Re-assignments to a plain name are also Assignment nodes; the type
> checker distinguishes "this name is already bound as var[T]"
> (re-assignment) from "this name is fresh" (which would be a Binding).

That's mostly true, but in practice:

- The **parser** always emits `Binding` for `name = expr`.
- The **parser** emits `Assignment` only for non-trivial lvalues
  (`self.x = 1`, `arr[i] = v`).
- The **type checker** sees a `Binding` and decides at semantic-analysis
  time whether it's a fresh binding or a re-assignment, by checking
  scope.

So the AST distinction is: target is a non-trivial lvalue → `Assignment`;
target is a bare name → `Binding`. Both end up doing the same thing
semantically (modify state).

### `For` and `Loop`

```python
For(binding="x", binding_type=None, iterable=expr, body=Block(...))
Loop(body=Block(...))
```

`For` doesn't carry an iterator-protocol hook; the type checker derives
the loop variable type from the iterable's `__next__` return type at
check time. The codegen, for v1, lowers `for x in slice` directly to
an index-based loop without going through `__iter__/__next__`.

`Loop` is just an infinite loop. `for` over a range or any non-slice
iterable will eventually lower (via `__iter__`/`__next__`) to a `Loop`.

### `Block(statements)`

A flat list. **No parent pointer.** The AST is a tree, not a
graph. Each pass threads parent context through its own state (e.g.
the type checker's `_current_self_ty`).

## Top-level (lines 422-505)

```python
Parameter(name: str, type: Type)
SelfParam(type: Type, is_default: bool)
Function(name, generics, self_param, params, return_type, body)
StructField(name: str, type: Type)
Struct(name, generics, fields, methods)
Import(binding: str, path: str)
TopLevelBinding(name, type: Optional[Type], value)
File(path: str, declarations: list[TopLevel])
```

### `SelfParam.is_default`

```python
@dataclass class SelfParam(Node):
    type: Type
    is_default: bool
```

When the user writes a bare `self`, the parser fills in
`type = WrapperType("ptr", SelfType())` and sets `is_default=True`.
When the user writes `self: ptr[var[Self]]`, the parser uses that
exactly and sets `is_default=False`.

This bit is preserved through to the codegen so we can tell "user
opted in to a different receiver" from "we synthesized the default."
Today nothing reads `is_default` after the parser, but it's there
when we need it.

### `Function.generics: list[str]`

Just names. No constraints, no relations between params. Pure
duck-typed monomorphization is the v1 plan, and that's what the AST
supports. v2 will extend this with constraint expressions.

### `Struct.fields` and `Struct.methods` are separate lists

Even though syntactically they're interleaved in source:

```ouro
struct Foo:
    x: i32
    fn bar(self): pass
    y: i32
```

The parser separates them. Methods get their own list because they
have a different shape (`Function` vs. `StructField`). Fields preserve
declaration order in `fields`; methods preserve declaration order in
`methods`. Order between fields and methods is *not* preserved.

### `Import.path: str`

Already escape-processed (because the source is `import("path")` and
the path is a STRING literal, which the lexer decoded). So
`import("std/io")` produces `Import(binding="io", path="std/io")`.

The path is *informational only* — the resolver doesn't actually
fetch anything. Imports register a name as a `ModuleTy(path)`, and
field accesses on modules are left to the type checker (and ultimately
to whatever symbol the codegen emits).

### `File.declarations: list[TopLevel]`

The root of every parse tree. The list is in declaration order. The
resolver and type checker do their two-pass walks over this list:
first to register names, then to check bodies.

## A worked example

```ouro
fn add(a: i32, b: i32) -> i32:
    return a + b
```

Becomes:

```python
File(
    path="<input>",
    declarations=[
        Function(
            name="add",
            generics=[],
            self_param=None,
            params=[
                Parameter(name="a", type=NamedType(name="i32")),
                Parameter(name="b", type=NamedType(name="i32")),
            ],
            return_type=NamedType(name="i32"),
            body=Block(statements=[
                Return(value=BinaryOp(
                    op="+",
                    left=Name(name="a"),
                    right=Name(name="b"),
                )),
            ]),
        ),
    ],
)
```

Spans omitted for clarity; every node has one in reality.

## What this file does NOT contain

- **No printer / pretty-formatter.** `dataclass`'s auto-`__repr__` is
  what tests use. A real pretty-printer would be a separate module.
- **No visitor framework.** Each pass writes its own dispatch
  (`if isinstance(stmt, ...): ...`). For the codebase's size, this is
  cleaner than abstracting; for a much larger compiler we might add a
  visitor.
- **No constructors that allocate.** All nodes are simple dataclasses;
  the parser is the only producer.
- **No semantic information.** Resolution lives in `ResolutionMap`,
  types in `TypeMap`, layouts in the codegen — all separate from the
  AST.

## Cross-references

- The parser ([`src/parser.py`](../src/parser.py)) is the only producer
  of these nodes. See [parser.md](parser.md).
- The resolver ([`src/resolver.py`](../src/resolver.py)) walks them to
  build a `ResolutionMap`. See [resolver.md](resolver.md).
- The type checker ([`src/typechecker.py`](../src/typechecker.py))
  walks them again with the resolution map in hand. See
  [typechecker.md](typechecker.md).
- The codegen ([`src/codegen.py`](../src/codegen.py)) is the
  consumer-of-last-resort. See [codegen.md](codegen.md).
