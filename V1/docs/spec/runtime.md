# Runtime

What the Ouro runtime provides and how programs link against it.

🟢 *The runtime is libc-free on Linux x86_64.* It's a small C file
(~280 lines) that provides syscall wrappers, an `mmap`-backed
allocator, ARC primitives, and minimal I/O. No `libc`, no `libgcc`,
no dynamic linker. See
[`runtime/runtime.c`](../../runtime/runtime.c) for the source.

🟡 *Other targets (Linux aarch64, WASI) will land as sibling files
under `runtime/` and select via build flag.*

## How linking works

```
.ou source  →  loader+codegen  →  .ssa (QBE IR)  →  qbe  →  .s (assembly)
                                                              ↓
                                                cc -nostdlib -static + runtime.o
                                                              ↓
                                                       static executable
```

The link is:

```
cc -O2 -static -nostdlib -ffreestanding -fno-stack-protector \
   -fno-builtin -fno-pic -fno-pie -no-pie \
   -o exe program.s runtime.o
```

No crt0, no libc — the runtime supplies `_start` and everything else
the program needs.

Resulting binaries:
- Statically linked (`ldd` reports "not a dynamic executable")
- ~14 KB for a "hello world" (down from libc-linked sizes)
- One single artifact, no runtime dependencies

## Entry point

The kernel jumps to `_start` with the stack pointing at `argc`. The
runtime's `_start` is naked inline asm — no compiler prologue, since
the kernel-supplied stack layout isn't a function call:

```c
__attribute__((naked, noreturn))
void _start(void) {
    __asm__ volatile(
        "xor %%rbp, %%rbp\n\t"     /* end of frame chain */
        "call main\n\t"
        "mov %%eax, %%edi\n\t"      /* main's i32 → exit code */
        "mov $60, %%eax\n\t"        /* SYS_exit */
        "syscall\n\t"
        ::: "memory"
    );
}
```

Ouro's `main` returns `i32` (QBE `function w $main`), which the SystemV
ABI puts in `%eax`. `_start` moves it to `%edi` (first arg of
`sys_exit`) and invokes syscall 60. No exit handlers, no atexit, no
flush — output is unbuffered anyway.

## Syscalls

Three inline-asm wrappers handle every kernel call we make:

```c
static long syscall1(long n, long a);
static long syscall3(long n, long a, long b, long c);
static long syscall6(long n, long a, long b, long c, long d, long e, long f);
```

Each emits `syscall` with the Linux x86_64 ABI: `%rax` = number,
`%rdi/%rsi/%rdx/%r10/%r8/%r9` = args, return in `%rax`. Clobbers
`%rcx`, `%r11`, and `"memory"`.

The runtime currently uses:

| Number | Name      | Used for                       |
|-------:|-----------|--------------------------------|
| `1`    | `write`   | `io.println`, `io.print`, `printf` |
| `9`    | `mmap`    | Allocator heap reservation     |
| `60`   | `exit`    | Process exit (via `_start`)    |

`munmap` is exposed but currently unused (the heap is mapped once at
startup and never returned to the kernel).

## Allocator

🟡 *v1: single mmap'd region, bump pointer + first-fit free list.
No coalescing.*

```
heap_start ─┐
            │  ┌────────────────────────────────────────┐
            │  │ used blocks                            │
            │  │  ├─ 8-byte size header                 │
            │  │  └─ payload (16-byte aligned)          │
            │  │                                        │
            │  │ ← heap_bump                            │
            │  │                                        │
            │  │ free space (bump down)                 │
            │  │                                        │
            │  └────────────────────────────────────────┘
heap_end ─→

free_list ─→ FreeBlock → FreeBlock → FreeBlock → null
              {size, next}
```

Reserved size: **64 MiB**. Failure to mmap or running out of room
calls `sys_exit(137)` (out-of-memory).

Released blocks join a singly-linked free list. `heap_alloc` checks
the free list first (first-fit), else bumps from the unused tail.
There is **no coalescing** of adjacent free blocks — fragmentation
grows with mixed sizes. v1 tests don't hit this; v2 will need a real
allocator.

Alignment: payload is always 16-byte aligned (after the size header).
This matches what the ARC layout expects.

## ARC primitives

🟢 *Locked.* Heap layout (24-byte header):

```
offset -24..-16 : weak refcount   (i64)
offset -16..-8  : strong refcount (i64)
offset  -8..0   : drop_fn         (i64; 0 = no destructor)
offset   0...   : user data
```

Functions exported to the codegen:

```c
void* arc_alloc(int64_t size, void* drop_fn);
void  arc_inc(void* user_ptr);
void  arc_release(void* user_ptr);
void  weak_inc(void* user_ptr);
void  weak_release(void* user_ptr);
void* weak_upgrade(void* user_ptr);
```

The codegen emits calls into these at construction, copy-binding, and
scope exit. See [memory.md](memory.md) for the full semantics.

## I/O

```c
void println(const ouro_slice_u8* s);          /* io.println */
void print(const ouro_slice_u8* s);            /* io.print */
int  printf(const char* fmt, ...);             /* io.printf intrinsic */
```

`println` and `print` lower from `[]u8` slices: the codegen passes the
**address** of the stack-allocated `{ptr, len}` pair, the runtime
reads it.

`printf` is a minimal implementation supporting:

| Specifier | Meaning |
|-----------|---------|
| `%d` `%i` | signed `int` |
| `%ld` `%li` | signed `long` |
| `%u` | unsigned `int` |
| `%lu` | unsigned `long` |
| `%x` | hex (lowercase, no prefix) |
| `%lx` | hex `long` |
| `%s` | NUL-terminated `char*` |
| `%c` | single byte |
| `%%` | literal `%` |

Notably **absent**: width and precision specifiers, `%f` (floats), `%p`
(pointers — use `%lx`), positional args. Each conversion emits one
`write(1, ...)` syscall — many small writes, but correctness over
speed at this scale.

The codegen treats `<module>.printf(fmt, ...)` as a variadic
intrinsic, lowering directly to `call $printf(l <fmt_ptr>, ..., <args>)`
with QBE's `...` separator. Real-module `.printf()` calls (after the
loader landed) get normal cross-module routing — the libc-printf
intrinsic only fires for legacy stub modules.

## Symbol layout

The runtime exports unqualified names that the codegen calls directly:

| Symbol         | Source             | Called from               |
|----------------|--------------------|---------------------------|
| `_start`       | runtime            | (kernel entry)            |
| `main`         | user program       | `_start`                  |
| `arc_alloc`    | runtime            | every struct literal      |
| `arc_inc`      | runtime            | copy-binding emission     |
| `arc_release`  | runtime            | scope-exit / return       |
| `weak_inc`     | runtime            | weak-field store          |
| `weak_release` | runtime            | weak-field overwrite/drop |
| `weak_upgrade` | runtime            | weak-field read           |
| `println`      | runtime            | `io.println(s)`           |
| `print`        | runtime            | `io.print(s)`             |
| `printf`       | runtime            | `io.printf(fmt, ...)`     |

The `io.X` resolution is the legacy "drop the module prefix" path in
codegen — it predates the module loader and stays for now. Once
`std/io.ou` exists as real Ouro source, these become `std__io__println`
etc., and the C runtime layer becomes an internal helper (`_println_raw`)
that `std/io.ou` calls into.

## Slice ABI

🟢 *Locked.*

```c
typedef struct {
    const unsigned char* ptr;   /* *T for non-u8 element types */
    int64_t              len;
} ouro_slice_u8;
```

The codegen allocates the `{ptr, len}` pair on the caller's stack and
passes its **address** to runtime functions. Two-fields-by-value would
also work but doubles the parameter count; address-by-reference keeps
parameter lists short.

## What the runtime does NOT do (yet)

| Feature | Status |
|---|---|
| stdin / `io.read_line` | not yet |
| File I/O (open/close/read/write) | not yet |
| Time, sleep | not yet |
| argv / env access | not yet (`_start` discards them) |
| `math.sqrt` / libm replacements | not yet |
| Heap coalescing on free | not yet — fragmentation grows |
| Other platforms (aarch64, WASI, macOS) | not yet |

## Cross-references

- [`runtime/runtime.c`](../../runtime/runtime.c) — the source
- [memory.md](memory.md) — slice and ARC semantics
- [stdlib.md](stdlib.md) — what migrates from runtime into `.ou` source
- [`docs/codegen.md`](../codegen.md) — how the codegen emits calls
