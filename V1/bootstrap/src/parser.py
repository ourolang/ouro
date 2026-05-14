"""Ouro recursive-descent + Pratt parser.

Entry point:
    from .parser import parse
    tree = parse(tokens, file="<path>")   # tokens from lexer.lex()
"""

from __future__ import annotations

from typing import Optional

from .nodes import (
    # Span / base
    Span,
    # Types
    NamedType,
    GenericType,
    WrapperType,
    SliceType,
    UnionType,
    InferType,
    SelfType,
    FnType,
    LikeMethod,
    LikeType,
    Type,
    # Patterns
    ValuePattern,
    TypePattern,
    WildcardPattern,
    Pattern,
    # Expressions
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
    Argument,
    Call,
    GenericInstantiation,
    FieldInit,
    StructLiteral,
    BinaryOp,
    UnaryOp,
    TypeTest,
    Cast,
    If,
    MatchArm,
    Match,
    Expression,
    # Statements
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
    Statement,
    # Top-level
    Parameter,
    SelfParam,
    Function,
    StructField,
    Struct,
    Import,
    AsmDecl,
    ExternDecl,
    TopLevelBinding,
    TypeAlias,
    EnumDecl,
    TopLevel,
    File,
)
from .tokens import Token, TokenKind


# ─── Errors ──────────────────────────────────────────────────────────────────


class ParseError(Exception):
    def __init__(self, message: str, span: Span) -> None:
        super().__init__(message)
        self.span = span

    def __str__(self) -> str:
        from .diagnostics import format_diagnostic
        return format_diagnostic(super().__str__(), self.span)


# ─── Precedence table (Pratt) ─────────────────────────────────────────────────

# Each entry: (left_bp, right_bp); right_bp < left_bp for left-assoc.
# Operators not listed have no infix binding power (0).

_INFIX_BP: dict[TokenKind, tuple[int, int]] = {
    TokenKind.OR: (10, 11),
    TokenKind.AND: (20, 21),
    TokenKind.EQ: (30, 31),
    TokenKind.NE: (30, 31),
    TokenKind.LT: (30, 31),
    TokenKind.LE: (30, 31),
    TokenKind.GT: (30, 31),
    TokenKind.GE: (30, 31),
    TokenKind.TYPE_TEST: (35, 36),  # ?= — right-hand side is a type, not an expr
    TokenKind.AS: (36, 37),  # `x as T` — RHS is a type

    # Bitwise group: | weakest, then ^, then &.  Matches C / Rust.
    TokenKind.PIPE: (38, 39),
    TokenKind.CARET: (40, 41),
    TokenKind.AMP: (42, 43),
    # Shifts sit just above bitwise ops, just below arithmetic.
    TokenKind.SHL: (44, 45),
    TokenKind.SHR: (44, 45),
    TokenKind.PLUS: (50, 51),
    TokenKind.MINUS: (50, 51),
    TokenKind.STAR: (60, 61),
    TokenKind.SLASH: (60, 61),
    TokenKind.PERCENT: (60, 61),
    # postfix / call-like handled separately
}

_PREFIX_BP: dict[TokenKind, int] = {
    TokenKind.MINUS: 70,
    TokenKind.NOT: 70,
}

# Binding power for postfix operators (call, index, field-access, generic[])
_POSTFIX_BP = 80


# ─── Parser ──────────────────────────────────────────────────────────────────


class Parser:
    def __init__(self, tokens: list[Token], file: str) -> None:
        self._tokens = tokens
        self._pos = 0
        self._file = file

    # ── Token navigation ────────────────────────────────────────────────────

    def _peek(self, offset: int = 0) -> Token:
        idx = self._pos + offset
        if idx >= len(self._tokens):
            return self._tokens[-1]  # EOF
        return self._tokens[idx]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        if self._pos < len(self._tokens) - 1:
            self._pos += 1
        return tok

    def _check(self, *kinds: TokenKind) -> bool:
        return self._peek().kind in kinds

    def _at_eof(self) -> bool:
        return self._peek().kind == TokenKind.EOF

    def _eat(self, *kinds: TokenKind) -> Token:
        """Consume and return the current token if it matches; raise ParseError otherwise."""
        tok = self._peek()
        if tok.kind not in kinds:
            expected = ", ".join(k.name for k in kinds)
            raise ParseError(
                f"expected {expected}, got {tok.kind.name} ({tok.lexeme!r})",
                tok.span,
            )
        return self._advance()

    def _eat_newlines(self) -> None:
        while self._check(TokenKind.NEWLINE, TokenKind.SEMICOLON):
            self._advance()

    def _span_from(self, start: Token) -> Span:
        """Build a span from `start` to (but not including) the current position."""
        end = self._tokens[max(0, self._pos - 1)]
        return Span(
            file=self._file,
            start_line=start.span.start_line,
            start_col=start.span.start_col,
            end_line=end.span.end_line,
            end_col=end.span.end_col,
        )

    def _err(self, msg: str) -> ParseError:
        return ParseError(msg, self._peek().span)

    # ── Skipping whitespace tokens inside a block ────────────────────────────

    def _skip_newlines(self) -> None:
        while self._check(TokenKind.NEWLINE, TokenKind.SEMICOLON):
            self._advance()

    # ── Types ────────────────────────────────────────────────────────────────

    def _parse_type(self) -> Type:
        """Parse a type expression, handling union `T1 | T2` at the top level."""
        first = self._parse_type_atom()
        if not self._check(TokenKind.PIPE):
            return first
        variants: list[Type] = [first]
        while self._check(TokenKind.PIPE):
            self._advance()
            variants.append(self._parse_type_atom())
        start_span = first.span
        end_tok = self._tokens[max(0, self._pos - 1)]
        span = Span(
            self._file,
            start_span.start_line,
            start_span.start_col,
            end_tok.span.end_line,
            end_tok.span.end_col,
        )
        return UnionType(span=span, variants=variants)

    def _parse_type_atom(self) -> Type:
        tok = self._peek()

        # `fn(params) -> ret` — function-pointer type
        if tok.kind == TokenKind.FN:
            self._advance()
            self._eat(TokenKind.LPAREN)
            params: list[Type] = []
            if not self._check(TokenKind.RPAREN):
                params.append(self._parse_type())
                while self._check(TokenKind.COMMA):
                    self._advance()
                    params.append(self._parse_type())
            close = self._eat(TokenKind.RPAREN)
            ret_ty: Optional[Type] = None
            end = close
            if self._check(TokenKind.ARROW):
                self._advance()
                ret_ty = self._parse_type()
            span = Span(
                self._file,
                tok.span.start_line,
                tok.span.start_col,
                ret_ty.span.end_line if ret_ty else end.span.end_line,
                ret_ty.span.end_col if ret_ty else end.span.end_col,
            )
            return FnType(span=span, params=params, return_type=ret_ty)

        # `[]T` — slice type
        if tok.kind == TokenKind.LBRACKET:
            self._advance()
            self._eat(TokenKind.RBRACKET)
            inner = self._parse_type_atom()
            span = Span(
                self._file,
                tok.span.start_line,
                tok.span.start_col,
                inner.span.end_line,
                inner.span.end_col,
            )
            return SliceType(span=span, element=inner)

        # `like[name(params) -> ret, ..., ...]` — duck-typed shape constraint
        if tok.kind == TokenKind.LIKE:
            return self._parse_like_type()

        # `?` — inference placeholder
        if tok.kind == TokenKind.QUESTION:
            self._advance()
            return InferType(span=tok.span)

        # `Self` keyword
        if tok.kind == TokenKind.SELF_UPPER:
            self._advance()
            return SelfType(span=tok.span)

        # Named / wrapper / generic starting with an identifier
        if tok.kind in (TokenKind.IDENT, TokenKind.VAR, TokenKind.CONST):
            name = tok.lexeme
            self._advance()

            # Wrapper types: var[T], const[T], rc[T], arc[T], weak[T], ptr[T]
            # (rc = non-atomic refcount; arc = atomic refcount; weak = weak
            # ref to either rc or arc value)
            if name in ("var", "const", "rc", "arc", "weak", "ptr") and self._check(
                TokenKind.LBRACKET
            ):
                self._advance()  # consume `[`
                inner = self._parse_type()
                end = self._eat(TokenKind.RBRACKET)
                span = Span(
                    self._file,
                    tok.span.start_line,
                    tok.span.start_col,
                    end.span.end_line,
                    end.span.end_col,
                )
                return WrapperType(span=span, wrapper=name, inner=inner)

            # Generic: Name[T, U]
            if self._check(TokenKind.LBRACKET):
                self._advance()
                args: list[Type] = [self._parse_type()]
                while self._check(TokenKind.COMMA):
                    self._advance()
                    args.append(self._parse_type())
                end = self._eat(TokenKind.RBRACKET)
                span = Span(
                    self._file,
                    tok.span.start_line,
                    tok.span.start_col,
                    end.span.end_line,
                    end.span.end_col,
                )
                return GenericType(span=span, base=name, args=args)

            # Module-qualified type: `module.TypeName`, plus
            # `Enum.Variant` (current module) and the chained form
            # `module.Enum.Variant` (cross-module enum variant).
            if self._check(TokenKind.DOT):
                self._advance()
                inner_tok = self._eat(TokenKind.IDENT)
                end_span = inner_tok.span
                # Cross-module enum variant: `mod.Enum.Variant`.  Two
                # dots, three identifiers.  Encoded as a NamedType
                # where `name` is the variant and `module` mangles
                # the (mod, enum) pair as `mod.Enum` so the resolver
                # can split it back.
                if self._check(TokenKind.DOT):
                    self._advance()
                    variant_tok = self._eat(TokenKind.IDENT)
                    span = Span(
                        self._file,
                        tok.span.start_line,
                        tok.span.start_col,
                        variant_tok.span.end_line,
                        variant_tok.span.end_col,
                    )
                    return NamedType(
                        span=span,
                        name=variant_tok.lexeme,
                        module=f"{name}.{inner_tok.lexeme}",
                    )
                span = Span(
                    self._file,
                    tok.span.start_line,
                    tok.span.start_col,
                    end_span.end_line,
                    end_span.end_col,
                )
                # Generic instantiation on qualified types:
                # `mod.Box[i32]` lands here too.
                if self._check(TokenKind.LBRACKET):
                    self._advance()
                    args: list[Type] = [self._parse_type()]
                    while self._check(TokenKind.COMMA):
                        self._advance()
                        args.append(self._parse_type())
                    end = self._eat(TokenKind.RBRACKET)
                    span = Span(
                        self._file,
                        tok.span.start_line,
                        tok.span.start_col,
                        end.span.end_line,
                        end.span.end_col,
                    )
                    return GenericType(
                        span=span,
                        base=inner_tok.lexeme,
                        args=args,
                        module=name,
                    )
                return NamedType(
                    span=span, name=inner_tok.lexeme, module=name
                )

            # Plain named type
            return NamedType(span=tok.span, name=name)

        raise self._err(f"expected type, got {tok.kind.name} ({tok.lexeme!r})")

    def _parse_array_literal(self) -> ArrayLiteral:
        """Parse `[e1, e2, ..., eN]`.  Empty `[]` is allowed; the
        element type stays open until inferred from context.
        Newlines inside the brackets are whitespace, same as every
        other bracket-delimited construct."""
        start = self._eat(TokenKind.LBRACKET)
        elements: list[Expression] = []
        if not self._check(TokenKind.RBRACKET):
            elements.append(self._parse_expr(0))
            while self._check(TokenKind.COMMA):
                self._advance()
                if self._check(TokenKind.RBRACKET):
                    break          # trailing comma
                elements.append(self._parse_expr(0))
        end = self._eat(TokenKind.RBRACKET)
        span = Span(
            self._file,
            start.span.start_line, start.span.start_col,
            end.span.end_line, end.span.end_col,
        )
        return ArrayLiteral(span=span, elements=elements)

    def _parse_like_type(self) -> LikeType:
        """Parse `like[m1, m2]` (explicit form) or `like[StructName]`
        (alias-from form).  In the explicit form each entry is
        `name(p1, p2) -> ret` — parameter names and the receiver are
        elided.  In the alias-from form the bracket body is a single
        type reference (`Name`, `Name[T]`, `mod.Name`, `mod.Name[T]`)
        whose methods become the constraint.  Newlines are whitespace
        inside brackets, so multi-line is free.  Shapes are always
        open: an impl may carry extra methods beyond the listed ones.
        """
        start = self._eat(TokenKind.LIKE)
        self._eat(TokenKind.LBRACKET)
        methods: list[LikeMethod] = []
        from_struct: Optional[Type] = None
        if self._is_like_alias_form():
            from_struct = self._parse_type()
        else:
            while not self._check(TokenKind.RBRACKET):
                methods.append(self._parse_like_method())
                if self._check(TokenKind.COMMA):
                    self._advance()
                elif not self._check(TokenKind.RBRACKET):
                    raise self._err(
                        f"expected ',' or ']' in like[...], got "
                        f"{self._peek().kind.name}"
                    )
        end = self._eat(TokenKind.RBRACKET)
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            end.span.end_line,
            end.span.end_col,
        )
        return LikeType(span=span, methods=methods, from_struct=from_struct)

    def _is_like_alias_form(self) -> bool:
        """At the start of a `like[...]` body, decide whether the
        first item is a type reference (alias form) or a method
        signature (explicit form).  A type reference is `IDENT
        ('.' IDENT)? ('[' balanced ']')?` with no trailing `(`; a
        method always has the trailing `(`.  Returns True for alias.
        """
        if not self._check(TokenKind.IDENT):
            return False
        i = 1
        if self._peek(i).kind == TokenKind.DOT:
            if self._peek(i + 1).kind != TokenKind.IDENT:
                return False
            i += 2
        if self._peek(i).kind == TokenKind.LBRACKET:
            depth = 1
            i += 1
            while depth > 0 and self._peek(i).kind != TokenKind.EOF:
                if self._peek(i).kind == TokenKind.LBRACKET:
                    depth += 1
                elif self._peek(i).kind == TokenKind.RBRACKET:
                    depth -= 1
                i += 1
        return self._peek(i).kind != TokenKind.LPAREN

    def _parse_like_method(self) -> LikeMethod:
        """One `name(p1, p2, ...) -> ret` entry inside `like[...]`.
        Parameter list is types only (no names, no receiver)."""
        name_tok = self._eat(TokenKind.IDENT)
        self._eat(TokenKind.LPAREN)
        params: list[Type] = []
        if not self._check(TokenKind.RPAREN):
            params.append(self._parse_type())
            while self._check(TokenKind.COMMA):
                self._advance()
                params.append(self._parse_type())
        close = self._eat(TokenKind.RPAREN)
        ret_ty: Optional[Type] = None
        end_line, end_col = close.span.end_line, close.span.end_col
        if self._check(TokenKind.ARROW):
            self._advance()
            ret_ty = self._parse_type()
            end_line, end_col = ret_ty.span.end_line, ret_ty.span.end_col
        span = Span(
            self._file,
            name_tok.span.start_line,
            name_tok.span.start_col,
            end_line,
            end_col,
        )
        return LikeMethod(
            span=span,
            name=name_tok.lexeme,
            params=params,
            return_type=ret_ty,
        )

    # ── Patterns (match arms) ────────────────────────────────────────────────

    def _parse_pattern(self) -> Pattern:
        """Parse a match arm pattern *including* its trailing colon.

        Arm syntax:
          value_pattern  → expr ':'                         (one colon)
          type_pattern   → (name | '_') ':' Type ':'        (two colons; first is binding separator, second is arm separator)
          wildcard       → '_' ':'                           (one colon — no type)

        In all cases this method leaves the cursor just after the final ':'.
        """
        tok = self._peek()

        # `_: Type:` type-pattern with discard binding, or `_:` plain wildcard
        if tok.kind == TokenKind.IDENT and tok.lexeme == "_":
            self._advance()
            self._eat(TokenKind.COLON)
            # If next token looks like a type, it's `_: Type:`
            if self._check(
                TokenKind.IDENT,
                TokenKind.SELF_UPPER,
                TokenKind.LBRACKET,
                TokenKind.QUESTION,
            ):
                typ = self._parse_type()
                # eat the arm-body colon
                self._eat(TokenKind.COLON)
                span = Span(
                    self._file,
                    tok.span.start_line,
                    tok.span.start_col,
                    typ.span.end_line,
                    typ.span.end_col,
                )
                return TypePattern(span=span, binding=None, type=typ)
            # Plain wildcard `_:` — colon already consumed above
            return WildcardPattern(span=tok.span)

        # `name: Type:` — type-discriminated pattern with binding
        if tok.kind == TokenKind.IDENT:
            next_tok = self._peek(1)
            if next_tok.kind == TokenKind.COLON:
                after_colon = self._peek(2)
                if after_colon.kind in (
                    TokenKind.IDENT,
                    TokenKind.SELF_UPPER,
                    TokenKind.LBRACKET,
                    TokenKind.QUESTION,
                ):
                    name = tok.lexeme
                    self._advance()  # consume name
                    self._advance()  # consume first `:`
                    typ = self._parse_type()
                    # eat the arm-body colon
                    self._eat(TokenKind.COLON)
                    span = Span(
                        self._file,
                        tok.span.start_line,
                        tok.span.start_col,
                        typ.span.end_line,
                        typ.span.end_col,
                    )
                    return TypePattern(span=span, binding=name, type=typ)

        # Value pattern: expr then arm-body `:`
        expr = self._parse_expr(0)
        self._eat(TokenKind.COLON)
        return ValuePattern(span=expr.span, value=expr)

    # ── Expressions (Pratt) ──────────────────────────────────────────────────

    def _parse_expr(self, min_bp: int) -> Expression:
        """Pratt expression parser with `min_bp` as minimum binding power."""
        tok = self._peek()

        # ── Prefix / atoms ──────────────────────────────────────────────────

        # Unary operators
        if tok.kind in _PREFIX_BP:
            bp = _PREFIX_BP[tok.kind]
            self._advance()
            operand = self._parse_expr(bp)
            span = Span(
                self._file,
                tok.span.start_line,
                tok.span.start_col,
                operand.span.end_line,
                operand.span.end_col,
            )
            lhs: Expression = UnaryOp(span=span, op=tok.lexeme, operand=operand)

        # Parenthesised expression
        elif tok.kind == TokenKind.LPAREN:
            self._advance()
            lhs = self._parse_expr(0)
            self._eat(TokenKind.RPAREN)

        # Array literal: `[a, b, c]` (or `[]` for empty).  Prefix
        # `[` in expression position is unambiguous — postfix
        # indexing `lhs[i]` is bound by `_POSTFIX_BP` on the lhs's
        # left side, not as a prefix.
        elif tok.kind == TokenKind.LBRACKET:
            lhs = self._parse_array_literal()

        # `if` expression
        elif tok.kind == TokenKind.IF:
            lhs = self._parse_if()

        # `match` expression
        elif tok.kind == TokenKind.MATCH:
            lhs = self._parse_match()

        # Literals
        elif tok.kind == TokenKind.INT:
            self._advance()
            lhs = IntLiteral(
                span=tok.span, value=tok.data["value"], suffix=tok.data.get("suffix")
            )
        elif tok.kind == TokenKind.FLOAT:
            self._advance()
            lhs = FloatLiteral(
                span=tok.span, value=tok.data["value"], suffix=tok.data.get("suffix")
            )
        elif tok.kind == TokenKind.TRUE:
            self._advance()
            lhs = BoolLiteral(span=tok.span, value=True)
        elif tok.kind == TokenKind.FALSE:
            self._advance()
            lhs = BoolLiteral(span=tok.span, value=False)
        elif tok.kind == TokenKind.BYTE:
            self._advance()
            lhs = ByteLiteral(span=tok.span, value=tok.data["value"])
        elif tok.kind == TokenKind.STRING:
            self._advance()
            lhs = StringLiteral(
                span=tok.span,
                value=tok.data["value"],
                is_multiline=tok.data.get("is_multiline", False),
            )

        # `_` discard
        elif tok.kind == TokenKind.IDENT and tok.lexeme == "_":
            self._advance()
            lhs = Discard(span=tok.span)

        # Identifier / keyword-as-name
        elif tok.kind in (TokenKind.IDENT, TokenKind.SELF_LOWER, TokenKind.SELF_UPPER):
            self._advance()
            lhs = Name(span=tok.span, name=tok.lexeme)

        else:
            raise self._err(
                f"unexpected token in expression: {tok.kind.name} ({tok.lexeme!r})"
            )

        # ── Postfix / infix loop ─────────────────────────────────────────────

        while True:
            op_tok = self._peek()

            # Field access: `lhs.field`
            if op_tok.kind == TokenKind.DOT and _POSTFIX_BP > min_bp:
                self._advance()
                field_tok = self._eat(TokenKind.IDENT)
                span = Span(
                    self._file,
                    lhs.span.start_line,
                    lhs.span.start_col,
                    field_tok.span.end_line,
                    field_tok.span.end_col,
                )
                lhs = FieldAccess(span=span, obj=lhs, field=field_tok.lexeme)
                continue

            # Call: `lhs(args)`
            if op_tok.kind == TokenKind.LPAREN and _POSTFIX_BP > min_bp:
                self._advance()
                args = self._parse_call_args()
                end = self._eat(TokenKind.RPAREN)
                span = Span(
                    self._file,
                    lhs.span.start_line,
                    lhs.span.start_col,
                    end.span.end_line,
                    end.span.end_col,
                )
                lhs = Call(span=span, callee=lhs, args=args)
                continue

            # Subscript / generic instantiation: `lhs[...]`
            if op_tok.kind == TokenKind.LBRACKET and _POSTFIX_BP > min_bp:
                lhs = self._parse_bracket_postfix(lhs)
                continue

            # Struct literal: `lhs { ... }` — only when lhs is a Name or
            # GenericInstantiation and next token is `{`
            if (
                op_tok.kind == TokenKind.LBRACE
                and _POSTFIX_BP > min_bp
                and isinstance(lhs, (Name, GenericInstantiation, FieldAccess))
            ):
                lhs = self._parse_struct_literal(lhs)
                continue

            # `?=` type-test operator
            if op_tok.kind == TokenKind.TYPE_TEST:
                left_bp, right_bp = _INFIX_BP[op_tok.kind]
                if left_bp <= min_bp:
                    break
                self._advance()
                typ = self._parse_type()
                span = Span(
                    self._file,
                    lhs.span.start_line,
                    lhs.span.start_col,
                    typ.span.end_line,
                    typ.span.end_col,
                )
                lhs = TypeTest(span=span, operand=lhs, type=typ)
                continue

            # `as` explicit cast — same shape as ?=, RHS is a type.
            if op_tok.kind == TokenKind.AS:
                left_bp, _right_bp = _INFIX_BP[op_tok.kind]
                if left_bp <= min_bp:
                    break
                self._advance()
                typ = self._parse_type()
                span = Span(
                    self._file,
                    lhs.span.start_line,
                    lhs.span.start_col,
                    typ.span.end_line,
                    typ.span.end_col,
                )
                lhs = Cast(span=span, operand=lhs, type=typ)
                continue

            # Normal binary operators
            if op_tok.kind in _INFIX_BP:
                left_bp, right_bp = _INFIX_BP[op_tok.kind]
                if left_bp <= min_bp:
                    break
                self._advance()
                rhs = self._parse_expr(right_bp)
                span = Span(
                    self._file,
                    lhs.span.start_line,
                    lhs.span.start_col,
                    rhs.span.end_line,
                    rhs.span.end_col,
                )
                lhs = BinaryOp(span=span, op=op_tok.lexeme, left=lhs, right=rhs)
                continue

            # `..` range
            if op_tok.kind == TokenKind.DOT_DOT and _POSTFIX_BP > min_bp:
                self._advance()
                # End may be absent (e.g. `arr[2..]`)
                if self._check(
                    TokenKind.RBRACKET,
                    TokenKind.NEWLINE,
                    TokenKind.EOF,
                    TokenKind.SEMICOLON,
                ):
                    end_expr = None
                    end_span = op_tok.span
                else:
                    end_expr = self._parse_expr(0)
                    end_span = end_expr.span
                span = Span(
                    self._file,
                    lhs.span.start_line,
                    lhs.span.start_col,
                    end_span.end_line,
                    end_span.end_col,
                )
                lhs = Range(span=span, start=lhs, end=end_expr)
                continue

            break

        return lhs

    def _parse_call_args(self) -> list[Argument]:
        """Parse comma-separated arguments until `)`. Already consumed `(`."""
        args: list[Argument] = []
        if self._check(TokenKind.RPAREN):
            return args
        args.append(self._parse_one_arg())
        while self._check(TokenKind.COMMA):
            self._advance()
            if self._check(TokenKind.RPAREN):
                break
            args.append(self._parse_one_arg())
        return args

    def _parse_one_arg(self) -> Argument:
        start = self._peek()
        # Named arg: `name: expr` — only if IDENT followed by `:`
        if start.kind == TokenKind.IDENT and self._peek(1).kind == TokenKind.COLON:
            name = start.lexeme
            self._advance()
            self._advance()  # `:`
            val = self._parse_expr(0)
            span = Span(
                self._file,
                start.span.start_line,
                start.span.start_col,
                val.span.end_line,
                val.span.end_col,
            )
            return Argument(span=span, name=name, value=val)
        val = self._parse_expr(0)
        return Argument(span=val.span, name=None, value=val)

    def _parse_bracket_postfix(self, lhs: Expression) -> Expression:
        """Handle `lhs[...]` — either index/slice or generic instantiation."""
        self._eat(TokenKind.LBRACKET)

        # Determine whether this is a type-argument list or a value index.
        # Heuristic: if contents look like types (or is empty), treat as generic.
        # We attempt to parse as type args; if we succeed and close with `]`,
        # it's GenericInstantiation. Else fall back to value index.
        saved_pos = self._pos
        try:
            type_args: list[Type] = []
            if not self._check(TokenKind.RBRACKET):
                type_args.append(self._parse_type())
                while self._check(TokenKind.COMMA):
                    self._advance()
                    type_args.append(self._parse_type())
            close = self._eat(TokenKind.RBRACKET)
            # Ambiguity: `x[i]` where `i` is a plain name could be both.
            # If the result is a single NamedType, we still need to decide.
            # Rule: if all args parsed as types AND there is no binary operator
            # following (which would indicate value context), treat as generic.
            span = Span(
                self._file,
                lhs.span.start_line,
                lhs.span.start_col,
                close.span.end_line,
                close.span.end_col,
            )
            return GenericInstantiation(span=span, base=lhs, type_args=type_args)
        except ParseError:
            self._pos = saved_pos

        # Fall back: value index expression
        idx_expr = self._parse_expr(0)
        close = self._eat(TokenKind.RBRACKET)
        span = Span(
            self._file,
            lhs.span.start_line,
            lhs.span.start_col,
            close.span.end_line,
            close.span.end_col,
        )
        return Index(span=span, obj=lhs, index=idx_expr)

    def _parse_struct_literal(self, lhs: Expression) -> StructLiteral:
        """Parse `lhs { field: expr, ... }`. `lhs` is already parsed."""
        # Convert lhs to a Type
        typ = self._expr_to_type(lhs)
        self._eat(TokenKind.LBRACE)
        fields: list[FieldInit] = []
        self._skip_newlines()
        while not self._check(TokenKind.RBRACE):
            name_tok = self._eat(TokenKind.IDENT)
            self._eat(TokenKind.COLON)
            val = self._parse_expr(0)
            end_span = val.span
            span = Span(
                self._file,
                name_tok.span.start_line,
                name_tok.span.start_col,
                end_span.end_line,
                end_span.end_col,
            )
            fields.append(FieldInit(span=span, name=name_tok.lexeme, value=val))
            if self._check(TokenKind.COMMA):
                self._advance()
            self._skip_newlines()
        close = self._eat(TokenKind.RBRACE)
        span = Span(
            self._file,
            lhs.span.start_line,
            lhs.span.start_col,
            close.span.end_line,
            close.span.end_col,
        )
        return StructLiteral(span=span, type=typ, fields=fields)

    def _expr_to_type(self, expr: Expression) -> Type:
        """Best-effort conversion of a parsed expression into a Type node."""
        if isinstance(expr, Name):
            return NamedType(span=expr.span, name=expr.name)
        if isinstance(expr, GenericInstantiation):
            if isinstance(expr.base, Name):
                return GenericType(
                    span=expr.span, base=expr.base.name, args=expr.type_args
                )
        if isinstance(expr, FieldAccess):
            # e.g. `Mod.Type` — represent as NamedType with dotted name for now
            return NamedType(
                span=expr.span, name=f"{self._expr_to_name(expr.obj)}.{expr.field}"
            )
        raise ParseError("expression cannot be used as a type", expr.span)

    def _expr_to_name(self, expr: Expression) -> str:
        if isinstance(expr, Name):
            return expr.name
        if isinstance(expr, FieldAccess):
            return f"{self._expr_to_name(expr.obj)}.{expr.field}"
        raise ParseError("expected a name", expr.span)

    # ── `if` and `match` ─────────────────────────────────────────────────────

    def _parse_body(self) -> Block:
        """Parse either an indented block or an inline single statement."""
        if self._check(TokenKind.INDENT):
            return self._parse_block()
        # Inline body: one (or more `;`-separated) statements on the same line
        stmts: list[Statement] = []
        stmts.append(self._parse_statement())
        while self._check(TokenKind.SEMICOLON):
            self._advance()
            if self._check(TokenKind.NEWLINE, TokenKind.DEDENT, TokenKind.EOF):
                break
            stmts.append(self._parse_statement())
        span = Span(
            self._file,
            stmts[0].span.start_line,
            stmts[0].span.start_col,
            stmts[-1].span.end_line,
            stmts[-1].span.end_col,
        )
        return Block(span=span, statements=stmts)

    def _parse_if(self) -> If:
        start = self._eat(TokenKind.IF)
        cond = self._parse_expr(0)
        self._eat(TokenKind.COLON)
        self._eat_newlines()
        then_block = self._parse_body()

        else_block: Optional[Block] = None
        if self._check(TokenKind.ELSE):
            self._advance()
            self._eat(TokenKind.COLON)
            self._eat_newlines()
            else_block = self._parse_body()
        elif self._check(TokenKind.ELIF):
            nested_if = self._parse_if_from_elif()
            else_block = Block(
                span=nested_if.span,
                statements=[ExprStatement(span=nested_if.span, expr=nested_if)],
            )

        span = self._span_from(start)
        return If(
            span=span, condition=cond, then_block=then_block, else_block=else_block
        )

    def _parse_if_from_elif(self) -> If:
        """Same as _parse_if but starts by consuming `elif` instead of `if`."""
        start = self._eat(TokenKind.ELIF)
        cond = self._parse_expr(0)
        self._eat(TokenKind.COLON)
        self._eat_newlines()
        then_block = self._parse_body()

        else_block: Optional[Block] = None
        if self._check(TokenKind.ELSE):
            self._advance()
            self._eat(TokenKind.COLON)
            self._eat_newlines()
            else_block = self._parse_body()
        elif self._check(TokenKind.ELIF):
            nested_if = self._parse_if_from_elif()
            else_block = Block(
                span=nested_if.span,
                statements=[ExprStatement(span=nested_if.span, expr=nested_if)],
            )

        span = self._span_from(start)
        return If(
            span=span, condition=cond, then_block=then_block, else_block=else_block
        )

    def _parse_match(self) -> Match:
        start = self._eat(TokenKind.MATCH)
        scrutinee = self._parse_expr(0)
        self._eat(TokenKind.COLON)
        self._eat_newlines()
        self._eat(TokenKind.INDENT)

        arms: list[MatchArm] = []
        while not self._check(TokenKind.DEDENT, TokenKind.EOF):
            self._skip_newlines()
            if self._check(TokenKind.DEDENT, TokenKind.EOF):
                break
            arm_start = self._peek()
            pattern = self._parse_pattern()
            # _parse_pattern already consumed the colon for value patterns,
            # and for type/wildcard patterns.
            self._eat_newlines()
            body = self._parse_arm_body()
            arm_span = Span(
                self._file,
                arm_start.span.start_line,
                arm_start.span.start_col,
                body.span.end_line,
                body.span.end_col,
            )
            arms.append(MatchArm(span=arm_span, pattern=pattern, body=body))
            self._skip_newlines()

        self._eat(TokenKind.DEDENT)
        span = self._span_from(start)
        return Match(span=span, scrutinee=scrutinee, arms=arms)

    def _parse_arm_body(self) -> Block:
        return self._parse_body()

    # ── Blocks and statements ────────────────────────────────────────────────

    def _parse_block(self) -> Block:
        """Parse an INDENT-delimited block of statements."""
        open_tok = self._eat(TokenKind.INDENT)
        stmts: list[Statement] = []
        while not self._check(TokenKind.DEDENT, TokenKind.EOF):
            self._skip_newlines()
            if self._check(TokenKind.DEDENT, TokenKind.EOF):
                break
            stmt = self._parse_statement()
            stmts.append(stmt)
            # Consume trailing semicolons / newlines
            while self._check(TokenKind.SEMICOLON):
                self._advance()
                if self._check(TokenKind.NEWLINE, TokenKind.DEDENT, TokenKind.EOF):
                    break
                stmts.append(self._parse_statement())
            self._skip_newlines()
        close = self._eat(TokenKind.DEDENT)
        span = Span(
            self._file,
            open_tok.span.start_line,
            open_tok.span.start_col,
            close.span.end_line,
            close.span.end_col,
        )
        return Block(span=span, statements=stmts)

    def _parse_statement(self) -> Statement:
        tok = self._peek()

        if tok.kind == TokenKind.RETURN:
            return self._parse_return()
        if tok.kind == TokenKind.PASS:
            self._advance()
            return Pass(span=tok.span)
        if tok.kind == TokenKind.BREAK:
            self._advance()
            return Break(span=tok.span)
        if tok.kind == TokenKind.CONTINUE:
            self._advance()
            return Continue(span=tok.span)
        if tok.kind == TokenKind.FOR:
            return self._parse_for()
        if tok.kind == TokenKind.LOOP:
            return self._parse_loop()
        if tok.kind == TokenKind.WHILE:
            return self._parse_while()
        if tok.kind == TokenKind.IF:
            expr = self._parse_if()
            return ExprStatement(span=expr.span, expr=expr)
        if tok.kind == TokenKind.MATCH:
            expr = self._parse_match()
            return ExprStatement(span=expr.span, expr=expr)

        # Binding or assignment — both start with an expression/name
        return self._parse_binding_or_assign_or_expr()

    def _parse_return(self) -> Return:
        start = self._eat(TokenKind.RETURN)
        if self._check(
            TokenKind.NEWLINE, TokenKind.SEMICOLON, TokenKind.DEDENT, TokenKind.EOF
        ):
            return Return(span=start.span, value=None)
        val = self._parse_expr(0)
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            val.span.end_line,
            val.span.end_col,
        )
        return Return(span=span, value=val)

    def _parse_for(self) -> For:
        start = self._eat(TokenKind.FOR)
        # binding (name or `_`)
        name_tok = self._peek()
        if name_tok.kind == TokenKind.IDENT:
            binding = name_tok.lexeme
            self._advance()
        else:
            raise self._err("expected identifier or `_` after `for`")

        # Optional type annotation: `for x: T in ...` or `for x: var[T] in ...`
        binding_type: Optional[Type] = None
        if self._check(TokenKind.COLON):
            self._advance()
            binding_type = self._parse_type()

        self._eat(TokenKind.IN)
        iterable = self._parse_expr(0)
        self._eat(TokenKind.COLON)
        self._eat_newlines()
        body = self._parse_body()
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            body.span.end_line,
            body.span.end_col,
        )
        return For(
            span=span,
            binding=binding,
            binding_type=binding_type,
            iterable=iterable,
            body=body,
        )

    def _parse_loop(self) -> Loop:
        start = self._eat(TokenKind.LOOP)
        self._eat(TokenKind.COLON)
        self._eat_newlines()
        body = self._parse_body()
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            body.span.end_line,
            body.span.end_col,
        )
        return Loop(span=span, body=body)

    def _parse_while(self) -> Loop:
        """Parse `while cond: body` and desugar to a Loop containing
        an `if not cond: break` guard at the top.

        Lowering produces a regular Loop, so the resolver, type checker,
        and codegen don't need any new logic.
        """
        start = self._eat(TokenKind.WHILE)
        cond = self._parse_expr(0)
        self._eat(TokenKind.COLON)
        self._eat_newlines()
        body = self._parse_body()
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            body.span.end_line,
            body.span.end_col,
        )

        not_cond = UnaryOp(span=cond.span, op="not", operand=cond)
        break_stmt = Break(span=cond.span)
        break_block = Block(span=cond.span, statements=[break_stmt])
        guard = If(
            span=cond.span,
            condition=not_cond,
            then_block=break_block,
            else_block=None,
        )
        guard_stmt = ExprStatement(span=cond.span, expr=guard)
        new_body = Block(
            span=body.span,
            statements=[guard_stmt, *body.statements],
        )
        return Loop(span=span, body=new_body)

    def _parse_binding_or_assign_or_expr(self) -> Statement:
        """Disambiguate between:
        name = expr           (Binding, no type)
        name: Type = expr     (Binding, with type)
        lhs = expr            (Assignment to lhs)
        expr                  (ExprStatement)
        """
        # Binding: `name: [Type] = expr` starts with an IDENT then `:` or `=`
        tok = self._peek()

        # `name: Type = expr` — explicit typed binding
        if tok.kind == TokenKind.IDENT and self._peek(1).kind == TokenKind.COLON:
            name = tok.lexeme
            self._advance()  # name
            self._advance()  # `:`
            ann = self._parse_type()
            self._eat(TokenKind.ASSIGN)
            val = self._parse_expr(0)
            span = Span(
                self._file,
                tok.span.start_line,
                tok.span.start_col,
                val.span.end_line,
                val.span.end_col,
            )
            return Binding(span=span, name=name, type=ann, value=val)

        # `name = expr` — inferred binding (if `name` hasn't been seen before)
        # We can't know here — emit Binding; type-checker resolves vs Assignment.
        if tok.kind == TokenKind.IDENT and self._peek(1).kind == TokenKind.ASSIGN:
            name = tok.lexeme
            self._advance()
            self._advance()  # `=`
            val = self._parse_expr(0)
            span = Span(
                self._file,
                tok.span.start_line,
                tok.span.start_col,
                val.span.end_line,
                val.span.end_col,
            )
            return Binding(span=span, name=name, type=None, value=val)

        # Otherwise parse as expression, then check for `=`
        expr = self._parse_expr(0)

        if self._check(TokenKind.ASSIGN):
            self._advance()
            val = self._parse_expr(0)
            span = Span(
                self._file,
                expr.span.start_line,
                expr.span.start_col,
                val.span.end_line,
                val.span.end_col,
            )
            return Assignment(span=span, target=expr, value=val)

        return ExprStatement(span=expr.span, expr=expr)

    # ── Top-level declarations ───────────────────────────────────────────────

    def _parse_import(self) -> Import:
        """Parse `name = import("path")`."""
        name_tok = self._eat(TokenKind.IDENT)
        self._eat(TokenKind.ASSIGN)
        self._eat(TokenKind.IMPORT)
        self._eat(TokenKind.LPAREN)
        path_tok = self._eat(TokenKind.STRING)
        self._eat(TokenKind.RPAREN)
        span = Span(
            self._file,
            name_tok.span.start_line,
            name_tok.span.start_col,
            path_tok.span.end_line,
            path_tok.span.end_col,
        )
        return Import(
            span=span, binding=name_tok.lexeme, path=path_tok.data["value"].decode()
        )

    def _parse_function(self) -> Function:
        start = self._eat(TokenKind.FN)
        name_tok = self._eat(TokenKind.IDENT)

        # Optional generic params: `[T, U]`
        generics: list[str] = []
        if self._check(TokenKind.LBRACKET):
            self._advance()
            generics.append(self._eat(TokenKind.IDENT).lexeme)
            while self._check(TokenKind.COMMA):
                self._advance()
                generics.append(self._eat(TokenKind.IDENT).lexeme)
            self._eat(TokenKind.RBRACKET)

        self._eat(TokenKind.LPAREN)
        self_param, params, is_variadic = self._parse_params()
        self._eat(TokenKind.RPAREN)

        # Optional return type: `-> T`
        return_type: Optional[Type] = None
        if self._check(TokenKind.ARROW):
            self._advance()
            return_type = self._parse_type()

        self._eat(TokenKind.COLON)
        self._eat_newlines()
        body = self._parse_block()
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            body.span.end_line,
            body.span.end_col,
        )
        return Function(
            span=span,
            name=name_tok.lexeme,
            generics=generics,
            self_param=self_param,
            params=params,
            return_type=return_type,
            body=body,
            is_variadic=is_variadic,
        )

    def _parse_extern_decl(self) -> ExternDecl:
        """`extern name(params [, ...]) -> return_type` — single-line
        declaration; no body, no colon.  The trailing `...` marks the
        function as variadic; calls past the fixed param count are
        accepted by the typechecker.
        """
        start = self._eat(TokenKind.EXTERN)
        name_tok = self._eat(TokenKind.IDENT)

        self._eat(TokenKind.LPAREN)
        params: list[Parameter] = []
        is_variadic = False
        if not self._check(TokenKind.RPAREN):
            while True:
                if self._check(TokenKind.ELLIPSIS):
                    self._advance()
                    is_variadic = True
                    break
                params.append(self._parse_one_param())
                if not self._check(TokenKind.COMMA):
                    break
                self._advance()
        end_tok = self._eat(TokenKind.RPAREN)
        end_line, end_col = end_tok.span.end_line, end_tok.span.end_col

        return_type: Optional[Type] = None
        if self._check(TokenKind.ARROW):
            self._advance()
            return_type = self._parse_type()
            end_line = return_type.span.end_line
            end_col = return_type.span.end_col
        self._eat_newlines()

        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            end_line,
            end_col,
        )
        return ExternDecl(
            span=span,
            name=name_tok.lexeme,
            params=params,
            return_type=return_type,
            is_variadic=is_variadic,
        )

    def _parse_asm_decl(self) -> AsmDecl:
        """`asm name(params) -> return_type: <ASM_BODY token>`.

        No generics, no self, no struct methods in v1.  The body is
        already captured as a single ASM_BODY token by the lexer.
        """
        start = self._eat(TokenKind.ASM)
        name_tok = self._eat(TokenKind.IDENT)

        self._eat(TokenKind.LPAREN)
        params: list[Parameter] = []
        if not self._check(TokenKind.RPAREN):
            while True:
                params.append(self._parse_one_param())
                if not self._check(TokenKind.COMMA):
                    break
                self._advance()
        self._eat(TokenKind.RPAREN)

        return_type: Optional[Type] = None
        if self._check(TokenKind.ARROW):
            self._advance()
            return_type = self._parse_type()

        self._eat(TokenKind.COLON)
        self._eat_newlines()
        self._eat(TokenKind.INDENT)
        body_tok = self._eat(TokenKind.ASM_BODY)
        # After the body, the lexer emits NEWLINE then DEDENT.  Both
        # are bookkeeping — consume them before returning so the
        # top-level loop lands cleanly on the next decl.
        self._eat_newlines()
        if self._check(TokenKind.DEDENT):
            self._advance()

        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            body_tok.span.end_line,
            body_tok.span.end_col,
        )
        return AsmDecl(
            span=span,
            name=name_tok.lexeme,
            params=params,
            return_type=return_type,
            body_text=body_tok.data["text"],
        )

    def _parse_params(
        self,
    ) -> tuple[Optional[SelfParam], list[Parameter], bool]:
        """Parse the parameter list (already inside parens).  Returns
        `(self_param, params, is_variadic)`; trailing `...` marks the
        function as variadic and must be the last entry."""
        self_param: Optional[SelfParam] = None
        params: list[Parameter] = []

        if self._check(TokenKind.RPAREN):
            return None, [], False

        # Check for `self` first param
        first = self._peek()
        if first.kind == TokenKind.SELF_LOWER:
            # Could be bare `self` or `self: Type`
            self._advance()
            if self._check(TokenKind.COLON):
                self._advance()
                typ = self._parse_type()
                self_param = SelfParam(span=first.span, type=typ, is_default=False)
            else:
                # Bare `self` → ptr[Self]
                ptr_self = WrapperType(
                    span=first.span,
                    wrapper="ptr",
                    inner=SelfType(span=first.span),
                )
                self_param = SelfParam(span=first.span, type=ptr_self, is_default=True)

            if self._check(TokenKind.COMMA):
                self._advance()
            else:
                return self_param, params, False

        # Regular parameters with optional trailing `...`.
        is_variadic = False
        while not self._check(TokenKind.RPAREN):
            if self._check(TokenKind.ELLIPSIS):
                self._advance()
                is_variadic = True
                break
            p = self._parse_one_param()
            params.append(p)
            if self._check(TokenKind.COMMA):
                self._advance()
            else:
                break

        return self_param, params, is_variadic

    def _parse_one_param(self) -> Parameter:
        name_tok = self._eat(TokenKind.IDENT)
        self._eat(TokenKind.COLON)
        typ = self._parse_type()
        span = Span(
            self._file,
            name_tok.span.start_line,
            name_tok.span.start_col,
            typ.span.end_line,
            typ.span.end_col,
        )
        return Parameter(span=span, name=name_tok.lexeme, type=typ)

    def _parse_struct(self) -> Struct:
        start = self._eat(TokenKind.STRUCT)
        name_tok = self._eat(TokenKind.IDENT)

        # Optional generic params
        generics: list[str] = []
        if self._check(TokenKind.LBRACKET):
            self._advance()
            generics.append(self._eat(TokenKind.IDENT).lexeme)
            while self._check(TokenKind.COMMA):
                self._advance()
                generics.append(self._eat(TokenKind.IDENT).lexeme)
            self._eat(TokenKind.RBRACKET)

        self._eat(TokenKind.COLON)
        self._eat_newlines()
        self._eat(TokenKind.INDENT)

        fields: list[StructField] = []
        methods: list[Function] = []

        while not self._check(TokenKind.DEDENT, TokenKind.EOF):
            self._skip_newlines()
            if self._check(TokenKind.DEDENT, TokenKind.EOF):
                break
            if self._check(TokenKind.FN):
                methods.append(self._parse_function())
            elif self._check(TokenKind.PASS):
                self._advance()
            elif self._check(TokenKind.IDENT):
                # Field: `name: Type`
                field_tok = self._peek()
                self._advance()
                self._eat(TokenKind.COLON)
                typ = self._parse_type()
                span = Span(
                    self._file,
                    field_tok.span.start_line,
                    field_tok.span.start_col,
                    typ.span.end_line,
                    typ.span.end_col,
                )
                fields.append(StructField(span=span, name=field_tok.lexeme, type=typ))
            else:
                raise self._err(
                    f"unexpected token in struct body: {self._peek().kind.name}"
                )
            self._skip_newlines()

        close = self._eat(TokenKind.DEDENT)
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            close.span.end_line,
            close.span.end_col,
        )
        return Struct(
            span=span,
            name=name_tok.lexeme,
            generics=generics,
            fields=fields,
            methods=methods,
        )

    def _looks_like_type_alias(self) -> bool:
        """Peek ahead to decide whether `IDENT ...` starts a type alias.
        Shape: `IDENT [ '[' IDENT (',' IDENT)* ']' ]? ':' 'type' '='`.
        Bails early on the first mismatch so the cost is bounded.
        """
        i = 1  # we're sitting on the IDENT; start with the next token
        if self._peek(i).kind == TokenKind.LBRACKET:
            i += 1
            if self._peek(i).kind != TokenKind.IDENT:
                return False
            i += 1
            while self._peek(i).kind == TokenKind.COMMA:
                i += 1
                if self._peek(i).kind != TokenKind.IDENT:
                    return False
                i += 1
            if self._peek(i).kind != TokenKind.RBRACKET:
                return False
            i += 1
        if self._peek(i).kind != TokenKind.COLON:
            return False
        i += 1
        if self._peek(i).kind != TokenKind.TYPE:
            return False
        i += 1
        return self._peek(i).kind == TokenKind.ASSIGN

    def _parse_enum(self) -> EnumDecl:
        """Parse `enum Name:\\n    Var1\\n    Var2\\n...`.  Payload-free
        variants only — each line inside the indented block is a bare
        identifier."""
        start = self._eat(TokenKind.ENUM)
        name_tok = self._eat(TokenKind.IDENT)
        self._eat(TokenKind.COLON)
        self._skip_newlines()
        self._eat(TokenKind.INDENT)

        variants: list[str] = []
        while not self._check(TokenKind.DEDENT) and not self._at_eof():
            if self._check(TokenKind.PASS):
                self._advance()
            else:
                variants.append(self._eat(TokenKind.IDENT).lexeme)
            self._skip_newlines()

        end = self._eat(TokenKind.DEDENT)
        span = Span(
            self._file,
            start.span.start_line,
            start.span.start_col,
            end.span.end_line,
            end.span.end_col,
        )
        return EnumDecl(span=span, name=name_tok.lexeme, variants=variants)

    def _parse_type_alias(self) -> TypeAlias:
        """Parse `name [generics] : type = TYPE_EXPR`."""
        name_tok = self._eat(TokenKind.IDENT)
        generics: list[str] = []
        if self._check(TokenKind.LBRACKET):
            self._advance()
            generics.append(self._eat(TokenKind.IDENT).lexeme)
            while self._check(TokenKind.COMMA):
                self._advance()
                generics.append(self._eat(TokenKind.IDENT).lexeme)
            self._eat(TokenKind.RBRACKET)
        self._eat(TokenKind.COLON)
        self._eat(TokenKind.TYPE)
        self._eat(TokenKind.ASSIGN)
        body = self._parse_type()
        span = Span(
            self._file,
            name_tok.span.start_line,
            name_tok.span.start_col,
            body.span.end_line,
            body.span.end_col,
        )
        return TypeAlias(span=span, name=name_tok.lexeme, generics=generics, body=body)

    def _parse_top_level_binding(self, name_tok: Token) -> TopLevelBinding:
        """Parse the rest of `name [: Type] = expr` after consuming `name`."""
        ann: Optional[Type] = None
        if self._check(TokenKind.COLON):
            self._advance()
            ann = self._parse_type()
        self._eat(TokenKind.ASSIGN)
        val = self._parse_expr(0)
        span = Span(
            self._file,
            name_tok.span.start_line,
            name_tok.span.start_col,
            val.span.end_line,
            val.span.end_col,
        )
        return TopLevelBinding(span=span, name=name_tok.lexeme, type=ann, value=val)

    # ── Entry point ─────────────────────────────────────────────────────────

    def parse_file(self) -> File:
        start = self._peek()
        decls: list[TopLevel] = []

        while not self._at_eof():
            self._skip_newlines()
            if self._at_eof():
                break

            tok = self._peek()

            if tok.kind == TokenKind.FN:
                decls.append(self._parse_function())
            elif tok.kind == TokenKind.STRUCT:
                decls.append(self._parse_struct())
            elif tok.kind == TokenKind.ENUM:
                decls.append(self._parse_enum())
            elif tok.kind == TokenKind.ASM:
                decls.append(self._parse_asm_decl())
            elif tok.kind == TokenKind.EXTERN:
                decls.append(self._parse_extern_decl())
            elif tok.kind == TokenKind.IDENT:
                # Could be `name = import(...)`, `name [: Type] = expr`,
                # or a type alias: `name [: type] = ...` / `name[T]: type
                # = ...`.  Look ahead just far enough to disambiguate.
                next_tok = self._peek(1)
                if (
                    next_tok.kind == TokenKind.ASSIGN
                    and self._peek(2).kind == TokenKind.IMPORT
                ):
                    decls.append(self._parse_import())
                elif self._looks_like_type_alias():
                    decls.append(self._parse_type_alias())
                else:
                    name_tok = self._advance()
                    decls.append(self._parse_top_level_binding(name_tok))
            else:
                raise self._err(
                    f"unexpected token at top level: {tok.kind.name} ({tok.lexeme!r})"
                )

            self._skip_newlines()

        span = self._span_from(start)
        return File(span=span, path=self._file, declarations=decls)


# ─── Public API ───────────────────────────────────────────────────────────────


def parse(tokens: list[Token], file: str = "<input>") -> File:
    """Parse a flat token list (from the lexer) into a File AST node."""
    return Parser(tokens, file).parse_file()
