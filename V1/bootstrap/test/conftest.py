"""Shared pytest fixtures + helpers for the test suite.

The compile / link helpers and the runtime fixture live here so both
the inline-source e2e tests (``test_e2e.py``) and the file-discovery
ouro suite (``test_ouro.py``) can use them without duplication.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.loader import compile_program


# ── Toolchain detection ──────────────────────────────────────────────────────

QBE = shutil.which("qbe")
CC = shutil.which("cc")
TOOLS_OK = QBE is not None and CC is not None

# This file lives at `<repo>/bootstrap/test/conftest.py`, so three
# parents up is the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_ROOT = REPO_ROOT / "std" / "runtime"

requires_toolchain = pytest.mark.skipif(
    not TOOLS_OK, reason="qbe and cc must be on PATH"
)


# Flags shared with the final link.  Freestanding, no stack protector,
# no PIE: the runtime is now pure Ouro (printf, ARC, allocator, _start
# all live in std/runtime/*.ou) and supplies its own _start.
_FREESTANDING_CC_FLAGS = [
    "-O2",
    "-ffreestanding",
    "-fno-stack-protector",
    "-fno-builtin",
    "-fno-pic",
    "-fno-pie",
    "-no-pie",
]


# ── Compile / run helpers ────────────────────────────────────────────────────


def compile_source(
    source: str,
    name: str,
    tmp_path: Path,
    extra_objects: list[Path] | None = None,
) -> Path:
    """Compile *source* end-to-end through the Ouro pipeline + qbe + cc.

    Asserts there are no resolver or type-checker errors.  Returns the
    path of the built executable.  Goes through the same loader path
    as file-based tests so `runtime/*.ou` files are auto-included
    (the runtime now lives there, not in compile_source's direct
    lex/parse path).
    """
    entry = tmp_path / f"{name}.ou"
    entry.write_text(source)
    return compile_entry_file(entry, tmp_path, extra_objects)


def compile_entry_file(
    entry: Path,
    tmp_path: Path,
    extra_objects: list[Path] | None = None,
) -> Path:
    """Like *compile_source*, but reads an entry `.ou` file from disk
    and recursively loads its imports through the module loader.  Use
    this for multi-file tests; for single-file tests `compile_source`
    is sufficient (and slightly faster).
    """
    ir, asm_sidecar = compile_program(entry, runtime_root=RUNTIME_ROOT)
    return _assemble_and_link(
        ir, asm_sidecar, entry.stem, tmp_path, extra_objects
    )


def _assemble_and_link(
    qbe_ir: str,
    asm_sidecar: str,
    name: str,
    tmp_path: Path,
    extra_objects: list[Path] | None,
) -> Path:
    """Common back end: write QBE IR, run qbe, append the asm sidecar
    to the produced assembly file, then link with `cc`.
    """
    assert QBE is not None and CC is not None
    ssa = tmp_path / f"{name}.ssa"
    asm = tmp_path / f"{name}.s"
    exe = tmp_path / name
    ssa.write_text(qbe_ir)

    subprocess.run([QBE, "-o", str(asm), str(ssa)], check=True)
    if asm_sidecar:
        # Append the asm bodies from `asm fn` decls to QBE's output so
        # the assembler sees one combined source.
        with asm.open("a") as f:
            f.write("\n")
            f.write(asm_sidecar)
            f.write("\n")

    cc_cmd = [CC, *_FREESTANDING_CC_FLAGS, "-static", "-nostdlib",
              "-o", str(exe), str(asm)]
    for obj in extra_objects or []:
        cc_cmd.append(str(obj))
    subprocess.run(cc_cmd, check=True)
    return exe


def run_exe(exe: Path) -> int:
    """Run *exe* and return its exit code."""
    return subprocess.run([str(exe)]).returncode


def capture_exe(exe: Path) -> str:
    """Run *exe* and capture its stdout as a string."""
    return subprocess.run([str(exe)], capture_output=True, text=True).stdout
