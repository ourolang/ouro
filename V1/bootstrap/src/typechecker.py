"""Type checker for Ouro.

Walks the AST (post name-resolution) and:
  1. Converts every type annotation to a semantic Ty.
  2. Infers / checks the type of every expression.
  3. Verifies assignments, return types, and mutability.

Errors are collected (not thrown eagerly) so the caller sees the full picture.

Usage:
    from .typechecker import typecheck
    result = typecheck(tree, res_map)
    if result.errors:
        for e in result.errors: print(e)
    ty = result.type_of(expr_node)   # Ty | None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from .nodes import (
    Assignment,
    Binding,
    Block,
    BoolLiteral,
    BinaryOp,
    Break,
    ByteLiteral,
    Call,
    Continue,
    ExprStatement,
    FieldAccess,
    File,
    FloatLiteral,
    For,
    Function,
    GenericInstantiation,
    AsmDecl,
    ExternDecl,
    FnType,
    GenericType,
    LikeType,
    If,
    Import,
    Index,
    InferType,
    IntLiteral,
    Loop,
    Match,
    MatchArm,
    Name,
    NamedType,
    Pass,
    Range,
    Return,
    SelfType,
    SliceType,
    Span,
    Struct,
    StructLiteral,
    StringLiteral,
    ArrayLiteral,
    TopLevelBinding,
    TypeAlias,
    TypePattern,
    Cast,
    TypeTest,
    UnaryOp,
    UnionType,
    ValuePattern,
    WrapperType,
)
from .resolver import ResolutionMap, SymbolKind


# ── Semantic type representation ──────────────────────────────────────────────


@dataclass(frozen=True)
class PrimTy:
    name: str  # "i8", "i32", "bool", "Null", …


@dataclass(frozen=True)
class StructTy:
    name: str
    type_args: tuple["Ty", ...] = ()


@dataclass(frozen=True)
class SliceTy:
    element: "Ty"


@dataclass(frozen=True)
class UnionTy:
    variants: frozenset["Ty"]  # normalised: no nested UnionTy


@dataclass(frozen=True)
class WrapperTy:
    wrapper: str  # "var" | "const" | "rc" | "arc" | "weak" | "ptr"
    inner: "Ty"


@dataclass(frozen=True)
class FnTy:
    """First-class function-pointer type: `fn(params) -> return_ty`."""

    params: tuple["Ty", ...]
    return_ty: "Ty"


@dataclass(frozen=True)
class TypeParamTy:
    name: str  # "T", "U", …


@dataclass(frozen=True)
class LikeTy:
    """`like[...]` shape constraint after alias expansion.  Carries
    the listed methods (name → param tys, return ty).  Shapes are
    always open — listing methods only constrains what must be
    present, not what must be absent.  In v1 this only appears
    transiently during fn-sig collection; the collector lifts each
    LikeTy-typed parameter to a fresh implicit generic parameter,
    after which the body and codegen see a plain TypeParamTy."""

    methods: tuple[tuple[str, tuple["Ty", ...], "Ty"], ...]


@dataclass(frozen=True)
class ModuleTy:
    binding: str  # the user's local name for the module (e.g. "io")


@dataclass(frozen=True)
class UnitTy:
    pass


@dataclass(frozen=True)
class NeverTy:
    pass


@dataclass(frozen=True)
class UnknownTy:
    """Stands in where we cannot determine the type (generics, unresolved names)."""


Ty = Union[
    PrimTy,
    StructTy,
    SliceTy,
    UnionTy,
    WrapperTy,
    FnTy,
    TypeParamTy,
    LikeTy,
    ModuleTy,
    UnitTy,
    NeverTy,
    UnknownTy,
]

UNIT: Ty = UnitTy()
NEVER: Ty = NeverTy()
UNKNOWN: Ty = UnknownTy()

_NUMERIC = frozenset(
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
    }
)
_INT = frozenset(
    {"i8", "i16", "i32", "i64", "isize", "u8", "u16", "u32", "u64", "usize"}
)


# ── Ty helpers ────────────────────────────────────────────────────────────────


def _unwrap(ty: Ty) -> Ty:
    """Strip all var/const/rc/arc/weak/ptr wrappers to reach the base type."""
    while isinstance(ty, WrapperTy):
        ty = ty.inner
    return ty


def _readable(ty: Ty) -> Ty:
    """Type produced when reading a binding.  Strips var/const/rc/arc — these
    wrappers describe how a value is held; the read yields the inner type.
    weak[T] and ptr[T] are left alone (weak reads desugar to T | Null
    elsewhere; ptr stays a pointer when read)."""
    if isinstance(ty, WrapperTy) and ty.wrapper in ("var", "const", "rc", "arc"):
        return ty.inner
    return ty


def _unify(param_ty: Ty, arg_ty: Ty, subst: dict[str, Ty]) -> None:
    """Best-effort unification: walk param_ty and arg_ty in parallel; on
    every TypeParamTy in param_ty, record its binding in *subst*. Mismatches
    or shapes we can't unify are ignored (the substitution stays partial).
    """
    if isinstance(param_ty, TypeParamTy):
        subst.setdefault(param_ty.name, arg_ty)
        return
    if isinstance(param_ty, StructTy) and isinstance(arg_ty, StructTy):
        if param_ty.name == arg_ty.name and len(param_ty.type_args) == len(arg_ty.type_args):
            for pa, aa in zip(param_ty.type_args, arg_ty.type_args):
                _unify(pa, aa, subst)
        return
    if isinstance(param_ty, SliceTy) and isinstance(arg_ty, SliceTy):
        _unify(param_ty.element, arg_ty.element, subst)
        return
    if isinstance(param_ty, WrapperTy) and isinstance(arg_ty, WrapperTy):
        if param_ty.wrapper == arg_ty.wrapper:
            _unify(param_ty.inner, arg_ty.inner, subst)
        return


def _subst(ty: Ty, subst: dict[str, Ty]) -> Ty:
    """Apply generic parameter substitutions."""
    if not subst:
        return ty
    if isinstance(ty, TypeParamTy):
        return subst.get(ty.name, ty)
    if isinstance(ty, StructTy) and ty.type_args:
        new_args = tuple(_subst(a, subst) for a in ty.type_args)
        return StructTy(ty.name, new_args) if new_args != ty.type_args else ty
    if isinstance(ty, SliceTy):
        ne = _subst(ty.element, subst)
        return SliceTy(ne) if ne is not ty.element else ty
    if isinstance(ty, UnionTy):
        nv = frozenset(_subst(v, subst) for v in ty.variants)
        return UnionTy(nv) if nv != ty.variants else ty
    if isinstance(ty, WrapperTy):
        ni = _subst(ty.inner, subst)
        return WrapperTy(ty.wrapper, ni) if ni is not ty.inner else ty
    if isinstance(ty, FnTy):
        new_params = tuple(_subst(p, subst) for p in ty.params)
        new_ret = _subst(ty.return_ty, subst)
        if new_params != ty.params or new_ret is not ty.return_ty:
            return FnTy(new_params, new_ret)
        return ty
    return ty


def _fill_unknowns(target: Ty, source: Ty) -> Ty:
    """Walk `target` and `source` in parallel; wherever `target` has an
    `UnknownTy` slot and `source` has a concrete sibling, copy the
    concrete one into `target`.  Used for `?`-placeholder inference at
    binding sites: `rc[HashMap[?, ?]] = HashMap[u64, i32].new()` ends
    up with `rc[HashMap[u64, i32]]` recorded.

    Structural cases that don't line up — e.g. target is `rc[X]` but
    source is bare `X` — leave the target untouched on that branch.
    """
    if isinstance(target, UnknownTy):
        return source
    if isinstance(target, WrapperTy):
        # Bare `wrapper[?]` — plug the *entire* source in, including any
        # wrappers it carries.  `var[?] = rc[HashMap[...]].new()` ends
        # up with `var[rc[HashMap[...]]]`, which is the principled
        # answer for orthogonal wrappers (var wraps storage; rc wraps
        # lifetime).  Peeling here would strip the rc and drop the
        # lifecycle contract.
        if isinstance(target.inner, UnknownTy):
            return WrapperTy(target.wrapper, source)
        # Structural recursion through identical wrappers, or unwrap
        # the source for any storage wrapper (var/const/rc/arc) so
        # `rc[HashMap[?, ?]]` matches a bare `HashMap[u64, i32]` value.
        src = source
        if isinstance(src, WrapperTy):
            if target.wrapper == src.wrapper:
                return WrapperTy(
                    target.wrapper,
                    _fill_unknowns(target.inner, src.inner),
                )
            if src.wrapper in ("var", "const", "rc", "arc"):
                return WrapperTy(
                    target.wrapper,
                    _fill_unknowns(target.inner, src.inner),
                )
        return WrapperTy(target.wrapper, _fill_unknowns(target.inner, src))
    if isinstance(target, StructTy):
        src = _unwrap(source) if isinstance(source, WrapperTy) else source
        if isinstance(src, StructTy) and target.name == src.name and \
                len(target.type_args) == len(src.type_args):
            new_args = tuple(
                _fill_unknowns(t, s)
                for t, s in zip(target.type_args, src.type_args)
            )
            return StructTy(target.name, new_args)
        return target
    if isinstance(target, SliceTy):
        src = _unwrap(source) if isinstance(source, WrapperTy) else source
        if isinstance(src, SliceTy):
            return SliceTy(_fill_unknowns(target.element, src.element))
        return target
    return target


def _ty_to_ast(ty: Ty, span: Span) -> Optional[Any]:
    """Reverse of `_ast_to_ty` for the slice of types we need to
    synthesize when rewriting an AST node (LHS-driven type-arg
    inference).  Returns None for shapes that don't have a clean
    AST representation (LikeTy, FnTy, ModuleTy, etc.) — the caller
    backs out the rewrite in that case.
    """
    from .nodes import (
        NamedType, GenericType, WrapperType, SliceType, UnionType,
    )
    if isinstance(ty, PrimTy):
        return NamedType(span=span, name=ty.name)
    if isinstance(ty, StructTy):
        if not ty.type_args:
            return NamedType(span=span, name=ty.name)
        args: list[Any] = []
        for a in ty.type_args:
            aa = _ty_to_ast(a, span)
            if aa is None:
                return None
            args.append(aa)
        return GenericType(span=span, base=ty.name, args=args)
    if isinstance(ty, WrapperTy):
        inner = _ty_to_ast(ty.inner, span)
        if inner is None:
            return None
        return WrapperType(span=span, wrapper=ty.wrapper, inner=inner)
    if isinstance(ty, SliceTy):
        e = _ty_to_ast(ty.element, span)
        if e is None:
            return None
        return SliceType(span=span, element=e)
    if isinstance(ty, UnionTy):
        vs: list[Any] = []
        for v in ty.variants:
            va = _ty_to_ast(v, span)
            if va is None:
                return None
            vs.append(va)
        return UnionType(span=span, variants=vs)
    return None


def _contains_unknown(ty: Ty) -> bool:
    """True if ty (recursively) contains UnknownTy or TypeParamTy."""
    if isinstance(ty, (UnknownTy, TypeParamTy)):
        return True
    if isinstance(ty, WrapperTy):
        return _contains_unknown(ty.inner)
    if isinstance(ty, SliceTy):
        return _contains_unknown(ty.element)
    if isinstance(ty, UnionTy):
        return any(_contains_unknown(v) for v in ty.variants)
    if isinstance(ty, StructTy):
        return any(_contains_unknown(a) for a in ty.type_args)
    if isinstance(ty, FnTy):
        return any(_contains_unknown(p) for p in ty.params) or _contains_unknown(ty.return_ty)
    return False


def _is_numeric(ty: Ty) -> bool:
    return isinstance(ty, PrimTy) and ty.name in _NUMERIC


def _is_int(ty: Ty) -> bool:
    return isinstance(ty, PrimTy) and ty.name in _INT


def _fmt(ty: Ty) -> str:
    if isinstance(ty, PrimTy):
        return ty.name
    if isinstance(ty, StructTy):
        if ty.type_args:
            args = ", ".join(_fmt(a) for a in ty.type_args)
            return f"{ty.name}[{args}]"
        return ty.name
    if isinstance(ty, SliceTy):
        return f"[]{_fmt(ty.element)}"
    if isinstance(ty, UnionTy):
        return " | ".join(_fmt(v) for v in sorted(ty.variants, key=_fmt))
    if isinstance(ty, WrapperTy):
        return f"{ty.wrapper}[{_fmt(ty.inner)}]"
    if isinstance(ty, FnTy):
        ps = ", ".join(_fmt(p) for p in ty.params)
        return f"fn({ps}) -> {_fmt(ty.return_ty)}"
    if isinstance(ty, TypeParamTy):
        return ty.name
    if isinstance(ty, ModuleTy):
        return f"module({ty.binding})"
    if isinstance(ty, UnitTy):
        return "()"
    if isinstance(ty, NeverTy):
        return "never"
    return "?"


def _make_union(*parts: Ty) -> Ty:
    """Flatten and deduplicate union parts; return singular if only one."""
    variants: set[Ty] = set()
    for p in parts:
        if isinstance(p, UnionTy):
            variants.update(p.variants)
        elif not isinstance(p, (NeverTy, UnknownTy)):
            variants.add(p)
    if not variants:
        return UNIT
    if len(variants) == 1:
        return next(iter(variants))
    return UnionTy(frozenset(variants))


# ── Struct / function metadata ────────────────────────────────────────────────


@dataclass
class FnSig:
    generics: list[str]
    self_ty: Optional[Ty]  # None → static method / free function
    params: list[tuple[str, Ty]]
    return_ty: Ty
    is_variadic: bool = False  # True for `extern f(a, ...)`


@dataclass
class StructInfo:
    name: str
    generics: list[str]
    fields: dict[str, Ty]  # may contain TypeParamTy for generic structs
    method_sigs: dict[str, FnSig]


# ── Error type ────────────────────────────────────────────────────────────────


@dataclass
class TypeError_:
    message: str
    span: Span

    def __str__(self) -> str:
        from .diagnostics import format_diagnostic
        return format_diagnostic(self.message, self.span)


# ── TypeMap (result) ──────────────────────────────────────────────────────────


@dataclass
class TypeMap:
    """Maps every expression node → its Ty, plus collected errors."""

    _types: dict[int, Ty] = field(default_factory=dict)
    errors: list[TypeError_] = field(default_factory=list)

    def record(self, node: Any, ty: Ty) -> None:
        self._types[id(node)] = ty

    def type_of(self, node: Any) -> Optional[Ty]:
        return self._types.get(id(node))

    def __len__(self) -> int:
        return len(self._types)


# ── Typing environment (scope chain) ─────────────────────────────────────────


class TyEnv:
    def __init__(self, parent: Optional["TyEnv"] = None):
        self._locals: dict[str, Ty] = {}
        self.parent = parent

    def define(self, name: str, ty: Ty) -> None:
        self._locals[name] = ty

    def redefine(self, name: str, ty: Ty) -> None:
        """Overwrite (used when narrowing after ?=)."""
        self._locals[name] = ty

    def lookup(self, name: str) -> Optional[Ty]:
        if name in self._locals:
            return self._locals[name]
        return self.parent.lookup(name) if self.parent else None

    def lookup_local(self, name: str) -> Optional[Ty]:
        """Look up only in THIS scope (not parents)."""
        return self._locals.get(name)

    def lookup_enclosing(self, name: str) -> Optional[Ty]:
        """Look up in parent scopes only (not this scope)."""
        return self.parent.lookup(name) if self.parent else None

    def has_local(self, name: str) -> bool:
        return name in self._locals


# ── Checker ───────────────────────────────────────────────────────────────────


class TypeChecker:
    def __init__(
        self,
        res: ResolutionMap,
        module_imports: Optional[dict[str, "TypeChecker"]] = None,
    ) -> None:
        self._res = res
        self._map = TypeMap()
        self._struct_info: dict[str, StructInfo] = {}
        self._fn_sigs: dict[str, FnSig] = {}
        # Enum-synthesized TypeAlias decls, keyed by enum name.  Used
        # by cross-module variant lookup (`mod.Enum.Variant` types).
        self._enum_aliases: dict[str, TypeAlias] = {}
        # All top-level `Foo: type = ...` aliases (including enum
        # synthesizes), keyed by name.  Lets cross-module references
        # (`mod.Value`) transparently expand into the importer's type
        # graph.
        self._type_aliases: dict[str, TypeAlias] = {}
        self._current_self_ty: Optional[Ty] = None
        self._current_return_ty: Ty = UNIT
        # binding (e.g. "math") → that module's already-finished TypeChecker.
        # Missing bindings denote legacy stubs (e.g. `std/io`) — the field
        # access returns UNKNOWN and the codegen handles them specially.
        self._module_imports: dict[str, "TypeChecker"] = module_imports or {}
        self._transitive_imports_cache: Optional[list["TypeChecker"]] = None
        # `like[...]`-derived constraints attached to lifted type-params,
        # keyed by the synth name (e.g. "_LikeT0").  Lets call-on-type-
        # param recover return types from the shape's method list; without
        # this, `c.next()` on a lifted `c: _LikeT0` would record UNKNOWN
        # and codegen would discard the return value.
        self._like_constraints: dict[str, LikeTy] = {}
        self._like_synth_counter: int = 0

    def _all_imports(self) -> list["TypeChecker"]:
        """Transitive imported typecheckers, this one excluded."""
        if self._transitive_imports_cache is not None:
            return self._transitive_imports_cache
        seen: set[int] = set()
        out: list["TypeChecker"] = []
        stack: list["TypeChecker"] = list(self._module_imports.values())
        while stack:
            tc = stack.pop()
            if id(tc) in seen:
                continue
            seen.add(id(tc))
            out.append(tc)
            stack.extend(tc._module_imports.values())
        self._transitive_imports_cache = out
        return out

    # helpers

    def _err(self, msg: str, span: Span) -> None:
        self._map.errors.append(TypeError_(msg, span))

    # ── AST type → Ty ─────────────────────────────────────────────────────────

    def _ast_to_ty(self, ast_type: Any, subst: dict[str, Ty]) -> Ty:
        if ast_type is None:
            return UNIT
        if isinstance(ast_type, NamedType):
            if ast_type.name == "Self":
                return self._current_self_ty or UNKNOWN
            if ast_type.module is not None:
                # `Foo.Bar` — module-qualified, enum-variant sugar,
                # or cross-module enum variant.
                sym = self._res.get(ast_type)
                if sym is None:
                    return UNKNOWN
                # Enum-variant sugar (current module): resolver records
                # the variant's struct symbol directly.
                if sym.kind == SymbolKind.STRUCT:
                    return StructTy(sym.name)
                if sym.kind == SymbolKind.IMPORT:
                    mod = self._module_imports.get(sym.name)
                    if mod is None:
                        return UNKNOWN
                    # Cross-module enum variant: module is "mod.Enum",
                    # name is the variant.  Look up Enum in the imported
                    # module's type-alias table for the mangling.
                    if "." in ast_type.module:
                        _mod_name, enum_name = ast_type.module.split(".", 1)
                        alias_node = mod._enum_aliases.get(enum_name)
                        if (
                            alias_node is not None
                            and ast_type.name in alias_node.enum_variants
                        ):
                            return StructTy(
                                alias_node.enum_variants[ast_type.name]
                            )
                        return UNKNOWN
                    if mod._struct_info.get(ast_type.name) is not None:
                        return StructTy(ast_type.name)
                    # Cross-module type alias: expand transparently.
                    # Delegate the body resolution to the imported
                    # typechecker — the body's NamedTypes were resolved
                    # against *its* ResolutionMap, so `_res.get(...)`
                    # here would return None.
                    alias = mod._type_aliases.get(ast_type.name)
                    if alias is not None and not alias.enum_variants:
                        if alias.generics:
                            return UNKNOWN  # need [args] at the use site
                        return mod._ast_to_ty(alias.body, subst)
                return UNKNOWN
            sym = self._res.get(ast_type)
            if sym is None:
                return UNKNOWN
            if sym.kind == SymbolKind.BUILTIN_TYPE:
                return PrimTy(ast_type.name)
            if sym.kind == SymbolKind.STRUCT:
                return StructTy(ast_type.name)
            if sym.kind == SymbolKind.TYPE_PARAM:
                return subst.get(ast_type.name, TypeParamTy(ast_type.name))
            if sym.kind == SymbolKind.TYPE_ALIAS:
                # Transparent expansion: a non-generic alias just
                # forwards its body's Ty.  Generic-alias use sites
                # are GenericType, handled below.
                alias = sym.node
                assert isinstance(alias, TypeAlias)
                if alias.generics:
                    return UNKNOWN  # need [args]; surface as error elsewhere
                return self._ast_to_ty(alias.body, subst)
            if sym.kind == SymbolKind.IMPORT:
                return ModuleTy(sym.name)
            return UNKNOWN
        if isinstance(ast_type, GenericType):
            args = tuple(self._ast_to_ty(a, subst) for a in ast_type.args)
            # `Option[i32]` may be a generic *alias* (transparent
            # expansion) or a generic *struct* (`Box[i32]`).  Tell
            # them apart by the resolved symbol on the GenericType
            # node — the resolver records both.  Aliases substitute
            # their body with the args; structs keep the `StructTy`
            # shape for specialization.
            base_sym = self._res.get(ast_type)
            if base_sym is not None and base_sym.kind == SymbolKind.TYPE_ALIAS:
                alias = base_sym.node
                assert isinstance(alias, TypeAlias)
                if len(args) != len(alias.generics):
                    return UNKNOWN
                alias_subst = dict(subst)
                alias_subst.update(zip(alias.generics, args))
                return self._ast_to_ty(alias.body, alias_subst)
            return StructTy(ast_type.base, args)
        if isinstance(ast_type, WrapperType):
            if isinstance(ast_type.inner, InferType):
                return WrapperTy(ast_type.wrapper, UNKNOWN)
            return WrapperTy(ast_type.wrapper, self._ast_to_ty(ast_type.inner, subst))
        if isinstance(ast_type, SliceType):
            return SliceTy(self._ast_to_ty(ast_type.element, subst))
        if isinstance(ast_type, UnionType):
            variants: set[Ty] = set()
            for v in ast_type.variants:
                vt = self._ast_to_ty(v, subst)
                if isinstance(vt, UnionTy):
                    variants.update(vt.variants)
                else:
                    variants.add(vt)
            return UnionTy(frozenset(variants))
        if isinstance(ast_type, FnType):
            params = tuple(self._ast_to_ty(p, subst) for p in ast_type.params)
            ret = (
                self._ast_to_ty(ast_type.return_type, subst)
                if ast_type.return_type is not None
                else UNIT
            )
            return FnTy(params, ret)
        if isinstance(ast_type, LikeType):
            if ast_type.from_struct is not None:
                return self._like_from_struct(ast_type.from_struct, subst)
            methods = tuple(
                (
                    m.name,
                    tuple(self._ast_to_ty(p, subst) for p in m.params),
                    self._ast_to_ty(m.return_type, subst)
                    if m.return_type is not None
                    else UNIT,
                )
                for m in ast_type.methods
            )
            return LikeTy(methods=methods)
        if isinstance(ast_type, SelfType):
            return self._current_self_ty or UNKNOWN
        if isinstance(ast_type, InferType):
            return UNKNOWN
        return UNKNOWN

    # ── Struct / function metadata collection ─────────────────────────────────

    def _collect_struct(self, struct: Struct) -> None:
        subst: dict[str, Ty] = {tp: TypeParamTy(tp) for tp in struct.generics}
        prev_self = self._current_self_ty
        self._current_self_ty = (
            StructTy(struct.name, tuple(TypeParamTy(tp) for tp in struct.generics))
            if struct.generics
            else StructTy(struct.name)
        )

        fields: dict[str, Ty] = {}
        for f in struct.fields:
            fields[f.name] = self._ast_to_ty(f.type, subst)

        method_sigs: dict[str, FnSig] = {}
        for m in struct.methods:
            method_sigs[m.name] = self._collect_fn_sig(m, subst)

        self._struct_info[struct.name] = StructInfo(
            name=struct.name,
            generics=struct.generics,
            fields=fields,
            method_sigs=method_sigs,
        )
        self._current_self_ty = prev_self

    def _collect_fn_sig(self, fn: Function, subst: dict[str, Ty]) -> FnSig:
        fn_subst = dict(subst)
        for tp in fn.generics:
            fn_subst[tp] = TypeParamTy(tp)

        self_ty: Optional[Ty] = None
        if fn.self_param is not None:
            self_ty = self._ast_to_ty(fn.self_param.type, fn_subst)

        # Each `like[...]`-typed parameter lifts to a fresh implicit
        # generic param.  We mutate fn.generics and fn.params[i].type
        # so codegen's AST-driven monomorphization sees a uniform
        # `NamedType("_LikeT<n>")` referring to a generic in fn.generics —
        # no special-casing needed downstream.  Only top-level LikeTy is
        # lifted in v1; nested usage stays opaque (UNKNOWN).
        params: list[tuple[str, Ty]] = []
        for p in fn.params:
            pty = self._ast_to_ty(p.type, fn_subst)
            if isinstance(pty, LikeTy):
                synth = f"_LikeT{self._like_synth_counter}"
                self._like_synth_counter += 1
                self._like_constraints[synth] = pty
                fn.generics.append(synth)
                p.type = NamedType(span=p.type.span, name=synth)
                fn_subst[synth] = TypeParamTy(synth)
                pty = TypeParamTy(synth)
            params.append((p.name, pty))

        return_ty = self._ast_to_ty(fn.return_type, fn_subst)
        return FnSig(
            generics=list(fn.generics),
            self_ty=self_ty,
            params=params,
            return_ty=return_ty,
            is_variadic=fn.is_variadic,
        )

    def _like_from_struct(self, from_struct: Type, subst: dict[str, Ty]) -> Ty:
        """Derive a `LikeTy` from a named struct (alias-from form of
        `like[StructName]`).  Methods come from the struct's
        `method_sigs`; the receiver is dropped (shapes elide it), and
        struct-level generic parameters are substituted by the
        from_struct's type arguments.  Lifecycle dunders aren't part
        of the shape.

        Returns `UNKNOWN` if the underlying struct can't be resolved
        yet (forward reference, or the bracket body resolves to a
        non-struct).  The `_collect_fn_sig` lift then declines to
        replace the param, and the body's method calls retain the
        existing bare duck-typed behavior."""
        ty = self._ast_to_ty(from_struct, subst)
        if isinstance(ty, LikeTy):
            return ty
        if not isinstance(ty, StructTy):
            return UNKNOWN
        info = self._struct_info.get(ty.name)
        if info is None:
            for mod in self._all_imports():
                cand = mod._struct_info.get(ty.name)
                if cand is not None:
                    info = cand
                    break
        if info is None:
            return UNKNOWN
        sub: dict[str, Ty] = {}
        if info.generics and ty.type_args:
            sub = dict(zip(info.generics, ty.type_args))
        methods_out: list[tuple[str, tuple[Ty, ...], Ty]] = []
        for name, sig in info.method_sigs.items():
            if name == "__drop__":
                continue
            params = tuple(_subst(p_ty, sub) for _, p_ty in sig.params)
            ret = _subst(sig.return_ty, sub)
            methods_out.append((name, params, ret))
        return LikeTy(methods=tuple(methods_out))

    def _struct_subst(self, struct_ty: StructTy) -> dict[str, Ty]:
        """Build substitution dict from a concrete StructTy and its
        StructInfo.  Walks imports when the struct is foreign — without
        this, cross-module generic methods (`vec.Vec[rc[B]].get`)
        keep their declared `T | Null` return parametric and the
        caller's narrowing can't resolve member access on T."""
        info = self._struct_info.get(struct_ty.name)
        if info is None:
            for mod in self._all_imports():
                cand = mod._struct_info.get(struct_ty.name)
                if cand is not None:
                    info = cand
                    break
        if not info or not info.generics or not struct_ty.type_args:
            return {}
        return dict(zip(info.generics, struct_ty.type_args))

    # ── File entry point (two phases) ─────────────────────────────────────────

    def check_file(self, tree: File) -> TypeMap:
        module_env = TyEnv()

        # Phase 0 — register enum-synthesized aliases so cross-module
        # variant lookups can find them, plus all top-level type
        # aliases for cross-module name resolution.
        for decl in tree.declarations:
            if isinstance(decl, TypeAlias):
                self._type_aliases[decl.name] = decl
                if decl.enum_variants:
                    self._enum_aliases[decl.name] = decl

        # Phase 1 — collect signatures (before checking bodies)
        for decl in tree.declarations:
            if isinstance(decl, Import):
                module_env.define(decl.binding, ModuleTy(decl.binding))
            elif isinstance(decl, Struct):
                self._collect_struct(decl)
                module_env.define(decl.name, StructTy(decl.name))
            elif isinstance(decl, Function):
                sig = self._collect_fn_sig(decl, {})
                self._fn_sigs[decl.name] = sig
                module_env.define(decl.name, sig.return_ty)
            elif isinstance(decl, AsmDecl):
                # Build a FnSig from the asm decl's signature so callers
                # type-check exactly like calls into a regular fn.  No
                # generics, no `self`, no body to walk.
                params = [(p.name, self._ast_to_ty(p.type, {})) for p in decl.params]
                ret_ty = self._ast_to_ty(decl.return_type, {})
                sig = FnSig(generics=[], self_ty=None, params=params, return_ty=ret_ty)
                self._fn_sigs[decl.name] = sig
                module_env.define(decl.name, sig.return_ty)
            elif isinstance(decl, ExternDecl):
                # Externs look like a function to the type checker: a
                # signature, no body.  Variadic externs carry the flag
                # so call-site arity checks let extra args through.
                params = [(p.name, self._ast_to_ty(p.type, {})) for p in decl.params]
                ret_ty = self._ast_to_ty(decl.return_type, {})
                sig = FnSig(generics=[], self_ty=None, params=params, return_ty=ret_ty)
                sig.is_variadic = decl.is_variadic
                self._fn_sigs[decl.name] = sig
                module_env.define(decl.name, sig.return_ty)
            elif isinstance(decl, TopLevelBinding):
                ann_ty = self._ast_to_ty(decl.type, {}) if decl.type else None
                if ann_ty:
                    module_env.define(decl.name, ann_ty)
                # value type resolved in phase 2

        # Phase 2 — check bodies
        for decl in tree.declarations:
            if isinstance(decl, Function):
                sig = self._fn_sigs[decl.name]
                self._check_fn(decl, sig, module_env, {})
            elif isinstance(decl, Struct):
                self._check_struct(decl, module_env)
            elif isinstance(decl, TopLevelBinding):
                val_ty = self._check_expr(decl.value, module_env)
                ann_ty = self._ast_to_ty(decl.type, {}) if decl.type else None
                if (
                    ann_ty
                    and not _contains_unknown(ann_ty)
                    and not _contains_unknown(val_ty)
                ):
                    self._check_assignable(ann_ty, val_ty, decl.span)
                final_ty = (
                    ann_ty if (ann_ty and not isinstance(ann_ty, UnknownTy)) else val_ty
                )
                module_env.define(decl.name, final_ty)

        return self._map

    # ── Struct body ───────────────────────────────────────────────────────────

    def _check_struct(self, struct: Struct, parent_env: TyEnv) -> None:
        prev_self = self._current_self_ty
        self._current_self_ty = (
            StructTy(struct.name, tuple(TypeParamTy(tp) for tp in struct.generics))
            if struct.generics
            else StructTy(struct.name)
        )
        subst: dict[str, Ty] = {tp: TypeParamTy(tp) for tp in struct.generics}

        info = self._struct_info[struct.name]
        for m in struct.methods:
            sig = info.method_sigs[m.name]
            self._check_fn(m, sig, parent_env, subst)

        self._current_self_ty = prev_self

    # ── Function body ─────────────────────────────────────────────────────────

    def _check_fn(
        self,
        fn: Function,
        sig: FnSig,
        parent_env: TyEnv,
        outer_subst: dict[str, Ty],
    ) -> None:
        fn_env = TyEnv(parent_env)
        subst = dict(outer_subst)
        for tp in fn.generics:
            subst[tp] = TypeParamTy(tp)

        if fn.self_param is not None:
            fn_env.define("self", sig.self_ty or UNKNOWN)

        for name, ty in sig.params:
            fn_env.define(name, ty)

        prev_return_ty = self._current_return_ty
        self._current_return_ty = sig.return_ty
        self._check_block(fn.body, fn_env, sig.return_ty)
        self._current_return_ty = prev_return_ty

    # ── Block ─────────────────────────────────────────────────────────────────

    def _check_block(self, block: Block, parent_env: TyEnv, return_ty: Ty) -> None:
        env = TyEnv(parent_env)
        for stmt in block.statements:
            self._check_stmt(stmt, env, return_ty)

    # ── Statements ────────────────────────────────────────────────────────────

    def _check_stmt(self, stmt: Any, env: TyEnv, return_ty: Ty) -> None:
        if isinstance(stmt, ExprStatement):
            self._check_expr(stmt.expr, env)

        elif isinstance(stmt, Binding):
            self._check_binding(stmt, env)

        elif isinstance(stmt, Assignment):
            self._check_assignment(stmt, env)

        elif isinstance(stmt, Return):
            val_ty = UNIT
            if stmt.value is not None:
                val_ty = self._check_expr(stmt.value, env)
            if (
                not _contains_unknown(return_ty)
                and not _contains_unknown(val_ty)
                and not isinstance(val_ty, NeverTy)
                and not self._is_assignable(return_ty, val_ty)
            ):
                self._err(
                    f"return type mismatch: expected {_fmt(return_ty)}, got {_fmt(val_ty)}",
                    stmt.span,
                )

        elif isinstance(stmt, (Pass, Break, Continue)):
            pass

        elif isinstance(stmt, For):
            self._check_for(stmt, env, return_ty)

        elif isinstance(stmt, Loop):
            self._check_block(stmt.body, env, return_ty)

        elif isinstance(stmt, Block):
            self._check_block(stmt, env, return_ty)

    def _check_binding(self, stmt: Binding, env: TyEnv) -> None:
        if stmt.name == "_":
            self._check_expr(stmt.value, env)
            return

        # `name = expr` with no annotation — could be a re-assignment to a
        # var[T] already in scope (the parser always emits Binding for `name = expr`).
        if stmt.type is None:
            # Check current scope first
            local_ty = env.lookup_local(stmt.name)
            if local_ty is not None:
                val_ty = self._check_expr(stmt.value, env)
                if isinstance(local_ty, WrapperTy) and local_ty.wrapper == "var":
                    if not _contains_unknown(local_ty.inner) and not _contains_unknown(
                        val_ty
                    ):
                        self._check_assignable(local_ty.inner, val_ty, stmt.span)
                else:
                    self._err(
                        f"'{stmt.name}' is not mutable (declare as var[T])", stmt.span
                    )
                return

            # Check enclosing scopes for a var[T] to assign into
            outer_ty = env.lookup_enclosing(stmt.name)
            if (
                outer_ty is not None
                and isinstance(outer_ty, WrapperTy)
                and outer_ty.wrapper == "var"
            ):
                val_ty = self._check_expr(stmt.value, env)
                if not _contains_unknown(outer_ty.inner) and not _contains_unknown(
                    val_ty
                ):
                    self._check_assignable(outer_ty.inner, val_ty, stmt.span)
                return

            # Genuinely fresh binding
            val_ty = self._check_expr(stmt.value, env)
            env.define(stmt.name, val_ty)
            return

        ann_ty = self._ast_to_ty(stmt.type, {})

        # LHS-driven inference for `m: T = mod.Struct.method(...)` when
        # `Struct` is generic and the RHS supplied no explicit type
        # args — borrow the concrete args from the annotation by
        # rewriting the call's callee into a `GenericInstantiation`.
        self._infer_call_type_args_from_ann(stmt, ann_ty)

        val_ty = self._check_expr(stmt.value, env)

        # `?` placeholders in the annotation are inference holes —
        # fill them from the RHS's concrete type wherever the
        # structures line up.  Covers shallow (`var[?] = 5`) and
        # nested (`rc[HashMap[?, ?]] = HashMap[u64, i32].new()`)
        # cases through one walker.
        if _contains_unknown(ann_ty):
            ann_ty = _fill_unknowns(ann_ty, val_ty)

        if not _contains_unknown(ann_ty) and not _contains_unknown(val_ty):
            self._check_assignable(_readable(ann_ty), val_ty, stmt.span)
        # `ptr[T] = expr` requires an addressable RHS — a Name or a
        # FieldAccess.  Literals and call results live in temps with no
        # observable address, so refuse them outright; otherwise the
        # codegen silently binds the wrong storage.
        if (
            isinstance(ann_ty, WrapperTy)
            and ann_ty.wrapper == "ptr"
            and not isinstance(stmt.value, (Name, FieldAccess))
        ):
            self._err(
                "`ptr[T] = expr` needs an addressable RHS — a name or field",
                stmt.span,
            )
        env.define(stmt.name, ann_ty)

    def _infer_call_type_args_from_ann(
        self, stmt: Binding, ann_ty: Ty
    ) -> None:
        """If `stmt.value` is `mod.Struct.method(...)` (or
        `Struct.method(...)`) with no explicit `[T, ...]` brackets,
        and `ann_ty` resolves to that same struct *with* type args,
        rewrite the callee in place to add the type args.  This lets
        `m: rc[HashMap[u64, i32]] = HashMap.new()` work — same shape
        as Rust's `let m: HashMap<u64, i32> = HashMap::new();`.
        """
        # Strip storage wrappers from the annotation to find the
        # innermost StructTy with concrete type args.
        t = ann_ty
        while isinstance(t, WrapperTy) and t.wrapper in ("var", "const", "rc", "arc"):
            t = t.inner
        if not isinstance(t, StructTy) or not t.type_args:
            return
        # Check the RHS is a Call whose callee is a static method on a
        # bare struct ref — no GenericInstantiation in the way.
        v = stmt.value
        if not isinstance(v, Call):
            return
        c = v.callee
        if not isinstance(c, FieldAccess):
            return
        # Two shapes:
        #   mod.Struct.method  →  c.obj is FieldAccess(Name(mod), Struct)
        #   Struct.method      →  c.obj is Name(Struct)
        # In both cases the existing c.obj is already resolver-marked
        # (it went through the normal parse + resolve), so we wrap
        # *that* node as the GenericInstantiation's base — don't
        # synthesize a fresh Name, the resolver wouldn't know it.
        if isinstance(c.obj, Name):
            struct_name = c.obj.name
        elif (
            isinstance(c.obj, FieldAccess)
            and isinstance(c.obj.obj, Name)
        ):
            struct_name = c.obj.field
        else:
            return
        if struct_name != t.name:
            return
        ast_args = [_ty_to_ast(a, c.obj.span) for a in t.type_args]
        if any(a is None for a in ast_args):
            return
        c.obj = GenericInstantiation(
            span=c.obj.span,
            base=c.obj,
            type_args=ast_args,
        )

    def _check_assignment(self, stmt: Assignment, env: TyEnv) -> None:
        target_ty = self._check_expr(stmt.target, env)
        val_ty = self._check_expr(stmt.value, env)

        # Mutability: the target must ultimately come from a var[T] binding or
        # a field whose declared type is var[T].
        if not self._lvalue_is_mutable(stmt.target, env):
            self._err("assignment target is not mutable (not var[T])", stmt.span)
            return

        if not _contains_unknown(target_ty) and not _contains_unknown(val_ty):
            base = _readable(target_ty)
            self._check_assignable(base, val_ty, stmt.span)

    def _lvalue_is_mutable(self, expr: Any, env: TyEnv) -> bool:
        """Return True if the lvalue roots in a var[T] binding or var[T] field."""
        if isinstance(expr, Name):
            ty = env.lookup(expr.name)
            return isinstance(ty, WrapperTy) and ty.wrapper == "var"
        if isinstance(expr, FieldAccess):
            obj_ty = _unwrap(self._check_expr(expr.obj, env))
            if isinstance(obj_ty, StructTy):
                info = self._struct_info.get(obj_ty.name)
                if info:
                    field_ty = info.fields.get(expr.field)
                    return isinstance(field_ty, WrapperTy) and field_ty.wrapper == "var"
            return True  # unknown struct — be lenient
        if isinstance(expr, Index):
            return True  # slice element writes require var slice (lenient for now)
        return False

    def _check_for(self, stmt: For, env: TyEnv, return_ty: Ty) -> None:
        iter_ty = self._check_expr(stmt.iterable, env)
        body_env = TyEnv(env)

        # `for i in start..end:` — the loop variable's type is the
        # range's bounds type.  Defaults to `isize` (matching the
        # comptime-literal default) when both ends are bare integer
        # literals.
        from .nodes import Range as _Range
        if isinstance(stmt.iterable, _Range) and stmt.iterable.end is not None:
            elem_ty = self._check_expr(stmt.iterable.end, env)
            if stmt.iterable.start is not None:
                self._check_expr(stmt.iterable.start, env)
            if stmt.binding != "_":
                ann_ty = (
                    self._ast_to_ty(stmt.binding_type, {})
                    if stmt.binding_type
                    else elem_ty
                )
                body_env.define(stmt.binding, ann_ty)
            for s in stmt.body.statements:
                self._check_stmt(s, body_env, return_ty)
            return

        # Infer the element type from the iterator's __next__ return type.
        elem_ty: Ty = UNKNOWN
        inner_iter = _unwrap(iter_ty)
        if isinstance(inner_iter, StructTy):
            info = self._struct_info.get(inner_iter.name)
            if info:
                nxt = info.method_sigs.get("__next__")
                if nxt:
                    nxt_ret = _subst(nxt.return_ty, self._struct_subst(inner_iter))
                    # __next__ returns T | StopIteration; element = first variant
                    if isinstance(nxt_ret, UnionTy):
                        non_stop = [
                            v
                            for v in nxt_ret.variants
                            if not (
                                isinstance(v, StructTy) and v.name == "StopIteration"
                            )
                        ]
                        elem_ty = (
                            non_stop[0]
                            if len(non_stop) == 1
                            else _make_union(*non_stop)
                        )
                    else:
                        elem_ty = nxt_ret

        if stmt.binding != "_":
            ann_ty = (
                self._ast_to_ty(stmt.binding_type, {}) if stmt.binding_type else elem_ty
            )
            body_env.define(stmt.binding, ann_ty)

        for s in stmt.body.statements:
            self._check_stmt(s, body_env, return_ty)

    # ── Expressions ───────────────────────────────────────────────────────────

    def _check_expr(self, expr: Any, env: TyEnv) -> Ty:
        ty = self._infer_expr(expr, env)
        self._map.record(expr, ty)
        return ty

    def _infer_expr(self, expr: Any, env: TyEnv) -> Ty:
        if isinstance(expr, IntLiteral):
            return PrimTy(expr.suffix) if expr.suffix else PrimTy("isize")

        if isinstance(expr, FloatLiteral):
            return PrimTy(expr.suffix) if expr.suffix else PrimTy("f64")

        if isinstance(expr, BoolLiteral):
            return PrimTy("bool")

        if isinstance(expr, ByteLiteral):
            return PrimTy("u8")

        if isinstance(expr, StringLiteral):
            return SliceTy(PrimTy("u8"))

        if isinstance(expr, ArrayLiteral):
            # Element type comes from the first element; subsequent
            # elements must be assignable to it.  Empty `[]` keeps
            # the element type open (UnknownTy) so the surrounding
            # context can narrow it on assignment.
            if not expr.elements:
                return SliceTy(UNKNOWN)
            first_ty = self._check_expr(expr.elements[0], env)
            elem_ty = _readable(first_ty)
            for e in expr.elements[1:]:
                et = self._check_expr(e, env)
                if not self._is_assignable(elem_ty, et):
                    self._err(
                        f"array element type mismatch: expected "
                        f"{_fmt(elem_ty)}, got {_fmt(et)}",
                        e.span,
                    )
            return SliceTy(elem_ty)

        if isinstance(expr, Name):
            return self._infer_name(expr, env)

        if isinstance(expr, FieldAccess):
            return self._infer_field_access(expr, env)

        if isinstance(expr, Index):
            # Strip storage wrappers (var/const/rc/arc) but keep
            # ptr/slice — those decide what kind of indexing happens.
            obj_ty = self._check_expr(expr.obj, env)
            while (
                isinstance(obj_ty, WrapperTy)
                and obj_ty.wrapper in ("var", "const", "rc", "arc")
            ):
                obj_ty = obj_ty.inner
            self._check_expr(expr.index, env)
            # `obj[start..end]` constructs a slice; `obj[i]` reads one
            # element.  Works for `[]T` (element type T) and `ptr[T]`
            # (inner type T).
            if isinstance(expr.index, Range):
                if isinstance(obj_ty, SliceTy):
                    return obj_ty
                if isinstance(obj_ty, WrapperTy) and obj_ty.wrapper == "ptr":
                    return SliceTy(obj_ty.inner)
                return UNKNOWN
            if isinstance(obj_ty, SliceTy):
                return obj_ty.element
            # User-defined indexing via `__getitem__(self, key) -> V`.
            # Codegen lowers `obj[k]` to a method call; here we just
            # return the method's substituted return type so callers
            # see the right shape (e.g. `V | Null` for HashMap).
            if isinstance(obj_ty, StructTy):
                info = self._struct_info.get(obj_ty.name)
                if info is None:
                    for mod in self._all_imports():
                        cand = mod._struct_info.get(obj_ty.name)
                        if cand is not None:
                            info = cand
                            break
                if info is not None and "__getitem__" in info.method_sigs:
                    sig = info.method_sigs["__getitem__"]
                    sub = self._struct_subst(obj_ty)
                    return _subst(sig.return_ty, sub)
            return UNKNOWN

        if isinstance(expr, Range):
            if expr.start:
                self._check_expr(expr.start, env)
            if expr.end:
                self._check_expr(expr.end, env)
            return UNKNOWN  # Range is only used inside Index

        if isinstance(expr, Call):
            return self._infer_call(expr, env)

        if isinstance(expr, GenericInstantiation):
            return self._infer_generic_instantiation(expr, env)

        if isinstance(expr, StructLiteral):
            return self._infer_struct_literal(expr, env)

        if isinstance(expr, BinaryOp):
            return self._infer_binary_op(expr, env)

        if isinstance(expr, UnaryOp):
            operand_ty = self._check_expr(expr.operand, env)
            if expr.op == "not":
                return PrimTy("bool")
            return operand_ty

        if isinstance(expr, TypeTest):
            self._check_expr(expr.operand, env)
            return PrimTy("bool")

        if isinstance(expr, Cast):
            src_ty = self._check_expr(expr.operand, env)
            tgt_ty = self._ast_to_ty(expr.type, {})
            # Allowed casts in v1:
            #   - numeric ↔ numeric (int/float of any width)
            #   - ptr[T] ↔ ptr[U] (raw pointer reinterpret)
            #   - fn(...) ↔ ptr / integer (function pointers are all
            #     `l`-sized; reinterpret as bit pattern)
            # Anything else is a hard error; the user can use a struct
            # method or explicit packing if they need it.
            src_readable = _readable(src_ty)
            src_ok = (
                _is_numeric(_unwrap(src_ty))
                or (
                    isinstance(src_readable, WrapperTy)
                    and src_readable.wrapper == "ptr"
                )
                or isinstance(src_readable, FnTy)
            )
            tgt_ok = (
                _is_numeric(_unwrap(tgt_ty))
                or (isinstance(tgt_ty, WrapperTy) and tgt_ty.wrapper == "ptr")
                or isinstance(tgt_ty, FnTy)
            )
            if not (src_ok and tgt_ok):
                self._err(
                    f"cannot cast {_fmt(src_ty)} → {_fmt(tgt_ty)}; "
                    f"only numeric, ptr[T], and fn(...) casts are "
                    f"supported in v1",
                    expr.span,
                )
            return tgt_ty

        if isinstance(expr, If):
            return self._infer_if(expr, env)

        if isinstance(expr, Match):
            return self._infer_match(expr, env)

        return UNKNOWN

    # ── Name ─────────────────────────────────────────────────────────────────

    def _infer_name(self, expr: Name, env: TyEnv) -> Ty:
        sym = self._res.get(expr)
        if sym is None:
            return UNKNOWN

        if sym.kind in (SymbolKind.PARAM, SymbolKind.LOCAL, SymbolKind.SELF_PARAM):
            ty = env.lookup(expr.name)
            if ty is not None:
                return _readable(ty)
            return UNKNOWN

        if sym.kind in (SymbolKind.MODULE_CONST, SymbolKind.MODULE_VAR):
            ty = env.lookup(expr.name)
            return _readable(ty) if ty else UNKNOWN

        if sym.kind == SymbolKind.FUNCTION:
            sig = self._fn_sigs.get(expr.name)
            if sig is None:
                return UNKNOWN
            # First-class function value: the name evaluates to a
            # function pointer.  `_infer_call` short-circuits the
            # Name-callee path before getting here, so this only
            # fires when the name appears in a non-call context
            # (binding RHS, argument, etc.).
            param_tys = tuple(pty for _name, pty in sig.params)
            return FnTy(param_tys, sig.return_ty)

        if sym.kind == SymbolKind.STRUCT:
            return StructTy(expr.name)

        if sym.kind == SymbolKind.IMPORT:
            return ModuleTy(sym.name)

        if sym.kind == SymbolKind.BUILTIN_TYPE:
            return PrimTy(expr.name)

        return UNKNOWN

    # ── Field access ─────────────────────────────────────────────────────────

    def _infer_field_access(self, expr: FieldAccess, env: TyEnv) -> Ty:
        # Enum dot-access: `EnumName.Variant` constructs an empty
        # variant struct.  Detect this before evaluating obj so we
        # don't spuriously type the bare enum name.
        if isinstance(expr.obj, Name):
            sym = self._res.get(expr.obj)
            if (
                sym is not None
                and sym.kind == SymbolKind.TYPE_ALIAS
                and isinstance(sym.node, TypeAlias)
                and sym.node.enum_variants
                and expr.field in sym.node.enum_variants
            ):
                return StructTy(sym.node.enum_variants[expr.field])

        obj_ty = self._check_expr(expr.obj, env)

        # Peel wrappers but keep track of whether we went through ptr/weak.
        base = _unwrap(obj_ty)

        # Module field access — look up the field in the imported
        # module's symbol tables (only available for real .ou modules;
        # legacy C-runtime stubs return UNKNOWN).
        if isinstance(base, ModuleTy):
            mod = self._module_imports.get(base.binding)
            if mod is None:
                return UNKNOWN
            sig = mod._fn_sigs.get(expr.field)
            if sig is not None:
                # `mod.fn` in expression context evaluates to a
                # function pointer (FnTy), same shape as a bare Name
                # referring to a top-level fn.  Real call sites
                # short-circuit through `_infer_call` before getting
                # here, so this only fires when the field is used as
                # a value (e.g., stored in a struct field of type
                # `fn(...) -> ...`).
                param_tys = tuple(pty for _name, pty in sig.params)
                return FnTy(param_tys, sig.return_ty)
            info = mod._struct_info.get(expr.field)
            if info is not None:
                return StructTy(expr.field)
            return UNKNOWN

        if not isinstance(base, StructTy):
            return UNKNOWN

        # Static method / field lookup when obj is a bare struct type
        # name.  Walks foreign module typecheckers when the struct
        # isn't ours, so cross-module method/field lookups still type.
        info = self._struct_info.get(base.name)
        if info is None:
            for mod in self._all_imports():
                cand = mod._struct_info.get(base.name)
                if cand is not None:
                    info = cand
                    break
        if info is None:
            return UNKNOWN

        sub = self._struct_subst(base)

        # Field lookup
        if expr.field in info.fields:
            ft = info.fields[expr.field]
            ft = _subst(ft, sub)
            # weak[T] read → T | Null
            if isinstance(ft, WrapperTy) and ft.wrapper == "weak":
                inner = _subst(ft.inner, sub)
                return _make_union(inner, PrimTy("Null"))
            # var[T] / const[T] read → T (readable)
            return _readable(ft)

        # Method lookup — return a callable marker (we don't have fn types yet)
        if expr.field in info.method_sigs:
            sig = info.method_sigs[expr.field]
            ret = _subst(sig.return_ty, sub)
            # Self → concrete struct type
            if isinstance(ret, (UnitTy,)) or _contains_unknown(ret):
                if any(
                    isinstance(ret, StructTy) and ret.name == base.name for _ in [None]
                ):
                    return ret
            return ret if not isinstance(ret, UnknownTy) else UNKNOWN

        return UNKNOWN

    # ── Call ─────────────────────────────────────────────────────────────────

    def _infer_call(self, expr: Call, env: TyEnv) -> Ty:
        # Evaluate all args once and remember their inferred types so we
        # can compare them against the callee's signature below.
        arg_types: list[Ty] = [self._check_expr(arg.value, env) for arg in expr.args]

        callee = expr.callee

        if isinstance(callee, FieldAccess):
            obj_ty = self._check_expr(callee.obj, env)
            base = _unwrap(obj_ty)

            # Method call on a lifted `like[...]` type-param: look up
            # the method in the constraint's shape so the call records
            # a concrete return type instead of UNKNOWN.  Codegen's
            # monomorphization picks the concrete impl by name.
            if isinstance(base, TypeParamTy):
                constraint = self._like_constraints.get(base.name)
                if constraint is not None:
                    for mname, _mparams, mret in constraint.methods:
                        if mname == callee.field:
                            return mret

            if isinstance(base, StructTy):
                info = self._struct_info.get(base.name)
                if info is None:
                    for mod in self._all_imports():
                        cand = mod._struct_info.get(base.name)
                        if cand is not None:
                            info = cand
                            break
                if info and callee.field in info.method_sigs:
                    sig = info.method_sigs[callee.field]
                    sub = self._struct_subst(base)
                    self._check_call_signature(expr, arg_types, sig, sub)
                    ret = _subst(sig.return_ty, sub)
                    # Self → concrete struct type
                    if isinstance(ret, UnknownTy):
                        return UNKNOWN
                    if isinstance(ret, StructTy) and ret.name == "Self":
                        return base
                    return ret
                # Indirect call through a fn-typed struct field:
                # `self._hash(k)` where `_hash: fn(K) -> u64`.
                if info and callee.field in info.fields:
                    ft = info.fields[callee.field]
                    ft = _subst(_readable(ft), self._struct_subst(base))
                    if isinstance(ft, FnTy):
                        # Record the callee's type so the codegen can
                        # see FnTy on the FieldAccess and route to its
                        # indirect-call path.
                        self._map.record(callee, ft)
                        return ft.return_ty
            # Real-module call (`math.foo(...)`): look up the fn in the
            # imported module's signatures.  Legacy stubs (no entry in
            # _module_imports) fall through to UNKNOWN.
            if isinstance(base, ModuleTy):
                mod = self._module_imports.get(base.binding)
                if mod is not None:
                    sig = mod._fn_sigs.get(callee.field)
                    if sig is not None:
                        if sig.generics:
                            # Cross-module generic-fn call: unify args
                            # against the declared params (skipping
                            # unknowns) so the recorded return type is
                            # concrete.  The codegen specializes the
                            # body in the callee module's worklist.
                            subst: dict[str, Ty] = {}
                            for (_pname, pty), aty in zip(sig.params, arg_types):
                                _unify(pty, aty, subst)
                            return _subst(sig.return_ty, subst)
                        self._check_call_signature(expr, arg_types, sig, {})
                        return sig.return_ty
            # Legacy stub / unknown module member.
            return UNKNOWN

        if isinstance(callee, Name):
            # Bare-name intrinsics inferring T from arg types.
            if callee.name == "hash" and len(arg_types) == 1:
                return PrimTy("u64")
            if callee.name == "eq" and len(arg_types) == 2:
                return PrimTy("bool")
            # Built-in comptime intrinsics that take value args (no
            # type-arg brackets).  `embed("path") -> []u8` is the
            # only such intrinsic today; the others (`sizeof`,
            # `mem_load`, `mem_store`, `drop_at`) all take a `[T]`
            # type arg and go through the `GenericInstantiation`
            # branch below.
            if callee.name == "embed":
                for a in expr.args:
                    self._check_expr(a.value, env)
                return SliceTy(PrimTy("u8"))
            if callee.name == "va_start":
                # Returns an opaque ptr[u8] (the va_list handle).
                return WrapperTy("ptr", PrimTy("u8"))
            sig = self._fn_sigs.get(callee.name)
            if sig:
                self._check_call_signature(expr, arg_types, sig, {})
                if sig.generics:
                    # Infer the substitution from the arg types so the
                    # call's recorded type is concrete (TypeMap consumers,
                    # like codegen, need a concrete width).
                    subst: dict[str, Ty] = {}
                    for (_pname, pty), aty in zip(sig.params, arg_types):
                        _unify(pty, aty, subst)
                    return _subst(sig.return_ty, subst)
                return sig.return_ty

        # Explicit-type-args call to a generic fn: `id[i32](42)`.
        if isinstance(callee, GenericInstantiation) and isinstance(callee.base, Name):
            # Comptime intrinsics — `sizeof[T]()`, `mem_load[T]`,
            # `mem_store[T]`.  Recognised by name rather than via
            # `_fn_sigs`, since they have no Ouro body.
            if callee.base.name == "sizeof":
                for ta in callee.type_args:
                    self._ast_to_ty(ta, {})
                return PrimTy("usize")
            if callee.base.name == "mem_load" and len(callee.type_args) == 1:
                t = self._ast_to_ty(callee.type_args[0], {})
                return t
            if callee.base.name == "mem_store":
                # Returns unit; type args validated for resolvability.
                for ta in callee.type_args:
                    self._ast_to_ty(ta, {})
                return UNIT
            if callee.base.name == "drop_at":
                for ta in callee.type_args:
                    self._ast_to_ty(ta, {})
                return UNIT
            if callee.base.name == "va_arg" and len(callee.type_args) == 1:
                # Returns the extracted variadic arg as the type T.
                t = self._ast_to_ty(callee.type_args[0], {})
                for a in expr.args:
                    self._check_expr(a.value, env)
                return t
            if callee.base.name == "hash" and len(callee.type_args) == 1:
                self._ast_to_ty(callee.type_args[0], {})
                for a in expr.args:
                    self._check_expr(a.value, env)
                return PrimTy("u64")
            if callee.base.name == "eq" and len(callee.type_args) == 1:
                self._ast_to_ty(callee.type_args[0], {})
                for a in expr.args:
                    self._check_expr(a.value, env)
                return PrimTy("bool")
            sig = self._fn_sigs.get(callee.base.name)
            if sig and sig.generics and len(callee.type_args) == len(sig.generics):
                subst = {
                    tp: self._ast_to_ty(ta, {})
                    for tp, ta in zip(sig.generics, callee.type_args)
                }
                self._check_call_signature(expr, arg_types, sig, subst)
                return _subst(sig.return_ty, subst)
            # No matching generic-fn sig — return UNKNOWN (e.g. could
            # be a generic struct method call, handled elsewhere).
            return UNKNOWN

        # Cross-module generic-fn instantiation: `mod.fn[T](...)`.
        if (
            isinstance(callee, GenericInstantiation)
            and isinstance(callee.base, FieldAccess)
            and isinstance(callee.base.obj, Name)
        ):
            inner = callee.base
            obj_ty = self._check_expr(inner.obj, env)
            if isinstance(obj_ty, ModuleTy):
                mod = self._module_imports.get(obj_ty.binding)
                if mod is not None:
                    sig = mod._fn_sigs.get(inner.field)
                    if (
                        sig
                        and sig.generics
                        and len(callee.type_args) == len(sig.generics)
                    ):
                        subst = {
                            tp: self._ast_to_ty(ta, {})
                            for tp, ta in zip(sig.generics, callee.type_args)
                        }
                        return _subst(sig.return_ty, subst)
            return UNKNOWN

        # Any other callee — could be a value of `FnTy` (a function
        # pointer in a binding or field).  Type-check and use the
        # callee's `FnTy.return_ty` if applicable.
        callee_ty = self._check_expr(callee, env)
        if isinstance(callee_ty, FnTy):
            return callee_ty.return_ty
        return UNKNOWN

    def _check_call_signature(
        self,
        expr: Call,
        arg_types: list[Ty],
        sig: FnSig,
        subst: dict[str, Ty],
    ) -> None:
        """Verify each positional arg is assignable to the corresponding
        parameter type, and that arity matches.

        Skipped silently for:
          - any named argument (we don't model name → param matching yet)
          - generic callees (deferred to monomorphization)
        """
        if any(arg.name is not None for arg in expr.args):
            return
        if sig.generics:
            return

        n_params = len(sig.params)
        n_args = len(arg_types)
        if sig.is_variadic:
            if n_args < n_params:
                self._err(
                    f"wrong number of arguments: expected at least "
                    f"{n_params}, got {n_args}",
                    expr.span,
                )
                return
        elif n_args != n_params:
            self._err(
                f"wrong number of arguments: expected {n_params}, got {n_args}",
                expr.span,
            )
            return

        for i, (arg_ty, (pname, pty)) in enumerate(zip(arg_types, sig.params)):
            pty_subbed = _subst(pty, subst)
            if _contains_unknown(arg_ty) or _contains_unknown(pty_subbed):
                continue
            target = _readable(pty_subbed)
            if not self._is_assignable(target, arg_ty):
                self._err(
                    f"argument {i + 1} (`{pname}`): expected "
                    f"{_fmt(target)}, got {_fmt(arg_ty)}",
                    expr.args[i].value.span,
                )

    # ── GenericInstantiation ─────────────────────────────────────────────────

    def _infer_generic_instantiation(
        self, expr: GenericInstantiation, env: TyEnv
    ) -> Ty:
        # `Name[T1, T2]` — produce a StructTy with concrete type args
        base_ty = self._check_expr(expr.base, env)

        # Ambiguity from parsing: `a[i]` lexes as generic instantiation
        # even when `a` is a slice-typed value and `i` is a local int.
        # If the base resolves to a slice type AND we got exactly one
        # arg that's a plain name bound in scope, treat it as an
        # index and return the element type.
        if (
            isinstance(_unwrap(base_ty), SliceTy)
            and len(expr.type_args) == 1
            and isinstance(expr.type_args[0], NamedType)
        ):
            inner = _unwrap(base_ty)
            assert isinstance(inner, SliceTy)
            arg_name = expr.type_args[0].name
            if env.lookup(arg_name) is not None:
                return inner.element

        type_args = tuple(self._ast_to_ty(ta, {}) for ta in expr.type_args)

        if isinstance(base_ty, StructTy):
            return StructTy(base_ty.name, type_args)

        # When used as `LinkedList[i32].new()`, the base resolves to StructTy
        # from the Name lookup; apply type args.
        if isinstance(expr.base, Name):
            sym = self._res.get(expr.base)
            if sym and sym.kind == SymbolKind.STRUCT:
                return StructTy(expr.base.name, type_args)

        return UNKNOWN

    # ── StructLiteral ─────────────────────────────────────────────────────────

    def _infer_struct_literal(self, expr: StructLiteral, env: TyEnv) -> Ty:
        struct_ty = self._ast_to_ty(expr.type, {})
        for fi in expr.fields:
            self._check_expr(fi.value, env)
        return struct_ty

    # ── Binary ops ───────────────────────────────────────────────────────────

    def _infer_binary_op(self, expr: BinaryOp, env: TyEnv) -> Ty:
        left = self._check_expr(expr.left, env)
        right = self._check_expr(expr.right, env)

        op = expr.op
        if op in ("==", "!=", "<", ">", "<=", ">="):
            return PrimTy("bool")
        if op in ("and", "or"):
            return PrimTy("bool")

        # Arithmetic / bitwise — return the more-specific numeric type
        if op in ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"):
            if _is_numeric(left) and _is_numeric(right):
                assert isinstance(left, PrimTy) and isinstance(right, PrimTy)
                # prefer the non-default type
                if left.name == "isize" and right.name != "isize":
                    return right
                return left
            if _is_numeric(left):
                return left
            if _is_numeric(right):
                return right

        return UNKNOWN

    # ── If expression ─────────────────────────────────────────────────────────

    def _infer_if(self, expr: If, env: TyEnv) -> Ty:
        # Narrow: if `cond` is a TypeTest `x ?= T`, bind x to the
        # complement type in the else branch and T in the then branch.
        # `and`-chains carry narrowings forward — `x ?= T and x.field`
        # checks the right operand with x already narrowed to T.  The
        # else-branch complement only fires for the lone-TypeTest shape;
        # NOT (a and b) isn't a single-name narrowing.
        then_env = TyEnv(env)
        else_env = TyEnv(env)

        if isinstance(expr.condition, TypeTest):
            self._check_expr(expr.condition, env)
            test = expr.condition
            if isinstance(test.operand, Name):
                name = test.operand.name
                original_ty = env.lookup(name)
                test_ty = self._ast_to_ty(test.type, {})
                if original_ty is not None:
                    then_env.redefine(name, test_ty)
                    complement = _subtract_ty(original_ty, test_ty)
                    else_env.redefine(name, complement)
        else:
            # Walk `and`-chains, applying TypeTest narrowings to a
            # progressively-narrowed then_env so later operands
            # typecheck against the narrowed name.  Anything that
            # isn't a TypeTest/and gets typechecked in the current
            # then_env.  No else-branch narrowing in the general case.
            self._narrow_and_typecheck(expr.condition, then_env, env)

        self._check_block_in_env(expr.then_block, then_env)
        if expr.else_block is not None:
            self._check_block_in_env(expr.else_block, else_env)

        return UNKNOWN  # if-as-expression type inference deferred

    def _narrow_and_typecheck(
        self, cond: Any, then_env: TyEnv, orig_env: TyEnv
    ) -> None:
        """Walk an `and`-chained condition.  TypeTest operands narrow
        the matching `Name` in `then_env`; other operands get a regular
        `_check_expr` in `then_env` so they see prior narrowings.
        """
        if isinstance(cond, BinaryOp) and cond.op == "and":
            self._narrow_and_typecheck(cond.left, then_env, orig_env)
            self._narrow_and_typecheck(cond.right, then_env, orig_env)
            return
        if isinstance(cond, TypeTest) and isinstance(cond.operand, Name):
            self._check_expr(cond.operand, then_env)
            name = cond.operand.name
            original_ty = then_env.lookup(name) or orig_env.lookup(name)
            test_ty = self._ast_to_ty(cond.type, {})
            if original_ty is not None:
                then_env.redefine(name, test_ty)
            # Record the test expr itself so codegen knows it's a bool.
            self._map.record(cond, PrimTy("bool"))
            return
        self._check_expr(cond, then_env)

    def _check_block_in_env(self, block: Block, env: TyEnv) -> None:
        """Check block statements using an already-created env (no extra nesting)."""
        for stmt in block.statements:
            self._check_stmt(stmt, env, self._current_return_ty)

    # ── Match expression ──────────────────────────────────────────────────────

    def _infer_match(self, expr: Match, env: TyEnv) -> Ty:
        self._check_expr(expr.scrutinee, env)
        for arm in expr.arms:
            self._check_arm(arm, env)
        return UNKNOWN

    def _check_arm(self, arm: MatchArm, env: TyEnv) -> None:
        arm_env = TyEnv(env)
        if isinstance(arm.pattern, ValuePattern):
            self._check_expr(arm.pattern.value, env)
        elif isinstance(arm.pattern, TypePattern):
            if arm.pattern.binding:
                bound_ty = self._ast_to_ty(arm.pattern.type, {})
                arm_env.define(arm.pattern.binding, bound_ty)
        # WildcardPattern: nothing to do
        for stmt in arm.body.statements:
            self._check_stmt(stmt, arm_env, self._current_return_ty)

    # ── Assignability ─────────────────────────────────────────────────────────

    def _is_assignable(self, target: Ty, source: Ty) -> bool:
        """Is `source` assignable to a slot of type `target`?"""
        if target == source:
            return True
        if isinstance(target, UnknownTy) or isinstance(source, UnknownTy):
            return True
        if isinstance(source, NeverTy):
            return True

        # Numeric widening: allow any numeric → numeric with same sign class
        # (lenient for comptime literals; v1 does not track literal narrowness)
        if _is_numeric(target) and _is_numeric(source):
            return True

        # Union: source must be a member of (or assignable to a member of) target.
        # An `rc[Union]`/`arc[Union]`/`var[Union]` source unwraps to its
        # inner Union first so the variant-set check fires correctly.
        if isinstance(target, UnionTy):
            unwrapped = source
            while (
                isinstance(unwrapped, WrapperTy)
                and unwrapped.wrapper in ("var", "const", "rc", "arc")
            ):
                unwrapped = unwrapped.inner
            if isinstance(unwrapped, UnionTy):
                return all(
                    any(self._is_assignable(tv, sv) for tv in target.variants)
                    for sv in unwrapped.variants
                )
            return any(self._is_assignable(v, unwrapped) for v in target.variants)

        # Slice: covariant element (lenient for v1)
        if isinstance(target, SliceTy) and isinstance(source, SliceTy):
            return self._is_assignable(target.element, source.element)

        # Function pointers: structural equality (same arity + each
        # param assignable + return assignable).  Lenient for v1.
        if isinstance(target, FnTy) and isinstance(source, FnTy):
            if len(target.params) != len(source.params):
                return False
            for tp, sp in zip(target.params, source.params):
                if not self._is_assignable(tp, sp):
                    return False
            return self._is_assignable(target.return_ty, source.return_ty)

        # Strip var/const wrappers when checking inner compatibility
        if isinstance(target, WrapperTy) and isinstance(source, WrapperTy):
            if target.wrapper == source.wrapper:
                return self._is_assignable(target.inner, source.inner)
        if isinstance(target, WrapperTy):
            return self._is_assignable(target.inner, source)
        # Symmetrically: reading a var/const/rc/arc source sheds the
        # wrapper, so `arc[R] → R` (and `var[i32] → i32`) flow naturally.
        if isinstance(source, WrapperTy) and source.wrapper in (
            "var",
            "const",
            "rc",
            "arc",
        ):
            return self._is_assignable(target, source.inner)

        # Null can go into any union that contains Null
        if isinstance(source, PrimTy) and source.name == "Null":
            if isinstance(target, UnionTy):
                return PrimTy("Null") in target.variants

        return False

    def _check_assignable(self, target: Ty, source: Ty, span: Span) -> None:
        if not self._is_assignable(target, source):
            self._err(
                f"type mismatch: expected {_fmt(target)}, got {_fmt(source)}",
                span,
            )


# ── Type subtraction (for union narrowing) ────────────────────────────────────


def _subtract_ty(original: Ty, to_remove: Ty) -> Ty:
    """Remove `to_remove` variant(s) from `original` (for else-branch narrowing)."""
    if isinstance(original, UnionTy):
        removed: frozenset[Ty] = (
            to_remove.variants
            if isinstance(to_remove, UnionTy)
            else frozenset({to_remove})
        )
        remaining = original.variants - removed
        if not remaining:
            return UNKNOWN
        if len(remaining) == 1:
            return next(iter(remaining))
        return UnionTy(frozenset(remaining))
    return original


# ── Public API ────────────────────────────────────────────────────────────────


def typecheck(tree: File, res: ResolutionMap) -> TypeMap:
    """Type-check `tree` using the already-computed name resolution map.

    Returns a TypeMap even when errors are present.
    """
    return TypeChecker(res).check_file(tree)
