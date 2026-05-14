"""Shared diagnostic rendering.

Every error type in the compiler (`LexerError`, `ParseError`, `NameError`,
`TypeError_`) ultimately formats itself as a string via
``format_diagnostic(message, span)``. The output is:

```
<file>:<line>:<col>: <message>
   <line-number> | <source line>
                 | <padding>^^^^
```

When the source file can't be opened (e.g. tests pass ``file="<input>"``
with inline source strings), the helper degrades gracefully to the
single-line ``<file>:<line>:<col>: <message>`` form, so existing
substring-based test assertions keep working.
"""

from __future__ import annotations

from .nodes import Span


def format_diagnostic(message: str, span: Span) -> str:
    """Render a diagnostic with file:line:col, source line, and a caret."""
    header = f"{span.file}:{span.start_line}:{span.start_col}: {message}"

    source_line = _read_source_line(span.file, span.start_line)
    if source_line is None:
        return header

    line_label = str(span.start_line)
    pad = " " * len(line_label)

    caret_offset = max(0, span.start_col - 1)
    if span.end_line == span.start_line:
        caret_len = max(1, span.end_col - span.start_col)
    else:
        caret_len = max(1, len(source_line) - caret_offset)

    return (
        f"{header}\n"
        f"  {line_label} | {source_line}\n"
        f"  {pad} | {' ' * caret_offset}{'^' * caret_len}"
    )


def _read_source_line(path: str, line_number: int) -> str | None:
    """Return line *line_number* (1-indexed) from *path*, or None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    idx = line_number - 1
    if 0 <= idx < len(lines):
        return lines[idx].rstrip("\n")
    return None
