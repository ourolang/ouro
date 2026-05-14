"""Code generation tests.

Each test compiles a small Ouro snippet and checks that the emitted QBE IR
contains the expected instructions or patterns.  We don't execute the IR —
just verify the structural correctness of the output.
"""


from src.lexer import lex
from src.parser import parse
from src.resolver import resolve
from src.typechecker import typecheck
from src.codegen import generate


def compile(source: str) -> str:
    """Lex → parse → resolve → typecheck → generate; return QBE IR text.
    (Discards the asm sidecar — these tests assert only on QBE IR.)
    """
    tokens = lex(source)
    tree = parse(tokens, "<test>")
    res = resolve(tree)
    types = typecheck(tree, res)
    ir, _asm = generate(tree, types, res)
    return ir


def has(ir: str, pattern: str) -> bool:
    """True if *pattern* appears as a substring of *ir*."""
    return pattern in ir


def lines(ir: str) -> list[str]:
    return [ln.strip() for ln in ir.splitlines() if ln.strip()]


# ── Function signatures ───────────────────────────────────────────────────────


def test_minimal_fn():
    ir = compile("fn f():\n    pass\n")
    assert "function $f()" in ir


def test_main_is_exported():
    ir = compile("fn main() -> i32:\n    return 0\n")
    assert "export function w $main()" in ir


def test_fn_return_type_i32():
    ir = compile("fn f() -> i32:\n    return 42\n")
    assert "function w $f()" in ir


def test_fn_return_type_i64():
    ir = compile("fn f() -> i64:\n    return 0\n")
    assert "function l $f()" in ir


def test_fn_params():
    ir = compile("fn add(a: i32, b: i32) -> i32:\n    return a\n")
    assert "w %a" in ir
    assert "w %b" in ir


def test_fn_param_i64():
    ir = compile("fn f(x: i64) -> i64:\n    return x\n")
    assert "l %x" in ir


# ── Return ────────────────────────────────────────────────────────────────────


def test_return_int_literal():
    ir = compile("fn f() -> i32:\n    return 42\n")
    assert "ret 42" in ir


def test_return_zero():
    ir = compile("fn main() -> i32:\n    return 0\n")
    assert "ret 0" in ir


def test_return_bool_true():
    ir = compile("fn f() -> bool:\n    return true\n")
    assert "ret 1" in ir


def test_return_bool_false():
    ir = compile("fn f() -> bool:\n    return false\n")
    assert "ret 0" in ir


# ── Arithmetic ────────────────────────────────────────────────────────────────


def test_add():
    ir = compile("fn f(a: i32, b: i32) -> i32:\n    return a + b\n")
    assert "add" in ir


def test_sub():
    ir = compile("fn f(a: i32, b: i32) -> i32:\n    return a - b\n")
    assert "sub" in ir


def test_mul():
    ir = compile("fn f(a: i32, b: i32) -> i32:\n    return a * b\n")
    assert "mul" in ir


def test_div():
    ir = compile("fn f(a: i32, b: i32) -> i32:\n    return a / b\n")
    assert "div" in ir


def test_neg():
    ir = compile("fn f(x: i32) -> i32:\n    return -x\n")
    assert "neg" in ir


def test_not():
    ir = compile("fn f(x: bool) -> bool:\n    return not x\n")
    assert "ceqw" in ir  # `not` lowers to ceq == 0


# ── Comparisons ───────────────────────────────────────────────────────────────


def test_eq():
    ir = compile("fn f(a: i32, b: i32) -> bool:\n    return a == b\n")
    assert "ceqw" in ir


def test_ne():
    ir = compile("fn f(a: i32, b: i32) -> bool:\n    return a != b\n")
    assert "cnew" in ir


def test_lt():
    ir = compile("fn f(a: i32, b: i32) -> bool:\n    return a < b\n")
    assert "csltw" in ir


def test_ge():
    ir = compile("fn f(a: i64, b: i64) -> bool:\n    return a >= b\n")
    assert "csgel" in ir


# ── Local bindings ────────────────────────────────────────────────────────────


def test_const_binding():
    ir = compile("fn f() -> i32:\n    x = 7\n    return x\n")
    # x should be an SSA temp derived from 7
    assert "ret" in ir


def test_var_binding_alloc():
    ir = compile("fn f() -> i32:\n    x: var[i32] = 0\n    return x\n")
    assert "alloc" in ir
    assert "storew" in ir
    assert "loadw" in ir


def test_var_reassign():
    ir = compile(
        "fn f() -> i32:\n"
        "    x: var[i32] = 0\n"
        "    x: var[i32] = 5\n"
        "    return x\n"
    )
    assert "storew" in ir


def test_var_i64():
    ir = compile("fn f() -> i64:\n    n: var[i64] = 0\n    return n\n")
    assert "alloc8 8" in ir
    assert "storel" in ir
    assert "loadl" in ir


# ── If / else ─────────────────────────────────────────────────────────────────


def test_if_emits_jnz():
    ir = compile(
        "fn f(x: i32) -> i32:\n"
        "    if x == 0:\n"
        "        return 1\n"
        "    return 0\n"
    )
    assert "jnz" in ir


def test_if_else():
    ir = compile(
        "fn abs(x: i32) -> i32:\n"
        "    if x < 0:\n"
        "        return -x\n"
        "    else:\n"
        "        return x\n"
    )
    assert "jnz" in ir
    assert "neg" in ir


# ── Match ─────────────────────────────────────────────────────────────────────


def test_match_value():
    ir = compile(
        "fn f(x: i32) -> i32:\n"
        "    match x:\n"
        "        0: return 10\n"
        "        1: return 20\n"
        "        _: return 0\n"
    )
    assert "ceqw" in ir
    assert "jnz" in ir


def test_match_wildcard_jumps():
    ir = compile(
        "fn f(x: i32) -> i32:\n"
        "    match x:\n"
        "        _: return 99\n"
    )
    assert "jmp" in ir


# ── Loop ──────────────────────────────────────────────────────────────────────


def test_loop_break():
    ir = compile(
        "fn f() -> i32:\n"
        "    x: var[i32] = 0\n"
        "    loop:\n"
        "        x: var[i32] = 1\n"
        "        break\n"
        "    return x\n"
    )
    assert "jmp" in ir


def test_for_over_slice():
    ir = compile(
        "fn sum(s: []i32) -> i32:\n"
        "    total: var[i32] = 0\n"
        "    for x in s:\n"
        "        total: var[i32] = total + x\n"
        "    return total\n"
    )
    assert "csgel" in ir  # loop termination comparison
    assert "loadl" in ir  # load length
    assert "loadw" in ir  # load i32 elements


# ── String literals ───────────────────────────────────────────────────────────


def test_string_literal_data_section():
    ir = compile('fn f() -> []u8:\n    return "hello"\n')
    assert 'b "hello"' in ir
    assert "b 0" in ir


def test_string_fat_pointer():
    ir = compile('fn f() -> []u8:\n    return "hi"\n')
    # fat pointer: alloc 16, storel ptr, storel len
    assert "alloc8 16" in ir
    assert "storel" in ir


def test_empty_string():
    ir = compile('fn f() -> []u8:\n    return ""\n')
    assert "b 0" in ir


# ── Struct type declarations ──────────────────────────────────────────────────


def test_struct_type_emitted():
    ir = compile("struct Point:\n    x: i32\n    y: i32\n\nfn f():\n    pass\n")
    assert "type :Point" in ir


def test_struct_fields_qbe_types():
    ir = compile("struct Point:\n    x: i32\n    y: i32\n\nfn f():\n    pass\n")
    assert "{ w, w }" in ir


def test_struct_i64_fields():
    ir = compile("struct Box:\n    v: i64\n\nfn f():\n    pass\n")
    assert "{ l }" in ir


# ── Struct literals ───────────────────────────────────────────────────────────


def test_struct_literal_stack_return_uses_alloc():
    """Bare-struct return → QBE aggregate type + alloca, no arc_alloc."""
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn make() -> Point:\n"
        "    return Point { x: 1, y: 2 }\n"
    )
    assert "function :Point $make()" in ir
    assert "alloc8 8" in ir
    assert "call $arc_alloc" not in ir


def test_struct_literal_rc_return_uses_arc_alloc():
    """rc[T] return keeps the refcounted-heap path with arc_alloc."""
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn make() -> rc[Point]:\n"
        "    return Point { x: 1, y: 2 }\n"
    )
    # arc_alloc takes (size, drop_fn).  No __drop__ → drop_fn is 0.
    assert "call $arc_alloc(l 8, l 0)" in ir


def test_struct_literal_drop_fn_when_present():
    """A struct with `__drop__` defined: the rc[T] path passes its
    symbol to arc_alloc as the drop_fn slot.  Bare-struct returns use
    aggregate + scope-exit direct __drop__ calls (no arc_alloc)."""
    ir = compile(
        "struct Foo:\n"
        "    v: i32\n"
        "\n"
        "    fn __drop__(self):\n"
        "        pass\n"
        "\n"
        "fn make() -> rc[Foo]:\n"
        "    return Foo { v: 0 }\n"
    )
    assert "$Foo____drop__" in ir
    assert "call $arc_alloc(l" in ir


# ── Field access ──────────────────────────────────────────────────────────────


def test_bare_struct_param_uses_aggregate_abi():
    """A bare-struct param `p: Point` is declared as `:Point %p`, the
    QBE aggregate-by-value convention — callers pass the address and
    QBE marshalls the copy per SystemV."""
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn get_x(p: Point) -> i32:\n"
        "    return p.x\n"
    )
    assert ":Point %p" in ir


def test_ptr_binding_from_stack_aliases_slot():
    """`addr: ptr[Point] = p` for a stack-bare `p` binds `addr` to
    the *same* slot pointer.  No alloca, no copy — `addr.x` reads
    `p`'s bytes directly."""
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn main() -> i32:\n"
        "    p = Point { x: 42, y: 7 }\n"
        "    addr: ptr[Point] = p\n"
        "    return addr.x\n"
    )
    # The IR shouldn't materialise a second slot for `addr` — only
    # one alloc for `p` itself.
    assert ir.count("alloc8") == 1
    # Reads through `addr` lower to a single loadw of the slot.
    assert "loadw %_sk_p0" in ir


def test_ptr_struct_param_passes_pointer():
    """A `ptr[Point]` param's ABI is plain `l` (pointer), so callers
    passing a stack-bare value emit `l %slot` — no aggregate copy.
    The callee-signature lookup chooses the ABI from the *declared*
    param type, not the arg's read type."""
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn read_x(p: ptr[Point]) -> i32:\n"
        "    return p.x\n\n"
        "fn main() -> i32:\n"
        "    p = Point { x: 1, y: 2 }\n"
        "    return read_x(p)\n"
    )
    assert "function w $read_x(l %p)" in ir
    assert "call $read_x(l " in ir
    assert ":Point %" not in ir.split("call $read_x")[1].split("\n")[0]


def test_field_access_offset_zero():
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn get_x(p: Point) -> i32:\n"
        "    return p.x\n"
    )
    assert "loadw" in ir


def test_field_access_nonzero_offset():
    ir = compile(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn get_y(p: Point) -> i32:\n"
        "    return p.y\n"
    )
    assert "add" in ir
    assert "loadw" in ir


# ── Method calls ──────────────────────────────────────────────────────────────


def test_method_naming():
    ir = compile(
        "struct Foo:\n"
        "    v: i32\n\n"
        "    fn new() -> Self:\n"
        "        return Self { v: 0 }\n\n"
        "fn f() -> Foo:\n"
        "    return Foo.new()\n"
    )
    assert "$Foo__new" in ir


def test_self_param():
    ir = compile(
        "struct Counter:\n"
        "    n: i32\n\n"
        "    fn get(self) -> i32:\n"
        "        return self.n\n\n"
        "fn f():\n    pass\n"
    )
    assert "l %self" in ir
    assert "$Counter__get" in ir


# ── Function calls ────────────────────────────────────────────────────────────


def test_call_free_function():
    ir = compile(
        "fn double(x: i32) -> i32:\n    return x + x\n\n"
        "fn main() -> i32:\n    return double(21)\n"
    )
    assert "call $double" in ir


def test_call_discards_unit_return():
    ir = compile(
        "fn noop():\n    pass\n\n"
        "fn main() -> i32:\n    noop()\n    return 0\n"
    )
    assert "call $noop" in ir


# ── Multiple functions ────────────────────────────────────────────────────────


def test_two_functions():
    ir = compile(
        "fn a() -> i32:\n    return 1\n\n"
        "fn b() -> i32:\n    return 2\n"
    )
    assert "$a" in ir
    assert "$b" in ir
