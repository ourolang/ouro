# Ouro compiler (in Ouro)

The self-hosted compiler lives here.  Empty for now — the bootstrap
(Python) compiler under [`../bootstrap/`](../bootstrap/) is what
compiles every `.ou` file today, including (eventually) this directory.

## Plan

The bootstrap exists so we can write the compiler in Ouro itself.
Order of porting, roughly:

1. **Lexer** (`lexer.ou`).  ~700 lines in Python; iterates bytes,
   builds a `Vec[Token]`.  Probably the first pass to fully self-host.
2. **AST + parser** (`nodes.ou`, `parser.ou`).  ~2,000 lines.
3. **Resolver** (`resolver.ou`).  ~600 lines.  Symbol tables —
   uses `StringMap[Symbol]` from `std/map`.
4. **Typechecker** (`typechecker.ou`).  ~1,400 lines.
5. **Codegen** (`codegen.ou`).  ~3,000 lines.  Emits QBE IR text.

When all five pass the same `test/ouro/` corpus that the bootstrap
already passes, we're self-hosted: the Ouro compiler can compile
its own source, drop the Python dependency, and we move forward
from there.

## Building

Once any source file lives here, the existing `make build
FILE=compiler/main.ou` (or similar) will compile it through the
bootstrap.  No new wiring needed — the bootstrap doesn't care
which directory the source lives in.
