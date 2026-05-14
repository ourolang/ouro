# Duck-typing: `like[...]` shape constraints

🟡 *Implemented at the signature level — body method-shape verification
is still bare duck-typing.*

A way to **name** the duck-typed shape that v1 generics already enforce
implicitly. `like[...]` is a type expression that says "any type with
these methods" — usable on the right-hand side of a type alias, as a
parameter type, or anywhere else a type is expected.

## Motivation

v1 generics are [pure duck-typed](types.md#generics-v1): the body of
a generic function uses whatever methods/operators it wants, and that
implicitly constrains valid type arguments. Today, the constraint
lives only in the body — it's invisible at the signature:

```ouro
fn collect[T, I](it: I) -> Vec[T]:
    v = Vec[T].new()
    while true:
        n ?= it.__next__()
        if n ?= Null:
            break
        v.push(n)
    return v
```

A caller looking at this signature has to read the body to know that
`I` must have `__next__() -> T | Null`. With `like[...]` and
a type alias, the same shape becomes a named, reusable thing:

```ouro
Iter[T]: type = like[__next__() -> T | StopIteration]

fn collect[T](it: Iter[T]) -> Vec[T]:
    ...
```

Same monomorphization, same generated code — just self-documenting
at the signature.

## Syntax

A `like[...]` type lists one or more **method signatures** separated
by commas. Shapes are always open: listing methods only constrains
what must be *present*, not what must be absent.

```ouro
like[__next__() -> T | Null]
like[push(T), pop() -> T | EmptyError]
like[__len__() -> usize]
```

Each element inside `like[...]` is a method signature in
declaration form: `name(params) -> ret`. Receiver, parameter names,
and the `fn` keyword are **elided** — only the name, parameter
types, and return type appear.

A bare `like[...]` is rarely written inline; the expected idiom is
to bind it to a name with a type alias:

```ouro
Iter[T]: type = like[__next__() -> T | Null]
Show: type     = like[show() -> []u8]
Comparable[T]: type = like[__cmp__(T) -> i32]
```

Multi-line is allowed — newlines inside `[...]` are whitespace,
same as every other bracket-delimited construct in Ouro:

```ouro
Iter[T]: type = like[
    __next__() -> T | Null,
    __prev__() -> T | Null,
]
```

A trailing comma before the closing `]` is allowed.

Aliases are transparent (per [declarations.md](declarations.md)) —
the typechecker expands `Iter[T]` to its `like[...]` body wherever
it appears.

### Alias-from form: `like[StructName]`

🟡 Instead of listing methods explicitly, you can derive the shape
from a named struct. The bracket body is a single type reference;
the typechecker reads the struct's method set, drops the receiver,
substitutes any struct-level generic args, and uses the resulting
shape as the constraint:

```ouro
struct Counter:
    n: i32
    fn value(self) -> i32: return self.n

# Two equivalent forms:
fn read(c: like[value() -> i32]) -> i32: ...   # explicit
fn read(c: like[Counter])         -> i32: ...   # alias-from
```

Any struct exposing a `value() -> i32` method satisfies `like[Counter]`
— `Counter` itself, but also any unrelated type matching the shape.
Generic structs work too: `like[Pair[i32]]` derives `first() -> i32`
after substituting `T → i32`.

Lifecycle dunders (`__drop__`) are excluded from the derived shape.
Field-derived constraints aren't part of v1; only the struct's
methods participate in the shape.

## Semantics

🔵 **`like[...]` is a compile-time constraint, not a runtime type.**
v1 has no vtables, fat pointers, or existentials. When a function
takes a `like[...]` parameter, it's still generic underneath; the
compiler infers a fresh type parameter from the call site and
monomorphizes per concrete impl.

So:

```ouro
Iter[T]: type = like[__next__() -> T | Null]

fn collect[T](it: Iter[T]) -> Vec[T]: ...
```

is **lowered** to:

```ouro
fn collect[T, _LikeT1](it: _LikeT1) -> Vec[T]: ...
```

with a fresh implicit generic parameter `_LikeT1` (the name is
synthesized; users don't write or see it). Each call site picks
`_LikeT1` from the concrete argument type and triggers a
specialization. The codegen path is identical to the existing
duck-typed generics.

### What the typechecker checks (v1)

🟡 The v1 implementation **lifts** but does **not yet verify** the
shape against the concrete argument at the call site. The body still
exercises the constraint implicitly — if it calls `it.__next__()`,
that resolves against the concrete type at monomorphization time,
and a missing method becomes a codegen-time error.

🔵 The eventual check: at each call site, the concrete argument type
must have every listed method with a matching signature — same name,
same arity, invariant parameter types, return type assignable to the
listed return type.

Methods not listed in `like[...]` are ignored — the impl can have
more.

### Receiver kinds

Methods inside `like[...]` don't specify the receiver — every
implementation's receiver shape (`self`, `ptr[var[Self]]`, etc.)
is allowed. This matches v1's bare-`self` receiver sugar; the
constraint only cares that the call site `it.method(args)` typechecks.

## Composition with other type features

🔵 `like[...]` is a type expression like any other. It composes:

```ouro
# Optional iter — either an iterator or null.
MaybeIter[T]: type = Iter[T] | Null

# Generic over the constrained shape.
fn count[T](it: Iter[T]) -> usize:
    n: var[usize] = 0
    while true:
        x ?= it.__next__()
        if x is StopIteration:
            break
        n = n + 1
    return n
```

Inside a function, narrowing with `?=` works as usual on union types
that happen to involve `like[...]` aliases:

```ouro
fn use(maybe: MaybeIter[i32]):
    it ?= maybe
    if it ?= Null:
        return
    # it: Iter[i32] here
    ...
```

## Worked example: the iterator protocol

The for-loop sugar already
[desugars](control-flow.md#for-loops) `for x in xs:` to calls on
`xs.__iter__()` and `iter.__next__()`. With `like[...]`, those two
protocols become spec'd types in the standard prelude:

```ouro
# std/iter.ou (sketch)

Iter[T]: type = like[
    __next__() -> T | Null,
]

Iterable[T]: type = like[
    __iter__() -> Iter[T],
]
```

Any user-defined iterator type satisfies `Iter[T]` automatically
by implementing `__next__`:

```ouro
struct Range:
    cur: var[i64]
    end: i64

    fn __next__(self: ptr[var[Self]]) -> i64 | Null:
        if self.cur >= self.end:
            return Null {}
        v = self.cur
        self.cur = self.cur + 1
        return v

# Range satisfies `Iter[i64]` — no explicit `impl` block needed.
fn sum_to(n: i64) -> i64:
    total: var[i64] = 0
    r = Range { cur: 0, end: n }
    total = sum(r)        # sum: fn[T](it: Iter[T]) -> T
    return total
```

## Why `like` (and not `trait`, `shape`, `has`)

- `trait` is too loaded — readers expect Rust-style trait *objects*
  (vtable + dynamic dispatch), which is not what this is.
- `shape` is precise but verbose.
- `has` reads well for single methods (`has[push(v: T)]`) but
  awkward for multi-method shapes.
- `like` captures the intent — "any type that looks like this" — and
  stays short.

## Limitations (v1)

🔵 The first cut covers only what's needed for the iterator
protocol and a few stdlib utilities:

- **Methods only.** No fields, associated types, or constants
  inside `like[...]`. (A field-bearing constraint can be added
  later as `like[.len: usize]` if needed.)
- **Top-level only.** A parameter's *type* itself must resolve to a
  `LikeTy` for the lift to fire — nested usage (e.g.
  `Vec[like[...]]`) becomes `UNKNOWN` and is unsupported in v1.
- **No call-site shape verification.** v1 lifts the parameter to an
  implicit generic and lets the body's method calls implicitly drive
  the constraint, same as today's bare-generic duck-typing. The lift
  *does* attach the shape's method signatures to the synthesized
  type-param so call-on-param recovers the right return type — but
  the concrete arg isn't checked against the shape at the call site.
- **Invariant parameter types** (when verification ships).
- **No `like[...]` arithmetic.** You can't combine two shapes with
  `&` or compose them structurally; only via aliases.
- **No `like[Self.foo]`.** Methods referring back to `Self` are
  inferred from the concrete impl, not declared in the constraint.

These can grow as concrete needs appear. The locked piece is the
**syntax and lowering rule**: `like[...]` is sugar for an implicit
duck-typed generic parameter, monomorphized at compile time.

## Open questions

🔵 Still to settle before locking:

- Does `like[...]` need an explicit receiver marker (e.g.,
  `like[push(self, T)]`) for clarity, or is "the receiver is
  implicit, params are types only" enough?
- How do operator constraints look — `like[__add__(T) -> T]`,
  or a dedicated `Add[T]` alias in the prelude, or both?
- Can a struct *declare* the shape it satisfies as documentation
  (`struct Foo: type Iter[i32] = ...`)? Probably yes, as a no-op
  assertion checked by the typechecker — but deferred to v2.
- Should call sites verify the shape now, or wait for a clear use
  case? (Lifting alone covers documentation; checking adds friction
  when the impl signature differs by wrapper depth, etc.)
