"""Name resolver tests."""

from src.lexer import lex
from src.parser import parse
from src.resolver import resolve


def go(source: str):
    return resolve(parse(lex(source), "<test>"))


def errors(source: str) -> list[str]:
    return [str(e) for e in go(source).errors]


def no_errors(source: str) -> None:
    result = go(source)
    assert result.errors == [], "\n".join(str(e) for e in result.errors)


# ── Module-level names ────────────────────────────────────────────────────────


def test_import_binding():
    no_errors('io = import("std/io")\n')


def test_top_level_const():
    no_errors("x = 42\n")


def test_top_level_typed():
    no_errors("x: i32 = 42\n")


def test_forward_reference():
    # g calls f which is defined after g — must work at module scope
    no_errors("fn g() -> i32:\n    return f()\n\nfn f() -> i32:\n    return 1\n")


def test_duplicate_top_level():
    errs = errors("fn f():\n    pass\n\nfn f():\n    pass\n")
    assert len(errs) == 1
    assert "'f' already defined" in errs[0]


# ── Function params & locals ──────────────────────────────────────────────────


def test_param_in_scope():
    no_errors("fn f(x: i32) -> i32:\n    return x\n")


def test_local_in_scope():
    no_errors("fn f() -> i32:\n    x = 42\n    return x\n")


def test_undefined_name():
    errs = errors("fn f():\n    return x\n")
    assert any("undefined name 'x'" in e for e in errs)


def test_duplicate_local_tolerated():
    # Same-scope re-binding is intentionally tolerated by the resolver —
    # the type checker rejects it as "not mutable" if `x` isn't var[T].
    # This way the parser can emit Binding for both fresh bindings and
    # re-assignments without distinguishing.
    errs = errors("fn f():\n    x = 1\n    x = 2\n")
    assert not any("'x' already defined" in e for e in errs)


def test_binding_rhs_before_lhs():
    # `x = x` — RHS `x` should fail (outer scope has no `x`), not self-reference
    errs = errors("fn f():\n    x = x\n")
    assert any("undefined name 'x'" in e for e in errs)


def test_discard_not_defined():
    # `_` on LHS should NOT pollute the scope
    no_errors("fn f():\n    _ = 42\n    _ = 99\n")


# ── Types ─────────────────────────────────────────────────────────────────────


def test_builtin_types_resolve():
    for t in ["i8", "i32", "u64", "f64", "bool", "usize", "Null"]:
        no_errors(f"fn f(x: {t}):\n    pass\n")


def test_unknown_type():
    errs = errors("fn f(x: Bogus):\n    pass\n")
    assert any("unknown type 'Bogus'" in e for e in errs)


def test_struct_type_resolves():
    no_errors("struct Foo:\n    pass\n\nfn f(x: Foo):\n    pass\n")


def test_generic_type_param_in_scope():
    no_errors("fn f[T](x: T) -> T:\n    return x\n")


def test_generic_type_param_unknown():
    errs = errors("fn f(x: T):\n    pass\n")
    assert any("unknown type 'T'" in e for e in errs)


def test_slice_type():
    no_errors("fn f(x: []u8):\n    pass\n")


def test_union_type():
    no_errors("struct Err:\n    pass\n\nfn f() -> i32 | Err:\n    pass\n")


def test_wrapper_type():
    no_errors("fn f(x: var[i32]):\n    pass\n")


def test_infer_type():
    no_errors("fn f():\n    x: var[?] = 42\n")


# ── Structs ───────────────────────────────────────────────────────────────────


def test_struct_field_types():
    no_errors("struct Point:\n    x: i32\n    y: i32\n")


def test_struct_generic_field():
    no_errors("struct Box[T]:\n    val: T\n")


def test_struct_method_self():
    no_errors("struct S:\n    fn f(self):\n        pass\n")


def test_struct_method_explicit_self():
    no_errors("struct S:\n    fn f(self: ptr[var[Self]]):\n        pass\n")


def test_struct_static_method():
    no_errors("struct S:\n    fn new() -> Self:\n        pass\n")


def test_struct_self_outside_method_error():
    errs = errors("fn f():\n    x = self\n")
    assert any("'self'" in e for e in errs)


# ── Control flow ──────────────────────────────────────────────────────────────


def test_for_binding_in_scope():
    no_errors("fn f(items: []i32):\n    for x in items:\n        y = x\n")


def test_for_binding_not_after_loop():
    errs = errors("fn f(items: []i32):\n    for x in items:\n        pass\n    y = x\n")
    assert any("undefined name 'x'" in e for e in errs)


def test_for_discard():
    no_errors("fn f(items: []i32):\n    for _ in items:\n        pass\n")


def test_if_condition_resolved():
    no_errors("fn f(x: bool):\n    if x:\n        pass\n")


def test_loop_break():
    no_errors("fn f():\n    loop:\n        break\n")


# ── Match ─────────────────────────────────────────────────────────────────────


def test_match_value_pattern():
    no_errors("fn f(x: i32):\n    match x:\n        1: pass\n        _: pass\n")


def test_match_type_pattern_binding():
    no_errors(
        "struct Err:\n    pass\n"
        "fn f(x: i32 | Err):\n"
        "    match x:\n"
        "        n: i32:\n"
        "            y = n\n"
        "        _: Err:\n"
        "            pass\n"
    )


def test_match_binding_not_after_arm():
    # `n` bound inside match arm should not leak out
    errs = errors(
        "struct Err:\n    pass\n"
        "fn f(x: i32 | Err):\n"
        "    match x:\n"
        "        n: i32:\n"
        "            pass\n"
        "        _: Err:\n"
        "            pass\n"
        "    y = n\n"
    )
    assert any("undefined name 'n'" in e for e in errs)


# ── Type test (?=) ────────────────────────────────────────────────────────────


def test_type_test_resolves():
    no_errors(
        "struct Err:\n    pass\nfn f(x: i32 | Err):\n    if x ?= Err:\n        pass\n"
    )


