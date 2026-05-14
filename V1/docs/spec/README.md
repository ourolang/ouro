# Ouro language specification

This folder is the **language reference** — what Ouro looks like and
what it means, written for someone learning it or looking something
up. (For *compiler internals* — how the lexer/parser/typechecker work
— see [`../`](../README.md).)

## Reading order

For a guided tour, read these in order:

1. **[Syntax](syntax.md)** — lexical rules, indentation, the grammar
2. **[Types](types.md)** — primitives, slices, structs, generics, unions
3. **[Memory model](memory.md)** — ARC, `var`/`const`/`weak`/`ptr`,
   slice borrowing
4. **[Control flow](control-flow.md)** — `if`/`match`/`for`/`loop`,
   expression-vs-statement
5. **[Declarations](declarations.md)** — `fn`, `struct`, `import`,
   methods, top-level
6. **[Errors](errors.md)** — tagged unions and `?=` narrowing
7. **[Conventions](conventions.md)** — naming, privacy, dunders,
   comments, struct construction
8. **[Runtime](runtime.md)** — what the C runtime exposes (`io.println`,
   etc.)
9. **[Standard library](stdlib.md)** — design draft for the `std/`
   namespace (modules, naming, error conventions)
10. **[Inline assembly](inline-asm.md)** — design draft for `asm fn`,
    the escape hatch that lets the runtime move from C to Ouro
11. **[Stack + `arc[T]`](stack-and-arc.md)** — design draft for the
    v2 memory model: structs on stack by default, opt-in heap via
    `arc[T]`, rename `weak[T]` → `warc[T]`
12. **[Duck-typing (`like[...]`)](duck-typing.md)** — naming
    duck-typed generic constraints; `Iter[T]` and the iterator
    protocol as a worked example. v1 lifts to implicit generics;
    call-site shape verification is still open.

## Status of the language

Ouro is **fully unlocked** until the self-host rewrite begins. Every
design decision in this spec is provisional — including the load-
bearing pillars (memory model, mutability, generics, error shape).
The bootstrap compiler implements one consistent reading at a time;
that reading changes as the language grows.

Locking happens **during the Ouro-in-Ouro rewrite**. Each chapter
of the self-hosted compiler will pick the design it relies on, and
*that* commitment is what locks the spec for v1. Until then, treat
"the way the bootstrap behaves" as the working definition, not as a
contract.

Where the spec and the bootstrap disagree, the bootstrap is what
runs — the spec is the working sketch.

## Current design pillars (unlocked)

These are the choices the bootstrap and stdlib are built around
today. They're internally consistent and unlikely to flip on a whim,
but none of them are locked until self-host.

| Topic | Current decision | Doc |
|---|---|---|
| Memory | ARC, no GC; RAII via `__drop__` at scope/refcount boundaries | [memory.md](memory.md) |
| Mutability | Default-const; `var[T]` wrapper for mutable bindings | [memory.md](memory.md) |
| Indirection | `ptr[T]` raw pointer, `weak[T]` synonym for `T \| Null`, `[]T` slices | [memory.md](memory.md) |
| Primitives | `i8…i64`, `u8…u64`, `isize`/`usize`, `f32`/`f64`, `bool`; defaults `isize` and `f64` | [types.md](types.md) |
| Strings | No magic type — `[]u8` byte slices | [types.md](types.md) |
| Modules | Zig-style `import("path")`; files are namespaces; no folder magic | [declarations.md](declarations.md) |
| Visibility | Leading-underscore = private; no `pub`/`priv` keywords | [conventions.md](conventions.md) |
| Errors | Tagged unions `T \| Error`, no exceptions; `?=` narrowing | [errors.md](errors.md) |
| Generics | Monomorphized at compile time, square-bracket syntax `List[T]`; pure duck-typed | [types.md](types.md) |
| Comments | `#` to end of line; no block comments | [conventions.md](conventions.md) |
| Construction | `Type { f: v }` for structs (Rust-style braces) | [conventions.md](conventions.md) |
| Privacy | `_name` strict struct-private; `__name__` reserved dunders | [conventions.md](conventions.md) |
| Control flow | `if`/`match`/`?=` are expressions; `__iter__`/`__next__` for `for` | [control-flow.md](control-flow.md) |
| Backend | QBE (text IR) | (see [`../codegen.md`](../codegen.md)) |

## Conventions used in this spec

- Code blocks tagged ` ```ouro ` are Ouro source.
- Code blocks tagged ` ```ssa ` are QBE IR.
- **EBNF-style grammar** uses lowercase non-terminals, single-quotes
  for literal terminals, `?` `*` `+` for repetition, `|` for
  alternation. Indentation/dedentation is shown as `INDENT` / `DEDENT`
  pseudo-tokens, since they're not characters.
- A leading `🟡 status:` line in some sections describes what's
  implemented vs. spec'd. Read these.

## Quick example

A program that exercises most of v1's working features:

```ouro
io = import("std/io")

fn fact(n: i64) -> i64:
    if n <= 1:
        return 1
    return n * fact(n - 1)

fn main() -> i32:
    n: var[i64] = 1
    loop:
        if n > 5:
            break
        io.printf("%ld\n", fact(n))
        n = n + 1
    return 0
```

Build and run:

```sh
make run FILE=examples/05_factorial.ou
```

Output:
```
1
2
6
24
120
exit=0
```

Read [syntax.md](syntax.md) next to understand the grammar that
makes this work.
