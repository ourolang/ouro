"""Type checker tests."""

from src.lexer import lex
from src.parser import parse
from src.resolver import resolve
from src.typechecker import (
    typecheck,
    PrimTy,
    SliceTy,
)


def go(source: str):
    tokens = lex(source)
    tree = parse(tokens, "<test>")
    res = resolve(tree)
    return typecheck(tree, res)


def ty_of_first_return(source: str):
    """Check the type of the return expression in the first function."""
    from src.nodes import Function, Return

    tokens = lex(source)
    tree = parse(tokens, "<test>")
    res = resolve(tree)
    tm = typecheck(tree, res)

    for decl in tree.declarations:
        if isinstance(decl, Function):
            for stmt in decl.body.statements:
                if isinstance(stmt, Return) and stmt.value is not None:
                    return tm.type_of(stmt.value)
    return None


def no_errors(source: str) -> None:
    result = go(source)
    assert result.errors == [], "\n".join(str(e) for e in result.errors)


def errors(source: str) -> list[str]:
    return [str(e) for e in go(source).errors]


# ── Literal types ─────────────────────────────────────────────────────────────


def test_int_default_type():
    assert ty_of_first_return("fn f() -> isize:\n    return 42\n") == PrimTy("isize")


def test_int_suffix_type():
    assert ty_of_first_return("fn f() -> i32:\n    return 42i32\n") == PrimTy("i32")


def test_float_default_type():
    assert ty_of_first_return("fn f() -> f64:\n    return 3.14\n") == PrimTy("f64")


def test_float_suffix_type():
    assert ty_of_first_return("fn f() -> f32:\n    return 1.0f32\n") == PrimTy("f32")


def test_bool_type():
    assert ty_of_first_return("fn f() -> bool:\n    return true\n") == PrimTy("bool")


def test_byte_type():
    assert ty_of_first_return("fn f() -> u8:\n    return 'A'\n") == PrimTy("u8")


def test_string_type():
    assert ty_of_first_return('fn f() -> []u8:\n    return "hi"\n') == SliceTy(
        PrimTy("u8")
    )


# ── Arithmetic / comparison types ─────────────────────────────────────────────


def test_add_i32():
    src = "fn f(a: i32, b: i32) -> i32:\n    return a + b\n"
    assert ty_of_first_return(src) == PrimTy("i32")


def test_comparison_bool():
    src = "fn f(a: i32, b: i32) -> bool:\n    return a == b\n"
    assert ty_of_first_return(src) == PrimTy("bool")


def test_not_bool():
    src = "fn f(x: bool) -> bool:\n    return not x\n"
    assert ty_of_first_return(src) == PrimTy("bool")


def test_arithmetic_literal_coerce():
    # `n + 1` where n:usize → result should be usize (literal coerces)
    src = "fn f(n: usize) -> usize:\n    return n + 1\n"
    no_errors(src)
    assert ty_of_first_return(src) == PrimTy("usize")


# ── var[?] inference ──────────────────────────────────────────────────────────


def test_var_infer():
    tokens = lex("fn f():\n    x: var[?] = 42\n")
    tree = parse(tokens, "<test>")
    res = resolve(tree)
    tm = typecheck(tree, res)

    from src.nodes import Function, Binding

    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    stmt = fn.body.statements[0]
    assert isinstance(stmt, Binding)
    ty = tm.type_of(stmt.value)
    assert ty == PrimTy("isize")


def test_var_infer_no_errors():
    no_errors("fn f():\n    x: var[?] = 42\n    x = x + 1\n")


# ── Mutability ────────────────────────────────────────────────────────────────


def test_var_reassign_ok():
    no_errors("fn f():\n    x: var[i32] = 1\n    x = 2\n")


def test_const_reassign_error():
    errs = errors("fn f():\n    x: i32 = 1\n    x = 2\n")
    assert any("not mutable" in e for e in errs)


def test_module_var_reassign_ok():
    no_errors("_n: var[usize] = 0\nfn f():\n    _n = _n + 1\n")


# ── Return type checking ──────────────────────────────────────────────────────


def test_return_ok():
    no_errors("fn f() -> i32:\n    return 42i32\n")


def test_return_mismatch():
    errs = errors("fn f() -> bool:\n    return 42i32\n")
    assert any("return type mismatch" in e for e in errs)


def test_return_unit_ok():
    no_errors("fn f():\n    pass\n")


# ── Type test ─────────────────────────────────────────────────────────────────


def test_type_test_bool():
    src = "struct Err:\n    pass\nfn f(x: i32 | Err) -> bool:\n    return x ?= Err\n"
    assert ty_of_first_return(src) == PrimTy("bool")


def test_type_test_no_errors():
    no_errors(
        "struct Err:\n    pass\nfn f(x: i32 | Err):\n    if x ?= Err:\n        pass\n"
    )


# ── Struct field types ────────────────────────────────────────────────────────


def test_struct_field_access_type():
    src = (
        "struct Point:\n    x: i32\n    y: i32\n"
        "fn f(p: Point) -> i32:\n    return p.x\n"
    )
    assert ty_of_first_return(src) == PrimTy("i32")


def test_struct_field_no_errors():
    no_errors(
        "struct Point:\n    x: i32\n    y: i32\n"
        "fn f(p: Point) -> i32:\n    return p.x\n"
    )


def test_struct_mutable_field_ok():
    no_errors(
        "struct Counter:\n    _n: var[i32]\nfn f(c: ptr[var[Counter]]):\n    c._n = 1\n"
    )


# ── Union types ───────────────────────────────────────────────────────────────


def test_union_return():
    src = (
        "struct Err:\n    pass\n"
        "fn f(x: bool) -> i32 | Err:\n"
        "    if x:\n"
        "        return 42i32\n"
        "    return Err {}\n"
    )
    no_errors(src)


def test_union_member_check():
    src = "struct Err:\n    pass\nfn f() -> i32 | Err:\n    return 42i32\n"
    no_errors(src)


# ── Import / module binding ───────────────────────────────────────────────────


def test_import_binding_type():
    tokens = lex('io = import("std/io")\n')
    tree = parse(tokens, "<test>")
    res = resolve(tree)
    tm = typecheck(tree, res)
    assert len(tm) >= 0  # just check no crash
    assert tm.errors == []


# ── For loop ──────────────────────────────────────────────────────────────────


def test_for_loop_no_errors():
    no_errors("fn f(items: []i32):\n    for x in items:\n        pass\n")


def test_for_loop_var_counter():
    no_errors(
        "fn f(items: []i32):\n"
        "    n: var[usize] = 0\n"
        "    for _ in items:\n"
        "        n = n + 1\n"
    )


# ── Self & methods ────────────────────────────────────────────────────────────


def test_method_self_field():
    no_errors(
        "struct S:\n    val: i32\n    fn get(self) -> i32:\n        return self.val\n"
    )


def test_static_method_return_self():
    no_errors("struct S:\n    pass\n    fn new() -> Self:\n        return Self {}\n")


# ── Call-site arg checking ───────────────────────────────────────────────────


def test_call_ok():
    no_errors(
        "fn add(a: i32, b: i32) -> i32:\n    return a + b\n"
        "fn main():\n    _ = add(1, 2)\n"
    )


def test_call_wrong_arity_too_few():
    errs = errors(
        "fn add(a: i32, b: i32) -> i32:\n    return a + b\n"
        "fn main():\n    _ = add(1)\n"
    )
    assert any("expected 2, got 1" in e for e in errs)


def test_call_wrong_arity_too_many():
    errs = errors(
        "fn add(a: i32, b: i32) -> i32:\n    return a + b\n"
        "fn main():\n    _ = add(1, 2, 3)\n"
    )
    assert any("expected 2, got 3" in e for e in errs)


def test_call_wrong_arg_type():
    errs = errors(
        "struct Box:\n    val: i32\n"
        "fn take_int(x: i32) -> i32:\n    return x\n"
        "fn main():\n    b = Box { val: 1 }\n    _ = take_int(b)\n"
    )
    assert any("argument 1" in e and "expected i32" in e and "got Box" in e for e in errs)


def test_call_numeric_widening_ok():
    # `1` is isize; passing to i32 is allowed under the lenient numeric
    # rule (no narrowness check in v1).
    no_errors(
        "fn take_i32(x: i32):\n    pass\n"
        "fn main():\n    take_i32(1)\n"
    )


def test_call_method_wrong_arg():
    errs = errors(
        "struct C:\n    pass\n"
        "    fn add1(self, x: i32) -> i32:\n        return x + 1\n"
        "fn main():\n    c = C {}\n    _ = c.add1(c)\n"
    )
    assert any("expected i32" in e and "got C" in e for e in errs)


def test_call_generic_function_skipped():
    # Generic functions are skipped — pure duck-typing v1.
    no_errors(
        "fn id[T](x: T) -> T:\n    return x\n"
        "fn main():\n    _ = id(42)\n"
    )


def test_call_named_arg_skipped():
    # Named args are not yet matched to params — skip the check.
    no_errors(
        "fn f(a: i32, b: i32):\n    pass\n"
        "fn main():\n    f(a: 1, b: 2)\n"
    )


def test_call_module_method_skipped():
    # Module methods have no signature; no arg check.
    no_errors(
        'io = import("std/io")\n'
        "fn main():\n"
        "    io.println(123)\n"   # bogus type, but no check available
    )


# ── ptr[T] construction ───────────────────────────────────────────────────────


def test_ptr_from_named_binding_ok():
    no_errors(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn main() -> i32:\n"
        "    p = Point { x: 1, y: 2 }\n"
        "    addr: ptr[Point] = p\n"
        "    return addr.x\n"
    )


def test_ptr_from_literal_rejected():
    errs = errors(
        "fn main() -> i32:\n"
        "    x: ptr[i32] = 42\n"
        "    return 0\n"
    )
    assert any("addressable RHS" in e for e in errs), errs


def test_type_alias_basic_no_errors():
    no_errors(
        "struct E:\n"
        "    pos: i64\n\n"
        "Result: type = i64 | E\n\n"
        "fn parse(b: bool) -> Result:\n"
        "    if b:\n"
        "        return 7\n"
        "    return E { pos: 0 }\n"
    )


def test_type_alias_generic_no_errors():
    no_errors(
        "Option[T]: type = T | Null\n\n"
        "fn maybe(b: bool) -> Option[i32]:\n"
        "    if b:\n"
        "        return 5\n"
        "    return Null {}\n"
    )


def test_ptr_from_call_result_rejected():
    errs = errors(
        "struct Point:\n    x: i32\n    y: i32\n\n"
        "fn make() -> Point:\n"
        "    return Point { x: 1, y: 2 }\n\n"
        "fn main() -> i32:\n"
        "    addr: ptr[Point] = make()\n"   # call result has no observable address
        "    return 0\n"
    )
    assert any("addressable RHS" in e for e in errs), errs


