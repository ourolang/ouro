# Conventions

Naming, privacy, dunders, comments, and struct construction. These
are **locked design decisions** that shape how Ouro code looks and
how it interacts with the compiler's special handling.

🟢 *All decisions in this doc are locked.*

## Comments

Only **line comments** with `#`:

```ouro
# This is a comment
x = 42  # inline comment, also fine
```

There are **no block comments** (no `/* ... */`). Multi-line "comments"
are multiple `#` lines:

```ouro
# This block of explanation
# spans several lines.
# Each line starts with #.
fn f(): pass
```

The tradeoff: block comments are convenient for temporarily disabling
chunks of code, but they're also a source of nesting bugs (`/*` inside
`/*`). Single-line comments are unambiguous and that's worth more than
the convenience.

## Naming convention

Three categories distinguished by underscore prefixes:

### `name` — public

```ouro
struct Foo:
    bar: i32                # public field
    fn baz(self): ...       # public method

x = 42                      # public name
```

Default visibility. Accessible from anywhere the symbol is in scope.

### `_name` — private

```ouro
struct LinkedList[T]:
    _head: var[_Node[T] | Null]   # private field
    fn _check_invariant(self): ...     # private method

struct _Node[T]:                       # private type
    value: T
    next: weak[_Node[T]]
```

A leading underscore marks an identifier as **strict struct-private**.
The compiler enforces:

- A `_field` is **only accessible from within the declaring struct's
  methods.** Other structs can't read or write it. Free functions in
  the same module can't read or write it.
- A `_method` is **only callable from within the declaring struct's
  methods.**
- A top-level `_name` (function, struct, or constant) is **module-
  private** — only accessible within the file.

🔵 *Open.* The compiler doesn't yet enforce these privacy rules.
The convention is locked; the enforcement is on the deferred list.

The choice of "leading underscore = private" matches Python and
several other languages. It avoids the visual noise of `pub`/`priv`
keywords on every public item. The downside (you can't tell at a
glance whether a `_name` is "really" private vs. "convention only")
is a non-issue once the compiler enforces it.

### `__name__` — reserved dunder

```ouro
struct Foo:
    fn __drop__(self): ...
    fn __iter__(self): ...
```

A name **surrounded** by double underscores (`__name__`) is reserved
for **special meaning** to the compiler or runtime. User code
generally shouldn't define dunders that aren't part of the locked
list (see [declarations.md](declarations.md#dunder-methods)).

### `__name` (leading double, no trailing) — REJECTED

```ouro
__foo                    # ERROR at lex time
```

The lexer rejects `__name` (double leading underscore, no trailing
`__`) with an explicit error: "ambiguous naming convention: use
`_name` for private or `__name__` for dunder."

The reason: in some languages this is mangling; in others it's
private; in others it's reserved. To avoid the foot-gun, Ouro forces
you to pick one of the unambiguous forms.

## Privacy enforcement (planned)

🔵 *Open status.* The compiler will check privacy at the resolver or
type-checker level. Today the convention is documented but
unenforced — `obj._private_field` reads will succeed.

When implemented:

- **Struct-private (`_field`/`_method`)**: lookup fails when the
  call site isn't in a method of the declaring struct.
- **Module-private (`_top_level_name`)**: lookup fails across
  module boundaries (i.e. through an `import("...")`).

## Struct construction — Rust-style

🟢 *Locked.*

Structs are constructed with **brace literals**:

```ouro
p = Point { x: 1, y: 2 }
empty = Foo {}                    # all fields zero-initialized via __zeroed__
```

The braces use `{ field: value, ... }` syntax. Field order in the
literal **doesn't have to match declaration order**:

```ouro
struct Person:
    name: []u8
    age: i32

p = Person { age: 30, name: "alice" }    # ok
```

### Why braces?

The alternatives considered:

- `Point(1, 2)` — call-style. Confuses with constructor functions.
- `Point[x: 1, y: 2]` — bracket-style. Clashes with generic
  instantiation `Point[T]`.
- `Point { x: 1, y: 2 }` — Rust-style. Visually clear, no clash.

Braces also visually echo the runtime layout: `{ field, field, field }`.

### Empty struct literal `Foo {}`

🟢 *Locked.*

`Foo {}` calls `Foo.__zeroed__()` to produce a default-initialized
instance. Every field gets its type's default value (zero for
numerics, empty for slices, Null for weak refs, etc.).

Used commonly for placeholder construction in tests or builder
patterns.

🟡 *Status:* `Foo {}` parses and emits an alloc, but the field
zeroing relies on `malloc` returning zeroed memory — which it
doesn't. Today this leaves uninitialized memory. The compiler-emitted
zeroing or `__zeroed__` invocation is on the deferred list.

## Method receivers — `self` conventions

```ouro
struct Foo:
    fn read(self):                        # bare self = ptr[Self]
    fn read_explicit(self: ptr[Self]):    # equivalent
    fn write(self: ptr[var[Self]]):       # mutable view
    fn static_method() -> Self:           # static — no self
```

See [memory.md](memory.md#default-vs-explicit-self) for the runtime
implications.

The bare-`self` default is **always `ptr[Self]`** — never `Self` (a
copy) or `var[Self]` (a mutable view). To mutate fields you must
explicitly request `ptr[var[Self]]`.

## Statement separator — `;`

🟢 *Locked.*

`;` is an **optional same-line separator**:

```ouro
if cond: a = 1; b = 2; c = 3
```

You can use `;` between statements anywhere a NEWLINE would be
accepted. Most code uses one statement per line.

`;` is **never required**. Statements end with newlines.

## Indentation

🟢 *Locked.*

- **Spaces only** — tabs are rejected with a lexer error.
- **Any positive width works** as long as it's consistent within
  nested blocks.
- 4-space indentation is the strong convention (matches all examples
  in `examples/`).

The lexer doesn't enforce a specific width — `2`, `4`, and `8`-space
indentation all work. Mixing within nested blocks is the issue: once
you've opened a block at, say, 4 spaces, the body must continue at
that level until dedenting.

## Identifier naming style

The convention (not enforced):

| Item | Style | Example |
|---|---|---|
| Function | `snake_case` | `parse_int`, `for_each` |
| Struct (and tagged union variant) | `PascalCase` | `LinkedList`, `ParseError` |
| Top-level constant | `SCREAMING_SNAKE_CASE` (for true constants) or `snake_case` | `PI`, `default_port` |
| Local binding | `snake_case` | `count`, `result` |
| Type parameter | single uppercase letter or `PascalCase` | `T`, `Key`, `Value` |
| Dunder | `__name__` | `__drop__`, `__iter__` |
| Private | leading `_` (still snake or PascalCase) | `_head`, `_Node` |

The compiler doesn't care; readers do.

## File naming

`.ou` extension. The filename relates to the import path:

```ouro
io = import("std/io")          # imports std/io.ou
util = import("./util")        # imports ./util.ou
```

🟢 *Locked.* No special file names like `mod.ou` or `__init__.ou`.

## What's NOT a convention

- **Newlines around `:`**: not required. `if x:\n    pass` and
  `if x: pass` are both fine.
- **Trailing commas in argument lists**: allowed, not required.
- **Spaces around operators**: not enforced. `x+1` and `x + 1` both
  parse.
- **One declaration per line**: not enforced. `;` separators are
  legal.

These are formatter concerns, not language concerns.

## Cross-references

- [syntax.md](syntax.md) for the lexical rules these conventions
  build on
- [declarations.md](declarations.md) for dunder semantics and
  struct definition
- [memory.md](memory.md) for receiver semantics
- [errors.md](errors.md) for error-type naming
