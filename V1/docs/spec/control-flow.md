# Control flow

`if`, `match`, `for`, and `loop`. The first two are **expressions**
(yielding values); the latter two are statements only.

## `if` / `elif` / `else`

```ouro
if cond:
    body
elif other:
    body
else:
    body
```

`elif` chains are sugar for nested `if`/`else` — the parser
flattens `if A: ... elif B: ... else: ...` into

```
If(A, then,
   else_block = Block([ExprStatement(If(B, then, else_block))]))
```

So later passes don't have a special "elif" case.

### `if` as expression

`if` yields the value of the executed branch:

```ouro
x = if cond:
    100
else:
    200
```

The value is the **tail expression** of the branch (the last
expression statement of the block). If the branch has no tail
expression (only statements like `return`), the slot retains its
default value (zero).

🟡 *Status:* The codegen yields `l`-wide values via a stack slot.
`w`-typed temps are sign-extended. **Floats yielded from arms aren't
yet handled.** Branch-type uniformity isn't checked by the type
checker (so `if x: 1 else: "abc"` slips through).

### Inline `if`

A single statement can follow the colon on the same line:

```ouro
if cond: return 1
```

For multi-statement inline bodies, use `;`:

```ouro
if cond: a = 1; b = 2; c = 3
```

Don't mix inline and block:

```ouro
if cond: return 1     # ok
else:
    return 2          # ok — the parser allows this
```

## `match`

```ouro
match scrutinee:
    pattern1: body1
    pattern2: body2
    _: default
```

🟢 *Locked: one keyword for value AND type patterns.*  We considered
splitting into `switch` (value) and `match` (type), but chose to
unify under `match` — Rust/Swift/Python convention.  The arm shape
(`literal:` vs `name: Type:`) tells the parser and codegen which
kind of dispatch to generate, and `?=` stays as the single-variant
type test inside an `if`.

### Three pattern kinds

```ouro
match x:
    200: io.println("OK")             # value pattern
    _:   io.println("not OK")         # wildcard

match err:
    p: ParseError:                    # type pattern with binding `p`
        io.println(p.msg)
    _: EmptyError:                    # type pattern, no binding
        io.println("empty")
```

#### Value pattern

`expr:` — match by `==` equality. The expression is evaluated in the
**outer scope** (it can reference enclosing names but not arm bindings).

#### Type pattern

`name: T:` or `_: T:` — match by **runtime type**. Used to discriminate
tagged unions:

```ouro
match parse_int(s):
    n: i64:
        io.printf("%ld\n", n)
    e: ParseError:
        io.println(e.msg)
```

If `name` is given, it's bound inside the arm body to the value as
typed `T`.

🟡 *Status:* The parser, resolver, and type checker handle type
patterns; the **codegen treats them as wildcards** (always matches).
So today value-pattern matching works correctly, but type-pattern
discrimination doesn't yet check the union tag.

#### Wildcard

`_:` — always matches, no binding. Use as the catch-all default arm.

### `match` as expression

Like `if`, `match` yields its arm's tail expression:

```ouro
status = match code:
    200: "OK"
    404: "Not Found"
    _:   "Unknown"
```

The slot is `l`-wide; `w`-typed temps are extended.

### Inline arm body

A single statement can follow the arm colon on the same line:

```ouro
match x:
    1: return 1
    2: return 4
    _: return 0
```

For multi-statement arms, use a block:

```ouro
match x:
    1:
        a = ...
        return a
```

### Match exhaustiveness

🔵 *Open.* The compiler does **not** yet check that arms cover the
scrutinee's type. A non-exhaustive `match` on a tagged union will
silently fall through to no arm, which the codegen handles by
storing 0 in the result slot. This is a known gap.

### Pattern colon-counting (parser-level)

Match patterns use **between one and two colons** depending on shape:

| Source | Pattern kind | Colon count |
|---|---|---|
| `1:` | value | 1 |
| `_:` | wildcard | 1 |
| `n: i32:` | type with binding | 2 |
| `_: ParseError:` | type without binding | 2 |

The grammar requires the parser to look ahead and count colons; see
[`docs/parser.md`](../parser.md#pattern-parsers-lines-280-348).

## `for`

```ouro
for x in iterable:
    body
```

Iterates a value that supports the **iterator protocol**:

- `__iter__(self) -> SomeIterator` — invoked once at loop start
- `__next__(self) -> T | StopIteration` — invoked each iteration

The loop variable `x` is bound to `T` (the non-`StopIteration`
variant). When `__next__` returns `StopIteration`, the loop exits.

```ouro
for x in [1, 2, 3]:
    io.printf("%ld\n", x)
```

🟡 *Status:* The codegen handles `for x in slice` with a special
**index-based lowering** (slice → `i = 0, while i < len: ...`).  A
second special case covers integer **ranges**: `for i in 0..n:`
lowers to a numeric counter (`i = 0; while i < n: ...; i += 1`) —
no iterator object, no heap, half-open `[start, end)` semantics.
The generic `__iter__`/`__next__` desugaring isn't yet emitted —
for-loops over user types fall back to a placeholder that runs
the body once.

### Optional type annotation

```ouro
for x: var[?] in items:
    x = transform(x)
```

If `x` is `var`, the body can mutate it (without affecting the
backing iterable, since each iteration gets a fresh binding).

### Discarding the variable

```ouro
for _ in 0..10:
    io.println("tick")
```

`for _ in ...` runs the body for each element without binding.

### Range syntax `0..10`

🔵 *Open.* The grammar parses `0..10` as a `Range` expression, but
the codegen doesn't yet handle `for x in 0..10` — only slice
iteration works end-to-end. To get a 10-iteration loop, use:

```ouro
n: var[i32] = 0
loop:
    if n >= 10: break
    ...
    n = n + 1
```

## `while`

```ouro
while cond:
    body
```

Loops while `cond` is true. The condition is re-evaluated at the top
of every iteration.

🟢 *Locked.* 🟡 *Implemented* — desugared at parse time to a `loop`
with an `if not cond: break` guard prepended to the body. So
`while`-specific bugs don't exist: anything that affects `loop` /
`if` / `break` applies to `while` too.

```ouro
n: var[i32] = 0
while n < 10:
    n = n + 1
```

`break` and `continue` work as expected — they bind to the
enclosing `while` (via the underlying `loop`).

## `loop`

Infinite loop:

```ouro
loop:
    body
```

Exit with `break`. There is no `do-while` or `until`. Use `loop` when
the exit condition is best placed somewhere other than the top of the
iteration, or when there is no natural condition at all:

```ouro
loop:
    work()
    if done(): break    # exit-at-bottom
```

For top-of-iteration conditions, prefer `while` (it's the same lowering,
but reads more directly).

🟡 *Status:* Works fully end-to-end.

## `break` / `continue`

`break` exits the innermost enclosing loop. `continue` jumps to the
next iteration of the innermost enclosing loop.

```ouro
loop:
    if done: break
    if skip: continue
    work()
```

There is no labeled `break` to exit outer loops. Use a flag:

```ouro
should_exit: var[bool] = false
loop:
    inner_loop:
        if condition:
            should_exit = true
            break
    if should_exit: break
```

## `return`

```ouro
return expr      # return a value
return           # return nothing (only legal when return type is unit)
```

The return type comes from the function's `-> T` annotation. If the
function has no `-> T`, the return type is **unit** `()` and `return`
without a value is required (or implicit at end of body).

`return` from within an `if`/`match`/`for`/`loop` exits the
**enclosing function**, not just the construct.

## Statement vs. expression position

| Construct | Statement | Expression |
|---|---|---|
| `if` | yes (value discarded) | yes (yields branch tail) |
| `match` | yes (value discarded) | yes (yields arm tail) |
| `for` | yes only | no |
| `loop` | yes only | no |
| `return` | yes only | no |
| `break` / `continue` | yes only | no |

Use `if`/`match` as statements when you don't need their value. The
parser decides position based on context (the parser's
`_parse_statement` wraps if/match in `ExprStatement`).

## Cross-references

- [syntax.md](syntax.md) for the grammar
- [errors.md](errors.md) for `?=` narrowing inside `if`
- [memory.md](memory.md) for ownership in for-loop bodies
- [`docs/parser.md`](../parser.md) for parser implementation details
