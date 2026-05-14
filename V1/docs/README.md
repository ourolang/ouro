# Ouro documentation

Two flavors of doc live here:

- **[Language spec](spec/README.md)** — what Ouro looks like and what
  it means. Read this if you're using the language.
- **Compiler internals** (this folder) — how the lexer / parser /
  type checker / codegen are implemented. Read this if you're
  working on the compiler itself.

# Ouro compiler — internals documentation

This folder contains a deep dive on each file in the compiler's
front-to-back pipeline. The order below is the order data flows:

| File | Lines | Doc |
|---|---:|---|
| [`src/tokens.py`](../src/tokens.py) | 129 | [tokens.md](tokens.md) — token kinds, `Token` dataclass, the alphabet |
| [`src/lexer.py`](../src/lexer.py) | 628 | [lexer.md](lexer.md) — source bytes → tokens (with INDENT/DEDENT) |
| [`src/nodes.py`](../src/nodes.py) | 505 | [nodes.md](nodes.md) — AST node dataclasses (Types, Patterns, Expressions, Statements, Top-level) |
| [`src/parser.py`](../src/parser.py) | 1211 | [parser.md](parser.md) — tokens → AST, recursive descent + Pratt expressions |
| [`src/resolver.py`](../src/resolver.py) | 480 | [resolver.md](resolver.md) — name resolution, builds `ResolutionMap` |
| [`src/typechecker.py`](../src/typechecker.py) | 1108 | [typechecker.md](typechecker.md) — type inference, builds `TypeMap` |
| [`src/codegen.py`](../src/codegen.py) | 864 | [codegen.md](codegen.md) — typed AST → QBE IR text |
| [`src/diagnostics.py`](../src/diagnostics.py) | ~50 | Shared error renderer used by all error types (`LexerError`, `ParseError`, `NameError`, `TypeError_`). Produces caret-pointing diagnostics with source-line context. |

After codegen, the IR is piped to `qbe` → assembly → `cc` →
executable, with the C runtime in
[`runtime/runtime.c`](../runtime/runtime.c) linked in.

## How to read these docs

Each doc has the same shape:

1. **What this file is responsible for** — one paragraph.
2. **File structure** — table of contents with line ranges.
3. **Section-by-section walkthrough** — explanation of each
   significant function or block, with code excerpts and references
   to the actual source by line number.
4. **Subtle points / design decisions** — choices that matter for
   understanding the code or extending it.
5. **What this file does NOT do** — explicit list of
   non-responsibilities, often pointing at later passes.
6. **Cross-references** — links to related files and docs.
7. **Related tests** — pointers to the relevant test file and what
   it covers.

If you only have time to read one section per doc, read **"Subtle
points / design decisions"** and **"What this file does NOT do"** —
those carry the most you can't easily figure out from reading the
source directly.

## Pipeline diagram

```
                    ┌──────────────────────────────────────┐
   .ou source       │  src/lexer.py                        │
   text  ──────────▶│    LexerError on failure             │
                    │  list[Token]                         │
                    └──────────────────┬───────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  src/parser.py                       │
                    │    ParseError on failure             │
                    │  File AST                            │
                    └──────────────────┬───────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  src/resolver.py                     │
                    │  ResolutionMap (errors collected)    │
                    └──────────────────┬───────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  src/typechecker.py                  │
                    │  TypeMap (errors collected)          │
                    └──────────────────┬───────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  src/codegen.py                      │
                    │  QBE IR text                         │
                    └──────────────────┬───────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  qbe (external)                      │
                    │  Assembly text                       │
                    └──────────────────┬───────────────────┘
                                       │
                                       ▼
                    ┌──────────────────────────────────────┐
                    │  cc (external) + runtime/runtime.c   │
                    │  Executable                          │
                    └──────────────────────────────────────┘
```

## Testing each layer

The test tree splits along Python-vs-Ouro:

```
test/
├── python/        — pytest unit + integration tests for the compiler
│   ├── conftest.py     (shared compile/run helpers, runtime fixture)
│   ├── test_lexer.py
│   ├── test_parser.py
│   ├── test_resolver.py
│   ├── test_typechecker.py
│   ├── test_codegen.py
│   ├── test_e2e.py     (inline-source end-to-end smoke tests)
│   └── test_ouro.py    (parametrized over every test/ouro/*.ou file)
└── ouro/          — `.ou` programs that exit 0 on success
    ├── basic/      (literals, arithmetic, comparison, logical)
    ├── bindings/   (const, var, var[?], discard)
    ├── control_flow/  (if, match, loop, while, for)
    ├── functions/  (recursion, multi-arg, chains)
    ├── structs/    (fields, methods, ARC, __drop__)
    └── io/         (println, print, printf — sibling .expected for stdout)
```

| Layer | Test file | Approx. tests |
|---|---|---:|
| Lexer | [`test/python/test_lexer.py`](../test/python/test_lexer.py) | ~30 |
| Parser | [`test/python/test_parser.py`](../test/python/test_parser.py) | ~75 |
| Resolver | [`test/python/test_resolver.py`](../test/python/test_resolver.py) | ~25 |
| Type checker | [`test/python/test_typechecker.py`](../test/python/test_typechecker.py) | ~33 |
| Codegen (IR shape) | [`test/python/test_codegen.py`](../test/python/test_codegen.py) | ~45 |
| End-to-end (Python harness) | [`test/python/test_e2e.py`](../test/python/test_e2e.py) | ~20 |
| Ouro programs | [`test/ouro/`](../test/ouro/) discovered by [`test_ouro.py`](../test/python/test_ouro.py) | ~44 |

Each `.ou` file in `test/ouro/` is its own test:

- The program **must compile cleanly and exit with code 0** for the
  test to pass.
- If a sibling `<name>.ou.expected` exists, the program's stdout
  must match it byte-for-byte.

Add a new test by dropping a file into one of the subdirs — the
discovery harness picks it up automatically. Run all of them with
`make check` (also runs lint and type-check on the Python source).

## Diagnostic format

Every compiler error (lex, parse, name, type) renders through a single
helper in [`src/diagnostics.py`](../src/diagnostics.py):

```
/path/to/file.ou:5:12: wrong number of arguments: expected 2, got 1
  5 |     return add(1)
    |            ^^^^^^
```

The first line stays `file:line:col: message` for clickable
diagnostics in IDEs. When the file isn't readable (e.g. inline-source
tests pass `file="<input>"`), the renderer degrades to just that
first line so existing substring-based test assertions keep working.

## Conventions across files

A few patterns recur across passes — once you've seen them in one,
you'll spot them everywhere:

- **Errors are collected, not thrown.** Each pass has its own error
  type (`NameError`, `TypeError_`, etc.) stored in a list on the
  result object. The lexer and parser are exceptions — they raise
  `LexerError` / `ParseError` because there's no recovery yet.
- **Two-pass walks** for forward references. The resolver and type
  checker both do "register names first, then walk bodies" so a
  function can call another defined later in the file.
- **`id(node)`-keyed maps** for storing per-node info. `ResolutionMap`
  and `TypeMap` both use this — Python's object identity is fast,
  unique, and stable as long as the AST isn't reallocated.
- **Lenient unknown handling.** `UnknownTy` (and missing entries in
  `ResolutionMap`) are treated as "compatible with everything"
  downstream, to prevent cascade errors.
- **Pure dispatch via `isinstance`.** Each pass writes its own
  `if isinstance(node, X): ...` switch. Repetitive, but no virtual
  method indirection or visitor framework boilerplate.
- **`Span` everywhere.** Defined in `nodes.py`, used by tokens, AST
  nodes, and every error type. One source-location story.
