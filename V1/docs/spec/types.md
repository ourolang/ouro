# Types

Ouro is **statically typed** with type inference. Every expression
has a type, computed at compile time. Types form the contract between
producer and consumer; mismatches are compile errors.

## Primitive types

🟢 *Locked.*

### Integer types

```
i8   i16  i32  i64        # signed
u8   u16  u32  u64        # unsigned
isize  usize              # pointer-sized
```

`isize`/`usize` are the size of a pointer on the target platform —
64 bits on 64-bit hosts, 32 bits on 32-bit hosts.

### Floating-point types

```
f32  f64
```

### Boolean

```
bool                      # true or false; 1 byte at runtime
```

### Sentinel error types

```
Null                 # produced by a null weak[T] read
StopIteration             # produced by __next__ at end of iteration
```

These are the only built-in error types. User code can define more
via `struct ParseError: ...` etc.

### Numeric literal defaults

```ouro
x = 42       # x: isize  (default integer type)
y = 3.14     # y: f64    (default float type)
```

When the literal flows into a context that demands a specific type,
the literal is **comptime-coerced**:

```ouro
fn add(a: i32, b: i32) -> i32: ...

result = add(2, 3)        # 2 and 3 are i32 by context
```

## String type

🟢 *Locked.*

There is **no `str` type**. Strings are `[]u8` — byte slices.

```ouro
s = "hello"     # s: []u8
s.len           # length in bytes
```

This is a deliberate decision. UTF-8 strings are bytes; treating them
as a special type would require committing to a particular encoding
philosophy (UCS-2? UTF-32? grapheme clusters?). Ouro punts and lets
the user reason about bytes.

## Slice type `[]T`

🟢 *Locked.*

```ouro
nums: []i32 = ...
s: []u8 = "hello"
```

A slice is a **fat pointer** at runtime: `{ ptr: *T, len: usize }`.
16 bytes total on 64-bit platforms.

Slices **bump the ARC count** of their backing storage when created
— this is "borrow-style" but sound by default. See
[memory.md](memory.md#slices).

Slice operations:

```ouro
s.len             # length (number of elements)
s.ptr             # raw data pointer (offset 0 of fat pointer) — for FFI
s[i]              # element access
s[start..end]     # sub-slice (planned; not yet in v1 codegen)
```

🟡 *Status:* `s.len` and `s.ptr` work end-to-end; `s[i]` works for
slice indexing; `s[start..end]` parses but the codegen doesn't yet
emit a sub-slice operation.

## Struct types

🟢 *Locked.*

A struct is a named record:

```ouro
struct Point:
    x: i32
    y: i32
```

Struct instances are **heap-allocated** with an ARC header:

```
[ refcount (8) | drop_fn (8) | x (4) | y (4) ]
                              └ user pointer
```

The user-visible "struct value" is actually a pointer to the data
section (after the header). Construction goes through a struct
literal:

```ouro
p = Point { x: 1, y: 2 }
```

Field access:

```ouro
p.x               # read
p.x = 5           # write — only if field is var[T]
```

Methods are declared inside the struct body. See
[declarations.md](declarations.md#structs).

## Union types `T1 | T2 | …`

🟢 *Locked.*

A type that may be one of several variants. Used for error handling:

```ouro
fn parse_int(s: []u8) -> i64 | ParseError: ...
```

The runtime representation is a **tagged union**: `{ tag, payload }`.
The tag selects which variant is currently inhabited. See
[errors.md](errors.md) for narrowing semantics with `?=`.

Unions are normalized:
- **Order doesn't matter:** `i64 | ParseError` ≡ `ParseError | i64`.
- **No duplicates:** `i64 | i64 | ParseError` is invalid (would dedup
  to `i64 | ParseError`).
- **Nested unions flatten:** `(i64 | A) | B` ≡ `i64 | A | B`.

## Wrapper types — `var`, `const`, `rc`, `arc`, `weak`, `ptr`

🟢 *Locked.*

Six **mutability/allocation/indirection wrappers**. Written as a name
with `[ T ]`:

```ouro
var[T]            # mutable binding/field
const[T]          # explicitly immutable (default; rarely written)
rc[T]             # heap-allocated, non-atomic refcount
arc[T]            # heap-allocated, atomic refcount (thread-safe)
weak[T]           # synonym for T | Null; null on dangling/dropped
ptr[T]            # raw pointer; user-managed
```

Wrappers are **not** types in the same sense as `i32` — they describe
how a value is *held*. A function takes `T`, not `var[T]`; the var
wrapper applies to the binding/field, not to values flowing in/out.

🟡 *Status:* in v1, **`rc[T]` and `arc[T]` are descriptive** — both
use the same non-atomic runtime since there's no threading.  Today
they're equivalent to today's bare-struct heap allocation (every
struct is implicitly refcounted on the heap).  The v2 rework
(see [stack-and-arc.md](stack-and-arc.md)) makes them load-bearing:
structs default to stack, and only `rc[T]` / `arc[T]` annotations
trigger the heap path.

See [memory.md](memory.md) for the full semantics.

## `Self` type

🟢 *Locked.*

Inside a struct's methods, `Self` is the enclosing struct type
(possibly with type arguments):

```ouro
struct LinkedList[T]:
    head: var[_Node[T] | Null]

    fn new() -> Self:               # returns LinkedList[T]
        return Self { head: ... }

    fn push(self: ptr[var[Self]], v: T):
        ...
```

Outside a struct, `Self` is an error.

## Generics

🟢 *Locked (v1: pure duck-typing, no constraints).*

Generic parameters in square brackets:

```ouro
struct LinkedList[T]:
    head: var[_Node[T] | Null]

    fn push(self: ptr[var[Self]], v: T):
        ...

fn map[T, U](xs: []T, f: T -> U) -> []U:
    ...
```

🟢 *Status:* Generic **structs and free functions** are both
monomorphized end-to-end. The codegen walks the TypeMap, finds every
`StructTy(name, type_args)` instantiation, and emits a specialized
struct + methods named e.g. `Box_i32` for `Box[i32]`. For free
functions, each call site infers the substitution from arg types
(or honors explicit type args like `id[i32](42)`), registers the
specialization, and emits a worklist-driven body afterwards (`id_i32`,
`pick_i32_i64`). Multiple instantiations, nested generics
(`Box[Box[i32]]`), generic structs with `__drop__`, and generics
containing managed fields all work.

### Constraints (v2+)

🔵 *Open.* No constraint syntax in v1. Generics are pure duck-typed:
the body uses whatever methods/operators it wants, and that
constrains valid type arguments implicitly. v2+ will likely add
`fn f[T: Show](x: T)` or similar.

## Inference placeholder `?`

🟢 *Locked.*

The `?` placeholder is **only legal inside type brackets** as the
inner of a wrapper:

```ouro
n: var[?] = 0          # type inferred to var[isize] from RHS
```

You can't write `?` alone as a type. The intent is "I want this
slot to hold a `var[T]`, but figure out `T` from the value."

## Type inference rules

The type checker infers types in these contexts:

| Context | Source of inference |
|---|---|
| Untyped binding `x = expr` | The type of `expr` |
| `var[?]` binding | The type of the RHS, stripped of var/const wrappers |
| For-loop variable | The element type of the iterable |
| Match arm bindings | The pattern's type |

For typed bindings, the type is taken from the annotation; the RHS
must be **assignable** to it (see below).

## Assignability

Roughly: when can a value of type `S` flow into a slot of type `T`?

| Target `T` | Source `S` | Assignable? |
|---|---|---|
| `T` | `T` | yes |
| anything | `never` | yes (never is bottom) |
| numeric `T` | numeric `S` | yes (any-to-any; v1 lenient) |
| `T1 \| T2` | `T1` (or `T2`) | yes |
| `T1 \| T2 \| ...` | `S1 \| S2` | yes if every `Si` is assignable to some `Ti` |
| `[]E` | `[]E2` | yes if `E2` assignable to `E` |
| `var[T]` | `T` | yes (storing into a var slot) |
| `T \| Null` | `Null` | yes |
| anything else | else | no |

Numeric widening is **lenient** in v1 — there's no narrowing check,
so `i32 → i8` would silently pass. v2 will tighten this.

🟡 *Status:* As of the latest version, the type checker enforces
**call-site arg checking**: each positional argument must be
assignable to the corresponding parameter type, and arity must
match. Generic callees, named-arg calls, and module method calls
are skipped (no signature info available).

## Type aliases

🟢 *Locked.*  Syntax:

```ouro
# Plain alias — re-name a type expression.
Result: type = i64 | ParseError

# Generic alias — type parameters in square brackets.  Each use site
# substitutes the args into the body.
Option[T]: type = T | Null
```

Aliases are **transparent**: a `NamedType` referencing an alias
expands to the alias's body during type checking and code
generation, so `Option[i32]` and `i32 | Null` are
indistinguishable to the rest of the compiler.  No nominal
distinction, no new runtime representation, no helpers to convert.

Uses can appear anywhere a type can appear — function signatures,
struct fields, bindings, `?=` narrowing patterns:

```ouro
fn parse(s: []u8) -> Option[i64]:
    ...

fn use(x: Option[i64]):
    if x ?= Null:
        return
    # x narrowed to i64 here
```

The `type` keyword is what disambiguates an alias declaration from
a regular module constant (`Foo: i32 = 0`).  It only appears at the
top level — aliases cannot be declared inside functions or structs
(v1 simplification; revisit when there's a real use case).

**Limitations:**

- Aliases are pure substitutions — they don't introduce a fresh
  nominal type.  `type Meters: type = f64` lets you write `Meters`
  for clarity but doesn't prevent assigning a `Seconds` value to a
  `Meters` slot.  A `distinct` form for nominal aliases is
  open-deferred.
- No recursive aliases (`Tree: type = Tree | Leaf` is rejected via
  the same cycle-detection that catches `struct Node: ...; n: Node`
  — but in practice the typechecker may infinite-loop today; the
  declared limit is "the body type expression must not refer to the
  alias being defined").

## What's NOT in v1

- **First-class function types** (e.g. `fn(i32) -> i32` as a value
  type) — open. Today, function names refer to the function's *return
  type* in expression context, which is a temporary simplification.
- **Tuple types** — open. `(i32, bool)` is not a type form.
- **Enum types** other than tagged unions — closed: tagged unions
  are how Ouro models enums.
- **Nominal type aliases** — open. v1 ships only *transparent*
  aliases (see [Type aliases](#type-aliases)); a `newtype` /
  `distinct` form that introduces a fresh nominal type is not in v1.
- **Trait/interface types** — open. v2+ work.

## Cross-references

- [memory.md](memory.md) for `var`/`const`/`weak`/`ptr` semantics
- [errors.md](errors.md) for tagged unions and `?=`
- [declarations.md](declarations.md) for struct and method syntax
- [`src/typechecker.py`](../../src/typechecker.py) — the source of
  truth for type inference and assignability
