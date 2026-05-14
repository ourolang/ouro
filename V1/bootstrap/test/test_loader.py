"""Tests for the module loader (path resolution, cycle detection,
cache).  End-to-end multi-file compilation is covered by file-based
tests under `test/ouro/modules/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.loader import Loader, LoaderError


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def test_loads_entry_with_no_imports(tmp_path: Path) -> None:
    entry = tmp_path / "main.ou"
    _write(entry, "fn main() -> i32:\n    return 0\n")

    mods = Loader().load_entry(entry)

    assert len(mods) == 1
    assert mods[0].name == ""  # entry has no prefix
    assert mods[0].path == entry.resolve()


def test_loads_relative_import(tmp_path: Path) -> None:
    _write(
        tmp_path / "main.ou",
        'helper = import("./helper")\n\nfn main() -> i32:\n    return helper.foo()\n',
    )
    _write(tmp_path / "helper.ou", "fn foo() -> i32:\n    return 0\n")

    mods = Loader().load_entry(tmp_path / "main.ou")

    # Deps come first; entry last.
    assert [m.name for m in mods] == ["helper", ""]


def test_cycle_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "a.ou",
        'b = import("./b")\nfn main() -> i32:\n    return 0\n',
    )
    _write(tmp_path / "b.ou", 'a = import("./a")\nfn foo() -> i32:\n    return 0\n')

    with pytest.raises(LoaderError, match="cycle"):
        Loader().load_entry(tmp_path / "a.ou")


def test_missing_relative_import_errors(tmp_path: Path) -> None:
    _write(
        tmp_path / "main.ou",
        'missing = import("./nope")\nfn main() -> i32:\n    return 0\n',
    )

    with pytest.raises(LoaderError, match="can't find imported file"):
        Loader().load_entry(tmp_path / "main.ou")


def test_diamond_compiles_dependency_once(tmp_path: Path) -> None:
    """A imports B and C; both B and C import D. D must be loaded once."""
    _write(
        tmp_path / "a.ou",
        (
            'b = import("./b")\n'
            'c = import("./c")\n'
            "fn main() -> i32:\n"
            "    return b.bv() + c.cv()\n"
        ),
    )
    _write(
        tmp_path / "b.ou",
        'd = import("./d")\nfn bv() -> i32:\n    return d.dv()\n',
    )
    _write(
        tmp_path / "c.ou",
        'd = import("./d")\nfn cv() -> i32:\n    return d.dv()\n',
    )
    _write(tmp_path / "d.ou", "fn dv() -> i32:\n    return 0\n")

    mods = Loader().load_entry(tmp_path / "a.ou")

    names = [m.name for m in mods]
    # d appears exactly once and comes before its importers.
    assert names.count("d") == 1
    assert names.index("d") < names.index("b")
    assert names.index("d") < names.index("c")
    assert names[-1] == ""  # entry last


def test_std_io_loaded_as_real_module(tmp_path: Path) -> None:
    """`import("std/io")` now resolves to the bundled `<repo>/std/io.ou`,
    so the loader returns it (plus its transitive runtime/syscalls
    dependency, auto-loaded) and the entry's imports map binds `io`
    to that module — no longer a legacy stub.
    """
    entry = tmp_path / "main.ou"
    _write(
        entry,
        'io = import("std/io")\n\nfn main() -> i32:\n    io.println("hi")\n    return 0\n',
    )

    mods = Loader().load_entry(entry)

    # Loader auto-loads runtime/*.ou (syscalls, _start) plus std/io
    # and its own runtime/syscalls import.  Order: deps before entry.
    names = [m.name for m in mods]
    assert "io" in names
    assert names[-1] == ""  # entry last
    entry_mod = mods[-1]
    assert "io" in entry_mod.imports
    assert entry_mod.imports["io"].name == "io"
