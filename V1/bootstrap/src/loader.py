"""Module loader — turn an entry `.ou` file plus its `import("...")`
declarations into a topologically-ordered list of compiled modules.

The loader runs lex+parse+resolve+typecheck per imported module before
returning. Codegen is the caller's job; the loader's output is what
`generate_program(modules)` consumes.

🟡 *In v1:*
- Relative imports `./X` and `../X/Y` resolve to user `.ou` files
- `std/X` resolves to a `.ou` file bundled with the compiler under
  `<repo>/std/X.ou`.  When the bundled file is missing, the import
  falls through to the legacy C-runtime stub path (kept for
  compatibility with the historic `io.println` etc. that pre-date
  the Ouro-side runtime migration).
- Cycle detection: a module appearing twice on the loading stack is
  an error
- Cross-module generic free functions are wired up via a
  fixed-point spec drain in `compile_program`: the caller registers
  a spec in the callee module's `_fn_specs`, and the driver loops
  over every codegen until no module has pending specs.  Cross-
  module generic *struct* methods are still single-module only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .codegen import Codegen
from .lexer import lex
from .nodes import (
    Argument,
    ArrayLiteral,
    Assignment,
    Block,
    Call,
    EnumDecl,
    ExprStatement,
    FieldAccess,
    File,
    Function,
    GenericInstantiation,
    GenericType,
    If,
    Import,
    Match,
    MatchArm,
    Name,
    NamedType,
    Parameter,
    Pass,
    Return,
    SelfParam,
    SelfType,
    SliceType,
    Span,
    StringLiteral,
    Struct,
    StructLiteral,
    TypeAlias,
    TypeTest,
    UnionType,
    ValuePattern,
    WildcardPattern,
    WrapperType,
)
from .parser import parse
from .resolver import ResolutionMap, resolve
from .typechecker import TypeChecker, TypeMap


def _desugar_enums(tree: File) -> None:
    """Replace each `EnumDecl` in *tree* with:
       - One empty `Struct` per variant, named `EnumName__VariantName`.
       - A `TypeAlias` `EnumName: type = V1 | V2 | ...` carrying the
         variant-name → mangled-struct-name map for dot-access sugar.
    Mutates *tree* in place.  Runs before the resolver so downstream
    passes see only structs and aliases — no special enum-awareness
    needed in resolver/typechecker beyond the dot-access lookup."""
    new_decls: list = []
    for decl in tree.declarations:
        if not isinstance(decl, EnumDecl):
            new_decls.append(decl)
            continue
        variant_map: dict[str, str] = {}
        variant_tys: list = []
        for v in decl.variants:
            mangled = f"{decl.name}__{v}"
            variant_map[v] = mangled
            new_decls.append(Struct(
                span=decl.span,
                name=mangled,
                generics=[],
                fields=[],
                methods=[],
            ))
            variant_tys.append(NamedType(span=decl.span, name=mangled))
        if not variant_tys:
            # Empty enum — still emit a type alias to a non-instantiable
            # union (just NamedType to a synthesized "never" struct).
            sentinel = f"{decl.name}__Empty"
            new_decls.append(Struct(
                span=decl.span,
                name=sentinel,
                generics=[],
                fields=[],
                methods=[],
            ))
            variant_tys.append(NamedType(span=decl.span, name=sentinel))
        body = (
            variant_tys[0] if len(variant_tys) == 1
            else UnionType(span=decl.span, variants=variant_tys)
        )
        new_decls.append(TypeAlias(
            span=decl.span,
            name=decl.name,
            generics=[],
            body=body,
            enum_variants=variant_map,
        ))
    tree.declarations = new_decls


_VALUE_VARIANT_PRIMS = frozenset({
    "i8", "i16", "i32", "i64",
    "u8", "u16", "u32", "u64",
    "isize", "usize",
    "f32", "f64",
    "bool",
})


def _field_maps_to_value(field_type) -> bool:
    """True when the field's type is one of `Value`'s variants — every
    primitive in `_VALUE_VARIANT_PRIMS`, the `Null` sentinel, or a
    `[]u8` byte slice.  Other types (structs, rc/arc wrappers, non-u8
    slices) get filed under the wildcard arm in the synthesized
    `__get__`, which returns `Null`.
    """
    if isinstance(field_type, NamedType) and field_type.module is None:
        return field_type.name in _VALUE_VARIANT_PRIMS or field_type.name == "Null"
    if isinstance(field_type, SliceType):
        elem = field_type.element
        return (
            isinstance(elem, NamedType)
            and elem.module is None
            and elem.name == "u8"
        )
    return False


def _find_value_binding(tree: File) -> Optional[str]:
    """Return the local binding the user gave to `std/value`, or None
    when this module doesn't import it.  Synthesis is opt-in: importing
    `std/value` enables compiler-generated `__get__` on this module's
    structs.  The binding name (`value`, `val`, etc.) is whatever the
    user wrote, so the synthesized return type tracks it.
    """
    for decl in tree.declarations:
        if isinstance(decl, Import) and decl.path == "std/value":
            return decl.binding
    return None


def _find_map_binding(tree: File) -> Optional[str]:
    """Return the local binding for `std/map`, or None.  `to_map`
    synthesis needs both `std/map` and `std/value` in scope; when map
    isn't imported, the per-struct `to_map` is omitted."""
    for decl in tree.declarations:
        if isinstance(decl, Import) and decl.path == "std/map":
            return decl.binding
    return None


def _synthesize_get_dunder(tree: File) -> None:
    """For each `Struct` in *tree* without a user-defined `__get__`,
    synthesize one that dispatches by field name to a `value.Value`.
    Same handling for `__set__` (writes via `var[T]` fields).  No-op
    when the module doesn't import `std/value` — synthesis is opt-in
    via the import.  User-defined methods always win.
    """
    value_binding = _find_value_binding(tree)
    if value_binding is None:
        return
    map_binding = _find_map_binding(tree)
    for decl in tree.declarations:
        if not isinstance(decl, Struct):
            continue
        if not any(m.name == "__get__" for m in decl.methods):
            decl.methods.append(_make_get_dunder(decl, value_binding))
        if not any(m.name == "__set__" for m in decl.methods):
            decl.methods.append(_make_set_dunder(decl, value_binding))
        if not any(m.name == "__fields__" for m in decl.methods):
            decl.methods.append(_make_fields_dunder(decl))
        if (
            map_binding is not None
            and not any(m.name == "to_map" for m in decl.methods)
        ):
            decl.methods.append(_make_to_map(decl, value_binding, map_binding))


def _unwrap_var(ty):
    """Peel a single `var[T]` wrapper, returning the inner type; pass
    through anything else.  Used by `__set__` synthesis — only var-
    fields are writable, and we want the inner type for narrowing."""
    if isinstance(ty, WrapperType) and ty.wrapper == "var":
        return ty.inner
    return ty


def _make_get_dunder(struct: Struct, value_binding: str) -> Function:
    span = struct.span
    arms: list[MatchArm] = []
    for f in struct.fields:
        if not _field_maps_to_value(f.type):
            continue
        arms.append(MatchArm(
            span=span,
            pattern=ValuePattern(
                span=span,
                value=StringLiteral(
                    span=span,
                    value=f.name.encode("utf-8"),
                    is_multiline=False,
                ),
            ),
            body=Block(span=span, statements=[
                Return(
                    span=span,
                    value=FieldAccess(
                        span=span,
                        obj=Name(span=span, name="self"),
                        field=f.name,
                    ),
                ),
            ]),
        ))
    # Wildcard fallback — return Null for unknown or unsupported fields.
    arms.append(MatchArm(
        span=span,
        pattern=WildcardPattern(span=span),
        body=Block(span=span, statements=[
            Return(
                span=span,
                value=StructLiteral(
                    span=span,
                    type=NamedType(span=span, name="Null"),
                    fields=[],
                ),
            ),
        ]),
    ))
    body = Block(span=span, statements=[
        ExprStatement(
            span=span,
            expr=Match(
                span=span,
                scrutinee=Name(span=span, name="name"),
                arms=arms,
            ),
        ),
    ])
    return Function(
        span=span,
        name="__get__",
        generics=[],
        self_param=SelfParam(
            span=span,
            type=SelfType(span=span),
            is_default=True,
        ),
        params=[Parameter(
            span=span,
            name="name",
            type=SliceType(
                span=span,
                element=NamedType(span=span, name="u8"),
            ),
        )],
        return_type=NamedType(
            span=span,
            name="Value",
            module=value_binding,
        ),
        body=body,
        is_variadic=False,
    )


def _make_fields_dunder(struct: Struct) -> Function:
    """Synthesize `__fields__(self) -> [][]u8` — returns a slice of
    field names in declaration order.  Lets generic reflection helpers
    (`to_map`, serializers) enumerate fields without per-struct
    knowledge.  No `value` dependency, so it's emitted for every
    struct in a `std/value`-importing module."""
    span = struct.span
    name_lits = [
        StringLiteral(
            span=span,
            value=f.name.encode("utf-8"),
            is_multiline=False,
        )
        for f in struct.fields
    ]
    body = Block(span=span, statements=[
        Return(
            span=span,
            value=ArrayLiteral(span=span, elements=name_lits),
        ),
    ])
    return Function(
        span=span,
        name="__fields__",
        generics=[],
        self_param=SelfParam(
            span=span,
            type=SelfType(span=span),
            is_default=True,
        ),
        params=[],
        return_type=SliceType(
            span=span,
            element=SliceType(
                span=span,
                element=NamedType(span=span, name="u8"),
            ),
        ),
        body=body,
        is_variadic=False,
    )


def _make_set_dunder(struct: Struct, value_binding: str) -> Function:
    """Synthesize `__set__(self: ptr[var[Self]], name: []u8, v: value.Value)`.

    Only `var[T]` fields generate match arms — immutable fields can't
    be re-bound, so they're silently skipped.  For each generated arm,
    the body narrows `v` to the field's inner type with `?=`; only on
    a successful narrow does the assignment fire, so callers passing
    a wrong variant fall through to a no-op.  Unknown names hit the
    wildcard arm (also no-op).
    """
    span = struct.span
    arms: list[MatchArm] = []
    for f in struct.fields:
        inner = _unwrap_var(f.type)
        if inner is f.type:
            continue  # not a var field — silently skip
        if not _field_maps_to_value(inner):
            continue
        # if v ?= <inner>: self.<f.name> = v
        assign = Assignment(
            span=span,
            target=FieldAccess(
                span=span,
                obj=Name(span=span, name="self"),
                field=f.name,
            ),
            value=Name(span=span, name="v"),
        )
        guarded = If(
            span=span,
            condition=TypeTest(
                span=span,
                operand=Name(span=span, name="v"),
                type=inner,
            ),
            then_block=Block(span=span, statements=[assign]),
            else_block=None,
        )
        arms.append(MatchArm(
            span=span,
            pattern=ValuePattern(
                span=span,
                value=StringLiteral(
                    span=span,
                    value=f.name.encode("utf-8"),
                    is_multiline=False,
                ),
            ),
            body=Block(span=span, statements=[
                ExprStatement(span=span, expr=guarded),
            ]),
        ))
    # Wildcard fallback — unknown name → silent no-op.
    arms.append(MatchArm(
        span=span,
        pattern=WildcardPattern(span=span),
        body=Block(span=span, statements=[Pass(span=span)]),
    ))
    body = Block(span=span, statements=[
        ExprStatement(
            span=span,
            expr=Match(
                span=span,
                scrutinee=Name(span=span, name="name"),
                arms=arms,
            ),
        ),
    ])
    return Function(
        span=span,
        name="__set__",
        generics=[],
        self_param=SelfParam(
            span=span,
            type=WrapperType(
                span=span,
                wrapper="ptr",
                inner=WrapperType(
                    span=span,
                    wrapper="var",
                    inner=SelfType(span=span),
                ),
            ),
            is_default=False,
        ),
        params=[
            Parameter(
                span=span,
                name="name",
                type=SliceType(
                    span=span,
                    element=NamedType(span=span, name="u8"),
                ),
            ),
            Parameter(
                span=span,
                name="v",
                type=NamedType(
                    span=span,
                    name="Value",
                    module=value_binding,
                ),
            ),
        ],
        return_type=None,
        body=body,
        is_variadic=False,
    )


def _make_to_map(struct: Struct, value_binding: str, map_binding: str) -> Function:
    """Synthesize `to_map(self) -> rc[map.HashMap[[]u8, value.Value]]`
    by unrolling a `put(name, self.field)` per Value-shaped field.
    Per-struct (not generic) so the spec body is monomorphic at parse
    time, sidestepping the cross-module generic-spec limitation that
    blocks a uniform `value.to_map[T](obj)` function.
    """
    span = struct.span
    map_ty = WrapperType(
        span=span,
        wrapper="rc",
        inner=GenericType(
            span=span,
            base="HashMap",
            args=[
                SliceType(
                    span=span,
                    element=NamedType(span=span, name="u8"),
                ),
                NamedType(span=span, name="Value", module=value_binding),
            ],
            module=map_binding,
        ),
    )
    # m: rc[map.HashMap[[]u8, value.Value]] = map.HashMap[[]u8, value.Value].new()
    # Construct the explicit-type-args form rather than relying on
    # LHS-driven inference, which doesn't fire for synthesized AST.
    # Shape: FieldAccess(GenericInstantiation(map.HashMap, [...]), "new").
    key_ty = SliceType(span=span, element=NamedType(span=span, name="u8"))
    val_ty = NamedType(span=span, name="Value", module=value_binding)
    ctor_callee = FieldAccess(
        span=span,
        obj=GenericInstantiation(
            span=span,
            base=FieldAccess(
                span=span,
                obj=Name(span=span, name=map_binding),
                field="HashMap",
            ),
            type_args=[key_ty, val_ty],
        ),
        field="new",
    )
    ctor_call = Call(span=span, callee=ctor_callee, args=[])
    out_binding_stmt = _binding_stmt(span, "m", map_ty, ctor_call)
    # m.put("field", self.field) per field
    body_stmts: list = [out_binding_stmt]
    for f in struct.fields:
        inner = _unwrap_var(f.type)
        if not _field_maps_to_value(inner):
            continue
        body_stmts.append(ExprStatement(
            span=span,
            expr=Call(
                span=span,
                callee=FieldAccess(
                    span=span,
                    obj=Name(span=span, name="m"),
                    field="put",
                ),
                args=[
                    Argument(
                        span=span,
                        name=None,
                        value=StringLiteral(
                            span=span,
                            value=f.name.encode("utf-8"),
                            is_multiline=False,
                        ),
                    ),
                    Argument(
                        span=span,
                        name=None,
                        value=FieldAccess(
                            span=span,
                            obj=Name(span=span, name="self"),
                            field=f.name,
                        ),
                    ),
                ],
            ),
        ))
    body_stmts.append(Return(
        span=span,
        value=Name(span=span, name="m"),
    ))
    return Function(
        span=span,
        name="to_map",
        generics=[],
        self_param=SelfParam(
            span=span,
            type=SelfType(span=span),
            is_default=True,
        ),
        params=[],
        return_type=map_ty,
        body=Block(span=span, statements=body_stmts),
        is_variadic=False,
    )


def _binding_stmt(span: Span, name: str, ty, value):
    """Helper: build a `Binding` statement.  Local import to avoid
    threading another node through the module-level imports list."""
    from .nodes import Binding
    return Binding(span=span, name=name, type=ty, value=value)


class LoaderError(Exception):
    """Raised when a file can't be found, a cycle is detected, or an
    imported module fails to resolve/typecheck.
    """


@dataclass
class Module:
    path: Path
    name: str  # symbol-mangling prefix; "" for the entry module
    source: str
    tree: File
    res: ResolutionMap
    types: TypeMap
    checker: TypeChecker
    # binding (e.g. "math") → loaded Module. Missing bindings are legacy stubs.
    imports: dict[str, "Module"] = field(default_factory=dict)


class Loader:
    """Recursively load Ouro modules from disk.

    Use as `Loader().load_entry(entry_path)` to get a list of
    Module objects in dependency order (deps first, entry last).
    """

    def __init__(self) -> None:
        self._cache: dict[Path, Module] = {}
        self._loading: list[Path] = []  # ordered stack for cycle reporting

    def load_entry(
        self, entry_path: Path, runtime_root: Optional[Path] = None
    ) -> list[Module]:
        """Load *entry_path* + all transitive imports, returning modules
        in dependency order (deps first, entry last).

        If *runtime_root* is given (or auto-detected), every `.ou` file
        in that directory is loaded as a runtime module *before* the
        entry's imports.  Runtime modules emit bare symbol names and
        get linked into every program automatically — they're the
        Ouro-side of the runtime.
        """
        entry_path = entry_path.resolve()
        ordered: list[Module] = []

        if runtime_root is None:
            runtime_root = self._auto_detect_runtime_root(entry_path)
        if runtime_root is not None and runtime_root.is_dir():
            for path in sorted(runtime_root.glob("*.ou")):
                self._load(path.resolve(), ordered)

        self._load(entry_path, ordered, is_entry=True)
        return ordered

    @staticmethod
    def _auto_detect_runtime_root(entry_path: Path) -> Optional[Path]:
        """Walk up from *entry_path* looking for the bundled
        runtime at `std/runtime/`.  Returns its absolute path or
        None if not found.  Falls back to a bare `runtime/`
        sibling for back-compat with any externally-pinned setup.
        """
        for parent in entry_path.parents:
            for rel in ("std/runtime", "runtime"):
                candidate = parent / rel
                if candidate.is_dir() and any(candidate.glob("*.ou")):
                    return candidate
        return None

    def _load(
        self, path: Path, ordered: list[Module], *, is_entry: bool = False
    ) -> Module:
        if path in self._cache:
            return self._cache[path]

        if path in self._loading:
            chain = " -> ".join(str(p) for p in [*self._loading, path])
            raise LoaderError(f"import cycle: {chain}")

        self._loading.append(path)
        try:
            source = path.read_text()
            tokens = lex(source, str(path))
            tree = parse(tokens, str(path))
            _desugar_enums(tree)
            _synthesize_get_dunder(tree)

            # Recursively load imports first so we can hand the typechecker
            # their TypeChecker instances for cross-module member lookup.
            imports: dict[str, Module] = {}
            for decl in tree.declarations:
                if not isinstance(decl, Import):
                    continue
                resolved = self._resolve_import_path(decl.path, path.parent)
                if resolved is None:
                    continue  # legacy stub — codegen handles it
                imports[decl.binding] = self._load(resolved, ordered)

            res = resolve(tree)
            if res.errors:
                msgs = "; ".join(str(e) for e in res.errors)
                raise LoaderError(f"resolve errors in {path}: {msgs}")

            checker = TypeChecker(
                res, module_imports={b: m.checker for b, m in imports.items()}
            )
            types = checker.check_file(tree)
            if types.errors:
                msgs = "; ".join(str(e) for e in types.errors)
                raise LoaderError(f"typecheck errors in {path}: {msgs}")

            # Runtime modules emit bare symbol names (no prefix) so the
            # codegen's well-known runtime symbols (`arc_alloc`, etc.)
            # link without going through the per-module mangling.  Match
            # is "the immediate parent directory is named `runtime`."
            is_runtime = path.parent.name == "runtime"
            name = "" if (is_entry or is_runtime) else path.stem
            module = Module(
                path=path,
                name=name,
                source=source,
                tree=tree,
                res=res,
                types=types,
                checker=checker,
                imports=imports,
            )
            self._cache[path] = module
            ordered.append(module)
            return module
        finally:
            self._loading.pop()

    def _resolve_import_path(
        self, import_path: str, importer_dir: Path
    ) -> Optional[Path]:
        """Return the absolute on-disk path for *import_path*, or None
        if the import should be treated as a legacy stub (no file load).
        """
        if import_path.startswith("./") or import_path.startswith("../"):
            candidate = (importer_dir / (import_path + ".ou")).resolve()
            if not candidate.exists():
                raise LoaderError(
                    f"can't find imported file {candidate} "
                    f"(from import {import_path!r})"
                )
            return candidate

        if import_path.startswith("std/"):
            # Bundled stdlib: `std/X` → `<repo>/std/X.ou`.  This file
            # lives at `<repo>/bootstrap/src/loader.py`, so three
            # parents up is the repo root.  Missing files fall
            # through to the legacy stub path while the stdlib is
            # still skeletal.
            std_root = Path(__file__).resolve().parent.parent.parent / "std"
            candidate = (std_root / (import_path[len("std/"):] + ".ou")).resolve()
            if candidate.exists():
                return candidate
            return None

        # Bare paths: relative to the importer's directory.
        candidate = (importer_dir / (import_path + ".ou")).resolve()
        if candidate.exists():
            return candidate
        raise LoaderError(
            f"can't resolve import {import_path!r} from {importer_dir}"
        )


def compile_program(
    entry_path: Path, runtime_root: Optional[Path] = None
) -> tuple[str, str]:
    """Load *entry_path* + all its transitive imports, run codegen for
    every module in dependency order, and return concatenated outputs.

    Returns `(qbe_ir, asm_sidecar)` — the first goes to QBE, the
    second is raw assembly emitted from `asm fn` declarations and gets
    appended to QBE's output before `cc`.  When no module contains an
    `asm` decl, the sidecar is an empty string.

    *runtime_root* points at the directory containing the Ouro-side
    runtime (`entry.ou`, syscall wrappers, etc.).  If omitted, the
    loader auto-detects by walking up from *entry_path* — useful for
    in-repo programs.  Tests that compile to a tmp_path should pass
    the runtime root explicitly so the entry symbol resolves.

    The entry module's symbols are emitted with no prefix (so the linker
    finds `main`); every imported module's symbols are prefixed with the
    importing-binding's file stem, e.g. `$math__foo`.  Files under a
    `runtime/` directory are also unprefixed.
    """
    modules = Loader().load_entry(entry_path, runtime_root=runtime_root)

    # Build all Codegen instances first so cross-module references —
    # specifically generic-fn specs requested by one module on
    # another — can flow during the main pass.  The instance map is
    # wired in dependency order; an entry's `_module_imports` points
    # at the already-constructed Codegen for each of its deps.
    codegens: dict[int, Codegen] = {}
    per_module_parts: dict[int, list[str]] = {}
    asm_parts: list[str] = []
    for mod in modules:
        deps = {b: codegens[id(m)] for b, m in mod.imports.items()}
        cg = Codegen(
            mod.types, mod.res, module_prefix=mod.name, module_imports=deps
        )
        codegens[id(mod)] = cg
        qbe_ir, asm = cg.generate(mod.tree)
        per_module_parts[id(mod)] = [qbe_ir]
        if asm:
            asm_parts.append(asm)

    # Fixed-point drain: a cross-module call inside one module's spec
    # body may register a spec on yet another module.  Loop over all
    # codegens, draining any newly-registered specs, until no module
    # has pending work.
    while any(cg.has_pending_specs() for cg in codegens.values()):
        for mod in modules:
            cg = codegens[id(mod)]
            new_specs = cg._drain_new_specs()
            if new_specs:
                per_module_parts[id(mod)].append("\n".join(new_specs))

    qbe_parts = [
        part for mod in modules for part in per_module_parts[id(mod)]
    ]
    program_ir = "\n".join(qbe_parts)

    # Program-level dedup: each module emits its own copy of the
    # bare-named helpers (`$_slice_eq_u8`, `$_slice_hash_u8`), which
    # produces a multiple-definition link error when two modules both
    # match on `[]u8` or hash slices.  Strip all but the first
    # occurrence of each.  Helpers are pure functions whose IR is
    # stable, so the surviving copy is interchangeable with any of
    # the duplicates.
    program_ir = _dedupe_shared_helpers(program_ir)
    return program_ir, "\n".join(asm_parts)


_SHARED_HELPER_SYMBOLS: tuple[str, ...] = (
    "$_slice_eq_u8",
    "$_slice_hash_u8",
)


def _dedupe_shared_helpers(ir: str) -> str:
    """Keep at most one definition of each bare-named helper function
    in *ir*.  A definition starts at `function <ret>? $name(...)` and
    runs until the matching closing `}` at column 0."""
    lines = ir.split("\n")
    out: list[str] = []
    i = 0
    seen: set[str] = set()
    while i < len(lines):
        line = lines[i]
        helper_hit: Optional[str] = None
        for sym in _SHARED_HELPER_SYMBOLS:
            if line.startswith("function ") and f"{sym}(" in line:
                helper_hit = sym
                break
        if helper_hit is not None and helper_hit in seen:
            # Skip until the closing `}` at column 0.
            while i < len(lines) and lines[i] != "}":
                i += 1
            i += 1  # consume the `}` too
            continue
        if helper_hit is not None:
            seen.add(helper_hit)
        out.append(line)
        i += 1
    return "\n".join(out)
