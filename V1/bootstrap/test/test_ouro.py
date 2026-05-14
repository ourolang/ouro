"""Discover and run every Ouro test file in ``<repo>/test/``.

Each ``.ou`` file in ``test/`` is a test program:

- It compiles cleanly through the Ouro pipeline (lex → parse → resolve →
  typecheck → codegen) plus ``qbe`` and ``cc``.
- It runs to completion with **exit code 0** to signal success.
- If a sibling file ``<name>.expected`` exists, the program's stdout
  must match its contents byte-for-byte.

Tests are parametrized by file path so the pytest report names them
clearly (e.g. ``test_ouro[basic/arithmetic_int.ou]``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import (
    REPO_ROOT,
    capture_exe,
    compile_entry_file,
    requires_toolchain,
    run_exe,
)


_OURO_TESTS_ROOT = REPO_ROOT / "test"


def _discover_ouro_tests() -> list[Path]:
    """Return every .ou file under test/, sorted for stable order.

    Files whose stem starts with `_` are treated as multi-file support
    modules (imported by an adjacent entry file) and skipped — the
    entry file pulls them in via the loader.
    """
    if not _OURO_TESTS_ROOT.exists():
        return []
    return sorted(
        p for p in _OURO_TESTS_ROOT.rglob("*.ou") if not p.stem.startswith("_")
    )


def _test_id(path: Path) -> str:
    """Pytest test id: path relative to test/."""
    return str(path.relative_to(_OURO_TESTS_ROOT))


@requires_toolchain
@pytest.mark.parametrize(
    "ou_file",
    _discover_ouro_tests(),
    ids=_test_id,
)
def test_ouro(ou_file: Path, tmp_path: Path) -> None:
    """Compile, run, and (optionally) check stdout for one .ou test file.

    Goes through the module loader so multi-file programs (entry file
    plus its `import("./helper")` siblings) work end-to-end.
    """
    exe = compile_entry_file(ou_file, tmp_path)

    # Stdout match if .expected sibling exists; otherwise just exit-code 0.
    expected_path = ou_file.with_suffix(ou_file.suffix + ".expected")
    if expected_path.exists():
        actual = capture_exe(exe)
        expected = expected_path.read_text()
        assert actual == expected, (
            f"{ou_file.name}: stdout mismatch\n"
            f"  expected: {expected!r}\n"
            f"  actual:   {actual!r}"
        )
    else:
        rc = run_exe(exe)
        assert rc == 0, f"{ou_file.name}: expected exit code 0, got {rc}"
