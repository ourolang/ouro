"""Parser tests."""

import pytest
from src.lexer import lex
from src.parser import parse, ParseError
from src.nodes import (
    File,
    Function,
    Struct,
    Import,
    TopLevelBinding,
    Binding,
    Assignment,
    Return,
    Pass,
    Break,
    Continue,
    For,
    Loop,
    ExprStatement,
    IntLiteral,
    FloatLiteral,
    BoolLiteral,
    StringLiteral,
    Name,
    Discard,
    FieldAccess,
    Call,
    GenericInstantiation,
    StructLiteral,
    BinaryOp,
    UnaryOp,
    TypeTest,
    If,
    Match,
    NamedType,
    GenericType,
    WrapperType,
    SliceType,
    UnionType,
    InferType,
    SelfType,
    ValuePattern,
    TypePattern,
    WildcardPattern,
)


def p(source: str) -> File:
    return parse(lex(source), "<test>")


def expr(source: str):
    """Parse source as a single expression statement inside a function."""
    tree = p(f"fn f():\n    {source}\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    if isinstance(s, ExprStatement):
        return s.expr
    return s


def stmt(source: str):
    """Parse source as a single statement inside a function."""
    tree = p(f"fn f():\n    {source}\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    return fn.body.statements[0]


# ── Top-level ─────────────────────────────────────────────────────────────────


def test_import():
    tree = p('io = import("std/io")\n')
    decl = tree.declarations[0]
    assert isinstance(decl, Import)
    assert decl.binding == "io"
    assert decl.path == "std/io"


def test_top_level_binding_inferred():
    tree = p("x = 42\n")
    decl = tree.declarations[0]
    assert isinstance(decl, TopLevelBinding)
    assert decl.name == "x"
    assert decl.type is None
    assert isinstance(decl.value, IntLiteral)


def test_top_level_binding_typed():
    tree = p("x: i32 = 42\n")
    decl = tree.declarations[0]
    assert isinstance(decl, TopLevelBinding)
    assert isinstance(decl.type, NamedType)
    assert decl.type.name == "i32"


def test_function_minimal():
    tree = p("fn f():\n    pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    assert fn.name == "f"
    assert fn.generics == []
    assert fn.self_param is None
    assert fn.params == []
    assert fn.return_type is None
    assert isinstance(fn.body.statements[0], Pass)


def test_function_with_return_type():
    tree = p("fn f() -> i32:\n    pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    assert isinstance(fn.return_type, NamedType)
    assert fn.return_type.name == "i32"


def test_function_generics():
    tree = p("fn f[T](x: T) -> T:\n    pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    assert fn.generics == ["T"]
    assert fn.params[0].name == "x"


def test_struct_empty():
    tree = p("struct Foo:\n    pass\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    assert s.name == "Foo"
    assert s.fields == []
    assert s.methods == []


def test_struct_fields():
    tree = p("struct Point:\n    x: i32\n    y: i32\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    assert len(s.fields) == 2
    assert s.fields[0].name == "x"
    assert isinstance(s.fields[0].type, NamedType)


def test_struct_with_method():
    tree = p("struct Foo:\n    fn bar(self):\n        pass\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    assert len(s.methods) == 1
    m = s.methods[0]
    assert m.name == "bar"
    assert m.self_param is not None
    assert m.self_param.is_default is True


def test_struct_generics():
    tree = p("struct Box[T]:\n    val: T\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    assert s.generics == ["T"]


# ── Types ─────────────────────────────────────────────────────────────────────


def _type(source: str):
    tree = p(f"fn f(x: {source}):\n    pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    return fn.params[0].type


def test_type_named():
    t = _type("i32")
    assert isinstance(t, NamedType)
    assert t.name == "i32"


def test_type_slice():
    t = _type("[]u8")
    assert isinstance(t, SliceType)
    assert isinstance(t.element, NamedType)


def test_type_wrapper_var():
    t = _type("var[i32]")
    assert isinstance(t, WrapperType)
    assert t.wrapper == "var"
    assert isinstance(t.inner, NamedType)


def test_type_wrapper_ptr():
    t = _type("ptr[Self]")
    assert isinstance(t, WrapperType)
    assert t.wrapper == "ptr"
    assert isinstance(t.inner, SelfType)


def test_type_infer():
    t = _type("var[?]")
    assert isinstance(t, WrapperType)
    assert isinstance(t.inner, InferType)


def test_type_union():
    t = _type("i32 | ParseError")
    assert isinstance(t, UnionType)
    assert len(t.variants) == 2


def test_type_generic():
    t = _type("LinkedList[T]")
    assert isinstance(t, GenericType)
    assert t.base == "LinkedList"
    assert isinstance(t.args[0], NamedType)


def test_type_self():
    t = _type("Self")
    assert isinstance(t, SelfType)


# ── Expressions ───────────────────────────────────────────────────────────────


def test_expr_int():
    e = expr("42")
    assert isinstance(e, IntLiteral)
    assert e.value == 42


def test_expr_float():
    e = expr("3.14")
    assert isinstance(e, FloatLiteral)


def test_expr_bool_true():
    e = expr("true")
    assert isinstance(e, BoolLiteral)
    assert e.value is True


def test_expr_bool_false():
    e = expr("false")
    assert isinstance(e, BoolLiteral)
    assert e.value is False


def test_expr_string():
    e = expr('"hello"')
    assert isinstance(e, StringLiteral)
    assert e.value == b"hello"


def test_expr_name():
    e = expr("foo")
    assert isinstance(e, Name)
    assert e.name == "foo"


def test_expr_discard():
    e = expr("_")
    assert isinstance(e, Discard)


def test_expr_unary_neg():
    e = expr("-x")
    assert isinstance(e, UnaryOp)
    assert e.op == "-"


def test_expr_unary_not():
    e = expr("not x")
    assert isinstance(e, UnaryOp)
    assert e.op == "not"


def test_expr_binary_add():
    e = expr("a + b")
    assert isinstance(e, BinaryOp)
    assert e.op == "+"


def test_expr_binary_precedence():
    e = expr("a + b * c")
    assert isinstance(e, BinaryOp)
    assert e.op == "+"
    assert isinstance(e.right, BinaryOp)
    assert e.right.op == "*"


def test_expr_field_access():
    e = expr("foo.bar")
    assert isinstance(e, FieldAccess)
    assert e.field == "bar"
    assert isinstance(e.obj, Name)


def test_expr_call_no_args():
    e = expr("f()")
    assert isinstance(e, Call)
    assert len(e.args) == 0


def test_expr_call_positional():
    e = expr("f(1, 2)")
    assert isinstance(e, Call)
    assert len(e.args) == 2
    assert e.args[0].name is None


def test_expr_call_named():
    e = expr("f(x: 1, y: 2)")
    assert isinstance(e, Call)
    assert e.args[0].name == "x"
    assert e.args[1].name == "y"


def test_expr_type_test():
    e = expr("x ?= ParseError")
    assert isinstance(e, TypeTest)
    assert isinstance(e.operand, Name)
    assert isinstance(e.type, NamedType)


def test_expr_type_test_union():
    e = expr("x ?= ParseError | EmptyError")
    assert isinstance(e, TypeTest)
    assert isinstance(e.type, UnionType)


def test_expr_struct_literal_empty():
    e = expr("Foo {}")
    assert isinstance(e, StructLiteral)
    assert e.fields == []


def test_expr_struct_literal_fields():
    e = expr("Point { x: 1, y: 2 }")
    assert isinstance(e, StructLiteral)
    assert len(e.fields) == 2
    assert e.fields[0].name == "x"


def test_expr_generic_instantiation():
    e = expr("LinkedList[i32]")
    assert isinstance(e, GenericInstantiation)


# ── Statements ────────────────────────────────────────────────────────────────


def test_stmt_binding_inferred():
    s = stmt("x = 42")
    assert isinstance(s, Binding)
    assert s.name == "x"
    assert s.type is None


def test_stmt_binding_typed():
    s = stmt("x: i32 = 42")
    assert isinstance(s, Binding)
    assert isinstance(s.type, NamedType)


def test_stmt_assignment_field():
    s = stmt("self.x = 1")
    assert isinstance(s, Assignment)
    assert isinstance(s.target, FieldAccess)


def test_stmt_return_value():
    s = stmt("return 42")
    assert isinstance(s, Return)
    assert isinstance(s.value, IntLiteral)


def test_stmt_return_nothing():
    s = stmt("return")
    assert isinstance(s, Return)
    assert s.value is None


def test_stmt_pass():
    s = stmt("pass")
    assert isinstance(s, Pass)


def test_stmt_break():
    s = stmt("break")
    assert isinstance(s, Break)


def test_stmt_continue():
    s = stmt("continue")
    assert isinstance(s, Continue)


def test_stmt_for():
    s = stmt("for x in items:\n        pass\n")
    assert isinstance(s, For)
    assert s.binding == "x"
    assert s.binding_type is None


def test_stmt_for_typed():
    s = stmt("for x: var[?] in items:\n        pass\n")
    assert isinstance(s, For)
    assert isinstance(s.binding_type, WrapperType)


def test_stmt_for_discard():
    s = stmt("for _ in items:\n        pass\n")
    assert isinstance(s, For)
    assert s.binding == "_"


def test_stmt_loop():
    tree = p("fn f():\n    loop:\n        break\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    assert isinstance(s, Loop)


def test_stmt_while_desugars_to_loop():
    # `while cond: body` parses as a Loop whose first statement is
    # `if not cond: break`.
    tree = p("fn f():\n    while true:\n        pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    assert isinstance(s, Loop)
    first = s.body.statements[0]
    assert isinstance(first, ExprStatement)
    assert isinstance(first.expr, If)
    assert isinstance(first.expr.condition, UnaryOp)
    assert first.expr.condition.op == "not"
    # The then-block contains a Break.
    then_first = first.expr.then_block.statements[0]
    assert isinstance(then_first, Break)


# ── If / elif / else ──────────────────────────────────────────────────────────


def test_if_basic():
    tree = p("fn f():\n    if x:\n        pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    assert isinstance(s, ExprStatement)
    assert isinstance(s.expr, If)
    assert s.expr.else_block is None


def test_if_else():
    tree = p("fn f():\n    if x:\n        pass\n    else:\n        pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    assert isinstance(s, ExprStatement)
    e = s.expr
    assert isinstance(e, If)
    assert e.else_block is not None


def test_if_elif():
    tree = p("fn f():\n    if x:\n        pass\n    elif y:\n        pass\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    assert isinstance(s, ExprStatement)
    e = s.expr
    assert isinstance(e, If)
    assert e.else_block is not None
    # else_block wraps a nested If
    inner_s = e.else_block.statements[0]
    assert isinstance(inner_s, ExprStatement)
    assert isinstance(inner_s.expr, If)


def test_if_inline():
    tree = p("fn f():\n    if x: return 1\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    s = fn.body.statements[0]
    assert isinstance(s, ExprStatement)
    e = s.expr
    assert isinstance(e, If)
    assert len(e.then_block.statements) == 1
    assert isinstance(e.then_block.statements[0], Return)


# ── Match ─────────────────────────────────────────────────────────────────────


def test_match_value_patterns():
    tree = p(
        "fn f():\n    match x:\n        1: pass\n        2: pass\n        _: pass\n"
    )
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    stmt_ = fn.body.statements[0]
    assert isinstance(stmt_, ExprStatement)
    s = stmt_.expr
    assert isinstance(s, Match)
    assert len(s.arms) == 3
    assert isinstance(s.arms[0].pattern, ValuePattern)
    assert isinstance(s.arms[2].pattern, WildcardPattern)


def test_match_type_patterns():
    tree = p(
        "fn f():\n    match x:\n        n: i32:\n            pass\n        _: Err:\n            pass\n"
    )
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    stmt_ = fn.body.statements[0]
    assert isinstance(stmt_, ExprStatement)
    s = stmt_.expr
    assert isinstance(s, Match)
    assert len(s.arms) == 2
    assert isinstance(s.arms[0].pattern, TypePattern)
    assert s.arms[0].pattern.binding == "n"
    assert isinstance(s.arms[1].pattern, TypePattern)
    assert s.arms[1].pattern.binding is None


def test_match_inline_arm():
    tree = p("fn f():\n    match x:\n        1: return 1\n        _: return 0\n")
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    stmt_ = fn.body.statements[0]
    assert isinstance(stmt_, ExprStatement)
    s = stmt_.expr
    assert isinstance(s, Match)
    assert len(s.arms) == 2


# ── Self params ───────────────────────────────────────────────────────────────


def test_self_bare():
    tree = p("struct S:\n    fn f(self):\n        pass\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    m = s.methods[0]
    assert m.self_param is not None
    assert m.self_param.is_default is True
    assert isinstance(m.self_param.type, WrapperType)
    assert m.self_param.type.wrapper == "ptr"


def test_self_explicit():
    tree = p("struct S:\n    fn f(self: ptr[var[Self]]):\n        pass\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    m = s.methods[0]
    assert m.self_param is not None
    assert m.self_param.is_default is False
    assert isinstance(m.self_param.type, WrapperType)


def test_static_method():
    tree = p("struct S:\n    fn new() -> Self:\n        pass\n")
    s = tree.declarations[0]
    assert isinstance(s, Struct)
    m = s.methods[0]
    assert m.self_param is None


# ── like[...] duck-typing constraint ──────────────────────────────────────────


def test_like_type_single_line():
    from src.nodes import LikeType, TypeAlias
    tree = p("It: type = like[show() -> i32, count() -> u64]\n")
    a = tree.declarations[0]
    assert isinstance(a, TypeAlias)
    assert isinstance(a.body, LikeType)
    assert [m.name for m in a.body.methods] == ["show", "count"]


def test_like_type_multiline():
    from src.nodes import LikeType, TypeAlias
    tree = p(
        "Iter[T]: type = like[\n"
        "    __next__() -> T | Null,\n"
        "    __prev__() -> T | Null,\n"
        "]\n"
    )
    a = tree.declarations[0]
    assert isinstance(a, TypeAlias)
    assert a.generics == ["T"]
    assert isinstance(a.body, LikeType)
    assert [m.name for m in a.body.methods] == ["__next__", "__prev__"]


def test_like_type_inline_param():
    from src.nodes import LikeType
    tree = p(
        "fn take(x: like[m() -> i32]) -> i32:\n"
        "    return 0\n"
    )
    fn = tree.declarations[0]
    assert isinstance(fn, Function)
    # Pre-typecheck: param.type is still LikeType in the AST.
    assert isinstance(fn.params[0].type, LikeType)
    assert fn.params[0].type.methods[0].name == "m"


# ── enum decls ────────────────────────────────────────────────────────────────


def test_enum_basic():
    from src.nodes import EnumDecl
    tree = p("enum Color:\n    Red\n    Green\n    Blue\n")
    e = tree.declarations[0]
    assert isinstance(e, EnumDecl)
    assert e.name == "Color"
    assert e.variants == ["Red", "Green", "Blue"]


def test_enum_empty():
    from src.nodes import EnumDecl
    tree = p("enum Empty:\n    pass\n")
    e = tree.declarations[0]
    assert isinstance(e, EnumDecl)
    assert e.variants == []


# ── Error cases ───────────────────────────────────────────────────────────────


def test_parse_error_unexpected_token():
    # `fn` inside a function body is not valid as a statement
    with pytest.raises(ParseError):
        p("fn f():\n    fn\n")


def test_parse_error_missing_colon():
    with pytest.raises(ParseError):
        p("fn f()\n    pass\n")
