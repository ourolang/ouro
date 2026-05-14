# Standard library

Ouro ships a small standard library under the reserved `std/` import
prefix. The intent is **a thin layer over the language**, not a
batteries-included toolbox: enough to write real programs without
re-inventing the basics, no more.

🟡 *In progress.* The bundled-stdlib path is wired up: the loader
resolves `import("std/X")` to `<repo>/std/X.ou` and treats the
result as a normal cross-module import (prefixed symbols and all).
What's been built: `std/io` (migrated out of `runtime/`, now with
`eprintln` / `eprint`), `std/math` (abs/min/max/clamp on i32+i64),
`std/string` (`eq` / `starts_with` / `ends_with` / `index_of` /
`contains`), and `std/assert` (`assert` / `panic`).  Most of
what's below is still design draft — read it as direction, not as
shipped.

## Design pillars

1. **Pure Ouro source.** The stdlib is `.ou` files under `std/` at
   the repository root. The same code runs in the bootstrap compiler
   today and in a self-hosted compiler later. C glue exists only for
   syscalls and primitives the language genuinely cannot express
   (allocator, libc printf, fileno-level I/O).

2. **Small and opinionated.** One way to do each thing. No alternate
   APIs for the same job. If two equally good designs exist we ship
   neither until we have a reason to pick.

3. **No magic.** Every stdlib symbol is reachable by reading source.
   No compiler-injected names, no hidden prelude — `io.println` works
   because the file `std/io.ou` exports `fn println`.

4. **Borrows from the language, doesn't extend it.** If a stdlib
   module wants a feature (e.g. higher-order fns for `iter.map`),
   that feature lands in the language first, not as a stdlib-only
   shortcut.

## Resolution

🔵 *Open — needs compiler work.*

```ouro
io = import("std/io")
```

The `"std/"` prefix is reserved. The compiler resolves it to a
bundled directory shipped with the toolchain.

**Bundling.** The bootstrap (Python) compiler reads the `std/` folder
from disk — same layout as the repo. The self-hosted compiler will
**embed the stdlib into its own binary** so the toolchain ships as a
single executable. There is no runtime "stdlib path" environment
variable; what you compile against is the version baked into the
compiler you're running.

Lookup is:

```
import("std/X")     →   <stdlib-root>/X.ou
import("std/X/Y")   →   <stdlib-root>/X/Y.ou
```

User code uses **relative-path imports** for everything outside
`std/`:

```ouro
util = import("./util")           # ./util.ou
parse = import("../parse/json")   # ../parse/json.ou
```

🟢 *Status:* **Relative imports and the `std/` prefix both load
files.** `import("./util")` and `import("../parse/json")` trigger
the [loader](../../src/loader.py); `import("std/X")` resolves to
`<repo>/std/X.ou` and goes through the same lex/parse/resolve/
typecheck pipeline.  Missing `std/X` files still fall through to
the legacy C-runtime stub path so historic callers (`io.printf`
etc.) keep working until every name is migrated.

Implemented today:

1. File-lookup for relative paths and the `std/` prefix.
2. Recursive lex/parse/resolve/typecheck of imported modules.
3. Path-keyed cache (a module imported twice compiles once).
4. Cycle detection (locked: cycles are forbidden).
5. Qualified symbol names — `$io__println` for `std/io`,
   `$math__abs_i32` for `std/math`, bare `$foo` for the entry
   module — keeping imported symbols from colliding with the entry's.
6. Bootstrap-compiler bundling: the loader looks under
   `<repo>/std/`, located via `Path(__file__).parent.parent` from
   `src/loader.py`.

Still **D** (deferred): self-hosted compiler embeds the stdlib in
its binary; cross-module struct sharing; cross-module generic
free-function calls.

## Naming and style

Stdlib code follows the project's [conventions](conventions.md):

| Kind | Style | Example |
|---|---|---|
| Functions | `snake_case` | `parse_int`, `read_line` |
| Types | `PascalCase` | `ParseError`, `File`, `Writer` |
| Constants | `SCREAMING_SNAKE` | `STDIN_FILENO`, `MAX_PATH` |
| Module-private | leading `_` | `_realloc`, `_BUF_SIZE` |
| Dunders | `__name__` | `__drop__`, `__iter__` |

Error types end in `Error` (`ParseError`, `IoError`). The two
language-level sentinels — `Null`, `StopIteration` — keep
their existing names.

Every fallible function returns `T | SomeError`. Infallible
functions return `T`. Functions never panic on bad input that the
caller could have prevented; they return an error union instead.
`assert` and friends are the only stdlib functions allowed to abort
the process.

## Error conventions

- **One error type per module** for everything that module can fail
  with, unless the module covers genuinely different failure modes.
  `std/io` exports `IoError`; `std/parse` exports `ParseError`.
- Errors carry a slice message (`msg: []u8`) plus whatever
  structured fields make sense (`pos: usize` for parse errors,
  `errno: i32` for io errors).
- Callers narrow with `?=`:
  ```ouro
  x = parse.parse_int(buf)
  if x ?= ParseError:
      io.eprintln(x.msg)
      return 1
  # x is i64 here
  ```

## v1 modules (target)

Modules grouped by how much compiler work they unblock. **D** =
deferred (waiting on a compiler feature).

### Ready to write today

These need only what the compiler already has — primitives, structs,
slices over data-section literals, ARC, generics, tagged unions.

#### `std/io`
- `fn println(s: []u8)` — already exists in C runtime
- `fn print(s: []u8)` — already exists in C runtime
- `fn printf(fmt: []u8, ...) -> i32` — variadic intrinsic (already)
- `fn eprintln(s: []u8)` — like println but stderr
- `fn eprint(s: []u8)` — like print but stderr

Reading is **D** — needs heap arrays to return owned slices.

#### `std/math`
- `fn abs[T](x: T) -> T`, `fn min[T](a: T, b: T) -> T`, `fn max[T](a: T, b: T) -> T`
- `fn sqrt(x: f64) -> f64`, `fn pow(b: f64, e: f64) -> f64`
- `fn sin(x: f64) -> f64`, `fn cos(x: f64) -> f64`, `fn tan(x: f64) -> f64`
- Constants: `PI: const[f64]`, `E: const[f64]`

All call out to libm via extern decls. The generic `min`/`max`/`abs`
specialize per numeric type. Because v1 generics are duck-typed, the
caller's type must support the ops the body uses — `<` for
`min`/`max`, `<` and unary `-` for `abs` (so unsigned integers won't
fit; trying to instantiate `abs[u32]` is a body-check failure once
those constraints are tightened). v2's constraint syntax will make
this explicit; for now the docstring is the contract.

#### `std/assert`
- `fn assert(cond: bool, msg: []u8)` — abort with msg if false
- `fn panic(msg: []u8) -> never` — unconditional abort
- `fn assert_eq[T](a: T, b: T)` — needs equality, which is
  duck-typed in v1 (works for primitives; structs need user equality)

#### `std/string`
String here means `[]u8`. No new type.

- `fn eq(a: []u8, b: []u8) -> bool`
- `fn starts_with(s: []u8, prefix: []u8) -> bool`
- `fn ends_with(s: []u8, suffix: []u8) -> bool`
- `fn index_of(s: []u8, needle: []u8) -> i64` — `-1` if not found
- `fn contains(s: []u8, needle: []u8) -> bool`

Mutating operations (`split`, `replace`, `to_upper`) are **D** —
they all need to return owned heap arrays.

#### `std/parse`
- `fn parse_int(s: []u8) -> i64 | ParseError`
- `fn parse_uint(s: []u8) -> u64 | ParseError`
- `fn parse_float(s: []u8) -> f64 | ParseError`

```ouro
struct ParseError:
    msg: []u8
    pos: usize
```

#### `std/mem`
Raw memory ops, mostly extern wrappers over libc.

- `fn copy(dst: ptr[u8], src: ptr[u8], n: usize)`
- `fn set(dst: ptr[u8], value: u8, n: usize)`
- `fn compare(a: ptr[u8], b: ptr[u8], n: usize) -> i32`

### Deferred to v2+

Each waits on a specific compiler feature.

| Module | Blocked on |
|---|---|
| `std/collections` (`Vec[T]`, `Map[K, V]`) | Heap arrays + (for `Map`) trait/constraint syntax |
| `std/iter` (`map`/`filter`/`fold`) | First-class function types |
| `std/fmt` (general formatter) | Varargs or builder + first-class fns |
| `std/sort` | First-class fns (comparators) |
| `std/fs` (read_to_end, write_all) | Heap arrays |
| `std/hash` | Constraint syntax (or interim hand-rolled `Hash` struct field) |
| `std/time` | OS clock syscalls (could be done now, but no compelling user yet) |
| `std/env` / `std/os` | Argv/env access through runtime (mostly easy, low priority) |

## Module structure

Each `.ou` stdlib file follows this layout, top-to-bottom:

```ouro
# Module docstring (a #-comment block) — what this module is for.

# Imports first, all at the top.
io = import("std/io")
mem = import("std/mem")

# Public types — exposed errors and structs.
struct ParseError:
    msg: []u8
    pos: usize

# Public functions in roughly user-facing order.
fn parse_int(s: []u8) -> i64 | ParseError:
    ...

fn parse_uint(s: []u8) -> u64 | ParseError:
    ...

# Module-private helpers last, with `_` prefix.
fn _is_digit(b: u8) -> bool:
    return b >= 48 and b <= 57
```

No `__all__` / `pub` markers — the `_` prefix is the entire privacy
mechanism, same as inside structs.

## Migration of `std/io`

🟢 *Done.* `std/io.ou` was moved out of `runtime/` and into
`std/`, so `io = import("std/io")` now goes through the normal
cross-module path:

- `io.println(s)` lowers to `call $io__println(l %s)`.
- `std/io.ou` itself imports `../runtime/syscalls` and calls
  `$sys_write` (bare runtime symbol).
- The codegen no longer has any `io`-specific prefix-dropping
  logic; the legacy fall-through path is reserved for genuinely
  missing `std/X` files.

`io.printf` keeps its variadic-intrinsic special case until Ouro
gains varargs or a `Display` trait — but the intrinsic now fires
on any module's `printf` field, not just legacy stubs, so a
proper `std/io.printf` declaration is possible once the language
side supports it.

## Locked decisions

- **Bundling.** Bootstrap reads `std/` from disk; self-hosted
  embeds the stdlib into the compiler binary for single-artifact
  distribution.
- **Test layout.** Stdlib tests live in `test/std/`, parallel to
  `test/ouro/` (which stays language-feature tests). Same
  `.ou` + optional `.expected` convention.
- **No prelude.** Every import is explicit in v1. No auto-injected
  `io`/`assert`/anything. Revisit only if the boilerplate becomes
  measurably painful.
- **Generic stdlib fns rely on duck typing.** `math.abs[T]`,
  `min`/`max`, etc. work for the operations their bodies use; the
  docstring is the contract until v2 constraint syntax lands.

## Open questions

🔵 *Still not decided:*

- **Versioning.** When the language changes incompatibly, do stdlib
  files declare a minimum compiler version? For v1, no — the
  stdlib and compiler ship together. v2+ can revisit.

## Cross-references

- [declarations.md](declarations.md) — `import` syntax
- [conventions.md](conventions.md) — naming, privacy, dunders
- [errors.md](errors.md) — `?=` narrowing, tagged-union returns
- [runtime.md](runtime.md) — what the C runtime currently exposes
