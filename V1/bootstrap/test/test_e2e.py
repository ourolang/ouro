"""End-to-end build tests using inline source strings.

For file-based tests, see ``test_ouro.py`` which discovers ``.ou``
files in ``test/ouro/``.

Shared compile/run helpers and the runtime fixture live in
``conftest.py``.
"""

from pathlib import Path

from .conftest import (
    REPO_ROOT,
    capture_exe as _capture,
    compile_source as _compile_source,
    requires_toolchain,
    run_exe as _run,
)


@requires_toolchain
def test_e2e_return_zero(tmp_path: Path):
    src = "fn main() -> i32:\n    return 0\n"
    assert _run(_compile_source(src, "ret0", tmp_path)) == 0


@requires_toolchain
def test_e2e_return_constant(tmp_path: Path):
    src = "fn main() -> i32:\n    return 42\n"
    assert _run(_compile_source(src, "ret42", tmp_path)) == 42


@requires_toolchain
def test_e2e_arithmetic(tmp_path: Path):
    src = "fn main() -> i32:\n    return (2 + 3) * 7\n"
    assert _run(_compile_source(src, "arith", tmp_path)) == 35


@requires_toolchain
def test_e2e_function_call(tmp_path: Path):
    src = (
        "fn add(a: i32, b: i32) -> i32:\n"
        "    return a + b\n"
        "\n"
        "fn main() -> i32:\n"
        "    return add(20, 22)\n"
    )
    assert _run(_compile_source(src, "call", tmp_path)) == 42


@requires_toolchain
def test_e2e_if_else(tmp_path: Path):
    src = (
        "fn pick(x: i32) -> i32:\n"
        "    if x > 0:\n"
        "        return 1\n"
        "    else:\n"
        "        return 2\n"
        "\n"
        "fn main() -> i32:\n"
        "    return pick(10)\n"
    )
    assert _run(_compile_source(src, "ifelse", tmp_path)) == 1


@requires_toolchain
def test_e2e_var_loop(tmp_path: Path):
    src = (
        "fn main() -> i32:\n"
        "    n: var[i32] = 0\n"
        "    loop:\n"
        "        if n >= 10:\n"
        "            break\n"
        "        n = n + 1\n"
        "    return n\n"
    )
    assert _run(_compile_source(src, "loop", tmp_path)) == 10


@requires_toolchain
def test_e2e_if_as_expression(tmp_path: Path):
    """`return if cond: a else: b` yields a value through the if-result slot."""
    src = (
        "fn pick(x: i32) -> i32:\n"
        "    return if x > 0:\n"
        "        100\n"
        "    else:\n"
        "        200\n"
        "\n"
        "fn main() -> i32:\n"
        "    return pick(5)\n"
    )
    assert _run(_compile_source(src, "ifexpr", tmp_path)) == 100


@requires_toolchain
def test_e2e_if_as_expression_else_branch(tmp_path: Path):
    src = (
        "fn pick(x: i32) -> i32:\n"
        "    return if x > 0:\n"
        "        100\n"
        "    else:\n"
        "        200\n"
        "\n"
        "fn main() -> i32:\n"
        "    return pick(0)\n"
    )
    assert _run(_compile_source(src, "ifexprelse", tmp_path)) == 200


@requires_toolchain
def test_e2e_if_yields_computed_value(tmp_path: Path):
    """if-as-expression with a computed (w-typed) arm value must widen
    to l before storing into the result slot.  Regression: prior to fix,
    'storel %wtmp' was rejected by QBE."""
    src = (
        "fn pick(x: i32) -> i32:\n"
        "    return if x > 0:\n"
        "        x + 100\n"
        "    else:\n"
        "        x - 100\n"
        "\n"
        "fn main() -> i32:\n"
        "    return pick(5)\n"
    )
    assert _run(_compile_source(src, "ifcomp", tmp_path)) == 105


@requires_toolchain
def test_e2e_match_yields_computed_value(tmp_path: Path):
    """Same widen-to-l requirement for match arms with computed values."""
    src = (
        "fn pick(x: i32) -> i32:\n"
        "    return match x:\n"
        "        0:\n"
        "            x + 1\n"
        "        1:\n"
        "            x + 10\n"
        "        _:\n"
        "            x + 100\n"
        "\n"
        "fn main() -> i32:\n"
        "    return pick(1)\n"
    )
    assert _run(_compile_source(src, "matchcomp", tmp_path)) == 11


@requires_toolchain
def test_e2e_match(tmp_path: Path):
    src = (
        "fn classify(x: i32) -> i32:\n"
        "    return match x:\n"
        "        1: 100\n"
        "        2: 200\n"
        "        _: 0\n"
        "\n"
        "fn main() -> i32:\n"
        "    return classify(2)\n"
    )
    assert _run(_compile_source(src, "match", tmp_path)) == 200


# ── Runtime / stdlib tests ────────────────────────────────────────────────────


@requires_toolchain
def test_e2e_println(tmp_path: Path):
    src = (
        'io = import("std/io")\n'
        "\n"
        "fn main() -> i32:\n"
        '    io.println("hello, ouro!")\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "println", tmp_path)
    assert _capture(exe) == "hello, ouro!\n"


@requires_toolchain
def test_e2e_print_no_newline(tmp_path: Path):
    src = (
        'io = import("std/io")\n'
        "\n"
        "fn main() -> i32:\n"
        '    io.print("ab")\n'
        '    io.print("cd")\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "print", tmp_path)
    assert _capture(exe) == "abcd"


@requires_toolchain
def test_e2e_printf_int(tmp_path: Path):
    src = (
        'io = import("std/io")\n'
        "\n"
        "fn main() -> i32:\n"
        '    io.printf("%ld\\n", 2 + 2 * 20)\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "printfint", tmp_path)
    assert _capture(exe) == "42\n"


@requires_toolchain
def test_e2e_printf_multiple_args(tmp_path: Path):
    src = (
        'io = import("std/io")\n'
        "\n"
        "fn main() -> i32:\n"
        '    io.printf("%ld + %ld = %ld\\n", 1, 2, 3)\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "printfmulti", tmp_path)
    assert _capture(exe) == "1 + 2 = 3\n"


@requires_toolchain
def test_e2e_printf_no_args(tmp_path: Path):
    src = (
        'io = import("std/io")\n'
        "\n"
        "fn main() -> i32:\n"
        '    io.printf("plain text\\n")\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "printfplain", tmp_path)
    assert _capture(exe) == "plain text\n"


@requires_toolchain
def test_e2e_factorial_example(tmp_path: Path):
    """The shipped examples/05_factorial.ou prints factorials 1..7."""
    src = (REPO_ROOT / "examples/05_factorial.ou").read_text()
    exe = _compile_source(src, "factorial", tmp_path)
    assert _capture(exe) == (
        "1! = 1\n"
        "2! = 2\n"
        "3! = 6\n"
        "4! = 24\n"
        "5! = 120\n"
        "6! = 720\n"
        "7! = 5040\n"
    )


@requires_toolchain
def test_e2e_arc_drop_runs_at_scope_end(tmp_path: Path):
    """A struct allocated then unused should have __drop__ called when its
    binding's scope ends.  Verify by observing __drop__'s side effect."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct Resource:\n"
        "    id: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.println("dropped")\n'
        "\n"
        "fn main() -> i32:\n"
        "    r = Resource { id: 42 }\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "arcdrop", tmp_path)
    assert _capture(exe) == "dropped\n"


@requires_toolchain
def test_e2e_arc_returned_struct_drops_in_caller(
    tmp_path: Path
):
    """Returning a struct via `rc[Box]` transfers ownership through the
    refcounted heap path; only the caller's release should drop it
    (single drop, not double).  Stack-by-default would copy at the
    return boundary and drop twice — see e2e_stack_returned_struct."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct Box:\n"
        "    val: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.println("dropped")\n'
        "\n"
        "fn make_box() -> rc[Box]:\n"
        "    b: rc[Box] = Box { val: 7 }\n"
        "    return b\n"
        "\n"
        "fn main() -> i32:\n"
        "    x: rc[Box] = make_box()\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "arcreturn", tmp_path)
    # __drop__ should print exactly once (when main's `x` releases).
    assert _capture(exe) == "dropped\n"


@requires_toolchain
def test_e2e_stack_returned_struct_double_drops(
    tmp_path: Path
):
    """Returning a bare struct uses value semantics: the callee's local
    drops at scope exit, and the caller's binding drops at scope exit
    — two distinct values, two __drop__s.  Contrast with the rc[Box]
    test above, where the return-by-pointer path drops exactly once."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct Box:\n"
        "    val: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.println("dropped")\n'
        "\n"
        "fn make_box() -> Box:\n"
        "    b = Box { val: 7 }\n"  # stack local in make_box
        "    return b\n"             # aggregate return copies bytes
        "\n"
        "fn main() -> i32:\n"
        "    x = make_box()\n"       # x is caller-side slot
        "    return 0\n"
    )
    exe = _compile_source(src, "stackreturn", tmp_path)
    assert _capture(exe) == "dropped\ndropped\n"


@requires_toolchain
def test_e2e_var_struct_reassign_drops_old(
    tmp_path: Path
):
    """A `var[Struct]` slot holds the struct's bytes inline; each
    reassignment fires the slot's `__drop__` on the *outgoing* value
    before writing the new one in place.  Slot's final value drops
    at scope exit, for a total of one drop per write."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct R:\n"
        "    id: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.printf("drop %ld\\n", self.id)\n'
        "\n"
        "fn main() -> i32:\n"
        "    r: var[R] = R { id: 1 }\n"
        "    r = R { id: 2 }\n"
        "    r = R { id: 3 }\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "varreassign", tmp_path)
    # 1 dropped on reassign to 2, 2 dropped on reassign to 3, 3 dropped at scope exit.
    assert _capture(exe) == "drop 1\ndrop 2\ndrop 3\n"


@requires_toolchain
def test_e2e_inline_struct_field_drop_chains(
    tmp_path: Path
):
    """An inline (bare-struct) field's `__drop__` is invoked by the
    outer's auto-generated drop wrapper.  Outer's user `__drop__`
    runs first, then the inline field's drop chain — same order as
    rc/arc field releases."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct Inner:\n"
        "    v: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.printf("inner %ld\\n", self.v)\n'
        "\n"
        "struct Outer:\n"
        "    a: i32\n"
        "    b: Inner\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.printf("outer %ld\\n", self.a)\n'
        "\n"
        "fn main() -> i32:\n"
        "    o = Outer { a: 1, b: Inner { v: 2 } }\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "inlinedrop", tmp_path)
    # Outer's user __drop__ runs first (it can still see fields),
    # then the inline Inner field's __drop__.
    assert _capture(exe) == "outer 1\ninner 2\n"


@requires_toolchain
def test_e2e_stack_arg_drops_callee_copy(
    tmp_path: Path
):
    """Passing a bare struct as an argument copies by value via QBE's
    `:Foo` aggregate ABI.  The callee's copy and the caller's binding
    each run __drop__ at their own scope exit — two distinct prints,
    callee first."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct Box:\n"
        "    val: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.printf("drop %ld\\n", self.val)\n'
        "\n"
        "fn take(b: Box):\n"
        "    pass\n"
        "\n"
        "fn main() -> i32:\n"
        "    b = Box { val: 1 }\n"
        "    take(b)\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "stackarg", tmp_path)
    assert _capture(exe) == "drop 1\ndrop 1\n"


@requires_toolchain
def test_e2e_arc_two_local_structs(tmp_path: Path):
    """Multiple local managed values should each be released (two drops)."""
    src = (
        'io = import("std/io")\n'
        "\n"
        "struct R:\n"
        "    id: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.println("d")\n'
        "\n"
        "fn main() -> i32:\n"
        "    a = R { id: 1 }\n"
        "    b = R { id: 2 }\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "arctwo", tmp_path)
    assert _capture(exe) == "d\nd\n"


@requires_toolchain
def test_e2e_cast_numeric(tmp_path: Path):
    """`x as T` for numeric ↔ numeric.  Round-trips a small set
    of widening / narrowing / int-float conversions."""
    src = (
        "fn main() -> i32:\n"
        "    a: i32 = 7\n"
        "    b = a as i64\n"
        "    if b != 7:\n"
        "        return 1\n"
        "    c = b as i32\n"
        "    if c != 7:\n"
        "        return 2\n"
        "    big: i64 = 0x1_0000_0007\n"
        "    low = big as i32\n"
        "    if low != 7:\n"
        "        return 3\n"
        "    n: u8 = 200\n"
        "    wide = n as u64\n"
        "    if wide != 200:\n"
        "        return 4\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "castnum", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_embed_file(tmp_path: Path):
    """`embed("path")` reads the file at compile time and returns
    a `[]u8` baked into the data section.  Verifies the bytes are
    accessible at runtime via slice indexing + length."""
    target = tmp_path / "greeting.txt"
    target.write_bytes(b"hi!")
    quoted = str(target).replace("\\", "\\\\").replace('"', '\\"')

    src = (
        "fn main() -> i32:\n"
        f'    msg = embed("{quoted}")\n'
        "    if msg.len != 3:\n"
        "        return 1\n"
        "    if msg[0] != 104:\n"   # 'h'
        "        return 2\n"
        "    if msg[1] != 105:\n"   # 'i'
        "        return 3\n"
        "    if msg[2] != 33:\n"    # '!'
        "        return 4\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "embed", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_fn_pointers(tmp_path: Path):
    """First-class function values.  Pass a fn as an arg, store it
    in a `var` slot, reassign, call indirectly.  Exercises the
    `FnTy` plumbing through parser, typechecker, and codegen, plus
    QBE's register-callee form of `call`."""
    src = (
        "fn add(a: i32, b: i32) -> i32:\n"
        "    return a + b\n"
        "\n"
        "fn sub(a: i32, b: i32) -> i32:\n"
        "    return a - b\n"
        "\n"
        "fn apply(f: fn(i32, i32) -> i32, x: i32, y: i32) -> i32:\n"
        "    return f(x, y)\n"
        "\n"
        "fn main() -> i32:\n"
        "    if apply(add, 10, 5) != 15:\n"
        "        return 1\n"
        "    if apply(sub, 10, 5) != 5:\n"
        "        return 2\n"
        "    op: var[fn(i32, i32) -> i32] = add\n"
        "    if op(3, 4) != 7:\n"
        "        return 3\n"
        "    op = sub\n"
        "    if op(10, 3) != 7:\n"
        "        return 4\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "fnptr", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_vec_managed_drop_chain(tmp_path: Path):
    """Vec[rc[R]] runs each element's `__drop__` when the Vec
    drops.  Pushes three `rc[R]` values, expects exactly three
    drop messages at scope exit — meaning `drop_at[T]` released
    each element and the underlying R's drop fired."""
    src = (
        'io = import("std/io")\n'
        'vec = import("std/vec")\n'
        "\n"
        "struct R:\n"
        "    id: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        '        io.printf("drop %ld\\n", self.id)\n'
        "\n"
        "fn main() -> i32:\n"
        "    v: rc[vec.Vec[rc[R]]] = vec.Vec[rc[R]].new()\n"
        "    a: rc[R] = R { id: 1 }\n"
        "    b: rc[R] = R { id: 2 }\n"
        "    c: rc[R] = R { id: 3 }\n"
        "    v.push(a)\n"
        "    v.push(b)\n"
        "    v.push(c)\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "vecmgmt", tmp_path)
    # Each of a/b/c holds one ref; each push bumps the count via
    # mem_store; the Vec's drop calls drop_at on each element
    # (refcount goes back to 1); the caller bindings then release
    # → drop fires once per id.
    assert _capture(exe) == "drop 1\ndrop 2\ndrop 3\n"


@requires_toolchain
def test_e2e_map_string_keys(tmp_path: Path):
    """`std/map.HashMap[[]u8, V]` round-trips put/get for >cap-many
    entries, forcing at least one rehash.  Exercises the comptime
    `hash[[]u8]` / `eq[[]u8]` intrinsics and the linear-probe path."""
    src = (
        'map = import("std/map")\n'
        "\n"
        "fn main() -> i32:\n"
        "    m: rc[map.HashMap[[]u8, i64]] = map.HashMap[[]u8, i64].new()\n"
        '    m.put("alpha", 1)\n'
        '    m.put("beta", 2)\n'
        '    m.put("gamma", 3)\n'
        '    m.put("delta", 4)\n'
        '    m.put("epsilon", 5)\n'
        '    m.put("zeta", 6)\n'
        '    m.put("eta", 7)\n'
        '    m.put("theta", 8)\n'
        '    m.put("iota", 9)\n'
        '    m.put("kappa", 10)\n'
        "    if m.len() != 10:\n"
        "        return 1\n"
        '    a = m.get("alpha")\n'
        "    if a ?= Null:\n"
        "        return 2\n"
        "    elif a != 1:\n"
        "        return 3\n"
        '    g = m.get("gamma")\n'
        "    if g ?= Null:\n"
        "        return 4\n"
        "    elif g != 3:\n"
        "        return 5\n"
        '    k = m.get("kappa")\n'
        "    if k ?= Null:\n"
        "        return 6\n"
        "    elif k != 10:\n"
        "        return 7\n"
        '    if not (m.get("missing") ?= Null):\n'
        "        return 8\n"
        '    m.put("alpha", 99)\n'   # overwrite
        "    if m.len() != 10:\n"
        "        return 9\n"
        '    a2 = m.get("alpha")\n'
        "    if a2 ?= Null:\n"
        "        return 10\n"
        "    elif a2 != 99:\n"
        "        return 11\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "mapstr", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_fs_read_file(tmp_path: Path):
    """`std/fs.read_file(path)` round-trips a file's contents into
    a `Bytes`.  Verifies the open/lseek/read/close syscall chain,
    the `Bytes.from_buffer` constructor, and the `IoError` arm
    for a missing path."""
    target = tmp_path / "hello.txt"
    target.write_bytes(b"abc")
    quoted = str(target).replace("\\", "\\\\").replace('"', '\\"')

    src = (
        'fs = import("std/fs")\n'
        "\n"
        "fn main() -> i32:\n"
        f'    r = fs.read_file("{quoted}".ptr)\n'
        "    if r ?= fs.IoError:\n"
        "        return 1\n"
        "    else:\n"
        "        if r.len() != 3:\n"
        "            return 2\n"
        "        g = r.get(0)\n"
        "        if g ?= Null:\n"
        "            return 3\n"
        "        elif g != 97:\n"   # 'a'
        "            return 4\n"
        "    bad = fs.read_file(\"/nope/does/not/exist\".ptr)\n"
        "    if not (bad ?= fs.IoError):\n"
        "        return 5\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "fsread", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_vec_i32_push_get(tmp_path: Path):
    """Generic `Vec[T]` end-to-end with i32 elements.  Each push
    grows the buffer geometrically; reads round-trip the stored
    values exactly.  Exercises `sizeof[T]()` + `mem_load[T]` /
    `mem_store[T]` intrinsics at offset math, not just byte-level
    access."""
    src = (
        'vec = import("std/vec")\n'
        "\n"
        "fn main() -> i32:\n"
        "    v: rc[vec.Vec[i32]] = vec.Vec[i32].new()\n"
        "    i: var[i32] = 0\n"
        "    while i < 30:\n"
        "        v.push(i * 10)\n"
        "        i = i + 1\n"
        "    if v.len() != 30:\n"
        "        return 1\n"
        "    j: var[i32] = 0\n"
        "    while j < 30:\n"
        "        e = v.get(j)\n"
        "        if e ?= Null:\n"
        "            return 2\n"
        "        elif e != j * 10:\n"
        "            return 3\n"
        "        j = j + 1\n"
        "    if not (v.get(30) ?= Null):\n"
        "        return 4\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "veci32", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_bytes_push_grow(tmp_path: Path):
    """`std/bytes` end-to-end.  Verifies the heap allocator
    (`mem_alloc` / `mem_free`) is reachable from Ouro, the
    grow-on-push cycle copies existing bytes correctly through
    `mem_copy`, and `__drop__` frees the buffer without
    crashing.  Pushes 1..40 to force at least three doublings
    (cap 8 → 16 → 32 → 64); reads every value back."""
    src = (
        'bytes = import("std/bytes")\n'
        "\n"
        "fn main() -> i32:\n"
        "    b: rc[bytes.Bytes] = bytes.Bytes.new()\n"
        "    i: var[i32] = 1\n"
        "    while i <= 40:\n"
        "        b.push(i)\n"
        "        i = i + 1\n"
        "    if b.len() != 40:\n"
        "        return 1\n"
        "    j: var[i32] = 0\n"
        "    while j < 40:\n"
        "        v = b.get(j)\n"
        "        if v ?= Null:\n"
        "            return 2\n"
        "        elif v != j + 1:\n"
        "            return 3\n"
        "        j = j + 1\n"
        "    if not (b.get(40) ?= Null):\n"
        "        return 4\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "bytespush", tmp_path)
    assert _run(exe) == 0


@requires_toolchain
def test_e2e_extern_variadic_printf(tmp_path: Path):
    """`extern printf(fmt: ptr[u8], ...) -> i32` lets user code call
    the C-runtime printf as a normal function — no codegen intrinsic
    needed.  The variadic call ABI inserts the QBE `...` separator
    between the fixed `fmt` arg and the rest.
    """
    src = (
        'extern printf(fmt: ptr[u8], ...) -> i32\n'
        "\n"
        "fn main() -> i32:\n"
        '    printf("plain text\\n".ptr)\n'
        '    printf("answer: %ld\\n".ptr, 42)\n'
        '    printf("%ld + %ld = %ld\\n".ptr, 1, 2, 3)\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "externprintf", tmp_path)
    assert _capture(exe) == "plain text\nanswer: 42\n1 + 2 = 3\n"


@requires_toolchain
def test_e2e_assert_failure_exits_with_one(tmp_path: Path):
    """A failing `assert.assert(...)` writes its message to stderr
    and exits with status 1.  The test_ouro harness only checks
    exit-code 0 or matching stdout, so the exit-1 case lives here
    as an explicit `_run`-and-compare-rc check."""
    src = (
        'assert = import("std/assert")\n'
        "\n"
        "fn main() -> i32:\n"
        '    assert.assert(1 + 1 == 3, "arithmetic glitch")\n'
        "    return 0\n"
    )
    exe = _compile_source(src, "assertfail", tmp_path)
    assert _run(exe) == 1


@requires_toolchain
def test_e2e_println_in_loop(tmp_path: Path):
    src = (
        'io = import("std/io")\n'
        "\n"
        "fn main() -> i32:\n"
        "    n: var[i32] = 0\n"
        "    loop:\n"
        "        if n >= 3:\n"
        "            break\n"
        '        io.println("tick")\n'
        "        n = n + 1\n"
        "    return 0\n"
    )
    exe = _compile_source(src, "loopprint", tmp_path)
    assert _capture(exe) == "tick\ntick\ntick\n"
