"""Ouro AST node definitions.

Every node carries a `span` for source positioning. Nodes are dataclasses;
the parser produces a tree of these, and later passes (type checker, codegen)
consume it.

Categories are expressed as Union type aliases (Type, Pattern, Expression,
Statement, TopLevel) so that container fields can declare what shape of child
they expect without requiring an inheritance tree.
"""

from dataclasses import dataclass, field
from typing import Optional, Union


# ─── Source positioning ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Span:
    """Source location for a node — half-open [start, end).

    Lines and columns are 1-indexed for human-friendliness in error messages.
    """

    file: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass(kw_only=True)
class Node:
    """Base class for every AST node. `span` must be passed by keyword."""

    span: Span


# ─── Types ───────────────────────────────────────────────────────────────────


@dataclass
class NamedType(Node):
    """Simple named type: `i32`, `bool`, `Connection`.

    `module` is set when the type is module-qualified —
    `parse.ParseError` parses as `NamedType(name="ParseError",
    module="parse")`.  The resolver looks up the `module` binding
    first, then resolves `name` inside that module's exports.
    """

    name: str
    module: Optional[str] = None


@dataclass
class GenericType(Node):
    """Generic instantiation: `LinkedList[i32]`, `List[T]`.

    `module` is set when the generic type is module-qualified
    (`mod.Box[i32]`).
    """

    base: str
    args: list["Type"]
    module: Optional[str] = None


@dataclass
class FnType(Node):
    """First-class function type: `fn(i32, i32) -> i32`.

    Function pointers in v1 — no closures, no captured environment.
    A `Name` resolving to a top-level `fn` evaluates to a value of
    the matching `FnType`, which can be stored in a binding, passed
    as an argument, and invoked indirectly.
    """

    params: list["Type"]
    return_type: Optional["Type"]  # None → unit


@dataclass
class WrapperType(Node):
    """Type wrapper: `var[T]`, `const[T]`, `weak[T]`, `ptr[T]`."""

    wrapper: str  # "var" | "const" | "weak" | "ptr"
    inner: "Type"


@dataclass
class SliceType(Node):
    """`[]T`."""

    element: "Type"


@dataclass
class UnionType(Node):
    """`T1 | T2 | ...`."""

    variants: list["Type"]


@dataclass
class InferType(Node):
    """`?` — only legal inside a wrapper, e.g. `var[?]`."""


@dataclass
class SelfType(Node):
    """`Self` — the enclosing struct type, with its generic parameters."""


@dataclass
class LikeMethod(Node):
    """One method signature inside a `like[...]` shape:
    `__next__(p1, p2) -> ret`.  Parameter names and the receiver
    are elided — only types appear."""

    name: str
    params: list["Type"]
    return_type: Optional["Type"]  # None → unit


@dataclass
class LikeType(Node):
    """`like[m1, m2]` — duck-typed shape constraint.  Lists the method
    signatures any concrete type must satisfy.  The shape is always
    open: a concrete impl may have *additional* methods beyond the
    listed ones.  v1 uses this primarily as documentation; the
    typechecker rewrites parameters with this shape into implicit
    generic params, and the existing duck-typed monomorphization
    machinery does the rest.

    Two equivalent surface forms:
      - explicit:   `like[m1(...) -> T, m2(...) -> U]`
      - alias-from: `like[StructName]` / `like[StructName[args]]`
                    — the shape is derived from a named struct's
                    methods (and fields, when verification ships).
    Exactly one of `methods` (non-empty) or `from_struct` (non-None)
    is set; the typechecker expands `from_struct` to a method list
    when lowering to `LikeTy`."""

    methods: list[LikeMethod]
    from_struct: Optional["Type"] = None


Type = Union[
    NamedType,
    GenericType,
    WrapperType,
    SliceType,
    UnionType,
    InferType,
    SelfType,
    "FnType",
    LikeType,
]


# ─── Patterns (used in match arms) ───────────────────────────────────────────


@dataclass
class ValuePattern(Node):
    """A literal value pattern: `200`, `"OK"`."""

    value: "Expression"


@dataclass
class TypePattern(Node):
    """`name: Type` or `_: Type` — discriminate by type, optionally bind."""

    binding: Optional[str]  # None for `_:`, identifier name otherwise
    type: Type


@dataclass
class WildcardPattern(Node):
    """`_:` — default arm, matches anything, no binding."""


Pattern = Union[ValuePattern, TypePattern, WildcardPattern]


# ─── Expressions ─────────────────────────────────────────────────────────────


# Literals
@dataclass
class IntLiteral(Node):
    """`42`, `1_000_000`, `0xFF`, `0b1010`, `0o52`, `42i64`."""

    value: int
    suffix: Optional[str]  # "i32", "u8", "isize", or None for default


@dataclass
class FloatLiteral(Node):
    """`3.14`, `1.5e10`, `3.14f32`."""

    value: float
    suffix: Optional[str]  # "f32", "f64", or None for default


@dataclass
class BoolLiteral(Node):
    """`true` or `false`."""

    value: bool


@dataclass
class ByteLiteral(Node):
    """`'A'`, `'\\n'`, `'\\x41'` — already escape-processed to a single u8."""

    value: int  # 0..=255


@dataclass
class StringLiteral(Node):
    """`"hello"` or `\"\"\"multi-line\"\"\"` — escape-processed UTF-8 bytes."""

    value: bytes
    is_multiline: bool  # whether the source used triple quotes


@dataclass
class ArrayLiteral(Node):
    """`[a, b, c]` — slice constructor.  Each element is an
    expression; all must produce values assignable to a common
    element type, inferred from the first element when not given.
    An empty `[]` keeps the element type as `UnknownTy` until the
    surrounding context narrows it.  Lowered by the codegen into a
    heap-allocated backing buffer plus a stack-resident fat-pointer
    slot, same shape as a string literal."""

    elements: list["Expression"]


# Names and access
@dataclass
class Name(Node):
    """Identifier reference: `foo`, `Connection`."""

    name: str


@dataclass
class Discard(Node):
    """The `_` placeholder — used as a binding target that doesn't bind."""


@dataclass
class FieldAccess(Node):
    """`obj.field`."""

    obj: "Expression"
    field: str


@dataclass
class Index(Node):
    """`obj[i]` — single-element subscript or `obj[start..end]` slice (when index is a Range)."""

    obj: "Expression"
    index: "Expression"


@dataclass
class Range(Node):
    """`start..end` — half-open range expression. Either bound may be omitted."""

    start: Optional["Expression"]
    end: Optional["Expression"]


# Calls and construction
@dataclass
class Argument(Node):
    """A call argument; `name` is None for positional, identifier for `name: value`."""

    name: Optional[str]
    value: "Expression"


@dataclass
class Call(Node):
    """`f(args)` — possibly with named arguments."""

    callee: "Expression"
    args: list[Argument]


@dataclass
class GenericInstantiation(Node):
    """`Foo[T1, T2]` — explicit generic application; result is a type or callable."""

    base: "Expression"
    type_args: list[Type]


@dataclass
class FieldInit(Node):
    """In a struct literal: `name: value`."""

    name: str
    value: "Expression"


@dataclass
class StructLiteral(Node):
    """`Type { field: value, ... }` (full) or `Type {}` (zero-default via __zeroed__)."""

    type: Type
    fields: list[FieldInit]  # empty list = `Type {}`


# Operators
@dataclass
class BinaryOp(Node):
    """`a + b`, `a == b`, `a and b`, `a | b` (type union), etc."""

    op: str
    left: "Expression"
    right: "Expression"


@dataclass
class UnaryOp(Node):
    """`-x`, `not x`."""

    op: str
    operand: "Expression"


@dataclass
class TypeTest(Node):
    """`x ?= Type` or `x ?= T1 | T2` — narrowing test."""

    operand: "Expression"
    type: Type


@dataclass
class Cast(Node):
    """`x as T` — explicit cast.  Covers numeric widening/narrowing
    (int↔int, float↔float) and pointer reinterprets.  Anything else
    is a typechecker error."""

    operand: "Expression"
    type: Type


# Control flow as expressions (also usable at statement position via ExprStatement)
@dataclass
class If(Node):
    """`if cond: then [else: else_block]`. Used as expression or statement.

    When evaluated as an expression, the value is the value of whichever Block ran.
    For elif chains, `else_block` is itself a Block containing a single ExprStatement
    wrapping another `If`.
    """

    condition: "Expression"
    then_block: "Block"
    else_block: Optional["Block"]


@dataclass
class MatchArm(Node):
    """One arm of a match: `pattern: body`."""

    pattern: Pattern
    body: "Block"


@dataclass
class Match(Node):
    """`match scrutinee: arms`. Used as expression or statement."""

    scrutinee: "Expression"
    arms: list[MatchArm]


Expression = Union[
    IntLiteral,
    FloatLiteral,
    BoolLiteral,
    ByteLiteral,
    StringLiteral,
    ArrayLiteral,
    Name,
    Discard,
    FieldAccess,
    Index,
    Range,
    Call,
    GenericInstantiation,
    StructLiteral,
    BinaryOp,
    UnaryOp,
    TypeTest,
    Cast,
    If,
    Match,
]


# ─── Statements ──────────────────────────────────────────────────────────────


@dataclass
class ExprStatement(Node):
    """An expression at statement position; result discarded."""

    expr: Expression


@dataclass
class Binding(Node):
    """`name = expr` or `name: T = expr`. Type is optional (inference allowed)."""

    name: str  # may be "_" for discard
    type: Optional[Type]
    value: Expression


@dataclass
class Assignment(Node):
    """`target = expr` where target is a non-trivial lvalue (FieldAccess or Index).

    Re-assignments to a plain name are also Assignment nodes; the type checker
    distinguishes "this name is already bound as var[T]" (re-assignment) from
    "this name is fresh" (which would be a Binding).
    """

    target: Expression  # FieldAccess, Index, or Name
    value: Expression


@dataclass
class Return(Node):
    """`return [expr]`."""

    value: Optional[Expression]


@dataclass
class Pass(Node):
    """`pass` — empty body marker."""


@dataclass
class Break(Node):
    """`break` — exit enclosing loop."""


@dataclass
class Continue(Node):
    """`continue` — skip to next loop iteration."""


@dataclass
class For(Node):
    """`for x in iter: body`. Lowering rewrites to __iter__/__next__ + loop."""

    binding: str  # bound name; "_" for discard
    binding_type: Optional[Type]
    iterable: Expression
    body: "Block"


@dataclass
class Loop(Node):
    """`loop: body` — infinite loop, exit via break. Used by for-loop lowering."""

    body: "Block"


@dataclass
class Block(Node):
    """A sequence of statements.

    In expression position, the block's value is the value of its last
    expression statement (or unit if the last statement isn't an expression).
    """

    statements: list["Statement"]


Statement = Union[
    ExprStatement,
    Binding,
    Assignment,
    Return,
    Pass,
    Break,
    Continue,
    For,
    Loop,
    Block,
]


# ─── Top-level ───────────────────────────────────────────────────────────────


@dataclass
class Parameter(Node):
    """A function parameter: `name: type`. May start with `_` for documented-but-unused."""

    name: str
    type: Type


@dataclass
class SelfParam(Node):
    """The special `self` first parameter of a method.

    For bare `self` (no annotation in source), `is_default` is True and `type`
    is `ptr[Self]`. For explicit annotations, `is_default` is False and `type`
    is whatever the user wrote.
    """

    type: Type
    is_default: bool


@dataclass
class Function(Node):
    """`fn name[generics](params) -> return_type: body`.

    Static methods (and free functions) have `self_param=None`. Instance
    methods have `self_param` set.
    """

    name: str
    generics: list[str]  # type-parameter names, e.g. ["T", "U"]
    self_param: Optional[SelfParam]
    params: list[Parameter]
    return_type: Optional[Type]  # None means returns nothing (implicit unit)
    body: Block
    is_variadic: bool = False  # trailing `...` after fixed params


@dataclass
class StructField(Node):
    """A struct field declaration: `name: type`."""

    name: str
    type: Type


@dataclass
class Struct(Node):
    """`struct Name[generics]: fields and methods`."""

    name: str
    generics: list[str]
    fields: list[StructField]
    methods: list[Function]


@dataclass
class Import(Node):
    """`name = import("path")` — comptime module import."""

    binding: str
    path: str  # the string-literal path, after escape processing


@dataclass
class AsmDecl(Node):
    """`asm name(params) -> return_type: <verbatim assembly body>`.

    The body is opaque text emitted into a per-module sidecar `.s`
    file by the codegen.  The Ouro signature determines the SystemV
    register layout: parameters arrive in rdi/rsi/rdx/rcx/r8/r9 (first
    six), the body returns through rax.  No prologue/epilogue is
    inserted — the body is the whole function.
    """

    name: str
    params: list[Parameter]
    return_type: Optional[Type]  # None means returns nothing (implicit unit)
    body_text: str  # verbatim asm body, one instruction per line


@dataclass
class ExternDecl(Node):
    """`extern name(params, ...) -> return_type` — declares an
    externally-linked C symbol.  No body emission; calls route to the
    *bare* symbol name (no module prefix) regardless of which module
    declared it.  A trailing `...` marks the function as variadic; the
    codegen emits the QBE variadic-call separator at call sites.
    """

    name: str
    params: list[Parameter]
    return_type: Optional[Type]  # None means returns nothing
    is_variadic: bool = False


@dataclass
class TopLevelBinding(Node):
    """A top-level `name = expr` or `name: T = expr` — module constant or singleton."""

    name: str
    type: Optional[Type]
    value: Expression


@dataclass
class TypeAlias(Node):
    """A type alias: `Result: type = u8 | Error` or generic
    `Option[T]: type = T | Null`.  Transparent — every use of
    the name expands to the alias's `body` (with generic args
    substituted) before reaching the typechecker's main passes.

    `enum_variants` is non-empty only when this alias was synthesized
    from an `enum` declaration; it maps each user-visible variant name
    to the mangled struct name so dot-access (`Name.Variant`) can be
    routed to the right struct construction.
    """

    name: str
    generics: list[str]  # empty if not a generic alias
    body: Type
    enum_variants: dict[str, str] = field(default_factory=dict)


@dataclass
class EnumDecl(Node):
    """`enum Name:\\n    Var1\\n    Var2\\n...` — payload-free tagged
    union sugar.  Desugared at load time into one empty `Struct` per
    variant (mangled as `Name__Variant`) plus a `TypeAlias` unioning
    them.  Dot-access `Name.Var1` constructs a variant value; the
    type form `Name__Var1` is used directly in match patterns."""

    name: str
    variants: list[str]


TopLevel = Union[
    Function, Struct, Import, TopLevelBinding, AsmDecl, TypeAlias, ExternDecl, EnumDecl
]


@dataclass
class File(Node):
    """A whole `.ou` source file; the root of every parse tree."""

    path: str  # filesystem path of the source file
    declarations: list[TopLevel]
