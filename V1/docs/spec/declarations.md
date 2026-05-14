# Declarations

Top-level Ouro source consists of four kinds of declaration:
**functions**, **structs**, **imports**, and **top-level bindings
(constants)**. This doc walks through each.

## Functions

```ouro
fn name[generics](params) -> return_type:
    body
```

All four parts after the name are optional:

```ouro
fn add(a: i32, b: i32) -> i32:        # full form
    return a + b

fn greet():                            # no params, no return type
    io.println("hi")

fn id[T](x: T) -> T:                   # generic
    return x
```

### Parameters

```ouro
fn f(a: i32, b: var[i64]):    # `b` is a mutable slot
    ...
```

Each parameter is `name: type`. Types can include any wrapper
(`var[T]`, `const[T]`, `weak[T]`, `ptr[T]`).

### Return type

`-> T` declares a return type. If omitted, the function returns
**unit** (no value):

```ouro
fn log(s: []u8):
    io.println(s)
    # implicit return
```

Returning a value requires `return expr`. Returning early without a
value uses `return`.

### Generic parameters

```ouro
fn map[T, U](xs: []T, f: T -> U) -> []U: ...
fn make_list[T]() -> LinkedList[T]: ...
```

🟢 *Status:* Generic free functions monomorphize end-to-end — the
codegen infers the substitution from each call site's arg types (or
honors explicit type args like `id[i32](42)`) and emits one spec per
unique instantiation.

## Structs

```ouro
struct Name[generics]:
    field1: type
    field2: type

    fn method(self):
        ...

    fn static_method() -> Self:
        ...
```

A struct declares **fields** (data) and **methods** (functions).
They can be interleaved in source; the parser separates them
internally.

### Empty struct

```ouro
struct Empty:
    pass
```

`pass` is required as the body content for an empty struct (otherwise
it'd be a parse error — the body must have at least one item).

### Field types

Fields can use any type, including wrappers:

```ouro
struct Connection:
    host: []u8                          # immutable
    port: i32
    open: var[bool]                     # mutable
    pool: weak[ConnectionPool]          # weak ref
```

Reading a `var[T]` field gives `T`. Writing to it through the
container requires the container itself to be `var[Self]` or
accessed via a `ptr[var[Self]]`.

### Struct construction

```ouro
p = Point { x: 1, y: 2 }
empty = Foo {}                        # zero-initialized via __zeroed__
```

Field order in the literal **doesn't have to match declaration
order**. The fields are listed by name.

🟡 *Status:* Field-order-independent construction works in the
type checker; the codegen emits stores in *declaration* order
regardless of the literal's order.

### Struct generics

```ouro
struct LinkedList[T]:
    head: var[_Node[T] | Null]

    fn new() -> Self:
        return Self { head: Null }

    fn push(self: ptr[var[Self]], v: T):
        ...

list = LinkedList[i32].new()
```

🟡 *Status:* Same as generic functions — parse and typecheck, no
codegen yet.

## Methods

A method is a function declared inside a struct body. The first
parameter is `self`.

### Bare `self` — the default receiver

```ouro
struct Foo:
    fn read(self):                # bare — equivalent to `self: ptr[Self]`
        ...
```

Bare `self` is sugar for `self: ptr[Self]` — a read-only pointer to
the struct. To mutate fields:

### Explicit `self`

```ouro
struct Foo:
    x: var[i32]

    fn modify(self: ptr[var[Self]]):
        self.x = self.x + 1
```

`self: ptr[var[Self]]` says "I get a mutable view of the struct."
Other forms are possible (`self: ptr[const[Self]]`, etc.) but rarely
useful.

### Static methods (no `self`)

A method **without** a `self` parameter is **static** — called on
the type, not an instance:

```ouro
struct LinkedList[T]:
    fn new() -> Self:                  # static
        return Self { head: ... }

    fn push(self: ptr[var[Self]], v: T):
        ...

list = LinkedList[i32].new()           # static call
list.push(42)                          # instance call
```

There is **no `static` keyword** — the presence/absence of `self`
is the only marker.

### Dunder methods

Methods named `__name__` (full dunder) are **special** — invoked by
the language at specific points. The locked dunders are:

| Dunder | When invoked | Purpose |
|---|---|---|
| `__drop__(self)` | refcount → 0 | Run cleanup before free |
| `__iter__(self) -> Iter` | start of `for x in self` | Get an iterator |
| `__next__(self) -> T \| StopIteration` | each `for` iteration | Yield next element |
| `__hash__(self) -> u64` | `hash[Struct](v)` intrinsic | Hash key for `HashMap` etc. |
| `__eq__(self, other: Self) -> bool` | `eq[Struct](a, b)` intrinsic | Equality for `HashMap` etc. |
| `__getitem__(self, k) -> V` | `obj[k]` indexing read | User-defined indexing |
| `__setitem__(self, k, v)` | `obj[k] = v` indexing write | User-defined indexing |
| `__zeroed__() -> Self` | empty struct literal `Foo {}` | Default values |
| `__str__(self) -> []u8` | string conversion (planned) | Render as bytes |

`__get__(self, name: []u8) -> V` and `__set__(self, name: []u8, v: V)`
are **not** special — they're just naming conventions for runtime
field-by-name access. Compilers don't fall through to them on
unknown `.field` lookups; users call them explicitly.

The grammar defines these as conventions; the runtime/compiler hook
into the actual mechanism.

🟡 *Status:* `__drop__` is parseable but not yet invoked.
`__iter__`/`__next__` are spec'd but for-loops over user types don't
yet desugar to them. `__hash__`/`__eq__` are wired through the
`hash[T]`/`eq[T]` comptime intrinsics. `__getitem__`/`__setitem__`
are wired — `obj[k]` and `obj[k] = v` desugar to them when the
receiver is a struct.  `__zeroed__` is spec'd but not yet emitted.

## Imports

🟢 *Locked.*

```ouro
io = import("std/io")
math = import("std/math")
util = import("./util")        # local file
```

Imports are **comptime**. The `import("path")` form returns a "module
struct" that you bind to a name. After import:

- `io.println(...)` — call function `println` from the module
- `math.PI` — read constant `PI` from the module
- `util.Foo` — refer to a type `Foo` from the module

### Path resolution

```
std/io          → built-in standard library
./relative      → file relative to the importer
/absolute       → file relative to project root (planned)
foo/bar         → first match in the search path
```

🔵 *Open.* Path resolution rules aren't fully implemented. Today,
imports register the binding but don't actually load anything — the
codegen lowers `obj.field` to `$field` when `obj` is a module, and
relies on the linker to resolve.

### Files are namespaces

🟢 *Locked.*

A `.ou` file is a **namespace**, not a "type." Top-level
declarations in the file are accessible as members of the imported
binding:

```ouro
# in std/io.ou
fn println(s: []u8): ...
fn printf(fmt: []u8, ...): ...           # variadic intrinsic

# in your code
io = import("std/io")
io.println("hello")
io.printf("%d\n", 42)
```

There's **no folder magic**: importing `"std"` doesn't auto-import
all files inside `std/`. You import each file explicitly.

### No selective imports

You can't write `from std/io import println`. The whole module is
bound under a single name. This is deliberate — it keeps the call
site explicit (`io.println` rather than `println`).

### No cycles

🔵 *Open.* Cycles between modules (`a.ou` imports `b.ou` imports
`a.ou`) are not yet detected. v1 will reject them at compile time.

## Top-level bindings (constants)

```ouro
PI: const[f64] = 3.14159
DEFAULT_PORT = 8080                  # type inferred to isize
GREETING: []u8 = "hello, ouro!"
```

A top-level binding is a **module-level constant**. Optional type
annotation, optional `const[T]` wrapper.

🟡 *Status:* Top-level bindings parse and typecheck. The codegen
does not yet emit them as data-section constants — they're skipped.
Use them inside function bodies for now.

🔵 *Open.* Whether top-level `var[T]` is allowed (mutable globals)
is undecided. The current spec is "constants only at top level."

## Order independence

🟢 *Locked.*

Top-level declarations are **order-independent**:

```ouro
fn main() -> i32:
    return helper()           # ok — helper is below

fn helper() -> i32:
    return 42
```

The resolver and type checker each do a two-pass walk: pass 1
registers every top-level name; pass 2 checks bodies. By the time
`main`'s body is checked, `helper` is in scope.

This applies to functions, structs, and imports. For top-level
bindings, the value's expression is evaluated in pass 2 — it can
reference functions and structs declared anywhere in the file.

## Cross-references

- [types.md](types.md) for type syntax inside declarations
- [memory.md](memory.md) for `var`/`const`/`weak`/`ptr` field
  semantics and `__drop__`
- [conventions.md](conventions.md) for the privacy rules and dunder
  conventions
- [`docs/parser.md`](../parser.md) for the grammar implementation
