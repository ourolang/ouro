# Inline assembly: `asm`-declared functions

🔵 *Design draft. Not yet implemented.*

A way to write raw assembly functions in Ouro source, so the runtime
(syscall wrappers, `_start`, future intrinsics) can move out of C
and into `.ou` files.

## Motivation

The current runtime is libc-free but still C: `_start`, syscall
wrappers, allocator, ARC, and I/O all live in
[`runtime/runtime.c`](../../runtime/runtime.c). Two of those pieces
fundamentally need an escape hatch into raw assembly:

1. **`_start`** — naked entry symbol with kernel-supplied stack
   layout; can't have a compiler-inserted prologue.
2. **Syscall wrappers** — need the `syscall` instruction plus the
   right ABI register binding (`%rdi`, `%rsi`, `%rdx`, …) and
   clobber discipline (`%rcx`, `%r11`).

Everything else (allocator, ARC, I/O) is just pointer arithmetic
and primitive ops. With `asm` providing the escape hatch, those
parts can move to plain Ouro source — same surface user code uses,
no C in the toolchain.

## Syntax

An `asm` declaration replaces the Ouro body with a sequence of
assembly lines. The signature is normal Ouro; the body is x86_64
assembly text emitted verbatim.

```ouro
asm sys_write(fd: i32, buf: ptr[u8], len: usize) -> i64:
    mov     $1, %rax
    syscall
    ret
```

Indentation rules:
- The `asm` header line ends with `:`.
- Body lines are indented one level relative to the header (same as
  every other Ouro indented block).
- Each indented line is one assembly instruction or directive,
  passed through to the assembler unchanged. Leading whitespace
  beyond the indentation level is preserved.
- The body ends at the first DEDENT.

Comments inside an `asm` body use the **assembler's** syntax —
`#` for GNU as on x86_64. The Ouro `#` comment rule is suppressed
inside asm bodies. (A line starting with `#` is passed through as
an assembler comment; a `#` mid-line behaves per the assembler's
rules, not Ouro's.) 🟢 *Locked.*

## Calling convention

`asm` follows the **SystemV x86_64 ABI** for its inputs and
outputs. The Ouro signature determines which registers the body can
read on entry and must populate on exit.

| Arg position | Register   |
|--------------|------------|
| 1            | `%rdi`     |
| 2            | `%rsi`     |
| 3            | `%rdx`     |
| 4            | `%rcx`     |
| 5            | `%r8`      |
| 6            | `%r9`      |
| further      | stack      |
| return       | `%rax`     |

Float args and return values use `%xmm0..%xmm7` per the same ABI.
For now, asm decls with `f32`/`f64` in their signature are accepted
but the docstring should call out that the body is responsible for
honoring the FP register usage.

## What's allowed in the signature

```ouro
asm name[no_generics_in_v1](            # ← generics forbidden
    a: i32,
    b: ptr[u8],                            # raw pointer OK
    c: []u8,                               # slice arg = pointer-to-fat-pointer
) -> i64:                                  # integer or pointer return
    ...
```

Permitted in v1:
- Primitive integer / float / `bool` parameters.
- `ptr[T]` parameters (treated as `l` — pointer-sized integer).
- `[]T` slice parameters — the caller passes a pointer to the
  16-byte fat pointer, so the asm body sees a single `l`.
- Primitive / pointer return types, including `never`.

**Not** permitted in v1:
- Generic parameters (`asm id[T](x: T) -> T`) — needs
  monomorphization at the asm level, deferred.
- Struct value parameters/returns — the ARC ownership model would
  collide with the assembly body's responsibility for refcounting.
- `Self` / method position (no `asm` inside `struct`).
- `var[T]` parameters — bindings are pass-by-value at the ABI.

## `never` return

Functions like `_start` and `sys_exit` never return. The Ouro
return type is the existing `never` (already used for sentinel
analyses). Codegen accepts no `ret` instruction in the body for
`never`-returning asm decls. The typechecker treats subsequent code
as dead, same as today.

## Codegen strategy

🔵 *Implementation plan, not finalized.*

QBE has no inline-assembly form. To keep the toolchain unchanged,
each `asm` is emitted into a **sidecar `.s` file** alongside the
QBE-emitted `.s`. Both files feed `cc` (or just `as` + `ld`) and
link together.

🟢 *Locked: one sidecar per module.* Each `.ou` source that contains
any `asm` decls produces its own `<name>.asm.s` file, paired with
the `<name>.qbe.s` that QBE produces from the regular IR. Per-module
files keep the model uniform with how the loader treats everything
else (each module is independently compilable) and the linker
overhead is negligible.

For each module:

```
foo.ou  ──►  loader+codegen  ──►  foo.ssa            (QBE IR)
                              ──►  foo.asm.s          (asm bodies)

                                          ↓ qbe
                                   foo.qbe.s
                                          ↓ cc (asm input)
                                   foo.qbe.o  +  foo.asm.o
                                          ↓ ld
                                       linked
```

The sidecar `.s` is generated by the codegen pass: for every
`asm` decl, emit:

```asm
    .globl <symbol>
    .type  <symbol>, @function
<symbol>:
    <body lines, one per Ouro line>
```

`<symbol>` follows the same module-prefix mangling as regular fns —
`$<prefix>__<name>` for imported modules, bare `$<name>` for entry
or for **runtime-well-known** symbols (see below).

## Runtime-well-known symbols

Some asm decls are called by the codegen *implicitly*, not by user
source: `arc_alloc`, `_start`, `sys_write` (when an `io.X` lowers).
These need stable, unprefixed symbol names like `_start` —
otherwise the linker can't find them.

🟢 *Locked: convention-based.* Files under `runtime/` are treated as
the implicit runtime — symbols defined there are emitted **bare**,
regardless of the per-module prefix mangling that applies elsewhere.
Same arrangement the current C runtime uses today: `runtime/runtime.c`
exports `arc_alloc`, `println`, etc. without any prefix; the codegen
calls them with their bare names.

No `@export` attribute in v1. If a future use case needs
finer-grained control, we add it then.

## Errors

The Ouro compiler validates only the signature and the structure;
the asm body is opaque text. Errors that show up:

- **At parse time**: mismatched indentation, missing `:`, signature
  syntax problems.
- **At assemble time** (after the sidecar `.s` is generated): the
  assembler reports invalid instructions, undefined labels, etc.
  Errors point at the sidecar line.

🟢 *Locked: source-line mapping via `# from foo.ou:N` comments.*
Each emitted asm body line is preceded by an assembler comment of
the form `# from <path>:<line>` so the assembler's "line N" error
message can be traced back to the original `.ou` source by reading
the sidecar. Full `.loc` debuginfo directives are deferred — the
comment form is cheap and good enough until source-level debugging
becomes a v2+ goal.

## Worked example: `sys_write` and `println`

`runtime/syscalls.ou`:

```ouro
asm sys_write(fd: i32, buf: ptr[u8], len: usize) -> i64:
    mov     $1, %rax
    syscall
    ret
```

Generated sidecar (`syscalls.asm.s`):

```asm
    .globl sys_write
    .type  sys_write, @function
sys_write:
    mov     $1, %rax
    syscall
    ret
```

Pure-Ouro `runtime/io.ou` using it (sketch — depends on slice ABI
details still being worked out):

```ouro
syscalls = import("./syscalls")

fn println(s: []u8):
    if s.len > 0:
        syscalls.sys_write(1, s.ptr, s.len)
    syscalls.sys_write(1, "\n".ptr, 1)
```

The codegen emits `call $syscalls__sys_write(...)` for each call;
the linker resolves it to the asm decl's body. Type-checking is
normal — the Ouro signature is the contract.

## `_start` is special

`_start` is the kernel-supplied entry point: the stack on entry
points at `argc`, not at a return address. Even though it looks
like a "function," it can't be called by anything (no caller in
Ouro). Two design choices:

- Treat `_start` as a **regular asm decl**. The fact that nobody
  Ouro-side calls it is fine; the linker uses it as the entry
  symbol. The body must not `ret` (kernel has nothing to return
  to); use `syscall(60, rc)` to exit.
- Add a **`@entry`** attribute that marks an asm decl as the binary
  entry. Lets us add invariants (no callers, no Ouro return type).

v1 lean: just a regular asm decl named `_start`, by convention.
v2 can add the attribute if it earns its keep.

## What this doesn't do

- **Inline asm inside a regular Ouro fn body.** Rust-style
  `asm!("...")` inside arbitrary code is much more invasive — needs
  to integrate with QBE's register allocator. Out of scope for v1.
- **Output/input/clobber constraints** like GCC's extended asm.
  The function-call ABI gives us this for free: inputs are
  register-passed per SystemV, outputs go through `%rax`, and the
  call-instruction boundary tells the optimizer "treat the whole
  thing as a clobber." If finer-grained control becomes necessary,
  add it later.
- **Multi-arch.** Linux x86_64 only. The body is target-specific.
  A multi-arch story (`asm` with platform-conditional bodies,
  or sibling files like `syscalls.x86_64.ou` / `syscalls.aarch64.ou`)
  is open.

## Locked summary

| Decision                  | Choice                                                            |
|---------------------------|-------------------------------------------------------------------|
| Runtime symbol mangling   | Convention: `runtime/*.ou` symbols emit bare (no prefix)          |
| Source-line mapping       | `# from <path>:<line>` comment prepended to each emitted line     |
| Comments inside asm body  | Assembler syntax (`#` for GNU as on x86_64) only; Ouro `#` suppressed |
| Sidecar layout            | One `.asm.s` per module, paired with the `.qbe.s` from QBE        |

## Migration plan

Once `asm` lands, the C runtime moves piece by piece:

1. **`syscalls.ou`** — sys_write, sys_mmap, sys_munmap, sys_exit
2. **`entry.ou`** — entry asm decl (`_start` symbol)
3. **`alloc.ou`** — heap allocator in pure Ouro, calling syscalls.sys_mmap
4. **`arc.ou`** — ARC primitives in pure Ouro
5. **`io.ou`** — println/print in pure Ouro
6. **`printf`** — decided at migration time: keep in asm/C as the
   final hold-out, or replace with typed `io.print_int` etc.
7. **Delete `runtime/runtime.c`** — single artifact, zero C.

## Cross-references

- [conventions.md](conventions.md) — privacy, dunders, naming
- [memory.md](memory.md) — slice ABI and ARC layout
- [`docs/codegen.md`](../codegen.md) — current emission strategy
- [`runtime/runtime.c`](../../runtime/runtime.c) — what's being
  migrated
