"""QBE IR code generator for Ouro.

Translates the typed, resolved AST to QBE SSA intermediate representation.

v1 scope:
  - Non-generic free functions and struct methods
  - Primitive types mapped to QBE base types (w / l / s / d)
  - Slices as fat pointers {ptr: l, len: l} allocated on the stack
  - Non-generic structs as QBE aggregate types; instances heap-allocated
    with a 16-byte ARC header (refcount + drop_fn)
  - Arithmetic, comparison, logical operators
  - if/else using jnz; match with value / wildcard patterns
  - for loops over slices lowered to index-based loops
  - loop / break / continue
  - Const bindings → SSA temps; var[T] bindings → alloc stack slots
  - String literals interned in the data section
  - Function calls with positional arguments

Deferred to v2+:
  - Generic monomorphization
  - ARC retain (arc_release emitted at scope end; retain skipped)
  - weak[T] upgrade / downgrade
  - Module import extern declarations
  - match type patterns and union tag comparisons (?=)
  - Named call argument reordering
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .nodes import (
    Argument,
    AsmDecl,
    ExternDecl,
    TopLevelBinding,
    Assignment,
    Binding,
    Block,
    BoolLiteral,
    BinaryOp,
    Break,
    ByteLiteral,
    Call,
    Continue,
    Discard,
    ExprStatement,
    FieldAccess,
    File,
    FloatLiteral,
    For,
    Function,
    GenericInstantiation,
    If,
    Index,
    IntLiteral,
    Loop,
    Match,
    Name,
    Pass,
    Range,
    Return,
    Struct,
    StructLiteral,
    StringLiteral,
    ArrayLiteral,
    TypeAlias,
    TypePattern,
    TypeTest,
    Cast,
    UnaryOp,
    ValuePattern,
)
from .nodes import NamedType, GenericType, SelfType, SliceType, UnionType, WrapperType, InferType, Span
from .typechecker import (
    Ty,
    TypeMap,
    FnTy,
    ModuleTy,
    PrimTy,
    SliceTy,
    StructTy,
    TypeParamTy,
    UnionTy,
    WrapperTy,
    UnitTy,
    UnknownTy,
    UNIT,
    UNKNOWN,
    _NUMERIC,
    _fmt,
    _subst,
    _unify,
    _unwrap,
    _readable,
)
from .resolver import BUILTIN_TYPES, ResolutionMap, SymbolKind


# ── Error ─────────────────────────────────────────────────────────────────────


class CodegenError(Exception):
    pass


# Slice-content equality for `[]u8` scrutinees in `match` arms.
# Emitted at most once per module (first match-on-byte-slice site
# triggers it via `_ensure_slice_eq_u8`).  Compares length, then
# bytes; returns w 1 / 0.  Kept inline here rather than in a runtime
# module so callers don't need to `import std/string` to use the
# feature.
_SLICE_HASH_U8_IR = """\
function l $_slice_hash_u8(l %s) {
@start
\t%dp =l loadl %s
\t%lp =l add %s, 8
\t%len =l loadl %lp
\t%hslot =l alloc8 8
\tstorel 14695981039346656037, %hslot
\t%islot =l alloc8 8
\tstorel 0, %islot
\tjmp @loop
@loop
\t%i =l loadl %islot
\t%done =w ceql %i, %len
\tjnz %done, @after, @body
@body
\t%addr =l add %dp, %i
\t%b =w loadub %addr
\t%bw =l extuw %b
\t%h =l loadl %hslot
\t%h2 =l xor %h, %bw
\t%h3 =l mul %h2, 1099511628211
\tstorel %h3, %hslot
\t%i2 =l add %i, 1
\tstorel %i2, %islot
\tjmp @loop
@after
\t%r =l loadl %hslot
\tret %r
}"""


_SLICE_EQ_U8_IR = """\
function w $_slice_eq_u8(l %a, l %b) {
@start
\t%alp =l add %a, 8
\t%alen =l loadl %alp
\t%blp =l add %b, 8
\t%blen =l loadl %blp
\t%leq =w ceql %alen, %blen
\tjnz %leq, @bytes, @false
@bytes
\t%adp =l loadl %a
\t%bdp =l loadl %b
\t%islot =l alloc8 8
\tstorel 0, %islot
\tjmp @loop
@loop
\t%i =l loadl %islot
\t%done =w ceql %i, %alen
\tjnz %done, @true, @cmp
@cmp
\t%aa =l add %adp, %i
\t%ba =l add %bdp, %i
\t%ab =w loadub %aa
\t%bb =w loadub %ba
\t%beq =w ceqw %ab, %bb
\tjnz %beq, @inc, @false
@inc
\t%i2 =l add %i, 1
\tstorel %i2, %islot
\tjmp @loop
@true
\tret 1
@false
\tret 0
}"""


# ── QBE type helpers ──────────────────────────────────────────────────────────

_ARC_HEADER = 16  # 8 bytes refcount + 8 bytes drop_fn


def _store_for_base(b: str) -> str:
    """QBE store mnemonic matching a register base (`w` → `storew`)."""
    return {"w": "storew", "l": "storel", "s": "stores", "d": "stored"}[b]


def _load_for_base(b: str) -> str:
    """QBE load mnemonic matching a register base (`w` → `loadw`)."""
    return {"w": "loadw", "l": "loadl", "s": "loads", "d": "loadd"}[b]


def _base(ty: Ty) -> str:
    """QBE register base type: w (32-bit), l (64-bit), s (f32), d (f64).

    `ptr[T]` / `weak[T]` are always pointer-sized (`l`) regardless of
    `T`'s width — they're handles, not the value itself.  Strip
    binding-style wrappers (`var`, `const`, `rc`, `arc`) first, then
    re-check the result: a `var[ptr[u8]]` slot stores a pointer, so
    its base is `l`, not the byte-wide inner type's `w`.
    """
    r = _readable(ty)
    if isinstance(r, WrapperTy) and r.wrapper in ("ptr", "weak"):
        return "l"
    if isinstance(r, FnTy):
        return "l"  # function pointer
    r = _unwrap(r)
    if isinstance(r, PrimTy):
        n = r.name
        if n == "f64":
            return "d"
        if n == "f32":
            return "s"
        if n in ("i64", "u64", "isize", "usize"):
            return "l"
        return "w"  # i8/u8/i16/u16/i32/u32/bool
    return "l"  # struct ref, slice fat-ptr, ptr


def _size(ty: Ty, layouts: Optional[dict[str, "StructLayout"]] = None) -> int:
    """Size in bytes.  Bare `StructTy` values inline their fields when
    *layouts* is given (composition path); `ptr[T]` / `weak[T]` are
    always pointer-sized regardless of `T`.  Other wrappers
    (`var`, `const`, `rc`, `arc`) strip to their inner type for the
    layout calculation."""
    # Bare struct → inline size from its layout.
    if isinstance(ty, StructTy) and layouts is not None:
        lay = layouts.get(_struct_name(ty))
        if lay is not None:
            return lay.total
    r = _readable(ty)
    if isinstance(r, WrapperTy) and r.wrapper in ("ptr", "weak"):
        return 8
    r = _unwrap(r)
    if isinstance(r, StructTy) and layouts is not None:
        lay = layouts.get(_struct_name(r))
        if lay is not None:
            return lay.total
    if isinstance(r, PrimTy):
        n = r.name
        if n in ("i8", "u8"):
            return 1
        if n in ("i16", "u16"):
            return 2
        if n in ("i32", "u32", "f32", "bool"):
            return 4
        return 8
    if isinstance(r, SliceTy):
        return 16
    return 8


def _store(ty: Ty) -> str:
    r = _readable(ty)
    if isinstance(r, WrapperTy) and r.wrapper in ("ptr", "weak"):
        return "storel"
    ty = _unwrap(r)
    if isinstance(ty, PrimTy):
        n = ty.name
        if n == "f64":
            return "stored"
        if n == "f32":
            return "stores"
        if n in ("i64", "u64", "isize", "usize"):
            return "storel"
        if n in ("i32", "u32", "bool"):
            return "storew"
        if n in ("i16", "u16"):
            return "storeh"
        if n in ("i8", "u8"):
            return "storeb"
    return "storel"  # struct ref, slice, ptr


def _load(ty: Ty) -> str:
    r = _readable(ty)
    if isinstance(r, WrapperTy) and r.wrapper in ("ptr", "weak"):
        return "loadl"
    ty = _unwrap(r)
    if isinstance(ty, PrimTy):
        n = ty.name
        if n == "f64":
            return "loadd"
        if n == "f32":
            return "loads"
        if n in ("i64", "u64", "isize", "usize"):
            return "loadl"
        if n in ("i32", "u32", "bool"):
            return "loadw"
        if n == "u16":
            return "loaduh"
        if n == "i16":
            return "loadsh"
        if n == "u8":
            return "loadub"
        if n == "i8":
            return "loadsb"
    return "loadl"  # struct ref, slice, ptr


def _is_managed(ty: Ty) -> bool:
    """Is this type ARC-managed (i.e. needs inc/release)?

    Post stack-by-default: bare `StructTy` is *not* managed — it lives
    on the stack (binding) or inline (field) and runs `__drop__`
    directly.  Only `rc[T]` / `arc[T]` / `weak[T]` wrappers around a
    struct put it on the refcounted heap.  `UnionTy` stays managed
    because tagged unions are still always `arc_alloc`-boxed (no
    stack-by-default story for unions yet).
    """
    if isinstance(ty, WrapperTy) and ty.wrapper in ("rc", "arc", "weak"):
        return True
    if isinstance(ty, WrapperTy) and ty.wrapper in ("var", "const"):
        # `var[arc[T]]` / `const[arc[T]]` — peel one layer and re-check.
        return _is_managed(ty.inner)
    if isinstance(ty, UnionTy):
        return True
    return False


def _is_union_payload_managed(ty: Ty) -> bool:
    """A union variant of `Foo` (bare struct) is *heap-boxed* via
    `arc_alloc` when stored into the union's payload slot, so the
    union's drop helper still needs to `arc_release` it — even though
    `Foo` on its own (binding/field) is now stack/inline.  Wrappers
    and other unions follow `_is_managed`."""
    if isinstance(_unwrap(ty), StructTy):
        return True
    return _is_managed(ty)


def _is_stack_struct_binding(decl_ty: Ty) -> bool:
    """True when *decl_ty* — the declared type of a binding — should
    land its struct value on the stack (alloca) instead of the heap
    (arc_alloc).  Initial scope: only bare `StructTy`.  Wrapper forms
    (var/const/rc/arc) stay on the heap path for now; `var[Struct]`
    reassignment-by-copy is a later slice.
    """
    return isinstance(decl_ty, StructTy)


def _is_stack_struct_return(ret_ty: Ty) -> bool:
    """True when a function's *ret_ty* is a bare `StructTy` and should
    use QBE's aggregate return convention (`function :Foo $f(...)`).
    `rc[T]`/`arc[T]` returns stay as `l`-typed pointer returns.
    """
    return isinstance(ret_ty, StructTy)


def _is_copy_of_existing_ref(value_expr: Any) -> bool:
    """Syntactic predicate: True if *value_expr* yields a reference
    that already exists elsewhere — `y = x` (Name) or `y = obj.field`
    (FieldAccess).

    NB: This is a *syntactic* check and intentionally returns True
    even for weak-field reads (which look like FieldAccess but
    actually allocate a fresh box).  Use `Codegen._is_borrowed_copy`
    when you need the semantic check that takes weak fields into
    account.
    """
    return isinstance(value_expr, (Name, FieldAccess))


def _is_weak_field(ty: Ty) -> bool:
    """Is this field declared as `weak[T]`?

    Weak fields use the weak_inc / weak_release / weak_upgrade runtime
    helpers instead of arc_inc / arc_release.  They don't keep the
    referent alive; reads produce `T | Null` (already exposed by
    the type checker).
    """
    return isinstance(ty, WrapperTy) and ty.wrapper == "weak"


def _field_inc_call(ty: Ty) -> str:
    """Runtime fn to bump *ty*'s refcount when storing a copy."""
    return "$weak_inc" if _is_weak_field(ty) else "$arc_inc"


def _field_release_call(ty: Ty) -> str:
    """Runtime fn to release *ty* (when overwriting or dropping)."""
    return "$weak_release" if _is_weak_field(ty) else "$arc_release"


def _ty_name_part(ty: Ty) -> str:
    """Encode a type as a fragment suitable for embedding in a QBE
    symbol name. Used to name specialized generic structs.
    """
    if isinstance(ty, PrimTy):
        return ty.name
    if isinstance(ty, StructTy):
        if ty.type_args:
            parts = [_ty_name_part(a) for a in ty.type_args]
            return f"{ty.name}_{'_'.join(parts)}"
        return ty.name
    if isinstance(ty, WrapperTy):
        return f"{ty.wrapper}_{_ty_name_part(ty.inner)}"
    if isinstance(ty, SliceTy):
        return f"slice_{_ty_name_part(ty.element)}"
    if isinstance(ty, UnionTy):
        # Spelling out every variant blows up QBE's identifier-length
        # limit on wide unions like `Value` (15 variants).  Use the
        # full spelling when it fits, fall back to a stable hash when
        # it would push the spec qname over the limit.
        spelled = "__".join(sorted(_ty_name_part(v) for v in ty.variants))
        if len(spelled) <= 40:
            return f"union_{spelled}"
        import hashlib as _hashlib
        digest = _hashlib.blake2b(spelled.encode(), digest_size=6).hexdigest()
        return f"union_{digest}"
    if isinstance(ty, TypeParamTy):
        return ty.name
    return "X"


def _ty_to_ast_for_intrinsic(ty: Ty, span: Span) -> Optional[Any]:
    """Recreate an AST type node from a typechecker `Ty`, enough for
    feeding into a synthesized `GenericInstantiation` (e.g. bare
    `hash(k)` → `hash[T](k)`).  Returns None when the Ty doesn't have
    a clean AST representation."""
    if isinstance(ty, PrimTy):
        return NamedType(span=span, name=ty.name)
    if isinstance(ty, StructTy):
        return NamedType(span=span, name=ty.name)  # type args ignored
    if isinstance(ty, SliceTy):
        inner = _ty_to_ast_for_intrinsic(ty.element, span)
        if inner is None:
            return None
        return SliceType(span=span, element=inner)
    if isinstance(ty, WrapperTy):
        inner = _ty_to_ast_for_intrinsic(ty.inner, span)
        if inner is None:
            return None
        return WrapperType(span=span, wrapper=ty.wrapper, inner=inner)
    return None


def _struct_name(ty: Ty) -> str:
    """Return the QBE symbol name for a StructTy — bare for non-generic
    structs, suffixed with encoded type args for specialized generics.
    """
    if isinstance(ty, StructTy):
        if ty.type_args:
            parts = [_ty_name_part(a) for a in ty.type_args]
            return f"{ty.name}_{'_'.join(parts)}"
        return ty.name
    return ""


# ── Struct layout ─────────────────────────────────────────────────────────────


@dataclass
class FieldLayout:
    name: str
    ty: Ty
    offset: int


@dataclass
class StructLayout:
    name: str
    fields: list[FieldLayout]
    total: int  # total size in bytes
    has_drop: bool = False  # True if the struct defines __drop__
    # True if any field needs cleanup at drop time: an ARC wrapper
    # (rc/arc/weak), a tagged union (always heap-boxed), or an inline
    # bare-struct whose own drop chain isn't a no-op.  The auto drop
    # wrapper dispatches to per-field cleanup accordingly.
    has_managed_fields: bool = False


def _layout(
    name: str,
    fields: list[tuple[str, Ty]],
    has_drop: bool = False,
    layouts: Optional[dict[str, StructLayout]] = None,
) -> StructLayout:
    off = 0
    result: list[FieldLayout] = []
    for fname, fty in fields:
        s = _size(fty, layouts)
        # Inline struct fields align to 8 (the largest primitive on
        # 64-bit); everything else aligns to its natural size capped
        # at 8.  Matches C's "natural alignment of the most-aligned
        # contained member" rule for a first cut.
        if isinstance(fty, StructTy):
            a = 8
        else:
            a = min(s, 8) if s > 0 else 1
        if off % a:
            off += a - (off % a)
        result.append(FieldLayout(fname, fty, off))
        off += s
    if off % 8:
        off += 8 - (off % 8)
    def _field_needs_cleanup(fty: Ty) -> bool:
        if _is_managed(fty):
            return True
        if isinstance(fty, StructTy) and layouts is not None:
            inner = layouts.get(_struct_name(fty))
            if inner is not None:
                return inner.has_drop or inner.has_managed_fields
        return False

    has_managed = any(_field_needs_cleanup(f.ty) for f in result)
    return StructLayout(name, result, off, has_drop, has_managed)


# ── Per-function context ──────────────────────────────────────────────────────


@dataclass
class Local:
    loc: str   # SSA temp name (const) or stack slot pointer (var)
    ty: Ty
    is_var: bool
    # True when this local owns a stack-allocated bare-struct value
    # (alloca slot or a `:Foo` aggregate parameter).  Lets call sites
    # distinguish from `rc[T]`/`arc[T]` reads — whose typemap entry is
    # also `StructTy` after `_readable` — for ABI selection.
    is_bare_struct: bool = False


class FnCtx:
    def __init__(self) -> None:
        self._n: int = 0
        self._ln: int = 0
        self.locals: dict[str, Local] = {}
        # Each loop_stack entry is (continue_lbl, break_lbl, body_depth)
        # where body_depth = len(managed_stack) at the moment the loop
        # body's scope is opened.  Break/continue release scopes from
        # the top of managed_stack down to body_depth (inclusive).
        self.loop_stack: list[tuple[str, str, int]] = []
        self.out: list[str] = []
        # Stack of managed-local name lists, one per open lexical scope.
        # Pushed on each _emit_block (or _emit_block_yielding) entry,
        # popped on exit.  At normal block exit we arc_release the
        # locals in the top scope; before `ret` we release everything
        # across all scopes; `break`/`continue` release down to the
        # enclosing loop's body scope.
        self.managed_stack: list[list[str]] = []
        # Parallel stack tracking stack-allocated struct locals (alloca
        # path).  Each entry is (slot_pointer, drop_fn_symbol); at
        # scope exit we emit a direct `call drop_fn(slot)` (no
        # refcount).  Pushed/popped alongside managed_stack on every
        # block entry/exit.  Only non-trivial drops appear here —
        # structs with no __drop__ and no managed fields drop_fn==`0`
        # are skipped to keep the output clean.
        self.stack_drop_stack: list[list[tuple[str, str]]] = []
        # Map of %tmp → QBE base ("w" / "l" / "s" / "d").  Populated by
        # call sites that produce a typed temp whose AST type the
        # typechecker can't see (e.g. io.printf intrinsic returning w).
        # _emit_block_yielding consults this to decide whether a w temp
        # needs sign-extension before storel.
        self.tmp_base: dict[str, str] = {}
        # If the enclosing function returns a tagged union, this is the
        # canonical sorted-by-_fmt list of variant Tys; tag = index.
        # Set in _emit_fn; consulted by _emit_return for boxing.
        self.union_return_variants: Optional[list[Ty]] = None
        # True when this function's return type is a bare `StructTy`
        # and codegen uses QBE's aggregate return.  `_emit_return`
        # allocates a stack slot for the struct literal value before
        # `ret %slot`.
        self.is_aggregate_return: bool = False
        # Name of the enclosing struct when emitting a method body —
        # used by _emit_struct_lit to resolve `Self { ... }`.  None when
        # emitting a free function.
        self.current_struct: Optional[str] = None
        # Allocas that must live at function entry rather than at the
        # point of use.  QBE's `allocN` reserves stack each time it
        # executes, so an alloca inside a loop body grows the stack
        # per-iteration.  Call sites that only need a transient
        # scratch slot (e.g. blit-target for a slice `mem_load`) hoist
        # their alloca through `prologue_alloca`; the same slot is
        # then reused across loop iterations.  Splice happens in
        # `_emit_fn` right after `@start`.
        self.prologue: list[str] = []

    def tmp(self, hint: str = "t") -> str:
        v = self._n
        self._n += 1
        return f"%_{hint}{v}"

    def lbl(self, hint: str = "L") -> str:
        v = self._ln
        self._ln += 1
        return f"@{hint}{v}"

    def emit(self, line: str) -> None:
        self.out.append("    " + line)

    def label(self, lbl: str) -> None:
        self.out.append(lbl)

    def prologue_alloca(self, align: int, size: int, hint: str = "pa") -> str:
        """Emit an `alloc{align} {size}` once at function entry and
        return the resulting temp.  Use this when the alloca's only
        purpose is to provide a scratch slot whose lifetime is the
        whole function — most importantly, when the call site sits
        inside a loop and per-iteration reallocation would otherwise
        leak stack."""
        slot = self.tmp(hint)
        self.prologue.append(f"    {slot} =l alloc{align} {size}")
        return slot


# ── Code generator ────────────────────────────────────────────────────────────


class Codegen:
    def __init__(
        self,
        types: TypeMap,
        res: ResolutionMap,
        module_prefix: str = "",
        module_imports: Optional[dict[str, "Codegen"]] = None,
    ) -> None:
        self._types = types
        self._res = res
        self._data: list[str] = []
        self._str_n: int = 0
        self._layouts: dict[str, StructLayout] = {}
        # Union drop_fn helpers, memoized by the union's canonical signature.
        # _union_drop_emit holds the QBE IR for each generated helper;
        # appended to the file output by `generate`.
        self._union_drop_fns: dict[str, str] = {}
        self._union_drop_emit: list[str] = []
        # Match-on-`[]u8` slice-equality helper, emitted at most once
        # per module.  Generated lazily on the first use site.
        self._slice_eq_u8_emitted: bool = False
        # `hash[[]u8]` intrinsic helper (FNV-1a).  Same once-per-module
        # generation as `_slice_eq_u8`.
        self._slice_hash_u8_emitted: bool = False
        # Per-struct sets of static method names (no self_param) — used
        # by _emit_call so `Box.make(v)` doesn't pass an implicit self.
        self._static_methods: dict[str, set[str]] = {}
        # Auto-generated drop wrappers for structs with managed fields.
        # The wrapper calls the user's __drop__ (if any) and then
        # arc_release()s each managed field.  Memoized by struct name.
        self._struct_drop_wrappers: dict[str, str] = {}
        self._struct_drop_emit: list[str] = []
        # Active type-parameter substitution.  Set per-specialization
        # while emitting a generic struct's specialized methods; consulted
        # by _ast_ty and _ty to resolve TypeParamTy("T") to concrete Tys.
        self._current_subst: dict[str, Ty] = {}
        # Generic decls indexed by name — used to find the original
        # struct AST when emitting a specialization.
        self._generic_structs: dict[str, Struct] = {}
        # Generic free functions indexed by name.
        self._generic_fns: dict[str, Function] = {}
        # Param type lists for non-generic free functions and struct
        # methods, indexed by the QBE-mangled callee name (without the
        # `$` sigil).  Lets call sites select the QBE arg ABI from the
        # *callee's* declared param type (`:Foo` for bare struct,
        # `l` for `ptr[T]` / `rc[T]` / `arc[T]`), rather than guessing
        # from the arg's read-stripped type.  Generic-fn specs and
        # method specs are added when each spec is routed/emitted.
        self._fn_param_tys: dict[str, list[Ty]] = {}
        # Top-level type aliases indexed by name.  `_ast_ty` expands
        # them transparently before producing a `Ty`.
        self._type_aliases: dict[str, "TypeAlias"] = {}
        # Top-level mutable globals (`name: var[T] = const_init`).
        # Maps the bare name → the inner Ty.  Codegen emits a data
        # entry per global, and reads/writes from name expressions
        # route through `$<prefix>__<name>` instead of a local slot.
        self._module_vars: dict[str, Ty] = {}
        # External-linkage decls (e.g. libc / C-runtime symbols
        # declared via `extern foo(...)`).  Calls route to the bare
        # name with no module prefix.  `_extern_variadic` is the
        # subset declared with a trailing `...`; their calls emit
        # QBE's variadic-call separator before any extra args.
        self._extern_decls: set[str] = set()
        self._extern_variadic: set[str] = set()
        # Cached directory of the current module's source file —
        # used by `embed("path")` to resolve relative paths.  Set
        # in `generate()` from the tree's `path` attribute.
        self._source_dir: Optional[Any] = None
        # Discovered specializations of generic free functions.
        # Keyed by specialized name (e.g. `id_i32`) → (decl, subst).
        # Populated lazily during call-site emission, then drained
        # by a worklist loop after the main pass.  `_emitted_fn_specs`
        # tracks which names have already been emitted so the driver
        # can call `_drain_new_specs` again later for cross-module
        # spec requests without re-emitting.
        self._fn_specs: dict[str, tuple[Function, dict[str, Ty]]] = {}
        self._emitted_fn_specs: set[str] = set()
        # Same shape, for generic *struct* specializations.  Each
        # entry's value is `(decl, subst)`; the drain emits the
        # specialized layout + each non-generic method body.  Cross-
        # module instantiation paths register the spec on the
        # *defining* module's set so symbols stay in the right
        # prefix namespace.
        self._struct_specs: dict[str, tuple[Struct, dict[str, Ty]]] = {}
        self._emitted_struct_specs: set[str] = set()
        # Symbol mangling prefix for this module ("" for the entry
        # module; "math" for an imported `math.ou`).  Applied to every
        # locally-defined fn / struct / spec.  Runtime symbols
        # (arc_alloc, printf, ...) stay bare.
        self._module_prefix: str = module_prefix
        # Binding (e.g. "math") → the imported module's `Codegen`
        # instance.  Used to route cross-module calls — both the
        # prefix (`other_cg._module_prefix`) for symbol mangling
        # and `other_cg._generic_fns` for cross-module generic-fn
        # specialization.  Bindings missing from this map (e.g.
        # legacy stub names) keep the current drop-the-module
        # behavior in `_emit_call`.
        self._module_imports: dict[str, "Codegen"] = module_imports or {}
        # Memoised transitive import set — used when a method call
        # on a foreign struct type needs to walk *every* imported
        # codegen (including transitively imported ones) to find
        # the defining module.  Populated lazily on first access.
        self._transitive_imports_cache: Optional[list["Codegen"]] = None
        # Internal-label prefix: always non-empty per-module, used to
        # uniquify labels the user never sees (`_s0`, `_union_drop_N`,
        # spec names) so multiple modules with empty `_module_prefix`
        # (entry + runtime/*.ou) don't collide.  Set by `generate()`
        # from the file path.
        self._internal_id: str = "mod"

    # ── Module symbol mangling ────────────────────────────────────────────────

    def _q(self, name: str) -> str:
        """Apply this module's prefix to a user-defined symbol name.
        Returns *name* unchanged for the entry module (empty prefix);
        otherwise returns `<prefix>__<name>`.  Pass through unchanged
        for runtime symbols (`arc_alloc`, `printf`, etc.) — those are
        looked up via the linker, not this map.
        """
        if self._module_prefix:
            return f"{self._module_prefix}__{name}"
        return name

    # ── Type helpers ──────────────────────────────────────────────────────────

    def _ty(self, node: Any) -> Ty:
        """Return the typechecker's recorded type for *node*, with the
        active type-parameter substitution applied so generic method
        bodies see concrete Tys during specialization."""
        t = self._types.type_of(node)
        if t is None:
            return UNKNOWN
        if self._current_subst:
            return _subst(t, self._current_subst)
        return t

    def _ast_ty(self, t: Any) -> Ty:
        """Convert an AST type node to a Ty.  Honors the active type-
        parameter substitution so generic structs/methods resolve `T`
        to the instantiation's concrete type during specialization.
        Expands type aliases transparently — `Option[i32]` becomes
        the substituted body type, never a `StructTy("Option", ...)`.
        """
        if isinstance(t, NamedType):
            # `Foo.Bar` — enum-variant sugar or module-qualified.  The
            # resolver records the variant's STRUCT symbol when this
            # is enum sugar (same module); for cross-module sugar
            # `mod.Enum.Variant` (module="mod.Enum") look up the
            # variant in the imported module's enum-alias table.
            if t.module is not None and self._res:
                sym = self._res.get(t)
                if sym is not None and sym.kind == SymbolKind.STRUCT:
                    return StructTy(sym.name)
                if sym is not None and sym.kind == SymbolKind.IMPORT and "." in t.module:
                    other = self._module_imports.get(sym.name)
                    if other is not None:
                        _mod, enum_name = t.module.split(".", 1)
                        alias = other._type_aliases.get(enum_name)
                        if alias is not None and t.name in alias.enum_variants:
                            return StructTy(alias.enum_variants[t.name])
                # Cross-module plain type alias: expand transparently.
                # Delegate to the imported codegen so the body's
                # NamedTypes resolve against *its* ResolutionMap.
                if sym is not None and sym.kind == SymbolKind.IMPORT and "." not in t.module:
                    other = self._module_imports.get(sym.name)
                    if other is not None:
                        alias = other._type_aliases.get(t.name)
                        if alias is not None and not alias.enum_variants and not alias.generics:
                            return other._ast_ty(alias.body)
            # Type parameter? (substitution is keyed by param name)
            if t.name in self._current_subst:
                return self._current_subst[t.name]
            alias = self._type_aliases.get(t.name)
            if alias is not None and not alias.generics:
                return self._ast_ty(alias.body)
            return PrimTy(t.name) if t.name in BUILTIN_TYPES else StructTy(t.name)
        if isinstance(t, SliceType):
            return SliceTy(self._ast_ty(t.element))
        if isinstance(t, WrapperType):
            inner = UNKNOWN if isinstance(t.inner, InferType) else self._ast_ty(t.inner)
            return WrapperTy(t.wrapper, inner)
        if isinstance(t, GenericType):
            args = tuple(self._ast_ty(a) for a in t.args)
            alias = self._type_aliases.get(t.base)
            if alias is not None and len(args) == len(alias.generics):
                prev = self._current_subst
                self._current_subst = {**prev, **dict(zip(alias.generics, args))}
                try:
                    return self._ast_ty(alias.body)
                finally:
                    self._current_subst = prev
            return StructTy(t.base, args)
        if isinstance(t, UnionType):
            return UnionTy(frozenset(self._ast_ty(v) for v in t.variants))
        return UNKNOWN

    # ── Data section ──────────────────────────────────────────────────────────

    def _const_init(self, expr: Any, ty: Ty) -> str:
        """Emit a QBE data-section initializer for a comptime
        constant expression.  Supports integer / bool / byte
        literals, and casts of those into pointer types (so
        `0 as ptr[u8]` initialises a ptr global to NULL).
        Anything else falls back to zero-fill of the slot's size."""
        if isinstance(expr, Cast):
            return self._const_init(expr.operand, ty)
        b = _base(ty)
        if isinstance(expr, IntLiteral):
            return f"{b} {expr.value}"
        if isinstance(expr, BoolLiteral):
            return f"{b} {1 if expr.value else 0}"
        if isinstance(expr, ByteLiteral):
            return f"b {expr.value}"
        # Fallback — initialize to zero of the right width.
        size = _size(ty, self._layouts)
        return f"z {max(size, 1)}"

    def _intern_str(self, value: bytes) -> str:
        """Emit a data entry for a bytes value; return its global name."""
        name = f"$_s_{self._internal_id}_{self._str_n}"
        self._str_n += 1
        parts: list[str] = []
        buf: list[str] = []
        for b in value:
            if 0x20 <= b < 0x7F and b not in (ord('"'), ord("\\")):
                buf.append(chr(b))
            else:
                if buf:
                    parts.append(f'b "{"".join(buf)}"')
                    buf = []
                parts.append(f"b {b}")
        if buf:
            parts.append(f'b "{"".join(buf)}"')
        parts.append("b 0")
        self._data.append(f"data {name} = {{ {', '.join(parts)} }}")
        return name

    # ── Struct layout collection ──────────────────────────────────────────────

    def _collect_layouts(self, tree: File) -> None:
        # Pre-pass: collect every type alias so `_ast_ty` can expand
        # them when called below — fields, params, return types can
        # all reference an alias declared later in the source file.
        for decl in tree.declarations:
            if isinstance(decl, TypeAlias):
                self._type_aliases[decl.name] = decl

        # First pass: register every non-generic struct's decl and
        # method index, plus generic templates and free-fn signatures.
        # Layout *bodies* are built in a second pass so an `Outer { b:
        # Inner }` declared before `Inner` can still resolve.
        struct_decls: list[Struct] = []
        for decl in tree.declarations:
            if isinstance(decl, Struct) and not decl.generics:
                struct_decls.append(decl)
                qname = self._q(decl.name)
                static = {m.name for m in decl.methods if m.self_param is None}
                if static:
                    self._static_methods[qname] = static
                for m in decl.methods:
                    self._fn_param_tys[f"{qname}__{m.name}"] = [
                        self._ast_ty(p.type) for p in m.params
                    ]
            elif isinstance(decl, Struct) and decl.generics:
                self._generic_structs[decl.name] = decl
            elif isinstance(decl, Function) and decl.generics:
                self._generic_fns[decl.name] = decl
            elif isinstance(decl, Function):
                self._fn_param_tys[self._q(decl.name)] = [
                    self._ast_ty(p.type) for p in decl.params
                ]
            elif isinstance(decl, ExternDecl):
                # Externs link to bare C symbols regardless of which
                # module declares them, so the index key is the bare
                # name (no module prefix).  The variadic flag drives
                # the QBE `...` separator at call sites.
                self._fn_param_tys[decl.name] = [
                    self._ast_ty(p.type) for p in decl.params
                ]
                self._extern_decls.add(decl.name)
                if decl.is_variadic:
                    self._extern_variadic.add(decl.name)
            elif isinstance(decl, TopLevelBinding):
                # `name: var[T] = const_init` lands a backing data
                # slot; `name: T = expr` (a constant) is a name-only
                # alias today and lives only in the typechecker's
                # symbol table — no codegen emission until we add
                # comptime evaluation for non-trivial constants.
                ann_ty = self._ast_ty(decl.type) if decl.type else UNKNOWN
                if (
                    isinstance(ann_ty, WrapperTy)
                    and ann_ty.wrapper == "var"
                ):
                    inner = _readable(ann_ty)
                    self._module_vars[decl.name] = inner
                    init = self._const_init(decl.value, inner)
                    size = _size(inner, self._layouts)
                    align = min(size if size > 0 else 1, 8)
                    self._data.append(
                        f"data ${self._q(decl.name)} = "
                        f"align {align} {{ {init} }}"
                    )

        # Second pass: resolve layouts in dependency order via DFS.  A
        # field of bare `StructTy` requires its target's layout to be
        # built first (we need its size for our field offsets).
        decl_by_qname = {self._q(d.name): d for d in struct_decls}
        building: set[str] = set()

        def build(qname: str) -> None:
            if qname in self._layouts or qname not in decl_by_qname:
                return
            if qname in building:
                # Cycle: an inline self-composition is unsupported (it
                # would have infinite size).  Leave the layout missing
                # — downstream `_size` falls through to 8 for any
                # unresolved name.  The typechecker should refuse this
                # case; for now we just don't crash.
                return
            building.add(qname)
            decl = decl_by_qname[qname]
            for f in decl.fields:
                fty = self._ast_ty(f.type)
                if isinstance(fty, StructTy):
                    dep = self._q(_struct_name(fty))
                    if dep in decl_by_qname:
                        build(dep)
            fields = [(f.name, self._ast_ty(f.type)) for f in decl.fields]
            has_drop = any(m.name == "__drop__" for m in decl.methods)
            self._layouts[qname] = _layout(qname, fields, has_drop, self._layouts)
            building.discard(qname)

        for d in struct_decls:
            build(self._q(d.name))

    def _collect_specializations(self, tree: File) -> dict[str, tuple[Struct, dict[str, Ty]]]:
        """Walk the TypeMap, collecting every (base, type_args) instantiation
        of a generic struct.  Returns a dict keyed by specialized name
        (`Box_i32`) → (original generic Struct AST, substitution dict).

        Skips "self-instantiations" where the args contain a TypeParamTy
        — those are the typechecker's view of the generic from inside
        its own body, not a real instantiation request.
        """
        specs: dict[str, tuple[Struct, dict[str, Ty]]] = {}

        def has_typeparam(ty: Ty) -> bool:
            if isinstance(ty, TypeParamTy):
                return True
            if isinstance(ty, StructTy):
                return any(has_typeparam(a) for a in ty.type_args)
            if isinstance(ty, WrapperTy):
                return has_typeparam(ty.inner)
            if isinstance(ty, SliceTy):
                return has_typeparam(ty.element)
            if isinstance(ty, UnionTy):
                return any(has_typeparam(v) for v in ty.variants)
            return False

        def visit(ty: Ty) -> None:
            if isinstance(ty, StructTy) and ty.type_args:
                if not has_typeparam(ty):
                    name = _struct_name(ty)
                    # Local generic struct → register on this codegen.
                    if ty.name in self._generic_structs:
                        if name not in specs:
                            decl = self._generic_structs[ty.name]
                            subst = dict(zip(decl.generics, ty.type_args))
                            specs[name] = (decl, subst)
                    else:
                        # Cross-module: find the defining module and
                        # register the spec there so its drain pass
                        # emits the body under the right prefix.  We
                        # also immediately add the specialized
                        # layout on the foreign side, so call-site
                        # resolution in *this* module can find
                        # `_q(spec_name)` in `other_cg._layouts`
                        # before the drain runs.
                        for other_cg in self._all_imports():
                            if ty.name in other_cg._generic_structs:
                                decl = other_cg._generic_structs[ty.name]
                                subst = dict(zip(decl.generics, ty.type_args))
                                if name not in other_cg._struct_specs:
                                    other_cg._struct_specs[name] = (decl, subst)
                                other_cg._add_specialized_layout(
                                    name, decl, subst
                                )
                                break
                for a in ty.type_args:
                    visit(a)
            elif isinstance(ty, WrapperTy):
                visit(ty.inner)
            elif isinstance(ty, SliceTy):
                visit(ty.element)
            elif isinstance(ty, UnionTy):
                for v in ty.variants:
                    visit(v)

        for t in self._types._types.values():
            visit(t)
        # Locally-collected specs are also stashed onto the class
        # so the post-main drain (and cross-module routing in other
        # codegens) can see what's already been picked up.
        for k, v in specs.items():
            self._struct_specs.setdefault(k, v)
        return specs

    def _route_generic_fn_call(
        self,
        fn_name: str,
        expr: Call,
        explicit_args: Optional[tuple[Ty, ...]],
        ctx: Optional[FnCtx] = None,
    ) -> str:
        """Compute the specialized symbol name for a generic-fn call and
        register the (decl, subst) for later body emission.  Returns the
        bare name (without the `$` sigil).

        If *explicit_args* is given (e.g. `id[i32](42)`), it overrides
        argument-driven inference.  When *ctx* is provided, Name args
        bring their declared (wrapper-faithful) type into unification —
        the typemap's `_readable`-stripped form loses the rc/arc tag,
        which would specialize a generic on the unwrapped struct and
        break the ABI for callers passing refcounted values.
        """
        decl = self._generic_fns[fn_name]
        if explicit_args is not None and len(explicit_args) == len(decl.generics):
            subst = dict(zip(decl.generics, explicit_args))
        else:
            arg_tys = [self._arg_decl_ty(arg.value, ctx) for arg in expr.args]
            subst = self._infer_fn_subst(decl, arg_tys)
        spec_name = self._spec_fn_name(decl, subst)
        self._fn_specs.setdefault(spec_name, (decl, subst))
        # Record this spec's substituted param types so call sites can
        # select the arg ABI from the callee's view.
        spec_qname = self._q(spec_name)
        if spec_qname not in self._fn_param_tys:
            generic_params = set(decl.generics)
            self._fn_param_tys[spec_qname] = [
                _subst(self._ast_ty_with_generics(p.type, generic_params), subst)
                for p in decl.params
            ]
        return spec_name

    def _arg_decl_ty(self, expr: Any, ctx: Optional[FnCtx]) -> Ty:
        """Wrapper-faithful type for a call-site argument.  Names look
        up their declared type from the local table (which preserves
        `rc[T]`/`arc[T]` etc.); other expression kinds fall back to the
        typemap's recorded type."""
        if ctx is not None and isinstance(expr, Name) and expr.name in ctx.locals:
            return ctx.locals[expr.name].ty
        return self._ty(expr)

    def _spec_fn_name(self, fn: Function, subst: dict[str, Ty]) -> str:
        """Symbol name for a generic free-function specialization.
        Mirrors `_struct_name`'s encoding: `id_i32`, `pair_i32_i64`, etc.
        """
        parts = [_ty_name_part(subst.get(tp, UNKNOWN)) for tp in fn.generics]
        return f"{fn.name}_{'_'.join(parts)}"

    def _ast_ty_with_generics(self, t: Any, generic_params: set[str]) -> Ty:
        """Like `_ast_ty`, but resolves names in *generic_params* to
        TypeParamTy so they participate in unification.
        """
        if isinstance(t, NamedType):
            if t.name in generic_params:
                return TypeParamTy(t.name)
            return PrimTy(t.name) if t.name in BUILTIN_TYPES else StructTy(t.name)
        if isinstance(t, SliceType):
            return SliceTy(self._ast_ty_with_generics(t.element, generic_params))
        if isinstance(t, WrapperType):
            inner = (
                UNKNOWN
                if isinstance(t.inner, InferType)
                else self._ast_ty_with_generics(t.inner, generic_params)
            )
            return WrapperTy(t.wrapper, inner)
        if isinstance(t, GenericType):
            args = tuple(self._ast_ty_with_generics(a, generic_params) for a in t.args)
            return StructTy(t.base, args)
        if isinstance(t, UnionType):
            return UnionTy(
                frozenset(self._ast_ty_with_generics(v, generic_params) for v in t.variants)
            )
        return UNKNOWN

    def _infer_fn_subst(self, fn: Function, arg_tys: list[Ty]) -> dict[str, Ty]:
        """Infer the type-parameter substitution for a generic-fn call by
        unifying each declared parameter type against the corresponding
        argument type. Skips params with unknown arg types.
        """
        subst: dict[str, Ty] = {}
        generic_params = set(fn.generics)
        for p, aty in zip(fn.params, arg_tys):
            if isinstance(aty, UnknownTy):
                continue
            _unify(self._ast_ty_with_generics(p.type, generic_params), aty, subst)
        return subst

    def _add_specialized_layout(self, spec_name: str, decl: Struct, subst: dict[str, Ty]) -> None:
        """Compute and register the StructLayout for a generic struct
        specialization (e.g. Box_i32).  Field types are substituted via
        the active subst so layout uses concrete sizes/alignments.
        The stored layout name is module-prefixed.
        """
        qname = self._q(spec_name)
        if qname in self._layouts:
            return
        prev = self._current_subst
        self._current_subst = subst
        try:
            fields = [(f.name, self._ast_ty(f.type)) for f in decl.fields]
        finally:
            self._current_subst = prev
        has_drop = any(m.name == "__drop__" for m in decl.methods)
        self._layouts[qname] = _layout(qname, fields, has_drop, self._layouts)
        static = {m.name for m in decl.methods if m.self_param is None}
        if static:
            self._static_methods[qname] = static
        # Index this spec's method param types (already substituted via
        # _current_subst above) so call sites can pick the arg ABI.
        prev = self._current_subst
        self._current_subst = subst
        try:
            for m in decl.methods:
                self._fn_param_tys[f"{qname}__{m.name}"] = [
                    self._ast_ty(p.type) for p in m.params
                ]
        finally:
            self._current_subst = prev

    # ── File ──────────────────────────────────────────────────────────────────

    def generate(self, tree: File) -> tuple[str, str]:
        """Return `(qbe_ir, asm_sidecar)`.

        The first string is QBE IR text — feed it to `qbe`.  The
        second is raw assembly emitted from `asm fn` declarations;
        append it to QBE's assembly output before invoking the
        assembler.  When no `asm fn` decls are present, the sidecar
        is an empty string.
        """
        # Derive a per-module internal-label prefix from the source
        # path so labels like `_s0` / `_union_drop_0` don't collide
        # across modules whose user-visible prefix is empty (entry +
        # runtime/*.ou both have `_module_prefix == ""`).
        from pathlib import Path as _P
        stem = _P(tree.path).stem or "mod"
        # Sanitize: keep only [A-Za-z0-9_].
        self._internal_id = "".join(
            c if (c.isalnum() or c == "_") else "_" for c in stem
        )
        # `embed("path")` resolves paths relative to this module's
        # source directory.  Captured here so call-site emission can
        # find the right base without re-deriving from `tree.path`.
        self._source_dir = _P(tree.path).parent if tree.path else None
        self._collect_layouts(tree)
        # Register generic-struct specialization layouts + static-method
        # tables BEFORE emitting any function body, so call-site name
        # resolution sees them.
        specs = self._collect_specializations(tree)
        for spec_name, (decl, subst) in specs.items():
            self._add_specialized_layout(spec_name, decl, subst)

        fn_parts: list[str] = []

        for decl in tree.declarations:
            if isinstance(decl, Function) and not decl.generics:
                fn_parts.append(self._emit_fn(decl, struct_name=None))
            elif isinstance(decl, Struct) and not decl.generics:
                for m in decl.methods:
                    if not m.generics:
                        fn_parts.append(self._emit_fn(m, struct_name=decl.name))

        # Emit each specialization's methods under the substitution.
        for spec_name, (decl, subst) in specs.items():
            prev = self._current_subst
            self._current_subst = subst
            try:
                for m in decl.methods:
                    if not m.generics:
                        fn_parts.append(self._emit_fn(m, struct_name=spec_name))
            finally:
                self._current_subst = prev
            self._emitted_struct_specs.add(spec_name)

        # Drain the generic-fn specialization worklist.  Each emission
        # may discover further specializations (nested generic calls),
        # so loop until the dict stops growing.  The set of already-
        # emitted spec names is a class member so the driver can
        # call `drain_new_specs()` again later — picking up specs
        # registered by another module's cross-module calls.
        fn_parts.extend(self._drain_new_specs())

        out: list[str] = []
        # QBE aggregate type declarations
        for lay in self._layouts.values():
            field_types = ", ".join(_base(f.ty) for f in lay.fields)
            out.append(f"type :{lay.name} = {{ {field_types} }}")
        if self._layouts:
            out.append("")
        # Data section (populated during fn emission)
        out.extend(self._data)
        if self._data:
            out.append("")
        out.extend(fn_parts)
        # Union drop_fn helpers (populated by _get_or_emit_union_drop)
        if self._union_drop_emit:
            out.append("")
            out.extend(self._union_drop_emit)
        # Per-struct drop wrappers (populated by _get_or_emit_struct_drop)
        if self._struct_drop_emit:
            out.append("")
            out.extend(self._struct_drop_emit)
        # Match-on-`[]u8` slice-equality helper (emitted at most once).
        if self._slice_eq_u8_emitted:
            out.append("")
            out.append(_SLICE_EQ_U8_IR)
        # `hash[[]u8]` helper (FNV-1a).
        if self._slice_hash_u8_emitted:
            out.append("")
            out.append(_SLICE_HASH_U8_IR)

        asm_sidecar = self._emit_asm_sidecar(tree)
        return "\n".join(out), asm_sidecar

    def _drain_new_specs(self) -> list[str]:
        """Emit every generic spec (fn or struct) in our registries
        that hasn't been emitted yet.  Loops because a spec body may
        itself trigger further specializations; returns the
        accumulated IR.  The driver calls this again after every
        module's main pass so cross-module spec requests (registered
        by another codegen) get picked up here.

        Union / struct drop helpers freshly added during the drain
        (e.g. a generic spec's body references `T | Null`)
        also need to be emitted; we capture them by snapshotting
        the helper lists before/after the drain and returning the
        delta alongside the spec bodies.
        """
        union_drop_pre = len(self._union_drop_emit)
        struct_drop_pre = len(self._struct_drop_emit)
        slice_eq_pre = self._slice_eq_u8_emitted
        slice_hash_pre = self._slice_hash_u8_emitted
        emitted: list[str] = []
        while True:
            # Drain struct specs first — they may add fn specs.
            struct_pending = [
                k for k in self._struct_specs
                if k not in self._emitted_struct_specs
            ]
            for spec_name in struct_pending:
                decl, subst = self._struct_specs[spec_name]
                # Layout might already exist (from a prior collect);
                # the helper is idempotent.
                self._add_specialized_layout(spec_name, decl, subst)
                prev = self._current_subst
                self._current_subst = subst
                try:
                    for m in decl.methods:
                        if not m.generics:
                            emitted.append(
                                self._emit_fn(m, struct_name=spec_name)
                            )
                finally:
                    self._current_subst = prev
                self._emitted_struct_specs.add(spec_name)

            fn_pending = [
                k for k in self._fn_specs if k not in self._emitted_fn_specs
            ]
            if not fn_pending and not struct_pending:
                break
            for spec_name in fn_pending:
                decl, subst = self._fn_specs[spec_name]
                prev = self._current_subst
                self._current_subst = subst
                try:
                    emitted.append(
                        self._emit_fn(
                            decl, struct_name=None, qname_override=spec_name
                        )
                    )
                finally:
                    self._current_subst = prev
                self._emitted_fn_specs.add(spec_name)
        # Capture helpers added during this drain.  Spec bodies may
        # reference union types (`T | Null`), trigger new auto
        # drop wrappers, or call the singleton slice-eq / slice-hash
        # helpers via the `eq[T]` / `hash[T]` intrinsics.
        emitted.extend(self._union_drop_emit[union_drop_pre:])
        emitted.extend(self._struct_drop_emit[struct_drop_pre:])
        if self._slice_eq_u8_emitted and not slice_eq_pre:
            emitted.append(_SLICE_EQ_U8_IR)
        if self._slice_hash_u8_emitted and not slice_hash_pre:
            emitted.append(_SLICE_HASH_U8_IR)
        return emitted

    def has_pending_specs(self) -> bool:
        """True if any spec registry contains entries not yet drained
        by `_drain_new_specs`.  Used by the driver's fixed-point loop."""
        if any(k not in self._emitted_fn_specs for k in self._fn_specs):
            return True
        return any(
            k not in self._emitted_struct_specs for k in self._struct_specs
        )

    def _all_imports(self) -> list["Codegen"]:
        """All codegens reachable through the import graph (DFS,
        de-duplicated, this codegen excluded).  Used to find the
        defining module of a struct/type when the value's `StructTy`
        carries only the bare name."""
        if self._transitive_imports_cache is not None:
            return self._transitive_imports_cache
        seen: set[int] = set()
        out: list["Codegen"] = []
        stack: list["Codegen"] = list(self._module_imports.values())
        while stack:
            cg = stack.pop()
            if id(cg) in seen:
                continue
            seen.add(id(cg))
            out.append(cg)
            stack.extend(cg._module_imports.values())
        self._transitive_imports_cache = out
        return out

    def _register_cross_module_spec(
        self,
        fn_name: str,
        call_expr: Call,
        caller_ctx: FnCtx,
        caller_cg: "Codegen",
        explicit_args: Optional[tuple[Ty, ...]] = None,
    ) -> str:
        """Route a `module.fn(args)` call where `fn` is a generic free
        function declared in *this* module.  Mirrors
        `_route_generic_fn_call` but uses the *caller's* typemap and
        local table to compute the substitution — the arg expressions
        belong to the caller's AST.

        With *explicit_args* (e.g. `mod.fn[i32, u64](...)`), the
        substitution is taken directly from those type args;
        otherwise the codegen unifies argument types against the
        declared parameter types.

        Returns the unmangled spec name; the body is emitted later
        when the driver re-runs `_drain_new_specs`.  Also populates
        `_fn_param_tys` so the caller's ABI-selection logic can
        consult the substituted param types of this spec.
        """
        decl = self._generic_fns[fn_name]
        if explicit_args is not None and len(explicit_args) == len(decl.generics):
            subst = dict(zip(decl.generics, explicit_args))
        else:
            arg_tys = [
                caller_cg._arg_decl_ty(arg.value, caller_ctx) for arg in call_expr.args
            ]
            subst = self._infer_fn_subst(decl, arg_tys)
        spec_name = self._spec_fn_name(decl, subst)
        self._fn_specs.setdefault(spec_name, (decl, subst))
        spec_qname = self._q(spec_name)
        if spec_qname not in self._fn_param_tys:
            generic_params = set(decl.generics)
            self._fn_param_tys[spec_qname] = [
                _subst(self._ast_ty_with_generics(p.type, generic_params), subst)
                for p in decl.params
            ]
        return spec_name

    def _emit_asm_sidecar(self, tree: File) -> str:
        """Build the per-module asm sidecar from every `asm` decl.

        Format: one global function per decl, with a `# from path:line`
        comment preceding each body line so assembler errors trace back
        to the source.  Symbol names follow the same `_q` mangling as
        regular fns, so they line up with the codegen's call sites.
        """
        parts: list[str] = []
        has_any = any(isinstance(d, AsmDecl) for d in tree.declarations)
        if has_any:
            # QBE's output ends with a section switch
            # (.note.GNU-stack) — re-enter .text so our symbols land
            # in the executable section.
            parts.append("    .text")
        for decl in tree.declarations:
            if not isinstance(decl, AsmDecl):
                continue
            sym = self._q(decl.name)
            header_line = decl.span.start_line
            parts.append(f"    .globl {sym}")
            parts.append(f"    .type  {sym}, @function")
            parts.append(f"{sym}:")
            # Each body line gets a # from <path>:N comment.  Strip a
            # trailing blank line if present (the lexer may capture an
            # extra "" when the body ends right before the next decl).
            body_lines = decl.body_text.split("\n")
            while body_lines and body_lines[-1].strip() == "":
                body_lines.pop()
            for i, line in enumerate(body_lines):
                src_line = header_line + 1 + i  # body starts one line below header
                parts.append(f"    {line}    # from {decl.span.file}:{src_line}")
            parts.append("")  # blank line between decls
        return "\n".join(parts)

    # ── Function ──────────────────────────────────────────────────────────────

    def _emit_fn(
        self,
        fn: Function,
        struct_name: Optional[str],
        qname_override: Optional[str] = None,
    ) -> str:
        ctx = FnCtx()
        ctx.current_struct = struct_name
        if qname_override is not None:
            qname = f"${self._q(qname_override)}"
        else:
            qname = (
                f"${self._q(struct_name)}__{fn.name}"
                if struct_name
                else f"${self._q(fn.name)}"
            )
        export = "export " if fn.name == "main" else ""

        # Return type — resolve `Self` (as SelfType or NamedType("Self"))
        # to the enclosing struct's type when emitting a method.
        rt = fn.return_type
        if rt is None:
            ret_ann: Ty = UNIT
        elif struct_name is not None and (
            isinstance(rt, SelfType)
            or (isinstance(rt, NamedType) and rt.name == "Self")
        ):
            ret_ann = StructTy(struct_name)
        else:
            ret_ann = self._ast_ty(rt)

        # Detect union return: box {tag: l, payload: l} returned by ptr (l).
        # Build the canonical sorted-by-_fmt variant list for tag assignment.
        # Also fires for `rc[Union]` / `arc[Union]` / `var[Union]` returns —
        # the box itself is reference-counted; we still need the tag.
        union_ret: Optional[UnionTy] = None
        ret_check = ret_ann
        while isinstance(ret_check, WrapperTy) and ret_check.wrapper in (
            "var", "const", "rc", "arc"
        ):
            ret_check = ret_check.inner
        if isinstance(ret_check, UnionTy):
            union_ret = ret_check
        if fn.return_type is not None and union_ret is not None:
            ctx.union_return_variants = sorted(union_ret.variants, key=_fmt)
            ret_qbe = "l"
        elif _is_stack_struct_return(ret_ann):
            # Bare-struct return → QBE aggregate type (`function :Foo $f`).
            # QBE handles the SystemV ABI for us: small structs in regs,
            # larger via sret-like.  The callee's `ret %slot` copies
            # bytes from the slot into the return convention.
            ret_qbe = f":{self._q(_struct_name(ret_ann))}"
            ctx.is_aggregate_return = True
        else:
            ret_qbe = _base(ret_ann) if not isinstance(ret_ann, (UnitTy, UnknownTy)) else ""

        # Parameters.  Bare-struct params use QBE's `:Foo` aggregate-by-
        # value calling convention — QBE handles the SystemV copy at the
        # call boundary, so `%name` inside the body is a pointer to the
        # callee-side copy (lifetimes are owned by this frame).
        params: list[str] = []
        param_drops: list[tuple[str, str]] = []
        if fn.self_param is not None:
            params.append("l %self")
            ctx.locals["self"] = Local("%self", WrapperTy("ptr", UNKNOWN), False)
        for p in fn.params:
            pty = self._ast_ty(p.type)
            bare = isinstance(pty, StructTy)
            if bare:
                qn = self._q(_struct_name(pty))
                params.append(f":{qn} %{p.name}")
                lay = self._layouts.get(qn)
                if lay is not None:
                    drop_fn = self._struct_drop_fn(lay)
                    if drop_fn != "0":
                        param_drops.append((f"%{p.name}", drop_fn))
            else:
                params.append(f"{_base(pty)} %{p.name}")
            ctx.locals[p.name] = Local(
                f"%{p.name}", pty, False, is_bare_struct=bare
            )

        # Variadic body declarations get a trailing `, ...` in the
        # QBE signature so `vastart` / `vaarg` resolve at runtime.
        params_str = ', '.join(params)
        if fn.is_variadic:
            params_str = f"{params_str}, ..." if params_str else "..."
        sig = f"{export}function {ret_qbe + ' ' if ret_qbe else ''}{qname}({params_str}) {{"

        # Outer scope holds the param-owned drops (bare-struct value
        # params).  _emit_block(fn.body) pushes an inner scope on top;
        # `return` / fall-through both walk all scopes, so params drop
        # exactly once at function exit.
        ctx.managed_stack.append([])
        ctx.stack_drop_stack.append(param_drops)

        ctx.label("@start")
        self._emit_block(fn.body, ctx)

        # Ensure the last block has a terminator.  Implicit fall-through to
        # function end releases all managed locals (none are being returned).
        last = ctx.out[-1].strip() if ctx.out else ""
        if not (last.startswith("ret") or last.startswith("jmp") or last.startswith("jnz")):
            self._emit_releases(ctx)
            ctx.emit("ret")

        ctx.managed_stack.pop()
        ctx.stack_drop_stack.pop()

        body_lines = list(ctx.out)
        if ctx.prologue:
            for i, line in enumerate(body_lines):
                if line == "@start":
                    body_lines[i + 1 : i + 1] = ctx.prologue
                    break
        return "\n".join([sig, *body_lines, "}", ""])

    # ── Block & statements ────────────────────────────────────────────────────

    def _emit_block(self, block: Block, ctx: FnCtx) -> None:
        """Emit a Block, opening a managed-locals scope for it.

        On normal exit (last emitted line is not a terminator), the
        scope's managed locals are arc_released. The scope is then
        popped from managed_stack.
        """
        ctx.managed_stack.append([])
        ctx.stack_drop_stack.append([])
        for stmt in block.statements:
            self._emit_stmt(stmt, ctx)
        last = ctx.out[-1].strip() if ctx.out else ""
        if not (last.startswith("ret") or last.startswith("jmp") or last.startswith("jnz")):
            self._emit_scope_releases(ctx, ctx.managed_stack[-1])
            self._emit_scope_stack_drops(ctx, ctx.stack_drop_stack[-1])
        ctx.managed_stack.pop()
        ctx.stack_drop_stack.pop()

    def _emit_stmt(self, stmt: Any, ctx: FnCtx) -> None:
        if isinstance(stmt, Pass):
            return
        if isinstance(stmt, ExprStatement):
            self._emit_expr(stmt.expr, ctx)
        elif isinstance(stmt, Binding):
            self._emit_binding(stmt, ctx)
        elif isinstance(stmt, Assignment):
            self._emit_assignment(stmt, ctx)
        elif isinstance(stmt, Return):
            self._emit_return(stmt, ctx)
        elif isinstance(stmt, Break):
            if ctx.loop_stack:
                _, brk, body_depth = ctx.loop_stack[-1]
                self._emit_releases_to_depth(ctx, body_depth)
                ctx.emit(f"jmp {brk}")
        elif isinstance(stmt, Continue):
            if ctx.loop_stack:
                cont, _, body_depth = ctx.loop_stack[-1]
                self._emit_releases_to_depth(ctx, body_depth)
                ctx.emit(f"jmp {cont}")
        elif isinstance(stmt, Loop):
            self._emit_loop(stmt.body, ctx)
        elif isinstance(stmt, For):
            self._emit_for(stmt, ctx)
        elif isinstance(stmt, Block):
            self._emit_block(stmt, ctx)

    def _emit_binding(self, stmt: Binding, ctx: FnCtx) -> None:
        if stmt.name == "_":
            self._emit_expr(stmt.value, ctx)
            return

        # Re-assignment into a module-level mutable global.  The
        # parser emits this as a `Binding` (no annotation), but if
        # the name resolves to a `MODULE_VAR` in scope we want a
        # plain store into the data slot — not a new local.
        if (
            stmt.type is None
            and stmt.name not in ctx.locals
            and stmt.name in self._module_vars
        ):
            ty = self._module_vars[stmt.name]
            val = self._emit_expr(stmt.value, ctx)
            ctx.emit(f"{_store(ty)} {val}, ${self._q(stmt.name)}")
            return

        # Re-assignment into existing var slot
        if stmt.type is None and stmt.name in ctx.locals:
            lv = ctx.locals[stmt.name]
            if lv.is_var:
                # var[Struct] in-place reassignment: re-run the slot's
                # `__drop__` (if any), then re-init from the RHS.
                # StructLiteral RHS writes fields directly; other
                # expressions `blit` from the source's address.
                if lv.is_bare_struct and isinstance(_unwrap(lv.ty), StructTy):
                    base_ty = _unwrap(lv.ty)
                    assert isinstance(base_ty, StructTy)
                    inner_lay = self._layouts.get(
                        self._q(_struct_name(base_ty))
                    )
                    if inner_lay is not None:
                        drop_fn = self._struct_drop_fn(inner_lay)
                        if drop_fn != "0":
                            ctx.emit(f"call {drop_fn}(l {lv.loc})")
                        if isinstance(stmt.value, StructLiteral):
                            self._emit_struct_lit(stmt.value, ctx, into=lv.loc)
                        else:
                            src = self._emit_expr(stmt.value, ctx)
                            if inner_lay.total > 0:
                                ctx.emit(
                                    f"blit {src}, {lv.loc}, {inner_lay.total}"
                                )
                        return
                val = self._emit_expr(stmt.value, ctx)
                if _is_managed(lv.ty):
                    # Inc-the-new-value before releasing-the-old protects
                    # against `x = x` where val and the slot's contents
                    # are the same pointer.
                    if self._is_borrowed_copy(stmt.value):
                        ctx.emit(f"call $arc_inc(l {val})")
                    old = ctx.tmp("old")
                    ctx.emit(f"{old} =l loadl {lv.loc}")
                    ctx.emit(f"call $arc_release(l {old})")
                ctx.emit(f"{_store(lv.ty)} {val}, {lv.loc}")
                return

        # Stack-allocated struct binding: bare StructTy on the LHS.
        # - StructLiteral RHS → emit `alloca` + populate fields in place.
        # - Call RHS to a bare-struct-returning fn → the call's result is
        #   already a pointer to a caller-side slot (QBE aggregate-return).
        # Both paths bind directly to the stack slot, track for direct
        # `__drop__` at scope exit, and skip `arc_release` entirely.
        # `rc[T]` / `arc[T]` annotations stay on the heap path; copy
        # bindings between bare structs aren't yet supported (a follow-up
        # slice will add memcpy semantics).
        decl_ty_initial = (
            self._ast_ty(stmt.type) if stmt.type else self._ty(stmt.value)
        )
        if (
            _is_stack_struct_binding(decl_ty_initial)
            and isinstance(stmt.value, Call)
            and _is_stack_struct_return(self._ty(stmt.value))
        ):
            assert isinstance(decl_ty_initial, StructTy)
            qname = self._q(_struct_name(decl_ty_initial))
            lay = self._layouts.get(qname)
            slot = self._emit_expr(stmt.value, ctx)
            ctx.locals[stmt.name] = Local(
                slot, decl_ty_initial, False, is_bare_struct=True
            )
            if lay is not None:
                drop_fn = self._struct_drop_fn(lay)
                if drop_fn != "0":
                    ctx.stack_drop_stack[-1].append((slot, drop_fn))
            return

        if (
            _is_stack_struct_binding(decl_ty_initial)
            and isinstance(stmt.value, StructLiteral)
        ):
            assert isinstance(decl_ty_initial, StructTy)
            qname = self._q(_struct_name(decl_ty_initial))
            lay = self._layouts.get(qname)
            if lay is not None:
                size = lay.total
                align = min(size, 8) if size > 0 else 1
                slot = ctx.tmp(f"sk_{stmt.name}")
                ctx.emit(f"{slot} =l alloc{align} {max(size, 1)}")
                self._emit_struct_lit(stmt.value, ctx, into=slot)
                ctx.locals[stmt.name] = Local(
                    slot, decl_ty_initial, False, is_bare_struct=True
                )
                drop_fn = self._struct_drop_fn(lay)
                if drop_fn != "0":
                    ctx.stack_drop_stack[-1].append((slot, drop_fn))
                return

        # `var[Struct]` binding: alloca with the struct's inline size
        # (not pointer-sized).  The slot holds the struct's bytes
        # directly so reassignment can be done in place via `blit` or
        # a struct-literal recurse.  Reading the binding returns the
        # slot's address — same shape as a stack-bare binding.
        if (
            isinstance(decl_ty_initial, WrapperTy)
            and decl_ty_initial.wrapper == "var"
            and isinstance(decl_ty_initial.inner, StructTy)
            and isinstance(stmt.value, StructLiteral)
        ):
            inner_ty = decl_ty_initial.inner
            qname = self._q(_struct_name(inner_ty))
            lay = self._layouts.get(qname)
            if lay is not None:
                size = lay.total
                align = min(size, 8) if size > 0 else 1
                slot = ctx.tmp(f"vk_{stmt.name}")
                ctx.emit(f"{slot} =l alloc{align} {max(size, 1)}")
                self._emit_struct_lit(stmt.value, ctx, into=slot)
                ctx.locals[stmt.name] = Local(
                    slot, decl_ty_initial, True, is_bare_struct=True
                )
                drop_fn = self._struct_drop_fn(lay)
                if drop_fn != "0":
                    ctx.stack_drop_stack[-1].append((slot, drop_fn))
                return

        val = self._emit_expr(stmt.value, ctx)
        decl_ty = self._ast_ty(stmt.type) if stmt.type else self._ty(stmt.value)
        src_ty = self._ty(stmt.value)
        is_var = isinstance(decl_ty, WrapperTy) and decl_ty.wrapper == "var"
        inner = _readable(decl_ty)

        # Auto-box a single-variant RHS into a tagged-union slot.
        # Mirror's `_emit_union_return`'s semantics: the box owns the
        # payload, so borrowed managed payloads get an arc_inc to
        # balance the box's drop_fn release later.
        variants = self._needs_variant_box(decl_ty, src_ty)
        if variants is not None:
            if _is_managed(src_ty) and self._is_borrowed_copy(stmt.value):
                ctx.emit(f"call $arc_inc(l {val})")
            val = self._box_into_union(val, src_ty, variants, ctx)
            src_ty = UnionTy(frozenset(variants))

        # If the RHS is a copy of an existing managed reference (Name or
        # FieldAccess), bump the refcount — both names now hold a
        # valid ref, and both will arc_release at scope end.  Fresh
        # allocations (Call, StructLiteral) already start at rc=1, so
        # no inc is needed.  Use the original `decl_ty` (with wrappers)
        # because `_is_managed` inspects the rc/arc/weak tag.
        if (
            variants is None
            and _is_managed(decl_ty)
            and self._is_borrowed_copy(stmt.value)
        ):
            ctx.emit(f"call $arc_inc(l {val})")

        # Widen a narrower value to match the declared base width
        # (`b: u64 = s[i]` where `s[i]` produces a w-typed byte).
        # Same lenient v1 widening as in `_emit_binary`.
        # Skip when we just boxed: the box pointer is already l-typed,
        # and reading the original w-typed base of stmt.value would
        # extsw the pointer and corrupt it.
        decl_base = _base(decl_ty)
        if variants is None:
            val_base = _base(self._ty(stmt.value))
            if decl_base == "l" and val_base == "w" and val.startswith("%"):
                widened = ctx.tmp("ext")
                ctx.emit(f"{widened} =l extsw {val}")
                val = widened

        if is_var:
            s = _size(inner)
            a = min(s, 8)
            slot = ctx.tmp(f"v_{stmt.name}")
            ctx.emit(f"{slot} =l alloc{a} {s}")
            ctx.emit(f"{_store(inner)} {val}, {slot}")
            ctx.locals[stmt.name] = Local(slot, decl_ty, True)
        else:
            ctx.locals[stmt.name] = Local(val, decl_ty, False)
            # Remember the actual register width: const bindings get
            # bound to the (possibly widened) temp, and the rest of
            # the codegen reads via `tmp_base` for cases where the
            # declared type isn't enough.
            if val.startswith("%"):
                ctx.tmp_base[val] = decl_base

        # Track managed locals in the current lexical scope so they
        # arc_release at scope exit (and at every `ret`).
        if _is_managed(decl_ty):
            ctx.managed_stack[-1].append(stmt.name)

    def _emit_return(self, stmt: Return, ctx: FnCtx) -> None:
        # Union return → box the value in a {tag: l, payload: l} ARC box.
        if ctx.union_return_variants is not None and stmt.value is not None:
            self._emit_union_return(stmt, ctx)
            return

        # Bare-struct return: function is declared `function :Foo $f(...)`.
        # If the value is a struct literal, allocate a stack slot and
        # populate it via `_emit_struct_lit(into=slot)`, then `ret %slot`
        # — QBE copies the aggregate into the return convention.  For
        # other expressions, we just `ret %v` (the value is already a
        # struct pointer).
        if (
            ctx.is_aggregate_return
            and stmt.value is not None
            and isinstance(stmt.value, StructLiteral)
        ):
            t = stmt.value.type
            sname: Optional[str] = None
            if isinstance(t, NamedType):
                sname = t.name
            elif isinstance(t, GenericType):
                spec_ty = self._ast_ty(t)
                sname = (
                    _struct_name(spec_ty) if isinstance(spec_ty, StructTy) else t.base
                )
            elif isinstance(t, SelfType) and ctx.current_struct is not None:
                sname = ctx.current_struct
            if sname == "Self" and ctx.current_struct is not None:
                sname = ctx.current_struct
            if sname is not None:
                lay = self._layouts.get(self._q(sname))
                if lay is not None:
                    size = max(lay.total, 1)
                    align = min(lay.total, 8) if lay.total > 0 else 1
                    slot = ctx.tmp("rslot")
                    ctx.emit(f"{slot} =l alloc{align} {size}")
                    self._emit_struct_lit(stmt.value, ctx, into=slot)
                    self._emit_releases(ctx)
                    ctx.emit(f"ret {slot}")
                    return

        # If returning a Name that's a managed local in any open scope,
        # that local is being transferred to the caller — skip its
        # release here.
        skip: Optional[str] = None
        if (
            stmt.value is not None
            and isinstance(stmt.value, Name)
            and any(stmt.value.name in scope for scope in ctx.managed_stack)
        ):
            skip = stmt.value.name

        if stmt.value is None:
            self._emit_releases(ctx, skip)
            ctx.emit("ret")
            return

        v = self._emit_expr(stmt.value, ctx)
        # If we're returning a borrowed managed reference — a Name to a
        # param (not in managed_locals) or a FieldAccess result — the
        # caller's binding needs its own rc.  Owned-local transfers
        # (skip != None) already hand off rc=1, so no inc there.
        decl_ty = self._expr_decl_ty(stmt.value, ctx)
        expr_ty = self._ty(stmt.value)
        # Union-narrow short-circuit: when the local is a UnionTy but
        # `?=` has narrowed the return path to a non-managed variant
        # (the payload escapes as a primitive), the declared "managed
        # union" flag would wrongly fire arc_inc on the unwrapped
        # payload value.
        narrowed_out = (
            isinstance(decl_ty, UnionTy)
            and not isinstance(expr_ty, UnionTy)
            and not _is_managed(expr_ty)
        )
        if (
            skip is None
            and not narrowed_out
            and _is_managed(decl_ty)
            and self._is_borrowed_copy(stmt.value)
        ):
            ctx.emit(f"call $arc_inc(l {v})")
        self._emit_releases(ctx, skip)
        ctx.emit(f"ret {v}")

    def _box_into_union(
        self,
        val: str,
        val_ty: Ty,
        variants: list[Ty],
        ctx: "FnCtx",
    ) -> str:
        """Wrap `val` (already-emitted, a single variant value) in a
        {tag: l, payload: l} ARC box matching `variants` (sorted by
        `_fmt`).  Returns the boxed pointer.  Caller is responsible
        for arc_inc-ing borrowed managed payloads — this helper only
        does the allocation, tag/payload store, and width-fix-up.
        """
        tag = self._union_tag_for(val_ty, variants)
        drop_fn = self._get_or_emit_union_drop(variants)
        box = ctx.tmp("box")
        ctx.emit(f"{box} =l call $arc_alloc(l 16, l {drop_fn})")
        ctx.emit(f"storel {tag}, {box}")
        payload = ctx.tmp("paddr")
        ctx.emit(f"{payload} =l add {box}, 8")
        # Payload slot is l-wide; sign-extend narrow integer temps.
        if val.startswith("%") and _base(val_ty) == "w":
            ext = ctx.tmp("ext")
            ctx.emit(f"{ext} =l extsw {val}")
            val = ext
        ctx.emit(f"storel {val}, {payload}")
        return box

    def _needs_variant_box(self, target_ty: Ty, src_ty: Ty) -> Optional[list[Ty]]:
        """If a value of `src_ty` is being assigned to a slot typed
        `target_ty` and the target is (eventually) a UnionTy while the
        source is one specific non-union variant, return the sorted
        variant list (for tag lookup).  Otherwise None.
        """
        # Peel storage wrappers from the target — `arc[Union]`, `rc[Union]`,
        # `var[Union]` all need the same boxing.
        t = target_ty
        while isinstance(t, WrapperTy) and t.wrapper in ("var", "const", "rc", "arc"):
            t = t.inner
        if not isinstance(t, UnionTy):
            return None
        # Source must be a concrete variant — bail when it's already
        # a union (already boxed), unknown, or otherwise unhelpful.
        s = src_ty
        while isinstance(s, WrapperTy) and s.wrapper in ("var", "const", "rc", "arc"):
            s = s.inner
        if isinstance(s, (UnionTy, UnknownTy, TypeParamTy)):
            return None
        return sorted(t.variants, key=_fmt)

    def _emit_union_return(self, stmt: Return, ctx: FnCtx) -> None:
        """Box the return value into a {tag: l, payload: l} ARC box.

        Tag is the index of the value's type in ctx.union_return_variants
        (which is sorted by _fmt for determinism, so consumers can recover
        the same mapping from the union's UnionTy).

        Pass-through case: when the return expression *already* evaluates
        to a matching boxed union (e.g. delegating to another fn whose
        return is the same `T | Null` shape), reuse the box rather than
        re-wrapping it — wrapping would store the box-pointer in the
        payload slot of a fresh tag-0 box, which the caller then
        misreads as Null.
        """
        assert ctx.union_return_variants is not None
        assert stmt.value is not None

        v_ty_preview = _readable(_unwrap(self._ty(stmt.value)))
        if (
            isinstance(v_ty_preview, UnionTy)
            and sorted(v_ty_preview.variants, key=_fmt)
            == ctx.union_return_variants
        ):
            v = self._emit_expr(stmt.value, ctx)
            skip_passthrough: Optional[str] = None
            if isinstance(stmt.value, Name) and any(
                stmt.value.name in scope for scope in ctx.managed_stack
            ):
                skip_passthrough = stmt.value.name
            if skip_passthrough is None and self._is_borrowed_copy(stmt.value):
                ctx.emit(f"call $arc_inc(l {v})")
            self._emit_releases(ctx, skip_passthrough)
            ctx.emit(f"ret {v}")
            return

        v = self._emit_expr(stmt.value, ctx)
        v_ty = self._ty(stmt.value)
        tag = self._union_tag_for(v_ty, ctx.union_return_variants)

        # Allocate 16-byte ARC box (header + tag + payload).  The drop_fn
        # is a per-union helper that releases the payload when it points
        # at a managed (StructTy) variant; "0" when none of the variants
        # need releasing.
        drop_fn = self._get_or_emit_union_drop(ctx.union_return_variants)
        box = ctx.tmp("box")
        ctx.emit(f"{box} =l call $arc_alloc(l 16, l {drop_fn})")
        ctx.emit(f"storel {tag}, {box}")
        payload = ctx.tmp("paddr")
        ctx.emit(f"{payload} =l add {box}, 8")

        # Payload is l-wide; sign-extend narrow integer temps.
        if v.startswith("%") and _base(v_ty) == "w":
            ext = ctx.tmp("ext")
            ctx.emit(f"{ext} =l extsw {v}")
            v = ext

        # Returning a Name that's a managed local consumes it.
        skip: Optional[str] = None
        if isinstance(stmt.value, Name) and any(
            stmt.value.name in scope for scope in ctx.managed_stack
        ):
            skip = stmt.value.name

        # Borrowed-into-box: if the payload is a managed Name/FieldAccess
        # that we DIDN'T consume (param, field), the box now holds an
        # extra ref to it.  Inc so the box's drop_fn release balances
        # against the original source's refcount.
        if (
            skip is None
            and _is_managed(self._expr_decl_ty(stmt.value, ctx))
            and self._is_borrowed_copy(stmt.value)
        ):
            ctx.emit(f"call $arc_inc(l {v})")
        ctx.emit(f"storel {v}, {payload}")

        self._emit_releases(ctx, skip)
        ctx.emit(f"ret {box}")

    def _emit_cast(self, expr: Cast, ctx: FnCtx) -> str:
        """`x as T` — emit the conversion that gets `x`'s bytes
        into `T`'s register shape.

        Cases (typechecker has already rejected the unsupported
        ones, so we trust the inputs):
          - same QBE base (`w → w`, `l → l`, `s → s`, `d → d`): a
            `copy` is enough.
          - `w → l`: sign-extend (`extsw`) for signed sources,
            zero-extend (`extuw`) for unsigned.
          - `l → w`: implicit truncation via `copy`.
          - `int → float`: `stosi`/`stoui`/`dtosi`/`dtoui` per the
            source signedness and target width.
          - `float → int`: `stosi`/`stoui`/`dtosi`/`dtoui` per the
            target signedness and source width.
          - `f32 ↔ f64`: `truncd` (d → s) / `exts` (s → d).
          - `ptr ↔ ptr`: `copy` (pointers are all `l`).
        """
        src_ty = self._ty(expr.operand)
        tgt_ty = self._ast_ty(expr.type)
        sb = _base(src_ty)
        tb = _base(tgt_ty)
        v = self._emit_expr(expr.operand, ctx)
        tmp = ctx.tmp("cv")

        if sb == tb:
            ctx.emit(f"{tmp} ={tb} copy {v}")
            return tmp

        # Integer widening / narrowing.
        if sb == "w" and tb == "l":
            src_inner = _readable(_unwrap(src_ty))
            signed = (
                isinstance(src_inner, PrimTy)
                and src_inner.name in ("i8", "i16", "i32", "i64", "isize")
            )
            op = "extsw" if signed else "extuw"
            ctx.emit(f"{tmp} =l {op} {v}")
            return tmp
        if sb == "l" and tb == "w":
            ctx.emit(f"{tmp} =w copy {v}")
            return tmp

        # Float narrowing / widening.
        if sb == "d" and tb == "s":
            ctx.emit(f"{tmp} =s truncd {v}")
            return tmp
        if sb == "s" and tb == "d":
            ctx.emit(f"{tmp} =d exts {v}")
            return tmp

        # Float ↔ int — match on the source/target bases.  Signedness
        # is read off the *target* for int destinations and off the
        # *source* for int sources.
        tgt_inner = _readable(_unwrap(tgt_ty))
        src_inner = _readable(_unwrap(src_ty))
        if sb in ("s", "d") and tb in ("w", "l"):
            signed_t = (
                isinstance(tgt_inner, PrimTy)
                and tgt_inner.name in ("i8", "i16", "i32", "i64", "isize")
            )
            ftype = "s" if sb == "s" else "d"
            op = f"{ftype}to{'si' if signed_t else 'ui'}"
            ctx.emit(f"{tmp} ={tb} {op} {v}")
            return tmp
        if sb in ("w", "l") and tb in ("s", "d"):
            signed_s = (
                isinstance(src_inner, PrimTy)
                and src_inner.name in ("i8", "i16", "i32", "i64", "isize")
            )
            # Widen w → l first if needed so the conversion ops
            # always start from `l`.
            src_val = v
            if sb == "w":
                widened = ctx.tmp("ew")
                ext = "extsw" if signed_s else "extuw"
                ctx.emit(f"{widened} =l {ext} {v}")
                src_val = widened
            op = "sltof" if signed_s else "ultof"
            if tb == "s":
                ctx.emit(f"{tmp} =s s{op} {src_val}")
            else:
                ctx.emit(f"{tmp} =d {op} {src_val}")
            return tmp

        # Fallback: copy and hope for the best (typechecker already
        # validated the shapes).
        ctx.emit(f"{tmp} ={tb} copy {v}")
        return tmp

    def _emit_type_test(self, expr: TypeTest, ctx: FnCtx) -> str:
        """`x ?= T` — load the tag from x (a union box pointer) and compare
        it to T's tag in the operand's union.  Returns a w-typed bool (0/1).

        When the operand isn't a union type, returns 0 (placeholder).  The
        type checker doesn't yet refuse `?=` on non-union operands, so we
        degrade gracefully rather than crashing.
        """
        operand = self._emit_expr(expr.operand, ctx)
        op_ty = self._ty(expr.operand)

        if not isinstance(op_ty, UnionTy):
            tmp = ctx.tmp("tt")
            ctx.emit(f"{tmp} =w copy 0")
            ctx.tmp_base[tmp] = "w"
            return tmp

        # Compute the tag for the test type within the operand's union.
        # The producer (function return) sorts variants by _fmt; mirror
        # that here so producer and consumer agree on tag numbering.
        variants = sorted(op_ty.variants, key=_fmt)
        test_ty = self._ast_ty(expr.type)

        # The test type may itself be a union (e.g. `x ?= A | B`).  In
        # that case we test if the operand's tag matches ANY of the test's
        # variants.  v1 simplification: just match the first variant.
        if isinstance(test_ty, UnionTy):
            test_variants = sorted(test_ty.variants, key=_fmt)
            target = test_variants[0] if test_variants else None
        else:
            target = test_ty

        tag = -1
        if target is not None:
            for i, v in enumerate(variants):
                if v == target:
                    tag = i
                    break
        if tag < 0:
            tmp = ctx.tmp("tt")
            ctx.emit(f"{tmp} =w copy 0")
            ctx.tmp_base[tmp] = "w"
            return tmp

        # Load tag at offset 0 of the union box pointed to by `operand`.
        tag_val = ctx.tmp("tag")
        ctx.emit(f"{tag_val} =l loadl {operand}")
        result = ctx.tmp("tt")
        ctx.emit(f"{result} =w ceql {tag_val}, {tag}")
        ctx.tmp_base[result] = "w"
        return result

    def _get_or_emit_union_drop(self, variants: list[Ty]) -> str:
        """Return the symbol name of a drop_fn for a union with these
        variants — or "0" if no variant needs releasing.

        The function dispatches on the box's tag; for each tag whose
        variant is ARC-managed (StructTy), it arc_release()s the payload
        pointer. Memoized: every distinct union shape generates exactly
        one drop helper, regardless of how many call sites box that
        shape.
        """
        managed_tags = [
            i for i, v in enumerate(variants) if _is_union_payload_managed(v)
        ]
        if not managed_tags:
            return "0"

        key = "|".join(_fmt(v) for v in variants)
        cached = self._union_drop_fns.get(key)
        if cached is not None:
            return cached

        sym = f"$_union_drop_{self._internal_id}_{len(self._union_drop_fns)}"
        self._union_drop_fns[key] = sym

        lines: list[str] = []
        lines.append(f"function {sym}(l %box) {{")
        lines.append("@start")
        lines.append("    %tag =l loadl %box")
        lines.append("    %paddr =l add %box, 8")
        lines.append("    %payload =l loadl %paddr")

        # Linear dispatch: each managed tag checks then jumps to release.
        for i, tag in enumerate(managed_tags):
            if i > 0:
                lines.append(f"@chk_{i}")
            next_label = (
                f"@chk_{i + 1}" if i + 1 < len(managed_tags) else "@done"
            )
            lines.append(f"    %is_{i} =w ceql %tag, {tag}")
            lines.append(f"    jnz %is_{i}, @rel_{i}, {next_label}")

        for i in range(len(managed_tags)):
            lines.append(f"@rel_{i}")
            lines.append("    call $arc_release(l %payload)")
            lines.append("    jmp @done")

        lines.append("@done")
        lines.append("    ret")
        lines.append("}")

        self._union_drop_emit.append("\n".join(lines))
        return sym

    def _expr_decl_ty(self, expr: Any, ctx: FnCtx) -> Ty:
        """Wrapper-faithful type of an expression.  Name args bring
        their declared (pre-`_readable`) type from the local table;
        FieldAccess looks up the field's declared type from the
        struct's layout.  Everything else falls back to the typemap.
        """
        if isinstance(expr, Name) and expr.name in ctx.locals:
            return ctx.locals[expr.name].ty
        if isinstance(expr, FieldAccess):
            obj_ty = _unwrap(self._ty(expr.obj))
            if isinstance(obj_ty, StructTy):
                lay = self._layouts.get(self._q(_struct_name(obj_ty)))
                if lay is not None:
                    fl = next(
                        (f for f in lay.fields if f.name == expr.field), None
                    )
                    if fl is not None:
                        return fl.ty
        return self._ty(expr)

    def _is_bare_struct_value(self, expr: Any, ctx: FnCtx) -> bool:
        """True when *expr* yields a stack-bare-struct value (no wrapper)
        — i.e. should be passed by value via QBE's `:Foo` aggregate ABI.
        Disambiguates from `rc[T]` / `arc[T]` reads where the typemap
        also surfaces `T` (via `_readable`) but the runtime value is a
        refcounted heap pointer.  Uses the *declared* type for names and
        fields; for literals and calls the typemap's stored type is
        already wrapper-faithful.
        """
        at = self._ty(expr)
        if not isinstance(at, StructTy):
            return False
        if isinstance(expr, Name) and expr.name in ctx.locals:
            return ctx.locals[expr.name].is_bare_struct
        if isinstance(expr, FieldAccess):
            obj_ty = _unwrap(self._ty(expr.obj))
            if isinstance(obj_ty, StructTy):
                lay = self._layouts.get(self._q(_struct_name(obj_ty)))
                if lay is not None:
                    fl = next((f for f in lay.fields if f.name == expr.field), None)
                    if fl is not None:
                        return isinstance(fl.ty, StructTy)
            return False
        return True

    def _is_borrowed_copy(self, value_expr: Any) -> bool:
        """Semantic version of `_is_copy_of_existing_ref`: also returns
        False for weak-field reads, which look like FieldAccess but
        actually allocate a fresh `T | Null` box.
        """
        if not _is_copy_of_existing_ref(value_expr):
            return False
        if isinstance(value_expr, FieldAccess):
            obj_ty = _unwrap(self._ty(value_expr.obj))
            if isinstance(obj_ty, StructTy):
                lay = self._layouts.get(self._q(_struct_name(obj_ty)))
                if lay is not None:
                    fl = next(
                        (f for f in lay.fields if f.name == value_expr.field),
                        None,
                    )
                    if fl is not None and _is_weak_field(fl.ty):
                        return False
        return True

    def _struct_drop_fn(self, lay: StructLayout) -> str:
        """Return the drop_fn symbol for a struct's arc_alloc.

        - No managed fields, no __drop__ → "0".
        - No managed fields, has __drop__ → "$<Struct>____drop__" (direct).
        - Has managed fields → auto-generated wrapper that calls user's
          __drop__ (if any) and then arc_release()s each managed field.
        """
        if not lay.has_managed_fields:
            return f"${lay.name}____drop__" if lay.has_drop else "0"

        cached = self._struct_drop_wrappers.get(lay.name)
        if cached is not None:
            return cached

        sym = f"${lay.name}____drop_full__"
        self._struct_drop_wrappers[lay.name] = sym

        lines: list[str] = []
        lines.append(f"function {sym}(l %self) {{")
        lines.append("@start")

        # User's __drop__ runs first so it can still observe fields.
        if lay.has_drop:
            lines.append(f"    call ${lay.name}____drop__(l %self)")

        # Then clean up each field.  Three cases:
        #   - rc/arc field   → arc_release on the loaded pointer
        #   - weak field     → weak_release on the loaded pointer
        #   - inline struct  → recursive __drop__ on the field's address
        idx = 0
        for fl in lay.fields:
            if _is_managed(fl.ty):
                if fl.offset == 0:
                    lines.append(f"    %fv{idx} =l loadl %self")
                else:
                    lines.append(f"    %fp{idx} =l add %self, {fl.offset}")
                    lines.append(f"    %fv{idx} =l loadl %fp{idx}")
                lines.append(
                    f"    call {_field_release_call(fl.ty)}(l %fv{idx})"
                )
                idx += 1
            elif isinstance(fl.ty, StructTy):
                inner_lay = self._layouts.get(self._q(_struct_name(fl.ty)))
                if inner_lay is None:
                    continue
                inner_drop = self._struct_drop_fn(inner_lay)
                if inner_drop == "0":
                    continue
                if fl.offset == 0:
                    lines.append(f"    call {inner_drop}(l %self)")
                else:
                    lines.append(f"    %fp{idx} =l add %self, {fl.offset}")
                    lines.append(f"    call {inner_drop}(l %fp{idx})")
                    idx += 1

        lines.append("    ret")
        lines.append("}")

        self._struct_drop_emit.append("\n".join(lines))
        return sym

    def _union_tag_for(self, value_ty: Ty, variants: list[Ty]) -> int:
        """Find the tag of value_ty within variants.  Exact-match preferred,
        falling back to numeric-class match (lenient v1 widening)."""
        for i, v in enumerate(variants):
            if v == value_ty:
                return i
        # Fallback: numeric → first numeric variant
        if isinstance(value_ty, PrimTy) and value_ty.name in _NUMERIC:
            for i, v in enumerate(variants):
                if isinstance(v, PrimTy) and v.name in _NUMERIC:
                    return i
        # If nothing matches, default to tag 0 to avoid a crash.
        return 0

    def _emit_releases(self, ctx: FnCtx, skip: Optional[str] = None) -> None:
        """Emit arc_release for every managed local across ALL open
        scopes, except `skip` (if any).  Used before `ret` — the
        function is exiting, so every scope's contents need to drop.
        Walks scopes top-down so inner scope locals are released first.
        Stack-allocated struct locals get a direct `__drop__` call in
        the same top-down order.
        """
        for i in range(len(ctx.managed_stack) - 1, -1, -1):
            self._emit_scope_releases(ctx, ctx.managed_stack[i], skip)
            self._emit_scope_stack_drops(ctx, ctx.stack_drop_stack[i])

    def _emit_releases_to_depth(self, ctx: FnCtx, depth: int) -> None:
        """Emit arc_release + stack drops for scopes whose index is
        >= `depth` (top-down).  Used by `break`/`continue` — the jump
        exits scopes up to (and including) the loop body.
        """
        for i in range(len(ctx.managed_stack) - 1, depth - 1, -1):
            self._emit_scope_releases(ctx, ctx.managed_stack[i])
            self._emit_scope_stack_drops(ctx, ctx.stack_drop_stack[i])

    def _emit_scope_releases(
        self,
        ctx: FnCtx,
        scope: list[str],
        skip: Optional[str] = None,
    ) -> None:
        """Emit arc_release for every managed local in *scope*, except
        *skip* (if given).  Var-slot locals are loaded first.
        """
        for name in scope:
            if name == skip:
                continue
            lv = ctx.locals.get(name)
            if lv is None:
                continue
            if lv.is_var:
                tmp = ctx.tmp("rls")
                ctx.emit(f"{tmp} =l loadl {lv.loc}")
                ctx.emit(f"call $arc_release(l {tmp})")
            else:
                ctx.emit(f"call $arc_release(l {lv.loc})")

    def _emit_scope_stack_drops(
        self,
        ctx: FnCtx,
        scope: list[tuple[str, str]],
    ) -> None:
        """Emit direct `__drop__` calls for every stack-allocated struct
        local in *scope*.  No refcount — these are bare structs whose
        storage was an alloca; we call the drop_fn on the slot address.
        Order matches managed_stack: innermost scope first, latest
        binding first within a scope.
        """
        for slot, drop_fn in scope:
            ctx.emit(f"call {drop_fn}(l {slot})")

    def _emit_assignment(self, stmt: Assignment, ctx: FnCtx) -> None:
        target = stmt.target
        # `obj[k] = v` → `obj.__setitem__(k, v)` for struct receivers.
        # Handled before the unconditional RHS evaluation so the value
        # expression is emitted exactly once, inside the synthetic call.
        # Slice writes via `obj[i] = v` are not supported in v1.
        if isinstance(target, Index):
            base_ty = _unwrap(self._ty(target.obj))
            if isinstance(base_ty, StructTy):
                callee = FieldAccess(
                    span=target.span, obj=target.obj, field="__setitem__"
                )
                k_arg = Argument(span=target.index.span, name=None, value=target.index)
                v_arg = Argument(span=stmt.value.span, name=None, value=stmt.value)
                call = Call(span=stmt.span, callee=callee, args=[k_arg, v_arg])
                self._types.record(call, UNIT)
                self._emit_call(call, ctx)
                return
        val = self._emit_expr(stmt.value, ctx)
        if (
            isinstance(target, Name)
            and target.name not in ctx.locals
            and target.name in self._module_vars
        ):
            ty = self._module_vars[target.name]
            ctx.emit(f"{_store(ty)} {val}, ${self._q(target.name)}")
            return
        if isinstance(target, Name) and target.name in ctx.locals:
            lv = ctx.locals[target.name]
            if lv.is_var:
                ctx.emit(f"{_store(lv.ty)} {val}, {lv.loc}")
        elif isinstance(target, FieldAccess):
            obj = self._emit_expr(target.obj, ctx)
            base_ty = _unwrap(self._ty(target.obj))
            if isinstance(base_ty, StructTy):
                lay = self._layouts.get(self._q(_struct_name(base_ty)))
                if lay:
                    fl = next((f for f in lay.fields if f.name == target.field), None)
                    if fl:
                        dest = obj if fl.offset == 0 else self._gep(obj, fl.offset, ctx)
                        # Managed field reassignment: inc new (if copy)
                        # then release old, then store.  Weak fields use
                        # weak_inc / weak_release; strong managed fields
                        # use arc_inc / arc_release.
                        if _is_managed(fl.ty):
                            if self._is_borrowed_copy(stmt.value):
                                ctx.emit(f"call {_field_inc_call(fl.ty)}(l {val})")
                            old = ctx.tmp("old")
                            ctx.emit(f"{old} =l loadl {dest}")
                            ctx.emit(f"call {_field_release_call(fl.ty)}(l {old})")
                        ctx.emit(f"{_store(fl.ty)} {val}, {dest}")

    # ── Loops ─────────────────────────────────────────────────────────────────

    def _emit_loop(self, body: Block, ctx: FnCtx) -> None:
        loop = ctx.lbl("loop")
        after = ctx.lbl("after")
        # body_depth = index of the body scope after _emit_block pushes it.
        body_depth = len(ctx.managed_stack)
        ctx.emit(f"jmp {loop}")
        ctx.label(loop)
        ctx.loop_stack.append((loop, after, body_depth))
        self._emit_block(body, ctx)
        ctx.loop_stack.pop()
        ctx.emit(f"jmp {loop}")
        ctx.label(after)

    def _emit_for(self, stmt: For, ctx: FnCtx) -> None:
        loop = ctx.lbl("for")
        body = ctx.lbl("forbody")
        after = ctx.lbl("forafter")

        # `for i in start..end:` — numeric counter loop, no slice, no
        # heap iterator.  Lowers to `i = start; while i < end: body;
        # i = i + 1`.  Skipped if `end` is absent (`for i in 0..:` is
        # an infinite range — not supported here).
        if isinstance(stmt.iterable, Range) and stmt.iterable.end is not None:
            start_expr = stmt.iterable.start
            end_expr = stmt.iterable.end
            # Pick the loop's working base from the end's type (start
            # is widened to match).  Defaults to `l` (isize/usize).
            elem_ty: Ty = self._ty(end_expr) if start_expr else self._ty(end_expr)
            b = _base(elem_ty)
            if b not in ("w", "l"):
                b = "l"

            start_val = self._emit_expr(start_expr, ctx) if start_expr else "0"
            end_val = self._emit_expr(end_expr, ctx)
            i_slot = ctx.tmp("i")
            slot_align = 4 if b == "w" else 8
            slot_size = 4 if b == "w" else 8
            ctx.emit(f"{i_slot} =l alloc{slot_align} {slot_size}")
            ctx.emit(f"{_store_for_base(b)} {start_val}, {i_slot}")

            ctx.emit(f"jmp {loop}")
            ctx.label(loop)
            iv = ctx.tmp("iv")
            ctx.emit(f"{iv} ={b} {_load_for_base(b)} {i_slot}")
            cond = ctx.tmp("c")
            ctx.emit(f"{cond} =w csge{b} {iv}, {end_val}")
            ctx.emit(f"jnz {cond}, {after}, {body}")

            ctx.label(body)
            if stmt.binding != "_":
                ctx.locals[stmt.binding] = Local(iv, elem_ty, False)
                ctx.tmp_base[iv] = b

            body_depth = len(ctx.managed_stack)
            ctx.loop_stack.append((loop, after, body_depth))
            self._emit_block(stmt.body, ctx)
            ctx.loop_stack.pop()

            iv2 = ctx.tmp("iv")
            ctx.emit(f"{iv2} ={b} add {iv}, 1")
            ctx.emit(f"{_store_for_base(b)} {iv2}, {i_slot}")
            ctx.emit(f"jmp {loop}")
            ctx.label(after)
            return

        iterable = self._emit_expr(stmt.iterable, ctx)
        iter_ty = _readable(_unwrap(self._ty(stmt.iterable)))

        if isinstance(iter_ty, SliceTy):
            elem_ty = iter_ty.element
            # The slice value is a pointer to a {data_ptr, len} fat
            # pointer.  Load both once.
            data_ptr = ctx.tmp("dp")
            ctx.emit(f"{data_ptr} =l loadl {iterable}")
            len_ptr = ctx.tmp("lp")
            length = ctx.tmp("len")
            ctx.emit(f"{len_ptr} =l add {iterable}, 8")
            ctx.emit(f"{length} =l loadl {len_ptr}")

            i_slot = ctx.tmp("i")
            ctx.emit(f"{i_slot} =l alloc8 8")
            ctx.emit(f"storel 0, {i_slot}")

            ctx.emit(f"jmp {loop}")
            ctx.label(loop)
            iv = ctx.tmp("iv")
            cond = ctx.tmp("c")
            ctx.emit(f"{iv} =l loadl {i_slot}")
            ctx.emit(f"{cond} =w csgel {iv}, {length}")
            ctx.emit(f"jnz {cond}, {after}, {body}")

            ctx.label(body)
            if stmt.binding != "_":
                es = _size(elem_ty)
                off = ctx.tmp("off")
                addr = ctx.tmp("addr")
                ctx.emit(f"{off} =l mul {iv}, {es}")
                ctx.emit(f"{addr} =l add {data_ptr}, {off}")
                # Slice elements are 16-byte fat pointers — the slice
                # value *is* the address into the backing buffer, so
                # skip the load and use `addr` directly.  Same trick
                # field reads on slice-typed members already use.
                if isinstance(elem_ty, SliceTy):
                    ctx.locals[stmt.binding] = Local(addr, elem_ty, False)
                else:
                    elem = ctx.tmp("el")
                    ctx.emit(f"{elem} ={_base(elem_ty)} {_load(elem_ty)} {addr}")
                    ctx.locals[stmt.binding] = Local(elem, elem_ty, False)
                    ctx.tmp_base[elem] = _base(elem_ty)

            body_depth = len(ctx.managed_stack)
            ctx.loop_stack.append((loop, after, body_depth))
            self._emit_block(stmt.body, ctx)
            ctx.loop_stack.pop()

            iv2 = ctx.tmp("iv")
            ctx.emit(f"{iv2} =l add {iv}, 1")
            ctx.emit(f"storel {iv2}, {i_slot}")
            ctx.emit(f"jmp {loop}")
            ctx.label(after)
        else:
            # Non-slice iterator: emit single-pass placeholder (v2: __iter__/__next__)
            body_depth = len(ctx.managed_stack)
            ctx.loop_stack.append((loop, after, body_depth))
            ctx.emit(f"jmp {loop}")
            ctx.label(loop)
            self._emit_block(stmt.body, ctx)
            ctx.loop_stack.pop()
            ctx.emit(f"jmp {after}")
            ctx.label(after)

    # ── Expressions ───────────────────────────────────────────────────────────

    def _emit_expr(self, expr: Any, ctx: FnCtx) -> str:
        if isinstance(expr, IntLiteral):
            return str(expr.value)

        if isinstance(expr, FloatLiteral):
            # QBE float literals use a typed prefix: d_<value> for double,
            # s_<value> for single.  copy lets the result land in a typed
            # SSA temp.
            ty = self._ty(expr)
            b = _base(ty)
            prefix = "s" if b == "s" else "d"
            tmp = ctx.tmp("f")
            ctx.emit(f"{tmp} ={b} copy {prefix}_{expr.value}")
            ctx.tmp_base[tmp] = b
            return tmp

        if isinstance(expr, BoolLiteral):
            return "1" if expr.value else "0"

        if isinstance(expr, ByteLiteral):
            return str(expr.value)

        if isinstance(expr, StringLiteral):
            return self._emit_string_lit(expr, ctx)

        if isinstance(expr, ArrayLiteral):
            return self._emit_array_lit(expr, ctx)

        if isinstance(expr, Name):
            return self._emit_name(expr, ctx)

        if isinstance(expr, Discard):
            return "0"

        if isinstance(expr, BinaryOp):
            return self._emit_binary(expr, ctx)

        if isinstance(expr, UnaryOp):
            return self._emit_unary(expr, ctx)

        if isinstance(expr, TypeTest):
            return self._emit_type_test(expr, ctx)

        if isinstance(expr, Cast):
            return self._emit_cast(expr, ctx)

        if isinstance(expr, FieldAccess):
            return self._emit_field(expr, ctx)

        if isinstance(expr, Index):
            return self._emit_index(expr, ctx)

        if isinstance(expr, Call):
            return self._emit_call(expr, ctx)

        if isinstance(expr, GenericInstantiation):
            # The parser greedily tries to read `lhs[...]` as a generic
            # instantiation (`Foo[i32]`); if the contents successfully
            # parse as types it stays a `GenericInstantiation`.  But
            # `a[i]` where `a` is a slice-typed value should be an
            # *index* — recover that here.  The typechecker uses the
            # same heuristic to type the expression (see
            # `_infer_generic_instantiation`).  Anything else (the
            # actual generic-instantiation-used-as-value case) just
            # propagates the base.
            base_ty = _unwrap(self._ty(expr.base))
            if (
                isinstance(base_ty, SliceTy)
                and len(expr.type_args) == 1
                and isinstance(expr.type_args[0], NamedType)
                and expr.type_args[0].name in ctx.locals
            ):
                name = expr.type_args[0].name
                idx_expr: Any = Name(span=expr.span, name=name)
                self._types.record(idx_expr, PrimTy("usize"))
                fake = Index(span=expr.span, obj=expr.base, index=idx_expr)
                self._types.record(fake, base_ty.element)
                return self._emit_index(fake, ctx)
            return self._emit_expr(expr.base, ctx)

        if isinstance(expr, StructLiteral):
            return self._emit_struct_lit(expr, ctx)

        if isinstance(expr, If):
            return self._emit_if(expr, ctx)

        if isinstance(expr, Match):
            return self._emit_match(expr, ctx)

        return "0"

    # ── Name lookup ───────────────────────────────────────────────────────────

    def _emit_name(self, expr: Name, ctx: FnCtx) -> str:
        lv = ctx.locals.get(expr.name)
        if lv is None:
            # Not a local — could be a top-level fn, module var, or
            # struct/import.  Module vars load from their backing
            # data slot; function names yield a fn pointer (module-
            # prefixed); other names fall through to a bare symbol.
            if expr.name in self._module_vars:
                ty = self._module_vars[expr.name]
                tmp = ctx.tmp(expr.name)
                ctx.emit(
                    f"{tmp} ={_base(ty)} {_load(ty)} "
                    f"${self._q(expr.name)}"
                )
                ctx.tmp_base[tmp] = _base(ty)
                return tmp
            sym = self._res.get(expr) if self._res else None
            if sym is not None and sym.kind == SymbolKind.FUNCTION:
                return f"${self._q(expr.name)}"
            return f"${expr.name}"

        # Read the raw value out of the binding.  A `var[Struct]` slot
        # holds the struct's bytes inline — the slot's address *is* the
        # struct value (a `:Foo`-typed pointer), no load.  Other var
        # slots hold the primitive/pointer directly and need a load.
        if lv.is_var and lv.is_bare_struct:
            raw = lv.loc
        elif lv.is_var:
            raw = ctx.tmp(expr.name)
            ctx.emit(f"{raw} ={_base(lv.ty)} {_load(lv.ty)} {lv.loc}")
        else:
            raw = lv.loc

        # If the binding held a tagged-union box but the type checker
        # has narrowed this reference to a single variant (via `?=`),
        # extract the payload at offset 8 of the box rather than handing
        # back the box pointer.  The payload is l-wide; for narrow
        # primitive variants the higher bits were sign-extended on
        # construction and downstream uses tolerate the wider value.
        stored_ty = _unwrap(lv.ty)
        recorded_ty = self._ty(expr)
        if (
            isinstance(stored_ty, UnionTy)
            and not isinstance(recorded_ty, (UnionTy, UnknownTy))
        ):
            paddr = ctx.tmp("paddr")
            ctx.emit(f"{paddr} =l add {raw}, 8")
            payload = ctx.tmp("pl")
            ctx.emit(f"{payload} =l loadl {paddr}")
            return payload

        return raw

    # ── Binary / unary ops ────────────────────────────────────────────────────

    def _emit_binary(self, expr: BinaryOp, ctx: FnCtx) -> str:
        left = self._emit_expr(expr.left, ctx)
        right = self._emit_expr(expr.right, ctx)
        lt = self._ty(expr.left)
        rt_arg = self._ty(expr.right)
        lb = _base(lt)
        rb_arg = _base(rt_arg)
        tmp = ctx.tmp("v")

        op = expr.op
        # Promote w → l when the two sides disagree.  QBE rejects
        # mixed widths in arithmetic / compare ops; the language-level
        # rule is "widening goes through silently in v1" so we patch
        # the narrower side up to the wider one here.
        def widen(val: str, from_b: str, to_b: str) -> str:
            if from_b == to_b or to_b not in ("l", "w"):
                return val
            if from_b == "w" and to_b == "l":
                t = ctx.tmp("ext")
                ctx.emit(f"{t} =l extsw {val}")
                return t
            return val

        cmp_b = "l" if "l" in (lb, rb_arg) else lb
        if cmp_b == "l":
            left = widen(left, lb, "l")
            right = widen(right, rb_arg, "l")

        cmp: dict[str, str] = {
            "==": f"ceq{cmp_b}", "!=": f"cne{cmp_b}",
            "<":  f"cslt{cmp_b}" if cmp_b in ("w", "l") else f"clt{cmp_b}",
            "<=": f"csle{cmp_b}" if cmp_b in ("w", "l") else f"cle{cmp_b}",
            ">":  f"csgt{cmp_b}" if cmp_b in ("w", "l") else f"cgt{cmp_b}",
            ">=": f"csge{cmp_b}" if cmp_b in ("w", "l") else f"cge{cmp_b}",
        }
        if op in cmp:
            ctx.emit(f"{tmp} =w {cmp[op]} {left}, {right}")
            return tmp

        if op in ("and", "or"):
            qop = "and" if op == "and" else "or"
            ctx.emit(f"{tmp} =w {qop} {left}, {right}")
            return tmp

        # Arithmetic and bitwise ops.  `|` doubles as the type-union
        # operator at parse time (handled inside `_parse_type`); here
        # it's bitwise OR.  Division and modulo dispatch on the LHS's
        # signedness — signed → `div` / `rem`, unsigned → `udiv` /
        # `urem`.  Right shift similarly: signed → `sar` (arithmetic),
        # unsigned → `shr` (logical).
        arith = {"+": "add", "-": "sub", "*": "mul", "/": "div",
                 "%": "rem", "|": "or", "&": "and", "^": "xor",
                 "<<": "shl"}
        left_base_ty = _readable(_unwrap(lt))
        signed_lhs = (
            isinstance(left_base_ty, PrimTy)
            and left_base_ty.name in ("i8", "i16", "i32", "i64", "isize")
        )
        if not signed_lhs:
            arith["/"] = "udiv"
            arith["%"] = "urem"
        if op == ">>":
            arith[">>"] = "sar" if signed_lhs else "shr"
        rt = self._ty(expr)
        rb = _base(rt) if not isinstance(rt, (UnknownTy,)) else cmp_b
        # Match operand widths to the result width.
        if rb == "l":
            left = widen(left, lb, "l")
            right = widen(right, rb_arg, "l")
        ctx.emit(f"{tmp} ={rb} {arith.get(op, 'add')} {left}, {right}")
        return tmp

    def _emit_unary(self, expr: UnaryOp, ctx: FnCtx) -> str:
        v = self._emit_expr(expr.operand, ctx)
        ty = self._ty(expr.operand)
        b = _base(ty)
        tmp = ctx.tmp("u")
        if expr.op == "-":
            ctx.emit(f"{tmp} ={b} neg {v}")
        elif expr.op == "not":
            ctx.emit(f"{tmp} =w ceqw {v}, 0")
        else:
            return v
        return tmp

    # ── Field access ──────────────────────────────────────────────────────────

    def _gep(self, ptr: str, offset: int, ctx: FnCtx) -> str:
        tmp = ctx.tmp("gep")
        ctx.emit(f"{tmp} =l add {ptr}, {offset}")
        return tmp

    def _enum_alias_for_obj(self, obj: Any) -> Optional[TypeAlias]:
        """If `obj` resolves to an enum-synthesized TypeAlias (either
        `Enum` or `mod.Enum`), return it; otherwise None."""
        if not self._res:
            return None
        if isinstance(obj, Name):
            sym = self._res.get(obj)
            if sym is not None and isinstance(sym.node, TypeAlias) and sym.node.enum_variants:
                return sym.node
        elif isinstance(obj, FieldAccess) and isinstance(obj.obj, Name):
            mod_sym = self._res.get(obj.obj)
            if mod_sym is None or mod_sym.kind != SymbolKind.IMPORT:
                return None
            other = self._module_imports.get(mod_sym.name)
            if other is None:
                return None
            alias = other._type_aliases.get(obj.field)
            if alias is not None and alias.enum_variants:
                return alias
        return None

    def _emit_field(self, expr: FieldAccess, ctx: FnCtx) -> str:
        # Enum dot-access: `Enum.Variant` (or `mod.Enum.Variant`)
        # constructs an empty variant struct.  Detection mirrors the
        # typechecker's: walk the obj to find a TypeAlias resolver
        # entry with non-empty `enum_variants`.
        enum_alias = self._enum_alias_for_obj(expr.obj)
        if enum_alias is not None and expr.field in enum_alias.enum_variants:
            mangled = enum_alias.enum_variants[expr.field]
            synth = StructLiteral(
                span=expr.span,
                type=NamedType(span=expr.span, name=mangled),
                fields=[],
            )
            return self._emit_struct_lit(synth, ctx)

        obj = self._emit_expr(expr.obj, ctx)
        base_ty = _unwrap(self._ty(expr.obj))

        if isinstance(base_ty, StructTy):
            sname = _struct_name(base_ty)
            lay = self._layouts.get(self._q(sname))
            if lay is None:
                # Foreign struct: search imports for the layout.
                for other_cg in self._all_imports():
                    cand = other_cg._layouts.get(other_cg._q(sname))
                    if cand is not None:
                        lay = cand
                        break
            if lay:
                fl = next((f for f in lay.fields if f.name == expr.field), None)
                if fl:
                    src = obj if fl.offset == 0 else self._gep(obj, fl.offset, ctx)
                    if _is_weak_field(fl.ty):
                        # Weak read produces a tagged `T | Null` box.
                        return self._emit_weak_read(fl, src, ctx)
                    if isinstance(fl.ty, StructTy):
                        # Inline composition: the field IS the inner
                        # struct; the field address is already its
                        # value (a `:Inner`-typed pointer).  No load.
                        return src
                    tmp = ctx.tmp("fv")
                    ctx.emit(f"{tmp} ={_base(fl.ty)} {_load(fl.ty)} {src}")
                    return tmp

        if isinstance(base_ty, SliceTy):
            if expr.field in ("len", "length"):
                # Length lives at offset 8 of the fat-pointer slot.
                lp = ctx.tmp("lp")
                lv = ctx.tmp("len")
                ctx.emit(f"{lp} =l add {obj}, 8")
                ctx.emit(f"{lv} =l loadl {lp}")
                return lv
            if expr.field == "ptr":
                # Data pointer lives at offset 0 — handy for FFI to
                # functions taking a raw `*const u8` etc.
                ptr = ctx.tmp("dptr")
                ctx.emit(f"{ptr} =l loadl {obj}")
                return ptr

        # `mod.fn` as a value — taking a fn pointer from a foreign
        # module.  Emit the qualified fn symbol address.  The
        # codegen tracks fn declarations via `_fn_param_tys`
        # (populated for every fn during file collection).
        if isinstance(base_ty, ModuleTy):
            other_cg = self._module_imports.get(base_ty.binding)
            if other_cg is not None:
                qname = other_cg._q(expr.field)
                if qname in other_cg._fn_param_tys:
                    return f"${qname}"
                if expr.field in other_cg._generic_fns:
                    # Generic fn taken as value: caller must instantiate
                    # via `mod.fn[T1, T2]` — bare `mod.fn` has no
                    # concrete address.
                    return "0"

        return "0"

    def _emit_weak_read(
        self, fl: FieldLayout, src: str, ctx: FnCtx
    ) -> str:
        """Read a `weak[T]` field and produce a `T | Null` boxed
        union.  Calls weak_upgrade — if alive, the box wraps the
        upgraded strong ref (tag for T); if dropped, the box wraps a
        Null marker (tag for Null).
        """
        # Compute T and the canonical sorted variants of `T | Null`
        # so the consumer's `?=` finds the same tag numbering.
        assert isinstance(fl.ty, WrapperTy)
        inner_ty = fl.ty.inner
        null_err = PrimTy("Null")
        variants = sorted([inner_ty, null_err], key=_fmt)
        t_tag = variants.index(inner_ty)
        ne_tag = variants.index(null_err)
        drop_fn = self._get_or_emit_union_drop(variants)

        # %wp = raw weak pointer stored in the field
        wp = ctx.tmp("wp")
        ctx.emit(f"{wp} =l loadl {src}")

        # Try to upgrade; either a fresh strong ref or NULL.
        up = ctx.tmp("up")
        ctx.emit(f"{up} =l call $weak_upgrade(l {wp})")

        # Result slot: we'll branch and have both arms write into it.
        res_slot = ctx.tmp("wres")
        ctx.emit(f"{res_slot} =l alloc8 8")
        ctx.emit(f"storel 0, {res_slot}")

        is_null = ctx.tmp("isnull")
        ctx.emit(f"{is_null} =w ceql {up}, 0")
        null_lbl = ctx.lbl("wnull")
        val_lbl = ctx.lbl("wval")
        done_lbl = ctx.lbl("wdone")
        ctx.emit(f"jnz {is_null}, {null_lbl}, {val_lbl}")

        # Null branch: build a Null box (payload = 0).
        ctx.label(null_lbl)
        nbox = ctx.tmp("nbox")
        ctx.emit(f"{nbox} =l call $arc_alloc(l 16, l {drop_fn})")
        ctx.emit(f"storel {ne_tag}, {nbox}")
        np = ctx.tmp("np")
        ctx.emit(f"{np} =l add {nbox}, 8")
        ctx.emit(f"storel 0, {np}")
        ctx.emit(f"storel {nbox}, {res_slot}")
        ctx.emit(f"jmp {done_lbl}")

        # Value branch: build a T box with the upgraded strong ref.
        ctx.label(val_lbl)
        vbox = ctx.tmp("vbox")
        ctx.emit(f"{vbox} =l call $arc_alloc(l 16, l {drop_fn})")
        ctx.emit(f"storel {t_tag}, {vbox}")
        vp = ctx.tmp("vp")
        ctx.emit(f"{vp} =l add {vbox}, 8")
        ctx.emit(f"storel {up}, {vp}")
        ctx.emit(f"storel {vbox}, {res_slot}")
        ctx.emit(f"jmp {done_lbl}")

        ctx.label(done_lbl)
        result = ctx.tmp("wread")
        ctx.emit(f"{result} =l loadl {res_slot}")
        return result

    # ── Index ─────────────────────────────────────────────────────────────────

    def _emit_index(self, expr: Index, ctx: FnCtx) -> str:
        # Strip storage wrappers (var/const/rc/arc) but keep ptr/slice
        # — those decide the indexing kind.
        ty = self._ty(expr.obj)
        while isinstance(ty, WrapperTy) and ty.wrapper in ("var", "const", "rc", "arc"):
            ty = ty.inner
        base_ty = ty

        # Range index: `obj[start..end]` constructs a slice fat-pointer
        # over the underlying memory.  Supported for `ptr[T]` and
        # `[]T` bases.  Element type comes from the base.
        if isinstance(expr.index, Range):
            obj = self._emit_expr(expr.obj, ctx)
            r = expr.index
            start_v: str = "0"
            if r.start is not None:
                start_v = self._emit_expr(r.start, ctx)
            assert r.end is not None, "open-ended ranges as slice not supported"
            end_v = self._emit_expr(r.end, ctx)
            # Element type + size.
            if isinstance(base_ty, SliceTy):
                et = base_ty.element
                data_ptr = ctx.tmp("dp")
                ctx.emit(f"{data_ptr} =l loadl {obj}")
            elif isinstance(base_ty, WrapperTy) and base_ty.wrapper == "ptr":
                et = base_ty.inner
                data_ptr = obj
            else:
                return "0"
            es = _size(et)
            # Adjusted data pointer = data_ptr + start * size.
            soff = ctx.tmp("soff")
            new_dp = ctx.tmp("ndp")
            ctx.emit(f"{soff} =l mul {start_v}, {es}")
            ctx.emit(f"{new_dp} =l add {data_ptr}, {soff}")
            # New length = end - start.
            nlen = ctx.tmp("nlen")
            ctx.emit(f"{nlen} =l sub {end_v}, {start_v}")
            # Emit the fat pointer.
            slot = ctx.tmp("sl")
            ctx.emit(f"{slot} =l alloc8 16")
            ctx.emit(f"storel {new_dp}, {slot}")
            lp = ctx.tmp("lp")
            ctx.emit(f"{lp} =l add {slot}, 8")
            ctx.emit(f"storel {nlen}, {lp}")
            return slot

        # User-defined indexing: rewrite `obj[k]` to `obj.__getitem__(k)`
        # for struct receivers.  The synthetic FieldAccess + Call mirror
        # what a hand-written call would produce; the typechecker has
        # already recorded the right return type on the original Index
        # node, so the synthetic Call inherits the same shape.  If the
        # struct doesn't define `__getitem__` the resulting symbol won't
        # exist at link time — the typechecker can't reject earlier
        # because it returns UNKNOWN for unresolved indexing.
        if isinstance(base_ty, StructTy):
            callee = FieldAccess(span=expr.span, obj=expr.obj, field="__getitem__")
            arg = Argument(span=expr.index.span, name=None, value=expr.index)
            call = Call(span=expr.span, callee=callee, args=[arg])
            self._types.record(call, self._ty(expr))
            return self._emit_call(call, ctx)

        obj = self._emit_expr(expr.obj, ctx)
        idx = self._emit_expr(expr.index, ctx)
        if isinstance(base_ty, SliceTy):
            et = base_ty.element
            es = _size(et)
            # The slice value is a pointer to a {data_ptr, len} fat
            # pointer.  To read element i we need *((*data_ptr) + i * size).
            dp = ctx.tmp("dp")
            ctx.emit(f"{dp} =l loadl {obj}")
            off = ctx.tmp("off")
            addr = ctx.tmp("addr")
            tmp = ctx.tmp("el")
            ctx.emit(f"{off} =l mul {idx}, {es}")
            ctx.emit(f"{addr} =l add {dp}, {off}")
            ctx.emit(f"{tmp} ={_base(et)} {_load(et)} {addr}")
            ctx.tmp_base[tmp] = _base(et)
            return tmp
        return "0"

    # ── Call ──────────────────────────────────────────────────────────────────

    def _emit_call(self, expr: Call, ctx: FnCtx) -> str:
        # Comptime intrinsics — `sizeof[T]()`, `mem_load[T](p, off)`,
        # `mem_store[T](p, off, v)`.  Substituted in-place rather
        # than dispatched through a body.  T may reference the
        # active specialisation's type parameters (e.g. inside a
        # generic method body), so we go through `_ast_ty` which
        # honours the current substitution table.
        c0 = expr.callee
        if (
            isinstance(c0, GenericInstantiation)
            and isinstance(c0.base, Name)
            and len(c0.type_args) == 1
        ):
            iname = c0.base.name
            if iname == "sizeof":
                t = self._ast_ty(c0.type_args[0])
                return str(_size(t, self._layouts))
            if iname == "mem_load":
                return self._emit_mem_load(expr, c0, ctx)
            if iname == "mem_store":
                self._emit_mem_store(expr, c0, ctx)
                return "0"
            if iname == "drop_at":
                self._emit_drop_at(expr, c0, ctx)
                return "0"
            if iname == "va_arg":
                t = self._ast_ty(c0.type_args[0])
                ap = self._emit_expr(expr.args[0].value, ctx)
                tmp = ctx.tmp("va")
                ctx.emit(f"{tmp} ={_base(t)} vaarg {ap}")
                ctx.tmp_base[tmp] = _base(t)
                return tmp
            if iname == "hash":
                return self._emit_hash_intrinsic(expr, c0, ctx)
            if iname == "eq":
                return self._emit_eq_intrinsic(expr, c0, ctx)
        # Bare-name `hash` / `eq` — T inferred from the arg type.
        # Same code path as the bracketed `hash[T]` / `eq[T]`, just
        # with a synthesized type-arg node.
        if isinstance(c0, Name) and c0.name in ("hash", "eq"):
            arity_ok = (c0.name == "hash" and len(expr.args) == 1) or (
                c0.name == "eq" and len(expr.args) == 2
            )
            if arity_ok:
                arg_ty = _readable(_unwrap(self._ty(expr.args[0].value)))
                inferred = _ty_to_ast_for_intrinsic(arg_ty, c0.span)
                if inferred is not None:
                    synth = GenericInstantiation(
                        span=c0.span,
                        base=c0,
                        type_args=[inferred],
                    )
                    if c0.name == "hash":
                        return self._emit_hash_intrinsic(expr, synth, ctx)
                    return self._emit_eq_intrinsic(expr, synth, ctx)

        # Comptime file-embed intrinsic.  `embed("path")` reads the
        # file relative to the current module's source directory at
        # *codegen* time, interns the bytes into the data section,
        # and emits a `[]u8` fat-pointer slot.  Takes a value arg
        # (not a type arg), so it lives in this side branch rather
        # than the `GenericInstantiation` short-circuit above.
        if (
            isinstance(c0, Name)
            and c0.name == "embed"
            and len(expr.args) == 1
            and isinstance(expr.args[0].value, StringLiteral)
        ):
            return self._emit_embed(expr.args[0].value.value, ctx)
        # Variadic-fn-body intrinsic.  `va_start()` allocates a
        # 32-byte va_list on the caller's stack (matches the SysV
        # va_list layout — QBE figures out the spill).
        if (
            isinstance(c0, Name)
            and c0.name == "va_start"
            and len(expr.args) == 0
        ):
            slot = ctx.tmp("va")
            ctx.emit(f"{slot} =l alloc8 32")
            ctx.emit(f"vastart {slot}")
            return slot

        # Variadic intrinsic: <module>.printf lowers to a libc printf
        # call, extracting the format slice's data pointer (libc
        # expects a NUL-terminated char*, not our fat pointer).
        # Triggered on any module's `printf` field because Ouro v1
        # has no variadic-function declaration syntax, so std/io
        # can't declare printf as a normal Ouro fn — the codegen
        # intercepts the call shape directly.  This goes away once
        # variadics land.
        c = expr.callee
        if isinstance(c, FieldAccess):
            obj_ty_check = _unwrap(self._ty(c.obj))
            if isinstance(obj_ty_check, ModuleTy) and c.field == "printf":
                return self._emit_printf_call(expr, ctx)

        # Resolve callee name and optional self argument
        implicit_self: Optional[str] = None
        callee: str

        # Indirect call through a struct field whose type is a fn pointer:
        # `(c.f)()` — the typechecker has recorded a FnTy for the
        # FieldAccess.  Emit the field load and let the generic call
        # path below dispatch via the loaded value (same shape as a
        # local fn-pointer variable).
        fnptr_callee: Optional[str] = None
        if isinstance(c, FieldAccess):
            field_ty = self._ty(c)
            if isinstance(field_ty, FnTy):
                fnptr_callee = self._emit_expr(c, ctx)

        if fnptr_callee is None and isinstance(c, FieldAccess):
            obj_val = self._emit_expr(c.obj, ctx)
            obj_ty = _unwrap(self._ty(c.obj))
            if isinstance(obj_ty, StructTy):
                # Resolve the struct's defining module: an explicit
                # `mod.StructName` qualifier wins; otherwise the struct
                # might still come from an import (a method called on
                # a value typed as a foreign struct), so walk the
                # imports looking for a matching layout.  Same-module
                # types fall through to `self`.
                sname = _struct_name(obj_ty)
                qualifier_cg: Optional["Codegen"] = None
                if (
                    isinstance(c.obj, FieldAccess)
                    and isinstance(c.obj.obj, Name)
                    and c.obj.obj.name in self._module_imports
                ):
                    qualifier_cg = self._module_imports[c.obj.obj.name]
                elif self._q(sname) not in self._layouts:
                    for other_cg in self._all_imports():
                        if other_cg._q(sname) in other_cg._layouts:
                            qualifier_cg = other_cg
                            break
                qsname = (
                    qualifier_cg._q(sname) if qualifier_cg else self._q(sname)
                )
                # If `c.field` is a data field (not a method), this is
                # a call through a stored function pointer — load the
                # field and call indirectly via a temp.
                lay = (
                    qualifier_cg._layouts.get(qsname)
                    if qualifier_cg
                    else self._layouts.get(qsname)
                )
                methods_index = (
                    qualifier_cg._static_methods.get(qsname, set())
                    if qualifier_cg
                    else self._static_methods.get(qsname, set())
                )
                # Methods live as $<S>__<name> symbols regardless of
                # whether they take self; data fields don't.  Detect
                # method-ness by symbol presence.  Field-call falls
                # through the StructTy branch and goes to indirect
                # dispatch via the generic field-access loader below.
                method_symbol = f"{qsname}__{c.field}"
                is_method = (
                    method_symbol in self._fn_param_tys
                    or (qualifier_cg is not None
                        and method_symbol in qualifier_cg._fn_param_tys)
                    or c.field in methods_index
                )
                callee = f"${method_symbol}"
                if c.field not in methods_index:
                    implicit_self = obj_val
            elif isinstance(obj_ty, ModuleTy):
                # Real imported module → route to that module's
                # symbol prefix.  Legacy stubs (binding not in
                # `_module_imports`) keep the bare-name behavior so
                # `$println` etc. link to the C runtime.  Generic
                # functions in the imported module specialize on
                # demand: we register the spec in *that* module's
                # `_fn_specs` so its drain loop emits the body.
                other_cg = self._module_imports.get(obj_ty.binding)
                if other_cg is not None:
                    other_prefix = other_cg._module_prefix
                    if c.field in other_cg._extern_decls:
                        # Extern decls always link bare — they refer
                        # to externally-linked C symbols regardless of
                        # which Ouro module declared them.
                        callee = f"${c.field}"
                    elif c.field in other_cg._generic_fns:
                        spec_name = other_cg._register_cross_module_spec(
                            c.field, expr, ctx, self
                        )
                        generic_spec = spec_name
                        callee = f"${other_cg._q(spec_name)}"
                    else:
                        callee = f"${other_prefix}__{c.field}" if other_prefix else f"${c.field}"
                else:
                    callee = f"${c.field}"
            else:
                callee = f"${c.field}"
        # If the resolved callee is a specialized generic fn, this holds
        # the spec name so we can recover the substitution and compute
        # the wrapper-faithful return type below.
        generic_spec: Optional[str] = None
        if fnptr_callee is not None:
            callee = fnptr_callee
        elif isinstance(c, FieldAccess):
            pass  # handled above
        elif isinstance(c, Name):
            if c.name in ctx.locals:
                # Indirect call through a local function-pointer
                # binding — `_emit_name` loads the pointer for var
                # slots and returns the bare temp for const ones.
                callee = self._emit_expr(c, ctx)
            elif c.name in self._extern_decls:
                # Extern decls always link bare — no module prefix.
                callee = f"${c.name}"
            elif c.name in self._generic_fns:
                generic_spec = self._route_generic_fn_call(c.name, expr, None, ctx)
                callee = f"${self._q(generic_spec)}"
            else:
                callee = f"${self._q(c.name)}"
        elif isinstance(c, GenericInstantiation):
            # e.g. LinkedList[i32].new() — the base is a FieldAccess after instantiation
            inner = c.base
            if isinstance(inner, FieldAccess):
                obj_ty = _unwrap(self._ty(inner.obj))
                if isinstance(obj_ty, StructTy):
                    callee = f"${self._q(_struct_name(obj_ty))}__{inner.field}"
                elif isinstance(obj_ty, ModuleTy):
                    # Cross-module generic-fn instantiation:
                    # `mod.fn[T1, T2](...)`.  Route through the
                    # callee module's specialization machinery.
                    other_cg = self._module_imports.get(obj_ty.binding)
                    if other_cg is not None and inner.field in other_cg._generic_fns:
                        explicit = tuple(self._ast_ty(ta) for ta in c.type_args)
                        spec_name = other_cg._register_cross_module_spec(
                            inner.field, expr, ctx, self, explicit_args=explicit
                        )
                        generic_spec = spec_name
                        callee = f"${other_cg._q(spec_name)}"
                    else:
                        callee = "$unknown"
                else:
                    callee = "$unknown"
            elif isinstance(inner, Name) and inner.name in self._generic_fns:
                # Explicit `id[i32](42)` — take subst from the type args.
                explicit = tuple(self._ast_ty(ta) for ta in c.type_args)
                generic_spec = self._route_generic_fn_call(
                    inner.name, expr, explicit, ctx
                )
                callee = f"${self._q(generic_spec)}"
            elif isinstance(inner, Name):
                callee = f"${self._q(inner.name)}"
            else:
                callee = "$unknown"
        else:
            callee = self._emit_expr(c, ctx)

        args: list[str] = []
        if implicit_self is not None:
            args.append(f"l {implicit_self}")

        # Pull the callee's declared param types so the arg ABI follows
        # the *callee's* expectation: bare `StructTy` → `:Foo` aggregate
        # by-value, every other shape (`ptr[T]`, `rc[T]`, `arc[T]`,
        # primitives, slices) → its `_base` (typically `l`).  Falls
        # back to arg-driven detection when the callee isn't in the
        # signature index (cross-module, unresolved, etc.).
        bare_callee = callee.lstrip("$")
        callee_param_tys = self._fn_param_tys.get(bare_callee)
        # Cross-module method specs live on the defining module's
        # codegen — walk imports if the local index doesn't have it.
        if callee_param_tys is None:
            for other_cg in self._all_imports():
                callee_param_tys = other_cg._fn_param_tys.get(bare_callee)
                if callee_param_tys is not None:
                    break
        # Variadic externs: any args past the declared fixed-arity
        # boundary go through QBE's variadic-call ABI (`...` separator
        # before the extra args; f32 → f64 promotion per C SysV).
        callee_is_variadic = bare_callee in self._extern_variadic
        if not callee_is_variadic:
            for other_cg in self._all_imports():
                if bare_callee in other_cg._extern_variadic:
                    callee_is_variadic = True
                    break
        n_fixed = len(callee_param_tys) if callee_param_tys is not None else 0
        # Where to splice `...` in the args list.  Includes the
        # implicit_self slot (already added to `args`) so the index
        # is into the final list, not into `expr.args`.
        variadic_split = (
            len(args) + n_fixed if callee_is_variadic else None
        )

        for i, arg in enumerate(expr.args):
            at = self._ty(arg.value)
            is_var_arg = callee_is_variadic and i >= n_fixed
            param_ty = (
                callee_param_tys[i]
                if (
                    callee_param_tys is not None
                    and i < len(callee_param_tys)
                    and not is_var_arg
                )
                else None
            )
            if param_ty is not None:
                pass_as_aggregate = isinstance(param_ty, StructTy)
            else:
                pass_as_aggregate = self._is_bare_struct_value(arg.value, ctx)

            if pass_as_aggregate:
                # Pass-by-value: bare struct args use QBE's `:Foo` ABI.
                # Wrapped values (`rc[T]`, `arc[T]`) keep the `l`-pointer
                # path — they're heap refs, not stack values.
                bare = (
                    param_ty
                    if isinstance(param_ty, StructTy)
                    else at
                )
                assert isinstance(bare, StructTy)
                sname = _struct_name(bare)
                qn = self._q(sname)
                lay = self._layouts.get(qn)
                if isinstance(arg.value, StructLiteral) and lay is not None:
                    # Direct literal — allocate a caller-side stack slot
                    # so we don't leak a heap allocation through the
                    # aggregate-arg path.  The slot's __drop__ runs at
                    # the caller's scope exit, mirroring a named binding.
                    size = lay.total
                    align = min(size, 8) if size > 0 else 1
                    slot = ctx.tmp(f"arg_{sname}")
                    ctx.emit(f"{slot} =l alloc{align} {max(size, 1)}")
                    self._emit_struct_lit(arg.value, ctx, into=slot)
                    drop_fn = self._struct_drop_fn(lay)
                    if drop_fn != "0":
                        ctx.stack_drop_stack[-1].append((slot, drop_fn))
                    args.append(f":{qn} {slot}")
                else:
                    av = self._emit_expr(arg.value, ctx)
                    args.append(f":{qn} {av}")
            else:
                av = self._emit_expr(arg.value, ctx)
                # Auto-box a single-variant arg into a tagged-union param.
                # Mirrors the binding-site variant-box path; without
                # this, `dump(42i32)` against `dump(v: i32 | Null)` would
                # pass the raw 32-bit value into an `l`-shaped slot.
                if param_ty is not None and not is_var_arg:
                    box_variants = self._needs_variant_box(param_ty, at)
                    if box_variants is not None:
                        if _is_managed(at) and self._is_borrowed_copy(arg.value):
                            ctx.emit(f"call $arc_inc(l {av})")
                        av = self._box_into_union(av, at, box_variants, ctx)
                        at = UnionTy(frozenset(box_variants))
                # Use the callee's base type when available — it tells us
                # `l` for a `ptr[T]` param even if the arg's read type is
                # bare struct (e.g. stack-binding passed where the param
                # asks for a pointer).
                base = _base(param_ty) if param_ty is not None else _base(at)
                # Widen a narrower w-typed temp to l when the callee
                # expects 64-bit (e.g. passing `j: i32` to a `usize`
                # param).  Same lenient widening the binding path uses;
                # without it QBE rejects the arg as type-mismatched.
                if (
                    param_ty is not None
                    and base == "l"
                    and _base(at) == "w"
                    and av.startswith("%")
                    and not is_var_arg
                ):
                    src_inner = _readable(_unwrap(at))
                    signed = (
                        isinstance(src_inner, PrimTy)
                        and src_inner.name in ("i8", "i16", "i32", "isize")
                    )
                    widened = ctx.tmp("ext")
                    op = "extsw" if signed else "extuw"
                    ctx.emit(f"{widened} =l {op} {av}")
                    av = widened
                if is_var_arg and base == "s":
                    # C variadic ABI: float promotes to double.
                    promo = ctx.tmp("p")
                    ctx.emit(f"{promo} =d exts {av}")
                    args.append(f"d {promo}")
                elif is_var_arg and base == "w":
                    # Variadic int promotion: widen `w`-typed values
                    # to `l` so all variadic callees can `vaarg[i64]`
                    # without worrying about per-arg widths.  Signed
                    # types sign-extend, unsigned zero-extend.
                    src_inner = _readable(_unwrap(at))
                    signed = (
                        isinstance(src_inner, PrimTy)
                        and src_inner.name in ("i8", "i16", "i32", "isize")
                    )
                    promo = ctx.tmp("vp")
                    op = "extsw" if signed else "extuw"
                    ctx.emit(f"{promo} =l {op} {av}")
                    args.append(f"l {promo}")
                else:
                    args.append(f"{base} {av}")

        # Splice in the QBE variadic-call separator between the fixed
        # and variadic arg groups.  Skipped when the callee isn't
        # variadic or no extra args were passed.
        if variadic_split is not None and len(args) > variadic_split:
            args.insert(variadic_split, "...")

        # For generic-fn calls, the typemap's recorded result type uses
        # the typechecker's own substitution which `_readable`-strips
        # wrappers off arg types — losing the rc/arc tag we need for
        # ABI selection.  Re-derive from the (decl, subst) the codegen
        # routed to, which preserves wrappers.
        if generic_spec is not None and generic_spec in self._fn_specs:
            decl, subst = self._fn_specs[generic_spec]
            if decl.return_type is None:
                ret_ty = UNIT
            else:
                rt = self._ast_ty_with_generics(decl.return_type, set(decl.generics))
                ret_ty = _subst(rt, subst)
        else:
            ret_ty = self._ty(expr)
        args_str = ", ".join(args)

        if isinstance(ret_ty, (UnitTy, UnknownTy)):
            ctx.emit(f"call {callee}({args_str})")
            return "0"
        # Bare-struct return → QBE aggregate type `:Foo`.  Qualify
        # the type name with the *defining* module's prefix (the
        # struct may come from an import; we use whichever codegen
        # owns the layout).
        if _is_stack_struct_return(ret_ty):
            sname = _struct_name(ret_ty)
            qname = self._q(sname)
            if qname not in self._layouts:
                for other_cg in self._all_imports():
                    if other_cg._q(sname) in other_cg._layouts:
                        qname = other_cg._q(sname)
                        break
            rb = f":{qname}"
        else:
            rb = _base(ret_ty)
        tmp = ctx.tmp("r")
        ctx.emit(f"{tmp} ={rb} call {callee}({args_str})")
        return tmp

    # ── Variadic libc printf intrinsic ────────────────────────────────────────

    def _emit_mem_load(
        self, expr: Call, callee: GenericInstantiation, ctx: FnCtx
    ) -> str:
        """`mem_load[T](p, off)` — load a `T` from `p + off`.

        Dispatch by T's runtime shape:
          - bare struct: return the address (the inline-composition
            convention — the field address *is* the struct value).
          - slice (`[]T`): blit the 16-byte fat pointer into a fresh
            stack slot and return its address (slice values are
            handles to a 16-byte slot, same as field reads).
          - primitive / pointer / etc.: emit the matching scalar
            load mnemonic.

        Managed T (`rc[T]` / `arc[T]` / `weak[T]`) also gets an
        auto-bump on the loaded pointer — the returned value is an
        owned reference whose `__drop__` at the caller's scope exit
        releases its share.
        """
        if len(expr.args) != 2:
            return "0"
        t = self._ast_ty(callee.type_args[0])
        p = self._emit_expr(expr.args[0].value, ctx)
        off = self._emit_expr(expr.args[1].value, ctx)
        addr = ctx.tmp("addr")
        ctx.emit(f"{addr} =l add {p}, {off}")
        if isinstance(t, StructTy):
            return addr  # field address IS the struct value
        if isinstance(t, SliceTy):
            # Hoist the scratch slot to function entry so a `mem_load`
            # inside a loop body doesn't leak 16 bytes of stack per
            # iteration — see FnCtx.prologue_alloca.  The slot is
            # blit-overwritten on each call, so iteration sharing is
            # safe; cross-call-site reuse is *not*, hence one slot
            # per textual call site.
            slot = ctx.prologue_alloca(8, 16, "slslot")
            ctx.emit(f"blit {addr}, {slot}, 16")
            return slot
        tmp = ctx.tmp("ml")
        ctx.emit(f"{tmp} ={_base(t)} {_load(t)} {addr}")
        ctx.tmp_base[tmp] = _base(t)
        if _is_managed(t):
            ctx.emit(f"call {_field_inc_call(t)}(l {tmp})")
        return tmp

    def _emit_mem_store(
        self, expr: Call, callee: GenericInstantiation, ctx: FnCtx
    ) -> None:
        """`mem_store[T](p, off, v)` — write `v` (typed as `T`) at
        `p + off`.  Mirrors `mem_load`: structs and slices `blit`
        their bytes, primitives use the matching scalar store.

        For managed T (rc/arc/weak), the stored value now lives in
        the destination too, so we `arc_inc` before the store.  The
        caller's binding's own release at scope exit balances its
        original ref; the storage's release (via `drop_at` in the
        owning container's `__drop__`) balances the inc here.
        """
        if len(expr.args) != 3:
            return
        t = self._ast_ty(callee.type_args[0])
        p = self._emit_expr(expr.args[0].value, ctx)
        off = self._emit_expr(expr.args[1].value, ctx)
        v = self._emit_expr(expr.args[2].value, ctx)
        addr = ctx.tmp("addr")
        ctx.emit(f"{addr} =l add {p}, {off}")
        if _is_managed(t):
            ctx.emit(f"call {_field_inc_call(t)}(l {v})")
        if isinstance(t, StructTy):
            size = _size(t, self._layouts)
            if size > 0:
                ctx.emit(f"blit {v}, {addr}, {size}")
            return
        if isinstance(t, SliceTy):
            ctx.emit(f"blit {v}, {addr}, 16")
            return
        ctx.emit(f"{_store(t)} {v}, {addr}")

    def _emit_embed(self, path_bytes: bytes, ctx: FnCtx) -> str:
        """Read the file at *path_bytes* (decoded as UTF-8, resolved
        relative to the current module's source directory if not
        absolute), intern its raw bytes into the data section, and
        emit a `[]u8` fat-pointer slot pointing at the data.

        Failure at codegen time is fatal — the path is wrong or the
        file is unreadable, both of which the user has to fix.
        """
        from pathlib import Path as _P
        rel = path_bytes.decode("utf-8")
        path = _P(rel)
        if not path.is_absolute() and self._source_dir is not None:
            path = self._source_dir / path
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CodegenError(
                f"embed: can't read {path}: {exc}"
            ) from exc
        ptr_sym = self._intern_str(data)
        length = len(data)
        slot = ctx.tmp("emb")
        ctx.emit(f"{slot} =l alloc8 16")
        ctx.emit(f"storel {ptr_sym}, {slot}")
        lp = ctx.tmp("lp")
        ctx.emit(f"{lp} =l add {slot}, 8")
        ctx.emit(f"storel {length}, {lp}")
        return slot

    def _emit_hash_intrinsic(
        self, expr: Call, callee: GenericInstantiation, ctx: FnCtx
    ) -> str:
        """`hash[T](v)` — comptime-specialized 64-bit hash.

          - integer / bool primitives + ptr[T]: Knuth multiplicative
            on the value widened to u64.
          - `[]u8` slice: inline FNV-1a length-walk.
          - `StructTy`: `T.__hash__(v)` method call.  Errors at link
            time if the struct hasn't defined `__hash__`.
        """
        t = self._ast_ty(callee.type_args[0])
        v = self._emit_expr(expr.args[0].value, ctx)

        # Slice — `[]u8` FNV-1a.  Other element types not supported.
        if isinstance(t, SliceTy):
            self._ensure_slice_hash_u8()
            tmp = ctx.tmp("h")
            ctx.emit(f"{tmp} =l call $_slice_hash_u8(l {v})")
            return tmp

        # ptr[T] / weak[T] / fn pointers / managed handles: hash the
        # pointer-sized value.
        if isinstance(t, (WrapperTy, FnTy)):
            return self._emit_knuth_hash(v, "l", ctx)

        # Struct: dispatch to user-defined __hash__.
        if isinstance(t, StructTy):
            sname = _struct_name(t)
            qname = self._q(sname)
            if f"{qname}__{'__hash__'}" not in self._fn_param_tys:
                for other_cg in self._all_imports():
                    if f"{other_cg._q(sname)}__{'__hash__'}" in other_cg._fn_param_tys:
                        qname = other_cg._q(sname)
                        break
            tmp = ctx.tmp("h")
            ctx.emit(f"{tmp} =l call ${qname}____hash__(l {v})")
            return tmp

        # Primitives (integers, bool, byte).
        base = _base(t)
        return self._emit_knuth_hash(v, base, ctx)

    def _emit_knuth_hash(self, v: str, base: str, ctx: FnCtx) -> str:
        """Widen `v` to u64 if needed, multiply by the Knuth constant.
        Returns the u64 result temp."""
        widened = v
        if base == "w":
            widened = ctx.tmp("hx")
            ctx.emit(f"{widened} =l extuw {v}")
        elif base != "l":
            # Floats not hashable here; emit zero.
            return "0"
        tmp = ctx.tmp("h")
        ctx.emit(f"{tmp} =l mul {widened}, 11400714819323198485")
        return tmp

    def _ensure_slice_hash_u8(self) -> None:
        """Mark `$_slice_hash_u8` to be appended to this module's IR
        on the first match-on-byte-slice / hash[[]u8] use site."""
        self._slice_hash_u8_emitted = True

    def _emit_eq_intrinsic(
        self, expr: Call, callee: GenericInstantiation, ctx: FnCtx
    ) -> str:
        """`eq[T](a, b)` — comptime-specialized equality.

          - integer / bool / byte primitives + ptr[T]: QBE `ceq{base}`.
          - `[]u8` slice: shared `$_slice_eq_u8` helper.
          - `StructTy`: `T.__eq__(a, b)` method call.
        """
        t = self._ast_ty(callee.type_args[0])
        a = self._emit_expr(expr.args[0].value, ctx)
        b = self._emit_expr(expr.args[1].value, ctx)

        if isinstance(t, SliceTy):
            self._ensure_slice_eq_u8()
            tmp = ctx.tmp("eq")
            ctx.emit(f"{tmp} =w call $_slice_eq_u8(l {a}, l {b})")
            return tmp

        if isinstance(t, (WrapperTy, FnTy)):
            tmp = ctx.tmp("eq")
            ctx.emit(f"{tmp} =w ceql {a}, {b}")
            return tmp

        if isinstance(t, StructTy):
            sname = _struct_name(t)
            qname = self._q(sname)
            if f"{qname}__{'__eq__'}" not in self._fn_param_tys:
                for other_cg in self._all_imports():
                    if f"{other_cg._q(sname)}__{'__eq__'}" in other_cg._fn_param_tys:
                        qname = other_cg._q(sname)
                        break
            tmp = ctx.tmp("eq")
            ctx.emit(f"{tmp} =w call ${qname}____eq__(l {a}, l {b})")
            return tmp

        # Primitives.
        base = _base(t)
        if base in ("s", "d"):
            return "0"
        tmp = ctx.tmp("eq")
        ctx.emit(f"{tmp} =w ceq{base} {a}, {b}")
        return tmp

    def _emit_drop_at(
        self, expr: Call, callee: GenericInstantiation, ctx: FnCtx
    ) -> None:
        """`drop_at[T](p, off)` — drop the T at `p + off`, mirroring
        the per-field cleanup the auto drop wrapper does for
        managed struct fields.  Cases:
          - managed T (rc/arc/weak): load the pointer, arc_release
            (or weak_release for weak).
          - bare struct T with a drop chain: call its drop_fn on the
            element's address (inline-struct convention).
          - everything else: no-op.
        """
        if len(expr.args) != 2:
            return
        t = self._ast_ty(callee.type_args[0])
        p = self._emit_expr(expr.args[0].value, ctx)
        off = self._emit_expr(expr.args[1].value, ctx)
        addr = ctx.tmp("addr")
        ctx.emit(f"{addr} =l add {p}, {off}")
        if _is_managed(t):
            ptr = ctx.tmp("dp")
            ctx.emit(f"{ptr} =l loadl {addr}")
            ctx.emit(f"call {_field_release_call(t)}(l {ptr})")
            return
        if isinstance(t, StructTy):
            lay = self._layouts.get(self._q(_struct_name(t)))
            if lay is None:
                return
            drop_fn = self._struct_drop_fn(lay)
            if drop_fn == "0":
                return
            ctx.emit(f"call {drop_fn}(l {addr})")
            return
        # Primitives, slices, pointers — nothing to drop.

    def _emit_printf_call(self, expr: Call, ctx: FnCtx) -> str:
        """Emit a variadic call to libc printf for `io.printf(fmt, ...)`.

        The format string is a `[]u8` slice; libc expects `const char*`,
        so we load offset 0 (the data pointer) from the slice slot.
        Remaining args are emitted after a `...` separator. f32 promotes
        to f64 per the C variadic ABI.
        """
        if not expr.args:
            ctx.emit("call $printf(l 0, ...)")
            tmp = ctx.tmp("r")
            ctx.emit(f"{tmp} =w copy 0")
            return tmp

        fmt_val = self._emit_expr(expr.args[0].value, ctx)
        fmt_ptr = ctx.tmp("fmt")
        ctx.emit(f"{fmt_ptr} =l loadl {fmt_val}")

        var_args: list[str] = []
        for arg in expr.args[1:]:
            av = self._emit_expr(arg.value, ctx)
            at = self._ty(arg.value)
            ab = _base(at)
            if ab == "s":
                # f32 → f64 promotion for variadic ABI
                promo = ctx.tmp("p")
                ctx.emit(f"{promo} =d exts {av}")
                var_args.append(f"d {promo}")
            elif ab == "w":
                # Widen w-typed integers to l for the variadic ABI;
                # printf reads everything with vaarg[i64].
                inner = _readable(_unwrap(at))
                signed = (
                    isinstance(inner, PrimTy)
                    and inner.name in ("i8", "i16", "i32", "isize")
                )
                promo = ctx.tmp("vp")
                op = "extsw" if signed else "extuw"
                ctx.emit(f"{promo} =l {op} {av}")
                var_args.append(f"l {promo}")
            else:
                var_args.append(f"{ab} {av}")

        args_str = f"l {fmt_ptr}, ..."
        if var_args:
            args_str += ", " + ", ".join(var_args)

        tmp = ctx.tmp("r")
        ctx.emit(f"{tmp} =w call $printf({args_str})")
        ctx.tmp_base[tmp] = "w"  # see FnCtx.tmp_base — needed for yielding-block
        return tmp

    # ── Struct literal ────────────────────────────────────────────────────────

    def _emit_struct_lit(
        self, expr: StructLiteral, ctx: FnCtx, into: Optional[str] = None
    ) -> str:
        """Emit a struct literal.

        With *into=None* (the default), allocates via `arc_alloc` and
        returns the user pointer past the 24-byte ARC header.  This is
        the heap path used for `rc[T]` / `arc[T]` bindings, function
        returns, struct-field inits, etc.

        With *into=<slot>*, writes fields directly into the caller-
        provided slot (no allocation, no ARC header).  Used by the
        stack path: the binding allocates a stack slot via `alloc{a}`
        and hands it to us.  Returns the slot pointer unchanged so the
        caller can keep using the same identifier.
        """
        t = expr.type
        if isinstance(t, NamedType):
            sname = t.name
        elif isinstance(t, GenericType):
            # `_Node[T] { ... }` in a generic body: resolve T via the
            # active substitution so we land on the right specialization.
            spec_ty = self._ast_ty(t)
            sname = _struct_name(spec_ty) if isinstance(spec_ty, StructTy) else t.base
        elif isinstance(t, SelfType):
            sname = ctx.current_struct or ""
        else:
            return "0"

        # `Self { ... }` (parsed as NamedType("Self")) resolves to the
        # enclosing struct's name (already specialized if applicable).
        if sname == "Self" and ctx.current_struct is not None:
            sname = ctx.current_struct

        lay = self._layouts.get(self._q(sname))
        if lay is None:
            # Foreign struct: search imported modules for the layout.
            # The drop_fn symbol lives in the defining module, so we
            # reuse it without re-emitting.
            for other_cg in self._all_imports():
                cand = other_cg._layouts.get(other_cg._q(sname))
                if cand is not None:
                    lay = cand
                    break
            if lay is None:
                return "0"

        if into is None:
            # Heap path: arc_alloc + drop_fn slot.
            # drop_fn is either:
            #   - 0 if no __drop__ AND no managed fields
            #   - $<S>____drop__ if user __drop__ and no managed fields
            #   - $<S>____drop_full__ (auto wrapper) if any managed field
            drop_fn = self._struct_drop_fn(lay)
            ptr = ctx.tmp("ptr")
            ctx.emit(f"{ptr} =l call $arc_alloc(l {lay.total}, l {drop_fn})")
        else:
            # Stack path: caller already allocated; we just init fields.
            ptr = into

        # Evaluate field initializers in source order.  For an inline
        # (bare-struct) field, we want the initializer to write directly
        # into the field's slot when it's a struct literal; otherwise
        # we `blit` the source bytes into the slot.  Non-inline fields
        # keep the existing `store{w,l,h,b}` path.
        fi_by_name = {fi.name: fi for fi in expr.fields}
        fvals: dict[str, str] = {}
        for fl in lay.fields:
            fi = fi_by_name.get(fl.name)
            if fi is None:
                continue
            dest = ptr if fl.offset == 0 else self._gep(ptr, fl.offset, ctx)
            if isinstance(fl.ty, StructTy):
                # Inline composition: this field IS the inner struct.
                inner_lay = self._layouts.get(self._q(_struct_name(fl.ty)))
                inner_size = inner_lay.total if inner_lay else 0
                if isinstance(fi.value, StructLiteral) and inner_lay is not None:
                    # Recurse with `into=dest` so the literal writes
                    # directly into our field slot, skipping the heap.
                    self._emit_struct_lit(fi.value, ctx, into=dest)
                else:
                    src = self._emit_expr(fi.value, ctx)
                    if inner_size > 0:
                        ctx.emit(f"blit {src}, {dest}, {inner_size}")
                continue
            fvals[fl.name] = self._emit_expr(fi.value, ctx)

        for fl in lay.fields:
            if isinstance(fl.ty, StructTy):
                continue  # handled inline above
            v = fvals.get(fl.name, "0")
            dest = ptr if fl.offset == 0 else self._gep(ptr, fl.offset, ctx)
            # Managed field storing a borrowed reference → bump rc so
            # the struct's drop wrapper's release balances.  weak
            # fields use weak_inc; strong managed fields use arc_inc.
            fi = fi_by_name.get(fl.name)
            if (
                _is_managed(fl.ty)
                and fi is not None
                and self._is_borrowed_copy(fi.value)
            ):
                ctx.emit(f"call {_field_inc_call(fl.ty)}(l {v})")
            ctx.emit(f"{_store(fl.ty)} {v}, {dest}")

        return ptr

    # ── String literal ────────────────────────────────────────────────────────

    def _emit_string_lit(self, expr: StringLiteral, ctx: FnCtx) -> str:
        """Emit a []u8 fat pointer on the stack."""
        ptr_name = self._intern_str(expr.value)
        length = len(expr.value)
        slot = ctx.tmp("sl")
        ctx.emit(f"{slot} =l alloc8 16")
        ctx.emit(f"storel {ptr_name}, {slot}")
        lp = ctx.tmp("lp")
        ctx.emit(f"{lp} =l add {slot}, 8")
        ctx.emit(f"storel {length}, {lp}")
        return slot

    # ── Array literal ─────────────────────────────────────────────────────────

    def _emit_array_lit(self, expr: ArrayLiteral, ctx: FnCtx) -> str:
        """Lower `[a, b, c]` to a heap-allocated backing buffer
        plus a stack-resident fat-pointer slot.  For owned-element
        slices (managed `StructTy` or `arc[T]` / `rc[T]` shapes) the
        codegen inserts `arc_inc` per element so the slice and any
        prior references hold valid refs.  Drop responsibility for
        the buffer is left to the v1 caller — slices don't yet have
        a `__drop__`."""
        ty = self._ty(expr)
        ty_inner = _readable(_unwrap(ty))
        if not isinstance(ty_inner, SliceTy):
            return "0"
        et = ty_inner.element
        es = _size(et, self._layouts)
        n = len(expr.elements)

        # Empty literal: data pointer is 0, length is 0.  Skip the
        # alloc — same shape as a 0-length slice from `data[0..0]`.
        slot = ctx.tmp("alit")
        ctx.emit(f"{slot} =l alloc8 16")
        if n == 0:
            ctx.emit(f"storel 0, {slot}")
            lp0 = ctx.tmp("lp")
            ctx.emit(f"{lp0} =l add {slot}, 8")
            ctx.emit(f"storel 0, {lp0}")
            return slot

        data = ctx.tmp("adata")
        total = n * max(es, 1)
        ctx.emit(f"{data} =l call $arc_alloc(l {total}, l 0)")

        store_op = _store(et)
        managed = _is_managed(et)
        for i, el in enumerate(expr.elements):
            ev = self._emit_expr(el, ctx)
            off = i * max(es, 1)
            addr = ctx.tmp("aoff") if off != 0 else data
            if off != 0:
                ctx.emit(f"{addr} =l add {data}, {off}")
            if managed:
                # Slice now holds a strong ref to each managed element.
                ctx.emit(f"call $arc_inc(l {ev})")
            ctx.emit(f"{store_op} {ev}, {addr}")

        ctx.emit(f"storel {data}, {slot}")
        lp = ctx.tmp("lp")
        ctx.emit(f"{lp} =l add {slot}, 8")
        ctx.emit(f"storel {n}, {lp}")
        return slot

    # ── If expression ─────────────────────────────────────────────────────────

    def _emit_if(self, expr: If, ctx: FnCtx) -> str:
        cond = self._emit_expr(expr.condition, ctx)
        then_lbl = ctx.lbl("then")
        else_lbl = ctx.lbl("else")
        after_lbl = ctx.lbl("endif")

        # Result slot — `if` may be used as an expression, in which case the
        # tail expression of each branch is its value.  Width `l` covers any
        # primitive or pointer.  Hoisted to the function prologue so an `if`
        # inside a loop doesn't allocate a fresh 8 bytes per iteration; the
        # in-body `storel 0` re-initializes the slot on each entry.
        result_slot = ctx.prologue_alloca(8, 8, "ires")
        ctx.emit(f"storel 0, {result_slot}")

        ctx.emit(f"jnz {cond}, {then_lbl}, {else_lbl}")

        ctx.label(then_lbl)
        self._emit_block_yielding(expr.then_block, result_slot, ctx)
        last = ctx.out[-1].strip() if ctx.out else ""
        if not (last.startswith("ret") or last.startswith("jmp") or last.startswith("jnz")):
            ctx.emit(f"jmp {after_lbl}")

        ctx.label(else_lbl)
        if expr.else_block is not None:
            self._emit_block_yielding(expr.else_block, result_slot, ctx)
        last = ctx.out[-1].strip() if ctx.out else ""
        if not (last.startswith("ret") or last.startswith("jmp") or last.startswith("jnz")):
            ctx.emit(f"jmp {after_lbl}")

        ctx.label(after_lbl)
        result = ctx.tmp("iv")
        ctx.emit(f"{result} =l loadl {result_slot}")
        return result

    # ── Match expression ──────────────────────────────────────────────────────

    def _ensure_slice_eq_u8(self) -> None:
        """Mark `$_slice_eq_u8` to be appended to this module's IR.
        Idempotent — called from every match-on-`[]u8` site, emits
        the helper body exactly once."""
        self._slice_eq_u8_emitted = True

    def _emit_match(self, expr: Match, ctx: FnCtx) -> str:
        scrut = self._emit_expr(expr.scrutinee, ctx)
        st = self._ty(expr.scrutinee)
        sb = _base(st)
        # Slice scrutinee → ValuePattern arms compare by content, not
        # by fat-pointer address.  Today only `[]u8` is supported (the
        # lexer-keyword use case); add per-element-type helpers when
        # something needs them.
        scrut_for_eq = _readable(_unwrap(st))
        slice_match_eq = (
            isinstance(scrut_for_eq, SliceTy)
            and isinstance(scrut_for_eq.element, PrimTy)
            and scrut_for_eq.element.name == "u8"
        )
        if slice_match_eq:
            self._ensure_slice_eq_u8()
        after = ctx.lbl("matchend")
        n = len(expr.arms)
        arm_lbls = [ctx.lbl(f"arm{i}") for i in range(n)]
        # Per-arm "check" label — where control lands when the previous arm
        # didn't match.  After the last arm, a fall-through goes to `after`.
        check_lbls = [ctx.lbl(f"chk{i}") for i in range(n)]

        # Result slot — match-as-expression stores its tail value here.
        # `l` width (8 bytes) is wide enough for any primitive or pointer.
        # Hoisted to the function prologue for the same reason as `if`:
        # a `match` inside a loop must not leak stack per iteration.
        result_slot = ctx.prologue_alloca(8, 8, "mres")
        ctx.emit(f"storel 0, {result_slot}")

        # Dispatch
        ctx.emit(f"jmp {check_lbls[0]}")
        # Pre-compute sorted variants for tag lookup when the scrutinee
        # is a union (so `TypePattern` arms test the matching variant's
        # tag).  Unions store the tag at offset 0 of a boxed payload.
        scrut_inner = _readable(_unwrap(st))
        union_variants = (
            sorted(scrut_inner.variants, key=_fmt)
            if isinstance(scrut_inner, UnionTy)
            else None
        )
        for i, arm in enumerate(expr.arms):
            ctx.label(check_lbls[i])
            nxt = check_lbls[i + 1] if i + 1 < n else after
            if isinstance(arm.pattern, ValuePattern):
                pv = self._emit_expr(arm.pattern.value, ctx)
                cond = ctx.tmp("mc")
                if slice_match_eq:
                    ctx.emit(f"{cond} =w call $_slice_eq_u8(l {scrut}, l {pv})")
                else:
                    ctx.emit(f"{cond} =w ceq{sb} {scrut}, {pv}")
                ctx.emit(f"jnz {cond}, {arm_lbls[i]}, {nxt}")
            elif isinstance(arm.pattern, TypePattern) and union_variants is not None:
                # Load the tag (offset 0) and compare against the arm's
                # variant tag.  WildcardPattern (default arm) falls
                # through the elif to the unconditional jump below.
                arm_ty = self._ast_ty(arm.pattern.type)
                arm_tag = self._union_tag_for(arm_ty, union_variants)
                tag = ctx.tmp("tag")
                ctx.emit(f"{tag} =l loadl {scrut}")
                cond = ctx.tmp("mc")
                ctx.emit(f"{cond} =w ceql {tag}, {arm_tag}")
                ctx.emit(f"jnz {cond}, {arm_lbls[i]}, {nxt}")
            else:
                ctx.emit(f"jmp {arm_lbls[i]}")

        # Arm bodies — last ExprStatement is treated as the arm's value.
        for i, arm in enumerate(expr.arms):
            ctx.label(arm_lbls[i])
            if isinstance(arm.pattern, TypePattern) and arm.pattern.binding:
                ctx.locals[arm.pattern.binding] = Local(scrut, st, False)
            self._emit_block_yielding(arm.body, result_slot, ctx)
            last = ctx.out[-1].strip() if ctx.out else ""
            if not (last.startswith("ret") or last.startswith("jmp") or last.startswith("jnz")):
                ctx.emit(f"jmp {after}")

        ctx.label(after)
        result = ctx.tmp("mv")
        ctx.emit(f"{result} =l loadl {result_slot}")
        return result

    def _emit_block_yielding(self, block: Block, slot: str, ctx: FnCtx) -> None:
        """Emit a block where the last ExprStatement's value is stored to *slot*.

        Used to make match arms (and if branches) yield values. If the last
        statement is not an ExprStatement, the slot keeps its previous value.

        The slot is `l`-wide (8 bytes) regardless of arm type, so narrow
        integer temps must be sign-extended before storel; constants and
        l-typed temps store directly.  Float yielding is not supported.

        Opens a managed-locals scope around the block so allocations
        inside the branch are released when control leaves the branch
        (analogous to _emit_block). NOTE: if the yielded value is itself
        a managed binding from this scope, the scope-release would drop
        it before the load completes — v1 doesn't support yielding
        managed values from if/match arms.
        """
        ctx.managed_stack.append([])
        ctx.stack_drop_stack.append([])
        stmts = block.statements
        if not stmts:
            ctx.managed_stack.pop()
            ctx.stack_drop_stack.pop()
            return
        for s in stmts[:-1]:
            self._emit_stmt(s, ctx)
        last = stmts[-1]
        if isinstance(last, ExprStatement):
            v = self._emit_expr(last.expr, ctx)
            if v.startswith("%"):
                base = ctx.tmp_base.get(v) or _base(self._ty(last.expr))
                if base == "w":
                    ext = ctx.tmp("ext")
                    ctx.emit(f"{ext} =l extsw {v}")
                    v = ext
            ctx.emit(f"storel {v}, {slot}")
        else:
            self._emit_stmt(last, ctx)
        last_line = ctx.out[-1].strip() if ctx.out else ""
        if not (
            last_line.startswith("ret")
            or last_line.startswith("jmp")
            or last_line.startswith("jnz")
        ):
            self._emit_scope_releases(ctx, ctx.managed_stack[-1])
            self._emit_scope_stack_drops(ctx, ctx.stack_drop_stack[-1])
        ctx.managed_stack.pop()
        ctx.stack_drop_stack.pop()


# ── Public API ────────────────────────────────────────────────────────────────


def generate(
    tree: File,
    types: TypeMap,
    res: ResolutionMap,
    module_prefix: str = "",
) -> tuple[str, str]:
    """Generate output for a single self-contained module; returns
    `(qbe_ir, asm_sidecar)`.

    Multi-module programs go through `loader.compile_program`, which
    constructs the `Codegen` instances directly so cross-module
    generic-fn specialization can flow between them.  This wrapper
    is for single-file tests and one-shot uses.
    """
    return Codegen(types, res, module_prefix).generate(tree)
