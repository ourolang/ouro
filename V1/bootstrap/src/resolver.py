"""Name resolution for Ouro.

Walk the AST, build a scope tree, and record what each Name / NamedType /
GenericType refers to.  All errors are collected (not thrown eagerly) so the
caller sees the full picture at once.

Usage:
    from .resolver import resolve
    result = resolve(tree)
    if result.errors:
        for e in result.errors: print(e)
    sym = result.get(name_node)  # Symbol | None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from .nodes import (
    File,
    Span,
    NamedType,
    GenericType,
    WrapperType,
    SliceType,
    UnionType,
    InferType,
    SelfType,
    FnType,
    LikeType,
    ValuePattern,
    TypePattern,
    WildcardPattern,
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
    MatchArm,
    IntLiteral,
    FloatLiteral,
    BoolLiteral,
    ByteLiteral,
    StringLiteral,
    ArrayLiteral,
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
    Function,
    Struct,
    Import,
    AsmDecl,
    ExternDecl,
    TopLevelBinding,
    TypeAlias,
)


# ── Built-in type names ───────────────────────────────────────────────────────

BUILTIN_TYPES: frozenset[str] = frozenset(
    {
        "i8",
        "i16",
        "i32",
        "i64",
        "isize",
        "u8",
        "u16",
        "u32",
        "u64",
        "usize",
        "f32",
        "f64",
        "bool",
        "Null",  # sentinel for weak[T] / null pointer
    }
)


# Compile-time intrinsics — recognised by the typechecker and the
# codegen rather than expanded from a body.  Currently:
#   sizeof[T]() -> usize    — substitutes the size of T as a literal
BUILTIN_FNS: frozenset[str] = frozenset(
    {
        "sizeof",
        # Typed memory access through a raw `ptr[u8]` buffer.
        # Used by `std/vec` to read/write Vec[T] elements without
        # introducing source-level pointer arithmetic.  Managed T
        # (rc/arc/weak wrappers) auto-bumps the refcount on both
        # `mem_store` (the storage now holds a new reference) and
        # `mem_load` (the loaded value is an owned copy).
        "mem_load",   # mem_load[T](p, offset) -> T
        "mem_store",  # mem_store[T](p, offset, v: T)
        # Drop the T at `p + offset`.  No-op for unmanaged T;
        # arc_release for rc/arc/weak; direct `__drop__` call for
        # bare struct with drop.  Used by Vec/Map's `__drop__`.
        "drop_at",    # drop_at[T](p: ptr[u8], offset: usize)
        # Comptime file embed.  `embed("path")` returns `[]u8` whose
        # contents are the file at `path` (resolved relative to the
        # importing source file), baked into the binary's data
        # section.  Path must be a string literal.
        "embed",
        # Variadic-fn-body access — `va_start()` opens a va_list
        # (returns an opaque `ptr[u8]` handle); `va_arg[T](ap)`
        # extracts the next variadic argument as `T`.  Only valid
        # inside a `fn name(..., ...)` body.
        "va_start",
        "va_arg",
        # Comptime-specialized hash + equality on K.  Codegen picks
        # the impl per K: Knuth multiplicative for integers / ptrs,
        # FNV-1a for `[]u8`, user-defined `__hash__` / `__eq__` for
        # structs.  Used by `std/map.HashMap`.
        "hash",       # hash[T](v: T) -> u64
        "eq",         # eq[T](a: T, b: T) -> bool
    }
)


# ── Symbols ───────────────────────────────────────────────────────────────────


class SymbolKind(Enum):
    FUNCTION = auto()  # fn name(…)
    STRUCT = auto()  # struct Name
    IMPORT = auto()  # name = import("…")
    MODULE_CONST = auto()  # top-level `name: T = expr`, immutable
    MODULE_VAR = auto()  # top-level `name: var[T] = expr`, mutable global
    LOCAL = auto()  # local binding inside a function
    PARAM = auto()  # regular function parameter
    SELF_PARAM = auto()  # the `self` parameter
    TYPE_PARAM = auto()  # generic type parameter [T]
    BUILTIN_TYPE = auto()  # i32, bool, Null, …
    BUILTIN_FN = auto()  # comptime intrinsics: `sizeof`
    TYPE_ALIAS = auto()  # `Foo: type = ...` (transparent alias)


@dataclass
class Symbol:
    kind: SymbolKind
    name: str
    span: Span
    node: Any  # the defining AST node; None for builtins


# ── Errors ────────────────────────────────────────────────────────────────────


@dataclass
class NameError:
    message: str
    span: Span

    def __str__(self) -> str:
        from .diagnostics import format_diagnostic
        return format_diagnostic(self.message, self.span)


# ── Scope ─────────────────────────────────────────────────────────────────────


class Scope:
    def __init__(self, parent: Optional["Scope"] = None, kind: str = "block") -> None:
        self._bindings: dict[str, Symbol] = {}
        self.parent = parent
        self.kind = kind  # "module" | "struct" | "function" | "block"

    def define(self, sym: Symbol) -> Optional[NameError]:
        """Register a symbol.  Returns a NameError if already defined here."""
        if sym.name in self._bindings:
            prev = self._bindings[sym.name]
            return NameError(
                f"'{sym.name}' already defined at "
                f"{prev.span.start_line}:{prev.span.start_col}",
                sym.span,
            )
        self._bindings[sym.name] = sym
        return None

    def lookup(self, name: str) -> Optional[Symbol]:
        if name in self._bindings:
            return self._bindings[name]
        return self.parent.lookup(name) if self.parent else None


# ── Resolution map ────────────────────────────────────────────────────────────


@dataclass
class ResolutionMap:
    """Maps every resolved Name / NamedType / GenericType node → its Symbol."""

    _refs: dict[int, Symbol] = field(default_factory=dict)
    errors: list[NameError] = field(default_factory=list)

    def record(self, node: Any, sym: Symbol) -> None:
        self._refs[id(node)] = sym

    def get(self, node: Any) -> Optional[Symbol]:
        return self._refs.get(id(node))

    def __len__(self) -> int:
        return len(self._refs)


# ── Resolver ──────────────────────────────────────────────────────────────────


class Resolver:
    def __init__(self) -> None:
        self._map = ResolutionMap()
        self._current_struct: Optional[Struct] = None

    # helpers

    def _err(self, msg: str, span: Span) -> None:
        self._map.errors.append(NameError(msg, span))

    def _define(self, scope: Scope, sym: Symbol) -> None:
        err = scope.define(sym)
        if err is not None:
            self._map.errors.append(err)

    # ── File (two-pass) ──────────────────────────────────────────────────────

    def resolve_file(self, tree: File) -> ResolutionMap:
        module = Scope(kind="module")

        # Seed built-in types directly (bypass duplicate check)
        dummy_span = Span(tree.path, 0, 0, 0, 0)
        for name in BUILTIN_TYPES:
            module._bindings[name] = Symbol(
                SymbolKind.BUILTIN_TYPE, name, dummy_span, None
            )
        # Built-in fns are seeded last so a user-defined top-level fn
        # with the same name (e.g. `string.eq` defined in std/string
        # vs. the `eq[T]` comptime intrinsic) wins.  The intrinsic is
        # still callable as `eq[T](...)` from modules that haven't
        # shadowed it.
        user_fn_names = {
            decl.name for decl in tree.declarations if isinstance(decl, Function)
        }
        for name in BUILTIN_FNS:
            if name in user_fn_names:
                continue
            module._bindings[name] = Symbol(
                SymbolKind.BUILTIN_FN, name, dummy_span, None
            )

        # Pass 1 — register every top-level name so forward references work
        for decl in tree.declarations:
            if isinstance(decl, Function):
                self._define(
                    module, Symbol(SymbolKind.FUNCTION, decl.name, decl.span, decl)
                )
            elif isinstance(decl, Struct):
                self._define(
                    module, Symbol(SymbolKind.STRUCT, decl.name, decl.span, decl)
                )
            elif isinstance(decl, Import):
                self._define(
                    module, Symbol(SymbolKind.IMPORT, decl.binding, decl.span, decl)
                )
            elif isinstance(decl, AsmDecl):
                # An asm decl is callable from Ouro just like a function.
                self._define(
                    module, Symbol(SymbolKind.FUNCTION, decl.name, decl.span, decl)
                )
            elif isinstance(decl, ExternDecl):
                # Externs are callable like normal functions; the codegen
                # routes calls to the bare symbol name (no module prefix).
                self._define(
                    module, Symbol(SymbolKind.FUNCTION, decl.name, decl.span, decl)
                )
            elif isinstance(decl, TopLevelBinding):
                # `name: var[T] = expr` is a mutable global; anything
                # else is a constant.  The distinction matters at
                # write sites (only MODULE_VAR is assignable) and in
                # codegen (MODULE_VAR backs a real data-section slot).
                is_var = (
                    isinstance(decl.type, WrapperType)
                    and decl.type.wrapper == "var"
                )
                kind = (
                    SymbolKind.MODULE_VAR if is_var else SymbolKind.MODULE_CONST
                )
                self._define(module, Symbol(kind, decl.name, decl.span, decl))
            elif isinstance(decl, TypeAlias):
                self._define(
                    module, Symbol(SymbolKind.TYPE_ALIAS, decl.name, decl.span, decl)
                )

        # Pass 2 — resolve bodies
        for decl in tree.declarations:
            if isinstance(decl, Function):
                self._resolve_function(decl, module)
            elif isinstance(decl, Struct):
                self._resolve_struct(decl, module)
            elif isinstance(decl, TopLevelBinding):
                self._resolve_type_opt(decl.type, module)
                self._resolve_expr(decl.value, module)
            elif isinstance(decl, AsmDecl):
                # Resolve the signature types; the body is opaque.
                for p in decl.params:
                    self._resolve_type(p.type, module)
                self._resolve_type_opt(decl.return_type, module)
            elif isinstance(decl, ExternDecl):
                for p in decl.params:
                    self._resolve_type(p.type, module)
                self._resolve_type_opt(decl.return_type, module)
            elif isinstance(decl, TypeAlias):
                # Resolve the alias body in a scope augmented with the
                # alias's own generic params, so `Option[T] = T | Null`
                # finds `T` bound to a TYPE_PARAM rather than erroring.
                alias_scope = Scope(module, kind="type_alias")
                for tp in decl.generics:
                    self._define(
                        alias_scope, Symbol(SymbolKind.TYPE_PARAM, tp, decl.span, decl)
                    )
                self._resolve_type(decl.body, alias_scope)
            # Import has no body

        return self._map

    # ── Types ────────────────────────────────────────────────────────────────

    def _resolve_type(self, typ: Any, scope: Scope) -> None:
        if isinstance(typ, NamedType):
            if typ.name == "Self":
                return  # same as SelfType — valid anywhere SelfType is
            if typ.module is not None:
                # `Foo.Bar` — could be:
                #   (1) a module-qualified type `mod.TypeName`,
                #   (2) enum-variant sugar `Enum.Variant` (current module),
                #   (3) cross-module enum variant `mod.Enum.Variant`,
                #       encoded by the parser as module="mod.Enum".
                if "." in typ.module:
                    # (3) — leave the variant resolution to the
                    # typechecker, which sees the imported module's
                    # type-alias table.  Just verify the leading
                    # name is an import.
                    mod_name = typ.module.split(".", 1)[0]
                    mod_sym = scope.lookup(mod_name)
                    if mod_sym is None or mod_sym.kind != SymbolKind.IMPORT:
                        self._err(
                            f"`{mod_name}` is not an imported module",
                            typ.span,
                        )
                    else:
                        self._map.record(typ, mod_sym)
                    return
                # Try (2) first.
                head = scope.lookup(typ.module)
                if (
                    head is not None
                    and head.kind == SymbolKind.TYPE_ALIAS
                    and isinstance(head.node, TypeAlias)
                    and head.node.enum_variants
                    and typ.name in head.node.enum_variants
                ):
                    mangled = head.node.enum_variants[typ.name]
                    variant_sym = scope.lookup(mangled)
                    if variant_sym is not None:
                        self._map.record(typ, variant_sym)
                        return
                # (1) module-qualified type.
                if head is None or head.kind != SymbolKind.IMPORT:
                    self._err(
                        f"`{typ.module}` is not an imported module",
                        typ.span,
                    )
                else:
                    self._map.record(typ, head)
                return
            sym = scope.lookup(typ.name)
            if sym is None:
                self._err(f"unknown type '{typ.name}'", typ.span)
            else:
                self._map.record(typ, sym)
        elif isinstance(typ, GenericType):
            if typ.module is not None:
                mod_sym = scope.lookup(typ.module)
                if mod_sym is None or mod_sym.kind != SymbolKind.IMPORT:
                    self._err(
                        f"`{typ.module}` is not an imported module",
                        typ.span,
                    )
                else:
                    self._map.record(typ, mod_sym)
            else:
                sym = scope.lookup(typ.base)
                if sym is None:
                    self._err(f"unknown type '{typ.base}'", typ.span)
                else:
                    self._map.record(typ, sym)
            for arg in typ.args:
                self._resolve_type(arg, scope)
        elif isinstance(typ, WrapperType):
            self._resolve_type(typ.inner, scope)
        elif isinstance(typ, SliceType):
            self._resolve_type(typ.element, scope)
        elif isinstance(typ, UnionType):
            for v in typ.variants:
                self._resolve_type(v, scope)
        elif isinstance(typ, FnType):
            for p in typ.params:
                self._resolve_type(p, scope)
            self._resolve_type_opt(typ.return_type, scope)
        elif isinstance(typ, LikeType):
            # Method names inside `like[...]` are surface labels, not
            # symbol references — resolve only the types they mention.
            # In the alias-from form (`like[StructName]`) the bracket
            # body is a type reference; resolve it normally so the
            # typechecker can look up the underlying struct.
            if typ.from_struct is not None:
                self._resolve_type(typ.from_struct, scope)
            for m in typ.methods:
                for p in m.params:
                    self._resolve_type(p, scope)
                self._resolve_type_opt(m.return_type, scope)
        elif isinstance(typ, (InferType, SelfType)):
            pass  # no names inside

    def _resolve_type_opt(self, typ: Any, scope: Scope) -> None:
        if typ is not None:
            self._resolve_type(typ, scope)

    # ── Struct ───────────────────────────────────────────────────────────────

    def _resolve_struct(self, struct: Struct, parent: Scope) -> None:
        prev = self._current_struct
        self._current_struct = struct

        struct_scope = Scope(parent, kind="struct")
        for tp in struct.generics:
            self._define(
                struct_scope, Symbol(SymbolKind.TYPE_PARAM, tp, struct.span, struct)
            )

        for f in struct.fields:
            self._resolve_type(f.type, struct_scope)

        for method in struct.methods:
            self._resolve_function(method, struct_scope)

        self._current_struct = prev

    # ── Function ─────────────────────────────────────────────────────────────

    def _resolve_function(self, fn: Function, parent: Scope) -> None:
        fn_scope = Scope(parent, kind="function")

        for tp in fn.generics:
            self._define(fn_scope, Symbol(SymbolKind.TYPE_PARAM, tp, fn.span, fn))

        if fn.self_param is not None:
            self._resolve_type(fn.self_param.type, fn_scope)
            self._define(
                fn_scope,
                Symbol(
                    SymbolKind.SELF_PARAM, "self", fn.self_param.span, fn.self_param
                ),
            )

        for p in fn.params:
            self._resolve_type(p.type, fn_scope)
            self._define(fn_scope, Symbol(SymbolKind.PARAM, p.name, p.span, p))

        self._resolve_type_opt(fn.return_type, fn_scope)
        self._resolve_block(fn.body, fn_scope)

    # ── Block & statements ───────────────────────────────────────────────────

    def _resolve_block(self, block: Block, parent: Scope) -> None:
        scope = Scope(parent, kind="block")
        for stmt in block.statements:
            self._resolve_stmt(stmt, scope)

    def _resolve_stmt(self, stmt: Any, scope: Scope) -> None:
        if isinstance(stmt, ExprStatement):
            self._resolve_expr(stmt.expr, scope)

        elif isinstance(stmt, Binding):
            # Resolve annotation + value *before* defining the name so that
            # `x = x` correctly refers to the outer `x`, not itself.
            self._resolve_type_opt(stmt.type, scope)
            self._resolve_expr(stmt.value, scope)
            if stmt.name != "_":
                # The parser emits Binding for both fresh bindings and
                # re-assignments to a same-scope var slot.  Same-scope
                # redefinition is therefore allowed here; the type checker
                # decides whether it's a valid re-assignment (var[T]) or
                # an error ("not mutable").
                if stmt.name not in scope._bindings:
                    self._define(
                        scope, Symbol(SymbolKind.LOCAL, stmt.name, stmt.span, stmt)
                    )

        elif isinstance(stmt, Assignment):
            self._resolve_expr(stmt.target, scope)
            self._resolve_expr(stmt.value, scope)

        elif isinstance(stmt, Return):
            if stmt.value is not None:
                self._resolve_expr(stmt.value, scope)

        elif isinstance(stmt, (Pass, Break, Continue)):
            pass

        elif isinstance(stmt, For):
            self._resolve_type_opt(stmt.binding_type, scope)
            self._resolve_expr(stmt.iterable, scope)
            # Loop variable lives only inside the body
            body_scope = Scope(scope, kind="block")
            if stmt.binding != "_":
                self._define(
                    body_scope, Symbol(SymbolKind.LOCAL, stmt.binding, stmt.span, stmt)
                )
            for s in stmt.body.statements:
                self._resolve_stmt(s, body_scope)

        elif isinstance(stmt, Loop):
            self._resolve_block(stmt.body, scope)

        elif isinstance(stmt, Block):
            self._resolve_block(stmt, scope)

    # ── Expressions ──────────────────────────────────────────────────────────

    def _resolve_expr(self, expr: Any, scope: Scope) -> None:
        if isinstance(expr, Name):
            if expr.name in ("_", "self", "Self"):
                # `self` / `Self` as expressions: look up normally
                if expr.name not in ("_",):
                    sym = scope.lookup(expr.name)
                    if sym is None:
                        self._err(f"'{expr.name}' used outside a method", expr.span)
                    else:
                        self._map.record(expr, sym)
                return
            sym = scope.lookup(expr.name)
            if sym is None:
                self._err(f"undefined name '{expr.name}'", expr.span)
            else:
                self._map.record(expr, sym)

        elif isinstance(
            expr,
            (
                IntLiteral,
                FloatLiteral,
                BoolLiteral,
                ByteLiteral,
                StringLiteral,
                Discard,
            ),
        ):
            pass

        elif isinstance(expr, UnaryOp):
            self._resolve_expr(expr.operand, scope)

        elif isinstance(expr, BinaryOp):
            self._resolve_expr(expr.left, scope)
            self._resolve_expr(expr.right, scope)

        elif isinstance(expr, TypeTest):
            self._resolve_expr(expr.operand, scope)
            self._resolve_type(expr.type, scope)

        elif isinstance(expr, Cast):
            self._resolve_expr(expr.operand, scope)
            self._resolve_type(expr.type, scope)

        elif isinstance(expr, FieldAccess):
            self._resolve_expr(expr.obj, scope)
            # field name is resolved by the type checker

        elif isinstance(expr, Index):
            self._resolve_expr(expr.obj, scope)
            self._resolve_expr(expr.index, scope)

        elif isinstance(expr, Range):
            if expr.start is not None:
                self._resolve_expr(expr.start, scope)
            if expr.end is not None:
                self._resolve_expr(expr.end, scope)

        elif isinstance(expr, Call):
            self._resolve_expr(expr.callee, scope)
            for arg in expr.args:
                self._resolve_expr(arg.value, scope)

        elif isinstance(expr, GenericInstantiation):
            self._resolve_expr(expr.base, scope)
            for ta in expr.type_args:
                self._resolve_type(ta, scope)

        elif isinstance(expr, StructLiteral):
            self._resolve_type(expr.type, scope)
            for fi in expr.fields:
                self._resolve_expr(fi.value, scope)

        elif isinstance(expr, ArrayLiteral):
            for el in expr.elements:
                self._resolve_expr(el, scope)

        elif isinstance(expr, If):
            self._resolve_expr(expr.condition, scope)
            self._resolve_block(expr.then_block, scope)
            if expr.else_block is not None:
                self._resolve_block(expr.else_block, scope)

        elif isinstance(expr, Match):
            self._resolve_expr(expr.scrutinee, scope)
            for arm in expr.arms:
                self._resolve_arm(arm, scope)

    def _resolve_arm(self, arm: MatchArm, scope: Scope) -> None:
        arm_scope = Scope(scope, kind="block")

        if isinstance(arm.pattern, ValuePattern):
            self._resolve_expr(arm.pattern.value, scope)
        elif isinstance(arm.pattern, TypePattern):
            self._resolve_type(arm.pattern.type, scope)
            if arm.pattern.binding is not None:
                self._define(
                    arm_scope,
                    Symbol(
                        SymbolKind.LOCAL,
                        arm.pattern.binding,
                        arm.pattern.span,
                        arm.pattern,
                    ),
                )
        elif isinstance(arm.pattern, WildcardPattern):
            pass

        for stmt in arm.body.statements:
            self._resolve_stmt(stmt, arm_scope)


# ── Public API ────────────────────────────────────────────────────────────────


def resolve(tree: File) -> ResolutionMap:
    """Resolve all name references in `tree`.

    Returns a ResolutionMap even when errors are present so callers can
    report all errors at once rather than stopping at the first.
    """
    return Resolver().resolve_file(tree)
