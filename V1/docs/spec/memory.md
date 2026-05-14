# Memory model

Ouro uses **automatic reference counting (ARC)** for heap-allocated
data. There is no garbage collector. Mutability and ownership are
expressed through six wrapper types: `var`, `const`, `rc`, `arc`,
`weak`, `ptr`. In v1 every struct is implicitly refcounted on the
heap regardless of wrapper; `rc[T]` / `arc[T]` annotations document
intent. The v2 rework (see [stack-and-arc.md](stack-and-arc.md))
makes the wrappers load-bearing: structs land on the stack unless an
`rc[T]` / `arc[T]` annotation forces the heap path.

> ⚠️ **Superseded by [stack-and-arc.md](stack-and-arc.md)** — the v2
> memory model moves to stack-by-default with explicit `arc[T]` for
> heap, and renames `weak[T]` to `warc[T]`.  This page documents the
> current heap-by-default implementation; once the v2 rework lands,
> it will be folded into stack-and-arc.md.

🟢 *Decisions documented here are the v1 implementation.* 🟡
*Implementation status varies — see notes inline.*

## Why ARC

The constraints:

- **No GC pauses.** Ouro is a systems language; predictable
  performance matters.
- **No manual `free`.** Manual memory management is too error-prone.
- **Cycle handling without a tracing collector.** ARC can leak
  cycles; the answer is `weak[T]` references at known cycle points.

ARC delivers all three:

- Each heap object carries a refcount; `inc` on copy/borrow, `dec`
  on drop. When count hits zero, the object's `__drop__` runs and
  memory is freed.
- The compiler inserts `inc`/`dec` calls at scope boundaries so the
  user never writes them manually.
- Cycles are broken by `weak[T]` fields, which **do not** bump the
  refcount — they fail safely (return `Null`) if the referent
  has been dropped.

🟡 *Implementation status:* Function-scope ARC works end-to-end:
- `arc_alloc(size, drop_fn)` is the runtime entry; struct construction
  goes through it.
- Each function tracks its managed locals.
- Before every `ret`, `arc_release` is emitted for every managed local
  *except* the value being returned (which transfers to the caller).
- `__drop__` is invoked when refcount hits zero.

Still **missing**:
- **Slice owner handle / refcount bump** — slices are still raw fat
  pointers; sharing a slice with the backing data dropped is unsafe.

## The wrappers

Six in total: `var[T]`, `const[T]`, `rc[T]`, `arc[T]`, `weak[T]`,
`ptr[T]`. The first two control mutability of the *binding*; the next
two control heap allocation + refcounting; `weak[T]` is a non-owning
reference; `ptr[T]` is a user-managed raw pointer. The sections below
cover them in detail. (`rc[T]` and `arc[T]` were added in the v2
design pass and behave identically in v1 — both compile to the
non-atomic refcount path documented under "weak/strong refcounts"
below. They diverge once threading lands.)

### `var[T]` — mutable binding/field

```ouro
n: var[i32] = 0
n = n + 1            # ok — n is var[i32]

fn inc(slot: var[i32]):
    slot = slot + 1
```

`var[T]` says "this slot can be reassigned." Without `var`, bindings
are **immutable**.

A `var[T]` field in a struct means the field can be written through
its container:

```ouro
struct Counter:
    count: var[i32]

c.count = c.count + 1     # ok
```

Reading from a `var[T]` slot gives you `T` (the wrapper is shed). So
`n + 1` works without unwrap/box/etc.

### `const[T]` — explicitly immutable

```ouro
PI: const[f64] = 3.14159
```

`const[T]` is the default — most bindings don't need it written
explicitly. It exists for cases where you want to make immutability
emphatic, or to match a struct field's expected mutability:

```ouro
struct Frozen:
    name: const[[]u8]      # explicitly cannot be reassigned
```

Reading a `const[T]` slot gives you `T`, just like `var[T]`.

### `ptr[T]` — raw pointer

```ouro
fn use_buf(buf: ptr[u8]):
    ...
```

`ptr[T]` is **user-managed**. No automatic refcount, no automatic
drop. Lifetime is the user's responsibility. Used for FFI, performance-
critical code, and sometimes as a method receiver (`self: ptr[Self]`
— see below).

You don't typically write `ptr[T]` for owned data — that's a misuse.
Use `T` (which is heap-allocated with ARC for structs) or a slice.

🟢 **Locked: construction via type-annotated binding.**  A `ptr[T]`
annotation on the LHS coerces the RHS to its address, mirroring how
`rc[T] = T { ... }` already promotes via the destination type:

```ouro
p = Point { x: 1, y: 2 }
addr: ptr[Point] = p          # &p — coerces because LHS asks for ptr
```

There's no `&` sigil and no `ptr.of(...)` intrinsic — the wrapper on
the LHS is the only construction form.  The address is whatever the
codegen already uses for that value (stack slot for bare structs,
heap user-data pointer for `rc[T]`/`arc[T]`).

⚠️ **Dangling is unchecked.**  A `ptr[T]` to a stack-bare struct goes
stale the moment the source leaves scope; pointing at an `arc[T]`
payload survives only as long as the caller holds an arc.  v1 has no
lifetime annotations — the rule is "trust the user," same status as
slices into stack values ([stack-and-arc.md](stack-and-arc.md#slices-into-stack-values)).

Passing a stack-bare value through a `ptr[T]` *parameter* is also
fine:

```ouro
fn read(p: ptr[Point]) -> i32:
    return p.x

p = Point { x: 1, y: 2 }    # stack
read(p)                     # callee's `p` is &(caller's slot)
```

The call site emits the pointer directly (`l %slot`), not the
`:Point` aggregate-by-value path used for bare-struct params — the
ABI follows the callee's declared parameter type.

### `weak[T]` — null-on-drop pointer

```ouro
struct _Node[T]:
    value: T
    next: var[_Node[T] | Null]
    prev: weak[_Node[T]]            # weak — breaks the cycle
```

`weak[T]` is **type-level synonym for `T | Null`**. When you
read a `weak[T]` field, you get `T | Null`. If the referent has
been dropped, you get `Null`; otherwise `T`.

Conceptually:

- A `weak[T]` field **does not** bump the refcount, so it doesn't
  prevent drops.
- When the referent is dropped, all `weak[T]` references to it
  invariantly produce `Null` thereafter.

```ouro
n: weak[Node] = ...
if n ?= Null:
    return                    # referent gone
io.println(n.name)            # n narrowed to Node here
```

The `?=` narrowing works on `weak[T]` reads exactly because
`weak[T]` is `T | Null` underneath.

🟡 *Implementation status:* Full pipeline works. The runtime has
24-byte ARC headers (`weak count`, `strong count`, `drop_fn`). The
codegen emits:

- `weak_inc(value)` when storing into a `weak[T]` field (instead of
  the regular `arc_inc`).
- `weak_release(value)` on overwrite or drop.
- A `weak_upgrade(value)` runtime call when *reading* a `weak[T]`
  field. The result is wrapped in a `T | Null` box —  the
  upgraded strong ref if alive, a Null marker if the referent
  was dropped. `?= Null` narrows correctly.

## Default vs. explicit `self`

🟢 *Locked.*

A method's first parameter is `self`. There are two ways to write it:

```ouro
struct Foo:
    fn bare(self):                # bare — default receiver
        ...

    fn explicit(self: ptr[var[Self]]):   # explicit
        ...
```

The **default** (bare `self`) is `ptr[Self]` — a raw pointer to the
struct, **read-only by default**. To mutate fields, write
`self: ptr[var[Self]]` (a pointer to a mutable Self).

The compiler distinguishes the two via `SelfParam.is_default`. Only
the parser fills in the default; later passes don't synthesize it.

## Static vs. instance methods

🟢 *Locked.*

A method without a `self` parameter is **static** — called on the
type:

```ouro
struct LinkedList[T]:
    fn new() -> Self:                  # static — no self
        return ...

    fn push(self: ptr[var[Self]], v: T):
        ...

list = LinkedList[i32].new()           # static call
list.push(42)                          # instance call
```

The `self`-vs-no-`self` distinction is the only marker. There's no
`static` keyword.

## Slices and borrow semantics

🟢 *Locked.*

A slice `[]T` is a **fat pointer**:

```
{ ptr: *T, len: usize }
```

16 bytes total on 64-bit platforms. It points into a backing buffer —
typically a heap-allocated array, but could be a stack array or a
data-section literal.

### ARC bump on slice creation

🟢 *Locked.* (Reverses an earlier brain-dump that said "no ARC bump.")

Slices **bump the refcount** of their backing storage when created.
This is "borrow-style" — the slice acts like a borrow — but it's
**sound by default** because the bump prevents the backing data from
being dropped while the slice is alive.

The slice carries an **owner handle** internally (in addition to the
visible `{ptr, len}`) — usually the original heap-allocated container.
On slice drop, the handle is dec'd.

```ouro
v = make_vec()         # vec on heap
s = v.slice(0..10)     # s bumps v's refcount
drop(v)                # v not actually freed yet — s holds it
io.printf("%ld\n", s.len)    # safe — backing data still alive
drop(s)                # finally freed
```

The compiler inserts the bump and the drop at scope boundaries.

🟡 *Implementation status:* The slice fat pointer is correctly
emitted (`{ptr, len}` on the stack). The owner handle and refcount
bump are not yet implemented — slices today are unchecked borrows.

### String literals don't bump

```ouro
s = "hello"
```

A string literal points into the **data section** (read-only static
storage), not heap-allocated memory. There's nothing to refcount — the
backing data is permanent. The slice's owner handle is null in this
case; the dec on drop is a no-op.

## `__drop__` — destructors

🟢 *Locked.*

A struct can declare a `__drop__` method (a dunder) that runs when
the struct's refcount hits zero:

```ouro
struct File:
    fd: var[i32]

    fn __drop__(self):
        if self.fd != 0:
            close(self.fd)
            self.fd = 0
```

When the compiler emits the alloc for a struct, it also stores a
pointer to `__drop__` in the ARC header's `drop_fn` slot (offset 8).
On dec-to-zero, the runtime calls `drop_fn(self)` before freeing.

🟡 *Implementation status:* The drop_fn slot is emitted (always 0
today). User `__drop__` methods are codegen'd as regular functions
but the slot isn't populated with their address yet, and the dec
logic isn't written.

## Memory layout summary

For a struct `Foo { x: i32, y: i64 }`:

```
heap allocation (32 bytes):

offset 0..8    : refcount (i64)
offset 8..16   : drop_fn (i64; 0 = no destructor)
offset 16..20  : x (i32)
offset 20..24  : padding (4 bytes; align y to 8)
offset 24..32  : y (i64)

user pointer = alloc + 16   ← what code sees
```

The user pointer is what every operation expects. To find the
header, subtract 16. To free, free the *header* pointer (i.e.
`alloc - 16`).

## What v1 doesn't yet do

| Feature | Status |
|---|---|
| ARC header layout | ✅ emitted |
| `arc_alloc` (heap allocation with header init) | ✅ emitted |
| `arc_release` at function exit | ✅ emitted (per-function scope) |
| `__drop__` invocation | ✅ runtime calls `drop_fn` on rc→0 |
| Return-value ownership transfer | ✅ skips release on consumed local |
| `arc_inc` on copy-binding (`y = x`, `y = obj.field`) | ✅ |
| `arc_inc` on returning a borrowed value (param, field) | ✅ |
| Managed struct fields (inc on store, release on drop) | ✅ — per-struct drop wrapper chains user `__drop__` then releases managed fields |
| Field assignment release-old + inc-new | ✅ — `obj.field = x` for managed fields |
| Block-scope releases (release at inner-block exit) | ✅ — each `Block` pushes a managed-locals scope; `break`/`continue` release down to the loop body's scope |
| Argument-passing inc/release | ❌ borrowed model (callee doesn't release args) |
| Field assignment inc + release-old | ❌ not yet |
| `weak[T]` null check / write | ❌ not yet |
| Slice owner handle / bump | ❌ not yet |
| Stack escape analysis | 🔵 not in v1 |

The end-to-end ARC tests in `test/test_e2e.py` cover the implemented
slice — single local struct, returned struct (transfer), multiple
locals, `__drop__` with side effects.

## Cross-references

- [types.md](types.md) for `T | E` union types (which `weak[T]`
  desugars to)
- [errors.md](errors.md) for `?=` narrowing on `weak[T]` reads
- [declarations.md](declarations.md) for `__drop__` syntax
- [`docs/codegen.md`](../codegen.md) for the QBE-level layout the
  codegen actually emits
