# Ouro

<p align="center">
  <img src="logo.png" alt="Ouro" width="400">
</p>

Ouro (`.ou`, "ouroboros") is a compiled systems programming language. The goal is Python-style indentation syntax with C/Rust-level performance, manual memory control, and type safety. The compiler targets [QBE](https://c9x.me/compile/) as its backend.

> **Status:** Bootstrap compiler in Python 3.14 produces working executables for a useful subset of the language: free functions, structs (incl. generic with monomorphization), generic free functions (subst inferred from arg types or explicit `id[i32](42)`), multi-file programs through a module loader (relative imports of user `.ou` files), refcount-managed allocation and `__drop__` destructors with `rc[T]` (non-atomic) and `arc[T]` (atomic-ready) wrappers, control flow, recursion, slices, primitive types, inline `asm` declarations, and minimal I/O (`println`/`print`/`printf`). The runtime is **libc-free on Linux x86_64** — `_start`, syscall wrappers, and `println`/`print` live in Ouro (`runtime/*.ou`); a small C layer still holds the mmap allocator, ARC primitives, and `printf`. Static binaries, no dynamic dependencies. Refcount handles copy-bindings, returns of borrowed values, managed struct fields, tagged-union returns + `?=` narrowing with payload extraction, block-scope releases (incl. break/continue), and `weak[T]` references with the standard weak-pointer semantics. Stack-by-default struct allocation (v2; locked design, not yet implemented), slice owner-handle, and `std/` prefix resolution are still deferred. The language spec is still under active design.

## Project layout

```
.
├── bootstrap/    # Python compiler (compiles every .ou today)
│   ├── src/         — the implementation
│   ├── test/python/ — unit + integration tests
│   ├── pyproject.toml
│   └── Makefile     — primary build targets
├── src/          # the self-hosted compiler in Ouro (planned)
├── std/          — Ouro stdlib (io, math, string, vec, map, ...)
├── runtime/      — runtime glue (Linux syscalls + libc-free C runtime)
├── test/         — language-level test programs (compiler-agnostic)
├── examples/     — example programs
├── docs/spec/    — language spec
└── editor/       — editor support (VS Code)
```

The top-level `Makefile` forwards every target to `bootstrap/Makefile`,
so commands work from either the repo root or from `bootstrap/`.

## Quickstart

Requirements: Python 3.14, [`uv`](https://docs.astral.sh/uv/), [`qbe`](https://c9x.me/compile/), `cc`.

```sh
make help                                  # list every target with usage
make sync                                  # install dev deps
make run FILE=examples/02_hello_world.ou   # build + run -> hello, ouro!
make run FILE=examples/05_factorial.ou     # build + run -> 1! = 1 ... 7! = 5040
make ir  FILE=examples/05_factorial.ou     # print QBE IR to stdout
make check                                 # ruff + ty + pytest
make test-ouro                             # run only the .ou test programs
make test-py                               # run only the Python tests
```

Browse [`examples/`](examples/) for a guided tour of the language —
nine small files that build up from a `return 0` to ARC + `__drop__`.

Built executables and intermediate `.ssa`/`.s` files land in `bootstrap/out/`. The
runtime that defines `io.println` etc. lives in [runtime/runtime.c](runtime/runtime.c)
and is auto-linked.

For VS Code syntax highlighting see [`editor/vscode/`](editor/vscode/).

## Design intent

Direction-setting choices. Some details are still under design — see [Open design questions](#open-design-questions).

- **Memory:** Stack-by-default (v2; in progress) with explicit opt-in to refcounted heap via `rc[T]` (non-atomic, single-thread) or `arc[T]` (atomic, thread-safe).  No runtime garbage collector. RAII via `__drop__` fires at scope/refcount boundaries.
- **Mutability:** Default-const everywhere. Opt in to mutability via the `var[T]` type wrapper.
- **Pointers, slices, weak refs:** `ptr[T]` for raw memory access; `[]T` is a slice that holds a strong reference to its source (sound by default); `weak[T]` for non-owning back-references to refcounted values.
- **Strings:** No magic string type. Strings are `[]u8` byte slices.
- **Modules:** File-as-struct, à la Zig. `import("std/io")` returns a struct.
- **Visibility:** Leading-underscore `_name` is private. No `pub` / `priv` keywords.
- **Errors:** Tagged-union error unions `T | Error`. No exceptions.
- **Generics:** Square-bracket `List[T]`. Monomorphized at compile time (zero-cost).
- **Control flow:** `if`, `else`, `match`, and `?=` are all expressions.
- **Polymorphism:** Dunder methods (`__str__`, `__iter__`, `__drop__`, ...) for duck-typing.

## Locked syntax: mutability

`var` and `const` are reserved keywords, but they **only ever appear as type wrappers** (`var[T]`, `const[T]`). They are never used as bare binding modifiers. The wrapper always sits in a type slot. The inference placeholder `?` may only appear inside the wrapper brackets — never bare — so it cannot collide with the `?=` pattern-match operator.

### Local declarations

```python
foo = 42                    # const, inferred type
foo: u8 = 42                # const, explicit type
foo: var[u8] = 42           # mutable, explicit type
foo: var[?] = 42            # mutable, inferred type
```

A type annotation is only required when you want mutability or want to assert a specific type. Otherwise, omit it.

### For-loops

The for-loop uses the same type-slot rule as declarations — there is no wrapper-on-name shorthand.

```python
for item in items:                  # const, inferred element type
for item: u8 in items:              # const, explicit element type
for item: var[?] in items:          # mutable, inferred element type
for item: var[u8] in items:         # mutable, explicit element type
```

### Function parameters

```python
fn handle(x: T):                    # const param
fn handle(x: var[T]):               # mutable param (callee can mutate the referent)
```

### Struct fields

Fields use the wrapper form only — there is no keyword form for fields.

```python
struct Server:
    port: i32                       # const field (default)
    active: var[bool]               # mutable field
```

### Composition with reference types

Because the wrapper applies wherever a type is expected, it composes naturally:

```python
x: ptr[T]                           # pointer to const T
x: ptr[var[T]]                      # pointer through which T is mutable
data: []u8                          # slice of const u8
data: []var[u8]                     # slice of mutable u8
```

The wrapper controls the **storage's** mutability. Since there is no bare-keyword binding form, binding mutability is not separately expressible — the storage is the binding.

## Locked syntax: error handling

Errors are values, expressed as tagged-union types: `T | Error`. There are no exceptions.

The `?=` operator is a **type-narrowing test**. `lhs ?= Type` returns a boolean and, when true, narrows `lhs` to `Type` inside the `if` body. On the fall-through path, `lhs` is narrowed to the remaining variants. No new binding is introduced — the variable you test is the variable you use afterward, just with a refined type.

```python
fn read_config() -> Config | Error:
    raw = read_file("config.toml")    # raw: []u8 | Error
    if raw ?= Error:
        return raw                     # raw narrowed to Error here
    return parse(raw)                  # raw narrowed to []u8 here
```

The right-hand side of `?=` accepts a single type or a union of types, so one test can match multiple variants:

```python
result = parse()                                  # []u8 | NotFoundError | NetworkError

if result ?= NotFoundError | NetworkError:
    log.error("parse failed")
    return result                                  # narrowed to NotFoundError | NetworkError

use(result)                                        # narrowed to []u8
```

For multi-variant unions, chained `?=` tests progressively narrow the source variable:

```python
sock = open_socket("api.example.com")
# sock: Socket | NotFoundError | TimeoutError | NetworkError

if sock ?= NotFoundError: return create_default()
if sock ?= TimeoutError:  return retry()
if sock ?= NetworkError:
    log.error("network down")
    return sock

use(sock)                                          # narrowed to Socket
```

For exhaustive discrimination, use `match`. Arms that don't reference the bound value use `_` instead of a name:

```python
match open_socket("api.example.com"):
    _: NotFoundError: return create_default()
    _: TimeoutError:  return retry()
    s: Socket:        use(s)
```

## Locked syntax: visibility and naming

Visibility is encoded in identifier names. There are no `pub` / `priv` keywords.

| Pattern | Meaning |
|---|---|
| `name` | public |
| `_name` | private |
| `__name__` | reserved dunder (language-defined behavior) |
| `__name` (no trailing `__`) | disallowed — compile error |

**Privacy is strict struct-private:** a struct member like `_port` is accessible only to the struct's own methods, not to other code in the same file. A top-level `_helper` in a file is file-private and not importable. A filename `_internal.ou` is package-private.

Dunders are exempt from the privacy rule because the compiler/runtime needs to invoke them across boundaries. The initial dunder set:

- `__drop__` — destructor; runs at scope end / refcount = 0
- `__str__` — returns `[]u8`; invoked for string conversion
- `__iter__` — invoked at the start of a `for` loop
- `__next__` — returns `T | StopIteration`; invoked repeatedly during iteration
- `__zeroed__` — static method returning `Self` with all-default values; used by the empty struct literal `Type {}`. Auto-generated by the compiler when every field has an implicit default; user can override

### Unused bindings

If you bind a name and don't reference it, it's a compile error. To match or destructure without binding, use `_` — a placeholder that doesn't bind and can't be referenced.

```python
match value:
    _: SomeType:       handle_without_bind()    # type-only match
    x: OtherType:      use(x)                   # x must be referenced

(_, second) = pair                              # discard first element

for _ in 0..10:                                 # ignore the loop variable
    do_something()
```

In **local, parameter, and match-binding scopes** (where there is no visibility scope to grant), `_name` opts out of the unused-binding error. Use it for documented-but-unused parameters:

```python
fn handler(req: Request, _ctx: Context) -> Response:
    return Response { ... }                     # _ctx documents the param without forcing use
```

This does not conflict with the privacy rule — privacy only applies at top-level and struct-member scope, where a leading underscore means private.

## Locked syntax: struct construction

Three complementary forms:

```python
# 1. Full struct-literal — every field specified. Direct field assignment, no logic.
#    Setting private fields via the literal is only legal inside the struct's own methods.
server = Server[Client] {
    _port: 8080,
    active: true,
    clients: [],
}

# 2. Empty struct-literal — all fields take their default value via __zeroed__.
#    Works whenever the struct has a __zeroed__ method (auto-generated when every
#    field has an implicit default; user can override for custom defaults).
list = LinkedList[i32] {}

# 3. User-defined static method — for construction with logic, validation, or allocation.
#    `.new` is a conventional name; not magic.
server = Server[Client].new(8080)
```

**Implicit defaults** (drive auto-generation of `__zeroed__`):

| Type | Default |
|---|---|
| Integer (`i*`, `u*`, `isize`, `usize`) | `0` |
| Float (`f32`, `f64`) | `0.0` |
| `bool` | `false` |
| `weak[T]`, any union containing `Null` | `Null {}` |
| `ptr[T]`, `[]T`, other structs without `__zeroed__`, unions without `Null` | **no implicit default** |

Partial struct literals (`Type { x: 5, ... }`) are not supported in v1. Use `Type {}` for full default or `Type { ...all fields... }` for fully explicit construction.

Calling a type directly (`Server[Client](...)`) is reserved for future use and not currently legal.

## Locked syntax: methods and conventions

### Method receivers

When a method's first parameter is named `self` and has no annotation, it defaults to `self: ptr[Self]` (immutable receiver, by-pointer). Annotate explicitly for non-default receivers:

```python
fn length(self) -> usize:                       # implicit self: ptr[Self]
fn push_back(self: ptr[var[Self]], v: T):       # explicit mutable receiver
```

### `Self` keyword

Inside a struct definition, `Self` is a reserved alias for the enclosing struct type, including any generic parameters. Avoids repetitive type names in method signatures and bodies.

```python
struct LinkedList[T]:
    fn new() -> Self:               # Self = LinkedList[T]
        return Self {}
```

### Static vs. instance methods

A method **without** `self` as the first parameter is a static method, called as `Type.method(...)`. A method **with** `self` first is an instance method, called as `instance.method(...)`.

### `pass` for empty bodies

```python
struct EmptyError:
    pass

fn noop():
    pass
```

### Statement separators

A `:` block accepts either an inline single statement or a newline-and-indented block. `;` is an optional inline separator for compactly placing multiple statements on one line. The indented block remains the standard form.

```python
if x ?= Error: return x                          # inline single statement

io.print("ok: "); io.print_usize(n); io.println("")    # ; as inline separator (sparingly)
```

### Iteration protocol

`for x in obj:` desugars to:

```python
_iter = obj.__iter__()
loop:
    _next = _iter.__next__()
    if _next ?= StopIteration: break
    x = _next
    # body
```

`__iter__` returns any type that defines `__next__`. `__next__` returns `T | StopIteration` for some element type `T`. `StopIteration` is a built-in zero-field struct.

### Struct value semantics

Structs are ARC-managed reference types. Multiple strong references to the same struct value see the same mutations. Primitives (`i32`, `u8`, `bool`, ...) are value types — copies are independent.

```python
a = Point { x: 1, y: 2 }
b = a                            # a and b are two refs to the same struct
b.x = 99
io.print_i32(a.x)                # prints 99 — mutation is shared
```

The compiler may stack-allocate non-escaping structs as a transparent optimization, but the language semantics are reference.

## Locked syntax: comments

```python
# Line comment, Python style. Everything from # to end of line.
```

No separate docstring construct.

## Locked syntax: primitive types and literals

**Types:**

- Signed integers: `i8`, `i16`, `i32`, `i64`
- Unsigned integers: `u8`, `u16`, `u32`, `u64`
- Pointer-sized: `isize`, `usize`
- Floats: `f32`, `f64`
- Boolean: `bool`, with literals `true` and `false` (reserved keywords)

There is no separate `char` type — byte literals are `u8`, and strings are `[]u8`.

**Numeric model.** Numeric literals are Zig-style comptime values: they coerce to whatever type the context expects, with the value bounds-checked at compile time. A suffix forces a type when no context can pin it down.

```python
x: i64 = 42        # 42 coerces to i64
y: u8  = 200       # 200 fits in u8
z: u8  = 1000      # compile error: 1000 doesn't fit in u8
w = 42             # no context — defaults to isize
v = 42i64          # suffix forces i64
```

When no context exists, integer literals default to `isize` and float literals default to `f64`.

**Literal forms:**

```python
42                 # decimal integer
1_000_000          # underscore digit separators
0xFF_FF            # hex
0b1010_1010        # binary
0o52               # octal
3.14
1.5e10             # scientific
'A'                # byte literal (u8)
"hello"            # string ([]u8)
"""multi
line"""            # triple-quoted multi-line string ([]u8)
```

**Suffixes** attach immediately, with no separator:

```python
42i64
3.14f32
0xFFu8
```

**Escape sequences:**

- In byte literals and strings: `\n`, `\r`, `\t`, `\0`, `\\`, `\'`, `\"`, `\xNN`
- In strings only: `\u{1F600}` — the codepoint is encoded as its UTF-8 bytes
- Not legal in byte literals (a codepoint may span multiple bytes)

Triple-quoted strings preserve content verbatim, including leading whitespace and newlines. No automatic indentation stripping.

**Arbitrary-width integers** (`u1`, `u7`, `i3`, `u23`, ... à la Zig) are part of the language design but deferred to a later release. The type system represents integer bit-width parametrically from the start, so when codegen lands, no type-system changes are needed. Until then, only the standard widths (`i8/i16/i32/i64`, `u8/u16/u32/u64`) compile; non-standard widths parse but produce a *"deferred feature"* error from the type checker.

## Locked semantics: generics

Generics use square-bracket syntax (`List[T]`, `fn foo[T](...)`) and are monomorphized at compile time — zero-cost specialization per instantiation.

In v1, generics are **pure duck-typing**. The type parameter `T` has no declared constraints; the function body uses `T` however it wants. Instantiation fails if the concrete type doesn't support what the body needs:

```python
fn sort[T](items: []var[T]):
    # body uses item.__lt__(other)
    ...

sort([3, 1, 2])           # OK — i32 implements __lt__
sort([Foo {}, Foo {}])    # error: Foo does not implement __lt__ (required at sort.ou:14)
```

Constraint syntax (e.g. `[T: __lt__]` for dunder-based bounds, or full interface declarations) is deferred to later releases. v1 errors are reported at the call site that triggered the bad instantiation.

## Locked semantics: weak references and cycle handling

ARC alone leaks reference cycles. To break them, Ouro provides `weak[T]` — a non-owning reference that does not bump the target's strong refcount.

```python
struct Parent:
    children: []var[Child]      # strong refs (parent owns children)

struct Child:
    _parent: weak[Parent]       # weak back-reference, breaks the cycle
```

**Type-system view.** `weak[T]` is treated by the type checker as a synonym for `T | Null`. The `?=` narrowing machinery already locked for error handling handles weak upgrades — no new pattern syntax.

**Reading a weak reference performs an atomic upgrade attempt** at the storage boundary:

```python
parent = child._parent           # parent: Parent | Null; atomic upgrade happens here
if parent ?= Null:
    return                        # the target was freed; nothing to do
use(parent)                       # narrowed to Parent
```

If the target's strong count is greater than zero at the moment of read, the upgrade increments the count and returns `Parent`. Otherwise it returns `Null`. Each read is an independent upgrade attempt — between two reads of the same weak field, the target may have been freed.

**Writing a strong into a weak storage is an implicit conversion.** No constructor function is required:

```python
strong_parent: Parent = ...
child._parent = strong_parent    # converts strong → weak at the storage boundary
```

**Runtime model.** ARC objects carry a control block with both `strong_count` and `weak_count`. The payload is destructed and freed when `strong_count → 0`; the control block persists until `weak_count → 0`. Each weak-read is a single atomic compare-and-increment on the strong count.

`Null` is a built-in zero-field type representing "the weak target was freed".

## Locked semantics: slices

A slice `[]T` is a **refcount-aware view** into its source. It holds a strong reference and is sound by default.

```python
fn make_slice() -> []u8:
    data = some_heap_alloc()    # refcount = 1
    return data[0..5]           # slice now holds a strong ref to data; refcount = 2
# data goes out of scope; refcount drops to 1
# Returned slice still keeps data alive — no dangling.
```

Runtime representation: `{ ptr, len, owner_handle }`. Slicing a slice carries the same owner handle forward — chains don't add extra refcount churn. Slices into static or stack memory carry a null owner handle and incur no inc/dec.

There is no explicit unowned-view type in v1. If real performance pressure surfaces later, an escape hatch can be added.

## Locked syntax: modules and imports

The import model is **Zig-inspired**, with one simplification: files are namespaces, not instantiable types.

`import("path")` is a comptime built-in. Its argument must be a string literal. The result is a namespace containing the imported file's public top-level declarations.

```python
io = import("std/io")
io.print("hello")
```

### Path resolution

- `import("./util")` — sibling file `util.ou`
- `import("../shared/log")` — relative path
- `import("std")` / `import("std/io")` — bundled standard library (`std` is a reserved prefix)
- `import("dep_name")` — external package, configured via a project manifest (deferred to package-manager phase)

There is **no folder-as-module magic**. To organize a multi-file unit, create an entry-point file that aggregates its submodules explicitly:

```python
# std/io.ou — entry point for the io module
print = import("./io/print")
read  = import("./io/read")
```

Then `import("std/io")` exposes `io.print` and `io.read` as sub-namespaces.

### Files as namespaces

Each `.ou` file is a namespace. Its public top-level declarations are members.

```python
# counter.ou
_value: var[i32] = 0           # file-private state, singleton, init'd on first import

fn increment():
    _value = _value + 1

fn get() -> i32:
    return _value
```

`import("./counter").increment()` works; `_value` is invisible to importers.

A file is **not** an instantiable type. To define an instantiable type, declare a `struct Foo: ...` inside the file and access it as `module.Foo`.

### Other rules

- **Module-level state:** top-level `var[T]` is a singleton, initialized once on first import.
- **Selective imports:** none. Use whole-module binding + dot-access. For short names, alias: `print = io.print`.
- **Circular imports:** compile error. Factor shared code into a third module.

## Deferred (planned, not yet in v1)

- **Arbitrary-width integers** (`u1`, `u7`, `i3`, ...) — type system already represents bit-width parametrically; codegen and full lexer support deferred.
- **Generic constraints** — `[T: __lt__]` dunder-based bounds (likely v2); full interface/trait system (v3+).
- **`?` postfix propagation operator** — revisit if propagation chains become verbose.
- **Unowned slice views** — explicit zero-cost view type, only if real performance pressure surfaces.

## Roadmap

1. ~~Lexer and token stream.~~ ✓
2. ~~AST node definitions.~~ ✓
3. ~~Parser.~~ ✓
4. ~~Name resolver.~~ ✓
5. ~~Type checker (mutability + assignability).~~ ✓
6. ~~QBE IR emitter (v1: non-generic).~~ ✓
7. ~~End-to-end build pipeline (Ouro → QBE → executable).~~ ✓
8. ~~ARC `arc_alloc` / `arc_release` at function exit;~~ ~~`__drop__` invocation~~ ✓
9. ~~Call-site arg type checking (arity + per-arg assignability)~~ ✓
10. ~~Diagnostics with source snippets (caret-pointing format across all error types)~~ ✓
11. ~~`?=` codegen: tag comparison + boxed-return ABI (`arc_alloc(16, 0)` boxes with `{tag, payload}`)~~ ✓
12. ~~Payload extraction for narrowed values (`e.msg` after `?= ParseError`)~~ ✓
13. ~~Per-union `drop_fn` (release boxed struct payload when box hits zero)~~ ✓
14. ~~Slice `.ptr` field accessor~~ ✓
15. ~~ARC `inc` on copy-bindings (`y = x`, `y = obj.field`)~~ ✓
16. ~~ARC `inc` on returning borrowed values (param, field) — both regular and union-boxed return paths~~ ✓
17. ~~Managed struct fields: inc on store, auto-release on drop via per-struct drop wrappers~~ ✓
18. ~~Block-scope releases (per-block scope stack; break/continue release down to loop body)~~ ✓
19. ~~`weak[T]` codegen: 24-byte header (weak/strong/drop_fn), weak_inc/release/upgrade, weak-read boxes as `T | Null`~~ ✓
20. ~~Generic-struct monomorphization (TypeMap-driven specialization, name-encoded symbols like `Box_i32`)~~ ✓
21. ~~Generic free-function monomorphization (subst inferred per call site or from explicit type args; worklist emits each spec under active substitution)~~ ✓
22. ~~Module loader: recursive load of `import("./helper")` / `import("../X/Y")` with cache + cycle detection; codegen prefixes each module's symbols (`$helper__foo`, `:helper__Point`). `std/` prefix still uses the C-runtime stub path until a bundled root lands.~~ ✓
23. ~~Libc-free runtime: syscall wrappers (write, mmap, exit) + `mmap`-backed allocator with first-fit free list + custom `_start` + minimal printf (`%ld %lu %d %u %s %c %x %%`). Static binaries, no dynamic linker. Linux x86_64; other targets via sibling files.~~ ✓
24. ~~Inline `asm name(...) -> ret:` declarations end-to-end (lexer raw-line capture, parser, codegen sidecar `.s`, SystemV calling convention).~~ ✓
25. ~~Runtime migration to Ouro: `_start`, syscall wrappers, `println`/`print` live in `runtime/*.ou`; only allocator + ARC + printf remain in C.~~ ✓
26. ~~Add `rc[T]` (non-atomic) and `arc[T]` (atomic-ready) wrappers as language surface; v1 both compile identically (no threading yet).~~ ✓
27. ~~Stack-by-default, first slices: bare struct bindings use `alloca` + direct `__drop__` at scope exit; bare struct returns use QBE's aggregate-return convention (`function :Foo`). `rc[T]` / `arc[T]` annotated bindings/returns keep the refcounted-heap path. Test-suite redistributed: `stack_*.ou` for value-only, `rc_*.ou` for shared single-thread, `arc[T]` reserved for thread-shared intent.~~ ✓
28. ~~Stack-by-default, next slice: pass-by-value for bare struct args via QBE's `:Foo` aggregate-by-value param ABI. Callee gets its own copy; callee + caller each drop independently. Generic-fn substitution now preserves rc/arc wrappers so refcounted args don't get specialized to their unwrapped struct type.~~ ✓
29. ~~Stack-by-default, fourth slice: inline composition for nested bare-struct fields. The outer struct embeds the inner's bytes (no pointer); struct literals init via QBE `blit`; field reads return the field address (the field IS the struct); the auto drop wrapper chains directly into the inner's `__drop__` instead of `arc_release`.~~ ✓
30. ~~Stack-by-default, fifth slice: `var[Struct]` slots hold the struct's bytes inline. Reassignment runs the slot's `__drop__`, then re-initialises in place — struct literals via `_emit_struct_lit(into=slot)`, other expressions via `blit`. No heap allocation, no pointer swap.~~ ✓
31. ~~Lock the `ptr[T] = expr` construction: typechecker accepts an addressable RHS (Name / FieldAccess) and rejects literals or call results; codegen reuses the existing wrapper-stripping path so the LHS just inherits the RHS's slot/heap address. Works through `var[T]` bindings (mutation), `rc[T]` user pointers, and stack-bare structs.~~ ✓
32. **Next:** Decide remaining sharp edges — slice-into-stack, `move` semantics, escape analysis (all currently "trust the user").
33. ~~Standard library, first slice: `std/` directory resolves via the loader to `<repo>/std/X.ou` (with a fallback to the legacy stub path for any name not yet migrated). `std/io` moved out of `runtime/` into `std/`; new `std/math` (abs/min/max/clamp) demonstrates the path for fresh modules.~~ ✓
34. ~~Type aliases: `Result: type = i64 | ParseError`, generic `Option[T]: type = T | Null`. Transparent — every use site expands to the body before the typechecker / codegen see it; no new nominal type. New `type` keyword; new `TypeAlias` AST node; resolver `TYPE_ALIAS` symbol kind; typechecker + codegen both substitute the body with the args.~~ ✓
35. ~~Stdlib expansion: `std/string` (eq, starts_with, ends_with, index_of, contains over `[]u8`), `std/assert` (assert + panic via sys_exit), `std/io` extended with eprintln/eprint. Parser-level fix: `a[i]` over a slice value was incorrectly parsed as `GenericInstantiation`; typechecker + codegen now re-route to slice-index when the base is `SliceTy` and the single bracketed arg is a name bound in scope.~~ ✓
36. ~~`extern name(params, ...) -> ret` declarations for C-linked symbols. Trailing `...` marks the fn variadic; call sites emit the QBE `...` separator between fixed and var args (f32 → f64 promotion per the C variadic ABI). New `extern` keyword + `ELLIPSIS` token; `_base(ptr[T])` now returns `l` instead of stripping to the inner type's width.~~ ✓
37. ~~`for i in start..end:` — half-open integer-range for-loops. The codegen lowers to a numeric counter (no iterator object, no slice); the typechecker types the loop variable from the range bounds. std/string switched its index-style scans to this form.~~ ✓
38. **Next:** `std/parse` (parse_int / parse_uint with `ParseError` union). Then move on to the first thing that needs heap arrays.
39. Slice owner-handle / refcount bump — pairs with heap-array support, which v1 doesn't have (today's slices only come from string literals → data-section pointers).
40. Self-hosting (long-term).

## Editor support

A VS Code extension with syntax highlighting lives in
[`editor/vscode/`](editor/vscode/). See its
[README](editor/vscode/README.md) for install instructions.
