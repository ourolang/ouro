# Ouro examples

A guided tour of the language, ordered by feature. Every program here
**compiles and runs** today — they're rebuilt on every CI run via the
`make test-ouro` harness (the same programs live in
[`test/ouro/`](../test/ouro/) as regression tests).

Run any of them:

```sh
make run FILE=examples/01_hello.ou
```

## Reading order

| #  | File | Demonstrates |
|---:|---|---|
| 1  | [`01_hello.ou`](01_hello.ou) | The minimum valid program — `fn main() -> i32: return 0` |
| 2  | [`02_hello_world.ou`](02_hello_world.ou) | `import("std/io")`, `io.println`, string literals are `[]u8` |
| 3  | [`03_arithmetic.ou`](03_arithmetic.ou) | `+ - * / %`, const bindings, `io.printf` formatting |
| 4  | [`04_control_flow.ou`](04_control_flow.ou) | `if / elif / else`, `match`, `while` loop, `var[T]` mutation |
| 5  | [`05_factorial.ou`](05_factorial.ou) | Recursion, i64 arithmetic |
| 6  | [`06_fizzbuzz.ou`](06_fizzbuzz.ou) | Classic combination: `while`, `if/elif`, modulo, mixed output |
| 7  | [`07_point.ou`](07_point.ou) | Struct with fields and a read-only method (bare `self`) |
| 8  | [`08_counter.ou`](08_counter.ou) | Mutable struct: `var[T]` field + `self: ptr[var[Self]]` |
| 9  | [`09_arc_drop.ou`](09_arc_drop.ou) | ARC + `__drop__`: deterministic cleanup at scope end |
| 10 | [`10_tour.ou`](10_tour.ou) | Everything together: generics, asm, tagged unions + `?=`, for-loops, drop |

Each file has its expected output documented in the comment header.

## What's NOT in these examples

The locked spec includes these features, but they're either deferred
or only partially exposed today:

- **Array literals** (`[1, 2, 3]`) and **heap arrays** — `[]T` slices
  exist and can be iterated, but only when they point at a string
  literal (data-section) today. No way to construct one from
  individual elements.
- **`std/` module loading** — `import("std/io")` resolves through the
  legacy C-runtime stub path; no `.ou` files under `std/` yet.  See
  [`docs/spec/stdlib.md`](../docs/spec/stdlib.md) for the design.
- **Slice owner-handle** — slices don't yet bump ARC; sharing a slice
  with the backing data dropped is unsafe in v1.
- **Stack-allocated structs** — bare struct bindings (`p = Point {...}`)
  now land on the stack with a direct `__drop__` at scope exit.
  `rc[T]` adds non-atomic refcount (shared, single-thread); `arc[T]`
  adds atomic refcount (thread-ready; identical runtime in v1).  See
  [`docs/spec/stack-and-arc.md`](../docs/spec/stack-and-arc.md) for
  the full design.  First-slice limitations: returning a bare struct
  by value, passing bare structs as args, and inline composition for
  nested struct fields all still use the heap path — these are
  follow-up slices.

See [`../docs/spec/`](../docs/spec/) for the full language reference.
