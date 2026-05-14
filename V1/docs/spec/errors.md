# Error handling

Ouro has **no exceptions**. Errors are values — modeled as **tagged
unions** like `i64 | ParseError`. The `?=` operator narrows a union
type to a specific variant, controlling flow.

🟢 *Decisions in this doc are locked.* 🟡 *Implementation status:*
the type checker handles narrowing fully; the codegen emits real tag
comparisons for `?=`; **payload extraction works** so reading fields
on a narrowed value (`e.msg` after `?= ParseError`) is supported.
Per-union `drop_fn` helpers release boxed struct payloads when the
box's refcount hits zero.

## The model

A function that can fail returns `T | E`:

```ouro
struct ParseError:
    msg: []u8

fn parse_int(s: []u8) -> i64 | ParseError:
    if s.len == 0:
        return ParseError { msg: "empty input" }
    return 42
```

The caller gets a value that's *either* the success type or the
error type. Before using it as the success type, they must check.

## Runtime representation

A union value is a **tagged union** in memory:

```
{ tag: usize, payload: [16 bytes] }
```

- `tag` discriminates which variant is inhabited.
- `payload` holds the value (inline if small enough; pointer
  otherwise). 16 bytes is enough for any of: a primitive, a slice
  fat pointer, or a heap-pointer.

The compiler assigns tag values per variant — by convention, `0` is
the "happy path" (the leftmost variant) and increasing tags follow.

🟡 *Implementation status:* Union returns are emitted via `arc_alloc(16, 0)`
boxing — see `_emit_union_return` in `src/codegen.py`. Tag is the index
of the value's variant in a list of the union's variants sorted by
`_fmt()` for determinism (so producer and consumer agree). The
**16-byte payload is stored as a single 8-byte slot today**; values
narrower than 8 bytes are sign-extended. Variant types whose payload
needs more than 8 bytes (i.e. nothing in v1 — structs are pointers,
primitives fit) would currently overflow.

Known v1 limitations:
- The 8-byte inline payload is sufficient for primitives and pointers;
  variant payloads larger than 8 bytes (none in v1 — structs are
  always pointers) would overflow.
- Inside the union box's drop_fn, payload extraction is done assuming
  managed payloads are heap struct pointers (one byte == one struct
  pointer). Same assumption as the rest of the v1 ABI.

## `?=` — the type-test operator

🟢 *Locked.*

```
expr ?= Type
```

Returns `bool`. **And** narrows `expr`'s type inside the enclosing
`if` branches.

```ouro
x = parse_int("42")              # x: i64 | ParseError
if x ?= ParseError:
    return x                     # x narrowed to ParseError here
io.printf("%ld\n", x)            # x narrowed to i64 here
```

### Narrowing rules

When `cond` is a `?=` test on a name `n`:

- In the `then` branch, `n` has the tested type.
- In the `else` branch (or the implicit else after `if ... return`),
  `n` has the **complement** — the union with the tested type
  removed.

If the complement is a single type, it's that type. If the complement
is empty, it's `never` / `UNKNOWN`. If the complement has multiple
variants, it stays a union.

```ouro
x: A | B | C = ...
if x ?= A:
    # x: A
    ...
else:
    # x: B | C
    if x ?= B:
        # x: B
        ...
    else:
        # x: C
```

### Right-hand side is a union, not just a single type

```ouro
if x ?= ParseError | EmptyError:
    handle_error(x)              # x: ParseError | EmptyError
```

Useful for handling multiple errors uniformly.

### Narrowing ends at the if

After the `if` block exits, the narrowing is reverted (because the
narrowing was scoped to the `then`/`else` envs). The original type
is back in scope at the next statement.

## Why no `?` postfix or implicit propagation

Some languages have `expr?` (postfix `?`) that auto-propagates errors
up. Ouro deliberately does **not** in v1:

- It hides control flow.
- It requires every function to declare a "throws" type bound (or
  inference-machinery to compute one).
- It's redundant — the `if expr ?= Err: return expr` pattern is
  three explicit lines that say what's happening.

🔵 *Open.* `?` postfix may come back in v2 once it's clear the
manual pattern is too verbose in practice.

## Error vs. failure

There's no distinction between "expected error" and "panic." All
errors are **values**. Programs that hit unrecoverable conditions
(out-of-bounds, divide by zero) trap at runtime — they're not
catchable in user code.

🔵 *Open.* The trap mechanism (abort? signal? user-defined?) is
unspecified.

## Error type design

User-defined error types are **regular structs**:

```ouro
struct ParseError:
    msg: []u8
    pos: i32

struct EmptyError:
    pass

fn parse(s: []u8) -> i64 | ParseError | EmptyError:
    if s.len == 0:
        return EmptyError {}
    if not is_valid(s):
        return ParseError { msg: "invalid format", pos: 0 }
    return 42
```

You can have as many error variants as you need. The compiler tracks
them in the union and the type checker narrows correctly.

### When to use a struct vs. an enum-like

There are no enums separate from tagged unions. So an error
"category" is just a bunch of related struct types unioned together.

```ouro
# different errors carry different data
ParseError | EmptyError | NetworkError

# "enum-like" — empty structs as cases
DiskFull | OutOfMemory | DeviceBusy
```

For "category with no data per variant," use empty structs:

```ouro
struct DiskFull: pass
struct OutOfMemory: pass
struct DeviceBusy: pass

fn fail() -> DiskFull | OutOfMemory | DeviceBusy: ...
```

## `Null` — the special built-in

🟢 *Locked.*

`Null` is a **built-in** error type — produced when reading a
`weak[T]` whose referent has been dropped. See [memory.md](memory.md)
for `weak[T]` semantics.

```ouro
fn first_child(self) -> Node | Null:
    return self.first_child       # field type: weak[Node]; read produces Node | Null
```

`Null` is a singleton at the type level. There's exactly one
"null" — no distinguishing different kinds of null.

## `StopIteration` — the iterator sentinel

🟢 *Locked.*

`StopIteration` is the built-in error returned by `__next__` when
iteration is exhausted:

```ouro
struct RangeIter:
    pos: var[i64]; end: i64

    fn __next__(self) -> i64 | StopIteration:
        if self.pos >= self.end:
            return StopIteration
        v = self.pos
        self.pos = self.pos + 1
        return v
```

The type checker recognizes this convention and infers loop
variables accordingly:

```ouro
for x in r:           # x: i64 (the non-StopIteration variant)
    ...
```

## Patterns for error handling

### Early return on error

```ouro
fn compute(s: []u8) -> i64 | ParseError:
    n = parse_int(s)
    if n ?= ParseError:
        return n
    return n * 2
```

### Default value on error

```ouro
fn safe_parse(s: []u8) -> i64:
    n = parse_int(s)
    if n ?= ParseError:
        return 0
    return n
```

### Pattern matching with `match`

```ouro
match parse_int(s):
    n: i64:
        io.printf("%ld\n", n)
    e: ParseError:
        io.println(e.msg)
```

## What this is NOT

- **Not exceptions.** No `try`/`catch`. No stack unwinding.
- **Not multiple return values.** A function returns one value, which
  may be a union.
- **Not Result/Option monads** (in the Rust/Haskell sense). No
  `map`, `and_then`, etc. as built-in operators.
- **Not Java-style checked exceptions.** The error types are *types*,
  not annotations on the function.

## Cross-references

- [types.md](types.md) for tagged union type syntax
- [memory.md](memory.md) for `weak[T]` and `Null`
- [control-flow.md](control-flow.md) for `if`/`match` semantics
- [`src/typechecker.py`](../../src/typechecker.py) — `_subtract_ty`
  is the function that computes the complement type for else-branch
  narrowing
