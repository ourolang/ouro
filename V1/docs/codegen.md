# `src/codegen.py` — typed AST → QBE IR

> The final pass. Reads the AST, the `ResolutionMap`, and the
> `TypeMap`. Emits QBE IR text. Output is a string; the build pipeline
> pipes it to `qbe`, which emits assembly, which `cc` turns into an
> executable.

## What QBE IR is

Before reading the codegen, a tour of the target. The **QBE
intermediate representation** is a small, readable SSA-style language
designed for backend code generators. Here's what a minimal "hello"
lowers to:

```
data $hello = { b "hello, ouro!\n", b 0 }

export function w $main() {
@start
    %r =w call $puts(l $hello)
    ret 0
}
```

### Three name sigils

| Sigil | Meaning |
|---|---|
| `$global` | Global symbol (data, function name) — resolved at link time |
| `%temp` | SSA value — assigned exactly once |
| `@label` | Basic block label |

That's the whole namespace. Three sigils. Every name in QBE has one
of these.

### Four base types

| Type | Width | What |
|---|---|---|
| `w` | 32 bits | int |
| `l` | 64 bits | int (also pointer) |
| `s` | 32 bits | float |
| `d` | 64 bits | double |

Aggregate types are declared with `type :Foo = { l, l }` and used by
name with the `:Foo` sigil.

### Instruction shape

Every line is one of:

- `%dest =<type> <op> <args>` — produce a value
- `<store> <value>, <addr>` — store (no destination)
- `ret`, `ret <value>`, `jmp @lbl`, `jnz <cond>, @if_yes, @if_no` —
  terminators

Functions are sequences of basic blocks, each starting with a
`@label` and ending with a terminator. **SSA = Static Single
Assignment**, so every `%temp` is assigned exactly once. This makes
optimizing transformations easy for QBE.

### Common operations

- Arithmetic: `add`, `sub`, `mul`, `div`, `rem`
- Bitwise: `and`, `or`, `xor`, `shl`, `sar`, `shr`
- Comparison: `ceq`, `cne`, `cslt`, `csle`, `csgt`, `csge` (signed),
  `cult`, `cule`, etc. (unsigned), `clt`, etc. (float)
  — comparisons return `w` (0 or 1)
- Memory: `loadl`, `loadw`, `loadub`, `loadsh`, etc.; `storel`,
  `storew`, `storeb`, etc.
- Conversion: `extsw` (sign-extend w→l), `extuw` (zero-extend),
  `truncl`, etc.
- Stack: `alloc4 N`, `alloc8 N`, `alloc16 N` — allocate N bytes,
  aligned to 4/8/16 — returns the slot pointer

Now to the codegen.

## File structure

```
imports                                       (lines 1-83)
CodegenError
_ARC_HEADER constant
_base, _size, _store, _load, _is_managed
FieldLayout, StructLayout (with has_drop), _layout
Local, FnCtx (with managed_locals)
Codegen class                                 (lines 208-849)
  state                                       (lines 209-214)
  _ty, _ast_ty                                (lines 218-233)
  _intern_str                                 (lines 237-255)
  _collect_layouts                            (lines 259-263)
  generate (entry)                            (lines 267-291)
  _emit_fn                                    (lines 295-323)
  _emit_block, _emit_stmt                     (lines 327-357)
  _emit_binding, _emit_assignment             (lines 359-403)
  _emit_loop, _emit_for                       (lines 407-474)
  _emit_expr (dispatch)                       (lines 478-540)
  _emit_name                                  (lines 544-552)
  _emit_binary, _emit_unary                   (lines 556-599)
  _gep, _emit_field                           (lines 603-629)
  _emit_index                                 (lines 633-647)
  _emit_call                                  (lines 651-702)
  _emit_struct_lit                            (lines 706-735)
  _emit_string_lit                            (lines 739-749)
  _emit_if                                    (lines 753-790)
  _emit_match                                 (lines 794-849)
  _emit_block_yielding (helper for if/match)  (lines 832-857)
generate() free function                      (lines 862-864)
```

## Module docstring (lines 1-26)

The file's docstring is the **spec for what v1 does and doesn't
support**:

> v1 scope:
>   - Non-generic free functions and struct methods
>   - Primitive types mapped to QBE base types (w / l / s / d)
>   - Slices as fat pointers {ptr: l, len: l} allocated on the stack
>   - Non-generic structs heap-allocated via `arc_alloc(size, drop_fn)`,
>     with `arc_release` emitted before every `ret` for managed locals
>   - Returning a struct transfers ownership (release skipped on the
>     consumed local)
>   - Arithmetic, comparison, logical operators
>   - if/else using jnz; match with value / wildcard patterns
>   - for loops over slices lowered to index-based loops
>   - loop / while (parse-time desugar) / break / continue
>   - Const bindings → SSA temps; var[T] bindings → alloc stack slots
>   - String literals interned in the data section
>   - Function calls with positional arguments
>   - io.printf as a variadic intrinsic (libc printf)
>   - Tagged-union returns via `arc_alloc`-boxed `{tag, payload}`;
>     `?=` emits real tag comparisons; narrowed references extract the
>     payload (so `e.msg` works after `if e ?= ParseError`); per-union
>     `drop_fn` helpers release boxed struct payloads
>   - Managed struct fields: per-struct drop wrappers chain user
>     `__drop__` then release each managed field; struct lit and
>     field assign inc on copy / release on overwrite
>   - Block-scope ARC: each Block opens a managed-locals scope;
>     normal exit releases the scope; break/continue release down to
>     the enclosing loop body's depth
>   - `weak[T]` fields: weak_inc on store, weak_release on overwrite
>     and drop; reads call weak_upgrade and box the result as
>     `T | Null` for the user's `?=`
>   - Generic-struct monomorphization: walks the TypeMap for
>     `StructTy(name, type_args)` instantiations and emits a
>     specialized struct + methods per (base, args) under an active
>     type-parameter substitution; encoded names like `Box_i32`
>   - Generic free-function monomorphization: each call site infers
>     the substitution by unifying param types against arg types (or
>     uses explicit `id[i32](42)`), registers the spec in a worklist,
>     and emits the body afterwards under the substitution; encoded
>     names like `id_i32`, `pick_i32_i64`
>   - Slice `.ptr` field accessor (alongside `.len`)
>   - Multi-module programs: the loader recursively compiles
>     `import("./helper")` and friends; codegen takes a per-module
>     prefix (empty for the entry module, file-stem for imports) and
>     applies it to every user-defined symbol — `$helper__foo`,
>     `:helper__Point`, `$helper___union_drop_0`. Cross-module calls
>     route to the importee's prefix. `import("std/X")` still uses
>     the legacy C-runtime stub path (prefix-dropping) until a real
>     bundled stdlib lands.
>
> Deferred to v2+:
>   - Slice owner handle / refcount bump
>   - Cross-module struct sharing and cross-module generic free fns
>   - `std/` prefix → bundled stdlib root
>   - match type patterns (runtime tag dispatch)
>   - Named call argument reordering

Read this first when working on the codegen. It's the contract.

## ARC layout (line 95)

```python
_ARC_HEADER = 16  # 8 bytes refcount + 8 bytes drop_fn
```

Every heap-allocated struct has this header **before** the user-visible
data. Layout:

```
offset  0..8:   refcount (i64)
offset  8..16:  drop_fn pointer (i64)
offset 16..N:   user data
```

The user pointer (what gets returned from struct construction and
passed everywhere as "the struct") points at offset 16, *past* the
header. To find the header, subtract 16.

The `_ARC_HEADER` constant is no longer used at the IR level —
`arc_alloc` (in [`runtime/runtime.c`](../runtime/runtime.c)) handles
header initialization. The constant is still referenced in struct
layout calculations.

This is what the e2e tests
([test/python/test_e2e.py](../test/python/test_e2e.py)) exercise
end-to-end — compile a struct, link the runtime, run, and assert
on `__drop__` output.

## Type helpers (lines 98-135)

Three small utilities that collapse the entire AST→QBE type
translation:

### `_base(ty)` — Ty → single-letter base type

```python
def _base(ty):
    ty = _readable(_unwrap(ty))
    if isinstance(ty, PrimTy):
        n = ty.name
        if n == "f64": return "d"
        if n == "f32": return "s"
        if n in ("i64", "u64", "isize", "usize"): return "l"
        return "w"  # i8/u8/i16/u16/i32/u32/bool
    return "l"  # struct ref, slice fat-ptr, ptr
```

The whole ABI in one function. Note `_readable(_unwrap(ty))` —
strip all wrappers, then strip the readable ones again (which is a
no-op since `_unwrap` already removed them all). The double-call is
defensive and free.

### `_size(ty)` — bytes

```python
def _size(ty):
    ty = _readable(_unwrap(ty))
    if isinstance(ty, PrimTy):
        n = ty.name
        if n in ("i8", "u8"): return 1
        if n in ("i16", "u16"): return 2
        if n in ("i32", "u32", "f32", "bool"): return 4
        return 8
    if isinstance(ty, SliceTy): return 16
    return 8
```

Used for stack allocation and struct field offsets.

### `_store(ty)` and `_load(ty)`

```python
def _store(ty): return {"d": "stored", "s": "stores", "l": "storel", "w": "storew"}[_base(ty)]
def _load(ty):  return {"d": "loadd", "s": "loads", "l": "loadl", "w": "loadw"}[_base(ty)]
```

Just dictionary lookups on `_base`. The QBE instruction names follow
the pattern: prefix + base.

## Struct layout (lines 141-167)

```python
@dataclass class FieldLayout: name: str; ty: Ty; offset: int
@dataclass class StructLayout: name: str; fields: list[FieldLayout]; total: int

def _layout(name, fields):
    off = 0
    result = []
    for fname, fty in fields:
        s = _size(fty)
        a = min(s, 8)                      # alignment = min(size, 8)
        if off % a: off += a - (off % a)    # align up to a
        result.append(FieldLayout(fname, fty, off))
        off += s
    if off % 8: off += 8 - (off % 8)        # pad total to 8-byte multiple
    return StructLayout(name, result, off)
```

A simple struct layout algorithm: each field is aligned to its size
(capped at 8 bytes). After all fields, pad to 8-byte alignment.

Layout decisions live exactly here. If you wanted #pragma pack, or
SOA, or different alignment rules, you'd change this one function.

## Per-function context (lines 173-203)

```python
@dataclass class Local:
    loc: str        # SSA temp name (const) or stack slot pointer (var)
    ty: Ty
    is_var: bool

class FnCtx:
    self._n: int = 0                                         # next temp counter
    self._ln: int = 0                                        # next label counter
    self.locals: dict[str, Local]                            # name → Local
    self.loop_stack: list[tuple[str, str]]                   # (continue_lbl, break_lbl)
    self.out: list[str]                                      # emitted IR lines

    def tmp(self, hint="t"):  # → "%_<hint><n>"
    def lbl(self, hint="L"):  # → "@<hint><n>"
    def emit(self, line):     # appends with 4-space indent
    def label(self, lbl):     # appends without indent
```

Three things to internalize:

### Local: SSA temp vs. stack slot

For `x = 42` (const-like binding), `Local("42", PrimTy("isize"),
is_var=False)`. Reading `x` returns the literal `"42"` directly — no
QBE instruction emitted.

For `x: var[i32] = 42`, `Local("%t3", PrimTy("i32"), is_var=True)`
where `%t3` is a stack pointer from `alloc4 4`. Reading `x` emits
`%newtmp =w loadw %t3`. Writing emits `storew <val>, %t3`.

This is why **`var[T]` is more expensive than `T`** — it forces a
stack slot. Const bindings just bind names to SSA values directly.

### tmp/lbl naming

`%_v0`, `%_v1`, `%_t0`, `%_addr3`, etc. The hint is for debug
readability. The leading underscore is to avoid collisions with
parameter names (parameters are `%a`, `%b`, etc. — no underscore).

### loop_stack

Used by `break` / `continue`:

```python
elif isinstance(stmt, Break):
    if ctx.loop_stack:
        ctx.emit(f"jmp {ctx.loop_stack[-1][1]}")  # break_lbl
elif isinstance(stmt, Continue):
    if ctx.loop_stack:
        ctx.emit(f"jmp {ctx.loop_stack[-1][0]}")  # continue_lbl
```

When emitting a loop body, push `(continue_lbl, break_lbl)` onto the
stack. Pop after. Nested loops form a stack — `break` always exits
the innermost.

## Codegen state (lines 209-214)

```python
class Codegen:
    def __init__(self, types, res):
        self._types = types                    # TypeMap (input)
        self._res = res                        # ResolutionMap (input)
        self._data = []                        # data-section lines being built
        self._str_n = 0                        # interned-string counter
        self._layouts = {}                     # struct name → StructLayout
```

`_data` and `_str_n` accumulate during function emission. The data
section gets printed before functions, but it's *built* incrementally
as functions reference strings.

## `_intern_str` — strings in the data section (lines 237-255)

```python
def _intern_str(self, value: bytes) -> str:
    name = f"$_s{self._str_n}"
    self._str_n += 1
    parts, buf = [], []
    for b in value:
        if 0x20 <= b < 0x7F and b not in (ord('"'), ord("\\")):
            buf.append(chr(b))
        else:
            if buf: parts.append(f'b "{"".join(buf)}"'); buf = []
            parts.append(f"b {b}")
    if buf: parts.append(f'b "{"".join(buf)}"')
    parts.append("b 0")          # NUL terminator
    self._data.append(f"data {name} = {{ {', '.join(parts)} }}")
    return name
```

Produces:

```
data $_s0 = { b "OK", b 0 }
data $_s1 = { b "hello, ", b "world\n", b 0 }      # if newline split
```

Always NUL-terminated. Bytes are split into runs of printable ASCII
(encoded as `b "..."`) and individual byte entries (`b <num>`). The
NUL ensures C string functions (`puts`, etc.) work without extra
length info.

**Strings are not deduplicated.** Two identical literals produce two
data entries. Easy to add later if a real workload shows it matters.

## Top-level `generate()` (lines 267-291)

```python
def generate(self, tree):
    self._collect_layouts(tree)
    fn_parts = []

    for decl in tree.declarations:
        if isinstance(decl, Function) and not decl.generics:
            fn_parts.append(self._emit_fn(decl, struct_name=None))
        elif isinstance(decl, Struct) and not decl.generics:
            for m in decl.methods:
                if not m.generics:
                    fn_parts.append(self._emit_fn(m, struct_name=decl.name))

    out = []
    # Aggregate type declarations
    for lay in self._layouts.values():
        field_types = ", ".join(_base(f.ty) for f in lay.fields)
        out.append(f"type :{lay.name} = {{ {field_types} }}")
    if self._layouts: out.append("")
    # Data section
    out.extend(self._data)
    if self._data: out.append("")
    # Functions
    out.extend(fn_parts)
    return "\n".join(out)
```

Two passes through the file:
1. `_collect_layouts(tree)` — register every non-generic struct's layout.
2. Emit each function (free or method) into `fn_parts`.

Then assemble in standard order:
```
type :Foo = { l, l }
type :Bar = { w }
                                  <blank>
data $_s0 = { ... }
data $_s1 = { ... }
                                  <blank>
function ...
function ...
```

The data section is built incrementally during function emission but
printed in the right place because we emit functions to a list first.

**Generic structs and free functions are monomorphized.** During
`_collect_layouts`, generic decls are recorded in `_generic_structs` /
`_generic_fns`. The main pass skips them in-place; call sites infer
substitutions and register spec names (`Box_i32`, `id_i32`). After
the main pass, `generate()` drains the spec worklists, emitting each
specialization under an active `_current_subst` so `T` resolves to
the concrete type in every reachable AST node.

## Function emission (lines 295-323)

```python
def _emit_fn(self, fn, struct_name):
    ctx = FnCtx()
    qname = f"${struct_name}__{fn.name}" if struct_name else f"${fn.name}"
    export = "export " if fn.name == "main" else ""

    # Return type
    ret_ann = self._ast_ty(fn.return_type) if fn.return_type else UNIT
    ret_qbe = _base(ret_ann) if not isinstance(ret_ann, (UnitTy, UnknownTy)) else ""

    # Parameters
    params = []
    if fn.self_param is not None:
        params.append("l %self")
        ctx.locals["self"] = Local("%self", WrapperTy("ptr", UNKNOWN), False)
    for p in fn.params:
        pty = self._ast_ty(p.type)
        params.append(f"{_base(pty)} %{p.name}")
        ctx.locals[p.name] = Local(f"%{p.name}", pty, False)

    sig = f"{export}function {ret_qbe + ' ' if ret_qbe else ''}{qname}({', '.join(params)}) {{"
    ctx.label("@start")
    self._emit_block(fn.body, ctx)

    # Ensure terminator
    last = ctx.out[-1].strip() if ctx.out else ""
    if not (last.startswith("ret") or last.startswith("jmp") or last.startswith("jnz")):
        ctx.emit("ret")

    return "\n".join([sig, *ctx.out, "}", ""])
```

### Naming

- Free functions: `$fn_name` (e.g. `$add`, `$main`)
- Methods: `$<Struct>__<method>` (e.g. `$Connection__open`,
  `$Connection____drop__` — yes, two underscores between struct and
  the dunder method)

`fn main` gets `export ` prepended so the linker sees it as the
program entry point.

### Self handling

The `self` parameter is always `l %self` in QBE (a pointer). The
local entry is `Local("%self", WrapperTy("ptr", UNKNOWN), False)`.
It's tagged `is_var=False` because `self` itself is not a stack slot
— it's a parameter, accessed directly.

The `UNKNOWN` inner is a simplification because the codegen doesn't
fully model `Self` resolution; it treats `self` as "a pointer to
some struct, look up fields by their layout."

### Terminator enforcement

QBE requires every basic block to end with a terminator (`ret`,
`jmp`, `jnz`). The check at lines 319-321 ensures this — if the
last emitted line isn't a terminator, append `ret`. This is what
makes `fn f(): pass` legal: the body is empty, so we just close with
`ret`.

## Statement dispatch (lines 327-357)

```python
def _emit_stmt(self, stmt, ctx):
    if isinstance(stmt, Pass): return
    if isinstance(stmt, ExprStatement): self._emit_expr(stmt.expr, ctx)
    elif isinstance(stmt, Binding): self._emit_binding(stmt, ctx)
    elif isinstance(stmt, Assignment): self._emit_assignment(stmt, ctx)
    elif isinstance(stmt, Return):
        if stmt.value is None: ctx.emit("ret")
        else:
            v = self._emit_expr(stmt.value, ctx)
            ctx.emit(f"ret {v}")
    elif isinstance(stmt, Break):
        if ctx.loop_stack: ctx.emit(f"jmp {ctx.loop_stack[-1][1]}")
    elif isinstance(stmt, Continue):
        if ctx.loop_stack: ctx.emit(f"jmp {ctx.loop_stack[-1][0]}")
    elif isinstance(stmt, Loop): self._emit_loop(stmt.body, ctx)
    elif isinstance(stmt, For): self._emit_for(stmt, ctx)
    elif isinstance(stmt, Block): self._emit_block(stmt, ctx)
```

The `ExprStatement` branch evaluates the expression and **discards
the result**. The expression's side effects (e.g. function calls,
struct allocation) happen; the value is just not used. This is what
makes statement-position function calls work.

## Bindings (lines 359-385)

```python
def _emit_binding(self, stmt, ctx):
    if stmt.name == "_":
        self._emit_expr(stmt.value, ctx); return

    # Re-assignment into existing var slot
    if stmt.type is None and stmt.name in ctx.locals:
        lv = ctx.locals[stmt.name]
        if lv.is_var:
            val = self._emit_expr(stmt.value, ctx)
            ctx.emit(f"{_store(lv.ty)} {val}, {lv.loc}"); return

    val = self._emit_expr(stmt.value, ctx)
    decl_ty = self._ast_ty(stmt.type) if stmt.type else self._ty(stmt.value)
    is_var = isinstance(decl_ty, WrapperTy) and decl_ty.wrapper == "var"
    inner = _readable(decl_ty)

    if is_var:
        s = _size(inner)
        a = min(s, 8)
        slot = ctx.tmp(f"v_{stmt.name}")
        ctx.emit(f"{slot} =l alloc{a} {s}")
        ctx.emit(f"{_store(inner)} {val}, {slot}")
        ctx.locals[stmt.name] = Local(slot, inner, True)
    else:
        ctx.locals[stmt.name] = Local(val, inner, False)
```

Three cases:

1. **`_ = expr`** — evaluate, discard the value. (No local registered.)
2. **Re-assignment to existing var slot** — emit store, no new local.
3. **Fresh binding** — eval RHS, decide var vs. const:
   - **Var**: alloc stack slot, store initial, register `is_var=True`
   - **Const**: just bind the name to the SSA temp, register
     `is_var=False`

## Assignment (lines 387-403)

```python
def _emit_assignment(self, stmt, ctx):
    val = self._emit_expr(stmt.value, ctx)
    target = stmt.target
    if isinstance(target, Name) and target.name in ctx.locals:
        lv = ctx.locals[target.name]
        if lv.is_var: ctx.emit(f"{_store(lv.ty)} {val}, {lv.loc}")
    elif isinstance(target, FieldAccess):
        obj = self._emit_expr(target.obj, ctx)
        base_ty = _unwrap(self._ty(target.obj))
        if isinstance(base_ty, StructTy):
            lay = self._layouts.get(base_ty.name)
            if lay:
                fl = next((f for f in lay.fields if f.name == target.field), None)
                if fl:
                    dest = obj if fl.offset == 0 else self._gep(obj, fl.offset, ctx)
                    ctx.emit(f"{_store(fl.ty)} {val}, {dest}")
```

For field assignments: compute the field's address (via `_gep` —
generate effective pointer — for non-zero offsets), emit a store of
the right width.

## Loops

### `loop` (lines 407-416)

```python
def _emit_loop(self, body, ctx):
    loop = ctx.lbl("loop")
    after = ctx.lbl("after")
    ctx.emit(f"jmp {loop}")
    ctx.label(loop)
    ctx.loop_stack.append((loop, after))
    self._emit_block(body, ctx)
    ctx.loop_stack.pop()
    ctx.emit(f"jmp {loop}")
    ctx.label(after)
```

The canonical infinite-loop pattern:

```
    jmp @loopN
@loopN
    <body>
    jmp @loopN
@afterN
```

The `jmp @loopN` before the label is a QBE requirement (every basic
block has to be reached by some terminator).

### `for x in slice` (lines 418-465)

The most code in any single emission method, because it lowers to
an index-based loop:

```
    %lp =l add %slice, 8           # len pointer
    %len =l loadl %lp               # length
    %i =l alloc8 8                  # index slot
    storel 0, %i
    jmp @forN
@forN
    %iv =l loadl %i
    %c =w csgel %iv, %len           # i >= len ?
    jnz %c, @afterN, @forbodyN
@forbodyN
    %off =l mul %iv, <esize>        # i * sizeof(elem)
    %addr =l add %slice, %off       # slice + i*esize  ← XXX wait, this is wrong
    %el =<base> <load> %addr        # load element
    <body with `x` bound to %el>
    %iv2 =l add %iv, 1
    storel %iv2, %i
    jmp @forN
@afterN
```

Note the address calculation at line 453: `add iterable, off` —
where `iterable` is the **slice slot pointer** (pointing at
`{ptr, len}`). To get the element address, you actually want
`*(slice.ptr) + off`, not `slice.addr + off`. **This looks like a
bug** but the emitted IR works for our test cases — possibly because
the test cases use stack-allocated arrays where the slice slot's
data pointer happens to alias the slot? Worth investigating.

The non-slice case (lines 466-474) is a placeholder — emits a
single-pass execution. The proper lowering through `__iter__` /
`__next__` is deferred to v2.

## Expression dispatch (lines 478-540)

One big `if isinstance` chain. **Each handler returns the string
representation** of the result value: a literal like `"42"`, or a
temp like `"%_v3"`. Sometimes constants stay as constants; often we
emit a `<tmp> =<base> <op> ...` line and return the tmp name.

This stringy return type is unusual but pragmatic — we don't need a
separate "QBE value" type, and emit-and-build is interleaved
naturally.

## Name lookup (lines 544-552)

```python
def _emit_name(self, expr, ctx):
    lv = ctx.locals.get(expr.name)
    if lv is None: return f"${expr.name}"             # global symbol
    if lv.is_var:
        tmp = ctx.tmp(expr.name)
        ctx.emit(f"{tmp} ={_base(lv.ty)} {_load(lv.ty)} {lv.loc}")
        return tmp
    return lv.loc                                       # const local — just the value
```

The `$<name>` fallback is **how the codegen calls undeclared symbols**
like `puts` or our runtime's `println`. There's no real "extern"
support — we just emit a global reference and let the linker resolve
it. That's also why `io.println` works: we lower `obj.field` to
`${field}` when `obj` is a module.

## Binary operators (lines 556-586)

The comparison table:

```python
cmp = {
    "==": f"ceq{lb}", "!=": f"cne{lb}",
    "<":  f"cslt{lb}" if lb in ("w", "l") else f"clt{lb}",
    "<=": f"csle{lb}" if lb in ("w", "l") else f"cle{lb}",
    ">":  f"csgt{lb}" if lb in ("w", "l") else f"cgt{lb}",
    ">=": f"csge{lb}" if lb in ("w", "l") else f"cge{lb}",
}
```

Note the signed/unsigned/float split: `cslt` (signed less-than) for
integers, `clt` for floats. We default to **signed** comparisons for
integers — there's no `unsigned int` type marker yet, so even `u32`
gets signed comparisons. Tightening to use `cult` etc. for unsigned
types is a known issue.

The arithmetic table:

```python
arith = {"+": "add", "-": "sub", "*": "mul", "/": "div",
         "%": "rem", "&": "and", "|": "or", "^": "xor",
         "<<": "shl", ">>": "sar"}
```

`>>` maps to `sar` (signed arithmetic shift right). Unsigned shift
right would use `shr`. Same signedness gotcha.

## Field access and `_gep` (lines 603-629)

```python
def _gep(self, ptr, offset, ctx):
    tmp = ctx.tmp("gep")
    ctx.emit(f"{tmp} =l add {ptr}, {offset}")
    return tmp

def _emit_field(self, expr, ctx):
    obj = self._emit_expr(expr.obj, ctx)
    base_ty = _unwrap(self._ty(expr.obj))

    if isinstance(base_ty, StructTy):
        lay = self._layouts.get(base_ty.name)
        if lay:
            fl = next((f for f in lay.fields if f.name == expr.field), None)
            if fl:
                src = obj if fl.offset == 0 else self._gep(obj, fl.offset, ctx)
                tmp = ctx.tmp("fv")
                ctx.emit(f"{tmp} ={_base(fl.ty)} {_load(fl.ty)} {src}")
                return tmp

    if isinstance(base_ty, SliceTy) and expr.field in ("len", "length"):
        lp = ctx.tmp("lp")
        lv = ctx.tmp("len")
        ctx.emit(f"{lp} =l add {obj}, 8")
        ctx.emit(f"{lv} =l loadl {lp}")
        return lv

    return "0"
```

`_gep` is the "compute address of field" primitive. The "GEP" name
borrows from LLVM's `getelementptr` instruction, which serves the
same purpose.

For struct fields: compute address (or use the obj directly if the
field is at offset 0), emit a `load` of the field's QBE base type.

For slice `.len`: special case at offset 8 (the length lives there
in the fat-pointer layout). `.ptr` (data pointer at offset 0) isn't
implemented — note for v2.

For unknown: return `"0"` as a placeholder. This lets compilation
continue but produces wrong code; ideally we'd emit a Codegen error.

## Calls (lines 651-702) — the routing logic

```python
def _emit_call(self, expr, ctx):
    implicit_self = None
    callee = ""

    c = expr.callee
    if isinstance(c, FieldAccess):
        obj_val = self._emit_expr(c.obj, ctx)
        obj_ty = _unwrap(self._ty(c.obj))
        if isinstance(obj_ty, StructTy):
            callee = f"${obj_ty.name}__{c.field}"
            implicit_self = obj_val
        else:
            # module.fn or unknown
            callee = f"${c.field}"
    elif isinstance(c, Name):
        callee = f"${c.name}"
    elif isinstance(c, GenericInstantiation):
        # e.g. LinkedList[i32].new()
        inner = c.base
        if isinstance(inner, FieldAccess):
            obj_ty = _unwrap(self._ty(inner.obj))
            if isinstance(obj_ty, StructTy):
                callee = f"${obj_ty.name}__{inner.field}"
            else:
                callee = "$unknown"
        elif isinstance(inner, Name):
            callee = f"${inner.name}"
        else:
            callee = "$unknown"
    else:
        callee = self._emit_expr(c, ctx)

    args = []
    if implicit_self is not None: args.append(f"l {implicit_self}")
    for arg in expr.args:
        av = self._emit_expr(arg.value, ctx)
        at = self._ty(arg.value)
        args.append(f"{_base(at)} {av}")

    ret_ty = self._ty(expr)
    args_str = ", ".join(args)

    if isinstance(ret_ty, (UnitTy, UnknownTy)):
        ctx.emit(f"call {callee}({args_str})")
        return "0"
    rb = _base(ret_ty)
    tmp = ctx.tmp("r")
    ctx.emit(f"{tmp} ={rb} call {callee}({args_str})")
    return tmp
```

Three callee shapes:

1. **`FieldAccess` callee** — `obj.method(...)`:
   - If `obj_ty` is a `StructTy`: `callee = $<StructName>__<method>`,
     pass `obj_val` as implicit first arg.
   - Otherwise (`ModuleTy` etc.): `callee = $<method>`. **Module
     name is dropped.** This is how `io.println(...)` becomes
     `call $println(...)` — and the runtime defines `println`.

2. **`Name` callee** — `f(...)`. `callee = $<name>`. If `f` is a
   generic free fn (`_generic_fns[name]`), the substitution is inferred
   by unifying parameter types against arg types, the spec is
   registered in `_fn_specs`, and the callee becomes `$<name>_<args>`.

3. **`GenericInstantiation` callee** —
   - `LinkedList[i32].new()` — strip the generic, use the inner
     `FieldAccess`. Specialization is driven by the struct's layout.
   - `id[i32](42)` — explicit generic-fn call. Type args come straight
     from `c.type_args`; spec is registered and routed the same as
     the inferred case.

After determining the callee, args are emitted left-to-right with
their type's base (`_base(self._ty(arg.value))`). If the function
returns `UnitTy` or `UnknownTy`, emit `call` with no destination.
Otherwise emit `<tmp> =<rb> call ...`.

**Argument types are the expression's type, not the parameter's.**
This is a known looseness — see [code review notes](../README.md).

## Struct literals (lines 706-735)

```python
def _emit_struct_lit(self, expr, ctx):
    ...                                    # determine struct name
    lay = self._layouts.get(sname)
    if lay is None: return "0"

    total = _ARC_HEADER + lay.total
    block = ctx.tmp("blk")
    ptr = ctx.tmp("ptr")
    ctx.emit(f"{block} =l call $malloc(l {total})")
    ctx.emit(f"storel 1, {block}")                          # refcount = 1
    ds = ctx.tmp("ds")
    ctx.emit(f"{ds} =l add {block}, 8")                     # drop slot
    ctx.emit(f"storel 0, {ds}")                             # drop_fn = 0
    ctx.emit(f"{ptr} =l add {block}, {_ARC_HEADER}")        # user pointer

    fvals = {fi.name: self._emit_expr(fi.value, ctx) for fi in expr.fields}
    for fl in lay.fields:
        v = fvals.get(fl.name, "0")
        dest = ptr if fl.offset == 0 else self._gep(ptr, fl.offset, ctx)
        ctx.emit(f"{_store(fl.ty)} {v}, {dest}")

    return ptr
```

`Foo { x: 1, y: 2 }` lowers to:

```
%_blk =l call $malloc(l 32)        # 16 (header) + 16 (struct)
storel 1, %_blk                    # refcount = 1
%_ds =l add %_blk, 8               # drop slot
storel 0, %_ds                     # drop_fn = 0 (null for now)
%_ptr =l add %_blk, 16             # user pointer past header
storel 1, %_ptr                    # x at offset 0
%_gep0 =l add %_ptr, 8             # y at offset 8
storel 2, %_gep0
```

Returns `%_ptr` — what every other operation expects.

**This is where ARC headers are emitted via `arc_alloc`.** Releases
are inserted before every `ret` (see `_emit_return` and
`_emit_releases`). The `drop_fn` is the struct's `__drop__` symbol if
defined, or 0 otherwise.

## String literals (lines 739-749) — fat pointer on the stack

```python
def _emit_string_lit(self, expr, ctx):
    ptr_name = self._intern_str(expr.value)
    length = len(expr.value)
    slot = ctx.tmp("sl")
    ctx.emit(f"{slot} =l alloc8 16")               # 16 bytes for {ptr, len}
    ctx.emit(f"storel {ptr_name}, {slot}")          # data pointer at offset 0
    lp = ctx.tmp("lp")
    ctx.emit(f"{lp} =l add {slot}, 8")              # len pointer
    ctx.emit(f"storel {length}, {lp}")              # length at offset 8
    return slot
```

Returns the **slice slot pointer**. The fat pointer is allocated on
the stack; its data pointer points into the data section. This is
what the runtime's `println(const ouro_slice_u8* s)` consumes — the C
signature takes a pointer to `{ptr, len}`, exactly matching this
layout.

## `if` and `match` as expressions

These produce a **stack-allocated result slot**. Each branch stores
its tail value to the slot via `_emit_block_yielding` (lines
832-857). After the construct, we load from the slot.

### `_emit_if` (lines 753-790)

```python
def _emit_if(self, expr, ctx):
    cond = self._emit_expr(expr.condition, ctx)
    then_lbl = ctx.lbl("then")
    else_lbl = ctx.lbl("else")
    after_lbl = ctx.lbl("endif")

    result_slot = ctx.tmp("ires")
    ctx.emit(f"{result_slot} =l alloc8 8")
    ctx.emit(f"storel 0, {result_slot}")

    ctx.emit(f"jnz {cond}, {then_lbl}, {else_lbl}")

    ctx.label(then_lbl)
    self._emit_block_yielding(expr.then_block, result_slot, ctx)
    if not last_is_terminator: ctx.emit(f"jmp {after_lbl}")

    ctx.label(else_lbl)
    if expr.else_block: self._emit_block_yielding(expr.else_block, result_slot, ctx)
    if not last_is_terminator: ctx.emit(f"jmp {after_lbl}")

    ctx.label(after_lbl)
    result = ctx.tmp("iv")
    ctx.emit(f"{result} =l loadl {result_slot}")
    return result
```

### `_emit_match` (lines 794-830)

Match dispatch uses **check labels**: each arm has a `@chk<i>`
label where the comparison happens, and an `@arm<i>` label where
the body lives.

```
    jmp @chk0
@chk0
    %_mc0 =w ceqw %scrut, <pat0>
    jnz %_mc0, @arm0, @chk1
@chk1
    %_mc1 =w ceqw %scrut, <pat1>
    jnz %_mc1, @arm1, @chk2
@chk2  (wildcard — jmp unconditionally)
    jmp @arm2
@arm0
    <body 0; storel result, slot>
    jmp @matchend
@arm1
    <body 1; storel result, slot>
    jmp @matchend
@arm2
    <body 2; storel result, slot>
    jmp @matchend
@matchend
    %mv =l loadl %result_slot
```

The check labels are necessary because **QBE requires a label after
every conditional jump**. Without them, the IR would have `jnz`
followed directly by `ceq`, which QBE rejects.

### `_emit_block_yielding` (lines 832-857) — the bug-fix function

```python
def _emit_block_yielding(self, block, slot, ctx):
    stmts = block.statements
    if not stmts: return
    for s in stmts[:-1]: self._emit_stmt(s, ctx)
    last = stmts[-1]
    if isinstance(last, ExprStatement):
        v = self._emit_expr(last.expr, ctx)
        if v.startswith("%") and _base(self._ty(last.expr)) == "w":
            ext = ctx.tmp("ext")
            ctx.emit(f"{ext} =l extsw {v}")
            v = ext
        ctx.emit(f"storel {v}, {slot}")
    else:
        self._emit_stmt(last, ctx)
```

The result slot is `l`-wide (8 bytes). Storing a `w`-typed temp via
`storel` is rejected by QBE. The fix: **sign-extend `w` temps to `l`
before storing**. Constants and `l`-typed temps store directly.

Floats (`s`/`d` types) aren't handled — they'd silently miscompile.
The yielding-block helper assumes integer/pointer arms. **Documented
gap.**

## What this pass does NOT do

- **No optimization passes.** QBE itself does the SSA → asm work; we
  just emit the IR.
- **No alias analysis, no escape analysis, no constant folding.** QBE
  folds constants at the IR level.
- **No call-site type coercion.** Args are emitted with the
  *expression's* base type, not the parameter's.
- **Generics are monomorphized**, not erased. Generic structs and
  free functions are emitted as specialized copies per concrete
  type-arg tuple (`Box_i32`, `id_i32`).
- **Bitwise `&`, `^`, `<<`, `>>` aren't lexed yet** — only `|` (which
  doubles as type-union) is recognized at all layers.
- **No `weak[T]` upgrade/downgrade.** The reads return `T | Null`
  via the type checker's narrowing, but the codegen for the actual
  null check isn't written.
- **Payload extraction is wired** for narrowed union references
  (`_emit_name` checks whether the stored type is a UnionTy but the
  recorded type was narrowed, and if so loads payload at offset 8 of
  the box). Slice `.ptr` field is also exposed alongside `.len`.

## Cross-references

- The AST nodes ([`src/nodes.py`](../src/nodes.py)) are the input
  vocabulary. See [nodes.md](nodes.md).
- The resolver ([`src/resolver.py`](../src/resolver.py)) is consumed
  via the `ResolutionMap`. See [resolver.md](resolver.md).
- The type checker ([`src/typechecker.py`](../src/typechecker.py))
  is consumed via the `TypeMap`. See [typechecker.md](typechecker.md).
- The runtime is split between Ouro (`runtime/_start.ou`,
  `runtime/syscalls.ou`, `runtime/io.ou`) and a small C layer
  ([`runtime/runtime.c`](../runtime/runtime.c)) for the ARC primitives,
  the mmap-backed allocator, and `printf` (varargs aren't expressible
  in Ouro yet).  `io.printf` is a variadic intrinsic in the codegen —
  it lowers directly to `$printf` (see `_emit_printf_call`).

## Related tests

[`test/test_codegen.py`](../test/test_codegen.py) — 45 unit tests
checking the IR text contains the right opcodes for each construct.
[`test/test_e2e.py`](../test/test_e2e.py) — 16 end-to-end tests that
actually compile through `qbe + cc` and run, asserting on exit code
or stdout.

The e2e tests are what give confidence the codegen produces *correct*
IR, not just text-shaped output.
