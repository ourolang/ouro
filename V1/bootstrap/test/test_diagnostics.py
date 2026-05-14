"""Tests for the shared diagnostic renderer in src/diagnostics.py."""

from pathlib import Path

from src.diagnostics import format_diagnostic
from src.nodes import Span


def _span(file: str, sl: int, sc: int, el: int, ec: int) -> Span:
    return Span(file=file, start_line=sl, start_col=sc, end_line=el, end_col=ec)


def test_format_with_source(tmp_path: Path) -> None:
    """When the file is readable, the diagnostic includes the source line + caret."""
    src_path = tmp_path / "ex.ou"
    src_path.write_text("fn main() -> i32:\n    return add(1)\n")
    span = _span(str(src_path), 2, 12, 2, 18)
    out = format_diagnostic("wrong number of arguments", span)
    assert "ex.ou:2:12: wrong number of arguments" in out
    assert "  2 |     return add(1)" in out
    assert "    |            ^^^^^^" in out


def test_format_without_source_path() -> None:
    """`<input>` (or any unreadable path) degrades to the single-line form."""
    span = _span("<input>", 1, 5, 1, 10)
    out = format_diagnostic("some message", span)
    # No source-line block — just the header.
    assert out == "<input>:1:5: some message"


def test_format_caret_length_matches_span_width(tmp_path: Path) -> None:
    """The caret line has exactly (end_col - start_col) carets when on one line."""
    src_path = tmp_path / "wide.ou"
    src_path.write_text("the quick brown fox\n")
    span = _span(str(src_path), 1, 5, 1, 10)
    out = format_diagnostic("test", span)
    # 5 carets: cols 5 through 9 inclusive (end is exclusive)
    assert "    |     ^^^^^" in out


def test_format_caret_min_one(tmp_path: Path) -> None:
    """A zero-width span still gets a caret (length 1)."""
    src_path = tmp_path / "zero.ou"
    src_path.write_text("abcdef\n")
    span = _span(str(src_path), 1, 3, 1, 3)
    out = format_diagnostic("zero-width", span)
    assert "    |   ^" in out


def test_format_line_out_of_range_graceful(tmp_path: Path) -> None:
    """A span referencing a non-existent line falls back to the header."""
    src_path = tmp_path / "short.ou"
    src_path.write_text("only line\n")
    span = _span(str(src_path), 999, 1, 999, 5)
    out = format_diagnostic("oops", span)
    assert "\n" not in out  # single-line fallback
    assert "short.ou:999:1: oops" in out
