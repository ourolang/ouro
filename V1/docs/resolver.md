# `src/resolver.py` — names → symbols

> Walks the AST and builds a `ResolutionMap`: for every `Name`,
> `NamedType`, and `GenericType` node, what does this name refer to?
> The map is consumed by the type checker. Errors are collected, not
> thrown — every undefined-name complaint comes back at once.

## What the resolver is responsible for

The job is **scope analysis**: build a tree of scopes, define names
in them, and for every name reference, walk up the scope chain to
find the matching definition. This is the same problem that any
language with lexical scoping has to solve — the resolver doesn't
care what `i32` *is*, only that it's a name reachable from this scope.

By doing this in a separate pass, the type checker can simply ask
"what symbol is this name?" via `ResolutionMap.get(node)` rather than
re-walking scopes. This separation of concerns also makes it
possible to write the resolver and type checker independently.

## File structure

```
imports                                     (lines 1-67)
BUILTIN_TYPES                               (lines 70-89)        primitive type names
SymbolKind enum                             (lines 95-104)
Symbol dataclass                            (lines 107-112)
NameError dataclass                         (lines 118-125)
Scope                                       (lines 131-152)
ResolutionMap                               (lines 158-172)
Resolver class                              (lines 178-468)
  helpers                                   (lines 185-191)
  resolve_file (entry, two-pass)            (lines 195-235)
  type resolution                           (lines 239-268)
  struct resolution                         (lines 272-288)
  function resolution                       (lines 292-312)
  block + statement resolution              (lines 316-362)
  expression resolution                     (lines 366-468)
resolve() free function                     (lines 474-481)
```

## `BUILTIN_TYPES` (lines 70-89)

```python
BUILTIN_TYPES: frozenset[str] = frozenset({
    "i8", "i16", "i32", "i64", "isize",
    "u8", "u16", "u32", "u64", "usize",
    "f32", "f64",
    "bool",
    "Null",  # sentinel for weak[T] / null pointer
})
```

These are the only names that aren't user-declared but exist as
types. They get **seeded into the module scope** at the start of
`resolve_file` (see below). Anything else (`StopIteration`,
`EmptyError`, etc.) has to be declared somewhere in source.

`Null` is special: it's the type of "no value here" for `weak[T]`
references and other nullable-pointer contexts. The type checker has
specific handling for it (`weak[T]` reads produce `T | Null`).

## `SymbolKind` (lines 95-104)

```python
class SymbolKind(Enum):
    FUNCTION       # fn name(…)
    STRUCT         # struct Name
    IMPORT         # name = import("…")
    MODULE_CONST   # top-level name [: T] = expr
    LOCAL          # local binding inside a function
    PARAM          # regular function parameter
    SELF_PARAM     # the `self` parameter
    TYPE_PARAM     # generic type parameter [T]
    BUILTIN_TYPE   # i32, bool, Null, …
```

Why so many kinds? Because **the type checker needs to dispatch
differently per kind**. A `FUNCTION` symbol's "type" is its return
type (used for callable lookups). A `STRUCT` symbol resolves to a
`StructTy`. A `BUILTIN_TYPE` becomes a `PrimTy`. A `PARAM` is looked
up in the local `TyEnv`. The kind tells the checker which path to
take.

`SELF_PARAM` is a special-case `PARAM`. It has its own kind so the
type checker can identify "this name is `self`" without string
comparison.

`TYPE_PARAM` is the kind for `[T, U]` generic parameters declared on
functions and structs. They get their own scope so they're only
visible inside the body of the declaring item.

## `Symbol` dataclass (lines 107-112)

```python
@dataclass class Symbol:
    kind: SymbolKind
    name: str
    span: Span
    node: Any
```

`node` is the *defining* AST node — `Function` for `FUNCTION`,
`Struct` for `STRUCT`, `Parameter` for `PARAM`, `None` for
`BUILTIN_TYPE`. Later passes use `node` to access declaration-specific
data without re-walking. Example: when the type checker sees a
`Name("foo")` resolving to a `FUNCTION` symbol, it can use
`sym.node.return_type` to get the AST return-type annotation.

## `NameError` (lines 118-125)

```python
@dataclass class NameError:
    message: str
    span: Span
    def __str__(self):
        return format_diagnostic(self.message, self.span)
```

Same shape as `LexerError`, `ParseError`, and the type checker's
`TypeError_` — all four route through the shared
[`src/diagnostics.py`](../src/diagnostics.py) renderer that produces
caret-pointing output with source-line context.

This is **a dataclass, not an Exception** — and that's intentional.
`NameError`s are *collected* into `ResolutionMap.errors`, not raised.
The caller decides what to do (print and exit, or continue to the
type checker for more errors).

## `Scope` (lines 131-152)

```python
class Scope:
    def __init__(self, parent: Optional["Scope"] = None, kind: str = "block"):
        self._bindings: dict[str, Symbol] = {}
        self.parent = parent
        self.kind = kind  # "module" | "struct" | "function" | "block"

    def define(self, sym):
        if sym.name in self._bindings:
            prev = self._bindings[sym.name]
            return NameError(f"'{sym.name}' already defined at ...")
        self._bindings[sym.name] = sym
        return None

    def lookup(self, name):
        if name in self._bindings: return self._bindings[name]
        return self.parent.lookup(name) if self.parent else None
```

A standard linked-scope chain. `define` returns a `NameError` (not
raises) on duplicates, which is the cooperative pattern: the
resolver is the only caller, and it puts the error in the
`ResolutionMap.errors` list instead of crashing.

The `kind` tag is documentation only — useful when debugging scope
issues and for future tooling that wants to treat function vs. block
scopes differently. It doesn't affect resolution behavior.

## `ResolutionMap` (lines 158-172)

```python
@dataclass class ResolutionMap:
    _refs: dict[int, Symbol] = field(default_factory=dict)
    errors: list[NameError]   = field(default_factory=list)

    def record(self, node, sym):
        self._refs[id(node)] = sym

    def get(self, node):
        return self._refs.get(id(node))
```

The map is keyed by `id(node)` (Python object identity). This works
because each AST node is a unique Python object — two `Name("foo")`
nodes at different positions are different objects. `id()` is fast
and unique for live objects.

Note `get(node)` returns `Optional[Symbol]`: a node that failed to
resolve (errors logged) just doesn't appear in `_refs`. Later passes
treat `None` as "unknown, fall back to lenient handling."

## The Resolver class

```python
class Resolver:
    def __init__(self):
        self._map = ResolutionMap()
        self._current_struct: Optional[Struct] = None  # for `Self` context
```

State is minimal. `_current_struct` is set when entering a struct's
methods so we can answer "is `Self` legal here?" — though in practice
it's not used (the resolver silently accepts `Self` everywhere; the
type checker handles legality).

## `resolve_file` — two-pass walk (lines 195-235)

```python
def resolve_file(self, tree):
    module = Scope(kind="module")

    # Seed built-in types directly (bypass duplicate check)
    dummy_span = Span(tree.path, 0, 0, 0, 0)
    for name in BUILTIN_TYPES:
        module._bindings[name] = Symbol(
            SymbolKind.BUILTIN_TYPE, name, dummy_span, None)

    # Pass 1 — register top-level names (forward references work)
    for decl in tree.declarations:
        if isinstance(decl, Function):
            self._define(module, Symbol(SymbolKind.FUNCTION, decl.name, decl.span, decl))
        elif isinstance(decl, Struct):
            self._define(module, Symbol(SymbolKind.STRUCT, decl.name, decl.span, decl))
        elif isinstance(decl, Import):
            self._define(module, Symbol(SymbolKind.IMPORT, decl.binding, decl.span, decl))
        elif isinstance(decl, TopLevelBinding):
            self._define(module, Symbol(SymbolKind.MODULE_CONST, decl.name, decl.span, decl))

    # Pass 2 — resolve bodies
    for decl in tree.declarations:
        if isinstance(decl, Function):       self._resolve_function(decl, module)
        elif isinstance(decl, Struct):       self._resolve_struct(decl, module)
        elif isinstance(decl, TopLevelBinding):
            self._resolve_type_opt(decl.type, module)
            self._resolve_expr(decl.value, module)
        # Import has no body
    return self._map
```

### Why two passes?

Because of **forward references**. `fn main` at line 5 might call
`fn helper` at line 10. If we walked top-down in one pass, `helper`
wouldn't be in scope when we resolve `main`'s body.

Pass 1 registers every top-level name. Pass 2 resolves bodies, with
all top-level names visible from the start.

### Why `BUILTIN_TYPES` bypasses `Scope.define`?

```python
for name in BUILTIN_TYPES:
    module._bindings[name] = Symbol(SymbolKind.BUILTIN_TYPE, name, dummy_span, None)
```

We write directly to `_bindings` instead of calling `define`. The
reason: `define` returns an error on duplicates. If a user happens to
declare `fn i32(): pass`, we want the error to come from a *user-level*
duplicate, not from the builtin seeding step. So the builtins go in
silently first; user definitions then collide as expected.

The `dummy_span = Span(tree.path, 0, 0, 0, 0)` is a fake span at line
0. It's only used in error messages saying "previous definition
here" — and pointing at line 0 makes it clear "this is a builtin."

## Type resolution (lines 239-268)

```python
def _resolve_type(self, typ, scope):
    if isinstance(typ, NamedType):
        if typ.name == "Self": return  # legal anywhere SelfType is
        sym = scope.lookup(typ.name)
        if sym is None:
            self._err(f"unknown type '{typ.name}'", typ.span)
        else:
            self._map.record(typ, sym)

    elif isinstance(typ, GenericType):
        sym = scope.lookup(typ.base)
        if sym is None: self._err(...)
        else: self._map.record(typ, sym)
        for arg in typ.args:
            self._resolve_type(arg, scope)

    elif isinstance(typ, WrapperType): self._resolve_type(typ.inner, scope)
    elif isinstance(typ, SliceType):   self._resolve_type(typ.element, scope)
    elif isinstance(typ, UnionType):   for v in typ.variants: self._resolve_type(v, scope)
    elif isinstance(typ, (InferType, SelfType)): pass
```

**`Self` is silently accepted** — both the `Self` keyword token
(which becomes `NamedType(name="Self")`) and the `SelfType` node.
The resolver doesn't try to resolve it. The type checker handles
`Self` via its `_current_self_ty` field.

**`InferType` (`?`) is silently accepted** in any type position. The
parser already restricts where it can appear (only inside wrapper
brackets), so by the time the resolver sees it, it's legal.

## Struct resolution (lines 272-288)

```python
def _resolve_struct(self, struct, parent):
    prev = self._current_struct
    self._current_struct = struct

    struct_scope = Scope(parent, kind="struct")
    for tp in struct.generics:
        self._define(struct_scope, Symbol(SymbolKind.TYPE_PARAM, tp, struct.span, struct))

    for f in struct.fields:
        self._resolve_type(f.type, struct_scope)

    for method in struct.methods:
        self._resolve_function(method, struct_scope)

    self._current_struct = prev
```

Structs **introduce a scope** containing their generic type
parameters. Field type annotations and method signatures resolve
against this scope, so `T` inside `LinkedList[T]`'s methods finds the
declared `T`.

The `_current_struct` save/restore is a simple guard for nested
structs (currently disallowed by the parser, but the pattern is
defensive).

## Function resolution (lines 292-312)

```python
def _resolve_function(self, fn, parent):
    fn_scope = Scope(parent, kind="function")

    for tp in fn.generics:
        self._define(fn_scope, Symbol(SymbolKind.TYPE_PARAM, tp, fn.span, fn))

    if fn.self_param is not None:
        self._resolve_type(fn.self_param.type, fn_scope)
        self._define(fn_scope, Symbol(SymbolKind.SELF_PARAM, "self", fn.self_param.span, fn.self_param))

    for p in fn.params:
        self._resolve_type(p.type, fn_scope)
        self._define(fn_scope, Symbol(SymbolKind.PARAM, p.name, p.span, p))

    self._resolve_type_opt(fn.return_type, fn_scope)
    self._resolve_block(fn.body, fn_scope)
```

The order matters:

1. Open function scope.
2. Define generic params (so they can be referenced in self/param/return types).
3. Define `self` (if present).
4. Resolve param type annotations *before* defining the param names —
   so `fn f(x: T, y: x)` would *not* resolve `y`'s type to the
   parameter `x`. (Actually the resolver doesn't even let `x` appear
   in a type position there because params aren't in scope yet at
   that point — but the order matters anyway.)
5. Resolve return type.
6. Recurse into body.

### Bare `self` and the synthesized type

Recall that bare `self` in source produces a parser-synthesized
`WrapperType("ptr", SelfType())`. When the resolver hits this, it
recursively resolves through the wrapper to the `SelfType`, which
silently accepts. So bare `self` is a no-op resolution-wise; the
information is preserved in the AST and consumed by later passes.

## Block and statement resolution (lines 316-362)

```python
def _resolve_block(self, block, parent):
    scope = Scope(parent, kind="block")
    for stmt in block.statements:
        self._resolve_stmt(stmt, scope)
```

Every block introduces a scope. Names defined in a block don't leak
to the outer scope.

### `Binding`: define after resolving (lines 325-333)

```python
elif isinstance(stmt, Binding):
    self._resolve_type_opt(stmt.type, scope)
    self._resolve_expr(stmt.value, scope)
    if stmt.name != "_":
        self._define(scope, Symbol(SymbolKind.LOCAL, stmt.name, stmt.span, stmt))
```

The order is: **resolve RHS first, then define the name**. This is
the classic shadowing rule. So `x = x` correctly references the
*outer* `x`:

```ouro
x = 1
fn f():
    x = x + 1   # `x` on RHS = outer x; `x` on LHS = newly defined inner x
```

### `Assignment`: no new binding (lines 335-337)

```python
elif isinstance(stmt, Assignment):
    self._resolve_expr(stmt.target, scope)
    self._resolve_expr(stmt.value, scope)
```

Both target and value are resolved as expressions. No `_define` —
because the parser only emits `Assignment` for non-trivial lvalues
(`self.x = 1`, `arr[i] = v`), and those don't introduce new names.

For bare-name re-assignments (`n = n + 1` where `n` is already
bound), the parser emits a `Binding`, and the type checker decides
at semantic time whether it's a "fresh binding" or "re-assignment to
existing var[T]".

### `For`: loop variable scope (lines 346-356)

```python
elif isinstance(stmt, For):
    self._resolve_type_opt(stmt.binding_type, scope)
    self._resolve_expr(stmt.iterable, scope)
    body_scope = Scope(scope, kind="block")
    if stmt.binding != "_":
        self._define(body_scope, Symbol(SymbolKind.LOCAL, stmt.binding, stmt.span, stmt))
    for s in stmt.body.statements:
        self._resolve_stmt(s, body_scope)
```

We **don't call `_resolve_block`** on `stmt.body`. Why? Because if we
did, it would create a *new* nested scope, and the loop variable
defined in `body_scope` wouldn't be visible to the body's statements.

So instead we manually create `body_scope`, define the loop variable
in it, and walk `stmt.body.statements` directly with that scope. The
resulting scope structure is:

```
function scope
  └─ for-body scope (loop variable lives here)
```

If we wrote `for x in items: y = x + 1`, the body's statements
include `y = x + 1`. The `_resolve_stmt(Binding(...))` call sees `y`
not yet defined, so it resolves `x` (finds it in body_scope) and
defines `y` in body_scope.

## Expression resolution (lines 366-445)

```python
def _resolve_expr(self, expr, scope):
    if isinstance(expr, Name):
        if expr.name in ("_", "self", "Self"):
            if expr.name not in ("_",):
                sym = scope.lookup(expr.name)
                if sym is None: self._err(f"'{expr.name}' used outside a method", expr.span)
                else:           self._map.record(expr, sym)
            return
        sym = scope.lookup(expr.name)
        if sym is None: self._err(f"undefined name '{expr.name}'", expr.span)
        else:           self._map.record(expr, sym)

    elif isinstance(expr, (IntLiteral, FloatLiteral, BoolLiteral, ByteLiteral, StringLiteral, Discard)):
        pass  # no names to resolve

    elif isinstance(expr, UnaryOp):           self._resolve_expr(expr.operand, scope)
    elif isinstance(expr, BinaryOp):
        self._resolve_expr(expr.left, scope); self._resolve_expr(expr.right, scope)
    elif isinstance(expr, TypeTest):
        self._resolve_expr(expr.operand, scope); self._resolve_type(expr.type, scope)
    elif isinstance(expr, FieldAccess):
        self._resolve_expr(expr.obj, scope)
        # field name is resolved by the type checker
    elif isinstance(expr, Index):             ...
    elif isinstance(expr, Range):             ...
    elif isinstance(expr, Call):
        self._resolve_expr(expr.callee, scope)
        for arg in expr.args:
            self._resolve_expr(arg.value, scope)
    elif isinstance(expr, GenericInstantiation):
        self._resolve_expr(expr.base, scope)
        for ta in expr.type_args:
            self._resolve_type(ta, scope)
    elif isinstance(expr, StructLiteral):
        self._resolve_type(expr.type, scope)
        for fi in expr.fields: self._resolve_expr(fi.value, scope)
    elif isinstance(expr, If):
        self._resolve_expr(expr.condition, scope)
        self._resolve_block(expr.then_block, scope)
        if expr.else_block is not None: self._resolve_block(expr.else_block, scope)
    elif isinstance(expr, Match):
        self._resolve_expr(expr.scrutinee, scope)
        for arm in expr.arms: self._resolve_arm(arm, scope)
```

A big switch. Note:

**Field names aren't resolved here.** The line `# field name is
resolved by the type checker` is the design decision: fields belong
to *types*, not *scopes*. The resolver doesn't have the obj's type;
the type checker does.

**Self/Self-as-expressions** (lines 367-376): `self` and `Self` look
up normally — they were defined in the function/struct scope when
entering. If they don't resolve, the error message is "used outside
a method" rather than "undefined name", which is the right level of
specificity.

**Discard `_` is silently accepted everywhere.** No symbol attached.
The line `if expr.name not in ("_",):` skips the lookup for it.

## Match arm resolution (lines 447-468)

```python
def _resolve_arm(self, arm, scope):
    arm_scope = Scope(scope, kind="block")

    if isinstance(arm.pattern, ValuePattern):
        self._resolve_expr(arm.pattern.value, scope)   # outer scope!
    elif isinstance(arm.pattern, TypePattern):
        self._resolve_type(arm.pattern.type, scope)
        if arm.pattern.binding is not None:
            self._define(arm_scope, Symbol(SymbolKind.LOCAL, arm.pattern.binding, ...))
    elif isinstance(arm.pattern, WildcardPattern):
        pass

    for stmt in arm.body.statements:
        self._resolve_stmt(stmt, arm_scope)
```

Two subtleties:

**`ValuePattern.value` resolves in the outer scope, not the arm
scope.** A pattern like `match x: foo: ...` — the `foo` in the pattern
references a name from the outer scope (e.g. a constant), not a name
defined inside the arm.

**The arm body walks `arm.body.statements` directly**, like the for-loop
case. We don't call `_resolve_block`, because we need the
pattern-introduced binding (for type patterns with binding) to be in
scope inside the body.

## What this pass does NOT do

- **Resolve module member names.** `io.println` — the `io` name
  resolves to an `IMPORT` symbol, but `println` (the field) is left
  alone.
- **Track types.** `i32` resolves to a `BUILTIN_TYPE` symbol with name
  `"i32"`, but no semantic type info is computed yet.
- **Check that imports actually exist.** `io = import("std/io")`
  registers `io`, but doesn't verify the path or fetch anything.
  Imports are informational at this stage.
- **Detect unused names.** Future improvement.
- **Warn about shadowing.** Bindings can shadow outer names freely.
  No warning.

## A worked example

For:

```ouro
fn add(a: i32, b: i32) -> i32:
    return a + b
```

After resolution, the `ResolutionMap._refs` contains roughly:

```
NamedType("i32", at param a)  → Symbol(BUILTIN_TYPE, "i32")
NamedType("i32", at param b)  → Symbol(BUILTIN_TYPE, "i32")
NamedType("i32", at return)   → Symbol(BUILTIN_TYPE, "i32")
Name("a", in body)             → Symbol(PARAM, "a", node=Parameter(...))
Name("b", in body)             → Symbol(PARAM, "b", node=Parameter(...))
```

No entries for the `BinaryOp("+")` node (operators don't go through
the symbol table) or the literals (no names to resolve).

## Cross-references

- The AST nodes ([`src/nodes.py`](../src/nodes.py)) are the input
  vocabulary. See [nodes.md](nodes.md).
- The parser ([`src/parser.py`](../src/parser.py)) produces the AST.
  See [parser.md](parser.md).
- The type checker ([`src/typechecker.py`](../src/typechecker.py))
  is the next consumer. See [typechecker.md](typechecker.md).

## Related tests

[`test/test_resolver.py`](../test/test_resolver.py) — covers:
- Built-in types resolve to BUILTIN_TYPE symbols
- Forward references between top-level functions
- Scope nesting (function, block, struct)
- Self / Self resolution inside methods
- For-loop variable scoping
- Match arm bindings
- Undefined-name errors with correct spans
- Duplicate-definition errors
- Shadowing in nested scopes
