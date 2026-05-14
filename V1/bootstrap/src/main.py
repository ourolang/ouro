#!/usr/bin/env python3
"""Ouro compiler CLI.

Usage:
    ouro <file.ou>                  # write QBE IR to stdout
    ouro <file.ou> --asm-out PATH   # also write asm sidecar to PATH

The compiler goes through the module loader so `import("./X")` resolves
recursively and `runtime/*.ou` files (auto-detected by walking up from
*file.ou*) are linked into every program.  When the program uses `asm`
declarations, the assembly bodies are emitted into a sidecar file
provided via `--asm-out`; otherwise that file is empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .loader import LoaderError, compile_program


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: ouro <file.ou> [--asm-out PATH]", file=sys.stderr)
        return 1

    input_path = Path(args[0])
    asm_out: Path | None = None
    i = 1
    while i < len(args):
        if args[i] == "--asm-out" and i + 1 < len(args):
            asm_out = Path(args[i + 1])
            i += 2
        else:
            print(f"error: unrecognized arg {args[i]!r}", file=sys.stderr)
            return 1

    try:
        qbe_ir, asm_sidecar = compile_program(input_path)
    except LoaderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    sys.stdout.write(qbe_ir)
    if asm_out is not None:
        asm_out.write_text(asm_sidecar)
    elif asm_sidecar.strip():
        print(
            "warning: program uses `asm` declarations but no --asm-out path "
            "was given; the assembly bodies will not be linked",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
