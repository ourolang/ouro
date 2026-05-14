# `src/typechecker.py` — AST + ResolutionMap → TypeMap

> Walks the resolved AST, computes a semantic `Ty` for every type
> annotation and every expression, verifies assignments / mutability
> / return types. Errors are collected, not thrown. Consumes the
> `ResolutionMap` from the resolver; produces a `TypeMap` for the
> codegen.

## What the type checker is responsible for

Three things:

1. **Annotation → semantic type.** AST type nodes (`NamedType`,
   `WrapperType`, etc.) become semantic `Ty` values (`PrimTy`,
   `WrapperTy`, etc.) that are easier to compare, substitute, and
   reason about.
2. **Expression type inference.** Every expression's `Ty` is computed
   and recorded.
3. **Verification.** Assignments check assignability, returns check
   against the function's return type, mutability is enforced (no
   writing to a non-`var` slot).

The output is a `TypeMap` that the codegen reads.

## File structure

```
imports                                     (lines 1-68)
PrimTy / StructTy / SliceTy / UnionTy / WrapperTy / TypeParamTy /
  ModuleTy / UnitTy / NeverTy / UnknownTy   (lines 74-141)
_NUMERIC, _INT                              (lines 143-161)
_unwrap, _readable, _subst, _contains_unknown,
  _is_numeric, _is_int, _fmt, _make_union   (lines 167-262)
FnSig, StructInfo                           (lines 268-282)
TypeError_                                  (lines 287-294)
TypeMap                                     (lines 300-314)
TyEnv                                       (lines 320-346)
TypeChecker class                           (lines 352-1077)
  helpers                                   (lines 363-365)
  _ast_to_ty                                (lines 368-408)
  _collect_struct, _collect_fn_sig          (lines 412-456)
  _struct_subst                             (lines 458-463)
  check_file (entry, two-phase)             (lines 467-508)
  _check_struct, _check_fn                  (lines 512-551)
  _check_block, _check_stmt                 (lines 555-597)
  _check_binding, _check_assignment, _lvalue_is_mutable
                                            (lines 599-685)
  _check_for                                (lines 687-724)
  _check_expr / _infer_expr                 (lines 728-797)
  _infer_name, _infer_field_access,
    _infer_call, _infer_generic_instantiation,
    _infer_struct_literal, _infer_binary_op,
    _infer_if, _infer_match, _check_arm     (lines 801-1027)
  _is_assignable, _check_assignable         (lines 1031-1077)
_subtract_ty                                (lines 1083-1097)
typecheck() free function                   (lines 1103-1108)
```

## The `Ty` hierarchy (lines 74-141)

This is the file's **semantic data model**. Completely separate from
AST type nodes:

```python
@dataclass(frozen=True) class PrimTy:       name: str        # "i32", "bool", "Null", …
@dataclass(frozen=True) class StructTy:     name: str; type_args: tuple["Ty", ...] = ()
@dataclass(frozen=True) class SliceTy:      element: "Ty"
@dataclass(frozen=True) class UnionTy:      variants: frozenset["Ty"]   # no nested unions
@dataclass(frozen=True) class WrapperTy:    wrapper: str; inner: "Ty"   # var/const/weak/ptr
@dataclass(frozen=True) class TypeParamTy:  name: str        # T, U, …
@dataclass(frozen=True) class ModuleTy:     path: str
@dataclass(frozen=True) class UnitTy:       pass             # ()
@dataclass(frozen=True) class NeverTy:      pass             # divergent / unreachable
@dataclass(frozen=True) class UnknownTy:    pass             # "we couldn't figure it out"
```

`Ty` (line 126) is the union. Three sentinel singletons:

```python
UNIT: Ty = UnitTy()
NEVER: Ty = NeverTy()
UNKNOWN: Ty = UnknownTy()
```

### Why frozen?

`@dataclass(frozen=True)` makes instances **hashable** (dataclass auto-
hashes if you opt in via `eq=True, frozen=True`). This is critical:

- `frozenset[Ty]` for `UnionTy.variants` requires `Ty` to be hashable.
- We use `Ty`s as dict keys in some places (e.g. substitution maps).
- Structural equality (`PrimTy("i32") == PrimTy("i32")`) is automatic
  thanks to dataclass's `__eq__`.

### Why a separate hierarchy from AST types?

Three reasons:

1. **Substitution.** `_subst` walks a `Ty` replacing `TypeParamTy("T")`
   with whatever `T` was instantiated as. AST types don't have a
   substitution operation; they're tied to source positions.
2. **Normalization.** `UnionTy` normalizes via `frozenset` (no
   duplicates, no order). AST `UnionType` is a `list` (preserves
   source order).
3. **Synthesis.** Sometimes we construct a `Ty` that has no AST
   counterpart (e.g. `_make_union(*parts)` builds a synthetic union
   from a `weak[T]` read). Easier with a separate value type.

### `UnknownTy` — the safety valve

When the checker can't figure something out (an unresolved name, a
generic instantiation we don't model, a method on a module), it
returns `UNKNOWN`. The `_contains_unknown(ty)` predicate (lines
202-214) is checked before strict assignability checks:

```python
if not _contains_unknown(target_ty) and not _contains_unknown(val_ty):
    self._check_assignable(target_ty, val_ty, span)
```

This **prevents cascade errors**: an unresolved name in one place
shouldn't trigger 50 follow-on type errors. UnknownTy is treated as
"compatible with everything" by `_is_assignable`, so it dies quietly.

## Numeric type sets (lines 143-161)

```python
_NUMERIC = frozenset({
    "i8", "i16", "i32", "i64", "isize",
    "u8", "u16", "u32", "u64", "usize",
    "f32", "f64",
})
_INT = frozenset({…all integer types…})
```

Used by `_is_numeric` / `_is_int` predicates and the assignability
rule "any numeric → any numeric is allowed" (v1 lenient).

## Type helpers

### `_unwrap(ty)` and `_readable(ty)` (lines 167-178)

```python
def _unwrap(ty):    # peel ALL var/const/ptr/weak wrappers
    while isinstance(ty, WrapperTy):
        ty = ty.inner
    return ty

def _readable(ty):  # peel only var/const (the "you read it; you get the inner type" rule)
    if isinstance(ty, WrapperTy) and ty.wrapper in ("var", "const"):
        return ty.inner
    return ty
```

The split matters:

- **Reading** a `var[i32]` gives you `i32`. Use `_readable`.
- **Indirecting** through a `ptr[Foo]` doesn't auto-deref. Use
  `_readable` — it leaves `ptr` and `weak` in place.
- **Asking "what's the underlying type, ignoring all wrappers?"**
  Use `_unwrap`. Used when looking up a struct's fields: `obj_ty =
  _unwrap(self._ty(expr.obj))` lets `(ptr[var[Foo]])` be treated as
  `Foo` for field-lookup purposes.

### `_subst(ty, subst)` (lines 181-199)

```python
def _subst(ty, subst):
    if not subst: return ty
    if isinstance(ty, TypeParamTy):
        return subst.get(ty.name, ty)
    if isinstance(ty, StructTy) and ty.type_args:
        new_args = tuple(_subst(a, subst) for a in ty.type_args)
        return StructTy(ty.name, new_args) if new_args != ty.type_args else ty
    ...
```

Walks a `Ty`, replacing any `TypeParamTy("T")` with `subst["T"]` if
present. Used when instantiating a generic struct's fields with
concrete types (`LinkedList[i32]` → field type `T` becomes `i32`).

### `_contains_unknown(ty)` (lines 202-214)

```python
def _contains_unknown(ty):
    if isinstance(ty, (UnknownTy, TypeParamTy)): return True
    if isinstance(ty, WrapperTy): return _contains_unknown(ty.inner)
    if isinstance(ty, SliceTy): return _contains_unknown(ty.element)
    if isinstance(ty, UnionTy): return any(_contains_unknown(v) for v in ty.variants)
    if isinstance(ty, StructTy): return any(_contains_unknown(a) for a in ty.type_args)
    return False
```

Note: **`TypeParamTy` is treated as unknown** here. So a generic
function's body, where parameter types are `TypeParamTy("T")`, is
never strictly checked — pure duck-typing. The codegen punts on
generics anyway, so this is consistent.

### `_fmt(ty)` (lines 225-247)

Human-readable type printer for error messages: `var[LinkedList[i32]]`,
`T1 | T2 | Null`, `import("std/io")`, `()`, `never`. Always used
in error messages — never used for codegen.

### `_make_union(*parts)` (lines 250-262)

Flattens nested unions, deduplicates, returns the singular variant
if only one. Used when synthesizing union types like `T | Null`
from a `weak[T]` read.

## Function and struct metadata (lines 268-282)

```python
@dataclass class FnSig:
    generics: list[str]
    self_ty: Optional[Ty]      # None → static method / free function
    params: list[tuple[str, Ty]]
    return_ty: Ty

@dataclass class StructInfo:
    name: str
    generics: list[str]
    fields: dict[str, Ty]      # may contain TypeParamTy for generic structs
    method_sigs: dict[str, FnSig]
```

These are the type checker's **declaration tables**. Built during
phase 1 (signature collection), consumed during phase 2 (body
checking).

`StructInfo.fields` and `method_sigs` are dicts keyed by name —
field/method lookup is O(1).

## TypeMap (lines 300-314)

```python
@dataclass class TypeMap:
    _types: dict[int, Ty] = field(default_factory=dict)
    errors: list[TypeError_] = field(default_factory=list)

    def record(self, node, ty): self._types[id(node)] = ty
    def type_of(self, node): return self._types.get(id(node))
```

Same shape as `ResolutionMap`. Keyed by `id(node)`. Errors collected
in parallel.

`TypeError_.__str__` defers to the shared `format_diagnostic` helper
in [`src/diagnostics.py`](../src/diagnostics.py), which renders a
caret-pointing diagnostic — same renderer used by `LexerError`,
`ParseError`, and `NameError`.

**Only expressions are recorded.** Statement-level nodes (Return,
Assignment, Binding) are not in the map. Only expressions have a
"type" in any meaningful sense.

## TyEnv — type-of-name scope chain (lines 320-346)

```python
class TyEnv:
    def define(self, name, ty): …
    def redefine(self, name, ty): …          # narrowing after `?=`
    def lookup(self, name) -> Optional[Ty]: …
    def lookup_local(self, name) -> Optional[Ty]: …       # this scope only
    def lookup_enclosing(self, name) -> Optional[Ty]: …   # parents only
    def has_local(self, name) -> bool: …
```

### Why a separate scope tree from the resolver?

The resolver's `Scope` answers "what symbol is this name?" — one
answer per name in scope.

The type checker's `TyEnv` answers "what's the *current* type of
this name?" — and that can change after **narrowing**:

```ouro
fn parse() -> i64 | ParseError: …

x = parse()
if x ?= ParseError:
    return x       # `x` has type ParseError here
io.printf("%ld\n", x)    # `x` has type i64 here
```

Inside the `then` branch, `redefine(x, ParseError)`. Inside the
`else` branch (or the implicit else after the if-with-return),
`redefine(x, complement_type)`. The `redefine` operation is what
makes narrowing tractable.

### Three lookup variants

The variants exist because of the **Binding-vs-Assignment**
disambiguation in `_check_binding`:

- `lookup_local(name)`: "Have I already defined this name in the
  current scope?" Used to detect same-scope duplicates and re-assignments.
- `lookup_enclosing(name)`: "Is there a `var[T]` outside this scope
  I'm re-assigning to?" Used to allow re-assignment to outer var
  slots.
- `lookup(name)`: regular full-chain lookup.

## TypeChecker state (lines 353-359)

```python
class TypeChecker:
    self._res: ResolutionMap            # input
    self._map: TypeMap                  # output
    self._struct_info: dict[str, StructInfo]
    self._fn_sigs: dict[str, FnSig]
    self._current_self_ty: Optional[Ty]
    self._current_return_ty: Ty = UNIT
```

The two `_current_*` fields are **threading state** — they're set
when entering a struct or function and used by nested code without
being passed explicitly through every call.

`_current_return_ty` is what enables `return` statements inside
nested blocks (if / match / loop body) to check against the
**enclosing function**'s return type, not a hardcoded `UNIT`. This
was a fix during the development session.

## `_ast_to_ty` (lines 368-408)

The bridge from AST type nodes to semantic `Ty`:

```python
def _ast_to_ty(self, ast_type, subst):
    if ast_type is None: return UNIT          # no annotation → unit return type

    if isinstance(ast_type, NamedType):
        if ast_type.name == "Self": return self._current_self_ty or UNKNOWN
        sym = self._res.get(ast_type)
        if sym is None: return UNKNOWN
        if sym.kind == BUILTIN_TYPE: return PrimTy(ast_type.name)
        if sym.kind == STRUCT:       return StructTy(ast_type.name)
        if sym.kind == TYPE_PARAM:   return subst.get(ast_type.name, TypeParamTy(ast_type.name))
        if sym.kind == IMPORT:       return ModuleTy(sym.name)
        return UNKNOWN

    if isinstance(ast_type, GenericType):
        args = tuple(self._ast_to_ty(a, subst) for a in ast_type.args)
        return StructTy(ast_type.base, args)

    if isinstance(ast_type, WrapperType):
        if isinstance(ast_type.inner, InferType):
            return WrapperTy(ast_type.wrapper, UNKNOWN)
        return WrapperTy(ast_type.wrapper, self._ast_to_ty(ast_type.inner, subst))

    if isinstance(ast_type, SliceType): return SliceTy(self._ast_to_ty(ast_type.element, subst))

    if isinstance(ast_type, UnionType):
        # flatten nested unions
        variants = set()
        for v in ast_type.variants:
            vt = self._ast_to_ty(v, subst)
            if isinstance(vt, UnionTy): variants.update(vt.variants)
            else: variants.add(vt)
        return UnionTy(frozenset(variants))

    if isinstance(ast_type, SelfType): return self._current_self_ty or UNKNOWN
    if isinstance(ast_type, InferType): return UNKNOWN
    return UNKNOWN
```

The `subst` parameter is how generic-parameter substitution flows
in. When checking a method on `LinkedList[T]`, we pass
`subst = {"T": TypeParamTy("T")}` so internal `T` references resolve
consistently to the same symbolic param.

When checking `LinkedList[i32].new()`, we'd pass
`subst = {"T": PrimTy("i32")}` so the method's signature gets
instantiated with the concrete type.

The `Self` resolution (lines 372-373) reads `_current_self_ty`. If
we're outside a struct context, it's `None`, and we return `UNKNOWN`
— a misuse of `Self` should produce an "unknown type" downstream.

## Phase 1 / Phase 2 (lines 467-508)

```python
def check_file(self, tree):
    module_env = TyEnv()

    # Phase 1 — collect signatures
    for decl in tree.declarations:
        if isinstance(decl, Import):
            module_env.define(decl.binding, ModuleTy(decl.path))
        elif isinstance(decl, Struct):
            self._collect_struct(decl)
            module_env.define(decl.name, StructTy(decl.name))
        elif isinstance(decl, Function):
            sig = self._collect_fn_sig(decl, {})
            self._fn_sigs[decl.name] = sig
            module_env.define(decl.name, sig.return_ty)
        elif isinstance(decl, TopLevelBinding):
            ann_ty = self._ast_to_ty(decl.type, {}) if decl.type else None
            if ann_ty: module_env.define(decl.name, ann_ty)
            # value type resolved in phase 2

    # Phase 2 — check bodies
    for decl in tree.declarations:
        if isinstance(decl, Function):
            sig = self._fn_sigs[decl.name]
            self._check_fn(decl, sig, module_env, {})
        elif isinstance(decl, Struct):
            self._check_struct(decl, module_env)
        elif isinstance(decl, TopLevelBinding):
            val_ty = self._check_expr(decl.value, module_env)
            ann_ty = self._ast_to_ty(decl.type, {}) if decl.type else None
            if ann_ty and not _contains_unknown(ann_ty) and not _contains_unknown(val_ty):
                self._check_assignable(ann_ty, val_ty, decl.span)
            final_ty = ann_ty if (ann_ty and not isinstance(ann_ty, UnknownTy)) else val_ty
            module_env.define(decl.name, final_ty)

    return self._map
```

Same two-phase shape as the resolver, for the same reason: forward
references between top-level declarations.

A peculiarity of Phase 1: for a `Function` decl, we `module_env.define(decl.name, sig.return_ty)`
— that is, **a function name's "type" in the env is its return type**.
This is a simplification because we don't have first-class function
types yet. When `f` is referenced as a value (rare), we get its
return type. When `f(args)` is called, the type checker looks up
`_fn_sigs[f]` directly to get the full signature.

## Statement type-checking (lines 562-685)

`_check_stmt` is a big switch. The interesting cases:

### `_check_binding` (lines 599-654) — fresh vs. re-assignment

```python
def _check_binding(self, stmt, env):
    if stmt.name == "_":
        self._check_expr(stmt.value, env); return

    if stmt.type is None:
        # `name = expr` with no annotation
        local_ty = env.lookup_local(stmt.name)
        if local_ty is not None:
            # Same-scope re-assignment
            val_ty = self._check_expr(stmt.value, env)
            if isinstance(local_ty, WrapperTy) and local_ty.wrapper == "var":
                ... check_assignable(local_ty.inner, val_ty)
            else:
                self._err(f"'{stmt.name}' is not mutable (declare as var[T])")
            return

        outer_ty = env.lookup_enclosing(stmt.name)
        if outer_ty is not None and isinstance(outer_ty, WrapperTy) and outer_ty.wrapper == "var":
            # Re-assignment to outer var slot (e.g. inside for loop body)
            val_ty = self._check_expr(stmt.value, env)
            ... check_assignable(outer_ty.inner, val_ty)
            return

        # Genuinely fresh binding
        val_ty = self._check_expr(stmt.value, env)
        env.define(stmt.name, val_ty)
        return

    # Typed binding `name: T = expr`
    ann_ty = self._ast_to_ty(stmt.type, {})

    # var[?] inference branch
    if isinstance(ann_ty, WrapperTy) and isinstance(ann_ty.inner, UnknownTy):
        val_ty = self._check_expr(stmt.value, env)
        inner = _readable(val_ty)
        final_ty = WrapperTy(ann_ty.wrapper, inner)
        env.define(stmt.name, final_ty); return

    val_ty = self._check_expr(stmt.value, env)
    if not _contains_unknown(ann_ty) and not _contains_unknown(val_ty):
        self._check_assignable(_readable(ann_ty), val_ty, stmt.span)
    env.define(stmt.name, ann_ty)
```

Three sub-cases for `name = expr` (no annotation):

1. **`name` already in current scope.** Check it's a `var[T]` (else
   error: not mutable). Otherwise check assignability of value to
   the inner type.
2. **`name` in an enclosing scope as `var[T]`.** Same — check
   assignability. This is what makes:
   ```ouro
   n: var[i32] = 0
   for x in items:
       n = n + 1     # re-assigns the outer `n`, not a fresh binding
   ```
   work.
3. **Genuinely fresh.** `env.define(name, val_ty)` and we're done.

For typed bindings:

- **`var[?]` inference**: read value type, strip its readable wrappers,
  use it as the wrapper's inner type. So `n: var[?] = 0` becomes
  `var[isize]`.
- **Standard typed binding**: check assignability of value to the
  annotation's readable inner type.

### `_check_assignment` (lines 656-668)

```python
def _check_assignment(self, stmt, env):
    target_ty = self._check_expr(stmt.target, env)
    val_ty = self._check_expr(stmt.value, env)

    if not self._lvalue_is_mutable(stmt.target, env):
        self._err("assignment target is not mutable (not var[T])"); return

    if not _contains_unknown(target_ty) and not _contains_unknown(val_ty):
        base = _readable(target_ty)
        self._check_assignable(base, val_ty, stmt.span)
```

For non-trivial lvalues only (field access, index). Bare-name
re-assignments are `Binding`s (handled above).

### `_lvalue_is_mutable` (lines 670-685)

Checks whether an lvalue ultimately "comes from" a `var[T]` binding
or field. Rules:

- `Name`: must be a `var[T]` in scope.
- `FieldAccess`: the *field's declared type* must be `var[T]`.
- `Index`: lenient — return `True`.

The Index case is the leniency we noted in the review: `arr[i] = v`
on a non-var slice would pass. Tightening this is on the deferred
list.

### `_check_for` (lines 687-724) — iterator inference

```python
def _check_for(self, stmt, env, return_ty):
    iter_ty = self._check_expr(stmt.iterable, env)
    body_env = TyEnv(env)

    elem_ty = UNKNOWN
    inner_iter = _unwrap(iter_ty)
    if isinstance(inner_iter, StructTy):
        info = self._struct_info.get(inner_iter.name)
        if info:
            nxt = info.method_sigs.get("__next__")
            if nxt:
                nxt_ret = _subst(nxt.return_ty, self._struct_subst(inner_iter))
                # __next__ returns T | StopIteration; element = first variant
                if isinstance(nxt_ret, UnionTy):
                    non_stop = [v for v in nxt_ret.variants
                                if not (isinstance(v, StructTy) and v.name == "StopIteration")]
                    elem_ty = non_stop[0] if len(non_stop) == 1 else _make_union(*non_stop)
                else:
                    elem_ty = nxt_ret

    if stmt.binding != "_":
        ann_ty = self._ast_to_ty(stmt.binding_type, {}) if stmt.binding_type else elem_ty
        body_env.define(stmt.binding, ann_ty)

    for s in stmt.body.statements:
        self._check_stmt(s, body_env, return_ty)
```

The loop variable's type is **inferred from the iterable's `__next__`
method**. The convention: `__next__` returns `T | StopIteration`, and
we extract the non-`StopIteration` variant as the element type.

If we can't infer (no struct, no `__next__`, etc.), `elem_ty =
UNKNOWN`. The user can still provide an explicit `for x: T in items`
to override.

## Expression inference (lines 728-1027)

`_check_expr` is the wrapper that records the type:

```python
def _check_expr(self, expr, env):
    ty = self._infer_expr(expr, env)
    self._map.record(expr, ty)
    return ty
```

`_infer_expr` is the dispatcher:

```python
def _infer_expr(self, expr, env):
    if isinstance(expr, IntLiteral):
        return PrimTy(expr.suffix) if expr.suffix else PrimTy("isize")
    if isinstance(expr, FloatLiteral):
        return PrimTy(expr.suffix) if expr.suffix else PrimTy("f64")
    if isinstance(expr, BoolLiteral): return PrimTy("bool")
    if isinstance(expr, ByteLiteral): return PrimTy("u8")
    if isinstance(expr, StringLiteral): return SliceTy(PrimTy("u8"))

    if isinstance(expr, Name):                  return self._infer_name(expr, env)
    if isinstance(expr, FieldAccess):           return self._infer_field_access(expr, env)
    if isinstance(expr, Index):                 …
    if isinstance(expr, Range):                 return UNKNOWN
    if isinstance(expr, Call):                  return self._infer_call(expr, env)
    if isinstance(expr, GenericInstantiation):  return self._infer_generic_instantiation(expr, env)
    if isinstance(expr, StructLiteral):         return self._infer_struct_literal(expr, env)
    if isinstance(expr, BinaryOp):              return self._infer_binary_op(expr, env)
    if isinstance(expr, UnaryOp):               …
    if isinstance(expr, TypeTest):              return PrimTy("bool")
    if isinstance(expr, If):                    return self._infer_if(expr, env)
    if isinstance(expr, Match):                 return self._infer_match(expr, env)
    return UNKNOWN
```

### Literal types

- `IntLiteral` defaults to `isize` if no suffix (Zig-style "smart"
  integer literal). With a suffix, the suffix is the type.
- `FloatLiteral` defaults to `f64`.
- `BoolLiteral` → `bool`.
- `ByteLiteral` → `u8`.
- `StringLiteral` → `[]u8`.

### Name lookup (lines 801-829)

```python
def _infer_name(self, expr, env):
    sym = self._res.get(expr)
    if sym is None: return UNKNOWN

    if sym.kind in (PARAM, LOCAL, SELF_PARAM):
        ty = env.lookup(expr.name)
        return _readable(ty) if ty else UNKNOWN

    if sym.kind == MODULE_CONST:
        ty = env.lookup(expr.name)
        return _readable(ty) if ty else UNKNOWN

    if sym.kind == FUNCTION:
        sig = self._fn_sigs.get(expr.name)
        return sig.return_ty if sig else UNKNOWN

    if sym.kind == STRUCT:    return StructTy(expr.name)
    if sym.kind == IMPORT:    return ModuleTy(sym.name)
    if sym.kind == BUILTIN_TYPE: return PrimTy(expr.name)
    return UNKNOWN
```

The key dispatch: the `sym.kind` from the resolver tells us how to
look up the type. PARAM/LOCAL/SELF_PARAM/MODULE_CONST go through
the TyEnv (with `_readable` to strip var/const). FUNCTION returns
its signature's return type (treating bare `f` as "calls f"). STRUCT
returns a bare `StructTy`.

### Field access (lines 833-876)

```python
def _infer_field_access(self, expr, env):
    obj_ty = self._check_expr(expr.obj, env)
    base = _unwrap(obj_ty)

    if isinstance(base, ModuleTy): return UNKNOWN
    if not isinstance(base, StructTy): return UNKNOWN

    info = self._struct_info.get(base.name)
    if info is None: return UNKNOWN

    sub = self._struct_subst(base)

    # Field lookup
    if expr.field in info.fields:
        ft = info.fields[expr.field]
        ft = _subst(ft, sub)
        # weak[T] read → T | Null
        if isinstance(ft, WrapperTy) and ft.wrapper == "weak":
            inner = _subst(ft.inner, sub)
            return _make_union(inner, PrimTy("Null"))
        # var[T] / const[T] read → T (readable)
        return _readable(ft)

    # Method lookup
    if expr.field in info.method_sigs:
        sig = info.method_sigs[expr.field]
        ret = _subst(sig.return_ty, sub)
        ...
        return ret if not isinstance(ret, UnknownTy) else UNKNOWN

    return UNKNOWN
```

Three things to note:

1. **`weak[T]` reads produce `T | Null`** (lines 858-860). The
   implicit upgrade rule. So `node._next` (where `_next: weak[Node]`)
   has type `Node | Null`, and the user can `?=` to narrow.
2. **`var[T]` and `const[T]` reads produce `T`** via `_readable`.
3. **Module field accesses always return `UNKNOWN`** because we don't
   model module member types yet.

### Call (lines 880-918)

```python
def _infer_call(self, expr, env):
    for arg in expr.args: self._check_expr(arg.value, env)

    callee = expr.callee

    if isinstance(callee, FieldAccess):
        obj_ty = self._check_expr(callee.obj, env)
        base = _unwrap(obj_ty)
        if isinstance(base, StructTy):
            info = self._struct_info.get(base.name)
            if info and callee.field in info.method_sigs:
                sig = info.method_sigs[callee.field]
                sub = self._struct_subst(base)
                ret = _subst(sig.return_ty, sub)
                if isinstance(ret, StructTy) and ret.name == "Self": return base
                return ret
        return UNKNOWN

    if isinstance(callee, Name):
        sig = self._fn_sigs.get(callee.name)
        if sig: return sig.return_ty
        ty = env.lookup(callee.name)
        if ty is not None: return _readable(ty)
        return UNKNOWN

    self._check_expr(callee, env)
    return UNKNOWN
```

Three callee shapes: method call (FieldAccess), free function (Name),
anything else.

**Param-type checking** lives in `_check_call_signature` (called from
both the FreeFn and Method branches). It verifies arity and that each
positional arg is assignable to the corresponding param's readable
type. Generic callees, named-arg calls, and module method calls are
skipped intentionally (no signature info available or not yet modeled).

### Binary ops (lines 951-974)

```python
def _infer_binary_op(self, expr, env):
    left = self._check_expr(expr.left, env)
    right = self._check_expr(expr.right, env)

    op = expr.op
    if op in ("==", "!=", "<", ">", "<=", ">=", "and", "or"):
        return PrimTy("bool")

    if op in ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"):
        if _is_numeric(left) and _is_numeric(right):
            # prefer the non-default type
            if left.name == "isize" and right.name != "isize":
                return right
            return left
        if _is_numeric(left): return left
        if _is_numeric(right): return right

    return UNKNOWN
```

Key rule: arithmetic returns the **more specific numeric type**. If
the literal default (`isize`) meets a specific type (`i32`), the
specific one wins. So:

```ouro
x: i32 = 1
y = x + 2      # y has type i32, not isize
```

This is what makes literals "comptime" in spirit: their type is
inferred from context.

### `if` and `match` as expressions (lines 978-1027)

The narrowing trick at lines 986-996:

```python
def _infer_if(self, expr, env):
    self._check_expr(expr.condition, env)

    then_env = TyEnv(env)
    else_env = TyEnv(env)

    if isinstance(expr.condition, TypeTest):
        test = expr.condition
        if isinstance(test.operand, Name):
            name = test.operand.name
            original_ty = env.lookup(name)
            test_ty = self._ast_to_ty(test.type, {})
            if original_ty is not None:
                then_env.redefine(name, test_ty)
                complement = _subtract_ty(original_ty, test_ty)
                else_env.redefine(name, complement)

    self._check_block_in_env(expr.then_block, then_env)
    if expr.else_block is not None:
        self._check_block_in_env(expr.else_block, else_env)

    return UNKNOWN
```

When the condition is a `TypeTest` like `x ?= ParseError`:
- In the `then` branch, `redefine(x, ParseError)` — narrowed.
- In the `else` branch, `redefine(x, complement)` — what's left.

`_subtract_ty` computes the complement (lines 1083-1097):

```python
def _subtract_ty(original, to_remove):
    if isinstance(original, UnionTy):
        removed = (to_remove.variants if isinstance(to_remove, UnionTy)
                   else frozenset({to_remove}))
        remaining = original.variants - removed
        if not remaining: return UNKNOWN
        if len(remaining) == 1: return next(iter(remaining))
        return UnionTy(frozenset(remaining))
    return original
```

Only union types are interesting — non-union types stay as is.

`_infer_if` returns `UNKNOWN` because we don't yet compute the
common-supertype of the two branches. The codegen handles this via
runtime stack slots, but the type checker doesn't unify yet.

`_infer_match` is similar — checks each arm, returns `UNKNOWN`.

## Assignability (lines 1031-1077)

```python
def _is_assignable(self, target, source) -> bool:
    if target == source: return True
    if isinstance(target, UnknownTy) or isinstance(source, UnknownTy): return True
    if isinstance(source, NeverTy): return True

    if _is_numeric(target) and _is_numeric(source): return True

    if isinstance(target, UnionTy):
        if isinstance(source, UnionTy):
            return all(any(self._is_assignable(tv, sv) for tv in target.variants)
                       for sv in source.variants)
        return any(self._is_assignable(v, source) for v in target.variants)

    if isinstance(target, SliceTy) and isinstance(source, SliceTy):
        return self._is_assignable(target.element, source.element)

    if isinstance(target, WrapperTy) and isinstance(source, WrapperTy):
        if target.wrapper == source.wrapper:
            return self._is_assignable(target.inner, source.inner)
    if isinstance(target, WrapperTy):
        return self._is_assignable(target.inner, source)

    if isinstance(source, PrimTy) and source.name == "Null":
        if isinstance(target, UnionTy):
            return PrimTy("Null") in target.variants

    return False
```

### Three lenient bits worth flagging

1. **Numeric widening (lines 1042-1043).** Any numeric → any
   numeric. No range checking. So `i64 → i8` would pass.
2. **Unwrapping target wrappers (line 1062-1063).** Passing `42`
   (a bare `isize`) to a `var[isize]` slot is fine.
3. **`Null` flowing into any union containing `Null`.**
   Supports `weak[T]` semantics: writing `null` (some way of
   producing Null) into a `weak[T]` slot.

### The recursion order

For unions, the rule is:

- If both are unions: every source variant must be assignable to some
  target variant.
- If only target is a union: source must be assignable to some target
  variant.

This is the standard subtyping rule for sum types.

## What this pass does NOT do

- **Generic monomorphization.** `LinkedList[i32]`'s methods aren't
  cloned; `T` stays as `TypeParamTy("T")` in their bodies. Codegen
  punts on generics in v1.
- **Mutability across slices.** `arr[i] = v` is allowed regardless
  of whether `arr` is `var`.
- **Match exhaustiveness.** Arms aren't checked to cover the union.
- **Match arm uniformity.** `match x: …` returns `UNKNOWN` rather
  than the lowest-common-supertype of arms. So mismatched arm types
  go undetected.
- **Generic function arg type checking.** When the callee is generic,
  arg types are not yet compared to params (pending monomorphization).
- **`if`-expression branch type.** Same as match — returns `UNKNOWN`.

## Cross-references

- The resolver ([`src/resolver.py`](../src/resolver.py)) is the
  required input. See [resolver.md](resolver.md).
- The AST nodes ([`src/nodes.py`](../src/nodes.py)) are the input
  vocabulary. See [nodes.md](nodes.md).
- The codegen ([`src/codegen.py`](../src/codegen.py)) reads the
  resulting `TypeMap`. See [codegen.md](codegen.md).

## Related tests

[`test/test_typechecker.py`](../test/test_typechecker.py) — covers:
- Literal types (`IntLiteral` defaults to `isize`, etc.)
- Arithmetic and comparison results
- `var[?]` inference
- Mutability checks (writing to non-var fails)
- Return type checking (mismatch → error)
- Union assignability (numeric literal → union containing i32 works)
- TypeTest (`?=`) returns bool
- Struct field access (var/const/weak handling)
- For loops (element type via `__next__`)
- Method calls
