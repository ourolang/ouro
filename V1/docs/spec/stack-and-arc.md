# Stack-by-default + `rc[T]` / `arc[T]` for explicit heap

🟢 *Stack-by-default is in.*  Five slices landed:

  1. **Bare struct bindings** with a struct-literal RHS use stack
     `alloca` + a direct `__drop__` call at scope exit.
  2. **Bare struct returns** use QBE's aggregate-return convention —
     the signature becomes `function :Foo $f(...)` and the caller
     receives a pointer to a caller-side slot QBE allocates per the
     SystemV ABI.  Two distinct values, two `__drop__` invocations
     (callee's local + caller's binding).
  3. **Bare struct args** use QBE's `:Foo` aggregate-by-value param
     ABI — QBE marshalls the byte copy at the call boundary, the
     callee's `%p` is a pointer to that fresh copy, and the copy's
     `__drop__` runs at function exit independently of the caller's.
     The call-site ABI follows the *callee's* declared param type, so
     a stack-bare value flows through a `ptr[T]` parameter as a plain
     pointer (no aggregate copy).
  4. **Inline composition** for bare-struct fields: an `Outer { b:
     Inner }` field embeds `Inner`'s bytes inline (no pointer).
     Field reads return the field's address (`:Inner`-typed), struct
     literals initialise nested fields with QBE's `blit`
     instruction, and the outer's auto drop wrapper chains directly
     into the inner's `__drop__` rather than `arc_release`-ing it.
  5. **`var[Struct]` reassignment** in place: the var slot holds the
     struct's bytes inline (alloca with the struct's full size).
     Reassignment runs the slot's `__drop__` on the outgoing value,
     then writes the new value via `_emit_struct_lit(into=slot)` for
     literals or `blit` for other expressions — no heap allocation,
     no pointer swap.

`rc[T]` / `arc[T]` annotated bindings, args, fields, and returns
keep the refcounted-heap path: `arc_alloc`, refcount manipulation,
single ownership transfer at the return boundary.

This document supersedes the heap-by-default model in
[memory.md](memory.md); the latter is now out-of-date and will be
folded in.

## Motivation

Today every `struct` literal compiles to `arc_alloc(...)` — a 24-byte
heap header plus refcount manipulation on every copy/scope-exit.
Three problems:

1. **Performance overhead** for short-lived structs that never
   actually need shared ownership.
2. **Heap pressure** when the working set is large.
3. **Mental model mismatch** with the rest of the language — primitive
   `var[T]` already lives on the stack; structs are the exception.

The rework: structs are **stack-allocated by default**.  Explicit
opt-in via `rc[T]` or `arc[T]` gives you today's heap+refcount
semantics when you actually need shared ownership.  `rc[T]` uses
non-atomic refcount ops (single-thread cheaper); `arc[T]` uses atomic
ops (thread-safe).  v1 has no threading, so both wrappers compile
identically today — the distinction primes the path for future
atomics without breaking user code.

## The wrapper set (v2)

| Wrapper    | Semantics                                                   |
|------------|-------------------------------------------------------------|
| `var[T]`   | Mutable binding/field (unchanged)                           |
| `const[T]` | Immutable binding/field (default; rarely written)           |
| `rc[T]`    | Non-atomic refcounted heap pointer (**new**; single-thread) |
| `arc[T]`   | Atomic refcounted heap pointer (**new**; thread-safe)       |
| `weak[T]`  | Weak ref to an `rc[T]` or `arc[T]` — `T \| Null` on read |
| `ptr[T]`   | Raw user-managed pointer (unchanged)                        |

🟢 **Locked: split refcount into `rc[T]` and `arc[T]`.**  v1 has no
threading, so today both wrappers compile to the same non-atomic
runtime operations — but the annotation already documents intent.
When threading lands, the `arc[T]` path switches to atomic primitives
without touching user code; `rc[T]` stays non-atomic for the cheaper
single-threaded case.  `weak[T]` keeps its name and works with values
of either kind.

## Construction

🟢 **Locked: implicit promotion from `arc[T]` annotations.**  The
struct literal stays unchanged; the destination type drives
allocation:

```ouro
# Stack — the Point lives in the caller's frame.
p = Point { x: 1, y: 2 }

# Heap (non-atomic refcount).  The literal is the same shape; the
# annotation says "wrap this in a heap allocation."
boxed: rc[Point] = Point { x: 1, y: 2 }

# Heap with atomic refcount.  Identical runtime today; once threading
# arrives, this will dispatch to atomic primitives.
shared: arc[Point] = Point { x: 1, y: 2 }

# Inferred: `rc[?]` / `arc[?]` resolves T from the RHS, mirroring
# the existing `var[?]` / `const[?]` placeholder.
also_boxed: rc[?] = Point { x: 1, y: 2 }
```

There is no `rc(...)` / `arc(...)` constructor call.  The annotation
form is the *only* way to land on the heap path; reading code, a
refcount wrapper on the LHS is the heap signal.

## Calling convention

🟢 **Locked: sret hidden pointer for stack-returned structs.**

```ouro
fn make_point() -> Point:        # Stack-returned
    return Point { x: 1, y: 2 }
```

The QBE-emitted signature is roughly:

```ssa
function $make_point(l %sret_ptr) {
@start
    storew 1, %sret_ptr
    storew 2, %sret_ptr+4    # or proper offset for y
    ret
}
```

The caller allocates the destination on its stack and passes the
address as a hidden first argument.  The callee writes the struct
directly into that slot.  No allocation in the function; no copy.

For `arc[Point]` returns, the value is a refcounted pointer — passed
in `%rax` as a plain `l`-typed integer like today.

```ouro
fn make_arc_point() -> arc[Point]:
    return Point { x: 1, y: 2 }   # destination type → implicit arc
```

## Struct field layout

🟢 **Locked: inline composition by default.**

```ouro
struct Outer:
    a: i32
    b: Inner       # Inner's bytes are laid out directly inside Outer
    c: arc[Inner]  # pointer-to-heap-Inner; 8 bytes on 64-bit
```

`Outer`'s size = `sizeof(i32) + sizeof(Inner) + 8` (plus alignment
padding).  This matches C and forces explicit indirection in the
recursive case:

```ouro
struct ListNode:
    value: i32
    next: ListNode          # ERROR — infinite size

struct ListNode:
    value: i32
    next: arc[ListNode]     # OK — pointer breaks the recursion
```

🔵 *Tooling:* the typechecker needs to detect recursive inline
composition and surface a "use `arc[T]` to break the cycle" error.

## `__drop__` semantics

Two firing points, depending on storage class:

- **Stack values**: `__drop__` runs at lexical scope exit (RAII).
  Same emission point the codegen uses today for `arc_release`, but
  the call is direct (`call $T____drop__(l %slot_addr)`) instead of
  going through a refcount.
- **`arc[T]` values**: `__drop__` still runs when the refcount hits
  zero, via the existing per-struct drop wrapper machinery.

Field destructors:

- An inline composed field (`b: Inner`) gets dropped *as part of*
  the outer's drop wrapper.  Same chain as today's "release each
  managed field," but inline.
- An `arc[T]` field gets `arc_release` on the outer's drop, exactly
  as today.

## Slices into stack values

🟡 *Open / partial.*  A slice (`[]T`) into a stack-allocated array
or struct field would dangle if it outlived the frame.  v1 already
defers the slice owner-handle, so this isn't worse than today —
but the rework makes it more visible.

For v2, we'll need *one* of:

- **Lifetime annotations** (Rust-style) — substantial language work
- **Stack-only slice marker** — slice types that the typechecker
  forbids from escaping
- **Just trust the user** (v1 status quo) — document the rule, no
  enforcement

🔵 *Lock the choice when we start writing user-facing slice ops.*

## Methods and `self`

Method receivers stay as `ptr[Self]` regardless of storage class:

```ouro
struct Point:
    x: i32
    y: i32

    fn translate(self: ptr[var[Self]], dx: i32, dy: i32):
        self.x = self.x + dx
        self.y = self.y + dy
```

For a stack-allocated `p: Point`, calling `p.translate(1, 2)` passes
`&p` (address of the stack slot) as `self`.  For
`q: arc[Point]`, calling `q.translate(...)` passes the arc payload
pointer (same address as the user data today).  The method body
doesn't need to know which.

Static methods (no `self`) are unchanged.

## Moving and copying

🟢 **Locked: copy semantics for stack structs (by-value).**

```ouro
p = Point { x: 1, y: 2 }
q = p                       # q is a fresh copy; p stays valid
q.x = 99                    # doesn't affect p
```

This is the systems-language norm.  `arc[T]` retains its current
ref-bump behavior:

```ouro
p: arc[Point] = Point { x: 1, y: 2 }
q = p                       # q and p share the same heap value (rc=2)
q.x = 99                    # affects what p sees too
```

Move semantics (consuming `p` so it can't be used again) are out
of scope for v1; no `move` keyword.

## Migration

The rework affects every existing test that today exercises
heap-allocated structs.  Plan:

1. **Audit** every `.ou` test file under `test/ouro/`.  Classify:
   - **Pure value semantics** (no aliasing, no `__drop__` reuse,
     no shared mutability) — works as-is after the rework.
   - **Needs heap** (returned structs that are then aliased,
     `weak[T]` usage, etc.) — adds an `arc[T]` annotation at the
     declaration site.
2. **No rename** — `weak[T]` keeps its v1 name and works with both
   `rc[T]` and `arc[T]` values.
3. **Recursive struct fields** (linked lists, trees) — add `arc[T]`
   wrapper.  These tests will break loudly without it.

Rough expectation: ~30–40% of tests get an `arc[T]` annotation
somewhere.  The bulk of `weak[T]` tests just rename.

## Compiler work breakdown

🔵 *Slicing plan, ~3–5 sessions.*

1. **Wrapper plumbing**: `rc[T]`, `arc[T]` token + parser + type
   system.  Both currently use the existing non-atomic runtime; the
   plumbing primes the `arc[T]` path for atomic dispatch when threads
   arrive.  `weak[T]` is unchanged.  Recursive-field cycle detection.

2. **Struct codegen, stack path**:
   - Struct literal lowers to `alloca` (size = struct's inline
     size, not 24 bytes).
   - Field access uses raw offsets, no ARC header offset.
   - `__drop__` invocation at scope exit calls the struct's drop
     wrapper directly on the slot address.

3. **Calling convention, sret**:
   - Functions returning stack structs get a hidden sret param.
   - Caller allocates the result slot; passes its address.
   - Codegen rewrites `return Point { ... }` to write into the
     sret slot, then `ret` with no value.

4. **Arc opt-in path**:
   - `arc[T]` annotation on the LHS triggers `arc_alloc` + ARC
     header + refcount, like today.
   - Copy bindings on `arc[T]` do `arc_inc`.
   - `arc[T]` return is a plain pointer in `%rax`.

5. **Field composition**: nested struct fields lay out inline; the
   drop wrapper chains through inline fields recursively.

6. **`weak[T]`**: unchanged from v1.  When atomics arrive, the
   weak-upgrade path will dispatch based on whether the target is
   `rc[T]` or `arc[T]`.

7. **Test migration**: walk the suite, add annotations, fix
   regressions.

## What this doesn't do

- **No lifetime annotations**.  Stack values can dangle through
  slices; we trust the user the same way we do today.
- **No move semantics / `move` keyword**.  v1 copies on `=`.
- **No struct-in-register optimization** (System V `cls_combined`).
  All stack returns use sret regardless of size.
- **No escape analysis**.  Returning a `ptr[Self]` to a stack value
  is unchecked.

## Cross-references

- [memory.md](memory.md) — current heap-by-default model (to be
  folded in)
- [types.md](types.md) — the wrapper-type set
- [`runtime/runtime.c`](../../runtime/runtime.c) — `arc_alloc` and
  related primitives (unchanged for the `arc[T]` path)
